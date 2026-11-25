"""Claude physical Activity and Files at the public Channel transport boundary."""
import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

from lark_channel import SendResult

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.domain import FeishuScope, ScopeKind
from tests.support import has_claude_sdk, requires_claude_sdk
from tests.support.channel_messages import FakeMessage
from tests.support.process_transport import ProcessTransport, check_pairs
from tests.test_claude_channel import owner_channel

if has_claude_sdk:
    from claude_agent_sdk import AssistantMessage, SystemMessage, ToolResultBlock, ToolUseBlock, UserMessage


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.005)


@asynccontextmanager
async def running_channel(*, cards=False, mention=False):
    async with owner_channel() as (app, fixture, clients, clock, ordinary):
        clock[0] = 2.
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = fixture.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
            task_feedback=BindingTaskFeedback(progress_card_enabled=cards, completion_mention_enabled=mention))
        process = ProcessTransport([])
        app._progress_cards._process_port = process.port
        app._progress_cards._poll_seconds = .01
        if cards:
            fixture.channel.reply_results.append(SendResult.ok(message_id="first"))
        await app.handle_message(FakeMessage("work", message_id="H"))
        client = clients.calls[-1]
        client.init(uuid="physical-A")
        await until(lambda: bool(app._progress_cards._sessions))
        yield app, fixture.channel, client, ordinary._active[binding.id], process, ordinary


def tool(client, name, key, args=None, *, parent=None):
    client.stream.put_nowait(AssistantMessage([ToolUseBlock(key, name, args or {})], "model",
                                               session_id=client.session, parent_tool_use_id=parent))


def result(client, key, payload):
    client.stream.put_nowait(UserMessage([ToolResultBlock(key)], tool_use_result=payload))


def system(client, name, **values):
    client.stream.put_nowait(SystemMessage(name, {"session_id": client.session, **values}))


def background(client):
    tool(client, "Bash", "bg", {"command": "fixture"})
    system(client, "task_started", task_id="t", run_id="r", tool_use_id="bg")
    system(client, "background_tasks_changed", tasks=[{"task_id": "t", "run_id": "r"}])
    result(client, "bg", {"backgroundTaskId": "t"})


def start_wake(client):
    system(client, "background_tasks_changed", tasks=[])
    client.init(user_message_uuids=[], uuid="physical-B")


def finish_wake(client, *, failed=False):
    client.result("error_max_turns" if failed else "success", "max_turns" if failed else None, failed,
                  result=None, user_message_uuid=None, uuid="wake-result",
                  origin={"kind": "task-notification", "producer": "session-task", "runId": "r"})
    system(client, "session_state_changed", state="idle")


@requires_claude_sdk
class ClaudeActivityDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_bodyless_interruption_keeps_its_own_process_verdict(self):
        async with running_channel() as (_, channel, client, execution, process, ordinary):
            background(client)
            tool(client, "Read", "open", {"file_path": "open-human"})
            await until(lambda: "open-human" in str(process.events))
            client.result("error_during_execution", "aborted_tools", True, result=None)
            await until(lambda: execution.admissible and not execution.delivery.queues)
            def rows_closed():
                updates = [args[1] for name, args, _ in process.events if name == "process.update"]
                starts, ends = check_pairs(updates[-1])
                return bool(starts) and set(starts) == ends
            await until(rows_closed)
            self.assertFalse(process.completed)
            self.assertFalse(channel.replies)
            start_wake(client)
            finish_wake(client, failed=True)
            self.assertTrue(await ordinary.wait_idle(5))
            await until(lambda: bool(process.completed))
            self.assertEqual([args[1] for name, args, _ in process.events if name == "process.complete"], ["done"])

    async def test_completed_step_survives_child_invalidation_between_polls(self):
        async with running_channel() as (app, _, client, execution, process, ordinary):
            background(client)
            tool(client, "TaskList", "list")
            result(client, "list", {"tasks": [{"id": "1", "subject": "observed-step", "status": "in_progress"}]})
            await until(lambda: "observed-step" in str(process.events))
            session, = app._progress_cards._sessions.values()
            with patch.object(session, "observe", return_value=session.observe()):
                tool(client, "TaskUpdate", "change")
                result(client, "change", {"success": True, "taskId": "1", "updatedFields": ["status"],
                                          "statusChange": {"from": "in_progress", "to": "completed"}})
                tool(client, "TaskList", "child", parent="bg")
                await until(lambda: execution.checklist.rows is None)
            def step_ended():
                updates = [args[1] for name, args, _ in process.events if name == "process.update"]
                return ("step", "observed-step") in check_pairs(updates[-1])[1]
            await until(step_ended)
            self.assertFalse(process.completed)
            client.result(result="initial answer")
            await until(lambda: execution.admissible and not execution.delivery.queues)
            start_wake(client)
            finish_wake(client)
            self.assertTrue(await ordinary.wait_idle(5))
