from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.support import has_claude_sdk, requires_claude_sdk

if has_claude_sdk:
    from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
    from netizen_cli.runtime.claude_activity import ClaudeChecklist, ClaudeTurnTools
    from netizen_cli.runtime.claude_client import validate_fields
from netizen_cli.turn_activity import TurnActivityStatus as Status


def frames(case):
    path = Path(__file__).parent / "fixtures" / "claude_tools_sdk_0_2_186_cli_2_1_329.jsonl"
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row["case"] != case:
            continue
        if row["class"] == "ResultMessage":
            yield Status.INTERRUPTED if row["terminal_reason"] == "aborted_tools" else Status.COMPLETED
        else:
            blocks = []
            for block in row["content"]:
                cls = ToolUseBlock if block.pop("class") == "ToolUseBlock" else ToolResultBlock
                blocks.append(cls(**block))
            parent = row["parent_tool_use_id"]
            if row["class"] == "AssistantMessage":
                yield AssistantMessage(blocks, "fixture-model", parent_tool_use_id=parent)
            else:
                message = UserMessage(blocks, parent_tool_use_id=parent, tool_use_result=row["tool_use_result"])
                validate_fields("user", message, "fixture-session")
                yield message


def replay(case, *, end=None):
    checklist = ClaudeChecklist()
    turn = ClaudeTurnTools(checklist, 120)
    events, plans, results = [], [], []
    for now, frame in enumerate(frames(case), start=1):
        if isinstance(frame, AssistantMessage):
            events.extend(turn.assistant(frame, now))
        elif isinstance(frame, UserMessage):
            events.extend(turn.user(frame, now))
            plans.append(None if checklist.rows is None else [(row.step, row.status.value) for row in checklist.rows.values()])
        else:
            closed, evidence = turn.finish(end or frame, now)
            events.extend(closed)
            results.append([o.path for o in evidence.outputs])
            turn = ClaudeTurnTools(checklist, 120)
    return events, plans, results


@requires_claude_sdk
class ClaudeToolsTest(unittest.TestCase):
    def test_top_level_start_end_and_effective_files(self):
        events, _, outputs = replay("ordinary")
        self.assertEqual([e.kind.value for e in events], ["command"] * 2 + ["fileChange"] * 4)
        self.assertEqual([e.status for e in events], [Status.IN_PROGRESS, Status.COMPLETED] * 3)
        self.assertEqual(outputs, [["example.txt", "example.txt"]])
        self.assertEqual(replay("write-rewrite")[2], [["actual.txt"]])
        self.assertEqual(replay("notebook")[2], [["fixture.ipynb"]])
        self.assertEqual(replay("notebook-error")[2], [[]])
        for status in (Status.FAILED, Status.INTERRUPTED):
            self.assertEqual(replay("write-rewrite", end=status)[2], [[]])
        self.assertEqual(replay("denied")[0][-1].status, Status.FAILED)
        self.assertEqual(replay("interrupt")[0][-1].status, Status.FAILED)

    def test_child_frames_and_late_children_do_not_change_a_successor(self):
        for case in ("child", "late-child"):
            events, _, outputs = replay(case)
            self.assertEqual([e.kind.value for e in events], ["subagent", "subagent"])
            self.assertTrue(all(e.text is None and e.count == 1 for e in events))
            self.assertFalse(any(outputs))
        checklist = ClaudeChecklist()
        turn = ClaudeTurnTools(checklist, 120)
        turn.assistant(AssistantMessage([ToolUseBlock("outer", "Agent", {})], "model"), 1)
        turn.finish(Status.COMPLETED, 2)
        successor = ClaudeTurnTools(checklist, 120)
        child = AssistantMessage([ToolUseBlock("child", "TaskUpdate", {})], "model", parent_tool_use_id="outer")
        checklist.observe_child(child)
        self.assertIsNone(checklist.rows)
        self.assertEqual(successor.assistant(child, 3), ())

    def test_checklist_uses_effective_results_and_full_baselines(self):
        self.assertEqual(replay("todo")[1][-1], [])
        self.assertEqual(replay("todo-rewrite")[1], [[("Item three", "completed")]] * 4)
        self.assertEqual(replay("subject-rewrite")[1][:4], [
            [("Prepare fixture", "pending")], None, None, [("Item three", "completed")],
        ])
        self.assertEqual(replay("tasks")[1][:4], [
            [("Prepare fixture", "pending")], [("Prepare fixture", "inProgress")],
            [("Prepare fixture", "inProgress")], [("Prepare fixture", "inProgress")],
        ])
        self.assertEqual(replay("missing")[1], [[], [], []])
        checklist = ClaudeChecklist()
        checklist.apply("TaskCreate", {"task": {"id": "one", "subject": "First"}})
        checklist.apply("TaskUpdate", {"success": True, "taskId": "one", "updatedFields": ["status"]})
        self.assertEqual(checklist.rows["one"].status.value, "pending")
        checklist.apply("TaskUpdate", {"success": True, "taskId": "one", "updatedFields": ["status"],
                                       "statusChange": {"to": "deleted"}})
        self.assertEqual(checklist.rows, {})
        checklist.apply("TaskUpdate", {"success": True, "taskId": "absent", "updatedFields": []})
        self.assertEqual(checklist.rows, {})
        checklist.apply("TodoWrite", {"newTodos": []})
        self.assertEqual(checklist.rows, {})
        checklist.apply("TodoWrite", {"todos": []})
        self.assertIsNone(checklist.rows)

    def test_missing_metadata_has_local_display_effects_and_end_requires_proof(self):
        turn = ClaudeTurnTools(ClaudeChecklist(), 50)
        result = turn.assistant(AssistantMessage([
            ToolUseBlock("r", "Read", {"file_path": 42}),
            ToolUseBlock("m", "mcp__PRIVATE_FIXTURE", {"secret": "PRIVATE_FIXTURE"}),
            ToolUseBlock("w", "Write", {"file_path": "requested", "content": "PRIVATE_FIXTURE"}),
        ], "model"), 20)
        self.assertEqual([row.text for row in result], ["读取文件", None, "写入文件 · requested"])
        self.assertEqual(turn.user(UserMessage([ToolResultBlock("other")]), 30), ())
        turn.user(UserMessage([ToolResultBlock("w")], tool_use_result={}), 50)
        closed, files = turn.finish(Status.INTERRUPTED, 90)
        self.assertEqual([(row.status, row.event_timestamp_ms) for row in closed], [(Status.INTERRUPTED, 90)] * 2)
        self.assertEqual(files.outputs, ())

    def test_ordered_reader_identity_validation_is_independent_of_display_shapes(self):
        for message in (UserMessage("text"), UserMessage([ToolResultBlock("call", is_error=True)], tool_use_result="denied")):
            validate_fields("user", message, "session")
        for message in (UserMessage(15), UserMessage([ToolResultBlock("")]),
                        UserMessage([ToolResultBlock("call", is_error=0)]), UserMessage("text", parent_tool_use_id=1)):
            with self.assertRaises(ValueError):
                validate_fields("user", message, "session")
