"""Claude physical Activity and Files at the public Channel transport boundary."""
import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

from lark_channel import SendResult

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.turn_files import TURN_FILE_PAGE_SIZE
from tests.support import has_claude_sdk, requires_claude_sdk
from tests.support.channel_cards import _elements, direct_button_event
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


def buttons(card, intent):
    return [entry["value"] for button in _elements(card, "button") for entry in button["behaviors"]
            if entry["value"].get("intent") == intent]


async def open_file_pages(channel, client, execution):
    background(client)
    for number in range(TURN_FILE_PAGE_SIZE + 1):
        write_file(client, number)
    client.result(result="body")
    await until(lambda: execution.admissible and not execution.delivery.queues)
    card = channel.updates[-1][1]
    assert "后台任务 1 个运行中" in str(card)
    action, = buttons(card, "turn-file.page")
    return action


async def select_page(app, channel, action, mid, page):
    await app.handle_card_action(direct_button_event(action, message_id=mid, form_value={"turn_file_page": str(page)}))
    updated_mid, card = channel.updates[-1]
    assert updated_mid == mid
    return card


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

    async def test_proven_interruption_reaches_process_before_held_stop_ack_and_eof(self):
        async with running_channel() as (_, channel, client, execution, process, ordinary):
            background(client)
            tool(client, "Read", "open", {"file_path": "open-human"})
            await until(lambda: "open-human" in str(process.events))
            entered, release = asyncio.Event(), asyncio.Event()

            async def acknowledge():
                entered.set()
                await release.wait()

            stop = asyncio.create_task(ordinary.stop_exact(execution.binding.id, acknowledge=acknowledge))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                client.result("error_during_execution", "aborted_tools", True, result=None, idle=False)
                await until(lambda: execution.live.physical is None and execution.human.status == "interrupted")
                client.stream.put_nowait(None)
                await until(lambda: execution.broken)
            finally:
                release.set()
                await stop
            self.assertTrue(await ordinary.wait_idle(5))
            await until(lambda: bool(process.completed))
            updates = [args[1] for name, args, _ in process.events if name == "process.update"]
            starts, ends = check_pairs(updates[-1])
            self.assertTrue(starts)
            self.assertEqual(set(starts), ends)
            self.assertEqual([args[1] for name, args, _ in process.events if name == "process.complete"], ["done"])
            self.assertFalse(channel.replies)

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

    async def test_selected_page_survives_completion_and_late_page_uses_final_line(self):
        async with running_channel(cards=True) as (app, channel, client, execution, _, ordinary):
            page = await open_file_pages(channel, client, execution)
            card = await select_page(app, channel, page, "first", 1)
            self.assertEqual(len(buttons(card, "turn-file.send")), 1)
            start_wake(client)
            finish_wake(client)
            self.assertTrue(await ordinary.wait_idle(5))
            final = channel.updates[-1][1]
            self.assertEqual(len(buttons(final, "turn-file.send")), 1)
            self.assertIn("全部完成", str(final))
            late = await select_page(app, channel, page, "first", 0)
            self.assertEqual(len(buttons(late, "turn-file.send")), TURN_FILE_PAGE_SIZE)
            self.assertIn("全部完成", str(late))
            self.assertNotIn("后台任务 1 个运行中", str(late))

    async def test_oldest_retired_file_card_falls_back_then_binding_retirement_clears_the_rest(self):
        with patch("netizen_cli.channel.reply_presenter._RETIRED_FILE_CARD_LIMIT", 1):
            async with running_channel(cards=True) as (app, channel, client, execution, _, ordinary):
                pages = []
                binding_id = execution.binding.id
                for mid in ("first", "second"):
                    if mid == "second":
                        channel.reply_results.append(SendResult.ok(message_id=mid))
                        await app.handle_message(FakeMessage("again", message_id="H2"))
                        execution = ordinary._active[binding_id]
                        client = execution.client
                        client.init(uuid="second-init")
                        await until(lambda: bool(app._progress_cards._sessions))
                    pages.append(await open_file_pages(channel, client, execution))
                    start_wake(client)
                    finish_wake(client)
                    self.assertTrue(await ordinary.wait_idle(5))
                first = await select_page(app, channel, pages[0], "first", 1)
                second = await select_page(app, channel, pages[1], "second", 1)
                self.assertIn("后台任务 1 个运行中", str(first))
                self.assertIn("全部完成", str(second))
                await app._progress_cards.retire_binding(binding_id)
                cleared = await select_page(app, channel, pages[1], "second", 0)
                self.assertIn("后台任务 1 个运行中", str(cleared))
