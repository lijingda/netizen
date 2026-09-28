from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from netizen.deployment.activation_recovery import (
    create_recovery, load_recovery, mark_candidate_admission,
)
from netizen.deployment.installer_support import InstallError
from scripts.netizen_installer import (
    _read_activation_intent, prepare_directories, resolve_layout,
)


class ActivationRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.layout = resolve_layout(
            account_home=self.directory, uid=os.geteuid(), username="current-user",
            platform_name="linux", environ={},
        )
        prepare_directories(self.layout)
        self.payload = {
            "phase": "prepared", "release": "a" * 64, "old_current": "b" * 64,
            "old_previous": None, "old_loaded": True, "old_enabled": True,
            "should_start": True, "should_enable": True, "source_version": 14,
            "target_version": 15,
            "definition": {"existed": True, "content": b"[Unit]\n".hex(), "mode": 0o600},
            "database_files": None,
        }

    def snapshot(self):
        recovery = create_recovery(self.layout, self.payload)
        database = recovery.root / "database"
        database.mkdir(mode=0o700)
        for name, content in (("channel.sqlite3", b"old-database"),
                              ("channel.sqlite3-wal", b"old-journal")):
            path = database / name
            path.write_bytes(content)
            path.chmod(0o640)
        recovery.seal_database(("channel.sqlite3", "channel.sqlite3-wal"))
        return recovery

    def write_intent(self, recovery, *, version=2):
        payload = recovery.payload
        intent = {
            "version": version, "release": payload["release"],
            "priorRelease": payload["old_current"],
            "shouldStart": payload["should_start"], "shouldEnable": payload["should_enable"],
        }
        if version == 2:
            intent["recovery"] = recovery.id
        path = self.layout.state_dir / ".activation-intent.json"
        path.write_text(json.dumps(intent))
        path.chmod(0o600)
        return path

    def candidate(self, name=None):
        path = self.layout.releases / (name or self.payload["release"])
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def test_durable_create_save_reload_and_private_modes(self) -> None:
        recovery = create_recovery(self.layout, self.payload)
        self.assertRegex(recovery.id, r"^[0-9a-f]{32}$")
        self.assertEqual(stat.S_IMODE(recovery.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((recovery.root / "manifest.json").stat().st_mode), 0o600)
        recovery.save(phase="publishing")
        loaded = load_recovery(self.layout, recovery.id)
        self.assertEqual(loaded.payload, self.payload | {"phase": "publishing"})
        detached = loaded.payload
        detached["definition"]["content"] = ""
        self.assertEqual(loaded.payload["definition"], self.payload["definition"])
        self.assertEqual(set(recovery.root.iterdir()), {recovery.root / "manifest.json"})

    def test_sealed_snapshot_preserves_hashes_modes_and_reloads(self) -> None:
        recovery = self.snapshot()
        self.assertEqual(set(recovery.payload["database_files"]),
                         {"channel.sqlite3", "channel.sqlite3-wal"})
        self.assertEqual(recovery.payload["database_files"]["channel.sqlite3"]["mode"], 0o640)
        load_recovery(self.layout, recovery.id).verify_database()
        with self.assertRaisesRegex(InstallError, "immutable"):
            recovery.save(database_files={})
        with self.assertRaisesRegex(InstallError, "already sealed"):
            recovery.seal_database(("channel.sqlite3", "channel.sqlite3-wal"))

    def test_empty_database_differs_from_incomplete_snapshot(self) -> None:
        recovery = create_recovery(self.layout, self.payload)
        with self.assertRaisesRegex(InstallError, "incomplete"):
            recovery.verify_database()
        recovery.seal_database(())
        loaded = load_recovery(self.layout, recovery.id)
        self.assertEqual(loaded.payload["database_files"], {})
        loaded.verify_database()

    def test_missing_changed_mode_changed_content_and_extra_files_fail_closed(self) -> None:
        for mutation in ("missing", "mode", "content", "extra", "symlink", "hardlink"):
            with self.subTest(mutation=mutation):
                recovery = self.snapshot()
                path = recovery.root / "database/channel.sqlite3"
                if mutation == "missing":
                    path.unlink()
                elif mutation == "mode":
                    path.chmod(0o600)
                elif mutation == "content":
                    path.write_bytes(b"modified")
                elif mutation == "extra":
                    (path.parent / "channel.sqlite3-journal").write_bytes(b"extra")
                else:
                    destination = self.directory / ("external-" + mutation)
                    path.rename(destination)
                    if mutation == "symlink":
                        path.symlink_to(destination)
                    else:
                        os.link(destination, path)
                with self.assertRaises(InstallError):
                    load_recovery(self.layout, recovery.id).verify_database()

    def test_partial_snapshot_is_not_sealed(self) -> None:
        recovery = create_recovery(self.layout, self.payload)
        (recovery.root / "database").mkdir(mode=0o700)
        with self.assertRaises(InstallError):
            recovery.seal_database(("channel.sqlite3",))
        self.assertIsNone(load_recovery(self.layout, recovery.id).payload["database_files"])

    def test_atomic_manifest_failure_keeps_previous_record(self) -> None:
        recovery = create_recovery(self.layout, self.payload)
        with patch("netizen.deployment.activation_recovery.os.replace", side_effect=OSError("failed")):
            with self.assertRaises(InstallError):
                recovery.save(phase="starting")
        self.assertEqual(load_recovery(self.layout, recovery.id).payload, self.payload)
        self.assertEqual(recovery.payload, self.payload)
        self.assertEqual(set(path.name for path in recovery.root.iterdir()), {"manifest.json"})

    def test_rejects_path_attacks_and_invalid_manifest_values(self) -> None:
        recovery = create_recovery(self.layout, self.payload)
        for identity in ("../elsewhere", "/tmp/elsewhere", "a" * 31, "A" * 32, True):
            with self.subTest(identity=identity), self.assertRaises(InstallError):
                load_recovery(self.layout, identity)
        changes = (
            {"phase": "unknown"}, {"release": "../release"}, {"old_current": "/release"},
            {"old_loaded": 1}, {"source_version": True}, {"target_version": 0},
            {"unexpected": "field"}, {"database_files": {"../channel.sqlite3": {}}},
            {"definition": {"existed": True, "content": "not-hex", "mode": 0o600}},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(InstallError):
                recovery.save(**change)
        path = recovery.root / "manifest.json"
        value = json.loads(path.read_text())
        for change in ({"version": True}, {"version": 2}, {"id": "f" * 32}):
            path.write_text(json.dumps(value | change))
            with self.assertRaises(InstallError):
                load_recovery(self.layout, recovery.id)
        path.write_text('{"version":1,"version":1}')
        with self.assertRaises(InstallError):
            load_recovery(self.layout, recovery.id)

    def test_rejects_wrong_owner_and_symlinked_recovery_components(self) -> None:
        recovery = self.snapshot()
        with self.assertRaises(InstallError):
            load_recovery(replace(self.layout, uid=self.layout.uid + 1), recovery.id)
        for target in (recovery.root / "manifest.json", recovery.root / "database", recovery.root):
            destination = target.with_name(target.name + "-real")
            target.rename(destination)
            target.symlink_to(destination, target_is_directory=destination.is_dir())
            with self.subTest(target=target), self.assertRaises(InstallError):
                if target.name == "database":
                    recovery.verify_database()
                else:
                    load_recovery(self.layout, recovery.id)
            target.unlink()
            destination.rename(target)

    def test_admission_is_persistent_idempotent_and_private(self) -> None:
        recovery = self.snapshot()
        self.assertFalse(recovery.admission_observed())
        recovery.mark_admission()
        recovery.mark_admission()
        self.assertTrue(load_recovery(self.layout, recovery.id).admission_observed())
        self.assertEqual(stat.S_IMODE((recovery.root / "admission").stat().st_mode), 0o600)

    def test_admission_rejects_broken_evidence(self) -> None:
        for mutation in ("content", "mode", "symlink"):
            with self.subTest(mutation=mutation):
                recovery = self.snapshot()
                recovery.mark_admission()
                path = recovery.root / "admission"
                if mutation == "content":
                    path.write_bytes(b"incorrect")
                elif mutation == "mode":
                    path.chmod(0o644)
                else:
                    path.unlink()
                    path.symlink_to(self.directory / "missing")
                with self.assertRaises(InstallError):
                    recovery.admission_observed()
                with self.assertRaises(InstallError):
                    recovery.mark_admission()

    def test_admission_publication_failure_never_leaves_a_partial_marker(self) -> None:
        recovery = self.snapshot()
        with patch("netizen.deployment.activation_recovery.os.replace", side_effect=OSError("failed")):
            with self.assertRaises(InstallError):
                recovery.mark_admission()
        self.assertFalse(recovery.admission_observed())
        self.assertFalse(any(path.name.startswith(".admission.") for path in recovery.root.iterdir()))

    def test_sigkill_after_admission_publication_remains_recoverable(self) -> None:
        recovery = self.snapshot()
        child = subprocess.run(
            [sys.executable, "-c", """
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
from netizen.deployment import activation_recovery as recovery_module

layout = SimpleNamespace(state_dir=Path(sys.argv[1]), uid=os.geteuid())
recovery = recovery_module.load_recovery(layout, sys.argv[2])
replace = os.replace

def publish_then_die(*args, **kwargs):
    replace(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)

recovery_module.os.replace = publish_then_die
recovery.mark_admission()
""", str(self.layout.state_dir), recovery.id],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
        recovered = load_recovery(self.layout, recovery.id)
        self.assertTrue(recovered.admission_observed())
        self.assertEqual((recovered.root / "admission").stat().st_nlink, 1)
        recovered.mark_admission()
        recovered.verify_database()
        recovered.remove()
        self.assertFalse(recovery.root.exists())

    def test_seal_sync_failure_does_not_publish_snapshot_evidence(self) -> None:
        recovery = create_recovery(self.layout, self.payload)
        database = recovery.root / "database"
        database.mkdir(mode=0o700)
        (database / "channel.sqlite3").write_bytes(b"snapshot")
        with patch("netizen.deployment.activation_recovery.os.fsync", side_effect=OSError("failed")):
            with self.assertRaises(InstallError):
                recovery.seal_database(("channel.sqlite3",))
        self.assertIsNone(load_recovery(self.layout, recovery.id).payload["database_files"])

    def test_runtime_marks_only_candidate_admission(self) -> None:
        recovery = self.snapshot()
        recovery.save(phase="starting")
        self.write_intent(recovery)
        mark_candidate_admission(self.layout.ready_file, self.candidate())
        self.assertTrue(recovery.admission_observed())

    def test_runtime_legacy_and_absent_intent_do_not_mark(self) -> None:
        recovery = self.snapshot()
        candidate = self.candidate()
        mark_candidate_admission(self.layout.ready_file, candidate)
        self.write_intent(recovery, version=1)
        mark_candidate_admission(self.layout.ready_file, candidate)
        self.assertFalse(recovery.admission_observed())

    def test_installer_and_runtime_reject_the_same_malformed_intents(self) -> None:
        recovery = self.snapshot()
        recovery.save(phase="starting")
        path = self.write_intent(recovery)
        candidate = self.candidate()
        valid = path.read_text()
        value = json.loads(valid)
        malformed = [
            "{", "[]", valid.replace('"version": 2', '"version": 2, "version": 2'),
            *(json.dumps(value | change) for change in (
                {"version": True}, {"version": 3}, {"release": "../release"},
                {"priorRelease": 1}, {"shouldStart": 1}, {"shouldEnable": "true"},
                {"recovery": "../outside"}, {"unknown": "field"},
            )),
        ]
        for raw in malformed:
            path.write_text(raw)
            with self.subTest(raw=raw):
                with self.assertRaises(InstallError):
                    _read_activation_intent(self.layout)
                with self.assertRaises(InstallError):
                    mark_candidate_admission(self.layout.ready_file, candidate)
                self.assertFalse(recovery.admission_observed())

    def test_runtime_rejects_intent_that_disagrees_with_recovery(self) -> None:
        recovery = self.snapshot()
        recovery.save(phase="starting")
        path = self.write_intent(recovery)
        value = json.loads(path.read_text())
        path.write_text(json.dumps(value | {"shouldEnable": False}))
        with self.assertRaisesRegex(InstallError, "disagree"):
            mark_candidate_admission(self.layout.ready_file, self.candidate())
        self.assertFalse(recovery.admission_observed())

    def test_runtime_restored_old_release_does_not_mark_admission(self) -> None:
        recovery = self.snapshot()
        recovery.save(phase="restoring_service")
        self.write_intent(recovery)
        mark_candidate_admission(self.layout.ready_file, self.candidate(self.payload["old_current"]))
        self.assertFalse(recovery.admission_observed())

    def test_runtime_restored_same_release_does_not_mark_admission(self) -> None:
        self.payload["old_current"] = self.payload["release"]
        recovery = self.snapshot()
        recovery.save(phase="restoring_service")
        self.write_intent(recovery)
        mark_candidate_admission(self.layout.ready_file, self.candidate())
        self.assertFalse(recovery.admission_observed())

    def test_runtime_rejects_wrong_phase_release_identity_and_paths(self) -> None:
        recovery = self.snapshot()
        intent = self.write_intent(recovery)
        candidate = self.candidate()
        with self.assertRaises(InstallError):
            mark_candidate_admission(self.layout.ready_file, candidate)
        recovery.save(phase="starting")
        with self.assertRaises(InstallError):
            mark_candidate_admission(self.layout.ready_file, self.candidate(self.payload["old_current"]))
        outside = self.directory / candidate.name
        outside.mkdir()
        with self.assertRaises(InstallError):
            mark_candidate_admission(self.layout.ready_file, outside)
        candidate.rmdir()
        candidate.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(InstallError):
            mark_candidate_admission(self.layout.ready_file, candidate)
        data = json.loads(intent.read_text())
        data["recovery"] = "../outside"
        intent.write_text(json.dumps(data))
        with self.assertRaises(InstallError):
            mark_candidate_admission(self.layout.ready_file, outside)
        self.assertFalse(recovery.admission_observed())

    def test_remove_is_scoped_to_owned_recovery_and_rejects_unexpected_entries(self) -> None:
        recovery = self.snapshot()
        recovery.mark_admission()
        sibling = self.layout.state_dir / "unrelated"
        sibling.write_bytes(b"keep")
        external = self.directory / "outside"
        external.write_bytes(b"keep")
        injected = recovery.root / "outside"
        injected.symlink_to(external)
        with self.assertRaises(InstallError):
            recovery.remove()
        self.assertTrue((recovery.root / "manifest.json").exists())
        injected.unlink()
        recovery.remove()
        self.assertFalse(recovery.root.exists())
        self.assertEqual(sibling.read_bytes(), b"keep")
        self.assertEqual(external.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
