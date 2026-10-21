from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import yaml

from netizen_cli.admin.port_config import ConfigFileEditor, ConfigFileSnapshot, write_configuration
from netizen_cli.progress_card_config import ProgressCardConfigConflict, ProgressCardConfigService
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.sessions.policy import (
    SessionAccessPolicy, SessionAccessRule, SessionPolicyConflict, SessionPolicyService,
)
from netizen_cli.worktrees.settings import (
    WorktreeSettings, WorktreeSettingsConflict, WorktreeSettingsService,
)


class ConfigurationPublicationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.yaml"
        self.path.write_text(yaml.safe_dump({"instance": {"dataDir": "/state"}}))
        self.editor = ConfigFileEditor(ConfigFileSnapshot.read(self.path))

    def cases(self):
        progress = ProgressCardConfigService(config_editor=self.editor)
        worktrees = WorktreeSettingsService(default_root=self.root / "worktrees", config_editor=self.editor)
        policy = SessionPolicyService(config_editor=self.editor)
        return (
            ("progressCard", progress, ProgressCardSettings(organization="time", show_plan=False),
             ProgressCardConfigConflict, lambda: (progress.current, progress.view())),
            ("worktrees", worktrees, WorktreeSettings(self.root / "updated", True),
             WorktreeSettingsConflict, lambda: (worktrees.snapshot()[0], worktrees.view(),
                 worktrees.snapshot(), worktrees.root, worktrees.revision, worktrees.editable)),
            ("sessionTools", policy, SessionAccessPolicy(
                SessionAccessRule("disabled"), SessionAccessRule("exclude", ("chosen",))),
             SessionPolicyConflict, lambda: (policy.snapshot()[0], policy.view(), policy.snapshot(),
                 policy.revision, policy.allows_discovery("chosen"), policy.allows_read_send("chosen"),
                 policy.editable)),
        )

    def test_reads_keep_the_published_state_while_a_save_is_pending(self):
        for section, service, settings, _, read in self.cases():
            for fail_write in (True, False):
                with self.subTest(section=section, fail_write=fail_write):
                    before = read()
                    content_before = self.path.read_bytes()
                    writing = threading.Event()
                    release = threading.Event()

                    def slow_write(snapshot, loaded):
                        writing.set()
                        if not release.wait(timeout=10):
                            raise AssertionError("test did not release the configuration writer")
                        if fail_write:
                            raise OSError("synthetic write failure")
                        return write_configuration(snapshot, loaded)

                    with patch("netizen_cli.admin.port_config.write_configuration", side_effect=slow_write):
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            saving = pool.submit(service.update, settings, expected_revision=before[1]["revision"])
                            try:
                                self.assertTrue(writing.wait(timeout=2), "save did not reach the writer")
                                self.assertEqual(pool.submit(read).result(timeout=2), before)
                                self.assertEqual(self.path.read_bytes(), content_before)
                            finally:
                                release.set()
                            if fail_write:
                                with self.assertRaisesRegex(OSError, "synthetic write failure"):
                                    saving.result(timeout=2)
                            else:
                                saving.result(timeout=2)

                    if fail_write:
                        self.assertEqual(read(), before)
                        self.assertEqual(self.path.read_bytes(), content_before)
                    else:
                        after = read()
                        self.assertEqual(after[0], settings)
                        self.assertNotEqual(after[1]["revision"], before[1]["revision"])
                        if section == "progressCard":
                            expected_view = {"settings": settings.to_dict(), "editable": True}
                        elif section == "worktrees":
                            expected_view = {"root": str(settings.root), "effectiveRoot": str(settings.root),
                                             "fetchBeforeCreate": settings.fetch_before_create, "editable": True}
                        else:
                            expected_view = settings.to_dict()
                        self.assertEqual(after[1], {**expected_view, "revision": after[1]["revision"]})
                        if section != "progressCard":
                            self.assertEqual(after[2], (settings, after[1]["revision"]))
                        self.assertEqual(yaml.safe_load(self.path.read_text())[section], settings.to_dict())

    def test_concurrent_saves_using_the_same_revision_allow_only_one_writer(self):
        for section, service, settings, conflict, read in self.cases():
            with self.subTest(section=section):
                before = read()
                writing = threading.Event()
                competing = threading.Event()
                release = threading.Event()

                def slow_write(snapshot, loaded):
                    writing.set()
                    if not release.wait(timeout=10):
                        raise AssertionError("test did not release the configuration writer")
                    return write_configuration(snapshot, loaded)

                def competing_save():
                    competing.set()
                    service.update(before[0], expected_revision=before[1]["revision"])

                with patch("netizen_cli.admin.port_config.write_configuration", side_effect=slow_write) as writer:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        first = pool.submit(service.update, settings, expected_revision=before[1]["revision"])
                        try:
                            self.assertTrue(writing.wait(timeout=2), "first save did not reach the writer")
                            second = pool.submit(competing_save)
                            self.assertTrue(competing.wait(timeout=2), "competing save did not start")
                        finally:
                            release.set()
                        first.result(timeout=2)
                        with self.assertRaises(conflict):
                            second.result(timeout=2)
                    self.assertEqual(writer.call_count, 1)
                self.assertEqual(read()[0], settings)
                self.assertEqual(yaml.safe_load(self.path.read_text())[section], settings.to_dict())
