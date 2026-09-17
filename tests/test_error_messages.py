from __future__ import annotations

import unittest
from types import SimpleNamespace

from openai_codex.errors import InternalRpcError, TransportClosedError
from openai_codex.types import TurnError
from pydantic import BaseModel, TypeAdapter, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from netizen.error_messages import describe_error, native_turn_failure


class ErrorMessagesTest(unittest.TestCase):
    def test_keeps_explicit_operation_context_and_underlying_rpc_reason(self) -> None:
        error = RuntimeError("状态无法确认，暂未启动新一轮")
        error.__cause__ = InternalRpcError(
            -32603, "provider is unavailable", data={"request": "private prompt"},
        )

        message = describe_error(error)

        self.assertIn("状态无法确认", message)
        self.assertIn("InternalRpcError", message)
        self.assertIn("-32603", message)
        self.assertIn("provider is unavailable", message)
        self.assertNotIn("private prompt", message)

    def test_implicit_context_is_not_published_and_cause_cycles_are_bounded(self) -> None:
        error = RuntimeError("public reason")
        error.__context__ = RuntimeError("unrelated private exception")
        self.assertEqual(describe_error(error), "public reason")
        error.__cause__ = error
        self.assertEqual(describe_error(error), "public reason")

    def test_long_wrapper_does_not_hide_underlying_reason_or_exceed_limit(self) -> None:
        error = RuntimeError("outer context " * 100)
        error.__cause__ = InternalRpcError(-32603, "capacity exhausted " * 100)

        message = describe_error(error, limit=160)

        self.assertLessEqual(len(message), 160)
        self.assertIn("capacity exhausted", message)
        self.assertIn("-32603", message)
        self.assertEqual(describe_error(error, limit=0), "")
        self.assertEqual(describe_error(error, limit=1), "…")

    def test_empty_transport_and_timeout_errors_explain_missing_confirmation(self) -> None:
        for error in (TimeoutError(), ConnectionError(), TransportClosedError()):
            with self.subTest(error=type(error).__name__):
                message = describe_error(error)
                self.assertIn(type(error).__name__, message)
                self.assertIn("未收到确认结果", message)

    def test_sensitive_messages_are_filtered_before_truncation(self) -> None:
        for sensitive in (
            "Authorization: Bearer private-credential",
            "https://name:private-credential@host.invalid/path",
            "https://host.invalid/path?token=private-credential",
            "API_KEY=private-credential",
            "sk-privatecredential123456789",
            "Cookie: session=private-credential",
        ):
            with self.subTest(sensitive=sensitive):
                error = InternalRpcError(-32603, sensitive + " other text" * 100)
                message = describe_error(error, limit=80)
                self.assertIn("-32603", message)
                self.assertNotIn("private", message)
                self.assertLessEqual(len(message), 80)

    def test_arbitrary_payloads_and_tracebacks_are_not_error_messages(self) -> None:
        for error in (
            RuntimeError({"prompt": "private-payload"}),
            RuntimeError("{'prompt': 'private-payload'}"),
            RuntimeError("['private-payload']"),
            RuntimeError("Traceback (most recent call last):\nprivate-payload"),
            RuntimeError("unrelated", "private-payload"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertNotIn("private-payload", describe_error(error))

    def test_sdk_validation_error_explains_mismatch_without_native_payload(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            TurnError.model_validate({"message": {"prompt": "private-native-payload"}})

        message = describe_error(caught.exception)

        self.assertIn("ValidationError", message)
        self.assertIn("数据格式不符合预期", message)
        self.assertIn("1 处校验错误", message)
        self.assertNotIn("private-native-payload", message)
        self.assertNotIn("input_value", message)

    def test_sdk_validation_cause_preserves_operation_context_and_bound(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            TurnError.model_validate({"payload": "private-native-payload"})
        error = RuntimeError("无法读取上一轮状态")
        error.__cause__ = caught.exception

        message = describe_error(error, limit=80)

        self.assertIn("无法读取上一轮状态", message)
        self.assertIn("ValidationError", message)
        self.assertIn("数据格式不符合预期", message)
        self.assertNotIn("private-native-payload", message)
        self.assertLessEqual(len(message), 80)

    def test_validation_summary_hides_dynamic_paths_and_custom_validator_data(self) -> None:
        with self.assertRaises(ValidationError) as nested:
            TypeAdapter(dict[str, list[int]]).validate_python({
                "private-dynamic-key": ["private-native-value", "private-other-value"],
            })

        class CustomValidated(BaseModel):
            value: str

            @field_validator("value")
            @classmethod
            def reject(cls, value: str) -> str:
                raise PydanticCustomError(
                    "private-custom-kind",
                    "private-custom-message {value}",
                    {"value": value},
                )

        with self.assertRaises(ValidationError) as custom:
            CustomValidated(value="private-validator-input")

        for error, expected_count in (
            (nested.exception, 2),
            (custom.exception, 1),
        ):
            with self.subTest(expected_count=expected_count):
                self.assertIn("private", str(error))
                message = describe_error(error)
                self.assertIn("ValidationError", message)
                self.assertIn(f"{expected_count} 处校验错误", message)
                self.assertNotIn("private", message)

    def test_native_codes_and_public_messages_cover_multiple_failure_causes(self) -> None:
        for code in ("serverOverloaded", "rateLimitExceeded", "unauthorized", "sandboxError"):
            with self.subTest(code=code):
                native = TurnError.model_validate({
                    "message": "use another available model",
                    "codexErrorInfo": code,
                    "additionalDetails": "private raw response",
                })
                message = describe_error(native_turn_failure(native))
                self.assertIn(code, message)
                self.assertIn("use another available model", message)
                self.assertNotIn("private raw response", message)

    def test_native_structured_code_keeps_only_variant_and_http_status(self) -> None:
        native = TurnError.model_validate({
            "message": "upstream connection failed",
            "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 503}},
            "additionalDetails": "private raw request",
        })

        message = describe_error(native_turn_failure(native))

        self.assertIn("httpConnectionFailed", message)
        self.assertIn("HTTP 503", message)
        self.assertIn("upstream connection failed", message)
        self.assertNotIn("private raw request", message)

    def test_native_message_redaction_preserves_code_and_never_uses_arbitrary_repr(self) -> None:
        native = TurnError.model_validate({
            "message": "Authorization: Bearer private-credential",
            "codexErrorInfo": "unauthorized",
        })
        message = describe_error(native_turn_failure(native))
        self.assertIn("unauthorized", message)
        self.assertNotIn("private-credential", message)
        for invalid in (None, {"message": "private-payload"}, SimpleNamespace(message="private-payload")):
            self.assertNotIn("private-payload", describe_error(native_turn_failure(invalid)))


if __name__ == "__main__":
    unittest.main()
