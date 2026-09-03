from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai_codex.types import ThreadItem

from netizen.turn_files import (
    TurnFileError,
    extract_turn_files,
    has_turn_file_references,
    inspect_turn_file_path,
    paginate_turn_files,
    require_turn_file_path,
    turn_diff_paths,
    turn_diff_summary,
)


def file_change(
    *changes: dict[str, object],
    status: str = "completed",
) -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "type": "fileChange",
            "id": "file-change",
            "status": status,
            "changes": list(changes),
        }
    )


def image_generation(path: Path, *, status: str = "completed") -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "type": "imageGeneration",
            "id": "image-generation",
            "result": "ok",
            "savedPath": str(path),
            "status": status,
        }
    )


class TurnFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home"
        self.root = self.home / "projects" / "test"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_extracts_completed_add_update_move_and_image_in_report_order(
        self,
    ) -> None:
        added = self.root / "reports" / "sales.xlsx"
        moved = self.root / "src" / "renamed.py"
        image = self.root / "output" / "trend.PNG"
        for path, body in (
            (added, b"sales"),
            (moved, b"code"),
            (image, b"\x89PNG\r\n\x1a\nimage"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

        items = (
            file_change(
                {
                    "path": "reports/sales.xlsx",
                    "diff": "",
                    "kind": {"type": "add"},
                },
                {
                    "path": "src/old.py",
                    "diff": "",
                    "kind": {"type": "update", "move_path": "src/renamed.py"},
                },
                {"path": "gone.txt", "diff": "", "kind": {"type": "delete"}},
            ),
            image_generation(image),
        )

        files = extract_turn_files(items, self.root)

        self.assertEqual(
            [item.display_path for item in files],
            ["reports/sales.xlsx", "src/renamed.py", "output/trend.PNG"],
        )
        self.assertEqual([item.size for item in files], [5, 4, 13])
        self.assertEqual(
            [item.media_kind for item in files],
            ["file", "file", "image"],
        )

    def test_only_supported_image_bytes_use_an_image_message(self) -> None:
        fake_png = self.root / "fake.png"
        jpeg = self.root / "photo.bin"
        fake_png.write_bytes(b"not a png")
        jpeg.write_bytes(b"\xff\xd8\xffjpeg")

        files = extract_turn_files(
            (
                file_change(
                    {"path": fake_png.name, "diff": "", "kind": {"type": "add"}},
                    {"path": jpeg.name, "diff": "", "kind": {"type": "add"}},
                ),
            ),
            self.root,
        )

        self.assertEqual(
            [item.media_kind for item in files],
            ["file", "image"],
        )

    def test_ignores_unfinished_items_missing_paths_and_duplicates(self) -> None:
        present = self.root / "present.txt"
        present.write_text("present", encoding="utf-8")
        items = (
            file_change(
                {"path": "present.txt", "diff": "", "kind": {"type": "update"}},
                {"path": "missing.txt", "diff": "", "kind": {"type": "add"}},
            ),
            file_change(
                {"path": "present.txt", "diff": "", "kind": {"type": "update"}},
            ),
            file_change(
                {"path": "ignored.txt", "diff": "", "kind": {"type": "add"}},
                status="inProgress",
            ),
            image_generation(self.root / "missing.png"),
        )

        files = extract_turn_files(items, self.root)

        self.assertEqual([item.display_path for item in files], ["present.txt"])
        self.assertTrue(has_turn_file_references(items))
        self.assertFalse(has_turn_file_references((object(),)))

    def test_unknown_file_change_kind_fails_closed(self) -> None:
        unknown = SimpleNamespace(
            root=SimpleNamespace(
                type="fileChange",
                status="completed",
                changes=(
                    SimpleNamespace(
                        path="future.txt",
                        kind=SimpleNamespace(
                            root=SimpleNamespace(type="futureKind")
                        ),
                    ),
                ),
            )
        )

        self.assertFalse(has_turn_file_references((unknown,)))
        self.assertEqual(extract_turn_files((unknown,), self.root), ())

    def test_never_scans_the_project_for_unreported_tool_outputs(self) -> None:
        (self.root / "shell-output.xlsx").write_bytes(b"unreported")

        self.assertEqual(extract_turn_files((), self.root), ())
        self.assertFalse(has_turn_file_references(()))

    def test_aggregate_diff_is_primary_and_structured_items_supplement_it(
        self,
    ) -> None:
        report = self.root / "report.md"
        item_only = self.root / "item-only.txt"
        report.write_text("report", encoding="utf-8")
        item_only.write_text("item", encoding="utf-8")
        diff = (
            "diff --git a/report.md b/report.md\n"
            "--- a/report.md\n"
            "+++ b/report.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        items = (
            file_change(
                {"path": "report.md", "diff": "", "kind": {"type": "update"}},
                {
                    "path": "item-only.txt",
                    "diff": "",
                    "kind": {"type": "add"},
                },
            ),
        )

        files = extract_turn_files(items, self.root, turn_diff=diff)

        self.assertEqual(
            [item.display_path for item in files],
            ["report.md", "item-only.txt"],
        )
        self.assertTrue(has_turn_file_references((), turn_diff=diff))

    def test_diff_parser_handles_delete_rename_binary_quotes_and_hunks(self) -> None:
        renamed = self.root / "docs" / "new report.md"
        binary = self.root / "assets" / "chart.png"
        unicode_path = self.root / "报告.md"
        deleted = self.root / "deleted.txt"
        for path, body in (
            (renamed, b"renamed"),
            (binary, b"binary"),
            (unicode_path, b"unicode"),
            (deleted, b"still present but deleted by this diff"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        diff = (
            "diff --git a/old.txt b/deleted.txt\n"
            "--- a/deleted.txt\n"
            "+++ /dev/null\n"
            "diff --git \"a/docs/old report.md\" \"b/docs/new report.md\"\n"
            "rename from docs/old report.md\n"
            "rename to docs/new report.md\n"
            "diff --git a/assets/chart.png b/assets/chart.png\n"
            "Binary files a/assets/chart.png and b/assets/chart.png differ\n"
            r'diff --git "a/\346\212\245\345\221\212.md" "b/\346\212\245\345\221\212.md"'
            "\n--- \"a/\\346\\212\\245\\345\\221\\212.md\"\n"
            "+++ \"b/\\346\\212\\245\\345\\221\\212.md\"\n"
            "@@ -1 +1 @@\n"
            "+++ b/not-a-header.txt\n"
        )

        self.assertEqual(
            turn_diff_paths(diff),
            (
                "docs/new report.md",
                "assets/chart.png",
                "报告.md",
            ),
        )
        files = extract_turn_files((), self.root, turn_diff=diff)
        self.assertEqual(
            [item.display_path for item in files],
            ["docs/new report.md", "assets/chart.png", "报告.md"],
        )

    def test_diff_parser_counts_multi_hunk_new_delete_and_rename(self) -> None:
        for relative, body in (
            ("src/app.py", b"app"),
            ("src/new.py", b"new"),
            ("docs/renamed.md", b"renamed"),
            ("assets/chart.png", b"\x89PNG\r\n\x1a\nimage"),
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,3 @@\n"
            " context\n"
            "-old\n"
            "+new\n"
            "+extra\n"
            "@@ -10,2 +11 @@\n"
            "-old one\n"
            "-old two\n"
            "+replacement\n"
            "diff --git a/src/new.py b/src/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/src/new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+first\n"
            "+second\n"
            "diff --git a/gone.txt b/gone.txt\n"
            "deleted file mode 100644\n"
            "--- a/gone.txt\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-gone one\n"
            "-gone two\n"
            "diff --git a/docs/old.md b/docs/renamed.md\n"
            "similarity index 100%\n"
            "rename from docs/old.md\n"
            "rename to docs/renamed.md\n"
            "diff --git a/assets/chart.png b/assets/chart.png\n"
            "Binary files a/assets/chart.png and b/assets/chart.png differ\n"
        )

        summary = turn_diff_summary(diff)

        self.assertEqual((summary.additions, summary.deletions), (5, 5))
        self.assertEqual(
            [
                (item.path, item.additions, item.deletions)
                for item in summary.files
            ],
            [
                ("src/app.py", 3, 3),
                ("src/new.py", 2, 0),
                ("docs/renamed.md", 0, 0),
                ("assets/chart.png", None, None),
            ],
        )
        files = extract_turn_files((), self.root, turn_diff=diff)
        self.assertEqual(
            [
                (
                    item.display_path,
                    item.additions,
                    item.deletions,
                    item.media_kind,
                )
                for item in files
            ],
            [
                ("src/app.py", 3, 3, "file"),
                ("src/new.py", 2, 0, "file"),
                ("docs/renamed.md", 0, 0, "file"),
                ("assets/chart.png", None, None, "image"),
            ],
        )

    def test_diff_parser_preserves_metadata_path_semantics(self) -> None:
        for relative, body in (
            ("src/app.py", b"changed"),
            ("a/new name.txt", b"renamed"),
            ("assets/before and after.bin", b"binary"),
            ("copies/a/copy.txt", b"copy"),
            ("empty new.txt", b""),
            ("mode only.txt", b"mode"),
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/old name.txt b/a/new name.txt\n"
            "similarity index 100%\n"
            "rename from old name.txt\n"
            "rename to a/new name.txt\n"
            "diff --git a/assets/before and after.bin "
            "b/assets/before and after.bin\n"
            "Binary files a/assets/before and after.bin and "
            "b/assets/before and after.bin differ\n"
            "diff --git a/source.txt b/copies/a/copy.txt\n"
            "similarity index 100%\n"
            "copy from source.txt\n"
            "copy to copies/a/copy.txt\n"
            "diff --git a/empty-delete.txt b/empty-delete.txt\n"
            "deleted file mode 100644\n"
            "index e69de29..0000000\n"
            "diff --git a/empty new.txt b/empty new.txt\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "diff --git a/mode only.txt b/mode only.txt\n"
            "old mode 100644\n"
            "new mode 100755\n"
        )

        summary = turn_diff_summary(diff)

        self.assertEqual((summary.additions, summary.deletions), (None, None))
        self.assertEqual(
            [
                (item.path, item.additions, item.deletions)
                for item in summary.files
            ],
            [
                ("src/app.py", 1, 1),
                ("a/new name.txt", 0, 0),
                ("assets/before and after.bin", None, None),
                ("copies/a/copy.txt", None, None),
                ("empty new.txt", None, None),
                ("mode only.txt", None, None),
            ],
        )
        self.assertNotIn("empty-delete.txt", turn_diff_paths(diff))

    def test_precomputed_summary_is_reused_for_reference_and_file_extraction(
        self,
    ) -> None:
        path = self.root / "report.md"
        path.write_text("new", encoding="utf-8")
        diff = (
            "diff --git a/report.md b/report.md\n"
            "--- a/report.md\n"
            "+++ b/report.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        summary = turn_diff_summary(diff)

        with patch(
            "netizen.turn_files.turn_diff_summary",
            side_effect=AssertionError("diff was parsed again"),
        ):
            self.assertTrue(
                has_turn_file_references((), diff_summary=summary)
            )
            files = extract_turn_files(
                (),
                self.root,
                diff_summary=summary,
            )

        self.assertEqual(
            [(item.display_path, item.additions, item.deletions) for item in files],
            [("report.md", 1, 1)],
        )

    def test_malformed_hunk_preserves_paths_but_omits_all_counts(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        diff = (
            "diff --git a/first.txt b/first.txt\n"
            "--- a/first.txt\n"
            "+++ b/first.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/second.txt b/second.txt\n"
            "--- a/second.txt\n"
            "+++ b/second.txt\n"
            "@@ malformed @@\n"
            "+untrusted\n"
        )

        summary = turn_diff_summary(diff)

        self.assertEqual((summary.additions, summary.deletions), (None, None))
        self.assertEqual(
            [(item.path, item.additions, item.deletions) for item in summary.files],
            [
                ("first.txt", None, None),
                ("second.txt", None, None),
            ],
        )
        self.assertEqual(
            [
                (item.display_path, item.additions, item.deletions)
                for item in extract_turn_files((), self.root, turn_diff=diff)
            ],
            [
                ("first.txt", None, None),
                ("second.txt", None, None),
            ],
        )

    def test_missing_hunk_and_stray_body_lines_fail_closed(self) -> None:
        for body in (
            (
                "diff --git a/truncated.txt b/truncated.txt\n"
                "--- a/truncated.txt\n"
                "+++ b/truncated.txt\n"
            ),
            (
                "diff --git a/stray.txt b/stray.txt\n"
                "-old\n"
                "+new\n"
            ),
        ):
            with self.subTest(body=body):
                summary = turn_diff_summary(body)
                self.assertEqual(
                    (summary.additions, summary.deletions),
                    (None, None),
                )
                self.assertEqual(len(summary.files), 1)
                self.assertEqual(
                    (
                        summary.files[0].additions,
                        summary.files[0].deletions,
                    ),
                    (None, None),
                )

    def test_truncated_nonempty_prelude_invalidates_later_counts(self) -> None:
        diff = (
            "--- a/first.txt\n"
            "+++ b/first.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/second.txt b/second.txt\n"
            "--- a/second.txt\n"
            "+++ b/second.txt\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "+after\n"
        )

        summary = turn_diff_summary(diff)

        self.assertEqual((summary.additions, summary.deletions), (None, None))
        self.assertEqual(
            [(item.path, item.additions, item.deletions) for item in summary.files],
            [("second.txt", None, None)],
        )

    def test_missing_hunk_invalidates_later_file_counts(self) -> None:
        diff = (
            "diff --git a/first.txt b/first.txt\n"
            "--- a/first.txt\n"
            "+++ b/first.txt\n"
            "diff --git a/second.txt b/second.txt\n"
            "--- a/second.txt\n"
            "+++ b/second.txt\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "+after\n"
        )

        summary = turn_diff_summary(diff)

        self.assertEqual((summary.additions, summary.deletions), (None, None))
        self.assertEqual(
            [(item.path, item.additions, item.deletions) for item in summary.files],
            [
                ("first.txt", None, None),
                ("second.txt", None, None),
            ],
        )

    def test_unsupported_metadata_only_changes_preserve_paths_without_counts(
        self,
    ) -> None:
        cases = (
            (
                "diff --git a/nonempty-new.txt b/nonempty-new.txt\n"
                "new file mode 100644\n"
                "index 0000000..1234567\n",
                ("nonempty-new.txt",),
            ),
            (
                "diff --git a/nonempty-delete.txt b/nonempty-delete.txt\n"
                "deleted file mode 100644\n"
                "index 1234567..0000000\n",
                (),
            ),
            (
                "diff --git a/old.txt b/renamed.txt\n"
                "similarity index 80%\n"
                "rename from old.txt\n"
                "rename to renamed.txt\n",
                ("renamed.txt",),
            ),
            (
                "diff --git a/mode.txt b/mode.txt\n"
                "old mode 100644\n"
                "new mode 100755\n"
                "index 1234567..89abcde\n",
                ("mode.txt",),
            ),
        )
        for body, expected_paths in cases:
            with self.subTest(body=body):
                summary = turn_diff_summary(body)
                self.assertEqual(
                    (summary.additions, summary.deletions),
                    (None, None),
                )
                self.assertEqual(
                    tuple(item.path for item in summary.files),
                    expected_paths,
                )
                self.assertTrue(
                    all(
                        item.additions is None and item.deletions is None
                        for item in summary.files
                    )
                )

    def test_accepts_exact_external_files_and_generated_images_without_scanning(
        self,
    ) -> None:
        home = self.home
        external = home / f"{self.root.name}-outside.txt"
        external.write_text("outside", encoding="utf-8")
        generated = home / ".codex" / "generated_images" / "thread" / "image.png"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        directory = self.root / "folder"
        directory.mkdir()
        link = self.root / "escape.txt"
        link.symlink_to(external)
        try:
            files = extract_turn_files(
                (
                    file_change(
                        {"path": str(external), "diff": "", "kind": {"type": "add"}},
                        {"path": "folder", "diff": "", "kind": {"type": "add"}},
                        {"path": "escape.txt", "diff": "", "kind": {"type": "add"}},
                    ),
                    image_generation(generated),
                ),
                self.root,
                home_directory=home,
            )
        finally:
            external.unlink()

        self.assertEqual(
            [item.display_path for item in files],
            [f"~/{external.name}", "生成图片/image.png"],
        )
        self.assertEqual(
            [item.resolved_path for item in files],
            [external.resolve(), generated.resolve()],
        )
        self.assertEqual([item.media_kind for item in files], ["file", "image"])

    def test_external_display_is_not_absolute_and_rebinding_follows_target(
        self,
    ) -> None:
        first = self.root.parent / f"{self.root.name}-first.txt"
        second = self.root.parent / f"{self.root.name}-second.txt"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        link = self.root / "current.txt"
        link.symlink_to(first)
        item = file_change(
            {"path": link.name, "diff": "", "kind": {"type": "update"}},
        )
        hidden_home = self.root / "unrelated-home"
        hidden_home.mkdir()
        try:
            before = extract_turn_files(
                (item,),
                self.root,
                home_directory=hidden_home,
            )[0]
            link.unlink()
            link.symlink_to(second)
            after = extract_turn_files(
                (item,),
                self.root,
                home_directory=hidden_home,
            )[0]
        finally:
            first.unlink()
            second.unlink()

        self.assertTrue(before.display_path.startswith("外部文件/"))
        self.assertFalse(before.display_path.startswith("/"))
        self.assertNotEqual(before.resolved_path, after.resolved_path)

    def test_pagination_is_complete_bounded_and_strict(self) -> None:
        changes: list[dict[str, object]] = []
        for index in range(18):
            path = self.root / f"file-{index:02}.txt"
            path.write_text(str(index), encoding="utf-8")
            changes.append(
                {"path": path.name, "diff": "", "kind": {"type": "add"}}
            )
        files = extract_turn_files((file_change(*changes),), self.root)

        first = paginate_turn_files(files, 0)
        last = paginate_turn_files(files, 2)

        self.assertEqual(len(first.items), 8)
        self.assertEqual(len(last.items), 2)
        self.assertEqual(first.total_items, 18)
        self.assertEqual(first.total_pages, 3)
        self.assertEqual(last.page, 2)
        with self.assertRaises(TurnFileError):
            paginate_turn_files(files, 3)
        with self.assertRaises(TurnFileError):
            paginate_turn_files(files, -1)

    def test_file_size_remains_internal_metadata(self) -> None:
        path = self.root / "report.pdf"
        path.write_bytes(b"x" * 1536)
        files = extract_turn_files(
            (
                file_change(
                    {"path": path.name, "diff": "", "kind": {"type": "add"}},
                ),
            ),
            self.root,
        )

        self.assertEqual(files[0].size, 1536)

    def test_v4_absolute_path_rechecks_current_regular_file_without_project_gate(
        self,
    ) -> None:
        outside = self.home / "outside.bin"
        outside.write_bytes(b"first")

        inspected = inspect_turn_file_path(str(outside), "outside.bin")
        self.assertTrue(inspected.available)
        self.assertEqual(inspected.size, 5)
        outside.write_bytes(b"new current content")
        current = require_turn_file_path(str(outside), "outside.bin")
        self.assertEqual(current.size, 19)

        outside.unlink()
        self.assertFalse(
            inspect_turn_file_path(str(outside), "outside.bin").available
        )
        with self.assertRaisesRegex(TurnFileError, "已不可用"):
            require_turn_file_path(str(outside), "outside.bin")
        with self.assertRaises(TurnFileError):
            require_turn_file_path(str(self.root), "directory")


if __name__ == "__main__":
    unittest.main()
