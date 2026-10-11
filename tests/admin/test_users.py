from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from netizen_cli.admin.users import (
    AdminIdentity, AdminPermissionDenied, AdminUsers, AdminUsersConflict,
    AdminUsersUnavailable, LastAdministrator, PrivateJSONError,
    read_private_json, write_private_json,
)


class AdminUsersTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.users = AdminUsers(self.root, "cli_test")
        self.alice = AdminIdentity("cli_test", "ou_alice")
        self.bob = AdminIdentity("cli_test", "ou_bob")

    def initialize(self):
        return self.users.add_initial(self.alice.open_id, name="Alice")

    def test_missing_and_valid_empty_are_distinct_from_invalid(self) -> None:
        self.assertFalse(self.users.has_admins)
        self.assertEqual(self.users.list(), ())
        self.assertEqual(self.users.revision, 0)
        write_private_json(self.users.path, {"version": 1, "revision": 3, "members": []})
        self.assertFalse(AdminUsers(self.root, "cli_test").has_admins)
        self.users.path.write_text("{invalid}")
        with self.assertRaises(AdminUsersUnavailable):
            AdminUsers(self.root, "cli_test")

    def test_persistence_private_mode_and_profile_does_not_change_revision(self) -> None:
        alice = self.initialize()
        self.assertEqual(stat.S_IMODE(self.users.path.stat().st_mode), 0o600)
        before = self.users.revision
        updated = self.users.update_profile(self.alice, name="New name", avatar_url="https://example.test/avatar",
                                            expected_add_revision=alice.add_revision)
        self.assertEqual(self.users.revision, before)
        self.assertEqual(updated.add_revision, alice.add_revision)
        self.assertEqual(updated.profile_source, "user_info")
        reloaded = AdminUsers(self.root, "cli_test")
        self.assertEqual(reloaded.list(), (updated,))
        self.assertEqual(reloaded.revision, before)
        self.assertEqual(list(self.users.path.parent.glob(".admin-users.json.*")), [])

    def test_app_switch_preserves_old_members_but_requires_new_initial_binding(self) -> None:
        self.initialize()
        switched = AdminUsers(self.root, "cli_new")
        self.assertFalse(switched.has_admins)
        self.assertEqual(switched.list(), ())
        self.assertIsNone(switched.get_membership(self.alice))
        new = switched.add_initial("ou_alice")
        self.assertEqual(new.identity, AdminIdentity("cli_new", "ou_alice"))
        self.assertNotEqual(new.identity, self.alice)
        self.assertEqual(len(AdminUsers(self.root, "cli_test").list()), 1)
        with self.assertRaises(LastAdministrator):
            switched.remove(new.identity, actor=new.identity, expected_revision=switched.revision)

    def test_initial_setup_never_replaces_existing_administrators(self) -> None:
        self.initialize()
        with self.assertRaises(AdminUsersConflict):
            self.users.add_initial("ou_bob")
        self.assertEqual(self.users.list()[0].identity, self.alice)

    def test_mutations_require_current_actor_and_exact_revision(self) -> None:
        self.initialize()
        for actor in (self.bob, AdminIdentity("cli_other", "ou_alice")):
            with self.assertRaises(AdminPermissionDenied):
                self.users.add(self.bob, actor=actor, expected_revision=self.users.revision)
        with self.assertRaises(AdminUsersConflict):
            self.users.add(self.bob, actor=self.alice, expected_revision=0)
        bob = self.users.add(self.bob, actor=self.alice, expected_revision=1)
        self.assertEqual(bob.add_revision, 2)
        with self.assertRaises(AdminUsersConflict):
            self.users.remove(self.bob, actor=self.alice, expected_revision=1)
        with self.assertRaises(AdminUsersConflict):
            self.users.add(self.bob, actor=self.alice, expected_revision=2)

    def test_self_removal_and_last_administrator_protection(self) -> None:
        self.initialize()
        self.users.add(self.bob, actor=self.alice, expected_revision=1)
        revoked = []
        self.users.subscribe_revocation(revoked.append)
        self.users.remove(self.alice, actor=self.alice, expected_revision=2)
        self.assertEqual(revoked, [self.alice])
        with self.assertRaises(LastAdministrator):
            self.users.remove(self.bob, actor=self.bob, expected_revision=3)
        self.assertTrue(self.users.has_admins)
        self.assertEqual(self.users.revision, 3)

    def test_cleanup_failure_does_not_skip_session_revocation_or_restore_membership(self) -> None:
        from netizen_cli.admin.auth import AdminAuth, SessionRejected

        initial = self.initialize()
        self.users.add(self.bob, actor=self.alice, expected_revision=1)

        def failing_cleanup(_identity):
            raise PrivateJSONError()

        self.users.subscribe_revocation(failing_cleanup)
        auth = AdminAuth(self.users)
        session = auth.issue_session(self.alice, source_ip="192.0.2.1",
                                     expected_add_revision=initial.add_revision)
        with self.assertRaises(AdminUsersUnavailable):
            self.users.remove(self.alice, actor=self.bob, expected_revision=2)
        self.assertIsNone(self.users.get_membership(self.alice))
        self.assertEqual(auth.state_counts().sessions, 0)
        with self.assertRaises(SessionRejected):
            auth.authenticate(session.token)
        self.assertEqual(AdminUsers(self.root, "cli_test").list()[0].identity, self.bob)

    def test_concurrent_deletions_cannot_remove_both_administrators(self) -> None:
        self.initialize()
        self.users.add(self.bob, actor=self.alice, expected_revision=1)
        barrier = threading.Barrier(3)

        def remove(identity):
            barrier.wait()
            try:
                self.users.remove(identity, actor=identity, expected_revision=2)
                return "removed"
            except AdminUsersConflict:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(remove, identity) for identity in (self.alice, self.bob)]
            barrier.wait()
            results = [future.result(timeout=5) for future in pending]
        self.assertCountEqual(results, ["removed", "stale"])
        self.assertEqual(len(self.users.list()), 1)

    def test_removed_user_late_profile_cannot_recreate_or_update_readded_user(self) -> None:
        initial = self.initialize()
        self.users.add(self.bob, actor=self.alice, expected_revision=1)
        self.users.remove(self.alice, actor=self.bob, expected_revision=2)
        self.assertIsNone(self.users.update_profile(self.alice, name="late", avatar_url=None,
                                                   expected_add_revision=initial.add_revision))
        replacement = self.users.add(self.alice, actor=self.bob, expected_revision=3)
        self.assertGreater(replacement.add_revision, initial.add_revision)
        self.assertIsNone(self.users.update_profile(self.alice, name="late", avatar_url=None,
                                                   expected_add_revision=initial.add_revision))
        self.assertEqual(self.users.get_membership(self.alice).name, "")

    def test_failed_save_latches_closed_and_revokes_even_when_replace_committed(self) -> None:
        for after_replace in (False, True):
            with self.subTest(after_replace=after_replace):
                root = self.root / str(after_replace)
                root.mkdir()
                users = AdminUsers(root, "cli_test")
                users.add_initial("ou_alice")
                revoked = []
                users.subscribe_revocation(revoked.append)
                real_write = write_private_json

                def fail(path, value):
                    if after_replace:
                        real_write(path, value)
                    raise PrivateJSONError()

                with patch("netizen_cli.admin.users.write_private_json", side_effect=fail):
                    with self.assertRaises(AdminUsersUnavailable):
                        users.add(self.bob, actor=self.alice, expected_revision=1)
                self.assertEqual(revoked, [self.alice])
                with self.assertRaises(AdminUsersUnavailable):
                    users.get_membership(self.alice)
                current = AdminUsers(root, "cli_test")
                self.assertEqual(len(current.list()), 2 if after_replace else 1)

    def test_membership_schema_rejects_duplicates_and_invalid_revisions(self) -> None:
        self.initialize()
        original = read_private_json(self.users.path)
        for change in ("duplicate", "bool_revision", "future_membership", "foreign_field"):
            value = json.loads(json.dumps(original))
            if change == "duplicate":
                value["members"].append(value["members"][0])
            elif change == "bool_revision":
                value["revision"] = True
            elif change == "future_membership":
                value["members"][0]["addRevision"] = 2
            else:
                value["members"][0]["tenantKey"] = "unneeded"
            with self.subTest(change=change):
                write_private_json(self.users.path, value)
                with self.assertRaises(AdminUsersUnavailable):
                    AdminUsers(self.root, "cli_test")


class PrivateJSONTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "private/data.json"
        write_private_json(self.path, {"value": "private-value"})

    def test_rejects_symlink_directory_fifo_hardlink_permissions_and_foreign_owner(self) -> None:
        linked = self.path.parent / "linked.json"
        linked.symlink_to(self.path)
        fifo = self.path.parent / "pipe"
        os.mkfifo(fifo, 0o600)
        for target in (linked, fifo, self.path.parent):
            with self.subTest(target=target.name), self.assertRaises(PrivateJSONError):
                read_private_json(target)
        with self.assertRaises(PrivateJSONError):
            write_private_json(linked, {"value": "changed"})
        self.assertEqual(read_private_json(self.path), {"value": "private-value"})
        hardlink = self.path.parent / "hardlink"
        os.link(self.path, hardlink)
        with self.assertRaises(PrivateJSONError):
            read_private_json(self.path)
        hardlink.unlink()
        self.path.chmod(0o644)
        with self.assertRaises(PrivateJSONError):
            read_private_json(self.path)
        self.path.chmod(0o600)
        with patch("netizen_cli.admin.users.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(PrivateJSONError):
                read_private_json(self.path)

    def test_parent_symlink_is_rejected(self) -> None:
        alias = self.root / "alias"
        alias.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(PrivateJSONError):
            read_private_json(alias / self.path.name)
        with self.assertRaises(PrivateJSONError):
            write_private_json(alias / self.path.name, {"changed": True})

    def test_parent_directory_must_be_private(self) -> None:
        self.path.parent.chmod(0o755)
        with self.assertRaises(PrivateJSONError):
            read_private_json(self.path)
        with self.assertRaises(PrivateJSONError):
            write_private_json(self.path, {})

    def test_duplicate_nonfinite_oversized_and_ambiguous_json_rejected(self) -> None:
        for content in ('{"a":1,"a":2}', '{"a":NaN}', '[]', '{invalid}', '{"long":"' + 'x' * 100 + '"}'):
            self.path.write_text(content)
            with self.subTest(content=content[:20]), self.assertRaises(PrivateJSONError):
                read_private_json(self.path, maximum=64)

    def test_atomic_replace_failure_preserves_prior_content_and_cleans_temporary(self) -> None:
        with patch("netizen_cli.admin.users.os.replace", side_effect=OSError("sensitive detail")):
            with self.assertRaises(PrivateJSONError) as caught:
                write_private_json(self.path, {"value": "new"})
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(read_private_json(self.path), {"value": "private-value"})
        self.assertEqual(list(self.path.parent.glob(".data.json.*")), [])

    def test_missing_is_explicit_and_relative_paths_rejected(self) -> None:
        self.assertIsNone(read_private_json(self.root / "missing.json", missing_ok=True))
        for path in (self.root / "missing.json", Path("relative.json")):
            with self.assertRaises(PrivateJSONError):
                read_private_json(path)
        with self.assertRaises(PrivateJSONError):
            write_private_json(Path("relative.json"), {})


if __name__ == "__main__":
    unittest.main()
