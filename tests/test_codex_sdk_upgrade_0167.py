from __future__ import annotations

import unittest

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.errors import InvalidRequestError, InternalRpcError, TransportClosedError
from openai_codex.generated.v2_all import (
    FileIdThreadItem, ImageGenerationThreadItem, ModelProviderError,
    ModelProviderMismatchModelProviderError, ModelProviderNotConfiguredModelProviderError,
    ModelProviderPolicyConflictModelProviderError, ResultThreadItem,
    ThreadItem, ThreadSectionChangedNotification,
)

from netizen_cli.runtime.native_values import _model_provider_rejection
from netizen_cli.codex_runtime import ModelProviderRejected

from netizen_cli.turn_activity import (
    TurnActivityNotificationProjection,
    project_turn_activity_notification,
)


class CodexSectionRoutingTest(unittest.TestCase):
    def test_section_does_not_complete_or_consume_a_turn_or_goal(self) -> None:
        # Section metadata has Thread identity but no execution identity.
        for goal_owned, section_id in ((False, None), (False, "section-one"),
                                       (True, None), (True, "section-one")):
            with self.subTest(goal_owned=goal_owned, section_id=section_id):
                client = CodexClient(CodexConfig())
                router = client._router
                with router.pending_turn("thread-one") as cursors:
                    subscription = router.prepare_turn(
                        "turn-active", "thread-one", cursors, for_handle=True,
                    )
                self.assertIsNotNone(subscription)
                self.addCleanup(subscription.close)
                goal = router.register_goal("thread-one") if goal_owned else None
                started = client._coerce_notification("turn/started", {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-active", "status": "inProgress", "items": []},
                })
                router.route_notification(started)
                before = subscription._state.next_event
                goal_count = goal._notifications.qsize() if goal is not None else None

                notification = client._coerce_notification("thread/section/changed", {
                    "threadId": "thread-one",
                    "sectionId": section_id,
                })
                self.assertIsInstance(notification.payload, ThreadSectionChangedNotification)
                router.route_notification(notification)
                self.assertIs(router.next_global_notification(), notification)
                self.assertEqual(subscription._state.next_event, before)
                self.assertFalse(subscription._state.completed)
                if goal is not None:
                    self.assertEqual(goal.current_turn(), "turn-active")
                    self.assertEqual(goal._notifications.qsize(), goal_count)
                    self.assertFalse(goal.is_finished())
                self.assertEqual(
                    project_turn_activity_notification(
                        notification, expected_thread_id="thread-one", expected_turn_id="turn-active",
                    ),
                    TurnActivityNotificationProjection(),
                )

                completed = client._coerce_notification("turn/completed", {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-active", "status": "completed", "items": []},
                })
                router.route_notification(completed)
                if goal is None:
                    self.assertTrue(subscription._state.completed)
                    self.assertIs(subscription.next(), started)
                    self.assertIs(subscription.next(), completed)
                    with self.assertRaises(TransportClosedError):
                        subscription.next()
                else:
                    self.assertEqual(subscription._state.next_event, before)
                    self.assertEqual(goal._notifications.qsize(), goal_count + 1)


class ModelProviderContractTest(unittest.TestCase):
    def test_generated_provider_variants_classify_with_specific_explanations(self) -> None:
        variants = (
            (ModelProviderMismatchModelProviderError(type="modelProviderMismatch",
                model_provider="saved", current_model_provider="current"),
                ("saved", "current", "切回")),
            (ModelProviderNotConfiguredModelProviderError(type="modelProviderNotConfigured",
                model_provider="missing"), ("missing", "不存在", "恢复")),
            (ModelProviderPolicyConflictModelProviderError(type="modelProviderPolicyConflict",
                model_provider="saved", required_model_provider="required"),
                ("saved", "required", "组织策略")),
        )
        for variant, expected in variants:
            with self.subTest(type=variant.type):
                data = ModelProviderError(variant).model_dump(mode="json", by_alias=True)
                rejection = _model_provider_rejection(InvalidRequestError(
                    -32600, "opaque native message", data=data,
                ))
                self.assertIsInstance(rejection, ModelProviderRejected)
                for text in (*expected, "未发送给模型", "其他会话不受影响"):
                    self.assertIn(text, str(rejection))

    def test_only_structured_invalid_request_proves_rejection(self) -> None:
        valid = {"type": "modelProviderNotConfigured", "modelProvider": "missing"}
        for error in (
            InvalidRequestError(-32601, "provider rejected", data=valid),
            InternalRpcError(-32600, "provider rejected", data=valid),
            RuntimeError("modelProviderNotConfigured"),
            *(InvalidRequestError(-32600, "provider rejected", data=data) for data in (
                None, [], "modelProviderMismatch", {},
                {"type": "modelProviderMismatch", "modelProvider": "saved"},
                {"type": "unknown", "modelProvider": "saved"},
                {"type": "modelProviderNotConfigured", "modelProvider": 12},
            )),
        ):
            with self.subTest(error=error):
                self.assertIsNone(_model_provider_rejection(error))

    def test_provider_ids_are_bounded_without_control_characters(self) -> None:
        rejection = _model_provider_rejection(InvalidRequestError(-32600, "unused", data={
            "type": "modelProviderMismatch", "modelProvider": "\n\x00\u202e" + "x" * 100,
            "currentModelProvider": "\r\t",
        }))
        text = str(rejection)
        self.assertIn("x" * 79 + "…", text)
        self.assertNotIn("x" * 80, text)
        self.assertIn("（未命名）", text)
        self.assertTrue(all(character.isprintable() for character in text))


class ImageGenerationContractTest(unittest.TestCase):
    def test_inline_alias_and_attachment_discriminator(self) -> None:
        self.assertIs(ImageGenerationThreadItem, ResultThreadItem)
        for fields, expected in (
            ({"result": "inline"}, ResultThreadItem),
            ({"fileId": "file-private"}, FileIdThreadItem),
            ({"fileId": "file-private", "result": "inline"}, FileIdThreadItem),
        ):
            with self.subTest(fields=fields):
                item = ThreadItem.model_validate({"type": "imageGeneration", "id": "image",
                    "status": "completed", "savedPath": "/tmp/probe.png", **fields}).root
                self.assertIs(type(item), expected)
                self.assertEqual(item.saved_path.root, "/tmp/probe.png")


if __name__ == "__main__":
    unittest.main()
