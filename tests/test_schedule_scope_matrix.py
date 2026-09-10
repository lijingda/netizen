"""Channel scope matrix; model interpretation and real Feishu remain live gates."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from lark_channel import OutboundCard

from netizen.bindings import BindingStore
from netizen.channel_app import ChannelApplication
from netizen.domain import FeishuScope, ScheduledOrigin, ScopeKind
from netizen.management import InstanceManagementService, ScopeCoordinator
from netizen.management.service import ManagementRuntimePort
from netizen.projects import ProjectRegistry
from netizen.runtime.contracts import (
    ActiveState, ActiveTurnSnapshot, Submission, SubmitDisposition, TurnOutcome,
)
from netizen.schedules.models import ScheduleRule

from test_channel_app import FakeChannel, FakeMessage, StubRuntime, completed_turn_result, sent_result
from test_schedule_cards import callback, elements, form_values, manager_form


# Topic-mode chats carry a real topic ID even for a top-level user topic.
SCENARIOS = (
    ("private", "p2p", ScopeKind.DIRECT, None),
    ("group", "group", ScopeKind.GROUP, None),
    ("topic_group", "topic", ScopeKind.TOPIC, "omt_source"),
    ("private_topic", "p2p", ScopeKind.TOPIC, "omt_source"),
    ("group_topic", "group", ScopeKind.TOPIC, "omt_source"),
)


class RecordingChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.reply_options = []

    async def reply(self, message, content, opts=None):
        self.reply_options.append(opts)
        return await super().reply(message, content, opts)


class ScheduleScopeMatrixTest(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def fixture(self, scenario):
        label, mode, kind, topic = scenario
        with tempfile.TemporaryDirectory() as directory:
            store = BindingStore()
            projects = ProjectRegistry(store=store, project_root=Path(directory), projects={"work": Path(directory)})
            runtime = StubRuntime()
            runtime.binding_store = store
            channel = RecordingChannel()
            channel.chat_types["oc_source"] = mode

            async def get_chat_info(chat_id):
                channel.chat_info_calls.append(chat_id)
                chat_mode = channel.chat_types.get(chat_id, "group")
                # getChat exposes visibility as chat_type for groups, while
                # real private conversations may omit chat_type entirely.
                if chat_mode == "p2p":
                    return SimpleNamespace(chat_mode=chat_mode)
                return SimpleNamespace(chat_type="private", chat_mode=chat_mode)

            channel.get_chat_info = get_chat_info
            management = InstanceManagementService(bindings=store, projects=projects,
                runtime=ManagementRuntimePort(runtime), scope_coordinator=ScopeCoordinator())
            app = ChannelApplication(app_id="app", channel=channel, runtime=runtime,
                bindings=store, projects=projects, management=management)
            scope = FeishuScope("app", "oc_source", kind, topic)
            source = store.create_channel_binding(scope=scope, project_alias="work", creator_id="ou_user")
            store.assign_native_thread_id(source.id, "native-source")
            source = store.get(source.id)
            initial = []

            async def submit_initial(**request):
                initial.append(request)
                binding = request["binding"]
                native_id = "native-" + request["run_id"]
                store.assign_native_thread_id(binding.id, native_id)
                store.schedules.set_run(request["run_id"], phase="handed_off", initial_turn_id="turn-initial")
                return Submission(SubmitDisposition.STARTED, binding.id, native_id, "turn-initial", lambda: None)

            runtime.submit_initial = submit_initial
            # Same Project, different chat: list/default resolution must stay exact.
            decoy = store.schedules.create(name="other-chat-plan", instructions="other chat", project_alias="work",
                app_id="app", chat_id="oc_other", schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
                enabled=False, request_id="decoy", now=100)
            state = SimpleNamespace(app=app, store=store, channel=channel, runtime=runtime, management=management,
                scope=scope, source=source, mode=mode, label=label, initial=initial, decoy_id=decoy.plan_id)
            try:
                yield state
            finally:
                await app.close()
                await management.close()
                store.close()

    def message(self, state, text, message_id, *, topic_id=None):
        return FakeMessage(text, message_id=message_id, chat_id=state.scope.chat_id,
            chat_type="p2p" if state.mode == "p2p" else "group",
            thread_id=state.scope.topic_id if topic_id is None else topic_id,
            mentioned_bot=state.mode != "p2p")

    async def card_action(self, state, *, value=None, form=None):
        state.channel.fetched_messages["om_card"] = {"code": 0, "data": {"items": [{
            "message_id": "om_card", "chat_id": state.scope.chat_id, "thread_id": state.scope.topic_id,
        }]}}
        await state.app.handle_card_action(SimpleNamespace(message_id="om_card", chat_id=state.scope.chat_id,
            operator=SimpleNamespace(open_id="ou_user"), action=SimpleNamespace(tag="button", value=value, form_value=form)))
        return OutboundCard(card=state.channel.updates[-1][1])

    async def manage(self, state, mode, **fields):
        result = await state.management.schedules.manage({"mode": mode, **fields},
            native_thread_id=state.source.native_thread_id, source="mcp")
        self.assertTrue(result["ok"], result)
        return result

    def fill(self, card, *, name, instructions):
        values = form_values(card)
        values[next(key for key in values if key.startswith("cron_name"))] = name
        values[next(key for key in values if key.startswith("cron_instructions"))] = instructions
        values["cron_timezone"] = "UTC"
        values["cron_kind"] = "interval"
        return values

    async def test_five_source_scopes_through_card_and_native_management_boundaries(self):
        for scenario in SCENARIOS:
            for entry in ("card", "native_management"):
                with self.subTest(scope=scenario[0], entry=entry):
                    async with self.fixture(scenario) as state:
                        await self.lifecycle(state, entry)

    async def test_cancelled_chunk_destination_check_records_possible_delivery(self):
        async with self.fixture(SCENARIOS[0]) as state:
            created = await self.manage(state, "create", name="private completion", instructions="report",
                schedule={"kind": "interval", "timezone": "UTC", "every_minutes": 1}, request_id="create")
            plan = state.store.schedules.get(created["plan"]["id"])
            claim = state.store.schedules.claim_due(plan.id, app_id="app", now=plan.next_due_at)
            state.channel.send_results.append(sent_result("om_root", chat_id=state.scope.chat_id,
                thread_id="omt_fresh", root_id="om_root"))
            await state.app.dispatch_scheduled_run(claim)
            request = state.initial[0]
            state.store.schedules.release(claim.run.id)
            state.channel.reply_results.append(SimpleNamespace(success=True, message_id="om_first",
                chunk_ids=["om_first", "om_last"], raw={"code": 0, "data": {
                    "message_id": "om_last", "chat_id": state.scope.chat_id, "thread_id": "omt_fresh",
                }}))
            checking = asyncio.Event()

            async def fetch_message(message_id):
                checking.set()
                await asyncio.Future()

            state.channel.fetch_message = fetch_message
            completion = asyncio.create_task(state.app.handle_completion(TurnOutcome(
                binding_id=request["binding"].id, thread_id="native-" + claim.run.id,
                turn_id="turn-initial", owner_id=request["owner_id"], origin=request["origin"],
                result=completed_turn_result(final_response="long result"))))
            try:
                await asyncio.wait_for(checking.wait(), timeout=1)
                completion.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await completion
                run = state.store.schedules.get_run(claim.run.id)
                self.assertEqual(run.delivery_state, "unknown")
                self.assertEqual(run.barrier, "released")
                self.assertEqual(len(state.channel.replies), 1)
            finally:
                if not completion.done():
                    completion.cancel()
                    await asyncio.gather(completion, return_exceptions=True)

    async def test_promoted_main_chat_card_is_stale_and_fresh_topic_cron_recovers(self):
        for scenario in SCENARIOS[:2]:
            with self.subTest(scope=scenario[0]):
                async with self.fixture(scenario) as state:
                    old_scope = state.scope
                    await state.app.handle_message(self.message(state, "/cron", "om_old_open"))
                    listed = state.channel.replies[-1][1]
                    form = await self.card_action(state, value=callback(listed, "新建定时任务"))
                    old_save = self.fill(form, name="old draft", instructions="old instructions")

                    # Public fetch now proves the original flat card became a
                    # topic message. Its old exact scope must not be rebound.
                    state.scope = FeishuScope("app", old_scope.chat_id, ScopeKind.TOPIC, "omt_promoted")
                    updates = len(state.channel.updates)
                    await self.card_action(state, form=old_save)
                    self.assertEqual(len(state.channel.updates), updates)
                    self.assertIn("/cron", str(state.channel.replies[-1]))
                    self.assertEqual(state.store.schedules.list(app_id="app", chat_id=old_scope.chat_id), ())
                    self.assertEqual(state.store.active_binding(old_scope.key).id, state.source.id)
                    self.assertIsNone(state.store.active_binding(state.scope.key))

                    await state.app.handle_message(self.message(state, "/cron", "om_new_open"))
                    listed = state.channel.replies[-1][1]
                    form = await self.card_action(state, value=callback(listed, "新建定时任务"))
                    await self.card_action(state, form=self.fill(form, name="new topic draft", instructions="new instructions"))
                    plans = state.store.schedules.list(app_id="app", chat_id=state.scope.chat_id)
                    self.assertEqual([(plan.name, plan.instructions) for plan in plans], [("new topic draft", "new instructions")])
                    self.assertEqual(state.store.active_binding(old_scope.key).id, state.source.id)
                    self.assertIsNone(state.store.active_binding(state.scope.key))

    async def lifecycle(self, state, entry):
        if entry == "card":
            await state.app.handle_message(self.message(state, "/cron", "om_open"))
            listed = state.channel.replies[-1][1]
            self.assertIsInstance(listed, OutboundCard)
            self.assertNotIn(state.decoy_id, str(listed.card))
            form = await self.card_action(state, value=callback(listed, "新建定时任务"))
            detail = await self.card_action(state, form=self.fill(form, name=state.label, instructions="check resource"))
            plans = state.store.schedules.list(app_id="app", chat_id=state.scope.chat_id)
            self.assertEqual(len(plans), 1)
            plan = plans[0]
        else:
            # This verifies the Channel input and exact native-context boundary;
            # it deliberately does not pretend a stub performs model interpretation.
            state.runtime.submission = Submission(SubmitDisposition.STARTED, state.source.id,
                state.source.native_thread_id, "turn-create", lambda: None)
            await state.app.handle_message(self.message(state, "每小时检查资源，创建定时任务", "om_request"))
            self.assertEqual(state.runtime.submit_calls[-1]["binding"].id, state.source.id)
            result = await self.manage(state, "create", name=state.label, instructions="check resource",
                schedule={"kind": "interval", "timezone": "UTC", "every_minutes": 60}, request_id="create")
            plan = state.store.schedules.get(result["plan"]["id"])
        self.assertEqual((plan.chat_id, plan.project_alias), (state.scope.chat_id, "work"))
        self.assertEqual(state.store.active_binding(state.scope.key).id, state.source.id)

        if entry == "card":
            detail = await self.card_action(state, form=manager_form(detail, "cron_filter", option="current"))
            detail = await self.card_action(state, form=manager_form(detail, "cron_manage", plan_id=plan.id))
            form = await self.card_action(state, value=callback(detail, "编辑"))
            detail = await self.card_action(state, form=self.fill(form, name=state.label + " edited", instructions="revised resource"))
            paused = await self.card_action(state, value=callback(detail, "暂停"))
            self.assertFalse(state.store.schedules.get(plan.id).enabled)
            detail = await self.card_action(state, value=callback(paused, "启用"))
            listed = await self.card_action(state, value=callback(detail, "刷新任务"))
            self.assertNotIn("other-chat-plan", str(listed.card))
            self.assertEqual(callback(listed, "编辑")["payload"]["plan_id"], plan.id)
        else:
            await self.manage(state, "update", plan_id=plan.id, expected_revision=plan.revision,
                name=state.label + " edited", instructions="revised resource", request_id="edit")
            edited = state.store.schedules.get(plan.id)
            await self.manage(state, "update", plan_id=plan.id, expected_revision=edited.revision, enabled=False, request_id="pause")
            paused = state.store.schedules.get(plan.id)
            self.assertFalse(paused.enabled)
            await self.manage(state, "update", plan_id=plan.id, expected_revision=paused.revision, enabled=True, request_id="enable")
            listed = await self.manage(state, "list")
            self.assertEqual([item["id"] for item in listed["plans"]], [plan.id])
        plan = state.store.schedules.get(plan.id)
        self.assertTrue(plan.enabled)
        self.assertEqual(plan.instructions, "revised resource")
        self.assertEqual(plan.chat_id, state.scope.chat_id)

        claim = state.store.schedules.claim_due(plan.id, app_id="app", now=plan.next_due_at)
        self.assertIsNotNone(claim)
        promote = state.mode != "topic"
        state.channel.send_results.append(sent_result("om_root", chat_id=state.scope.chat_id,
            thread_id=None if promote else "omt_fresh", root_id=None if promote else "om_root"))
        if promote:
            state.channel.send_results.append(sent_result("om_seed", chat_id=state.scope.chat_id,
                thread_id="omt_fresh", root_id="om_root", parent_id="om_root"))
        await state.app.dispatch_scheduled_run(claim)
        self.assertEqual(len(state.initial), 1)
        request = state.initial[0]
        run = state.store.schedules.get_run(claim.run.id)
        binding = state.store.get(run.binding_id)
        self.assertEqual(binding.scope_key, FeishuScope("app", state.scope.chat_id, ScopeKind.TOPIC, "omt_fresh").key)
        self.assertNotEqual(binding.id, state.source.id)
        self.assertNotEqual(run.topic_id, state.scope.topic_id)
        self.assertIsNone(state.channel.send_calls[0][2].reply_to)
        self.assertEqual(state.store.active_binding(state.scope.key).id, state.source.id)
        self.assertIsInstance(request["origin"], ScheduledOrigin)
        self.assertEqual(request["origin"].message_id, "om_seed" if promote else "om_root")

        state.runtime.active[binding.id] = ActiveTurnSnapshot(binding.id, binding.native_thread_id,
            "turn-initial", request["owner_id"], ActiveState.RUNNING)
        await state.app.handle_message(self.message(state, "/stop", "om_stop", topic_id=run.topic_id))
        self.assertEqual(state.runtime.stop_calls, [binding.id])
        state.runtime.active.pop(binding.id, None)
        state.store.schedules.release(run.id)
        state.channel.reply_results.append(sent_result("om_result", chat_id=state.scope.chat_id,
            thread_id=run.topic_id, root_id=run.root_message_id, parent_id=run.origin_message_id))
        await state.app.handle_completion(TurnOutcome(binding_id=binding.id, thread_id=binding.native_thread_id,
            turn_id="turn-initial", owner_id=request["owner_id"], origin=request["origin"],
            result=completed_turn_result(final_response="scheduled result")))
        opts = state.channel.reply_options[-1]
        self.assertEqual((opts.reply_to, opts.reply_in_thread, opts.reply_target_gone), (run.origin_message_id, True, "fail"))
        self.assertEqual(state.store.schedules.get_run(run.id).delivery_state, "sent")
        state.runtime.submission = Submission(SubmitDisposition.STARTED, binding.id,
            binding.native_thread_id, "turn-followup", lambda: None)
        await state.app.handle_message(self.message(state, "继续检查", "om_followup", topic_id=run.topic_id))
        self.assertEqual(state.runtime.submit_calls[-1]["binding"].id, binding.id)
        self.assertEqual(state.store.active_binding(state.scope.key).id, state.source.id)

        # An old occurrence's later human Turn does not block or capture the
        # next automatic occurrence. It gets another fresh ordinary topic.
        state.runtime.active[binding.id] = ActiveTurnSnapshot(binding.id, binding.native_thread_id,
            "turn-followup", "ou_user", ActiveState.RUNNING)
        current = state.store.schedules.get(plan.id)
        following = state.store.schedules.claim_due(plan.id, app_id="app", now=current.next_due_at)
        self.assertIsNotNone(following)
        state.channel.send_results.append(sent_result("om_root_second", chat_id=state.scope.chat_id,
            thread_id=None if promote else "omt_second", root_id=None if promote else "om_root_second"))
        if promote:
            state.channel.send_results.append(sent_result("om_seed_second", chat_id=state.scope.chat_id,
                thread_id="omt_second", root_id="om_root_second", parent_id="om_root_second"))
        await state.app.dispatch_scheduled_run(following)
        second = state.store.schedules.get_run(following.run.id)
        self.assertEqual(len(state.initial), 2)
        self.assertNotEqual(second.binding_id, binding.id)
        self.assertEqual(second.topic_id, "omt_second")
        self.assertNotEqual(second.root_uuid, run.root_uuid)
        self.assertEqual(state.store.active_binding(binding.scope_key).id, binding.id)
        self.assertEqual(state.runtime.active[binding.id].turn_id, "turn-followup")
        self.assertEqual(state.store.active_binding(state.scope.key).id, state.source.id)

        before_delete = state.store.schedules.get_run(run.id)
        if entry == "card":
            detail = await self.card_action(state, form=manager_form(listed, "cron_manage", plan_id=plan.id))
            recent = await self.card_action(state, value=callback(detail, "最近执行"))
            self.assertIn("已投递", str(recent.card))
            self.assertNotIn("open_url", str(recent.card))
            self.assertEqual(callback(recent, "编辑")["payload"]["plan_id"], plan.id)
            await self.card_action(state, value=callback(recent, "删除计划"))
        else:
            recent = await self.manage(state, "runs", plan_id=plan.id)
            first_projection = next(item for item in recent["runs"] if item["id"] == run.id)
            self.assertEqual(first_projection["scope_key"], binding.scope_key)
            self.assertEqual(first_projection["native_thread_id"], binding.native_thread_id)
            await self.manage(state, "delete", plan_id=plan.id, expected_revision=plan.revision, request_id="delete")
        self.assertTrue(state.store.schedules.get(plan.id, include_deleted=True).deleted)
        self.assertEqual(state.store.get(binding.id).scope_key, binding.scope_key)
        self.assertEqual(state.store.schedules.get_run(run.id), before_delete)
        self.assertEqual(state.store.active_binding(state.scope.key).id, state.source.id)


if __name__ == "__main__":
    unittest.main()
