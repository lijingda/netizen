from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace

from netizen_cli.bindings import BindingStore, BindingTaskFeedback, BindingTurnSettings
from netizen_cli.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen_cli.model_settings import ModelCatalog, ModelCatalogError
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.session_settings import SessionSettings, SessionSettingsError
from tests.test_model_settings import Effort, effort, model, tier


class SessionSettingsTest(unittest.TestCase):
    def catalog(self):
        return ModelCatalog.from_response(SimpleNamespace(data=[model(
            "model-a", default=True, efforts=[effort(Effort.LOW)],
            default_effort=Effort.LOW, tiers=[tier("priority", "Fast")],
            default_tier="priority",
        )], next_cursor=None))

    def test_full_wire_roundtrip_and_partial_null_reset(self):
        settings = SessionSettings(
            BindingTurnSettings("model-a", "low", "priority"),
            BindingTaskFeedback(True, True, False), MentionContextMode.CATCH_UP,
        )
        self.assertEqual(SessionSettings.from_dict(settings.to_dict()), settings)
        reset = settings.merge({"turn_settings": None, "progress_card_enabled": False})
        self.assertIsNone(reset.turn_settings)
        self.assertEqual(reset.task_feedback, BindingTaskFeedback(True, False, False))
        self.assertEqual(reset.message_context_mode, MentionContextMode.CATCH_UP)
        self.assertEqual(settings.merge({}), settings)
        self.assertEqual(settings.turn_settings.model_id, "model-a")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.task_feedback.progress_card_enabled = False

    def test_strict_shape_and_types_reject_partial_triple_and_unknown_fields(self):
        default = SessionSettings()
        for partial in (
            {"turn_settings": {}}, {"turn_settings": {"model_id": "m"}},
            {"turn_settings": {"model_id": "m", "effort_id": "low", "service_tier_id": 2}},
            {"reaction_pulse_enabled": 1}, {"progress_card_enabled": "false"},
            {"completion_mention_enabled": 1}, {"completion_mention_enabled": None},
            {"message_context_mode": "all-history"}, {"message_context_mode": None},
            {"sandbox": "none"}, {"context_anchor": {"message_id": "old"}},
        ):
            with self.subTest(partial=partial), self.assertRaises(SessionSettingsError):
                default.merge(partial)
        with self.assertRaises(SessionSettingsError):
            SessionSettings.from_dict({"turn_settings": None})
        legacy = default.to_dict()
        del legacy["completion_mention_enabled"]
        with self.assertRaises(SessionSettingsError):
            SessionSettings.from_dict(legacy)

    def test_progress_card_settings_roundtrip_and_unrelated_updates_preserve_preferences(self):
        card = ProgressCardSettings(
            show_commentary=False, show_operations=False, show_plan=False,
            commentary_limit=8, operation_limit=16, commentary_max_chars=320,
            operation_max_chars=240, organization="time",
        )
        settings = SessionSettings(task_feedback=BindingTaskFeedback(True, True, False, card))
        self.assertEqual(SessionSettings.from_dict(settings.to_dict()), settings)
        updated = settings.merge({"turn_settings": None, "completion_mention_enabled": True,
                                  "progress_card_enabled": False})
        self.assertEqual(updated.task_feedback.progress_card_settings, card)
        self.assertEqual(updated.merge({"progress_card_enabled": True}).task_feedback.progress_card_settings, card)
        self.assertEqual(updated.merge({"progress_card_settings": ProgressCardSettings().to_dict()})
                         .task_feedback.progress_card_settings, ProgressCardSettings())
        inherited = updated.merge({"progress_card_settings": None})
        self.assertIsNone(inherited.task_feedback.progress_card_settings)
        self.assertEqual(inherited.task_feedback.progress_card_enabled, updated.task_feedback.progress_card_enabled)
        self.assertEqual(SessionSettings.from_dict(inherited.to_dict()), inherited)
        self.assertIsNone(inherited.merge({"completion_mention_enabled": False}).task_feedback.progress_card_settings)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            card.organization = "content"

    def test_progress_card_settings_validate_complete_shape_boolean_types_and_bounds(self):
        valid = ProgressCardSettings().to_dict()
        for field in ("show_commentary", "show_operations", "show_plan"):
            for invalid in (0, 1, None, "true"):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    ProgressCardSettings.from_dict({**valid, field: invalid})
        for field, maximum in (("commentary_limit", 8), ("operation_limit", 16),
                               ("commentary_max_chars", 320), ("operation_max_chars", 240)):
            for accepted in (1, maximum):
                self.assertEqual(getattr(ProgressCardSettings.from_dict({**valid, field: accepted}), field), accepted)
            for invalid in (0, -1, maximum + 1, True, 2.0, "2", None):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    ProgressCardSettings.from_dict({**valid, field: invalid})
        for invalid in (None, [], {}, {**valid, "extra": False},
                        {**valid, "organization": "timeline"}, {**valid, "organization": False}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ProgressCardSettings.from_dict(invalid)
        for field in valid:
            with self.subTest(missing=field), self.assertRaises(ValueError):
                ProgressCardSettings.from_dict({key: value for key, value in valid.items() if key != field})
        missing = SessionSettings().to_dict()
        del missing["progress_card_settings"]
        with self.assertRaises(SessionSettingsError):
            SessionSettings.from_dict(missing)
        for invalid in (False, {}, {**valid, "commentary_limit": True}):
            with self.assertRaises(SessionSettingsError):
                SessionSettings().merge({"progress_card_settings": invalid})
        with self.assertRaises(ValueError):
            BindingTaskFeedback(progress_card_settings={})

    def test_new_defaults_resolve_catalog_but_source_inherit_remains_inherit(self):
        catalog = self.catalog()
        defaults = SessionSettings.new_defaults(catalog)
        self.assertIsNone(defaults.task_feedback.progress_card_settings)
        self.assertIsNone(defaults.to_dict()["progress_card_settings"])
        self.assertEqual(defaults.turn_settings, BindingTurnSettings("model-a", "low", "priority"))
        self.assertEqual(defaults.task_feedback, BindingTaskFeedback(False, True))
        self.assertTrue(defaults.task_feedback.completion_mention_enabled)
        self.assertFalse(defaults.merge({"completion_mention_enabled": False}).task_feedback.completion_mention_enabled)
        defaults.validate_catalog(catalog)
        self.assertEqual(SessionSettings.new_defaults(None), SessionSettings(task_feedback=BindingTaskFeedback(False, True)))
        self.assertEqual(defaults.merge({"progress_card_enabled": False}).task_feedback, BindingTaskFeedback(False, False))
        with self.assertRaises(ModelCatalogError):
            defaults.merge({"turn_settings": {"model_id": "unavailable", "effort_id": "low", "service_tier_id": "priority"}}).validate_catalog(catalog)
        store = BindingStore()
        try:
            store.bootstrap_project(alias="p", cwd="/tmp/p")
            binding = store.create_channel_binding(
                scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "source-topic"),
                project_alias="p", creator_id="user", turn_settings=None,
                task_feedback=BindingTaskFeedback(True, False),
                message_context_mode=MentionContextMode.CATCH_UP,
                context_anchor=MessageContextAnchor("source-anchor", 100),
            )
            copied = SessionSettings.from_binding(binding)
            self.assertIsNone(copied.turn_settings)
            self.assertEqual(copied.message_context_mode, MentionContextMode.CATCH_UP)
            self.assertEqual(copied.task_feedback, BindingTaskFeedback(True, False))
            self.assertNotIn("context_anchor", copied.to_dict())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
