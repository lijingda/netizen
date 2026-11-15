from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from openai_codex import AsyncCodex
from openai_codex.errors import InvalidRequestError
from openai_codex.generated.v2_all import CodexErrorInfo, TurnError

from netizen_cli.bindings import BindingStore
from netizen_cli.codex_runtime import CodexRuntime, ModelProviderRejected, _final_agent_response
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.error_messages import native_turn_failure
from netizen_cli.partial_answers import PartialAnswer, partial_answers_from_items
from netizen_cli.runtime.native_values import _model_provider_rejection
from netizen_cli.thread_history import AppServerThreadHistory
from netizen_cli.turn_activity import project_turn_activity_notification
from netizen_cli.turn_plan_observer import PinnedTurnActivityObserver


class CodexAnswerContractTest(unittest.TestCase):
    def test_new_answer_fields_preserve_projection_and_single_consumer(self) -> None:
        codex = AsyncCodex()
        codex._initialized = True
        client = codex._client._sync
        router = client._router
        with router.pending_turn("thread-one") as cursors:
            subscription = router.prepare_turn("turn-one", "thread-one", cursors, for_handle=True)
        self.assertIsNotNone(subscription)
        self.addCleanup(subscription.close)
        observer = PinnedTurnActivityObserver(codex)
        notifications = []
        projections = []
        for phase, text in (
            ("commentary", "Checking the repository layout."),
            ("partial_answer", "  Stable answer with whitespace.\n" * 100),
            ("final_answer", "Final answer text."),
        ):
            with self.subTest(phase=phase):
                payload = {
                    "threadId": "thread-one", "turnId": "turn-one", "completedAtMs": 42,
                    "item": {"type": "agentMessage", "id": phase, "phase": phase, "text": text},
                }
                baseline = project_turn_activity_notification(
                    client._coerce_notification("item/completed", payload),
                    expected_thread_id="thread-one", expected_turn_id="turn-one",
                )
                notification = client._coerce_notification("item/completed", {
                    **payload, "contentTokenCount": 23,
                    "item": {**payload["item"], "moderation": {
                        "moderated": True, "display": {"action": "block", "blockType": "unknown"},
                    }},
                })
                projection = project_turn_activity_notification(
                    notification, expected_thread_id="thread-one", expected_turn_id="turn-one",
                )
                self.assertEqual(projection, baseline)
                projections.append(projection)
                notifications.append(notification)
                router.route_notification(notification)

        state = router._turn_states["turn-one"]
        subscribers = dict(state.subscribers)
        events = tuple(state.events.items())
        observation = observer.observe(thread_id="thread-one", turn_id="turn-one", after_cursor=0)
        self.assertEqual(state.subscribers, subscribers)
        self.assertEqual(tuple(state.events.items()), events)
        self.assertEqual(observation.events, (projections[0].event,))
        partial = PartialAnswer("thread-one", "turn-one", "partial_answer",
                                "  Stable answer with whitespace.\n" * 100)
        self.assertEqual(observation.partial_answers, (partial,))
        self.assertFalse(observation.turn_completed)
        self.assertEqual(observation.next_cursor, len(notifications))
        subsequent = observer.observe(thread_id="thread-one", turn_id="turn-one",
                                      after_cursor=observation.next_cursor)
        self.assertEqual(subsequent.events, ())
        self.assertEqual(subsequent.partial_answers, ())
        for notification in notifications:
            self.assertIs(subscription.next(), notification)
        items = [notification.payload.item for notification in notifications]
        self.assertEqual(_final_agent_response(items), "Final answer text.")
        self.assertEqual(partial_answers_from_items(items, thread_id="thread-one", turn_id="turn-one"),
                         (partial,))

    def test_inference_hook_literal_is_displayed_as_native_error_code(self) -> None:
        info = CodexErrorInfo.model_validate("inferenceHookDenied")
        self.assertIs(type(info.root), str)
        error = native_turn_failure(TurnError(message="denied by hook", codexErrorInfo=info))
        self.assertIn("inferenceHookDenied", str(error))
        self.assertIn("denied by hook", str(error))

    def test_untyped_resume_provider_switch_is_not_verified_rejection(self) -> None:
        error = InvalidRequestError(-32600, "Cannot change this thread's model provider from a to b on resume.")
        self.assertIsNone(_model_provider_rejection(error))


class CodexProviderAdmissionContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_remaining_typed_rejections_preserve_admission_and_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            store = BindingStore()
            self.addCleanup(store.close)
            store.bootstrap_project(alias="probe", cwd=str(cwd))
            binding = store.create_channel_binding(
                scope=FeishuScope("app", "chat", ScopeKind.DIRECT),
                project_alias="probe", creator_id="owner",
            )
            store.assign_native_thread_id(binding.id, "saved-thread")
            before = store.get(binding.id)
            codex = SimpleNamespace(thread_resume=AsyncMock(), thread_start=AsyncMock())
            cleanup = SimpleNamespace(clean_thread=AsyncMock())
            runtime = CodexRuntime(codex=codex, bindings=store, terminal_cleanup=cleanup,
                                   automatic_thread_naming=False)
            try:
                for data in (
                    {"type": "modelProviderNotConfigured", "modelProvider": "saved-provider"},
                    {"type": "modelProviderPolicyConflict", "modelProvider": "saved-provider",
                     "requiredModelProvider": "required-provider"},
                ):
                    with self.subTest(variant=data["type"]):
                        codex.thread_resume.side_effect = InvalidRequestError(-32600, "opaque", data=data)
                        with self.assertRaises(ModelProviderRejected) as rejected:
                            await runtime.submit(binding=before, cwd=cwd, input="blocked",
                                                 owner_id="owner", origin=None)
                        self.assertIn("saved-provider", str(rejected.exception))
                        self.assertIn("未发送给模型", str(rejected.exception))
                        if data["type"] == "modelProviderNotConfigured":
                            self.assertIn("/fork", str(rejected.exception))
                        else:
                            self.assertNotIn("/fork", str(rejected.exception))
                        self.assertTrue(runtime._accepting)
                        self.assertNotIn(binding.id, runtime._active)
                        self.assertIsNone(runtime.thread_subscription_snapshot(binding.id))
                        self.assertEqual(store.get(binding.id), before)
                self.assertEqual(codex.thread_resume.await_count, 2)
                codex.thread_resume.assert_awaited_with("saved-thread", include_turns=False)
                codex.thread_start.assert_not_awaited()
                cleanup.clean_thread.assert_not_awaited()
            finally:
                runtime.close_admission()
                await runtime.cancel_tasks()


class CodexHistoryContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_history_does_not_defer_output_and_accepts_deferred_response_field(self) -> None:
        def respond(method, params, *, response_model):
            return response_model.model_validate({"data": [{
                "turnId": "turn-one", "outputDeferred": True,
                "item": {"type": "agentMessage", "id": "answer", "text": "history answer"},
            }], "nextCursor": None, "backwardsCursor": None})

        request = AsyncMock(side_effect=respond)
        codex = SimpleNamespace(_initialized=True, _client=SimpleNamespace(request=request))
        page = await AppServerThreadHistory(codex).list_items("thread-one", "turn-one", limit=3)
        self.assertEqual(request.await_count, 1)
        method, payload = request.call_args.args
        self.assertEqual(method, "thread/items/list")
        self.assertEqual(payload, {"threadId": "thread-one", "turnId": "turn-one", "limit": 3,
                                   "sortDirection": "desc"})
        self.assertNotIn("deferOutput", payload)
        self.assertEqual(len(page.data), 1)
        self.assertEqual(page.data[0].turn_id, "turn-one")
        self.assertEqual(page.data[0].item.root.id, "answer")
        self.assertEqual(page.data[0].item.root.text, "history answer")
