from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from netizen_cli.admin.port_config import (
    AdminPortConfigurationError, ConfigFileSnapshot, persist_admin_port,
)
from netizen_cli.sessions.policy import (
    SessionAccessPolicy, SessionAccessRule, SessionPolicyConflict, SessionPolicyError,
    SessionPolicyService,
)
from netizen_cli.settings import Settings, SettingsError
from netizen_cli.lark_app import encode_lark_app


class SessionPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.yaml"
        self.initial = {"instance": {"projectRoot": "/project", "dataDir": "/state"},
                        "adminWeb": {"host": "127.0.0.1"}, "projects": {"test": "/project/test"}}
        self.path.write_text(yaml.safe_dump(self.initial))
        self.service = SessionPolicyService(config_snapshot=ConfigFileSnapshot.read(self.path))

    def test_modes_are_independent_and_match_target_chats(self) -> None:
        self.assertTrue(self.service.allows_discovery("new-chat"))
        self.assertTrue(self.service.allows_read_send("new-chat"))
        for mode, chat_ids, permitted in (
            ("all", (), {"chosen", "other"}),
            ("only", ("chosen",), {"chosen"}),
            ("exclude", ("chosen",), {"other"}),
            ("disabled", (), set()),
            ("only", (), set()),
            ("exclude", (), {"chosen", "other"}),
        ):
            with self.subTest(mode=mode, chat_ids=chat_ids):
                for group in ("discovery", "readSend"):
                    policy = SessionAccessPolicy.from_mapping({group: {"mode": mode, "chatIds": list(chat_ids)}})
                    self.service.update(policy, expected_revision=self.service.revision)
                    method = self.service.allows_discovery if group == "discovery" else self.service.allows_read_send
                    other = self.service.allows_read_send if group == "discovery" else self.service.allows_discovery
                    for chat in ("chosen", "other"):
                        self.assertEqual(method(chat), chat in permitted)
                        self.assertTrue(other(chat))

    def test_save_preserves_other_configuration_and_old_snapshot_conflicts(self) -> None:
        before = self.service.view()
        policy = SessionAccessPolicy(SessionAccessRule("disabled"), SessionAccessRule("exclude", ("oc_private",)))
        self.service.update(policy, expected_revision=before["revision"])
        self.assertEqual(yaml.safe_load(self.path.read_text()), {**self.initial, "sessionTools": policy.to_dict()})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotEqual(self.service.revision, before["revision"])
        self.assertFalse(self.service.allows_discovery("any"))
        self.assertFalse(self.service.allows_read_send("oc_private"))
        self.assertTrue(self.service.allows_read_send("new-chat"))
        with self.assertRaises(SessionPolicyConflict):
            self.service.update(SessionAccessPolicy(), expected_revision=before["revision"])
        self.assertEqual(self.service.view()["readSend"], policy.read_send.to_dict())
        # The service owns a refreshed snapshot after each successful save.
        self.service.update(SessionAccessPolicy(), expected_revision=self.service.revision)
        self.assertTrue(self.service.allows_discovery("any"))
        self.assertNotEqual(self.service.revision, before["revision"])

    def test_call_snapshot_retains_its_rules_after_admin_update(self) -> None:
        admitted, revision = self.service.snapshot()
        disabled = SessionAccessPolicy(SessionAccessRule("disabled"), SessionAccessRule("disabled"))
        self.service.update(disabled, expected_revision=revision)
        current, current_revision = self.service.snapshot()
        self.assertTrue(admitted.discovery.allows("target"))
        self.assertTrue(admitted.read_send.allows("target"))
        self.assertFalse(current.discovery.allows("target"))
        self.assertFalse(current.read_send.allows("target"))
        self.assertNotEqual(current_revision, revision)

    def test_file_edit_or_write_failure_does_not_change_current_rules(self) -> None:
        policy = SessionAccessPolicy(discovery=SessionAccessRule("disabled"))
        before = self.service.view()
        with patch("netizen_cli.admin.port_config.os.replace", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                self.service.update(policy, expected_revision=self.service.revision)
        self.assertEqual(self.service.view(), before)
        self.assertEqual(yaml.safe_load(self.path.read_text()), self.initial)
        self.assertEqual(list(self.path.parent.glob(".netizen-config-*")), [])
        manual = self.path.read_bytes() + b"# manual change\n"
        self.path.write_bytes(manual)
        with self.assertRaises(AdminPortConfigurationError):
            self.service.update(policy, expected_revision=self.service.revision)
        self.assertEqual(self.path.read_bytes(), manual)
        self.assertEqual(self.service.view(), before)

    def test_allocated_port_snapshot_can_be_used_for_later_policy_edits(self) -> None:
        updated = persist_admin_port(ConfigFileSnapshot.read(self.path), 8787)
        service = SessionPolicyService(config_snapshot=updated)
        service.update(SessionAccessPolicy(), expected_revision=service.revision)
        self.assertEqual(yaml.safe_load(self.path.read_text())["adminWeb"]["port"], 8787)

    def test_invalid_rules_do_not_silently_expand_access(self) -> None:
        for value in (None, [], {"send": {}}, {"discovery": None},
                      {"discovery": {"mode": "blacklist"}},
                      {"readSend": {"mode": False}},
                      {"discovery": {"mode": "all", "chatIds": ["oc_hidden"]}},
                      {"discovery": {"mode": "only", "chatIds": "oc_a"}},
                      {"readSend": {"mode": "only", "chatIds": [" oc_a"]}},
                      {"discovery": {"mode": "only", "chatIds": [None]}},
                      {"readSend": {"mode": "only", "chatIds": ["oc_a\n"]}}):
            with self.subTest(value=value), self.assertRaises(SessionPolicyError):
                SessionAccessPolicy.from_mapping(value)

    def test_settings_loads_saved_rules_with_default_all_and_rejects_invalid_rules(self) -> None:
        app_path = self.path.parent / "lark-app/config.json"
        app_path.parent.mkdir()
        app_path.write_bytes(encode_lark_app("cli_test", "synthetic-secret"))
        app_path.chmod(0o600)
        self.assertEqual(Settings.from_file(self.path, environment={}).session_tools, SessionAccessPolicy())
        policy = SessionAccessPolicy(read_send=SessionAccessRule("only", ("oc_second", "oc_first", "oc_first")))
        self.service.update(policy, expected_revision=self.service.revision)
        self.assertEqual(Settings.from_file(self.path, environment={}).session_tools, policy)
        self.assertEqual(policy.read_send.chat_ids, ("oc_first", "oc_second"))
        self.path.write_text(yaml.safe_dump({**self.initial, "sessionTools": {"readSend": {"mode": "typo"}}}))
        with self.assertRaises(SettingsError):
            Settings.from_file(self.path, environment={})
