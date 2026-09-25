from __future__ import annotations

import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from netizen.cards import (
    CardActionError,
    binding_configured_card,
    binding_created_card,
    config_card,
    decode_card_form,
    new_binding_card,
)
from netizen.cards.controls import decode_session_settings_form
from netizen.cards.scheduled import schedule_form_card
from netizen.domain import CardControlName, FeishuScope, MentionContextMode, ScopeKind
from netizen.projects import Project


def _elements(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _elements(child)
    elif isinstance(value, list):
        for child in value:
            yield from _elements(child)


class AutonomyCardsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        self.projects = (Project("test", Path("/workspace/test"), True, 3),)

    def card(self, prefix: str, **overrides: Any) -> Any:
        kwargs = {"scope": self.scope, **overrides}
        if prefix == "new":
            return new_binding_card(projects=self.projects, **kwargs)
        return config_card(
            binding_id="binding-1", short_id="b1", project_alias="test",
            settings_revision=7, context_revision=11, feedback_revision=13,
            turn_settings=None, catalog=None, **kwargs,
        )

    def fields(self, card: Any) -> dict[str, Any]:
        return {
            item["name"]: item for item in _elements(card.card)
            if item.get("tag") == "select_static"
        }

    def values(self, prefix: str) -> dict[str, Any]:
        return {
            name: field.get("initial_option", field["options"][0]["value"])
            for name, field in self.fields(self.card(prefix)).items()
        }

    def decode(self, payload: dict[str, Any], *, scope: FeishuScope | None = None) -> Any:
        return decode_card_form(
            scope=scope or self.scope, message_id="om_card", sender_id="ou_user",
            tag="button", form_value=payload,
        )

    def test_new_and_config_explicitly_offer_experimental_choice_in_groups_and_topics(self) -> None:
        scopes = (self.scope, FeishuScope("cli_test", "oc_group", ScopeKind.TOPIC, "omt_topic"))
        for scope in scopes:
            for prefix in ("new", "config"):
                with self.subTest(scope=scope.kind, prefix=prefix):
                    card = self.card(prefix, scope=scope, show_autonomy=True)
                    fields = self.fields(card)
                    choice = fields[f"{prefix}_context_mode"]
                    self.assertEqual(
                        [option["value"] for option in choice["options"]],
                        ["context-mode:v1:current-only", "context-mode:v1:catch-up", "autonomy:v1:enabled"],
                    )
                    self.assertIn("实验", str(choice["options"][-1]))
                    self.assertEqual(choice["initial_option"], "context-mode:v1:current-only")
                    self.assertFalse(any("provider" in name or "key" in name or "url" in name for name in fields))
                    self.assertIn("判定跳过时不发送通知", str(card.card))
                    self.assertNotIn("机器人始终只响应", str(card.card))

    def test_saved_autonomy_is_projected_without_a_new_context_enum(self) -> None:
        for prefix in ("new", "config"):
            with self.subTest(prefix=prefix):
                card = self.card(prefix, show_autonomy=True, autonomy_enabled=True)
                choice = self.fields(card)[f"{prefix}_context_mode"]
                self.assertEqual(choice["initial_option"], "autonomy:v1:enabled")
        self.assertEqual(set(MentionContextMode), {MentionContextMode.CURRENT_ONLY, MentionContextMode.CATCH_UP})

    def test_defaults_direct_and_suppressed_context_do_not_offer_autonomy(self) -> None:
        direct = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        cases = ({}, {"scope": direct, "show_autonomy": True}, {"show_autonomy": True, "allow_context_mode": False})
        for prefix in ("new", "config"):
            for kwargs in cases:
                with self.subTest(prefix=prefix, kwargs=kwargs):
                    card = self.card(prefix, **kwargs)
                    self.assertNotIn("autonomy:v1", str(card.card))
                    self.assertNotIn("自主模式", str(card.card))

    def test_autonomy_decodes_to_independent_intent_and_current_only(self) -> None:
        for prefix in ("new", "config"):
            with self.subTest(prefix=prefix):
                values = self.values(prefix)
                values[f"{prefix}_context_mode"] = "autonomy:v1:enabled"
                result = self.decode(values)
                self.assertTrue(result.autonomy_enabled)
                self.assertIs(result.message_context_mode, MentionContextMode.CURRENT_ONLY)
                self.assertEqual(values[f"{prefix}_context_mode"], "autonomy:v1:enabled")
                self.assertEqual(result.scope, self.scope)
                self.assertEqual(result.source_id, "om_card")
                self.assertEqual(result.sender_id, "ou_user")
                if prefix == "config":
                    self.assertIs(result.name, CardControlName.CONFIGURE_BINDING)
                    self.assertEqual(result.binding_id, "binding-1")
                    self.assertEqual((result.expected_settings_revision, result.expected_context_revision, result.feedback_revision), (7, 11, 13))
                else:
                    self.assertIs(result.name, CardControlName.CREATE_BINDING)
                    self.assertEqual(result.project_alias, "test")
                    self.assertEqual(result.expected_revision, 3)

    def test_original_choices_explicitly_disable_autonomy_and_absent_choice_preserves_legacy(self) -> None:
        for prefix in ("new", "config"):
            for mode in MentionContextMode:
                with self.subTest(prefix=prefix, mode=mode):
                    values = self.values(prefix)
                    values[f"{prefix}_context_mode"] = f"context-mode:v1:{mode.value}"
                    result = self.decode(values)
                    self.assertFalse(result.autonomy_enabled)
                    self.assertIs(result.message_context_mode, mode)
            values = self.values(prefix)
            del values[f"{prefix}_context_mode"]
            result = self.decode(values)
            self.assertIsNone(result.autonomy_enabled)
            self.assertIs(result.message_context_mode, MentionContextMode.CURRENT_ONLY)

    def test_unknown_values_extra_fields_and_direct_autonomy_are_rejected(self) -> None:
        for prefix in ("new", "config"):
            for value in ("autonomy:v2:enabled", "autonomy:v1:disabled", "context-mode:v1:autonomous", True):
                with self.subTest(prefix=prefix, value=value), self.assertRaises(CardActionError):
                    self.decode({**self.values(prefix), f"{prefix}_context_mode": value})
            with self.subTest(prefix=prefix, extra=True), self.assertRaises(CardActionError):
                self.decode({**self.values(prefix), f"{prefix}_autonomy_enabled": True})
            direct = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            with self.subTest(prefix=prefix, direct=True), self.assertRaises(CardActionError):
                self.decode({**self.values(prefix), f"{prefix}_context_mode": "autonomy:v1:enabled"}, scope=direct)

    def test_scheduled_forms_neither_offer_nor_decode_autonomy(self) -> None:
        card = schedule_form_card(
            self.scope, projects=self.projects, default_timezone="Asia/Shanghai", allow_context_mode=True,
        )
        self.assertNotIn("autonomy:v1", str(card.card))
        self.assertNotIn("自主模式", str(card.card))
        self.assertIn("context-mode:v1:catch-up", str(card.card))
        values = {
            "cron_model": "new-model:v1:inherit",
            "cron_context_mode": "autonomy:v1:enabled",
            "cron_task_reactions": "task-feedback:v2:off",
            "cron_progress_card": "task-feedback:v2:on",
            "cron_completion_mention": "task-feedback:v2:on",
        }
        with self.assertRaises(CardActionError):
            decode_session_settings_form(values, prefix="cron")

    def test_success_cards_describe_enabled_mode_without_claiming_every_message_requires_mention(self) -> None:
        for renderer in (binding_created_card, binding_configured_card):
            with self.subTest(renderer=renderer.__name__):
                card = renderer(short_id="b1", project_alias="test", settings=None, autonomy_enabled=True)
                self.assertIn("自主模式（实验）", str(card.card))
                self.assertNotIn("每条消息都需要 @", str(card.card))
                ordinary = renderer(short_id="b1", project_alias="test", settings=None)
                self.assertNotIn("自主模式", str(ordinary.card))


if __name__ == "__main__":
    unittest.main()
