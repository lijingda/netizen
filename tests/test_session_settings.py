from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace

from netizen.bindings import BindingStore, BindingTaskFeedback, BindingTurnSettings
from netizen.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen.model_settings import ModelCatalog, ModelCatalogError
from netizen.session_settings import SessionSettings, SessionSettingsError
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
            BindingTaskFeedback(True, True), MentionContextMode.CATCH_UP,
        )
        self.assertEqual(SessionSettings.from_dict(settings.to_dict()), settings)
        reset = settings.merge({"turn_settings": None, "progress_card_enabled": False})
        self.assertIsNone(reset.turn_settings)
        self.assertEqual(reset.task_feedback, BindingTaskFeedback(True, False))
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
            {"message_context_mode": "all-history"}, {"message_context_mode": None},
            {"sandbox": "none"}, {"context_anchor": {"message_id": "old"}},
        ):
            with self.subTest(partial=partial), self.assertRaises(SessionSettingsError):
                default.merge(partial)
        with self.assertRaises(SessionSettingsError):
            SessionSettings.from_dict({"turn_settings": None})

    def test_new_defaults_resolve_catalog_but_source_inherit_remains_inherit(self):
        catalog = self.catalog()
        defaults = SessionSettings.new_defaults(catalog)
        self.assertEqual(defaults.turn_settings, BindingTurnSettings("model-a", "low", "priority"))
        self.assertEqual(defaults.task_feedback, BindingTaskFeedback(False, True))
        defaults.validate_catalog(catalog)
        self.assertEqual(SessionSettings.new_defaults(None), SessionSettings(task_feedback=BindingTaskFeedback(False, True)))
        self.assertEqual(defaults.merge({"progress_card_enabled": False}).task_feedback, BindingTaskFeedback(False, False))
        with self.assertRaises(ModelCatalogError):
            defaults.merge({"turn_settings": {"model_id": "unavailable", "effort_id": "low", "service_tier_id": "priority"}}).validate_catalog(catalog)
        store = BindingStore()
        try:
            binding = store.create_binding(
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
