from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from netizen_cli.admin.port_config import AdminPortConfigurationError, ConfigFileEditor, ConfigFileSnapshot
from netizen_cli.lark_app import encode_lark_app
from netizen_cli.question_notifications import (
    QuestionNotificationConfigConflict, QuestionNotificationConfigError,
    QuestionNotificationConfigService, QuestionNotificationSettings,
)
from netizen_cli.sessions.policy import SessionAccessPolicy, SessionAccessRule, SessionPolicyService
from netizen_cli.settings import Settings, SettingsError
from netizen_cli.worktrees.settings import WorktreeSettings, WorktreeSettingsService


class QuestionNotificationConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.yaml"
        self.initial = {"instance": {"projectRoot": "/project", "dataDir": "/state"},
                        "adminWeb": {"host": "127.0.0.1", "port": 8787}}
        self.path.write_text(yaml.safe_dump(self.initial))
        app = self.root / "lark-app/config.json"
        app.parent.mkdir()
        app.write_bytes(encode_lark_app("cli_test", "synthetic-secret"))
        app.chmod(0o600)
        self.editor = ConfigFileEditor(ConfigFileSnapshot.read(self.path))
        self.service = QuestionNotificationConfigService(config_editor=self.editor)

    def test_default_then_saved_settings_survive_reload_without_changing_frozen_value(self):
        self.assertEqual(Settings.from_file(self.path, environment={}).question_notifications, QuestionNotificationSettings())
        captured = self.service.current
        settings = QuestionNotificationSettings(mention_initiator=True)
        before = self.service.view()
        self.service.update(settings, expected_revision=before["revision"])
        self.assertEqual(self.service.current, settings)
        self.assertEqual(captured, QuestionNotificationSettings())
        self.assertNotEqual(self.service.view()["revision"], before["revision"])
        self.assertEqual(yaml.safe_load(self.path.read_text()), {**self.initial, "questionNotifications": settings.to_dict()})
        loaded = Settings.from_file(self.path, environment={})
        restarted = QuestionNotificationConfigService(loaded.question_notifications)
        self.assertEqual(restarted.current, settings)

    def test_shared_editor_preserves_other_sections_across_interleaved_saves(self):
        policy = SessionPolicyService(config_editor=self.editor)
        worktrees = WorktreeSettingsService(default_root=self.root / "worktrees", config_editor=self.editor)
        access = SessionAccessPolicy(discovery=SessionAccessRule("disabled"))
        workspace = WorktreeSettings(fetch_before_create=True)
        settings = QuestionNotificationSettings(mention_initiator=True)
        policy.update(access, expected_revision=policy.revision)
        self.service.update(settings, expected_revision=self.service.view()["revision"])
        worktrees.update(workspace, expected_revision=worktrees.revision)
        self.service.update(settings, expected_revision=self.service.view()["revision"])
        self.assertEqual(yaml.safe_load(self.path.read_text()), {
            **self.initial, "sessionTools": access.to_dict(), "worktrees": workspace.to_dict(),
            "questionNotifications": settings.to_dict(),
        })

    def test_stale_revision_and_external_edit_do_not_replace_current_settings(self):
        old = self.service.view()
        settings = QuestionNotificationSettings(mention_initiator=True)
        self.service.update(settings, expected_revision=old["revision"])
        current = self.service.view()
        with self.assertRaises(QuestionNotificationConfigConflict):
            self.service.update(QuestionNotificationSettings(), expected_revision=old["revision"])
        manual = self.path.read_bytes() + b"# manual change\n"
        self.path.write_bytes(manual)
        with self.assertRaises(AdminPortConfigurationError):
            self.service.update(QuestionNotificationSettings(), expected_revision=current["revision"])
        self.assertEqual(self.service.view(), current)
        self.assertEqual(self.path.read_bytes(), manual)

    def test_write_failure_keeps_current_settings_and_allows_later_save(self):
        before = self.service.view()
        settings = QuestionNotificationSettings(mention_initiator=True)
        with patch("netizen_cli.admin.port_config.os.replace", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                self.service.update(settings, expected_revision=before["revision"])
        self.assertEqual(self.service.view(), before)
        self.assertEqual(yaml.safe_load(self.path.read_text()), self.initial)
        self.service.update(settings, expected_revision=before["revision"])
        self.assertEqual(self.service.current, settings)

    def test_readonly_default_service_does_not_save(self):
        service = QuestionNotificationConfigService()
        view = service.view()
        self.assertEqual(view["settings"], QuestionNotificationSettings().to_dict())
        self.assertFalse(view["editable"])
        with self.assertRaises(QuestionNotificationConfigError):
            service.update(QuestionNotificationSettings(), expected_revision=view["revision"])

    def test_invalid_yaml_section_is_rejected_instead_of_defaulting(self):
        default = QuestionNotificationSettings().to_dict()
        for value in (None, [], {}, {**default, "unknown": True},
                      {"mentionInitiator": "true"}, {"mentionInitiator": 1}):
            with self.subTest(value=value):
                self.path.write_text(yaml.safe_dump({**self.initial, "questionNotifications": value}))
                with self.assertRaises(SettingsError):
                    Settings.from_file(self.path, environment={})
