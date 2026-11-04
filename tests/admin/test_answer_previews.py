from __future__ import annotations

import base64
from html.parser import HTMLParser
import struct
import unittest
from unittest.mock import patch
import zlib

from netizen_cli.admin.answer_previews import render_answer_preview
from netizen_cli.project_files import FileBrowseError, validate_image
from netizen_cli.runtime.contracts import NativeAnswerImage, NativeLatestAnswer


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOesAAAAASUVORK5CYII="


class Images(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.images = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))


def render(text, images=(), status="completed"):
    return render_answer_preview(NativeLatestAnswer("fork", "turn", status, text, images=tuple(images)))


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def solid_png(width, height):
    compressor = zlib.compressobj()
    compressed = b"".join(compressor.compress(bytes(width + 1)) for _ in range(height)) + compressor.flush()
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    ).decode("ascii")


def padded_png(size):
    raw = base64.b64decode(PNG)
    text = b"Comment\0" + b"x" * (size - len(raw) - 12 - 8)
    return base64.b64encode(raw[:-12] + chunk(b"tEXt", text) + raw[-12:]).decode("ascii")


class AnswerPreviewsTest(unittest.TestCase):
    def test_only_exact_referenced_native_bytes_are_selected_without_file_access(self):
        path = "/missing/source-thread/猫 图.png"
        result = render(
            f"before ![猫](<{path}>) after ![again](/missing/source-thread/%E7%8C%AB%20%E5%9B%BE.png)",
            [NativeAnswerImage(path, PNG), NativeAnswerImage("/unused.png", PNG)],
        )
        self.assertEqual(result["images"], [{"id": "image-0", "mime": "image/png", "data": PNG}])
        nodes = Images(result["markdownHtml"]).images
        self.assertEqual([node["data-answer-image"] for node in nodes], ["image-0", "image-0"])
        self.assertTrue(all("src" not in node and node["loading"] == "lazy" for node in nodes))
        self.assertNotIn("/missing", result["markdownHtml"])
        self.assertNotIn(PNG, result["markdownHtml"])
        self.assertIn("before", result["markdownHtml"])

    def test_code_links_raw_html_and_nested_alt_do_not_select_images(self):
        result = render(
            '`![code](/code.png)` [link](/link.png) <img src="/html.png">\n\n'
            '![outer ![inner](/inner.png) text](/outer.png)',
            [NativeAnswerImage(f"/{name}.png", PNG) for name in ("code", "link", "html", "inner", "outer")],
        )
        self.assertEqual(len(result["images"]), 1)
        nodes = Images(result["markdownHtml"]).images
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["alt"], "outer inner text")
        self.assertIn("&lt;img", result["markdownHtml"])

    def test_unsupported_or_ambiguous_references_remain_text(self):
        targets = ("a.png", "https://example.com/a.png", "//example.com/a.png", "/a.png?x=1",
                   "/a.png#fragment", "/a/../a.png", "/%2561.png", "file://host/a.png", "/missing.png")
        result = render("\n".join(f"![image]({target})" for target in targets), [NativeAnswerImage("/a.png", PNG)])
        self.assertEqual(result["images"], [])
        self.assertEqual(Images(result["markdownHtml"]).images, [])
        result = render("![ambiguous](/a.png)", [NativeAnswerImage("/a.png", PNG)] * 2)
        self.assertEqual(result["images"], [])
        self.assertIn("ambiguous", result["markdownHtml"])

    def test_noncompleted_invalid_or_unsupported_images_preserve_text_and_other_images(self):
        for status in ("failed", "interrupted", "inProgress"):
            with self.subTest(status=status):
                result = render("text ![image](/a.png)", [NativeAnswerImage("/a.png", PNG)], status)
                self.assertEqual(result["images"], [])
                self.assertIn("text", result["markdownHtml"])
        invalid = ("", "not base64", base64.b64encode(b"<svg></svg>").decode(),
                   base64.b64encode(base64.b64decode(PNG)[:-12]).decode(), solid_png(16385, 1))
        for data in invalid:
            with self.subTest(data=data[:20]):
                result = render("before ![bad](/bad.png) after ![good](/good.png)",
                                [NativeAnswerImage("/bad.png", data), NativeAnswerImage("/good.png", PNG)])
                self.assertEqual(len(result["images"]), 1)
                self.assertIn("after", result["markdownHtml"])
                self.assertEqual(Images(result["markdownHtml"]).images[0]["alt"], "good")

    def test_byte_limits_precede_decoding_and_repeated_bytes_are_counted_once(self):
        size = len(base64.b64decode(PNG))
        with patch("netizen_cli.admin.answer_previews.MAX_IMAGE_BYTES", size - 1), \
                patch("netizen_cli.admin.answer_previews.base64.b64decode") as decode:
            self.assertEqual(render("![image](/a.png)", [NativeAnswerImage("/a.png", PNG)])["images"], [])
            decode.assert_not_called()
        with patch("netizen_cli.admin.answer_previews.MAX_TOTAL_IMAGE_BYTES", size * 2):
            result = render("![a](/a.png) ![a](/a.png) ![b](/b.png) ![c](/c.png)",
                            [NativeAnswerImage(f"/{name}.png", PNG) for name in "abc"])
        self.assertEqual(len(result["images"]), 2)
        self.assertEqual(len(Images(result["markdownHtml"]).images), 3)

    def test_total_pixels_bound_highly_compressed_images_and_repeated_occurrences(self):
        large = solid_png(4000, 4000)
        self.assertLess(len(large), 100_000)
        for names in (list("abcdefgh"), ["a"] * 8):
            with self.subTest(names=names):
                result = render(" ".join(f"![{name}](/{name}.png)" for name in names),
                                [NativeAnswerImage(f"/{name}.png", large) for name in set(names)])
                self.assertEqual(len(result["images"]), min(2, len(set(names))))
                self.assertEqual(len(Images(result["markdownHtml"]).images), 2)

    def test_larger_images_fit_the_answer_byte_budget_without_relaxing_file_previews(self):
        data = padded_png(6 * 1024 * 1024)
        result = render(" ".join(f"![{name}](/{name}.png)" for name in "abcd"),
                        [NativeAnswerImage(f"/{name}.png", data) for name in "abcd"])
        self.assertEqual(len(result["images"]), 3)
        self.assertEqual(len(Images(result["markdownHtml"]).images), 3)
        with self.assertRaises(FileBrowseError) as error:
            validate_image(base64.b64decode(data), "image/png")
        self.assertEqual(error.exception.code, "preview_too_large")

    def test_new_per_image_byte_boundary_is_inclusive_and_rejects_before_decoding(self):
        data = padded_png(10 * 1024 * 1024)
        self.assertEqual(len(render("![image](/a.png)", [NativeAnswerImage("/a.png", data)])["images"]), 1)
        oversized = padded_png(10 * 1024 * 1024 + 1)
        with patch("netizen_cli.admin.answer_previews.base64.b64decode") as decode:
            self.assertEqual(render("![image](/a.png)", [NativeAnswerImage("/a.png", oversized)])["images"], [])
            decode.assert_not_called()

    def test_larger_dimensions_and_pixel_areas_remain_bounded_and_answer_specific(self):
        for width, height, accepted in ((16384, 1, True), (16385, 1, False),
                                        (8000, 4000, True), (8000, 4001, False)):
            with self.subTest(width=width, height=height):
                data = solid_png(width, height)
                result = render("text ![image](/a.png)", [NativeAnswerImage("/a.png", data)])
                self.assertEqual(len(result["images"]), int(accepted))
                self.assertIn("text", result["markdownHtml"])
                with self.assertRaises(FileBrowseError) as error:
                    validate_image(base64.b64decode(data), "image/png")
                self.assertEqual(error.exception.code, "preview_too_large")

    def test_image_count_and_response_limits_keep_complete_text(self):
        result = render(" ".join(f"![image](/{index}.png)" for index in range(18)),
                        [NativeAnswerImage(f"/{index}.png", PNG) for index in range(18)])
        self.assertEqual(len(result["images"]), 16)
        self.assertEqual(len(Images(result["markdownHtml"]).images), 16)
        repeated = render(" ".join(["![image](/a.png)"] * 18), [NativeAnswerImage("/a.png", PNG)])
        self.assertEqual(len(repeated["images"]), 1)
        self.assertEqual(len(Images(repeated["markdownHtml"]).images), 16)
        with patch("netizen_cli.admin.answer_previews.MAX_IMAGE_RESPONSE_BYTES", 1):
            result = render("before ![image](/a.png) after", [NativeAnswerImage("/a.png", PNG)])
        self.assertEqual(result["images"], [])
        self.assertEqual(Images(result["markdownHtml"]).images, [])
        self.assertIn("before", result["markdownHtml"])
        self.assertIn("after", result["markdownHtml"])
        self.assertIn("previewMessage", result)
