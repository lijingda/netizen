from __future__ import annotations

import unittest
from copy import deepcopy

from netizen_cli.cards.callbacks import CardActionError
from netizen_cli.cards.chat_target import (
    CHAT_ID_HELP_URL,
    CHAT_SEARCH_LIMIT,
    ChatSearchSnapshot,
    ChatSearchView,
    ChatTargetDraft,
    chat_options,
    chat_target_elements,
    decode_chat_snapshot,
    encode_chat_snapshot,
    initial_chat_target,
    read_chat_target,
    resolve_chat_target,
)
from netizen_cli.management.chat_directory import AvailableChat


class ChatTargetCardsTest(unittest.TestCase):
    fields = {"mode_field": "mode", "choice_field": "group", "id_field": "id"}

    def read(self, **form):
        return read_chat_target(form, **self.fields)

    def resolve(self, **form):
        return resolve_chat_target(self.read(**form), current_chat_id="oc_current")

    def test_only_selected_field_determines_target_even_with_invalid_inactive_value(self):
        for inactive in ("oc_residual", "not/a/chat", "x" * 1000, [], False, None):
            with self.subTest(mode="current", inactive=inactive):
                self.assertEqual(self.resolve(mode="current", group=inactive, id=inactive), "oc_current")
            with self.subTest(mode="group", inactive=inactive):
                self.assertEqual(self.resolve(mode="group", group="oc_group", id=inactive), "oc_group")
            with self.subTest(mode="id", inactive=inactive):
                self.assertEqual(self.resolve(mode="id", group=inactive, id="  oc_direct  "), "oc_direct")

    def test_only_current_mode_defaults_current_and_empty_inputs_never_fall_back(self):
        self.assertEqual(self.resolve(mode="current"), "oc_current")
        for blank in ("", "  ", None):
            with self.subTest(blank=blank), self.assertRaises(CardActionError):
                self.resolve(mode="id", group="oc_group", id=blank)
        with self.assertRaises(CardActionError):
            self.resolve(mode="id", group="oc_group")
        for blank in ("", "  ", None):
            with self.assertRaises(CardActionError):
                self.resolve(mode="group", group=blank, id="oc_residual")
        with self.assertRaisesRegex(CardActionError, "请选择飞书群聊"):
            self.resolve(mode="group", id="oc_residual")

    def test_unknown_modes_and_malformed_selected_inputs_are_rejected(self):
        for mode in ("choice", "both", "", None, [], False):
            with self.subTest(mode=mode), self.assertRaises(CardActionError):
                self.resolve(mode=mode, group="oc_group", id="oc_direct")
        for value in ([], {}, False, 42, "bad/id", "id\nvalue", "x" * 193):
            for mode in ("group", "id"):
                with self.subTest(mode=mode, value=value), self.assertRaises(CardActionError):
                    self.resolve(mode=mode, group=value, id=value)

    def test_search_can_read_text_drafts_before_target_validation(self):
        draft = self.read(mode="id", group="oc_group", id="unfinished/id")
        self.assertEqual(draft, ChatTargetDraft("id", "oc_group", "unfinished/id"))
        with self.assertRaises(CardActionError):
            resolve_chat_target(draft, current_chat_id="oc_current")

    def test_pure_group_options_reuse_avatars_and_disambiguate_only_duplicates(self):
        first = AvailableChat("oc_one_12345678", "研发群", "group", False)
        other = AvailableChat("oc_two_87654321", "研发群", "group", True)
        options = chat_options((first, other, first), avatar_keys={first.chat_id: "img_group"})
        self.assertEqual([item["value"] for item in options], [first.chat_id, other.chat_id])
        self.assertEqual([item["text"]["content"] for item in options],
                         ["研发群 · 12345678", "研发群 · 87654321 · 外部群"])
        self.assertEqual(options[0]["icon"], {"tag": "custom_icon", "img_key": "img_group"})
        self.assertEqual(options[1]["icon"], {"tag": "standard_icon", "token": "group_outlined"})
        self.assertEqual(chat_options(()), [])

    def test_mode_is_native_single_select_both_target_inputs_optional_and_no_callbacks(self):
        fields = chat_target_elements(**self.fields, draft=ChatTargetDraft("id", "", "oc_direct"), options=[])
        by_name = {item["name"]: item for item in fields if "name" in item}
        self.assertEqual(by_name["mode"]["tag"], "select_static")
        self.assertEqual(by_name["mode"]["initial_option"], "id")
        self.assertEqual([option["value"] for option in by_name["mode"]["options"]], ["current", "group", "id"])
        self.assertTrue(by_name["mode"]["required"])
        self.assertFalse(by_name["group"]["required"])
        self.assertFalse(by_name["id"]["required"])
        self.assertNotIn("initial_option", by_name["group"])
        self.assertEqual(by_name["id"]["default_value"], "oc_direct")
        self.assertTrue(all("behaviors" not in item for item in fields))
        self.assertIn(CHAT_ID_HELP_URL, str(fields))

    def test_initial_mode_preserves_saved_chat_even_without_display_metadata(self):
        options = chat_options((AvailableChat("oc_group", "群", "group", False),))
        for chat_id, expected in (
            (None, ChatTargetDraft()), ("oc_current", ChatTargetDraft()),
            ("oc_group", ChatTargetDraft("group", "oc_group")),
            ("oc_direct", ChatTargetDraft("id", chat_id="oc_direct")),
        ):
            with self.subTest(chat_id=chat_id):
                draft = initial_chat_target(current_chat_id="oc_current", target_chat_id=chat_id, options=options)
                self.assertEqual(draft, expected)
                self.assertEqual(resolve_chat_target(draft, current_chat_id="oc_current"), chat_id or "oc_current")

    def test_explicit_search_precedes_result_select_and_pagination(self):
        options = chat_options((AvailableChat("oc_group", "研发群", "group", False),))
        search = {"tag": "button", "name": "search"}
        page = {"tag": "button", "name": "page"}
        fields = chat_target_elements(
            **self.fields, draft=ChatTargetDraft(choice="oc_group"), options=options,
            search=ChatSearchView("query", "新关键词", "研发", search, page),
        )
        names = [item["name"] for item in fields if "name" in item]
        self.assertEqual(names, ["mode", "query", "search", "group", "page", "id"])
        query = next(item for item in fields if item.get("name") == "query")
        self.assertEqual(query["default_value"], "新关键词")
        self.assertFalse(query["required"])
        choice = next(item for item in fields if item.get("name") == "group")
        self.assertEqual(choice["initial_option"], "oc_group")
        self.assertNotIn("过滤", str(fields))
        self.assertIn("关键词「研发」", str(fields))
        self.assertIn("已选：研发群", str(fields))

    def test_initial_view_only_shows_retained_target_and_empty_search_has_no_empty_select(self):
        options = chat_options((AvailableChat("oc_retained", "原目标", "group", False),
                                AvailableChat("oc_other", "未查找的群", "group", False)))
        search = ChatSearchView("query", "", None, {"tag": "button", "name": "search"})
        fields = chat_target_elements(**self.fields, draft=ChatTargetDraft(choice="oc_retained"),
                                      options=options, search=search)
        choice = next(item for item in fields if item.get("name") == "group")
        self.assertEqual([item["value"] for item in choice["options"]], ["oc_retained"])
        for applied_query in (None, "没有匹配"):
            with self.subTest(applied_query=applied_query):
                fields = chat_target_elements(
                    **self.fields, draft=ChatTargetDraft(), options=[],
                    search=ChatSearchView("query", "", applied_query, search.search_button),
                )
                self.assertFalse(any(item.get("name") == "group" for item in fields))
                self.assertIn("没有找到可选群聊" if applied_query is not None else "请先查找群聊", str(fields))

    def test_complete_search_snapshot_round_trip_preserves_order_and_icons_without_urls(self):
        groups = tuple(AvailableChat(f"oc_{index}", f"研发 {index}", "group", index == 1,
                                    "https://p3-lark-file.byteimg.com/avatar") for index in range(23))
        snapshot = ChatSearchSnapshot("研发", groups, {"oc_0": "img_first", "oc_22": "img_last"}, "目录提示")
        encoded = encode_chat_snapshot(snapshot)
        self.assertNotIn("https://", str(encoded))
        restored = decode_chat_snapshot(encoded)
        self.assertEqual(restored.query, "研发")
        self.assertEqual(restored.total_pages, 3)
        self.assertEqual(restored.avatar_keys, snapshot.avatar_keys)
        self.assertEqual(restored.notice, "目录提示")
        self.assertEqual([chat.chat_id for page in range(3) for chat in restored.page_chats(page)],
                         [chat.chat_id for chat in groups])
        self.assertEqual(len(restored.page_chats(2)), 3)
        self.assertTrue(restored.chats[1].external)
        # Directory metadata may have an empty/whitespace display name. This
        # is not an invalid identity, and initial display and replay must agree.
        for name in ("", "  "):
            unnamed = ChatSearchSnapshot("", (AvailableChat("oc_unnamed", name, None, None),), {})
            replay = decode_chat_snapshot(encode_chat_snapshot(unnamed))
            self.assertEqual(chat_options(replay.chats)[0]["text"]["content"], "oc_unnamed")

    def test_snapshot_rejects_malformed_or_oversized_callback_display_data(self):
        valid = encode_chat_snapshot(ChatSearchSnapshot("", (AvailableChat("oc_group", "群", None, None),), {}))
        mutations = (
            {"v": True}, {"v": 2}, {"query": " dirty "}, {"query": "a" * 51},
            {"notice": "x" * 2001}, {"chats": valid["chats"] * 2},
            {"chats": [{"id": f"oc_{i}", "name": "群"} for i in range(CHAT_SEARCH_LIMIT + 1)]},
            {"chats": [{"id": "oc_bad/id", "name": "群"}]},
            {"chats": [{"id": "oc_group", "name": "群", "avatar": "https://example.com"}]},
            {"chats": [{"id": "oc_group", "name": "群", "external": 1}]},
        )
        for patch in mutations:
            with self.subTest(patch=patch), self.assertRaises(CardActionError):
                decode_chat_snapshot({**deepcopy(valid), **patch})

    def test_empty_snapshot_is_complete_and_has_no_result_options(self):
        restored = decode_chat_snapshot(encode_chat_snapshot(ChatSearchSnapshot("空", (), {})))
        self.assertEqual((restored.total_pages, restored.page_chats(0)), (1, ()))
