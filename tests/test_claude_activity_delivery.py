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
from tests.support.channel_results import sent_result
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


def write_file(client, number):
    name = f"fixture-{number}.txt"
    (client.cwd / name).write_text("fixture")
    tool(client, "Write", name, {"file_path": name})
    result(client, name, {"filePath": name})


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

    async def test_lost_file_only_receipt_suppresses_mention(self):
        from netizen_cli.channel.physical_delivery import INCOMPLETE
        async with running_channel(mention=True) as (_, channel, client, _, _, ordinary):
            write_file(client, 0)
            channel.reply_results.extend([TimeoutError(), SendResult.ok(message_id="notice")])
            client.result(reason=None, result=None)
            self.assertTrue(await ordinary.wait_idle(5))
            self.assertEqual([text for _, text in channel.replies if isinstance(text, str)], [INCOMPLETE])
            self.assertFalse(channel.send_calls)

    async def test_confirmed_file_only_wake_is_latest_answer_anchor(self):
        for cards in (True, False):
            with self.subTest(cards=cards):
                async with running_channel(cards=cards, mention=True) as (_, channel, client, execution, _, ordinary):
                    background(client)
                    if not cards:
                        channel.reply_results.append(SendResult.ok(message_id="first"))
                    client.result(result="first answer")
                    await until(lambda: execution.admissible and not execution.delivery.queues)
                    start_wake(client)
                    write_file(client, 0)
                    channel.reply_results.append(SendResult.ok(message_id="wake-files"))
                    channel.send_results.append(sent_result("mention", chat_id="oc_direct", root_id="H", parent_id="wake-files"))
                    finish_wake(client)
                    self.assertTrue(await ordinary.wait_idle(5))
                    self.assertEqual([opts.reply_to for _, msg, opts in channel.send_calls if getattr(msg, "mentions", ())],
                                     ["wake-files"])

    async def test_file_limits_deliver_body_and_precise_notice_in_both_modes(self):
        from netizen_cli.cards import TURN_FILE_MANIFEST_LIMIT
        from netizen_cli.cards import reply as renderer
        for cards in (True, False):
            for count, budget, reason in ((TURN_FILE_MANIFEST_LIMIT + 1, renderer.TURN_FILE_CARD_JSON_LIMIT_BYTES, "400"),
                                          (2, 100, "100 bytes")):
                with self.subTest(cards=cards, reason=reason):
                    async with running_channel(cards=cards) as (_, channel, client, _, _, ordinary):
                        for number in range(count):
                            write_file(client, number)
                        with patch.object(renderer, "TURN_FILE_CARD_JSON_LIMIT_BYTES", budget):
                            client.result(result="final answer")
                            self.assertTrue(await ordinary.wait_idle(10))
                        texts = [msg for _, msg in channel.replies if isinstance(msg, str)]
                        self.assertEqual(texts[0], "final answer")
                        self.assertEqual(len(texts), 2)
                        self.assertIn("本轮文件卡片未生成", texts[1])
                        self.assertIn(reason, texts[1])
