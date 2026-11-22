"""Pinned public Claude frame families and their expected delivery and control identities."""
from dataclasses import dataclass
from uuid import UUID

from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from netizen_cli.runtime.claude_client import CONSUMED_FIELDS


def uid(number):
    return str(UUID(int=number, version=4))


@dataclass(frozen=True)
class Family:
    frames: tuple
    results: tuple
    closes: bool = True


HUMAN_START = ("running", "human", "tool_a", "members_a", "start_a")
HUMAN_RESULT = ("stop_a", "first", "idle")
WAKE_RESULT = ("end_a", "empty", "wake_a", "stop_empty_a", "answer_a", "idle")
TWO_TASKS = HUMAN_START + ("tool_b", "members_ab", "start_b", "stop_ab", "first", "idle")
BATCH_WAKE = ("end_b", "members_a", "end_a", "empty", "wake_b", "stop_empty_b", "answer_b",
         "wake_a", "stop_empty_a", "answer_a", "idle")

FAMILIES = {
    "single": Family(HUMAN_START + HUMAN_RESULT + WAKE_RESULT, ("first", "answer_a")),
    "duplicates": Family(HUMAN_START + HUMAN_RESULT + ("start_a", "human") + WAKE_RESULT[:2] + ("end_a",) + WAKE_RESULT[2:], ("first", "answer_a")),
    "late_membership": Family(("running", "human", "tool_a", "initial_members", "start_a") + HUMAN_RESULT + WAKE_RESULT[:2]
                              + ("repeat_members",) + WAKE_RESULT[2:], ("first", "answer_a")),
    "foreign": Family(("running", "human", "foreign_tool", "members_a", "start_a") + HUMAN_RESULT + WAKE_RESULT, ("first",), False),
    "batch": Family(TWO_TASKS + BATCH_WAKE, ("first", "answer_b", "answer_a")),
    "reverse": Family(TWO_TASKS + ("end_b", "members_a", "wake_b", "stop_a_b", "answer_b", "idle") + WAKE_RESULT,
                      ("first", "answer_b", "answer_a")),
    "crossing": Family(HUMAN_START + ("end_a", "empty", "stop_empty_h", "first", "idle", "wake_a",
                            "old_stop", "cron", "stop_empty_a", "answer_a", "idle"), ("first", "answer_a"), False),
    "early_duplicate_stop": Family(HUMAN_START + ("end_a", "empty", "stop_empty_h", "old_stop", "first", "idle", "wake_a",
                                       "cron", "stop_empty_a", "answer_a", "idle"), ("first", "answer_a"), False),
    "mixed_inventory": Family(("running", "human", "mixed_inventory"), (), False),
    "unpaired": Family(("running", "human", "unpaired"), (), False),
    "interrupted": Family(("running", "human", "aborted", "idle"), ("interrupted",)),
    "tool_identity_only": Family(("running", "human", "bare_tool", "members_a", "start_a") + HUMAN_RESULT
                                + WAKE_RESULT[:3] + ("bare_tool",) + WAKE_RESULT[3:], ("first", "answer_a")),
    "late_tool": Family(HUMAN_START + HUMAN_RESULT + ("tool_a",) + WAKE_RESULT, ("first", "answer_a")),
    "repeat_stop": Family(("running", "human", "stop_empty_h", "old_stop", "first", "idle"), ("first",)),
    "plain": Family(("running", "human", "stop_empty_h", "first", "idle"), ("first",)),
    "slow_batch": Family(TWO_TASKS + BATCH_WAKE[:5] + ("slow_wake",) + BATCH_WAKE[5:], ("first", "answer_b", "answer_a")),
    "close_timeout": Family(HUMAN_START + HUMAN_RESULT + WAKE_RESULT, ("first", "answer_a")),
    "held_notice": Family(HUMAN_START + ("malformed_stop", "first", "idle") + WAKE_RESULT, ("first", "answer_a"), False),
    "lost_write": Family(("running", "human", "slow_human", "stop_empty_h", "first", "idle"), ("first",)),
    "deadline": Family(HUMAN_START + HUMAN_RESULT + ("end_a", "empty", "idle", "quota_window") + WAKE_RESULT[2:], ("first", "answer_a")),
    "dropped_list": Family(("running", "human", "members_a", "empty", "stop_empty_h", "first", "idle"), ("first",), False),
    "failure": Family(("running", "human", "failed", "idle"), ("failed",)),
    "restart_replay": Family(("running", "replay_result", "human", "stop_empty_h", "first", "idle"), ("first",)),
    "detached": Family(("running", "human", "tool_a", "unpaired"), (), False),
    "physical": Family(HUMAN_START + HUMAN_RESULT + ("running", "wake_b", "wake_a"), ("first",), False),
    **{f"conflict_{key}": Family(HUMAN_START + HUMAN_RESULT + WAKE_RESULT[:3] + (f"conflict_{key}",) + WAKE_RESULT[3:],
                                  ("first", "unknown_a"), False)
       for key in ("cwd", "permissionMode", "session_id", "user_message_uuids")},
}


def emit(client, frame):
    human = client.inputs[0]["uuid"]
    suffix = frame[-1]
    if frame in ("human", "wake_a", "wake_b") or frame.startswith("conflict_"):
        wake = frame != "human"
        fields = dict(uuid=uid(11 if frame == "human" else 13 if frame == "wake_b" else 12),
                      cwd=str(client.cwd), permissionMode="auto", user_message_uuids=[] if wake else [human])
        if frame.startswith("conflict_"):
            field = frame.removeprefix("conflict_")
            fields[field] = [human] if field == "user_message_uuids" else "different"
        client.system("init", **fields)
    elif frame == "bare_tool":
        client.stream.put_nowait(AssistantMessage([ToolUseBlock("tool-a", "Bash", {"run_in_background": True})],
                                                   "fixture", session_id=client.session))
    elif frame in ("tool_a", "tool_b", "foreign_tool", "cron"):
        foreign = frame == "foreign_tool"
        block = ToolUseBlock("cron", "CronCreate", {}) if frame == "cron" else (
            ToolUseBlock("tool-" + ("a" if foreign else suffix), "Bash", {"run_in_background": True}))
        client.stream.put_nowait(AssistantMessage([block], "fixture", session_id=client.session,
            uuid=uid(70 if suffix == "a" else 71), user_message_uuid="different" if foreign else None if frame == "cron" else human))
    elif frame in ("initial_members", "repeat_members"):
        client.system("background_tasks_changed", uuid=uid(33), tasks=[dict(task_id="task-a", run_id="run-a", task_type="local_bash")])
    elif frame.startswith("members_") or frame == "empty":
        ids = [] if frame == "empty" else frame.removeprefix("members_")
        client.system("background_tasks_changed", tasks=[dict(task_id="task-" + x, run_id="run-" + x,
                                                               task_type="local_bash") for x in ids])
    elif frame.startswith("start_"):
        client.system("task_started", task_id="task-" + suffix, run_id="run-" + suffix, tool_use_id="tool-" + suffix,
                      task_type="local_bash", is_backgrounded=True)
    elif frame.startswith("end_"):
        client.system("task_updated", task_id="task-" + suffix, run_id="run-" + suffix, patch={"status":"completed"})
    elif frame == "malformed_stop":
        client.callback(dict(session_id=client.session, prompt_id=uid(74), background_tasks=None, session_crons=[]))
    elif frame == "mixed_inventory":
        client.callback(dict(session_id=client.session, prompt_id=uid(51),
            background_tasks=[dict(id="shell", type="shell", status="running"), {}], session_crons=[]))
    elif frame.startswith("stop_") or frame in ("old_stop", "unpaired"):
        source = "b" if frame in ("stop_a_b", "stop_empty_b") else "a" if frame == "stop_empty_a" else "h"
        tasks = ["task-a", "task-b"] if frame == "stop_ab" else ["task-a"] if frame in ("stop_a", "stop_a_b", "unpaired") else []
        client.callback(dict(session_id=client.session, prompt_id=uid(ord(source)),
            background_tasks=[dict(id=x, type="shell", status="running") for x in tasks],
            session_crons=[dict(id="cron", recurring=True)] if frame == "unpaired" else []), uid(900 if frame == "old_stop" else 300 + ord(source)))
    elif frame == "replay_result":
        client.wake_result(run="retired", result="old answer", uuid=uid(91), snapshot=False, idle=False)
    elif frame == "aborted":
        client.result(uuid=uid(23), subtype="error_during_execution", reason="aborted_streaming", is_error=True, snapshot=False, idle=False)
    elif frame == "failed":
        client.result(uuid=uid(23), subtype="error_during_execution", reason="api_error", is_error=True, snapshot=False, idle=False)
    elif frame == "first":
        client.result(uuid=uid(23), result="same body", snapshot=False, idle=False)
    elif frame.startswith("answer_"):
        client.wake_result(run="run-" + suffix, uuid=uid(24 if suffix == "a" else 25), result="same body", snapshot=False, idle=False)
    else:
        assert frame in ("idle", "running")
        client.system("session_state_changed", state=frame)


def unresolved_at_cut(frames):
    pending = set()
    for frame in frames:
        if frame in ("unpaired", "mixed_inventory", "cron"):
            pending.add(frame)
        elif frame.startswith("members_"):
            pending.update(frame.removeprefix("members_"))
        elif frame.startswith("tool_") or frame in ("bare_tool", "foreign_tool"):
            pending.add("a" if frame in ("foreign_tool", "bare_tool") else frame[-1])
        elif frame.startswith("answer_") and "foreign_tool" not in frames and not any(x.startswith("conflict_") for x in frames):
            pending.discard(frame[-1])
    wake_open = any(x.startswith("wake_") for x in frames) and frames[-1] in ("wake_a", "wake_b", "slow_wake")
    return bool(pending) or wake_open

# A registered cron keeps the client open for replays even after both deliveries.
LEDGER_FRAMES = ("running", "human", "state_id", "tool_a", "initial_members", "start_a", "receipt_tool", "registered",
                 "first", "idle", "end_a", "empty", "wake_a", "stop_empty_a", "answer_a", "idle")
# Pair each native claim with a contradictory claim for the same identity.
LEDGER_CASES = {
    "state": ("state_id", "changed_state"),
    "tool_receipt": ("receipt_tool", "changed_tool_receipt"),
    "init": ("human", "changed_human"),
    "assistant": ("tool_a", "changed_assistant"),
    "tool": ("tool_a", "changed_tool"),
    "list": ("initial_members", "changed_list"),
    "run": ("start_a", "changed_run"),
    "task": ("start_a", "changed_task"),
    "tool_run": ("start_a", "claimed_tool"),
    "callback_payload": ("registered", "changed_payload"),
    "callback": ("registered", "changed_callback"),
    "terminal": ("end_a", "changed_terminal"),
    "human_result": ("first", "changed_first"),
    "wake_result": ("answer_a", "changed_answer"),
}


def evidence_frame(client, frame):
    human = client.inputs[0]["uuid"]
    if frame in ("state_id", "changed_state"):
        client.system("session_state_changed", uuid=uid(67), state="running" if frame == "state_id" else "idle")
    elif frame in ("receipt_tool", "changed_tool_receipt"):
        client.stream.put_nowait(UserMessage([ToolResultBlock("tool-a", "", False)], uuid=uid(65 if frame == "receipt_tool" else 66),
            tool_use_result={"backgroundTaskId": "task-a" if frame == "receipt_tool" else "task-x"}))
    elif frame == "changed_human":
        client.system("init", uuid=uid(11), cwd="/different", permissionMode="auto", user_message_uuids=[human])
    elif frame in ("changed_assistant", "changed_tool"):
        block = ToolUseBlock("tool-a", "Bash", {"run_in_background": True, "description": "different"})
        client.stream.put_nowait(AssistantMessage([block], "fixture", session_id=client.session,
            uuid=uid(70 if frame == "changed_assistant" else 77), user_message_uuid=human))
    elif frame in ("changed_list", "empty_id"):
        rows = [] if frame == "empty_id" else [dict(task_id="other", run_id="run-a", task_type="local_bash")]
        client.system("background_tasks_changed", uuid=uid(33), tasks=rows)
    elif frame in ("changed_run", "changed_task", "claimed_tool"):
        client.system("task_started", run_id="run-a" if frame == "changed_run" else "another-run",
            task_id="task-a" if frame == "changed_task" else "another-task",
            tool_use_id="tool-a" if frame == "claimed_tool" else "another-tool", task_type="local_bash", is_backgrounded=True)
    elif frame in ("registered", "changed_callback", "changed_payload"):
        client.callback(dict(session_id=client.session, prompt_id=uid(80 if frame != "changed_callback" else 81),
            background_tasks=[dict(id="task-a", type="shell", status="running")] if frame != "changed_callback" else [],
            stop_hook_active=frame == "changed_payload", session_crons=[dict(id="cron", recurring=True)]), uid(82))
    elif frame == "changed_terminal":
        client.system("task_updated", run_id="run-a", task_id="task-a", patch={"status": "failed"})
    elif frame == "changed_first":
        client.result(uuid=uid(23), result="changed", snapshot=False, idle=False)
    elif frame == "changed_answer":
        client.wake_result("run-a", uuid=uid(24), result="changed", snapshot=False, idle=False)
    elif frame == "receipt_a":
        client.wake_result("run-a", uuid=uid(24), reason=None, result="", snapshot=False, idle=False)
    elif frame == "foreign_producer":
        client.wake_result("run-a", uuid=uid(24), result="same body", origin={
            "kind": "task-notification", "producer": "peer", "runId": "run-a"}, snapshot=False, idle=False)
    else:
        emit(client, frame)


# The first entry of each distinct prompt precedes its own Result. Either
# callback can lead the application reader, including across H1's terminal.
BUFFERED = HUMAN_START + ("first", "idle") + WAKE_RESULT[:3] + ("answer_a", "idle")


def callback_schedules():
    for human in range(BUFFERED.index("first") + 1):
        for wake in range(human, BUFFERED.index("answer_a") + 1):
            yield human, wake




# Values not admitted by the public shape at this pin. Nullable SDK fields
# and open JSON objects keep their legal None, empty string or dictionary.
WRONG = (17, None, {}, [17], "", False)
# Link SDK samples to the public objects they contain. Field names and their
# admitted value shapes come directly from the runtime's consumed-field table.
SAMPLES = {
    "state": (("system", ""), ("session_state_changed", "data.")),
    "init": (("init", "data."),), "list": (("background_tasks_changed", "data."),),
    "start": (("task_started", "data."),), "update": (("task_updated", "data."),),
    "notification": (("task_notification", "data."),),
    "assistant": (("assistant", ""), ("tool", "content.0."), ("launch", "content.0.")),
    "taskstop": (("taskstop", "content.0."),),
    "user": (("user", ""), ("receipt", "content.0."), ("native_result", "")),
    "stream": (("stream", ""),), "result": (("result", ""),), "stop": (("stop", ""), ("callback", "")),
    "hook": (("hook", ""),), "rate": (("rate", ""),),
}


def shape_cases():
    assert {kind for scopes in SAMPLES.values() for kind, _ in scopes} == CONSUMED_FIELDS.keys()
    for sample, scopes in SAMPLES.items():
        for kind, prefix in scopes:
            for field, accepts in CONSUMED_FIELDS[kind].items():
                path = prefix + field.replace("*", "0").rstrip("?")
                for index, value in enumerate(WRONG):
                    if not accepts(value):
                        yield sample, path, index


def malformed(client, kind, path, index):
    from claude_agent_sdk import RateLimitEvent, RateLimitInfo, ResultMessage, StreamEvent, SystemMessage
    obj = dict(session_id=client.session, uuid=uid(820))
    work = dict(task_id="probe-task", run_id="probe-run", tool_use_id="probe-tool")
    system = {
        "init": ("init", dict(cwd=str(client.cwd), permissionMode="auto", model="fixture", user_message_uuids=[])),
        "state": ("session_state_changed", dict(state="running")),
        "list": ("background_tasks_changed", dict(tasks=[dict(task_id="probe-task", run_id="probe-run", task_type="local_bash")])),
        "start": ("task_started", dict(**work, task_type="local_bash", is_backgrounded=True)),
        "update": ("task_updated", dict(**work, patch={"status": "completed"})),
        "notification": ("task_notification", dict(**work, status="completed")),
    }
    if kind in system:
        subtype, fields = system[kind]
        frame = SystemMessage(subtype, obj | fields)
    elif kind in ("stop", "hook"):
        frame = dict(hook_event_name="Stop", session_id=client.session, prompt_id=uid(821), callback=uid(822), stop_hook_active=False,
                     background_tasks=[dict(id="probe-task", type="shell", status="running")],
                     session_crons=[dict(id="cron", recurring=True)])
    elif kind in ("assistant", "taskstop"):
        block = (ToolUseBlock("probe-tool", "Bash", {"run_in_background": True}) if kind == "assistant"
                 else ToolUseBlock("stop-tool", "TaskStop", {"task_id": "probe-task"}))
        frame = AssistantMessage([block], "model", **obj)
    elif kind == "user":
        frame = UserMessage([ToolResultBlock("probe-tool", "", False)], uuid=uid(823),
                            tool_use_result=dict(backgroundTaskId="probe-task", task_id="probe-task", task_type="local_bash"))
    elif kind == "rate":
        frame = RateLimitEvent(RateLimitInfo("allowed"), **obj)
    elif kind == "stream":
        frame = StreamEvent(**obj, event={})
    else:
        frame = ResultMessage("success", 1, 1, False, 1, **obj, terminal_reason="completed", result="probe body",
                              origin={"kind": "task-notification", "producer": "session-task", "runId": "probe-run"})
    obj = frame
    *parents, field = path.split(".")
    for key in parents:
        obj = obj[int(key)] if isinstance(obj, list) else obj[key] if isinstance(obj, dict) else getattr(obj, key)
    if isinstance(obj, dict):
        obj[field] = WRONG[index]
    else:
        setattr(obj, field, WRONG[index])
    if kind == "hook":
        return client.background.stop_hook(frame, frame.pop("callback"), {})
    if kind == "stop":
        callback = frame.pop("callback")
        client.background.snapshot(frame, callback)
    else:
        client.stream.put_nowait(frame)
