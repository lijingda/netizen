from __future__ import annotations

import json
import unittest

from netizen_cli.cards.scheduled import (
    SCHEDULE_CARD_JSON_LIMIT_BYTES,
    schedule_trigger_instructions,
)
from tests.support.channel_cards import elements


class ScheduleTriggerInstructionsTest(unittest.TestCase):
    def test_short_content_is_literal_without_preview_or_fold(self):
        for content in ("检查资源", "汉" * 120, "甲\n\n乙", "甲\r\n乙\r\n丙", "😀" * 120):
            with self.subTest(content=content):
                body = schedule_trigger_instructions(content)
                self.assertEqual(elements(body, "collapsible_panel"), [])
                self.assertEqual(elements(body, "plain_text"), [
                    {"tag": "plain_text", "content": content},
                ])

    def test_preview_stops_at_first_limit_preserving_original_whitespace(self):
        cases = (
            ("汉" * 121, "汉" * 120),
            ("😀" * 121, "😀" * 120),
            ("甲\n\n  乙\n丙", "甲\n\n  乙"),
            ("甲\r\n\r\n  乙\r\n丙", "甲\r\n\r\n  乙"),
            ("甲\r乙\r丙\r丁", "甲\r乙\r丙"),
            ("甲\n\n" + "乙" * 118, "甲\n\n" + "乙" * 117),
            ("甲" * 119 + "\r\n乙", "甲" * 119),
        )
        for content, preview in cases:
            with self.subTest(content=content):
                body = schedule_trigger_instructions(content)
                self.assertEqual(body[0]["text"]["content"], "内容预览\n" + preview + "…")
                panel, = elements(body, "collapsible_panel")
                self.assertFalse(panel["expanded"])
                self.assertEqual(panel["header"]["title"]["content"], "查看完整任务内容")
                self.assertEqual(panel["elements"][0]["text"]["content"], content)
                # The preview remains outside the panel during native expansion.
                self.assertNotIn(body[0], panel["elements"])

    def test_expanded_content_uses_ellipsis_only_when_over_limit(self):
        for length in (2999, 3000, 3001, 32000):
            with self.subTest(length=length):
                content = "首行\n\n" + "汉" * (length - 4)
                panel, = elements(schedule_trigger_instructions(content), "collapsible_panel")
                self.assertEqual(panel["elements"][0]["text"]["content"],
                                 content[:3000] + ("…" if length > 3000 else ""))

    def test_markup_remains_plain_text_and_bounded_for_multibyte_and_escaped_content(self):
        for sample in ("汉", "😀", "\x01", '"\\', '<at id="all">所有人</at>\n**literal**'):
            with self.subTest(sample=sample):
                content = sample * 3001
                body = schedule_trigger_instructions(content)
                self.assertEqual(elements(body, "markdown"), [])
                self.assertEqual(elements(body, "button"), [])
                panel, = elements(body, "collapsible_panel")
                self.assertEqual(panel["elements"][0]["text"], {
                    "tag": "plain_text", "content": content[:3000] + "…",
                })
                # Even worst-case JSON escaping leaves ample space for the
                # bounded title, trigger metadata, and footer on the full card.
                self.assertLess(len(json.dumps(body, ensure_ascii=False).encode()),
                                SCHEDULE_CARD_JSON_LIMIT_BYTES - 5000)
