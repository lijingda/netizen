import json
import unittest
from dataclasses import replace

from netizen_cli.process_messages import (
    CONTENT_LIMIT, ActivitySnapshot, SnapshotContent, build_snapshot_content, snapshot_revision,
)


def encoded(content, activity_id="turn"):
    return json.dumps({
        "type": "ACTIVITY_SNAPSHOT", "messageId": activity_id, "activityType": "PLAN",
        "content": {"title": content.title, "rows": content.rows}, "replace": True,
    }, ensure_ascii=False, separators=(",", ":"))


class SnapshotBudget(unittest.TestCase):
    def test_exact_boundary_round_trips_unicode_and_escaped_rows(self):
        for unit in ("字", "🍎", '"', "\\", "\n", "\x00"):
            with self.subTest(unit=unit):
                size = CONTENT_LIMIT - len(encoded(SnapshotContent("标题", ("",))))
                unit_size = len(json.dumps(unit, ensure_ascii=False)) - 2
                exact = unit * (size // unit_size) + "甲" * (size % unit_size)
                for suffix in ("", "溢"):
                    built = build_snapshot_content("turn", "标题", (exact + suffix,))
                    wire = encoded(built)
                    self.assertLessEqual(len(wire), CONTENT_LIMIT)
                    self.assertEqual(json.loads(wire)["content"]["rows"], list(built.rows))
                    if not suffix:
                        self.assertEqual(len(wire), CONTENT_LIMIT)
                        self.assertEqual(built.rows, (exact,))
                    else:
                        self.assertTrue(built.rows[0].endswith("…"))
                        self.assertTrue(exact.startswith(built.rows[0][:-1]))

    def test_removes_oldest_before_shortening_any_text(self):
        newest = ("二" * 500, "三" * 200)
        content = build_snapshot_content("turn", "标题", ("一" * 1500, *newest),
                                         state_line="状态", terminal_lines=("终态", "错误"))
        self.assertEqual(content, SnapshotContent("标题", ("状态", *newest, "终态", "错误")))

    def test_shortens_last_row_only_after_dropping_older_rows(self):
        content = build_snapshot_content("turn", "标题", ("old", "new" * 900),
                                         state_line="状态", terminal_lines=("失败",))
        state, newest, terminal = content.rows
        self.assertEqual((state, terminal), ("状态", "失败"))
        self.assertTrue(newest.startswith("new"))
        self.assertTrue(newest.endswith("…"))
        self.assertEqual(len(encoded(content)), CONTENT_LIMIT)

    def test_long_required_fields_survive_and_displace_ordinary_rows(self):
        rows = ("ordinary",)
        args = dict(state_line='"' * 2500, terminal_lines=("错" * 3000, "短"))
        content = build_snapshot_content("turn", "题" * 3000, rows, **args)
        state, error, short = content.rows
        for value in (content.title, state, error):
            self.assertTrue(value.endswith("…"))
        self.assertEqual(short, "短")
        self.assertLessEqual(len(encoded(content)), CONTENT_LIMIT)
        self.assertEqual(content, build_snapshot_content("turn", "题" * 3000, rows, **args))

    def test_budget_accounts_for_identity_and_minimum_structure(self):
        long_id = "id" * 500
        content = build_snapshot_content(long_id, "标题", ("字" * 3000,))
        self.assertEqual(len(encoded(content, long_id)), CONTENT_LIMIT)
        with self.assertRaises(ValueError):
            build_snapshot_content("x" * CONTENT_LIMIT, "", ())


class SnapshotTime(unittest.TestCase):
    def test_replay_and_revisions_across_clock_changes(self):
        content = SnapshotContent("标题", ("甲",))
        first = snapshot_revision("turn", content, now_seconds=1_800_000_000)
        self.assertEqual(first, ActivitySnapshot("turn", content, 1_800_000_000))
        for now in (1_800_000_020, 1_799_999_990):
            self.assertIs(snapshot_revision("turn", content, now_seconds=now, previous=first), first)
        second = snapshot_revision("turn", replace(content, rows=("乙",)),
                                   now_seconds=1_799_999_990, previous=first)
        self.assertEqual(second.timestamp, first.timestamp + 1)
        third = snapshot_revision("turn", content, now_seconds=1_800_000_070, previous=second)
        self.assertEqual(third.timestamp, 1_800_000_070)
        self.assertEqual(snapshot_revision("other", content, now_seconds=1_800_000_000).timestamp,
                         first.timestamp)
        with self.assertRaises(ValueError):
            snapshot_revision("other", content, now_seconds=third.timestamp, previous=third)

    def test_rejects_impossible_seconds_and_overflow(self):
        content = SnapshotContent("", ())
        for invalid in (True, 999_999_999, 10_000_000_000, 1_800_000_000.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                snapshot_revision("turn", content, now_seconds=invalid)
        last = snapshot_revision("turn", content, now_seconds=9_999_999_999)
        self.assertIs(snapshot_revision("turn", content, now_seconds=9_999_999_999, previous=last), last)
        with self.assertRaises(ValueError):
            snapshot_revision("turn", replace(content, title="变"), now_seconds=last.timestamp, previous=last)
