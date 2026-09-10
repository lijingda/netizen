from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.documentation_links import local_link_errors


class DocumentationLinksTest(unittest.TestCase):
    def test_heading_links_use_rendered_text_and_github_duplicate_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text(
                "# 使用 `run_task()` 与 **Codex**！\n"
                "## Files & [Results](https://example.com)\n"
                "## Files & Results\n"
                "## Files & Results-1\n"
                "## Files & Results\n"
                "部署说明\n--------\n"
                "[代码标题](#使用-run_task-与-codex)\n"
                "[first](#files--results) [duplicate](#files--results-1)\n"
                "[collision](#files--results-1-1) [third](#files--results-2)\n"
                "[setext](#%E9%83%A8%E7%BD%B2%E8%AF%B4%E6%98%8E)\n",
                encoding="utf-8",
            )

            self.assertEqual(local_link_errors(root, [guide]), [])

    def test_markdown_and_html_images_links_and_custom_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            (root / "product demo.svg").write_text("<svg/>", encoding="utf-8")
            guide.write_text(
                '<a name="legacy"></a>\n'
                '![演示](<product demo.svg> "产品演示")\n'
                '<img\n    src="product%20demo.svg"\n    alt="demo">\n'
                '<picture><source srcset="product%20demo.svg 1x, product%20demo.svg 2x"></picture>\n'
                '<a href="#legacy">保留的入口</a>\n'
                "[demo][asset]\n[asset]: <product demo.svg>\n"
                "[missing](#gone) ![missing image](missing.svg)\n"
                '<img src="missing-html.svg">\n',
                encoding="utf-8",
            )

            errors = local_link_errors(root, [guide])
            self.assertEqual(len(errors), 3)
            self.assertTrue(any("#gone: missing heading" in error for error in errors))
            self.assertTrue(any("missing.svg: missing file" in error for error in errors))
            self.assertTrue(any("missing-html.svg: missing file" in error for error in errors))

    def test_fenced_examples_inline_code_and_comments_are_inert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text(
                "~~~markdown\n# Fake heading\n[example](missing.md)\n~~~\n"
                "```markdown\n## Another fake\n![example](missing.svg)\n```\n"
                "`[literal](missing.md)` <!-- [comment](missing.md) -->\n"
                "[real navigation](#fake-heading)\n",
                encoding="utf-8",
            )

            errors = local_link_errors(root, [guide])
            self.assertEqual(len(errors), 1)
            self.assertIn("#fake-heading: missing heading", errors[0])

    def test_package_links_allow_parent_segments_but_reject_escaping_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            references = skill / "references"
            references.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
            (root / "repository.md").write_text("# Outside\n", encoding="utf-8")
            guide = references / "guide.md"
            guide.write_text(
                "[合法包内跳转](../SKILL.md#skill)\n"
                "[仓库依赖](../../repository.md#outside)\n",
                encoding="utf-8",
            )

            errors = local_link_errors(skill, skill.rglob("*.md"))
            self.assertEqual(len(errors), 1)
            self.assertIn("../../repository.md#outside: escapes documentation root", errors[0])
