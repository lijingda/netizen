import unittest

from netizen_cli.file_citations import citation_code, citation_label, rewrite_file_citations


CITATION = ':codex-file-citation{path="/tmp/report.pdf"}'


class FileCitationsTest(unittest.TestCase):
    def test_grammar_and_attribute_values(self):
        for colons in (":", "::", ":::"):
            raw = colons + """codex-file-citation{ \tpath \t= "/a}b" key-2='c d' _3 =e=f\t}"""
            seen = []
            self.assertEqual(rewrite_file_citations(raw, lambda item: seen.append(item) or "ok"), "ok")
            citation, = seen
            self.assertEqual(citation.raw, raw)
            self.assertEqual(citation.path, "/a}b")
            self.assertEqual(citation.attributes, {"path": "/a}b", "key-2": "c d", "_3": "e=f"})

    def test_attribute_immediately_after_a_closing_quote(self):
        for raw, key, expected in (
            (':codex-file-citation{path="x"next=y}', "next", "y"),
            (':codex-file-citation{path="/a/x.pdf"purpose="source"}', "purpose", "source"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(rewrite_file_citations(raw, lambda item: item.attributes[key]), expected)

    def test_rejected_candidates(self):
        for raw in (
            "::::codex-file-citation{path=x}", ":xcodex-file-citation{path=x}",
            ":codex-file-citation-x{path=x}", ":codex-file-citation[label]{path=x}",
            ":codex-file-citation {path=x}", ":codex-file-citation{path=x path=x}",
            ':codex-file-citation{path=""}', ":codex-file-citation{Path=x}",
            ":codex-file-citation{}", ":codex-file-citation{path=}",
            ':codex-file-citation{path="missing}', ":codex-file-citation{path='one\ntwo'}",
            ':codex-file-citation{path="one\rtwo"}',
            ':codex-file-citation{path=x\n}', ':codex-file-citation{bad.key=x path=y}',
            "&#58;codex-file-citation{path=x}",
        ):
            with self.subTest(raw=raw):
                self.assertIs(rewrite_file_citations(raw, citation_label), raw)

    def test_literal_then_backslash_quoting(self):
        for raw, expected in (
            (r''':codex-file-citation{path="C:\repo\" purpose="source"}''', "C:\\repo\\"),
            (r''':codex-file-citation{path="/a\"b"}''', '/a"b'),
            (r""":codex-file-citation{path='/tmp/team\'s files'}""", "/tmp/team's files"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(rewrite_file_citations(raw, lambda item: item.path), expected)

    def test_escapes_and_colon_runs(self):
        for colons in (1, 2, 3, 4):
            for count in range(5):
                with self.subTest(colons=colons, count=count):
                    prefix = "\\" * count
                    raw = ":" * (colons - 1) + CITATION
                    self.assertEqual(rewrite_file_citations(prefix + raw, citation_label),
                                     prefix + ("report.pdf" if colons <= 3 and count % 2 == 0 else raw))
        source = "\\" + CITATION + " " + CITATION
        self.assertEqual(rewrite_file_citations(source, citation_label), "\\" + CITATION + " report.pdf")

    def test_length_limit_and_many_candidates(self):
        empty = ":codex-file-citation{path=}"
        for length in (4095, 4096, 4097):
            raw = empty[:-1] + "猫" * (length - len(empty)) + "}"
            with self.subTest(length=length):
                self.assertEqual(rewrite_file_citations(raw, lambda _: "!"), "!" if length <= 4096 else raw)
        malformed = ":codex-file-citation{path=" * 4096
        self.assertIs(rewrite_file_citations(malformed, citation_label), malformed)
        self.assertEqual(rewrite_file_citations((CITATION + " ") * 512, citation_label), "report.pdf " * 512)

    def test_literal_markdown(self):
        raw = ":codex-file-citation{path=/tmp/report.pdf}"
        for context in (
            "~~~md\n{}\n~~~\n", "    {}\n", "text `{}`", "text ``{}``",
            "inline <span title='{}'>x</span>", "<!-- {} -->", "<div>\n{}\n</div>\n",
            "[{}](https://example.org)", "[{}](file:///tmp/other)", "[link]({})",
            "![{}](x.png)", "![alt]({})", "[link][ref]\n\n[ref]: {}",
            "[{}][ref]\n\n[ref]: /tmp/a", "<https://example.org/{}>",
            "visit https://example.org/{}",
        ):
            with self.subTest(context=context):
                source = context.format(raw)
                self.assertIs(rewrite_file_citations(source, citation_label), source)

    def test_prose_offsets_and_markdown_significant_path(self):
        raw = ':codex-file-citation{path="/tmp/__init__*b*.py"}'
        for context in (
            "前\t{}  \r\n", "## {} ##\n", "- {}\n", "> {}\n", "*{}*", "~~{}~~",
            "| x | y |\n|---|---|\n| {} | z |", "\\[&amp;{}\\*&#x1F642;",
        ):
            with self.subTest(context=context):
                self.assertEqual(rewrite_file_citations(context.format(raw), citation_label),
                                 context.format("__init__*b*.py"))

    def test_ambiguity_is_per_spelling(self):
        other = ":codex-file-citation{path=/tmp/other}"
        text = f"{CITATION}\n\n`{CITATION}`\r\n\r\n{other}  "
        self.assertEqual(rewrite_file_citations(text, citation_label), text.replace(other, "other"))
        # A candidate starting inside code can extend beyond its closing backtick.
        # Its nested prose spelling also occurs in code, so neither copy can move.
        text = f'`:codex-file-citation{{path="` {other} tail"}} then `{other}`'
        self.assertIs(rewrite_file_citations(text, citation_label), text)

    def test_table_pipe_escaping_keeps_distinct_source_spellings(self):
        plain = ':codex-file-citation{path="/tmp/one|two"}'
        escaped = plain.replace("|", r"\|")
        text = f"`{plain}`\n\n| header |\n| --- |\n| {escaped} |"
        self.assertEqual(rewrite_file_citations(text, lambda _: "ok"), text.replace(escaped, "ok"))

    def test_no_edits_return_original_object(self):
        for text in ("original\r\n", f"`{CITATION}`", "codex-file-citation"):
            self.assertIs(rewrite_file_citations(text, citation_label), text)
        self.assertIs(rewrite_file_citations(CITATION, lambda item: item.raw), CITATION)

    def test_labels(self):
        for attrs, expected in (
            ('path="/abs/报告.pdf" purpose=output unknown=9', "报告.pdf"),
            ('path="/abs/x" page_number=4', "x · 第 4 页"),
            ('path="/abs/x" sheet="Revenue Model" range=C27', "x · Revenue Model!C27"),
            ('path="/abs/x" sheet=Q1', "x · Q1"), ('path="/abs/x" range=A1:B2', "x · A1:B2"),
            ('path="/abs/x" slide_number=1 slide_id=id label="ARR chart"', "x · 第 1 张幻灯片 · ARR chart"),
            ('path="/tmp/reports/"', "/tmp/reports/"),
            ('path="/tmp/ab\x00\x07c" label="re\x7fport"', "abc · report"),
            ('path="/tmp/non\u00a0breaking"', "non\u00a0breaking"),
            ('path=/tmp/' + "长" * 201, "长" * 199 + "…"),
        ):
            with self.subTest(attrs=attrs):
                self.assertEqual(rewrite_file_citations(f":codex-file-citation{{{attrs}}}", citation_label), expected)
        self.assertEqual(rewrite_file_citations(':codex-file-citation{path="/tmp/report`s.pdf"}', citation_code),
                         "`report's.pdf`")
        self.assertEqual(rewrite_file_citations(':codex-file-citation{path="/tmp/reports/"}', citation_code),
                         "`reports/`")
