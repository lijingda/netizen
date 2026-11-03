from __future__ import annotations

from html.parser import HTMLParser
import unittest
from unittest.mock import patch
from urllib.parse import quote

from netizen_cli.admin.file_previews import (
    MAX_MARKDOWN_BYTES,
    MAX_MARKDOWN_IMAGES,
    MAX_MARKDOWN_TOKENS,
    PreviewUnavailable,
    render_markdown,
)


class ParsedHTML(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    def attributes(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]


class FilePreviewsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.images: list[str] = []

    def image_url(self, path: str) -> str:
        self.images.append(path)
        return "/api/projects/p/files/image?path=" + quote(path, safe="")

    def render(self, text: str, *, path: str = "docs/README.md") -> ParsedHTML:
        return ParsedHTML(render_markdown(text, path=path, image_url=self.image_url))

    def test_commonmark_text_structure_and_code_remain_readable(self) -> None:
        parsed = self.render("# 标题\n\n**粗体**、*斜体*、`<script>`\n\n- 一\n- 二\n\n```py\nprint('<x>')\n```\n")
        tags = [tag for tag, _ in parsed.tags]
        for tag in ("h1", "strong", "em", "code", "ul", "li", "pre"):
            self.assertIn(tag, tags)
        self.assertIn("<script>", parsed.text)
        self.assertEqual(parsed.attributes("script"), [])
        self.assertEqual(parsed.attributes("code")[-1], {"class": "language-py"})

    def test_raw_html_and_scripts_are_inert_text(self) -> None:
        source = '<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">\n\n<iframe src="https://bad.test"></iframe>'
        parsed = self.render(source)
        self.assertFalse(any(tag in {"script", "img", "iframe"} for tag, _ in parsed.tags))
        self.assertIn('<script>alert(1)</script>', "".join(parsed.text))
        self.assertEqual(self.images, [])

    def test_answer_preview_shares_tables_and_has_no_file_authority(self) -> None:
        source = "# 回答\n\n| 项目 | 说明 |\n| --- | ---: |\n| **费用** | `<script>` |\n\n"
        source += "[外链](https://example.org) [本地](docs/a.md) [绝对](/tmp/a.md) [危险](javascript:alert(1))\n\n"
        source += '<script>alert(1)</script>\n\n![本地图](a.png) ![远程图](https://example.org/a.png)'
        parsed = ParsedHTML(render_markdown(source))
        for tag in ("h1", "table", "th", "td", "strong", "code"):
            self.assertTrue(parsed.attributes(tag), tag)
        self.assertEqual(parsed.attributes("script"), [])
        self.assertEqual(parsed.attributes("img"), [])
        links = parsed.attributes("a")
        self.assertEqual([a["href"] for a in links if "href" in a], ["https://example.org"])
        self.assertFalse(any("data-file-browser-path" in a for a in links))
        self.assertIn("本地图", "".join(parsed.text))
        self.assertIn("远程图", "".join(parsed.text))
        self.assertTrue(self.render(source).attributes("table"), "file previews use the same table renderer")
        self.assertEqual(parsed.attributes("td")[-1]["class"], "markdown-align-right")
        self.assertFalse(any("style" in attrs for _, attrs in parsed.tags), "table alignment respects Admin CSP")

    def test_local_links_resolve_unicode_spaces_and_file_fragments(self) -> None:
        parsed = self.render('[一](../src/main.py#L5) [二](<参考 说明.md>) [三](./%E4%B8%AD%E6%96%87.md)')
        self.assertEqual(parsed.attributes("a"), [
            {"href": "#", "data-file-browser-path": "src/main.py"},
            {"href": "#", "data-file-browser-path": "docs/参考 说明.md"},
            {"href": "#", "data-file-browser-path": "docs/中文.md"},
        ])

    def test_only_explicit_safe_external_schemes_are_clickable(self) -> None:
        parsed = self.render('[HTTPS](https://example.org/a%20b?x=1&y=2#p) [HTTP](http://example.org) [邮件](mailto:hello@example.org)')
        links = parsed.attributes("a")
        self.assertEqual([link["href"] for link in links], [
            "https://example.org/a%20b?x=1&y=2#p", "http://example.org", "mailto:hello@example.org",
        ])
        for link in links:
            self.assertEqual(link["target"], "_blank")
            self.assertEqual(link["rel"], "noopener noreferrer")
            self.assertNotIn("data-file-browser-path", link)

    def test_encoded_filename_separators_are_preserved_for_links_and_images(self) -> None:
        parsed = self.render('[hash](a%23b.md#heading) [query](a%3Fb.md) '
            '![hash](../a%23b.png) ![query](../a%3Fb.png)')
        self.assertEqual(parsed.attributes("a"), [
            {"href": "#", "data-file-browser-path": "docs/a#b.md"},
            {"href": "#", "data-file-browser-path": "docs/a?b.md"},
        ])
        self.assertEqual(self.images, ["a#b.png", "a?b.png"])
        self.assertEqual([image["src"] for image in parsed.attributes("img")], [
            "/api/projects/p/files/image?path=a%23b.png",
            "/api/projects/p/files/image?path=a%3Fb.png",
        ])

    def test_directory_links_preserve_navigation_hints_and_root_targets(self) -> None:
        root = self.render('[文档](docs/) [当前](.)', path="README.md")
        self.assertEqual(root.attributes("a"), [
            {"href": "#", "data-file-browser-path": "docs", "data-file-browser-directory": "true"},
            {"href": "#", "data-file-browser-path": "", "data-file-browser-directory": "true"},
        ])
        nested = self.render('[上级](../) [上级无斜线](..) [当前](./) [文档](../docs/.)')
        self.assertEqual(nested.attributes("a"), [
            {"href": "#", "data-file-browser-path": "", "data-file-browser-directory": "true"},
            {"href": "#", "data-file-browser-path": "", "data-file-browser-directory": "true"},
            {"href": "#", "data-file-browser-path": "docs", "data-file-browser-directory": "true"},
            {"href": "#", "data-file-browser-path": "docs", "data-file-browser-directory": "true"},
        ])

    def test_directory_targets_cannot_cross_root_or_request_images(self) -> None:
        for destination, path in (("../", "README.md"), ("..", "README.md"), ("../../", "docs/README.md")):
            with self.subTest(destination=destination, path=path):
                parsed = self.render(f"[越界]({destination})", path=path)
                self.assertEqual(parsed.attributes("a"), [{}])
        parsed = self.render('![上级](../) ![目录](images/)')
        self.assertEqual(parsed.attributes("img"), [])
        self.assertEqual(self.images, [])

    def test_unsafe_or_ambiguous_urls_have_no_href_or_local_target(self) -> None:
        for destination in (
            "javascript:alert%281%29", "javaScript%3Aalert%281%29",
            "java%73cript%3Aalert%281%29", "javascript%253Aalert%281%29",
            "data:text/html;base64,WA==", "vbscript:msgbox%281%29", "file:///etc/passwd",
            "//example.org/a", "%2f%2fexample.org/a", "https:%5c%5cexample.org/a",
            "a%00.md", "a%0A.md", "a%7f.md", "a%C2%85.md", "../%ff.md",
            "../../private.txt", "%2e%2e/%2e%2e/private.txt", "../%252e%252e/private.txt",
            "../.git/config", "../%2egit/config", "../.GIT/config", "%5c..%5csecret",
            "/etc/passwd", "report.md?action=delete", "https:/missing-host", "#section",
        ):
            with self.subTest(destination=destination):
                parsed = self.render(f"[点击]({destination})")
                for link in parsed.attributes("a"):
                    self.assertNotIn("href", link)
                    self.assertNotIn("data-file-browser-path", link)

    def test_plain_urls_are_not_automatically_linkified(self) -> None:
        self.assertEqual(self.render("https://example.org").attributes("a"), [])

    def test_title_and_alt_are_attribute_escaped(self) -> None:
        parsed = self.render('[外链](https://example.org "&quot; onmouseover=&quot;bad")\n\n![&quot; onerror=&quot;bad <script>](../image.png "&quot; onload=&quot;bad")')
        link = parsed.attributes("a")[0]
        image = parsed.attributes("img")[0]
        self.assertEqual(link["title"], '" onmouseover="bad')
        self.assertNotIn("onmouseover", link)
        self.assertNotIn("onerror", image)
        self.assertNotIn("onload", image)
        self.assertEqual(image["alt"], '" onerror="bad <script>')
        self.assertEqual(image["title"], '" onload="bad')

    def test_local_images_use_authenticated_callback_and_lazy_loading(self) -> None:
        parsed = self.render('![说明](<../图片 集/a.png> "图片标题")')
        self.assertEqual(self.images, ["图片 集/a.png"])
        image = parsed.attributes("img")[0]
        self.assertEqual(image["src"], "/api/projects/p/files/image?path=%E5%9B%BE%E7%89%87%20%E9%9B%86%2Fa.png")
        self.assertEqual(image["loading"], "lazy")
        self.assertEqual(image["decoding"], "async")
        self.assertEqual(image["referrerpolicy"], "no-referrer")
        self.assertEqual(image["alt"], "说明")

    def test_remote_and_unsafe_images_never_call_endpoint_builder(self) -> None:
        for destination in ("https://example.org/pixel.gif", "//example.org/pixel.gif", "../../secret.png", "../.git/secret.png", "data:image/png;base64,WA==", "image.png?track=1"):
            with self.subTest(destination=destination):
                parsed = self.render(f"![图片说明]({destination})")
                self.assertEqual(parsed.attributes("img"), [])
                self.assertIn("图片说明", "".join(parsed.text))
        self.assertEqual(self.images, [])

    def test_nested_images_and_code_in_alt_remain_plain_text(self) -> None:
        parsed = self.render('![a ![inner](inner.png) `code` &amp;](outer.png)')
        self.assertEqual(self.images, ["docs/outer.png"])
        self.assertEqual(parsed.attributes("img")[0]["alt"], "a inner code &")

    def test_images_are_bounded_and_excess_keep_alt_text(self) -> None:
        parsed = self.render("\n\n".join(f"![第{i}张]({i}.png)" for i in range(MAX_MARKDOWN_IMAGES + 2)))
        self.assertEqual(len(parsed.attributes("img")), MAX_MARKDOWN_IMAGES)
        self.assertEqual(len(self.images), MAX_MARKDOWN_IMAGES)
        self.assertIn("第8张", "".join(parsed.text))
        self.assertIn("最多预览 8 张图片", "".join(parsed.text))

    def test_input_budget_is_measured_in_utf8_bytes(self) -> None:
        self.render("x" * MAX_MARKDOWN_BYTES)
        with self.assertRaisesRegex(PreviewUnavailable, "256 KiB"):
            self.render("x" * (MAX_MARKDOWN_BYTES + 1))
        with self.assertRaisesRegex(PreviewUnavailable, "256 KiB"):
            self.render("中" * (MAX_MARKDOWN_BYTES // 3 + 1))

    def test_token_budget_rejects_excessive_small_elements(self) -> None:
        with self.assertRaisesRegex(PreviewUnavailable, "过于复杂"):
            self.render("*x* " * (MAX_MARKDOWN_TOKENS // 3 + 1))

    def test_sparse_tables_stop_during_block_parsing_before_inline_expansion(self) -> None:
        from netizen_cli.admin.file_previews import _MARKDOWN

        table = "|".join(["h"] * 8) + "\n" + "|".join(["---"] * 8) + "\n"
        table += "x\n" * 8
        # A deliberately tiny budget proves early rejection with small input;
        # never reproduce the original large allocation to test its limit.
        with patch("netizen_cli.admin.file_previews.MAX_MARKDOWN_TOKENS", 100), \
             patch.object(_MARKDOWN.inline, "parse", wraps=_MARKDOWN.inline.parse) as inline:
            with self.assertRaisesRegex(PreviewUnavailable, "过于复杂"):
                render_markdown(table)
            inline.assert_not_called()

    def test_table_token_budget_is_shared_across_tables(self) -> None:
        table = "| a | b |\n| --- | --- |\n| x | y |\n"
        with patch("netizen_cli.admin.file_previews.MAX_MARKDOWN_TOKENS", 70):
            render_markdown(table)
            with self.assertRaisesRegex(PreviewUnavailable, "过于复杂"):
                render_markdown((table + "\n") * 4)

    def test_reference_link_expansion_is_bounded_before_rendering(self) -> None:
        text = "[a] " * 100 + "\n\n[a]: https://example.org/" + "x" * 20_000
        self.assertLess(len(text.encode()), MAX_MARKDOWN_BYTES)
        with self.assertRaisesRegex(PreviewUnavailable, "预览内容过大"):
            self.render(text)

    def test_invalid_document_path_or_encoding_fails_closed(self) -> None:
        for path in ("", "/README.md", "../README.md", "docs/../README.md", ".git/a.md", "docs\\a.md", "docs/\x00.md"):
            with self.subTest(path=path), self.assertRaises(PreviewUnavailable):
                self.render("text", path=path)
        with self.assertRaisesRegex(PreviewUnavailable, "文本编码"):
            self.render("\ud800")

    def test_deep_nesting_remains_bounded_and_inert(self) -> None:
        parsed = self.render("> " * 100 + '<script>alert(1)</script>')
        self.assertLessEqual(len(parsed.attributes("blockquote")), 20)
        self.assertEqual(parsed.attributes("script"), [])


if __name__ == "__main__":
    unittest.main()
