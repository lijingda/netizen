from __future__ import annotations

import base64
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from netizen_cli import project_files
from netizen_cli.project_files import FileBrowseError, list_directory, read_file, root_identity


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOesAAAAASUVORK5CYII="
)
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def webp_header(kind: bytes, payload: bytes) -> bytes:
    chunk = kind + len(payload).to_bytes(4, "little") + payload
    if len(payload) % 2:
        chunk += b"\0"
    return b"RIFF" + (len(chunk) + 4).to_bytes(4, "little") + b"WEBP" + chunk


class ProjectFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="netizen-project-files-")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / "project"
        self.root.mkdir()
        self.context = "project:registered-generation:main"
        self.identity = root_identity(self.root, context=self.context)

    def read(self, path):
        return read_file(self.root, path, identity=self.identity, context=self.context)

    def listing(self, path=""):
        return list_directory(self.root, path, identity=self.identity, context=self.context)

    def assert_error(self, code, operation, *args, **kwargs):
        with self.assertRaises(FileBrowseError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(str(self.parent), caught.exception.message)
        return caught.exception

    def test_lists_one_directory_with_kinds_and_directories_first(self):
        (self.root / "sub").mkdir()
        (self.root / "B.txt").write_text("hello")
        (self.root / "a.txt").write_text("中文", encoding="utf-8")
        (self.root / "sub" / "child.txt").write_text("child")
        page = self.listing()
        self.assertEqual([entry["name"] for entry in page["entries"]], ["sub", "a.txt", "B.txt"])
        self.assertEqual([entry["kind"] for entry in page["entries"]], ["directory", "file", "file"])
        self.assertFalse(page["truncated"])
        self.assertEqual(self.listing("sub")["entries"][0]["path"], "sub/child.txt")

    def test_directory_enumeration_is_bounded_and_truncation_is_explicit(self):
        for index in range(2001):
            (self.root / f"file-{index:04}").touch()
        page = self.listing()
        self.assertEqual(len(page["entries"]), 2000)
        self.assertTrue(page["truncated"])

    def test_rejects_unsafe_relative_paths_before_opening(self):
        for path in ("/etc/passwd", "../secret", "a/../b", "a//b", "a/", "./a", "a\\b", "a\0b", "a\nb", "a\x7fb", ".git", ".GIT/config", "a/.git/config", "a/\udcff", "/".join(["a"] * 129)):
            with self.subTest(path=repr(path)), patch.object(project_files.os, "open", side_effect=AssertionError("must not open")):
                self.assert_error("invalid_path", self.read, path)
                self.assert_error("invalid_path", self.listing, path)
        self.assert_error("invalid_path", self.read, "")

    def test_hides_git_and_unusable_names_but_keeps_other_hidden_files(self):
        (self.root / ".git").mkdir()
        (self.root / "line\nbreak").touch()
        (self.root / ".env.example").touch()
        self.assertEqual([entry["name"] for entry in self.listing()["entries"]], [".env.example"])

    def test_total_root_and_relative_depth_is_bounded_before_opening(self):
        root = Path("/" + "/".join(["root"] * 100))
        path = "/".join(["directory"] * 40)
        with patch.object(project_files.os, "open", side_effect=AssertionError("must not open")):
            self.assert_error("invalid_path", list_directory, root, path, identity=self.identity, context=self.context)

    def test_reads_utf8_bom_and_empty_text_without_duplicate_bytes(self):
        content = "# 标题\nhello\tworld\r\n"
        (self.root / "readme.md").write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
        preview = self.read("readme.md")
        self.assertEqual(preview.kind, "text")
        self.assertEqual(preview.text, content)
        self.assertIsNone(preview.data)
        self.assertEqual(preview.size, len(content.encode("utf-8")) + 3)
        self.assertEqual(preview.mime, "text/plain; charset=utf-8")
        (self.root / "empty").touch()
        self.assertEqual(self.read("empty").text, "")

    def test_active_html_and_svg_are_only_text(self):
        for name, content in (("index.html", "<script>alert(1)</script>"), ("drawing.svg", '<svg onload="alert(1)"></svg>')):
            with self.subTest(name=name):
                (self.root / name).write_text(content)
                preview = self.read(name)
                self.assertEqual(preview.kind, "text")
                self.assertEqual(preview.mime, "text/plain; charset=utf-8")
                self.assertEqual(preview.text, content)

    def test_rejects_binary_and_non_utf8(self):
        for name, data, code in (("binary", b"abc\0def", "unsupported_file"), ("control", b"hello\x1b[0m", "unsupported_file"), ("legacy", b"caf\xe9", "invalid_encoding")):
            with self.subTest(name=name):
                (self.root / name).write_bytes(data)
                self.assert_error(code, self.read, name)

    def test_text_byte_line_and_single_line_limits(self):
        path = self.root / "text"
        for content in (b"x" * (project_files.MAX_TEXT_BYTES + 1), b"\n" * 10000, b"x" * 20001):
            with self.subTest(size=len(content)):
                path.write_bytes(content)
                self.assert_error("preview_too_large", self.read, "text")
        for content in (b"\n" * 9999, b"x" * 20000):
            path.write_bytes(content)
            self.assertEqual(self.read("text").text.encode(), content)
        content = (b"x" * 1000 + b"\n") * 1047
        content += b"x" * (project_files.MAX_TEXT_BYTES - len(content))
        path.write_bytes(content)
        self.assertEqual(self.read("text").size, project_files.MAX_TEXT_BYTES)

    def test_image_content_and_mime_are_detected_with_bounded_dimensions(self):
        for name, content, mime in (("pixel.png", PNG, "image/png"), ("pixel.gif", GIF, "image/gif"), ("no-extension", PNG, "image/png")):
            with self.subTest(name=name):
                (self.root / name).write_bytes(content)
                preview = self.read(name)
                self.assertEqual(preview.kind, "image")
                self.assertEqual(preview.mime, mime)
                self.assertEqual(preview.data, content)
                self.assertIsNone(preview.text)

    def test_jpeg_and_webp_dimension_headers(self):
        # Header parsing deliberately does not decode image pixel data.
        jpeg = b"\xff\xd8\xff\xe0\x00\x04xx\xff\xc0\x00\x0b\x08\x00\x01\x00\x02\x01\x01\x11\x00\xff\xd9"
        vp8x = webp_header(b"VP8X", b"\0" * 4 + b"\x01\0\0" + b"\0\0\0")
        vp8l = webp_header(b"VP8L", b"\x2f" + (1).to_bytes(4, "little"))
        vp8 = webp_header(b"VP8 ", b"\0" * 3 + b"\x9d\x01\x2a\x02\0\x01\0")
        for index, content in enumerate((jpeg, vp8x, vp8l, vp8)):
            name = f"image-{index}"
            (self.root / name).write_bytes(content)
            self.assertEqual(self.read(name).kind, "image")

    def test_invalid_and_oversized_images_do_not_return_bytes(self):
        cases = [b"not an image", PNG[:24]]
        for width, height in ((0, 1), (8193, 1), (4001, 4000)):
            cases.append(PNG[:16] + width.to_bytes(4, "big") + height.to_bytes(4, "big") + PNG[24:])
        for index, content in enumerate(cases):
            with self.subTest(index=index):
                (self.root / "image.png").write_bytes(content)
                code = "invalid_image" if index < 3 else "preview_too_large"
                self.assert_error(code, self.read, "image.png")
        with (self.root / "image.png").open("wb") as output:
            output.write(PNG)
            output.truncate(project_files.MAX_IMAGE_BYTES + 1)
        self.assert_error("preview_too_large", self.read, "image.png")

    def test_rejects_malformed_jpeg_and_webp_headers(self):
        for content in (b"\xff\xd8\xff\xc0\x00\x01", b"\xff\xd8\xff\xda", b"RIFF\0\0\0\0WEBPVP8X", webp_header(b"VP8L", b"\x2f\xff\xff\xff\xff")):
            (self.root / "image").write_bytes(content)
            self.assert_error("invalid_image", self.read, "image")

    def test_symlinks_are_visible_but_never_followed(self):
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret")
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        (self.root / "secret").symlink_to(outside / "secret")
        self.assertTrue(all(entry["kind"] == "symlink" for entry in self.listing()["entries"]))
        for operation, path in ((self.listing, "link"), (self.read, "link/secret"), (self.read, "secret")):
            self.assert_error("unsafe_path", operation, path)

    def test_root_symlink_and_symlink_ancestor_are_rejected(self):
        alias = self.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assert_error("unsafe_path", root_identity, alias, context=self.context)
        outer = self.parent / "outer"
        outer.symlink_to(self.parent, target_is_directory=True)
        self.assert_error("unsafe_path", root_identity, outer / "project", context=self.context)

    def test_identity_includes_context_and_root_directory_identity(self):
        self.assertNotEqual(self.identity, root_identity(self.root, context="new-registration"))
        self.assert_error("root_changed", list_directory, self.root, "", identity=self.identity, context="new-registration")
        self.root.rename(self.parent / "previous")
        self.root.mkdir()
        self.assert_error("root_changed", self.listing)

    def test_fifo_and_directory_are_rejected_without_blocking(self):
        os.mkfifo(self.root / "pipe")
        (self.root / "directory").mkdir()
        for name in ("pipe", "directory"):
            self.assert_error("unsupported_file", self.read, name)
        entries = {entry["name"]: entry["kind"] for entry in self.listing()["entries"]}
        self.assertEqual(entries["pipe"], "unsupported")

    def test_socket_and_device_metadata_are_rejected_before_open(self):
        path = self.root / "special"
        path.touch()
        real_stat = os.stat
        info = path.stat()
        for mode in (stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            special = os.stat_result((mode | 0o600, *tuple(info)[1:]))

            def metadata(name, **kwargs):
                return special if name == "special" else real_stat(name, **kwargs)

            with self.subTest(mode=mode), patch.object(project_files.os, "stat", side_effect=metadata):
                self.assert_error("unsupported_file", self.read, "special")

    def test_fifo_swapped_in_before_open_is_nonblocking_and_rejected(self):
        path = self.root / "file"
        path.write_text("text")
        real_open = os.open

        def replace(name, flags, **kwargs):
            if name == "file":
                path.unlink()
                os.mkfifo(path)
                self.assertTrue(flags & os.O_NONBLOCK)
            return real_open(name, flags, **kwargs)

        with patch.object(project_files.os, "open", side_effect=replace):
            self.assert_error("unsupported_file", self.read, "file")

    def test_symlink_swapped_in_before_open_is_never_read(self):
        path = self.root / "file"
        path.write_text("safe")
        secret = self.parent / "secret"
        secret.write_text("secret")
        real_open = os.open

        def replace(name, flags, **kwargs):
            if name == "file":
                path.unlink()
                path.symlink_to(secret)
            return real_open(name, flags, **kwargs)

        with patch.object(project_files.os, "open", side_effect=replace), patch.object(project_files.os, "read", side_effect=AssertionError("must not read")):
            self.assert_error("unsafe_path", self.read, "file")

    def test_root_or_intermediate_directory_replacement_discards_read(self):
        real_read = os.read
        for replace_root in (True, False):
            with self.subTest(replace_root=replace_root):
                directory = self.root / "sub"
                directory.mkdir(exist_ok=True)
                (directory / "file").write_text("text")
                changed = False

                def replace(fd, count):
                    nonlocal changed
                    data = real_read(fd, count)
                    if not changed:
                        changed = True
                        target = self.root if replace_root else directory
                        target.rename(target.with_name(target.name + "-old"))
                        target.mkdir()
                    return data

                with patch.object(project_files.os, "read", side_effect=replace):
                    self.assert_error("root_changed", self.read, "sub/file")
                self.identity = root_identity(self.root, context=self.context)

    def test_file_mutation_or_replacement_discards_read(self):
        real_read = os.read
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                path = self.root / "file"
                path.write_text("before")
                before = path.stat()
                changed = False

                def mutate(fd, count):
                    nonlocal changed
                    data = real_read(fd, count)
                    if not changed:
                        changed = True
                        if replacement:
                            path.unlink()
                        path.write_text("after!")
                        # Make mutation observable even on coarse-timestamp filesystems.
                        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
                    return data

                with patch.object(project_files.os, "read", side_effect=mutate):
                    self.assert_error("file_changed", self.read, "file")

    def test_file_growth_cannot_bypass_read_limit(self):
        path = self.root / "file"
        path.write_text("small")
        real_read = os.read
        changed = False

        def grow(fd, count):
            nonlocal changed
            result = real_read(fd, count)
            if not changed:
                changed = True
                with path.open("ab") as output:
                    output.write(b"x" * project_files.MAX_TEXT_BYTES)
            return result

        with patch.object(project_files.os, "read", side_effect=grow):
            self.assert_error("preview_too_large", self.read, "file")

    def test_missing_paths_have_safe_errors(self):
        self.assert_error("not_found", self.read, "missing")
        self.assert_error("not_found", self.listing, "missing")

    def test_version_changes_after_edit(self):
        path = self.root / "file"
        path.write_text("old")
        before = self.read("file")
        self.assertEqual(before.version, self.read("file").version)
        path.write_text("new text")
        self.assertNotEqual(before.version, self.read("file").version)


if __name__ == "__main__":
    unittest.main()
