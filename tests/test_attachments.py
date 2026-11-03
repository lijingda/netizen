from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from netizen_cli.attachments import AttachmentInput, AttachmentStore
from netizen_cli.project_files import FileBrowseError


class AttachmentStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "attachments"
        self.store = AttachmentStore(self.root)

    def batch(self, name="hello.txt", data=b"hello"):
        return self.store.prepare([AttachmentInput(name, data)])

    def publish(self, name="hello.txt", data=b"hello"):
        batch = self.batch(name, data)
        self.store.publish(batch)
        return batch

    def entry(self, batch):
        identity = self.store.root_info()["identity"]
        return identity, self.store.directory(identity=identity, path=batch.batch_id)["entries"][0]

    def test_partial_directory_creation_does_not_require_rollback_before_retry(self):
        mkdir, fsync = os.mkdir, os.fsync
        for fault in ("fsync1", ".staging", "retained", "fsync2", "fsync3"):
            for restart in (False, True):
                with self.subTest(fault=fault, restart=restart):
                    root = self.root.with_name(f"attachments-{fault}-{restart}")
                    store = AttachmentStore(root)
                    syncs = 0

                    def fail_mkdir(name, *args, **kwargs):
                        if name == fault:
                            raise OSError(errno.ENOSPC, "disk full")
                        return mkdir(name, *args, **kwargs)

                    def fail_fsync(fd):
                        nonlocal syncs
                        syncs += 1
                        if fault == f"fsync{syncs}":
                            raise OSError(errno.ENOSPC, "disk full")
                        return fsync(fd)

                    with patch("netizen_cli.attachments.os.mkdir", side_effect=fail_mkdir), patch(
                        "netizen_cli.attachments.os.fsync", side_effect=fail_fsync,
                    ):
                        with self.assertRaises((FileBrowseError, OSError)):
                            first = store.prepare([AttachmentInput("first.txt", b"first")])
                            store.publish(first)
                    self.assertTrue(root.is_dir(), "partial directories need no rollback")
                    if restart:
                        store = AttachmentStore(root)
                    batch = store.prepare([AttachmentInput("retry.txt", b"accepted")])
                    store.publish(batch)
                    self.assertEqual(batch.files[0].path.read_bytes(), b"accepted")

    def test_existing_private_directories_and_unknown_material_do_not_block_input(self):
        self.root.mkdir(mode=0o700)
        (self.root / "foreign").write_bytes(b"keep")
        marker = self.root / ".netizen-attachments.json"
        marker.write_bytes(b"incomplete old marker")
        self.publish("new.txt", b"new")
        self.assertEqual((self.root / "foreign").read_bytes(), b"keep")
        self.assertEqual(marker.read_bytes(), b"incomplete old marker")
        self.assertEqual(len(self.store.directory(identity=self.store.root_info()["identity"], path="")["entries"]), 1)

    def test_unsafe_directory_blocks_only_operations_that_use_it(self):
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        with self.assertRaises(FileBrowseError):
            self.publish()
        self.root.chmod(0o700)
        old = self.publish()
        identity, entry = self.entry(old)
        staging = self.root / ".staging"
        staging.rmdir()
        foreign = self.root.parent / "foreign"
        foreign.mkdir()
        (foreign / "keep").write_bytes(b"keep")
        staging.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(FileBrowseError):
            self.batch()
        self.assertTrue(self.store.delete(identity=identity, path=entry["path"], version=entry["version"])["deleted"])
        self.assertEqual((foreign / "keep").read_bytes(), b"keep")
        staging.unlink()
        self.assertEqual(self.publish("next.txt", b"next").files[0].path.read_bytes(), b"next")

    def test_replaced_root_invalidates_old_admin_identity_but_new_operations_work(self):
        old = self.publish()
        identity, entry = self.entry(old)
        moved = self.root.with_name("moved")
        self.root.rename(moved)
        new = self.publish("new.txt", b"new")
        with self.assertRaises(FileBrowseError) as caught:
            self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
        self.assertEqual(caught.exception.code, "root_changed")
        self.assertEqual(new.files[0].path.read_bytes(), b"new")
        self.assertEqual((moved / "retained" / old.batch_id / old.files[0].path.name).read_bytes(), b"hello")

    def test_complete_publication_stable_path_and_only_owned_files_visible(self):
        batch = self.batch()
        identity = self.store.root_info()["identity"]
        self.assertFalse(batch.files[0].path.exists())
        self.assertEqual(self.store.directory(identity=identity, path="")["entries"], [])
        self.store.publish(batch)
        self.assertEqual(batch.files[0].path.read_bytes(), b"hello")
        (batch.files[0].path.parent / "foreign.txt").write_text("private")
        tree = self.store.directory(identity=identity, path="")["entries"]
        self.assertEqual(len(tree), 1)
        self.assertIn(batch.batch_id[:8], tree[0]["displayName"])
        entries = self.store.directory(identity=identity, path=batch.batch_id)["entries"]
        self.assertEqual([entry["name"] for entry in entries], ["01-hello.txt"])
        self.assertEqual(entries[0]["size"], 5)
        self.assertEqual(self.store.content(identity=identity, path=entries[0]["path"]).text, "hello")
        for path in (batch.batch_id + "/foreign.txt", batch.batch_id + "/.manifest.json", "../secret"):
            with self.assertRaises(FileBrowseError):
                self.store.content(identity=identity, path=path)

    def test_published_delete_needs_no_execution_receipt_and_removes_empty_batch(self):
        batch = self.publish()
        identity, entry = self.entry(batch)
        result = self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
        self.assertTrue(result["removedBatch"])
        self.assertFalse(batch.files[0].path.parent.exists())
        self.assertEqual(self.store.directory(identity=identity, path="")["entries"], [])
        self.publish(data=b"12345")

    def test_saved_original_size_does_not_limit_new_input(self):
        old = self.publish("old.txt", b"old")
        # A sparse file crosses the former aggregate limit without allocating
        # a large test buffer or consuming a gigabyte of physical disk space.
        old_size = 1024 * 1024 * 1024 + 1
        with old.files[0].path.open("r+b") as stream:
            stream.truncate(old_size)
        self.store = AttachmentStore(self.root)
        new = self.publish("new.txt", b"new")
        self.assertEqual(new.files[0].path.read_bytes(), b"new")
        self.assertEqual(old.files[0].path.stat().st_size, old_size)

    def test_incomplete_old_batches_do_not_block_or_change_new_input(self):
        self.store.root_info()
        for area in ("retained", ".staging"):
            for index, manifest in enumerate((None, b'{"format":')):
                with self.subTest(area=area, manifest=manifest):
                    old = self.root / area / f"{index + 1:032x}"
                    old.mkdir(parents=True)
                    old.parent.chmod(0o700)
                    foreign = old / "foreign"
                    foreign.mkdir()
                    (foreign / "keep.txt").write_bytes(b"keep")
                    if manifest is not None:
                        (old / ".manifest.json").write_bytes(manifest)
                    self.store = AttachmentStore(self.root)
                    new = self.publish("new.txt", b"new")
                    self.assertEqual(new.files[0].path.read_bytes(), b"new")
                    self.assertEqual((foreign / "keep.txt").read_bytes(), b"keep")
                    if manifest is not None:
                        self.assertEqual((old / ".manifest.json").read_bytes(), manifest)
                    else:
                        self.assertFalse((old / ".manifest.json").exists())

    def test_deletion_is_available_for_unsupported_preview_and_preserves_unknown_files(self):
        batch = self.publish("archive.zip", b"PK\x00\xff")
        (batch.files[0].path.parent / "foreign").write_text("keep")
        identity, entry = self.entry(batch)
        with self.assertRaises(FileBrowseError):
            self.store.content(identity=identity, path=entry["path"])
        result = self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
        self.assertFalse(result["removedBatch"])
        self.assertTrue((batch.files[0].path.parent / "foreign").exists())
        self.assertTrue((batch.files[0].path.parent / ".manifest.json").exists())

    def test_stale_version_and_replaced_inode_reject_delete(self):
        batch = self.publish()
        identity, entry = self.entry(batch)
        batch.files[0].path.write_text("changed")
        with self.assertRaises(FileBrowseError) as caught:
            self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
        self.assertEqual(caught.exception.code, "file_changed")
        replacement = batch.files[0].path.parent / "replacement"
        replacement.write_text("other")
        os.replace(replacement, batch.files[0].path)
        with self.assertRaises(FileBrowseError):
            self.store.metadata(identity=identity, path=entry["path"])
        self.assertEqual(batch.files[0].path.read_text(), "other")

    def test_symlinks_at_root_batch_or_file_never_followed(self):
        batch = self.publish()
        identity, entry = self.entry(batch)
        secret = self.root.parent / "secret"
        secret.write_text("keep")
        batch.files[0].path.unlink()
        batch.files[0].path.symlink_to(secret)
        with self.assertRaises(FileBrowseError):
            self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
        with self.assertRaises(FileBrowseError):
            self.store.content(identity=identity, path=entry["path"])
        self.assertEqual(secret.read_text(), "keep")
        moved = self.root.parent / "moved"
        self.root.rename(moved)
        self.root.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(FileBrowseError):
            self.store.root_info()

    def test_safe_unique_filenames_and_limits(self):
        name = "../" + "中" * 200 + "\n.txt"
        batch = self.store.prepare([AttachmentInput(name, b"1"), AttachmentInput(name, b"2")])
        self.store.publish(batch)
        for item in batch.files:
            self.assertTrue(item.path.is_relative_to(self.root))
            self.assertLess(len(item.path.name.encode()), 256)
        identity = self.store.root_info()["identity"]
        self.assertEqual(len(self.store.directory(identity=identity, path=batch.batch_id)["entries"]), 2)
        with self.assertRaises(FileBrowseError):
            self.store.prepare([AttachmentInput("x", b"")] * 21)
        small = AttachmentStore(self.root, max_file_bytes=1, max_batch_bytes=1)
        with self.assertRaises(FileBrowseError):
            small.prepare([AttachmentInput("x", b"12")])
        with self.assertRaises(FileBrowseError):
            small.prepare([AttachmentInput("x", b"1"), AttachmentInput("y", b"2")])

    def test_restart_and_new_input_leave_old_staging_and_retained_untouched(self):
        retained = self.publish()
        staged = self.batch()
        unknown = self.root / ".staging" / ("f" * 32)
        unknown.mkdir()
        (unknown / "foreign").write_text("keep")
        fresh = AttachmentStore(self.root)
        new = fresh.prepare([AttachmentInput("new.txt", b"new")])
        fresh.publish(new)
        self.assertTrue(retained.files[0].path.exists())
        self.assertTrue((self.root / ".staging" / staged.batch_id).exists())
        self.assertTrue((unknown / "foreign").exists())

    def test_discard_only_exact_batch_and_foreign_batch_is_rejected(self):
        first = self.publish()
        second = self.publish()
        self.store.discard(first)
        self.assertFalse(first.files[0].path.exists())
        self.assertTrue(second.files[0].path.exists())
        self.store.discard(first)
        other = AttachmentStore(self.root.parent / "other")
        with self.assertRaises(FileBrowseError):
            other.discard(second)

    def test_partial_write_failure_cleans_own_files(self):
        original = os.fsync
        calls = 0
        self.store.root_info()
        def fail(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk failure")
            return original(fd)
        with patch("netizen_cli.attachments.os.fsync", side_effect=fail):
            with self.assertRaises((FileBrowseError, OSError)):
                self.store.prepare([AttachmentInput("a", b"1"), AttachmentInput("b", b"2")])
        self.assertEqual(list((self.root / ".staging").iterdir()), [])

    def test_delete_after_unlink_failure_reports_unknown_without_restoring(self):
        batch = self.publish()
        identity, entry = self.entry(batch)
        with patch("netizen_cli.attachments.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(FileBrowseError) as caught:
                self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertFalse(batch.files[0].path.exists())

    def test_last_file_cleanup_failures_preserve_success_and_allow_new_input(self):
        rmdir, unlink, fsync, scandir = os.rmdir, os.unlink, os.fsync, os.scandir
        for fault in ("scandir", "manifest", "rmdir", "fsync"):
            with self.subTest(fault=fault):
                batch = self.publish()
                identity, entry = self.entry(batch)
                syncs = 0

                def fail_unlink(name, **kwargs):
                    if fault == "manifest" and name == ".manifest.json":
                        raise OSError(errno.EIO, "cleanup failed")
                    return unlink(name, **kwargs)

                def fail_rmdir(*args, **kwargs):
                    if fault == "rmdir":
                        raise OSError(errno.EIO, "cleanup failed")
                    return rmdir(*args, **kwargs)

                def fail_scandir(*args, **kwargs):
                    if fault == "scandir":
                        raise OSError(errno.EIO, "cleanup failed")
                    return scandir(*args, **kwargs)

                def fail_fsync(fd):
                    nonlocal syncs
                    syncs += 1
                    if fault == "fsync" and syncs == 2:
                        raise OSError(errno.EIO, "cleanup failed")
                    return fsync(fd)

                with patch("netizen_cli.attachments.os.unlink", side_effect=fail_unlink), patch(
                    "netizen_cli.attachments.os.rmdir", side_effect=fail_rmdir,
                ), patch("netizen_cli.attachments.os.scandir", side_effect=fail_scandir), patch(
                    "netizen_cli.attachments.os.fsync", side_effect=fail_fsync,
                ):
                    result = self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
                self.assertTrue(result["deleted"])
                self.assertEqual(result["removedBatch"], fault in ("rmdir", "fsync"))
                self.assertFalse(batch.files[0].path.exists())
                if not result["removedBatch"]:
                    self.assertEqual(self.store.directory(identity=identity, path=batch.batch_id)["entries"], [])
                else:
                    self.assertNotIn(batch.batch_id, [item["name"] for item in self.store.directory(identity=identity, path="")["entries"]])
                self.publish("same-process.txt", b"next")
                self.store = AttachmentStore(self.root)
                self.publish("after-restart.txt", b"next")

    def test_failed_write_and_failed_cleanup_do_not_block_new_input(self):
        old = self.publish("old.txt", b"keep")
        with patch("netizen_cli.attachments.os.fsync", side_effect=OSError(errno.ENOSPC, "disk full")), patch(
            "netizen_cli.attachments.os.unlink", side_effect=OSError(errno.EIO, "cleanup failed"),
        ):
            with self.assertRaises((OSError, FileBrowseError)):
                self.batch("partial.txt", b"partial")
        residue = list((self.root / ".staging").iterdir())
        self.assertEqual(len(residue), 1)
        self.assertTrue((residue[0] / "01-partial.txt").exists())
        self.publish("same-process.txt", b"next")
        self.store = AttachmentStore(self.root)
        self.publish("after-restart.txt", b"next")
        self.assertEqual(old.files[0].path.read_bytes(), b"keep")
        self.assertEqual((residue[0] / "01-partial.txt").read_bytes(), b"partial")

    def test_last_batch_cleanup_never_uses_existing_staging_content(self):
        batch = self.publish()
        identity, entry = self.entry(batch)
        staged = self.root / ".staging" / batch.batch_id
        staged.mkdir()
        (staged / "foreign").write_bytes(b"keep")
        result = self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
        self.assertTrue(result["deleted"])
        self.assertTrue(result["removedBatch"])
        self.assertEqual((staged / "foreign").read_bytes(), b"keep")
        self.publish("next.txt", b"next")

    def test_batch_directory_replacement_is_not_owned(self):
        batch = self.publish()
        identity, entry = self.entry(batch)
        directory = batch.files[0].path.parent
        moved = directory.with_name("moved")
        directory.rename(moved)
        directory.mkdir()
        for child in moved.iterdir():
            child.rename(directory / child.name)
        with self.assertRaises(FileBrowseError):
            self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
        self.assertTrue(batch.files[0].path.exists())

    def test_hardlinked_file_cannot_be_deleted_as_an_original(self):
        batch = self.publish()
        identity, entry = self.entry(batch)
        os.link(batch.files[0].path, self.root.parent / "linked")
        with self.assertRaises(FileBrowseError):
            self.store.delete(identity=identity, path=entry["path"], version=entry["version"])

    def test_publish_failure_keeps_exact_batch_discardable(self):
        batch = self.batch()
        with patch("netizen_cli.attachments.os.rename", side_effect=OSError("disk failure")):
            with self.assertRaises(FileBrowseError):
                self.store.publish(batch)
        self.store.discard(batch)
        self.assertFalse(batch.files[0].path.exists())
        self.assertFalse((self.root / ".staging" / batch.batch_id).exists())

    def test_publish_rechecks_opened_batch_before_moving_and_never_replaces_a_target(self):
        prepared = self.batch()
        batch_path = self.root / ".staging" / prepared.batch_id
        original = self.store._owned_info
        def swap(fd, item):
            info = original(fd, item)
            batch_path.rename(batch_path.with_name("moved"))
            batch_path.mkdir()
            (batch_path / "foreign").write_text("keep")
            return info
        with patch.object(self.store, "_owned_info", side_effect=swap):
            with self.assertRaises(FileBrowseError):
                self.store.publish(prepared)
        self.assertTrue((batch_path / "foreign").exists())
        self.assertFalse((self.root / "retained" / prepared.batch_id).exists())

    def test_publish_never_replaces_an_existing_empty_target(self):
        prepared = self.batch()
        self.store.root_info()
        target = self.root / "retained" / prepared.batch_id
        target.mkdir()
        info = target.stat()
        with self.assertRaises(FileBrowseError):
            self.store.publish(prepared)
        self.assertEqual(target.stat().st_ino, info.st_ino)

    def test_delete_checks_batch_location_after_open_before_unlink(self):
        batch = self.publish()
        identity, entry = self.entry(batch)
        directory = batch.files[0].path.parent
        moved = directory.with_name("moved")
        original = self.store._owned_info
        calls = 0
        def swap(fd, item):
            nonlocal calls
            info = original(fd, item)
            calls += 1
            if calls == 2:
                directory.rename(moved)
                directory.mkdir()
                (directory / "foreign").write_text("keep")
            return info
        with patch.object(self.store, "_owned_info", side_effect=swap):
            with self.assertRaises(FileBrowseError):
                self.store.delete(identity=identity, path=entry["path"], version=entry["version"])
        self.assertTrue((moved / "01-hello.txt").exists())
        self.assertEqual((directory / "foreign").read_text(), "keep")

    def test_root_replacement_between_identity_check_and_descriptor_open_is_rejected(self):
        batch = self.publish()
        identity, _ = self.entry(batch)
        original = self.store._require_identity
        def swap(value):
            original(value)
            old = self.root / "old-retained"
            self.store.retained.rename(old)
            self.store.retained.mkdir(mode=0o700)
            (old / batch.batch_id).rename(self.store.retained / batch.batch_id)
        with patch.object(self.store, "_require_identity", side_effect=swap):
            with self.assertRaises(FileBrowseError) as caught:
                self.store.directory(identity=identity, path=batch.batch_id)
        self.assertEqual(caught.exception.code, "root_changed")

    def test_new_store_preserves_partial_manifest_and_foreign_contents(self):
        self.store.root_info()
        partial = self.root / ".staging" / ("e" * 32)
        partial.mkdir(parents=True, mode=0o700)
        partial.parent.chmod(0o700)
        (partial / ".manifest.json").write_bytes(b'{"format": 1,')
        (partial / "01-file.txt").write_text("incomplete")
        self.store = AttachmentStore(self.root)
        self.assertEqual((partial / "01-file.txt").read_text(), "incomplete")
        self.assertEqual((partial / ".manifest.json").read_bytes(), b'{"format": 1,')
        # Retaining an incomplete private write must not disable future input.
        self.publish("next.txt", b"next")


class AttachmentCancellationTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_prepare_waits_for_worker_and_discards_exact_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AttachmentStore(Path(temp).resolve() / "attachments")
            entered = threading.Event()
            release = threading.Event()
            original = store.prepare
            def slow(files):
                result = original(files)
                entered.set()
                release.wait(5)
                return result
            with patch.object(store, "prepare", side_effect=slow):
                task = asyncio.create_task(store.prepare_files([AttachmentInput("x", b"x")]))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(list((store.root / ".staging").iterdir()), [])

    async def test_repeated_cancel_during_publish_waits_then_cleans_published_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AttachmentStore(Path(temp).resolve() / "attachments")
            batch = store.prepare([AttachmentInput("x", b"x")])
            entered = threading.Event()
            release = threading.Event()
            original = store.publish
            def slow(prepared):
                original(prepared)
                entered.set()
                release.wait(5)
            with patch.object(store, "publish", side_effect=slow):
                task = asyncio.create_task(store.publish_files(batch))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(batch.files[0].path.parent.exists())
