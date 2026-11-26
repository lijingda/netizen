import asyncio
import itertools
import sqlite3
import tempfile
import unittest
from contextlib import AsyncExitStack, asynccontextmanager, closing
from pathlib import Path
from unittest.mock import patch

from openai_codex import ExternalMessage, InvalidRequestError

from netizen_cli.bindings import BindingStore, BindingTaskFeedback
from netizen_cli.channel_app import ChannelApplication
from netizen_cli.codex_runtime import CodexRuntime, RuntimeClosed, StopDisposition, SubmitDisposition, TurnStartFailed
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management import CurrentBindingTarget, InstanceManagementService, ManagementRuntimePort, ScopeCoordinator
from netizen_cli.projects import ProjectRegistry
from netizen_cli.runtime.codex_ordinary import CodexOrdinarySessions
from netizen_cli.schedules.models import ScheduleRule
from netizen_cli.worktrees.policy import WorkspacePolicy
from tests.support.channel_cards import _card_button_value, direct_card_event
from tests.support.channel_messages import FakeChannel, FakeMessage
from tests.test_codex_runtime import (
    FakeBackgroundTerminalInspector, FakeTerminalCleanup, FakeThreadSubscriptionControl, provider_error,
)
from tests.test_session_external_runtime import ExternalCodex, ExternalThread


START_ACK = "本轮正在启动，Codex 确认启动后将立即中断。"
ENDED = "当前任务恰好已经结束。"
REJECTED = "本次启动未获接受，未请求中断。"
INTERRUPTED = (
    "Codex Turn 已中断；已请求清理该 Thread 中已登记的后台终端。"
    "前台工具进程不受此接口保证，可能仍在运行。"
)


class HeldReceipt:
    """Hold one selected native receipt, after creating the Turn on success."""

    def __init__(self, outcome, call):
        self.outcome = outcome
        self.call = call
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def install(self):
        native_turn = ExternalThread.turn

        async def turn(thread, input, **kwargs):
            self.calls += 1
            if self.calls != self.call:
                return await native_turn(thread, input, **kwargs)
            if self.outcome not in {"rejected", "external_rejected"}:
                handle = await native_turn(thread, input, **kwargs)
            self.entered.set()
            await self.release.wait()
            if self.outcome == "rejected":
                raise provider_error()
            if self.outcome == "external_rejected":
                raise InvalidRequestError(-32600, "input rejected")
            if self.outcome == "unknown":
                raise ConnectionError("native start receipt lost")
            return handle

        return patch.object(ExternalThread, "turn", turn)


class StopCase:
    def __init__(self, store, cwd, held):
        self.store = store
        self.held = held
        self.codex = ExternalCodex()
        self.cleanup = FakeTerminalCleanup(self.codex.events)
        self.runtime = CodexRuntime(
            codex=self.codex, bindings=store, terminal_cleanup=self.cleanup,
            automatic_thread_naming=False, poll_interval_seconds=0,
        )
        projects = ProjectRegistry(store=store, project_root=cwd, projects={"test": cwd})
        self.scope = FeishuScope("app", "oc_direct", ScopeKind.DIRECT)
        self.binding = store.create_channel_binding(
            scope=self.scope, project_alias="test", creator_id="ou_user",
            task_feedback=BindingTaskFeedback(progress_card_enabled=False),
        )
        self.cwd = cwd
        ordinary = CodexOrdinarySessions(self.runtime)
        self.management = InstanceManagementService(
            bindings=store, projects=projects, runtime=ManagementRuntimePort(self.runtime, ordinary=ordinary),
            scope_coordinator=ScopeCoordinator(),
        )
        self.channel = FakeChannel()
        self.app = ChannelApplication(
            app_id="app", channel=self.channel, bindings=store, projects=projects, runtime=self.runtime,
            ordinary=ordinary, management=self.management,
        )
        self.first_stop_reply = asyncio.Event()
        self.allow_ack = asyncio.Event()
        self.allow_ack.set()
        reply = self.channel.reply

        async def record(message, content, opts=None):
            if message.id == "stop":
                self.first_stop_reply.set()
                await self.allow_ack.wait()
            self.codex.events.append((message.id, content))
            return await reply(message, content, opts)

        self.channel.reply = record

    async def message(self, text, message_id):
        await self.app.handle_message(FakeMessage(text, message_id=message_id))

    async def external(self):
        receipt = await self.runtime.submit_external(
            binding=self.binding, cwd=self.cwd, owner_id="agent", origin=FakeMessage("tool", message_id="tool"),
            message=ExternalMessage("send_message_to_thread", "follow-up", "netizen"),
        )
        if receipt.release_receipt_attempt is not None:
            receipt.release_receipt_attempt()
        return receipt

    def replies(self, message_id):
        return [value for key, value in self.channel.replies if key == message_id]


@asynccontextmanager
async def stop_case(outcome="success", call=1):
    async with asyncio.timeout(5), AsyncExitStack() as resources:
        cwd = Path(resources.enter_context(tempfile.TemporaryDirectory()))
        store = resources.enter_context(closing(BindingStore()))
        held = HeldReceipt(outcome, call)
        resources.enter_context(held.install())
        resources.enter_context(patch("netizen_cli.runtime.limits.INTERRUPTION_CONFIRMATION_SECONDS", 0))
        case = StopCase(store, cwd, held)
        resources.push_async_callback(case.management.close)
        # Close presentation before cancelling Turn consumers that may still be awaiting a card poller.
        resources.push_async_callback(case.runtime.cancel_tasks)
        resources.push_async_callback(case.app.close)
        async with asyncio.TaskGroup() as tasks:
            try:
                yield case, tasks
            finally:
                held.release.set()
                case.allow_ack.set()


class TurnStartStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_feishu_stop_before_native_receipt(self):
        async with stop_case() as (case, tasks):
            tasks.create_task(case.message("first", "first"))
            await case.held.entered.wait()
            first, = case.codex.handles
            self.assertIsNone(case.runtime.active_turn(case.binding.id))
            stop = tasks.create_task(case.message("/stop", "stop"))
            await case.first_stop_reply.wait()
            self.assertEqual(case.replies("stop"), [START_ACK])
            self.assertFalse(stop.done())
            self.assertEqual((first.interrupt_count, case.cleanup.calls), (0, []))
            case.held.release.set()
            await stop
            self.assertTrue(await case.runtime.wait_idle(2))
            self.assertEqual((first.interrupt_count, case.cleanup.calls), (1, [first.thread_id]))
            self.assertEqual(case.replies("stop"), [START_ACK])
            events = case.codex.events
            self.assertLess(events.index(("stop", START_ACK)), events.index(("interrupt", first.thread_id)))
            self.assertLess(events.index(("interrupt", first.thread_id)), events.index(("cleanup", first.thread_id)))
            self.assertLess(events.index(("cleanup", first.thread_id)), events.index(("first", INTERRUPTED)))
            self.assertEqual(case.replies("first").count(INTERRUPTED), 1)

    async def test_cancelled_stop_acknowledgement(self):
        for pending, cancel_caller in itertools.product((True, False), repeat=2):
            with self.subTest(pending=pending, cancel_caller=cancel_caller):
                async with stop_case() as (case, tasks):
                    first = tasks.create_task(case.message("first", "first"))
                    await case.held.entered.wait()
                    if not pending:
                        case.held.release.set()
                        await first
                    send = asyncio.get_running_loop().create_future()
                    with patch.object(case.allow_ack, "wait", new=lambda: send):
                        stop = tasks.create_task(case.message("/stop", "stop"))
                        await case.first_stop_reply.wait()
                        if cancel_caller:
                            stop.cancel()
                        else:
                            send.cancel()
                        case.held.release.set()
                        await first
                        if cancel_caller:
                            with self.assertRaises(asyncio.CancelledError):
                                await stop
                        else:
                            await stop
                            self.assertTrue(await case.runtime.wait_idle(2))
                    handle, = case.codex.handles
                    expected = (0, []) if cancel_caller else (1, [handle.thread_id])
                    self.assertEqual((handle.interrupt_count, case.cleanup.calls), expected)

    async def test_stop_orderings(self):
        # Stop is captured before the start response. The second input may arrive on
        # either side of the stop and the response. Holding Scope delays redemption
        # independently of native acceptance and of the Binding lock.
        orders = [p for p in itertools.permutations(("stop", "input", "receipt")) if p.index("stop") < p.index("receipt")]
        for outcome, order in itertools.product(("success", "rejected", "unknown", "cancelled"), orders):
            finishes = ("running", "before_receipt", "before_input") if outcome == "success" else ("none",)
            for finish in finishes:
                with self.subTest(outcome=outcome, order=order, finish=finish):
                    await self.explore(outcome, order, finish)

        with self.subTest(order="stop, switch current Binding, receipt"):
            await self.explore_scope_switch()

    async def explore(self, outcome, order, finish):
        async with stop_case(outcome) as (case, tasks):
            first = tasks.create_task(case.message("first", "first"))
            await case.held.entered.wait()

            async def second_input():
                input_entered.set()
                try:
                    return await case.external()
                except RuntimeClosed:
                    return None

            input_entered = asyncio.Event()
            async with case.management.scope_coordinator.hold(case.scope.key):
                for step in order:
                    if step == "stop":
                        stop = tasks.create_task(case.message("/stop", "stop"))
                        await case.first_stop_reply.wait()
                        self.assertEqual(case.replies("stop"), [START_ACK])
                    elif step == "input":
                        case.codex.advance_on_external = finish == "before_input"
                        second = tasks.create_task(second_input())
                        await input_entered.wait()
                    else:
                        if finish == "before_receipt":
                            case.codex.handles[0].complete(response="natural")
                        if outcome == "cancelled":
                            first.cancel()
                        case.held.release.set()
                receipt = await second

            await stop
            if receipt is not None and receipt.disposition is SubmitDisposition.STARTED:
                successor = next(h for h in case.codex.handles if h.id == receipt.turn_id)
                self.assertEqual(successor.interrupt_count, 0, "captured stop interrupted the successor")
            replies = case.replies("stop")
            if outcome in {"unknown", "cancelled"}:
                self.assertIsNone(receipt)
                self.assertFalse(case.runtime._accepting)
                async with case.management.scope_coordinator.hold(case.scope.key), case.runtime._lock(case.binding.id):
                    await case.message("/stop", "again")
                self.assertEqual(len(replies), 2)
                self.assertIn("启动结果未确认", replies[1])
                self.assertEqual(replies[1], "Codex Turn 启动结果未确认；本次未请求中断。服务已停止接收新任务，请重启服务。")
                self.assertEqual(case.replies("again"), [replies[1]])
            elif outcome == "success" and finish == "running":
                self.assertEqual(receipt.disposition, SubmitDisposition.JOINED)
                self.assertEqual(replies, [START_ACK])
                self.assertEqual(case.codex.handles[0].interrupt_count, 1)
                self.assertEqual(case.cleanup.calls, [receipt.thread_id])
                return
            elif outcome == "rejected":
                self.assertEqual(receipt.disposition, SubmitDisposition.STARTED)
                self.assertEqual(replies, [START_ACK, REJECTED])
            else:
                self.assertEqual(receipt.disposition, SubmitDisposition.STARTED)
                self.assertEqual(replies, [START_ACK, ENDED])
            self.assertTrue(all(h.interrupt_count == 0 for h in case.codex.handles))
            self.assertEqual(case.cleanup.calls, [])

    async def explore_scope_switch(self):
        async with stop_case() as (case, tasks):
            tasks.create_task(case.message("first", "first"))
            await case.held.entered.wait()
            stop = tasks.create_task(case.message("/stop", "stop"))
            await case.first_stop_reply.wait()
            kept = await case.management.create_current_binding(
                scope=case.scope, project_alias="test", creator_id="ou_other", only_if_empty=True,
            )
            self.assertEqual(kept.binding.id, case.binding.id)
            switching = tasks.create_task(case.management.create_current_binding(
                scope=case.scope, project_alias="test", creator_id="ou_other",
            ))
            # /new commits the pointer before reconciling the old subscription.
            while case.store.active_binding(case.scope.key).id == case.binding.id:
                await asyncio.sleep(0)
            self.assertFalse(stop.done())
            case.held.release.set()
            await switching
            await stop
            self.assertIn("active 会话已切换", case.replies("stop")[-1])
            self.assertEqual(case.codex.handles[0].interrupt_count, 0)

    async def rejected_external(self, case):
        with self.assertRaises(InvalidRequestError):
            await case.external()

    async def test_bare_stop_keeps_its_preview_across_scope_wait(self):
        async with stop_case("external_rejected", call=2) as (case, tasks):
            await case.message("first", "first")
            first, = case.codex.handles
            previewed, selecting = asyncio.Event(), asyncio.Event()
            stop_current = case.management.stop_current_binding
            stop_exact = case.runtime.stop_exact

            async def after_preview(**kwargs):
                self.assertIsNone(kwargs["preview"].start)
                previewed.set()
                return await stop_current(**kwargs)

            async def under_scope(*args, **kwargs):
                selecting.set()
                return await stop_exact(*args, **kwargs)

            with patch.object(case.management, "stop_current_binding", after_preview), patch.object(
                case.runtime, "stop_exact", under_scope,
            ):
                async with case.management.scope_coordinator.hold(case.scope.key):
                    stop = tasks.create_task(case.message("/stop", "stop"))
                    await previewed.wait()
                    tasks.create_task(self.rejected_external(case))
                    await case.held.entered.wait()
                await selecting.wait()
                case.held.release.set()
                await stop
            self.assertEqual(case.replies("stop"), ["正在中断当前 Codex Turn。"])
            self.assertEqual((first.interrupt_count, case.cleanup.calls), (1, [first.thread_id]))

    async def test_provider_rejection_survives_local_subscription_error(self):
        async with stop_case("rejected") as (case, tasks):
            async def first_start():
                with self.assertRaisesRegex(sqlite3.OperationalError, "release read failed"):
                    await case.external()

            first = tasks.create_task(first_start())
            await case.held.entered.wait()
            stop = tasks.create_task(case.message("/stop", "stop"))
            await case.first_stop_reply.wait()
            with (
                patch.object(case.runtime, "_thread_subscription_control", FakeThreadSubscriptionControl()),
                patch.object(case.runtime, "_background_terminal_inspector", FakeBackgroundTerminalInspector()),
                patch.object(case.store, "get", side_effect=sqlite3.OperationalError("release read failed")) as read,
            ):
                case.held.release.set()
                await first
                read.assert_called_once_with(case.binding.id)
            await stop
            self.assertEqual(case.replies("stop"), [START_ACK, REJECTED])
            self.assertEqual((case.codex.handles, case.cleanup.calls), ([], []))
            self.assertEqual((await case.external()).disposition, SubmitDisposition.STARTED)

    async def test_rejected_stop_returns_while_second_receipt_is_withheld(self):
        async with stop_case("rejected") as (case, tasks):
            second_receipt = HeldReceipt("success", call=2)
            with second_receipt.install():
                tasks.create_task(case.message("first", "first"))
                await case.held.entered.wait()
                stop = tasks.create_task(case.message("/stop", "stop"))
                await case.first_stop_reply.wait()
                queued = asyncio.Event()

                async def second_input():
                    queued.set()
                    return await case.external()

                second = tasks.create_task(second_input())
                await queued.wait()
                case.held.release.set()
                try:
                    await second_receipt.entered.wait()
                    kept = await case.management.create_current_binding(
                        scope=case.scope, project_alias="test", creator_id="ou_other", only_if_empty=True,
                    )
                    await stop
                    self.assertEqual(kept.binding.id, case.binding.id)
                    self.assertEqual(case.replies("stop"), [START_ACK, REJECTED])
                    self.assertFalse(second.done())
                finally:
                    second_receipt.release.set()
                self.assertEqual((await second).disposition, SubmitDisposition.STARTED)
                self.assertEqual(case.codex.handles[0].interrupt_count, 0)

    async def test_captured_external_rejection_leaves_running_turn_alone(self):
        async with stop_case("external_rejected", call=2) as (case, tasks):
            await case.message("first", "first")
            first, = case.codex.handles
            tasks.create_task(self.rejected_external(case))
            await case.held.entered.wait()
            async with case.management.scope_coordinator.hold(case.scope.key):
                stop = tasks.create_task(case.message("/stop", "stop"))
                await case.first_stop_reply.wait()
                case.held.release.set()
                await stop
            self.assertEqual(case.replies("stop"), [START_ACK, REJECTED])
            self.assertEqual(first.record.status.value, "inProgress")
            self.assertEqual((first.interrupt_count, case.cleanup.calls), (0, []))
            await case.message("/stop", "again")
            self.assertEqual((first.interrupt_count, case.cleanup.calls), (1, [first.thread_id]))

    async def test_normal_steer_then_stop_targets_same_running_turn(self):
        async with stop_case() as (case, _):
            case.held.release.set()
            await case.message("first", "first")
            await case.message("follow-up", "follow-up")
            first, = case.codex.handles
            self.assertEqual(len(first.steers), 1)
            await case.message("/stop", "stop")
            self.assertEqual(case.replies("stop"), ["正在中断当前 Codex Turn。"])
            self.assertEqual((first.interrupt_count, case.cleanup.calls), (1, [first.thread_id]))

    async def test_stop_captured_during_start_interrupts_same_turn_after_normal_steer(self):
        async with stop_case() as (case, tasks):
            prompt = tasks.create_task(case.message("first", "first"))
            await case.held.entered.wait()
            first, = case.codex.handles
            async with case.management.scope_coordinator.hold(case.scope.key):
                stop = tasks.create_task(case.message("/stop", "stop"))
                await case.first_stop_reply.wait()
                self.assertEqual(case.replies("stop"), [START_ACK])
                case.held.release.set()
                await prompt
                await case.message("follow-up", "follow-up")
                self.assertEqual(len(first.steers), 1)
                self.assertEqual((first.interrupt_count, case.cleanup.calls), (0, []))

            await stop
            self.assertTrue(await case.runtime.wait_idle(2))
            self.assertEqual((first.interrupt_count, case.cleanup.calls), (1, [first.thread_id]))
            self.assertEqual(case.replies("stop"), [START_ACK])

    async def test_status_shortcut_captures_pending_start(self):
        async with stop_case() as (case, tasks):
            await case.message("/status", "status")
            card, = case.replies("status")
            event = direct_card_event(case.channel, {}, message_id="stop")
            event.action.value = _card_button_value(card, "停止")
            tasks.create_task(case.message("first", "first"))
            await case.held.entered.wait()
            stop = tasks.create_task(case.app.handle_card_action(event))
            await case.first_stop_reply.wait()
            self.assertEqual(case.replies("stop"), [START_ACK])
            case.held.release.set()
            await stop
            self.assertEqual(case.codex.handles[0].interrupt_count, 1)
            self.assertEqual(case.replies("stop"), [START_ACK])

    async def test_terminal_waits_for_inflight_early_receipt(self):
        async with stop_case() as (case, tasks):
            case.allow_ack.clear()
            prompt = tasks.create_task(case.message("first", "first"))
            await case.held.entered.wait()
            stop = tasks.create_task(case.message("/stop", "stop"))
            await case.first_stop_reply.wait()
            case.codex.handles[0].complete(response="natural")
            case.held.release.set()
            await prompt
            # Observe retirement independently from sending the terminal.
            while case.runtime.active_turn(case.binding.id) is not None:
                await asyncio.sleep(0)
            self.assertEqual(case.channel.replies, [])
            case.allow_ack.set()
            await stop
            self.assertTrue(await case.runtime.wait_idle(2))
            self.assertEqual(case.channel.replies[0], ("stop", START_ACK))
            self.assertEqual(case.replies("stop"), [START_ACK, ENDED])
            self.assertTrue(case.replies("first"))

    async def test_external_rpc_targets_its_returned_handle(self):
        for advance in (False, True):
            with self.subTest(advance=advance):
                async with stop_case(call=2) as (case, tasks):
                    await case.message("first", "first")
                    case.codex.advance_on_external = advance
                    external = tasks.create_task(case.external())
                    await case.held.entered.wait()
                    stop = tasks.create_task(case.message("/stop", "stop"))
                    await case.first_stop_reply.wait()
                    case.held.release.set()
                    receipt = await external
                    await stop
                    for handle in case.codex.handles:
                        self.assertEqual(handle.interrupt_count, int(handle.id == receipt.turn_id))
                    self.assertEqual(case.replies("stop"), [START_ACK])
                    self.assertEqual(case.cleanup.calls, [receipt.thread_id])

    async def test_known_acceptance_survives_scheduled_handoff_failure(self):
        async with stop_case() as (case, tasks):
            schedules = case.store.schedules
            plan = schedules.create(
                name="test", instructions="test", project_alias="test", app_id="app", chat_id="oc_group",
                schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
                request_id="create", workspace=WorkspacePolicy(), now=100,
            )
            claim = schedules.claim_due(plan.plan_id, app_id="app", now=160)
            scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_test")
            schedules.set_run(
                claim.run.id, phase="publishing_topic", root_message_id="root", topic_id=scope.topic_id,
                origin_message_id="seed",
            )
            binding = case.store.create_scheduled_binding(run_id=claim.run.id, scope=scope)
            message = FakeMessage("/stop", message_id="scheduled_stop", chat_type="group", chat_id=scope.chat_id,
                                  thread_id=scope.topic_id)

            async def submit():
                with self.assertRaises(TurnStartFailed):
                    await case.runtime.submit_initial(
                        run_id=claim.run.id, binding=binding, cwd=case.cwd, input="test",
                        owner_id="scheduled_plan", origin=message,
                    )

            with patch.object(case.store, "mark_scheduled_turn_started", side_effect=OSError("disk error")):
                submitting = tasks.create_task(submit())
                await case.held.entered.wait()
                await case.app.handle_message(message)
                self.assertEqual(case.replies("scheduled_stop"), ["定时任务正在启动，请稍后重发。"])
                preview = await case.app._ordinary.preview_stop(binding)
                self.assertFalse(preview.start.resolved.is_set())
                stop = tasks.create_task(case.management.stop_current_binding(
                    target=CurrentBindingTarget(scope.key, binding.id), preview=preview,
                ))
                case.held.release.set()
                await submitting
                self.assertEqual((await stop).disposition, StopDisposition.REQUESTED)
            self.assertFalse(case.runtime._accepting)
            self.assertEqual(case.codex.handles[0].interrupt_count, 1)
            self.assertEqual(case.cleanup.calls, [case.codex.handles[0].thread_id])

    async def test_slow_early_ack_times_out_without_a_second_attempt(self):
        async with stop_case() as (case, tasks):
            case.allow_ack.clear()
            tasks.create_task(case.message("first", "first"))
            await case.held.entered.wait()
            with patch("netizen_cli.runtime.limits.STOP_ACK_ATTEMPT_TIMEOUT_SECONDS", 0), self.assertLogs(
                "netizen_cli.channel_app", level="WARNING",
            ):
                stop = tasks.create_task(case.message("/stop", "stop"))
                await case.first_stop_reply.wait()
                case.held.release.set()
                await stop
            self.assertEqual(case.replies("stop"), [])
            self.assertEqual(case.codex.handles[0].interrupt_count, 1)
            self.assertEqual(case.cleanup.calls, [case.codex.handles[0].thread_id])
