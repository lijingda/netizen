from __future__ import annotations

import re
import unittest

from lark_channel import OutboundCard

from netizen_cli.account_rate_limits import (
    AccountCredits,
    AccountMonthlyCreditLimit,
    AccountRateLimitBucket,
    AccountRateLimitsSnapshot,
    AccountRateLimitWindow,
)
from netizen_cli.cards.usage import account_usage_card


def _text(*buckets: AccountRateLimitBucket) -> str:
    card = account_usage_card(AccountRateLimitsSnapshot(buckets))
    return card.card["body"]["elements"][0]["content"]


def _window(used: int = 25, duration: int | None = 300, resets: int | None = None):
    return AccountRateLimitWindow(used, duration, resets)


class AccountUsageCardTest(unittest.TestCase):
    def test_uses_one_card_markdown_with_client_local_date_time_and_timezone(self):
        resets = 1791600000
        card = account_usage_card(AccountRateLimitsSnapshot((
            AccountRateLimitBucket("codex", "Codex", _window(resets=resets), None),
        )))
        self.assertIsInstance(card, OutboundCard)
        self.assertEqual(card.card["schema"], "2.0")
        elements = card.card["body"]["elements"]
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["tag"], "markdown")
        content = elements[0]["content"]
        for format_type in ("date_num", "time", "timezone"):
            self.assertIn(
                f"<local_datetime millisecond='{resets * 1000}' format_type='{format_type}'>",
                content,
            )
        self.assertNotIn("UTC", content)
        self.assertIn("共享账号", content)

    def test_window_periods_use_actual_duration_with_native_tolerance(self):
        for duration, label in (
            (300, "5 小时"), (285, "5 小时"), (315, "5 小时"),
            (1440, "每日"), (10080, "每周"), (43200, "每月"), (525600, "每年"),
        ):
            with self.subTest(duration=duration):
                for primary in (True, False):
                    window = _window(duration=duration)
                    text = _text(AccountRateLimitBucket(
                        None, None, window if primary else None, None if primary else window,
                    ))
                    self.assertIn(label, text)
        text = _text(AccountRateLimitBucket("codex", "Codex", _window(duration=284), _window(duration=None)))
        self.assertIn("主额度", text)
        self.assertIn("次额度", text)
        self.assertNotIn("5 小时", text)

    def test_bars_and_percentages_show_remaining_and_clamp_to_display_range(self):
        for used, remaining, filled in ((0, 100, 20), (25, 75, 15), (37, 63, 13), (100, 0, 0), (110, 0, 0)):
            with self.subTest(used=used):
                text = _text(AccountRateLimitBucket("codex", None, _window(used=used), None))
                bar, = re.findall(r"\[([█░]+)\]", text)
                self.assertEqual(len(bar), 20)
                self.assertEqual(bar.count("█"), filled)
                self.assertIn(f"剩余 {remaining}%", text)

    def test_buckets_keep_their_names_and_codex_precedes_model_quotas(self):
        for limit_id, limit_name in (("codex", "Codex quota"), ("shared-id", "Codex")):
            with self.subTest(limit_id=limit_id, limit_name=limit_name):
                text = _text(
                    AccountRateLimitBucket("model-id", "Model Name", _window(), None),
                    AccountRateLimitBucket(limit_id, limit_name, _window(), None),
                    AccountRateLimitBucket("unknown-model-id", None, None, _window()),
                    AccountRateLimitBucket("metadata-only", "Hidden Metadata", None, None),
                )
                self.assertLess(text.index(limit_name), text.index("Model Name"))
                self.assertIn("unknown-model-id", text)
                self.assertNotIn("Hidden Metadata", text)
                self.assertNotIn("metadata-only", text)

    def test_missing_and_unrepresentable_reset_times_never_get_default_tags(self):
        for resets in (None, 10**30):
            with self.subTest(resets=resets):
                text = _text(AccountRateLimitBucket("codex", None, _window(resets=resets), None))
                self.assertNotIn("local_datetime", text)
        text = _text(AccountRateLimitBucket("codex", None, _window(resets=0), None))
        self.assertEqual(text.count("millisecond='0'"), 3)

    def test_names_cannot_inject_card_markup_mentions_or_links(self):
        text = _text(AccountRateLimitBucket(
            "model", '<at id="all">`[link](https://example.com)`</at>', _window(), None,
        ))
        self.assertNotIn("<at", text)
        self.assertNotIn("[link]", text)
        self.assertIn("&lt;at", text)
        self.assertIn("\\[link\\]", text)

    def test_credits_distinguish_finite_hidden_and_unlimited(self):
        for credits, expected in (
            (AccountCredits(True, False, 1234), "1,234 credits"),
            (AccountCredits(True, False, None), "可用"),
            (AccountCredits(False, True, None), "无限"),
        ):
            with self.subTest(credits=credits):
                text = _text(AccountRateLimitBucket("codex", None, None, None, credits=credits))
                self.assertIn(expected, text)
                self.assertNotIn("%", text)
                self.assertNotIn("暂不可用", text)
                self.assertNotIn("local_datetime", text)
        text = _text(AccountRateLimitBucket("metadata", None, None, None))
        self.assertNotIn("100%", text)
        self.assertNotIn("metadata", text)
        self.assertIn("暂不可用", text)

    def test_monthly_limit_renders_reported_remaining_and_amounts_even_without_windows(self):
        for remaining, expected in ((43, 43), (-5, 0), (110, 100)):
            with self.subTest(remaining=remaining):
                text = _text(AccountRateLimitBucket(
                    "codex", None, None, None,
                    monthly_limit=AccountMonthlyCreditLimit(10000, 1500, remaining, 0),
                ))
                self.assertIn(f"剩余 {expected}%", text)
                self.assertIn("1,500 / 10,000", text)
                self.assertEqual(text.count("local_datetime millisecond='0'"), 3)
                self.assertNotIn("85%", text)


if __name__ == "__main__":
    unittest.main()
