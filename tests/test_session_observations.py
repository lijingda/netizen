import asyncio
import json
from unittest.mock import AsyncMock, patch
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
import unittest

from lark_channel import OutboundCard

from claude_agent_sdk import RateLimitEvent, RateLimitInfo
from netizen_cli.admin.presentation import _runtime_binding_json
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.group_access import GroupAccessRule, GroupAccessService, GROUP_ACCESS_NOTICE
from netizen_cli.session_views import Availability
from netizen_cli.runtime.contracts import ContextWindowUsage
from tests.support.channel_cards import _card_button_value, _elements, direct_button_event, direct_card_event
from tests.support.channel_fixtures import channel_fixture
from netizen_cli.runtime.claude_status import ClaudeObservations
from tests.support.channel_messages import FakeMessage
from tests.test_claude_channel import owner_channel


class ObservationTest(unittest.IsolatedAsyncioTestCase):
    async def test_channel_uses_only_attributed_samples_and_never_opens_a_reader(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            created = await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            binding = created.binding.id
            async def status():
                await app.handle_message(FakeMessage("/status", message_id="status"))
                return json.dumps(fx.channel.replies[-1][1].card, ensure_ascii=False)
            cold = await status()
            for label in ("未观测", "取不到", "不适用"):
                self.assertIn(label, cold)
            self.assertEqual(clients.calls, [])
            projection = await app._management.binding_status_exact(binding)
            with patch.object(app._management, "binding_status_exact", return_value=replace(projection, observations=None)):
                fallback = await status()
                self.assertIn("上下文窗口：取不到", fallback)
                self.assertIn("订阅：不适用", fallback)
            await app.handle_message(FakeMessage("work", message_id="work"))
            client = clients.calls[-1]
            client.init(model="claude-test")
            private_value = "excluded-native-payload"
            client.stream.put_nowait(RateLimitEvent(RateLimitInfo("rejected", raw={"secret": private_value}), "wrong", "other"))
            client.result(user_message_uuid="other", total_cost_usd=999)
            client.stream.put_nowait(RateLimitEvent(RateLimitInfo("allowed", utilization=0.0), "rate", client.session))
            client.result(total_cost_usd=0, usage={"input_tokens": 0, "output_tokens": True, "ignored": private_value})
            self.assertTrue(await ordinary.wait_idle(3))
            before = list(clients.lookups)
            warm = await status()
            self.assertIn("claude-test", warm)
            self.assertIn("allowed", warm)
            self.assertNotIn(private_value, warm)
            values = ordinary.observations.read(binding, client.session, client.inputs[0]["uuid"]).json()
            self.assertEqual(Path(values["cwd"]["value"]).resolve(), fx.project.resolve())
            self.assertEqual(values["input_tokens"]["value"], 0)
            self.assertEqual(values["totalCostUsd"]["value"], 0)
            self.assertEqual(values["output_tokens"]["availability"], "取不到")
            self.assertIsNotNone(values["model"]["sampledAt"])
            self.assertEqual((len(clients.calls), client.consumers, clients.lookups), (1, 1, before))

    async def test_local_page_includes_off_page_current_and_polling_uses_same_sample(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            first = (await fx.create_binding(scope)).binding
            second = (await fx.create_binding(scope)).binding
            fx.store.activate(scope_key=scope.key, binding_id=first.id)
            deadline = asyncio.get_running_loop().time() + 5
            page = await app._management.query_scope_sessions(scope=scope, limit=1, deadline=deadline)
            self.assertEqual([row.record.binding.id for row in page.items], [second.id])
            self.assertEqual(page.current.record.binding.id, first.id)
            self.assertIsNotNone(page.next_cursor)
            with patch("netizen_cli.channel.session_cards.SESSIONS_PAGE_SIZE", 1):
                card = (await app._sessions_card(scope=scope)).card
            text = json.dumps(card, ensure_ascii=False)
            for label in ("当前会话", "本页会话", "未观测", "下一页", "登记时间"):
                self.assertIn(label, text)
            self.assertNotIn("归档", text)
            self.assertNotIn("删除", text)
            next_page = await app._management.query_scope_sessions(
                scope=scope, cursor=page.next_cursor, limit=1, deadline=deadline,
            )
            self.assertEqual(next_page.items, (next_page.current,))
            statuses = await app._management.binding_statuses_exact(binding_ids=(first.id,), deadline=deadline)
            self.assertEqual(_runtime_binding_json(statuses[0])["observations"], ordinary.observations.read(first.id, None, None).json())
            self.assertIsNone(await app._management.binding_metadata_exact(first.id, deadline=deadline))
            self.assertEqual((clients.calls, clients.lookups), ([], []))

    async def test_terminal_result_uses_original_request_and_safe_native_summary(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            cases = (
                ({"result": ""}, "Claude 已完成本次执行。", True),
                ({"reason": "api_error", "is_error": True, "api_error_status": 503,
                  "result": "unfiltered native error"}, "Claude 请求失败（HTTP 503）。", True),
                ({"subtype": "error_during_execution", "reason": "aborted_tools", "is_error": True},
                 "Claude 本次执行已停止。", False),
            )
            for index, (native, expected, mention) in enumerate(cases):
                origin = "result-" + str(index)
                await app.handle_message(FakeMessage("work", message_id=origin))
                client = clients.calls[-1]
                client.init()
                client.result(**native)
                self.assertTrue(await ordinary.wait_idle(2))
                message_id, reply = fx.channel.replies[-1]
                self.assertEqual(message_id, origin)
                self.assertEqual(getattr(reply, "markdown", reply), expected)
                self.assertEqual([item.open_id for item in getattr(reply, "mentions", ())],
                                 ["ou_user"] if mention else [])
                self.assertEqual(client.consumers, 1)

    async def test_execution_change_never_exposes_previous_result_or_stops_successor(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await fx.create_binding(scope)).binding
            await fx.app.handle_message(FakeMessage("/status", message_id="old"))
            alias = _card_button_value(fx.channel.replies[-1][1], "停止")
            await app.handle_message(FakeMessage("A", message_id="A"))
            first = clients.calls[-1]
            first.init()
            first.result(usage={"input_tokens": 17})
            self.assertTrue(await ordinary.wait_idle(2))
            idle = app._management._runtime.runtime_snapshot_exact(binding.id)
            first_field = (await app._management.binding_status_exact(binding.id)).observations.json()["input_tokens"]
            self.assertEqual((first_field["value"], first_field["executionId"]), (17, first.inputs[0]["uuid"]))

            await app.handle_message(FakeMessage("B", message_id="B"))
            second = clients.calls[-1]
            async def fields():
                return (await app._management.binding_status_exact(binding.id)).observations.json()

            self.assertEqual((await fields())["input_tokens"]["availability"], "未观测")
            second.init(model="claude-opus-4-6[1m]")
            async with asyncio.timeout(2):
                while (await fields())["model"]["value"] is None:
                    await asyncio.sleep(0)
            live = await fields()
            self.assertEqual(live["model"]["executionId"], second.inputs[0]["uuid"])
            self.assertEqual(live["input_tokens"]["availability"], "未观测")
            with patch.object(ordinary, "local_snapshot", side_effect=AssertionError("unexpected runtime reread")):
                captured = await app._management.binding_status_exact(binding.id, snapshot=idle)
                self.assertIs(captured.snapshot, idle)
                self.assertEqual(captured.local_label, "空闲")
                historical = captured.observations.json()
                self.assertEqual(historical["input_tokens"], first_field)
                self.assertNotEqual(historical["model"]["value"], live["model"]["value"])
                unattributed = replace(idle, activity_revision=idle.activity_revision + 1)
                missing = await app._management.binding_status_exact(binding.id, snapshot=unattributed)
                self.assertEqual(missing.observations.json()["model"]["availability"], "未观测")
            direct_card_event(fx.channel, {}, message_id="alias")
            event = direct_button_event(alias, message_id="alias")
            with patch.object(ordinary, "preview_stop", new_callable=AsyncMock) as preview:
                await app.handle_card_action(event)
                await app._handle_status_command(event)
                preview.assert_not_awaited()
            self.assertEqual(second.interrupts, 0)
            second.result(usage={"input_tokens": 31})
            self.assertTrue(await ordinary.wait_idle(2))
            self.assertEqual((await fields())["input_tokens"]["value"], 31)
            again = await app._management.binding_status_exact(binding.id, snapshot=idle)
            self.assertEqual(again.observations.json(), historical)

    async def test_background_phases_and_each_wake_have_their_own_status_samples(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await fx.create_binding(scope)).binding
            await app.handle_message(FakeMessage("work", message_id="origin"))
            client = clients.calls[-1]
            human = client.inputs[0]["uuid"]
            client.init(model="human-model")
            client.start_background()
            both = [{"task_id": "task", "run_id": "run"}, {"task_id": "other", "run_id": "other-run"}]
            client.start_background("other-run", "other", "other-tool", members=both)
            client.stop_snapshot(["task", "other"])
            client.result(snapshot=False, usage={"input_tokens": 11})

            async def until(predicate):
                async with asyncio.timeout(3):
                    while not predicate():
                        await asyncio.sleep(0)

            async def surfaces(expected):
                await app.handle_message(FakeMessage("/status", message_id="status"))
                self.assertIn(expected, str(fx.channel.replies[-1][1].card))
                self.assertIn(expected, str((await app._sessions_card(scope=scope)).card))
                statuses = await app._management.binding_statuses_exact(
                    binding_ids=(binding.id,), deadline=asyncio.get_running_loop().time() + 2)
                row = _runtime_binding_json(statuses[0])
                self.assertEqual(row["localLabel"], expected)
                return row

            await until(lambda: client.background.first_done and client.background.idle)
            pending = ordinary.capture_status(binding.id)
            row = await surfaces("首答已发，后台交接中")
            self.assertEqual(row["observations"]["input_tokens"]["executionId"], human)
            client.end_background(members=both[1:])
            client.system("session_state_changed", state="running")
            await until(lambda: client.background.wake)
            self.assertIs(ordinary.capture_status(binding.id).observations.get("model").availability, Availability.UNOBSERVED)
            first_wake = str(uuid4())
            client.init(user_message_uuids=[], uuid=first_wake, model="first-wake")
            client.stream.put_nowait(RateLimitEvent(RateLimitInfo("allowed"), "rate", client.session))
            await until(lambda: ordinary.capture_status(binding.id).observations.get("limitStatus").value == "allowed")
            row = await surfaces("后台完成轮运行中")
            self.assertEqual(row["observations"]["model"]["executionId"], first_wake)
            self.assertEqual(row["observations"]["limitStatus"]["executionId"], first_wake)
            self.assertEqual(row["observations"]["input_tokens"]["availability"], "未观测")
            client.stop_snapshot(["other"])
            client.wake_result(snapshot=False, result="first completion", usage={"input_tokens": 27})
            await until(lambda: client.background.runs["run"].settled and client.background.idle)
            first = ordinary.capture_status(binding.id)
            self.assertEqual((first.observations.get("input_tokens").execution_id,
                              first.observations.get("input_tokens").value), (first_wake, 27))
            second_wake = str(uuid4())
            client.end_background(run="other-run", task="other")
            client.init(user_message_uuids=[], uuid=second_wake, model="second-wake")
            client.wake_result(run="other-run", result="last completion", usage={"input_tokens": 41})
            self.assertTrue(await ordinary.wait_idle(3))
            row = await surfaces("空闲")
            self.assertEqual(row["observations"]["input_tokens"]["executionId"], second_wake)
            self.assertEqual(row["observations"]["input_tokens"]["value"], 41)
            self.assertEqual(row["observations"]["limitStatus"]["availability"], "未观测")
            for snapshot, execution in ((pending, human), (first, first_wake)):
                old = await app._management.binding_status_exact(binding.id, snapshot=snapshot)
                self.assertEqual(old.observations.get("model").execution_id, execution)
            answers = [reply.markdown for origin, reply in fx.channel.replies if origin == "origin" and hasattr(reply, "markdown")]
            self.assertEqual(answers, ["answer", "first completion", "last completion"])
            self.assertEqual((client.consumers, len(client.inputs)), (1, 1))

    async def test_cron_status_uses_provider_label_and_abandon_requires_bound_provider(self):
        from netizen_cli.backends import BackendUnsupported
        from netizen_cli.management import CurrentBindingTarget

        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await fx.create_binding(scope)).binding
            await app.handle_message(FakeMessage("work", message_id="origin"))
            client = clients.calls[-1]
            client.init()
            client.stop_snapshot(crons=["registered"])
            client.result(snapshot=False)
            async with asyncio.timeout(2):
                while not any("/stop abandon-background " in str(reply) for _, reply in fx.channel.replies):
                    await asyncio.sleep(0)
            await app.handle_message(FakeMessage("/status", message_id="status"))
            self.assertIn("等待后台唤醒，可放弃", str(fx.channel.replies[-1][1].card))
            self.assertIn("等待后台唤醒，可放弃", str((await app._sessions_card(scope=scope)).card))
            self.assertEqual(_runtime_binding_json(await app._management.binding_status_exact(binding.id))["localLabel"],
                             "等待后台唤醒，可放弃")
            target = CurrentBindingTarget(scope.key, binding.id)
            generation = client.inputs[0]["uuid"]
            with patch.object(app._management, "background_control", None):
                with self.assertRaises(BackendUnsupported):
                    await app._management.abandon_background(target, generation, "ou_user")
                await app.handle_message(FakeMessage("/stop abandon-background " + generation, message_id="denied"))
                self.assertFalse(client.closing.is_set())
            await app._management.abandon_background(target, generation, "ou_user")
            self.assertTrue(await ordinary.wait_idle(2))

    async def test_wake_sampling_failure_preserves_delivery_and_idle_ownership(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            binding = (await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))).binding
            await app.handle_message(FakeMessage("work", message_id="origin"))
            client = clients.calls[-1]
            client.init(model="human-model")
            client.start_background()
            client.stop_snapshot(["task"])
            client.result(snapshot=False, usage={"input_tokens": 7})
            async with asyncio.timeout(3):
                while not client.background.first_done:
                    await asyncio.sleep(0)
            with (patch.object(ordinary.observations, "init", side_effect=ValueError),
                  patch.object(ordinary.observations, "rate", side_effect=ValueError),
                  patch.object(ordinary.observations, "result", side_effect=ValueError),
                  self.assertLogs("netizen_cli.runtime.claude_status", level="WARNING") as warnings):
                client.end_background()
                client.init(user_message_uuids=[])
                client.stream.put_nowait(RateLimitEvent(RateLimitInfo("allowed"), "rate", client.session))
                client.wake_result(result="wake completion")
                self.assertTrue(await ordinary.wait_idle(3))
            self.assertEqual(len(warnings.output), 3)
            current = ordinary.capture_status(binding.id)
            self.assertIsNone(current.turn)
            for key in ("model", "input_tokens", "limitStatus"):
                self.assertIs(current.observations.get(key).availability, Availability.UNOBSERVED)
            self.assertEqual(fx.channel.replies[-1][1].markdown, "wake completion")

    async def test_idle_revision_after_failed_init_sampling_does_not_inherit_earlier_execution(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await fx.create_binding(scope)).binding
            await app.handle_message(FakeMessage("A", message_id="A"))
            first = clients.calls[-1]
            first.init(model="first-model")
            first.result(usage={"input_tokens": 17})
            self.assertTrue(await ordinary.wait_idle(2))
            before = ordinary.capture_status(binding.id)
            self.assertEqual(before.observations.get("input_tokens").value, 17)

            with patch.object(ordinary.observations, "init", side_effect=ValueError), self.assertLogs(
                "netizen_cli.runtime.claude_status", level="WARNING",
            ):
                await app.handle_message(FakeMessage("B", message_id="B"))
                second = clients.calls[-1]
                second.init(model="second-model")
                second.result(usage={"input_tokens": 31})
                self.assertTrue(await ordinary.wait_idle(2))
            current = await app._management.binding_status_exact(binding.id)
            self.assertGreater(current.snapshot.activity_revision, before.activity_revision)
            retained = await app._management.binding_status_exact(binding.id, snapshot=current.snapshot)
            for view in (current, retained):
                self.assertEqual(view.local_label, "空闲")
                for key in ("model", "input_tokens"):
                    field = view.observations.get(key)
                    self.assertIs(field.availability, Availability.UNOBSERVED)
                    self.assertIsNone(field.execution_id)
            historical = await app._management.binding_status_exact(binding.id, snapshot=before)
            self.assertEqual(historical.observations.get("model").value, "first-model")

    async def test_sampling_failures_leave_native_result_and_delivery_intact(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            with (patch.object(ordinary.observations, "init", side_effect=ValueError("private")),
                  patch.object(ordinary.observations, "rate", side_effect=ValueError("private")),
                  patch.object(ordinary.observations, "result", side_effect=ValueError("private")),
                  self.assertLogs("netizen_cli.runtime.claude_status", level="WARNING") as warnings):
                await app.handle_message(FakeMessage("work", message_id="source"))
                client = clients.calls[-1]
                client.init()
                client.stream.put_nowait(RateLimitEvent(RateLimitInfo("allowed"), "event", client.session))
                client.result(result="native conclusion")
                self.assertTrue(await ordinary.wait_idle(2))
            self.assertEqual(len(warnings.output), 3)
            self.assertNotIn("private", str(warnings.output))
            origin, answer = fx.channel.replies[-1]
            self.assertEqual((origin, answer.markdown, client.interrupts), ("source", "native conclusion", 0))

    async def test_group_refusal_precedes_capabilities_and_question_precedes_schedule(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            event = direct_button_event({"kind": "netizen_cron"}, form_value={"netizen_question_choice": "choice"})
            event.chat_id = "oc_group"
            fx.channel.chat_types["oc_group"] = "group"
            app._group_access = GroupAccessService(GroupAccessRule("only"))
            await app.handle_card_action(event)
            self.assertEqual(fx.channel.replies[-1][1], GROUP_ACCESS_NOTICE)
            app._group_access = GroupAccessService()
            await app.handle_card_action(event)
            self.assertIn("原生提问", fx.channel.replies[-1][1])

    async def test_local_fourth_page_activation_can_return_to_every_predecessor(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            records = [(await fx.create_binding(scope)).binding for _ in range(4)]
            direct_card_event(fx.channel, {}, message_id="page")
            async def click(card, label):
                await app.handle_card_action(direct_button_event(_card_button_value(card, label), message_id="page"))
                return OutboundCard(card=fx.channel.updates[-1][1])
            with patch("netizen_cli.channel.session_cards.SESSIONS_PAGE_SIZE", 1):
                card = await app._sessions_card(scope=scope)
                for _ in range(3):
                    card = await click(card, "下一页")
                card = await click(card, "设为当前")
                self.assertEqual(fx.store.active_binding(scope.key).id, records[0].id)
                for page in (3, 2, 1):
                    card = await click(card, "上一页")
                    self.assertIn(f"第 {page} 页", str(card.card))
                    self.assertIn(records[-page].short_id, str(card.card))
                with patch.object(app._management, "binding_status_exact", side_effect=TimeoutError):
                    fallback = (await app._sessions_card(scope=scope)).card
                buttons = [button["text"]["content"] for button in _elements(fallback, "button")]
                for action in ("归档", "删除", "重新检查", "停止"):
                    self.assertNotIn(action, buttons)
            with patch.object(ordinary, "local_snapshot", return_value=replace(
                ordinary.local_snapshot(records[0].id), unavailable=True,
            )):
                await app.handle_message(FakeMessage("/status", message_id="unavailable"))
                text = str(fx.channel.replies[-1][1].card)
                self.assertIn("无法确认 Claude 客户端已关闭", text)
                self.assertNotIn("unknown", text)

    async def test_codex_status_preserves_context_sample_and_model_read_times(self):
        async with channel_fixture() as fx:
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await fx.create_binding(scope)).binding
            with patch.object(fx.runtime, "context_window_usage", side_effect=AssertionError("lazy read")):
                self.assertIs(fx.app._context_usage_field(binding, active_turn=False).availability, Availability.NOT_APPLICABLE)
            fx.runtime.materialize(binding.id, "native", cwd=fx.project)
            binding = fx.store.get(binding.id)
            self.assertIs(fx.app._context_usage_field(binding, active_turn=False).availability, Availability.UNOBSERVED)
            with patch.object(fx.runtime, "context_window_read_failed_at", return_value=12.5):
                failed = fx.app._context_usage_field(binding, active_turn=False)
                self.assertEqual((failed.availability, failed.sampled_at), (Availability.UNAVAILABLE, 12.5))
            fx.runtime.context_window_usage_values[binding.id] = ContextWindowUsage(7, 100, 123)
            for active in (True, False):
                field = fx.app._context_usage_field(binding, active_turn=active)
                self.assertEqual((field.sampled_at, field.source, field.usage.used_tokens), (123, "Codex token usage", 7))
            with patch.object(fx.runtime, "context_window_usage", side_effect=OSError), self.assertRaises(OSError):
                fx.app._context_usage_field(binding, active_turn=False)
            model = await fx.app._binding_model_status(binding)
            for field in (model.model, model.effort, model.speed, model.configuration):
                self.assertIsNotNone(field.sampled_at)
                self.assertEqual(field.source, "Binding settings")
            self.assertIs(model.model.availability, Availability.UNOBSERVED)
            self.assertIs(model.configuration.availability, Availability.OBSERVED)

    def test_bound_and_execution_identity(self):
        samples = ClaudeObservations(capacity=1, clock=lambda: 1.5)
        samples.init("a", "x", "first", "old", "/project")
        samples.init("a", "y", "second", "new", "/project")
        samples.rate("a", "y", "first", RateLimitInfo("rejected"))
        self.assertFalse(samples.read("a", "x", "first").fields)
        self.assertEqual(samples.read("a", "y", "second").json()["limitStatus"]["availability"], "未观测")
        samples.init("b", "z", "third", {"private": "ignored"}, "/different")
        self.assertFalse(samples.read("a", "y", "second").fields)
        self.assertEqual(samples.read("b", "z", "third").json()["model"]["availability"], "取不到")
