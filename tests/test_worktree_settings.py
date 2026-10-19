from __future__ import annotations

import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import yaml

from netizen_cli.admin.port_config import AdminPortConfigurationError, ConfigFileEditor, ConfigFileSnapshot
from netizen_cli.lark_app import encode_lark_app
from netizen_cli.sessions.policy import SessionAccessPolicy, SessionAccessRule, SessionPolicyService
from netizen_cli.settings import Settings, SettingsError
from netizen_cli.worktrees.settings import WorktreeSettings, WorktreeSettingsConflict, WorktreeSettingsError, WorktreeSettingsService


class WorktreeSettingsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.yaml"
        self.initial = {"instance": {"projectRoot": "/project", "dataDir": "/state"},
                        "adminWeb": {"host": "127.0.0.1"}, "projects": {"p": "/project/p"}}
        self.path.write_text(yaml.safe_dump(self.initial))
        self.editor = ConfigFileEditor(ConfigFileSnapshot.read(self.path))
        self.service = WorktreeSettingsService(default_root=self.root / "worktrees", config_editor=self.editor)

    def test_defaults_are_instance_local_and_require_no_saved_root(self):
        view = self.service.view()
        self.assertEqual(view["root"], "")
        self.assertEqual(view["effectiveRoot"], str(self.root / "worktrees"))
        self.assertFalse(view["fetchBeforeCreate"])
        self.assertTrue(view["editable"])
        self.assertEqual(WorktreeSettings.from_mapping({}), WorktreeSettings())
        self.assertEqual(WorktreeSettings.from_mapping({"root": ""}), WorktreeSettings())
        self.assertEqual(self.service.root, self.root / "worktrees")

    def test_save_preserves_other_sections_and_updates_only_after_success(self):
        before = self.service.view()
        settings = WorktreeSettings(self.root / "other", True)
        self.service.update(settings, expected_revision=before["revision"])
        self.assertEqual(yaml.safe_load(self.path.read_text()), {**self.initial, "worktrees": settings.to_dict()})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.service.root, settings.root)
        self.assertNotEqual(self.service.revision, before["revision"])
        with self.assertRaises(WorktreeSettingsConflict):
            self.service.update(WorktreeSettings(), expected_revision=before["revision"])
        self.assertEqual(self.service.snapshot()[0], settings)

    def test_shared_editor_allows_interleaved_services_without_clobbering(self):
        policy = SessionPolicyService(config_editor=self.editor)
        disabled = SessionAccessPolicy(discovery=SessionAccessRule("disabled"))
        policy.update(disabled, expected_revision=policy.revision)
        self.service.update(WorktreeSettings(fetch_before_create=True), expected_revision=self.service.revision)
        policy.update(SessionAccessPolicy(), expected_revision=policy.revision)
        self.service.update(WorktreeSettings(self.root / "second"), expected_revision=self.service.revision)
        data = yaml.safe_load(self.path.read_text())
        self.assertEqual(data, {**self.initial, "sessionTools": SessionAccessPolicy().to_dict(),
                               "worktrees": WorktreeSettings(self.root / "second").to_dict()})

    def test_shared_editor_serializes_concurrent_section_writes(self):
        policy = SessionPolicyService(config_editor=self.editor)
        settings = WorktreeSettings(self.root / "concurrent", True)
        access = SessionAccessPolicy(read_send=SessionAccessRule("disabled"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service.update, settings, expected_revision=self.service.revision)
            second = pool.submit(policy.update, access, expected_revision=policy.revision)
            first.result(timeout=5)
            second.result(timeout=5)
        data = yaml.safe_load(self.path.read_text())
        self.assertEqual(data["worktrees"], settings.to_dict())
        self.assertEqual(data["sessionTools"], access.to_dict())

    def test_external_changes_conflict_without_adopting_or_overwriting(self):
        policy = SessionPolicyService(config_editor=self.editor)
        before = self.service.view()
        data = {**self.initial, "worktrees": {"fetchBeforeCreate": True}}
        self.path.write_text(yaml.safe_dump(data))
        for operation in (
            lambda: self.service.update(WorktreeSettings(), expected_revision=self.service.revision),
            lambda: policy.update(SessionAccessPolicy(), expected_revision=policy.revision),
        ):
            with self.assertRaises(AdminPortConfigurationError):
                operation()
        self.assertEqual(self.service.view(), before)
        self.assertEqual(yaml.safe_load(self.path.read_text()), data)
        self.assertTrue(policy.allows_discovery("chat"))

    def test_write_failure_preserves_active_settings_and_shared_editor(self):
        before = self.service.view()
        with patch("netizen_cli.admin.port_config.os.replace", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                self.service.update(WorktreeSettings(fetch_before_create=True), expected_revision=self.service.revision)
        self.assertEqual(self.service.view(), before)
        self.assertEqual(yaml.safe_load(self.path.read_text()), self.initial)
        self.assertEqual(list(self.root.glob(".netizen-config-*")), [])
        self.service.update(WorktreeSettings(fetch_before_create=True), expected_revision=self.service.revision)
        self.assertTrue(self.service.snapshot()[0].fetch_before_create)

    def test_uneditable_service_rejects_save(self):
        service = WorktreeSettingsService(default_root=self.root / "worktrees")
        self.assertFalse(service.editable)
        with self.assertRaises(WorktreeSettingsError):
            service.update(WorktreeSettings(), expected_revision=service.revision)

    def test_invalid_values_are_not_silently_normalized(self):
        for value in (None, [], {"unexpected": True}, {"root": None}, {"root": False},
                      {"root": "relative/path"}, {"root": " /tmp/path"}, {"root": "/tmp/path\n"},
                      {"root": "/tmp/\x00path"}, {"fetchBeforeCreate": "false"}, {"fetchBeforeCreate": 1}):
            with self.subTest(value=value), self.assertRaises(WorktreeSettingsError):
                WorktreeSettings.from_mapping(value)

    def test_settings_file_loading_defaults_saved_settings_and_invalid_values(self):
        app = self.root / "lark-app/config.json"
        app.parent.mkdir()
        app.write_bytes(encode_lark_app("cli_test", "synthetic-secret"))
        app.chmod(0o600)
        self.assertEqual(Settings.from_file(self.path, environment={}).worktrees, WorktreeSettings())
        settings = WorktreeSettings(self.root / "managed", True)
        self.service.update(settings, expected_revision=self.service.revision)
        self.assertEqual(Settings.from_file(self.path, environment={}).worktrees, settings)
        self.path.write_text(yaml.safe_dump({**self.initial, "worktrees": {"fetchBeforeCreate": "false"}}))
        with self.assertRaises(SettingsError):
            Settings.from_file(self.path, environment={})
