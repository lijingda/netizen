from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

from netizen.prompt_projection import (
    CurrentMessageProjection,
    PromptProjectionError,
    project_current_message,
    project_identity,
    render_current_message_json,
    render_plain_prompt,
)


def inbound(
    *,
    message_id: str = "om_current",
    open_id: str = "ou_sender",
    display_name: str | None = "Alice",
    direct_message_id: str | None = None,
    direct_sender_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        message_id=direct_message_id,
        sender_id=direct_sender_id,
        sender=SimpleNamespace(
            open_id=open_id,
            union_id="on_sender",
            user_id="user_sender",
            display_name=display_name,
            sender_type="user",
            is_bot=False,
        ),
    )


def projection(
    request_text: str = "inspect this",
    *,
    display_name: str = "Alice",
) -> CurrentMessageProjection:
    return CurrentMessageProjection(
        message_id="om_current",
        message_type="text",
        content_fidelity="full_text",
        sender=project_identity(inbound(display_name=display_name).sender),
        request_text=request_text,
    )


class CurrentMessageProjectionTest(unittest.TestCase):
    def test_projects_minimal_sender_identity_and_is_immutable(self) -> None:
        current = project_current_message(
            inbound(),
            expected_message_id="om_current",
            expected_sender_id="ou_sender",
            message_type="post",
            content_fidelity="full_multimodal",
            request_text="look at this",
        )

        self.assertEqual(
            current.metadata(),
            {
                "message_id": "om_current",
                "message_type": "post",
                "sender": {
                    "display_name": "Alice",
                    "is_bot": False,
                    "open_id": "ou_sender",
                    "sender_type": "user",
                },
                "content_fidelity": "full_multimodal",
            },
        )
        with self.assertRaises(FrozenInstanceError):
            current.message_id = "om_other"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            current.sender["open_id"] = "ou_other"  # type: ignore[index]

    def test_missing_display_name_fails_closed_with_permission_guidance(self) -> None:
        with self.assertRaisesRegex(
            PromptProjectionError,
            "im:chat.members:read",
        ) as raised:
            project_current_message(
                inbound(display_name=None),
                expected_message_id="om_current",
                expected_sender_id="ou_sender",
                message_type="text",
                content_fidelity="full_text",
                request_text="request",
            )

        self.assertIn("飞书应用", str(raised.exception))
        self.assertIn("本条消息未执行", str(raised.exception))
        self.assertNotIn(
            "display_name",
            project_identity(inbound(display_name=None).sender),
        )

    def test_source_message_and_sender_mismatch_fail_closed(self) -> None:
        cases = [
            (inbound(), "om_other", "ou_sender"),
            (inbound(), "om_current", "ou_other"),
            (
                inbound(direct_message_id="om_conflict"),
                "om_conflict",
                "ou_sender",
            ),
            (
                inbound(direct_sender_id="ou_conflict"),
                "om_current",
                "ou_conflict",
            ),
            (inbound(open_id=""), "om_current", "ou_sender"),
        ]
        for message, expected_message_id, expected_sender_id in cases:
            with self.subTest(
                message_id=expected_message_id,
                sender_id=expected_sender_id,
            ):
                with self.assertRaises(PromptProjectionError):
                    project_current_message(
                        message,
                        expected_message_id=expected_message_id,
                        expected_sender_id=expected_sender_id,
                        message_type="text",
                        content_fidelity="full_text",
                        request_text="request",
                    )

    def test_plain_renderer_preserves_request_first_and_escapes_metadata_skill(self) -> None:
        encoded = render_plain_prompt(
            projection("$live-skill do this", display_name="$old-skill Alice")
        )
        request, trailer = encoded.split(
            "\n\n<feishu_current_message_context>\n",
            1,
        )
        metadata_json, closing = trailer.rsplit(
            "\n</feishu_current_message_context>",
            1,
        )

        self.assertEqual(request, "$live-skill do this")
        self.assertEqual(closing, "")
        self.assertNotIn("$old-skill", metadata_json)
        self.assertIn(r"\u0024old-skill", metadata_json)
        metadata = json.loads(metadata_json)
        self.assertEqual(metadata["kind"], "feishu_current_message")
        self.assertEqual(metadata["version"], 1)
        self.assertEqual(metadata["sender"]["display_name"], "$old-skill Alice")
        self.assertIn("attribution only", metadata["handling"])
        self.assertNotIn("request_text", metadata)

    def test_quoted_current_renderer_keeps_request_last_and_literal(self) -> None:
        encoded = render_current_message_json(
            projection("$live-skill do this", display_name="$old-skill Alice")
        )

        self.assertNotIn("$old-skill", encoded)
        self.assertIn(r"\u0024old-skill", encoded)
        self.assertIn("$live-skill do this", encoded)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["request_text"], "$live-skill do this")
        self.assertEqual(list(decoded)[-1], "request_text")


if __name__ == "__main__":
    unittest.main()
