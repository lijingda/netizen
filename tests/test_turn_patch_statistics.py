from __future__ import annotations

import difflib
import tempfile
import unittest
from pathlib import Path

from openai_codex.types import ThreadItem

from netizen.turn_files import extract_turn_files, turn_patch_summary
from netizen.turn_patch_children import TaskPatchChildren, TurnPatchBatch


def patch_item(
    item_id: str, path: str, diff: str, *, kind: str = "update",
    move_path: str | None = None, status: str = "completed",
) -> ThreadItem:
    change_kind = {"type": kind}
    if move_path is not None:
        change_kind["move_path"] = move_path
    return ThreadItem.model_validate({
        "type": "fileChange", "id": item_id, "status": status,
        "changes": [{"path": path, "diff": diff, "kind": change_kind}],
    })


def update(before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="a/file", tofile="b/file",
    ))


class TurnPatchStatisticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_then_update_accumulates_instead_of_reconstructing_net_diff(self) -> None:
        before = "".join(f"line {i}\n" for i in range(337))
        after = "".join(f"line {i}\n" for i in range(310)) + "new\n" * 32
        summary = turn_patch_summary((
            patch_item("add", "file", before, kind="add"),
            patch_item("edit", "file", update(before, after)),
        ), self.root)
        self.assertEqual((summary.additions, summary.deletions), (369, 27))
        self.assertEqual((summary.files[0].additions, summary.files[0].deletions), (369, 27))

    def test_edit_then_revert_still_counts_both_successful_operations(self) -> None:
        summary = turn_patch_summary((
            patch_item("edit", "file", update("a\n", "b\n")),
            patch_item("revert", "file", update("b\n", "a\n")),
        ), self.root)
        self.assertEqual((summary.additions, summary.deletions), (2, 2))

    def test_raw_add_delete_count_lf_and_support_empty_files(self) -> None:
        for content, lines in (("", 0), ("x", 1), ("x\n", 1), ("\n", 1), ("x\ny", 2), ("x\u2028y\n", 1)):
            with self.subTest(content=content):
                summary = turn_patch_summary((
                    patch_item("add", "new", content, kind="add"),
                    patch_item("del", "old", content, kind="delete"),
                ), self.root)
                self.assertEqual((summary.additions, summary.deletions), (lines, lines))

    def test_valid_hunks_headers_no_newline_marker_and_header_like_content(self) -> None:
        hunk = "@@ -1 +1 @@\n---old\n+++new\n\\ No newline at end of file\n"
        for body in (hunk, "--- a/file\n+++ b/file\n" + hunk, "diff --git a/file b/file\n--- a/file\n+++ b/file\n" + hunk):
            with self.subTest(body=body):
                summary = turn_patch_summary((patch_item("patch", "file", body),), self.root)
                self.assertEqual((summary.additions, summary.deletions), (1, 1))

    def test_sdk_move_suffix_is_metadata_and_destination_is_used(self) -> None:
        target = str(self.root / "renamed file")
        for body, expected in ((update("old\n", "new\n"), (1, 1)), ("", (0, 0))):
            summary = turn_patch_summary((patch_item(
                "move", "old", body + f"\n\nMoved to: {target}", move_path=target,
            ),), self.root)
            self.assertEqual((summary.additions, summary.deletions), expected)
            self.assertEqual(summary.files[0].path, target)

    def test_update_counts_lf_without_treating_unicode_separators_as_lines(self) -> None:
        for newline in ("\n", "\r\n"):
            body = newline.join(("@@ -1 +1 @@", "-old", "+new\u2028text", ""))
            summary = turn_patch_summary((patch_item("p", "file", body),), self.root)
            self.assertEqual((summary.additions, summary.deletions), (1, 1))

    def test_duplicate_completed_items_and_lifecycle_views_are_not_added_twice(self) -> None:
        success = patch_item("same", "file", "a\n", kind="add")
        summary = turn_patch_summary((
            patch_item("same", "file", "a\n", kind="add", status="inProgress"),
            success, success,
            patch_item("failed", "file", "bad\n", kind="add", status="failed"),
            patch_item("declined", "file", "bad\n", kind="add", status="declined"),
        ), self.root)
        self.assertEqual((summary.additions, summary.deletions), (1, 0))

    def test_child_item_ids_are_namespaced_and_paths_merge_across_projects(self) -> None:
        outside = self.root / "other-project"
        outside.mkdir()
        destination = outside / "file"
        destination.write_text("child\nparent\n")
        child = TurnPatchBatch("child", "turn", outside, (
            patch_item("same", "file", "child\n", kind="add"),
        ))
        summary = turn_patch_summary((
            patch_item("same", "../other-project/file", "@@ -1 +1,2 @@\n child\n+parent\n"),
        ), self.root / "root-project", children=TaskPatchChildren((child, child)))
        self.assertEqual((summary.additions, summary.deletions), (2, 0))
        self.assertEqual(len(summary.files), 1)
        self.assertEqual(summary.files[0].path, str(destination))
        files = extract_turn_files((), self.root, diff_summary=summary)
        self.assertEqual((files[0].additions, files[0].deletions), (2, 0))

    def test_conflicting_completed_identity_keeps_only_unambiguous_numbers(self) -> None:
        summary = turn_patch_summary((
            patch_item("conflict", "bad", "one\n", kind="add"),
            patch_item("conflict", "bad", "one\ntwo\n", kind="add"),
            patch_item("known", "good", "known\n", kind="add"),
        ), self.root)
        self.assertIsNone(summary.additions)
        self.assertIsNone(summary.files[0].additions)
        self.assertEqual(summary.files[1].additions, 1)

    def test_missing_child_omits_total_but_preserves_known_file_statistics(self) -> None:
        summary = turn_patch_summary((
            patch_item("own", "file", "known\n", kind="add"),
        ), self.root, children=TaskPatchChildren(complete=False))
        self.assertIsNone(summary.additions)
        self.assertIsNone(summary.deletions)
        self.assertEqual((summary.files[0].additions, summary.files[0].deletions), (1, 0))

    def test_invalid_patch_only_invalidates_affected_path_and_total(self) -> None:
        summary = turn_patch_summary((
            patch_item("good", "good", "known\n", kind="add"),
            patch_item("prior", "bad", "known\n", kind="add"),
            patch_item("bad", "bad", "@@ -1 +1,2 @@\n-old\n+truncated\n"),
        ), self.root)
        self.assertIsNone(summary.additions)
        self.assertEqual(summary.files[0].additions, 1)
        self.assertIsNone(summary.files[1].additions)

    def test_binary_and_unknown_update_do_not_fabricate_zero(self) -> None:
        for body in ("", "Binary files a/file and b/file differ\n", "GIT binary patch\n", "x\0y"):
            with self.subTest(body=body):
                summary = turn_patch_summary((patch_item("patch", "file", body),), self.root)
                self.assertIsNone(summary.additions)
                self.assertIsNone(summary.files[0].additions)

    def test_aggregate_only_supplements_paths_never_duplicates_patch_counts(self) -> None:
        aggregate = "diff --git a/file b/file\n" + update("a\n", "b\n")
        summary = turn_patch_summary((
            patch_item("own", "file", "cumulative\n" * 5, kind="add"),
        ), self.root, turn_diff=aggregate)
        self.assertEqual((summary.additions, summary.deletions), (5, 0))
        summary = turn_patch_summary((), self.root, turn_diff=aggregate)
        self.assertIsNone(summary.additions)
        self.assertEqual(summary.files[0].path, str(self.root / "file"))

    def test_images_keep_send_capability_without_per_file_numbers(self) -> None:
        image = self.root / "image.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        summary = turn_patch_summary((patch_item("p", image.name, "\0", kind="add"),), self.root)
        files = extract_turn_files((), self.root, diff_summary=summary)
        self.assertEqual(files[0].media_kind, "image")
        self.assertIsNone(files[0].additions)

    def test_aggregate_deleted_path_without_a_patch_also_marks_total_unknown(self) -> None:
        aggregate = (
            "diff --git a/gone b/gone\n"
            "deleted file mode 100644\n--- a/gone\n+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-old\n"
        )
        own = patch_item("own", "known", "new\n", kind="add")
        summary = turn_patch_summary((own,), self.root, turn_diff=aggregate)
        self.assertIsNone(summary.additions)
        self.assertEqual([(Path(f.path).name, f.additions) for f in summary.files], [("known", 1)])
        summary = turn_patch_summary((
            own, patch_item("delete", "gone", "old\n", kind="delete"),
        ), self.root, turn_diff=aggregate)
        self.assertEqual((summary.additions, summary.deletions), (1, 1))

    def test_child_patch_cannot_cover_a_missing_root_patch_to_the_same_path(self) -> None:
        child = TurnPatchBatch("child", "turn", self.root, (
            patch_item("patch", "file", "child\n", kind="add"),
        ))
        aggregate = "diff --git a/file b/file\n" + update("child\n", "parent\n")
        summary = turn_patch_summary((), self.root, children=TaskPatchChildren((child,)), turn_diff=aggregate)
        self.assertIsNone(summary.additions)
        self.assertIsNone(summary.files[0].additions)


if __name__ == "__main__":
    unittest.main()
