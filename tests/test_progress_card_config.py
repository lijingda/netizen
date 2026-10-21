from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from netizen_cli.admin.port_config import AdminPortConfigurationError, ConfigFileEditor, ConfigFileSnapshot
from netizen_cli.lark_app import encode_lark_app
from netizen_cli.progress_card_config import (
    ProgressCardConfigConflict, ProgressCardConfigError, ProgressCardConfigService,
)
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.sessions.policy import SessionAccessPolicy, SessionAccessRule, SessionPolicyService
from netizen_cli.settings import Settings, SettingsError
from netizen_cli.worktrees.settings import WorktreeSettings, WorktreeSettingsService


class ProgressCardConfigTest(unittest.TestCase):
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
        self.service = ProgressCardConfigService(config_editor=self.editor)

    def test_default_then_saved_settings_survive_reload_without_changing_frozen_value(self):
        self.assertEqual(Settings.from_file(self.path, environment={}).progress_card, ProgressCardSettings())
        captured = self.service.current
        settings = ProgressCardSettings(organization="time", commentary_limit=8)
        before = self.service.view()
        self.service.update(settings, expected_revision=before["revision"])
        self.assertEqual(self.service.current, settings)
        self.assertEqual(captured, ProgressCardSettings())
        self.assertNotEqual(self.service.view()["revision"], before["revision"])
        self.assertEqual(yaml.safe_load(self.path.read_text()), {**self.initial, "progressCard": settings.to_dict()})
        loaded = Settings.from_file(self.path, environment={})
        restarted = ProgressCardConfigService(loaded.progress_card)
        self.assertEqual(restarted.current, settings)

    def test_shared_editor_preserves_other_sections_across_interleaved_saves(self):
        policy = SessionPolicyService(config_editor=self.editor)
        worktrees = WorktreeSettingsService(default_root=self.root / "worktrees", config_editor=self.editor)
        access = SessionAccessPolicy(discovery=SessionAccessRule("disabled"))
        workspace = WorktreeSettings(fetch_before_create=True)
        settings = ProgressCardSettings(show_plan=False, organization="time")
        policy.update(access, expected_revision=policy.revision)
        self.service.update(settings, expected_revision=self.service.view()["revision"])
        worktrees.update(workspace, expected_revision=worktrees.revision)
        self.service.update(settings, expected_revision=self.service.view()["revision"])
        self.assertEqual(yaml.safe_load(self.path.read_text()), {
            **self.initial, "sessionTools": access.to_dict(), "worktrees": workspace.to_dict(),
            "progressCard": settings.to_dict(),
        })

    def test_stale_revision_and_external_edit_do_not_replace_current_settings(self):
        old = self.service.view()
        settings = ProgressCardSettings(organization="time")
        self.service.update(settings, expected_revision=old["revision"])
        current = self.service.view()
        with self.assertRaises(ProgressCardConfigConflict):
            self.service.update(ProgressCardSettings(), expected_revision=old["revision"])
        manual = self.path.read_bytes() + b"# manual change\n"
        self.path.write_bytes(manual)
        with self.assertRaises(AdminPortConfigurationError):
            self.service.update(ProgressCardSettings(), expected_revision=current["revision"])
        self.assertEqual(self.service.view(), current)
        self.assertEqual(self.path.read_bytes(), manual)

    def test_write_failure_keeps_current_settings_and_allows_later_save(self):
        before = self.service.view()
        settings = ProgressCardSettings(organization="time")
        with patch("netizen_cli.admin.port_config.os.replace", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                self.service.update(settings, expected_revision=before["revision"])
        self.assertEqual(self.service.view(), before)
        self.assertEqual(yaml.safe_load(self.path.read_text()), self.initial)
        self.service.update(settings, expected_revision=before["revision"])
        self.assertEqual(self.service.current, settings)

    def test_readonly_default_service_does_not_save(self):
        service = ProgressCardConfigService()
        view = service.view()
        self.assertEqual(view["settings"], ProgressCardSettings().to_dict())
        self.assertFalse(view["editable"])
        with self.assertRaises(ProgressCardConfigError):
            service.update(ProgressCardSettings(), expected_revision=view["revision"])

    def test_invalid_yaml_section_is_rejected_instead_of_defaulting(self):
        default = ProgressCardSettings().to_dict()
        for value in (None, [], {}, {**default, "unknown": True},
                      {**default, "commentary_limit": True}, {**default, "operation_limit": 17},
                      {**default, "organization": "other"}):
            with self.subTest(value=value):
                self.path.write_text(yaml.safe_dump({**self.initial, "progressCard": value}))
                with self.assertRaises(SettingsError):
                    Settings.from_file(self.path, environment={})
