import json
import unittest
from dataclasses import FrozenInstanceError
from itertools import permutations

from netizen_cli.process_messages import (
    CONTENT_LIMIT, ITEM_LIMIT, ITEM_TEXT_LIMIT, StepItem, TextItem, ToolItem, build_items, finish_items, item_events,
)


T = 1_800_000_000


class ItemProjection(unittest.TestCase):
    def test_first_observation_and_first_finish_are_stable_including_clock_rollback(self):
        item = ToolItem("tool", "安全操作", T)
        self.assertEqual(item.state, "running")
        first = item_events((item,))
        self.assertEqual([e["event_type"] for e in first], ["TOOL_CALL_START"])
        for finish_at in (T - 1, T, T + 2):
            done = item.finish(finish_at)
            self.assertEqual(done.finished_at, max(T, finish_at))
            self.assertEqual(done.state, "finished")
            self.assertIs(done.finish(T + 200), done)
            self.assertEqual(item_events((done,))[0], first[0])
            self.assertEqual(item_events((done,)), item_events((done.finish(T + 200),)))
        with self.assertRaises(FrozenInstanceError):
            item.text = "其他"

    def test_tiebreak_is_stable_across_input_order_and_preserves_pairs_and_triples(self):
        items = (ToolItem("t", "读", T, T), StepItem("s", "核查", T, T),
                 TextItem("c", "记录", T, T))
        expected = ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
                    "STEP_STARTED", "STEP_FINISHED", "TOOL_CALL_START", "TOOL_CALL_END"]
        baseline = item_events(items)
        self.assertEqual([e["event_type"] for e in baseline], expected)
        for order in permutations(items):
            self.assertEqual(item_events(order), baseline)

    def test_cap_keeps_all_running_and_most_recently_finished_items(self):
        running = (ToolItem("running", "运行", T - 1), StepItem("step", "进行中", T - 5))
        finished = tuple(ToolItem(str(i), str(i), T - i, T + i) for i in range(ITEM_LIMIT + 20))
        selected = build_items((*finished, *running))
        self.assertEqual(len(selected), ITEM_LIMIT)
        self.assertEqual(set(selected), {*running, *finished[-(ITEM_LIMIT - 2):]})
        self.assertEqual(selected, build_items((*running, *reversed(finished))))
        final = finish_items((*running, *finished), T + 2)
        self.assertEqual({i.item_id for i in final}, {i.item_id for i in selected})
        self.assertTrue(all(i.state == "finished" for i in final))
        self.assertEqual(finish_items(final, T + 200), final)
        self.assertEqual(len(item_events(final)), 2 * ITEM_LIMIT)

    def test_full_running_list_has_no_finished_slots(self):
        running = tuple(ToolItem(str(i), "工具", T) for i in range(ITEM_LIMIT))
        self.assertEqual(set(build_items((*running, TextItem("text", "进展", T, T)))), set(running))
        with self.assertRaises(ValueError):
            build_items((running[0], running[0].finish(T)))

    def test_running_overflow_keeps_oldest_and_admits_next_when_a_slot_opens(self):
        # Items 59 and 60 straddle the window at the same second.
        running = [ToolItem(f"tool-{i:02}", "运行", T + (i + 1) // 2) for i in range(62)]
        self.assertEqual(build_items(running[::-1]), tuple(running[:60]))
        final = finish_items(running[::-1], T + 100)
        self.assertEqual(final, tuple(item.finish(T + 100) for item in running[:60]))

        running[0] = running[0].finish(T + 100)
        self.assertEqual(build_items(running[::-1]), tuple(running[1:61]))

    def test_window_rolls_between_writes_while_an_old_tool_waits_for_its_end(self):
        running = ToolItem("old-tool", "运行", T)
        items = [TextItem(f"text-{i}", "进展", T + i, T + i) for i in range(59)]
        for first in range(4):
            window = build_items([running, *items])
            self.assertEqual({i.item_id for i in window},
                             {"old-tool", *(f"text-{i}" for i in range(first, first + 59))})
            items.append(TextItem(f"text-{first + 59}", "新进展", T + first + 59, T + first + 59))
        final = item_events([running.finish(T + 100), *items])
        self.assertEqual([e["event_type"] for e in final if "toolCallId" in json.loads(e["content"])],
                         ["TOOL_CALL_START", "TOOL_CALL_END"])

    def test_clips_visible_text_to_300_including_ellipsis(self):
        for char in ("字", "🍎", '"', "\\", "\n"):
            for size in (299, 300, 301, 3000):
                text = char * size
                items = (ToolItem("tool", text, T, T), StepItem("step", text, T, T), TextItem("text", text, T, T))
                wire = [json.loads(e["content"]) for e in item_events(items)]
                labels = [value for e in wire for field, value in e.items() if field in ("toolCallName", "stepName", "delta")]
                label = text if size <= ITEM_TEXT_LIMIT else text[:299] + "…"
                self.assertEqual(labels, [label] * 4)

    def test_per_event_budget_includes_escaping_and_longer_step_end(self):
        for make in (lambda: ToolItem("id", "\x00" * 300, T),
                     lambda: StepItem("step", "\x00" * 300, T)):
            item = make()
            earlier, = item_events((item,))
            later = item_events((item.finish(T + 1),))
            self.assertEqual(earlier, later[0])
            for e in later:
                self.assertLessEqual(len(e["content"]), CONTENT_LIMIT)
            if isinstance(item, StepItem):
                self.assertEqual(json.loads(earlier["content"])["stepName"],
                                 json.loads(later[-1]["content"])["stepName"])
        empty = item_events((TextItem("id", "", T, T),))[1]["content"]
        escaped, remainder = divmod(CONTENT_LIMIT - len(empty), 6)
        boundary = "\x00" * escaped + "甲" * remainder
        for text, expected in ((boundary, boundary), (boundary + "乙", boundary[:-1] + "…")):
            content = item_events((TextItem("id", text, T, T),))[1]["content"]
            self.assertLessEqual(len(content), CONTENT_LIMIT)
            self.assertEqual(json.loads(content)["delta"], expected)

    def test_10_digit_seconds_and_ordering_are_validated(self):
        for bad in (True, T + 0.1, 999_999_999, 10_000_000_000):
            with self.assertRaises(ValueError):
                StepItem("x", "步骤", bad)
            with self.assertRaises(ValueError):
                StepItem("x", "步骤", T).finish(bad)
        for start, content, end in ((T + 1, T, T + 2), (T, T + 2, T + 1), (T + 1, T, T)):
            with self.assertRaises(ValueError):
                TextItem("x", "文字", start, end, content_at=content)
        with self.assertRaises(ValueError):
            TextItem("x", "文字", T, None)
        with self.assertRaises(ValueError):
            StepItem("", "步骤", T)
        with self.assertRaises(ValueError):
            ToolItem("tool", {"raw": "payload"}, T)
