from __future__ import annotations

import unittest
from copy import deepcopy

from markdown_it import MarkdownIt

from netizen.markdown_images import (
    image_tokens,
    parse_markdown,
    render_markdown,
    replace_image_with_text,
)


class MarkdownImagesTest(unittest.TestCase):
    def assert_rewrite_preserves_semantics(self, source, *, rewrite_images=True):
        tokens, env = parse_markdown(source)
        images = tuple(image_tokens(tokens))
        if rewrite_images:
            for index, token in enumerate(images, 1):
                token.attrSet("src", f"img_uploaded_{index}")
        expected_sources = [token.attrGet("src") for token in images]
        html = MarkdownIt("commonmark")
        expected = html.renderer.render(deepcopy(tokens), html.options, env)

        rewritten = render_markdown(tokens, env)
        actual_tokens, actual_env = parse_markdown(rewritten)

        self.assertEqual(
            html.renderer.render(actual_tokens, html.options, actual_env), expected,
        )
        self.assertEqual(
            [token.attrGet("src") for token in image_tokens(actual_tokens)],
            expected_sources,
        )
        return rewritten

    def test_only_image_destination_changes_not_link_alt_title_or_surroundings(self):
        source = '**before** ![效果图](result.png "title") [普通链接](result.png) *after*'
        rewritten = self.assert_rewrite_preserves_semantics(source)
        self.assertIn('[普通链接](result.png)', rewritten)
        self.assertIn('![效果图](img_uploaded_1 "title")', rewritten)

    def test_code_escapes_raw_html_and_incomplete_syntax_are_not_images(self):
        for example in (
            "`![demo](a.png)`",
            "``![demo](a.png)``",
            "```md\n![demo](a.png)\n```",
            "~~~\n![demo](a.png)\n~~~",
            "    ![demo](a.png)",
            "\t![demo](a.png)",
            r"\![demo](a.png)",
            "<!-- ![demo](a.png) -->",
            "<!--\n![demo](a.png)\n-->",
            "<div>\n![demo](a.png)\n</div>",
            '<img src="a.png" alt="![demo](a.png)">',
            '<span data-demo="![demo](a.png)">text</span>',
            "![missing][unknown]",
            "![broken](a.png",
        ):
            with self.subTest(example=example):
                source = example + "\n\n![real](b.png)"
                tokens, _ = parse_markdown(source)
                self.assertEqual([t.attrGet("src") for t in image_tokens(tokens)], ["b.png"])
                self.assert_rewrite_preserves_semantics(source)

    def test_reference_images_do_not_change_a_shared_ordinary_link(self):
        source = (
            "![first][picture] / ![picture][] / ![picture] / [download][picture]\n\n"
            '[picture]: <assets/a b.png> "title"\n'
        )
        tokens, _ = parse_markdown(source)
        self.assertEqual(
            [t.attrGet("src") for t in image_tokens(tokens)], ["assets/a%20b.png"] * 3,
        )
        rewritten = self.assert_rewrite_preserves_semantics(source)
        rewritten_tokens, _ = parse_markdown(rewritten)
        links = [
            child.attrGet("href")
            for token in rewritten_tokens
            for child in token.children or ()
            if child.type == "link_open"
        ]
        self.assertEqual(links, ["assets/a%20b.png"])

    def test_local_file_urls_and_encoded_destinations_remain_parseable(self):
        for source, target in (
            ("![local](file:///tmp/a%20b.png)", "file:///tmp/a%20b.png"),
            ("![图](a(b(c)).png)", "a(b(c)).png"),
            (r'![图](a\(b\).png "title")', "a(b).png"),
            ('![图](<a b.png> "title")', "a%20b.png"),
            ('![图](a.png "a &quot;quote&quot; and \\\\path")', "a.png"),
            ("![图](a&amp;b.png)", "a&b.png"),
        ):
            with self.subTest(source=source):
                tokens, _ = parse_markdown(source)
                self.assertEqual([t.attrGet("src") for t in image_tokens(tokens)], [target])
                self.assert_rewrite_preserves_semantics(source)
        for target in (
            "file://host/tmp/a.png", "file://localhost/tmp/a.png",
            "file:///tmp/a.png?query", "file:///tmp/a.png#fragment",
            "javascript:alert(1)", "vbscript:example", "data:text/html,example",
        ):
            with self.subTest(target=target):
                tokens, _ = parse_markdown(f"![rejected]({target})")
                self.assertEqual(tuple(image_tokens(tokens)), ())

    def test_image_in_link_and_nested_alt_image_are_not_uploaded_separately(self):
        source = "[![outer ![inner](inner.png) **label**](outer.png)](https://example.com)"
        tokens, _ = parse_markdown(source)
        self.assertEqual([t.attrGet("src") for t in image_tokens(tokens)], ["outer.png"])
        self.assert_rewrite_preserves_semantics(source)

    def test_nested_lists_quotes_and_multiline_images_keep_their_structure(self):
        for source in (
            "intro\r\n\r\n> 1. first ![one](one.png)\r\n"
            ">    - second ![two](two.png)\r\n"
            ">      continuation ![three](three.png)\r\n\r\nend",
            '> before ![multi\r\n> line](<a b.png>\r\n> "title") after\r\n',
            "# ![one](a.png) #\r\r> lead\r![two](b.png)\r\r![three](c.png)\r===\r",
            "- paragraph\n \t![x](x.png)",
        ):
            with self.subTest(source=source):
                self.assert_rewrite_preserves_semantics(source)

    def test_gfm_table_escaped_pipes_strikethrough_and_task_lists_survive(self):
        source = (
            "| Picture | Status |\n| --- | --- |\n"
            r"| ![a\|b](a.png) | ~~old\|value~~ |" "\n\n"
            "- [x] done ![done](done.png)\n- [ ] pending\n"
        )
        tokens, _ = parse_markdown(source)
        self.assertEqual([t.attrGet("src") for t in image_tokens(tokens)], ["a.png", "done.png"])
        self.assert_rewrite_preserves_semantics(source)

    def test_feishu_font_and_at_markup_are_preserved(self):
        source = (
            "<font color='green'>ready</font>\n\n"
            '<at id="ou_user">person</at> ![result](result.png)\n\n'
            "<font color='red'>remaining</font>"
        )
        rewritten = self.assert_rewrite_preserves_semantics(source)
        self.assertIn("<font color='green'>ready</font>", rewritten)
        self.assertIn('<at id="ou_user">person</at>', rewritten)
        self.assertIn("<font color='red'>remaining</font>", rewritten)

    def test_alt_entities_escaped_punctuation_and_formatting_keep_their_meaning(self):
        for source in (
            "![a &amp; b &lt;script&gt; &quot;quote&quot;](a.png)",
            r"![escaped \*star\* \[label\] \\](a.png)",
            "![**bold** *italic* `code` &amp; [link](x)](a.png)",
        ):
            with self.subTest(source=source):
                self.assert_rewrite_preserves_semantics(source)

    def test_literal_entities_and_backslashes_in_attributes_are_not_decoded_twice(self):
        for source in (
            r'[download](report&amp;amp;.pdf "literal \\*star and &amp;quot;")',
            '![remote](https://example.com/x?literal=&amp;amp; "&amp;lt;")',
            '<https://example.com/x?literal=&amp;amp;>',
            '[download][ref]\n\n[ref]: report&amp;quot;.pdf "literal &amp;lt;"',
        ):
            with self.subTest(source=source):
                self.assert_rewrite_preserves_semantics(
                    "![local](img_uploaded_1)\n\n" + source, rewrite_images=False,
                )

    def test_replacing_image_with_text_cannot_inject_markup_or_another_image(self):
        tokens, env = parse_markdown(
            '[![**label** &lt;script&gt;](missing.png "title")](https://example.com)'
        )
        (token,) = image_tokens(tokens)
        replace_image_with_text(token, 'missing ![injected](other.png) <script> & [link](https://evil.example)')
        html = MarkdownIt("commonmark")
        expected = html.renderer.render(deepcopy(tokens), html.options, env)

        rewritten = render_markdown(tokens, env)
        actual_tokens, actual_env = parse_markdown(rewritten)

        self.assertEqual(tuple(image_tokens(actual_tokens)), ())
        actual = html.renderer.render(actual_tokens, html.options, actual_env)
        self.assertEqual(actual, expected)
        self.assertIn("missing", actual)
        self.assertIn("&lt;script&gt;", actual)
        self.assertEqual(actual.count("<a "), 1)
        self.assertNotIn("<script>", actual)
        self.assertNotIn("<strong>", actual)


if __name__ == "__main__":
    unittest.main()
