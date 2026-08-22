from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openai_codex.types import ThreadItem

from netizen.turn_files import (
    TurnFileError,
    extract_turn_files,
    format_file_size,
    has_turn_file_references,
    inspect_turn_file_path,
    paginate_turn_files,
    require_turn_file_path,
    turn_diff_paths,
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

    def test_size_formatting(self) -> None:
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

        self.assertEqual(format_file_size(files[0].size), "1.5 KB")
        self.assertEqual(format_file_size(0), "0 B")

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
