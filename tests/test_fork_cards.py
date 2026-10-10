from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from lark_channel import CardActionEvent, Events, FeishuChannel
from lark_channel.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from netizen_cli.cards.callbacks import CardActionError
from netizen_cli.cards.chat_target import ChatSearchSnapshot, encode_chat_snapshot
from netizen_cli.cards.fork import (
    ForkSource,
    ForkCardCapacityError,
    ForkFormValidationError,
    decode_fork_action,
    fork_form_card,
    fork_status_card,
    is_fork_card_action,
)
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management.chat_directory import AvailableChat
from tests.support.channel_cards import callback, elements, form_values


def tagged_elements(value):
    if isinstance(value, dict):
        return int("tag" in value) + sum(tagged_elements(item) for item in value.values())
    if isinstance(value, list):
        return sum(tagged_elements(item) for item in value)
    return 0


class ForkCardsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = FeishuScope("app", "oc_source", ScopeKind.TOPIC, "omt_source")
        self.source = ForkSource("binding-1", "native-thread-1", 2, 3, 4, 5)
        self.display = {"source_title": "设计方案", "project_alias": "work"}
        self.chat = AvailableChat("oc_target_12345678", "研发群", "group", False)
        self.current_chat = AvailableChat(self.scope.chat_id, "当前研发群", "group", False)

    def form(self, **kwargs):
        return fork_form_card(self.scope, self.source, **self.display, **kwargs)

    def snapshot(self, count=23, *, query="研发", chats=None, avatar_keys=None):
        groups = chats if chats is not None else (
            self.chat, *(replace(self.chat, chat_id=f"oc_group_{index:08}", name=f"研发群 {index}")
                         for index in range(1, count)),
        )
        return ChatSearchSnapshot(query, tuple(groups), avatar_keys or {})

    @staticmethod
    def choice(card):
        return next(item for item in elements(card.card, "select_static") if item["name"] == "fork_target_v5")

    def decode(self, card, label="创建分支", **changes):
        return decode_fork_action(self.scope, callback(card, label), {**form_values(card), **changes})

    def test_initial_card_has_name_and_current_target_in_one_submit(self):
        card = self.form(chats=(self.chat,))
        self.assertEqual(len(elements(card.card, "form")), 1)
        self.assertEqual(len(elements(card.card, "button")), 2)
        self.assertEqual(set(form_values(card)), {
            "fork_name_v5", "fork_mode_v5", "fork_chat_id_v5", "fork_query_v5",
        })
        action = self.decode(card)
        self.assertEqual(action.action, "create")
        self.assertEqual(action.target_chat_id, self.scope.chat_id)
        self.assertEqual(action.name, "设计方案 · 分支")
        self.assertEqual(action.source, self.source)
        self.assertTrue(card.card["config"]["update_multi"])
        self.assertTrue(is_fork_card_action(callback(card, "创建分支"), form_values(card)))
        self.assertIn("在所选聊天中新建分支话题", str(card.card))
        self.assertFalse(any(item["name"] == "fork_target_v5" for item in elements(card.card, "select_static")))
        changed = self.decode(card, fork_mode_v5="group", fork_target_v5=self.chat.chat_id, fork_name_v5="方案 B")
        self.assertEqual((changed.target_chat_id, changed.name), (self.chat.chat_id, "方案 B"))

    def test_search_results_only_show_returned_chats(self):
        card = self.form(chat_snapshot=self.snapshot(chats=(self.chat, self.current_chat)))
        self.assertEqual([item["value"] for item in self.choice(card)["options"]],
                         [self.chat.chat_id, self.scope.chat_id])
        missing = self.form(target_chat_id=self.chat.chat_id)
        self.assertFalse(any(item["name"] == "fork_target_v5" for item in elements(missing.card, "select_static")))
        self.assertEqual(form_values(missing)["fork_mode_v5"], "id")
        self.assertEqual(self.decode(missing).target_chat_id, self.chat.chat_id)
        card = self.form(chats=(self.chat,), target_chat_id=self.chat.chat_id)
        self.assertEqual(self.decode(card).target_chat_id, self.chat.chat_id)

    def test_mode_selects_one_input_and_ignores_inactive_residual_values(self):
        card = self.form(chat_snapshot=self.snapshot(chats=(self.chat,)))
        mode = next(item for item in elements(card.card, "select_static") if item["name"] == "fork_mode_v5")
        self.assertEqual(mode["initial_option"], "current")
        self.assertNotIn("behaviors", mode)
        self.assertFalse(self.choice(card)["required"])
        for inactive in ("bad/id", "x" * 500, [], None):
            with self.subTest(inactive=inactive):
                current = self.decode(card, fork_target_v5=inactive, fork_chat_id_v5=inactive)
                self.assertEqual(current.target_chat_id, self.scope.chat_id)
                selected = self.decode(card, fork_mode_v5="group", fork_target_v5=self.chat.chat_id, fork_chat_id_v5=inactive)
                self.assertEqual(selected.target_chat_id, self.chat.chat_id)
                direct = self.decode(card, fork_mode_v5="id", fork_chat_id_v5="  oc_direct  ",
                                     fork_target_v5=inactive)
                self.assertEqual(direct.target_chat_id, "oc_direct")
        with self.assertRaises(ForkFormValidationError):
            self.decode(card, fork_mode_v5="id", fork_chat_id_v5=" ", fork_target_v5=self.chat.chat_id)
        for mode in (None, "choice", [], "both"):
            with self.subTest(mode=mode), self.assertRaises(CardActionError):
                self.decode(card, fork_mode_v5=mode)

    def test_empty_choice_and_invalid_id_preserve_complete_draft_without_cross_fallback(self):
        card = self.form(chat_snapshot=self.snapshot(chats=(self.chat,)), name=" 已填名称 ")
        for changes in (
            {"fork_mode_v5": "group", "fork_target_v5": "", "fork_chat_id_v5": "oc_direct"},
            {"fork_mode_v5": "id", "fork_chat_id_v5": "bad/id"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ForkFormValidationError) as caught:
                    self.decode(card, **changes)
                draft = caught.exception.draft
                self.assertIsNone(draft.target_chat_id)
                self.assertEqual(draft.name, " 已填名称 ")
                self.assertEqual(draft.query, "研发")
                self.assertEqual(draft.target_id, changes["fork_chat_id_v5"])
                restored = self.form(chats=(self.chat,), name=draft.name, query=draft.query,
                                     target_mode=draft.target_mode, target_choice=draft.target_choice,
                                     target_id=draft.target_id)
                restored_draft = self.decode(restored, "查找群聊")
                self.assertEqual(restored_draft.name, draft.name)
                self.assertEqual(restored_draft.target_id, draft.target_id)
                self.assertEqual(restored_draft.target_choice, "")
                self.assertTrue(restored_draft.search_requested)
                self.assertFalse(draft.search_requested)

    def test_id_mode_search_keeps_both_inputs_and_accepts_unfinished_target(self):
        snapshot = self.snapshot()
        card = self.form(chat_snapshot=snapshot, target_mode="id", target_choice=self.chat.chat_id,
                         target_id="unfinished/id")
        action = self.decode(card, "跳转", fork_page_v5="2")
        self.assertIsNone(action.target_chat_id)
        self.assertEqual(action.target_mode, "id")
        self.assertEqual(action.target_choice, self.chat.chat_id)
        self.assertEqual(action.target_id, "unfinished/id")
        self.assertEqual(action.chat_page, 2)
        self.assertEqual(action.chat_snapshot.query, snapshot.query)
        self.assertFalse(action.search_requested)
        action = self.decode(card, "查找群聊", fork_mode_v5="group", fork_target_v5="")
        self.assertEqual(action.target_choice, "")
        self.assertEqual(action.target_id, "unfinished/id")

    def test_only_duplicate_names_receive_short_identity_labels(self):
        other = AvailableChat("oc_other_87654321", self.chat.name, None, True)
        card = self.form(chat_snapshot=self.snapshot(chats=(self.current_chat, self.chat, other)))
        options = self.choice(card)["options"]
        self.assertEqual(options[0]["text"]["content"], self.current_chat.name)
        self.assertEqual(options[1]["text"]["content"], "研发群 · 12345678")
        self.assertEqual(options[2]["text"]["content"], "研发群 · 87654321 · 外部群")
        self.assertTrue(all(option["text"]["tag"] == "plain_text" for option in options))

    def test_avatars_follow_exact_chat_ids_and_missing_images_use_group_icon(self):
        other = AvailableChat("oc_other_87654321", self.chat.name, None, True)
        plain = self.form(chat_snapshot=self.snapshot(chats=(self.current_chat, self.chat, other)))
        card = self.form(chat_snapshot=self.snapshot(
            chats=(self.current_chat, self.chat, other),
            avatar_keys={self.scope.chat_id: "img_current", self.chat.chat_id: "img_target",
                         "oc_unrelated": "img_unrelated"},
        ))
        options = self.choice(card)["options"]
        self.assertEqual(options[0]["icon"], {"tag": "custom_icon", "img_key": "img_current"})
        self.assertEqual(options[1]["icon"], {"tag": "custom_icon", "img_key": "img_target"})
        self.assertEqual(options[2]["icon"], {"tag": "standard_icon", "token": "group_outlined"})
        self.assertEqual(form_values(card), form_values(plain))
        self.assertEqual(callback(card, "创建分支")["source"], callback(plain, "创建分支")["source"])
        for item in self.choice(plain)["options"]:
            self.assertEqual(item["icon"], {"tag": "standard_icon", "token": "group_outlined"})
        self.assertNotIn("img_unrelated", str(card.card))

    def test_direct_and_unresolved_topic_current_chat_are_not_mislabeled_group(self):
        for kind in ScopeKind:
            with self.subTest(kind=kind):
                scope = FeishuScope("app", "oc_source", kind, "omt_source" if kind is ScopeKind.TOPIC else None)
                card = fork_form_card(scope, self.source, **self.display)
                option = next(item for item in elements(card.card, "select_static")
                              if item["name"] == "fork_mode_v5")["options"][0]
                self.assertEqual(option["text"]["content"], "当前聊天")
                self.assertNotIn("icon", option)
                self.assertEqual(decode_fork_action(scope, callback(card, "创建分支"), form_values(card)).target_chat_id,
                                 scope.chat_id)

    def test_shared_context_and_group_visibility_notice_are_always_present(self):
        for card in (self.form(), self.form(chats=(self.chat,), target_chat_id=self.chat.chat_id)):
            self.assertIn("目标聊天的参与者", str(card.card))
            self.assertIn("后续回答可能引用", str(card.card))
            self.assertIn("共享项目文件", str(card.card))
            self.assertIn("不按操作者的群成员身份筛选", str(card.card))

    def test_search_clears_target_and_page_keeps_applied_query_separate_from_input(self):
        snapshot = self.snapshot()
        card = self.form(chat_snapshot=snapshot, target_chat_id=self.chat.chat_id, name="  草稿\n B  ")
        search = self.decode(card, "查找群聊", fork_query_v5="  产品  ")
        self.assertEqual((search.action, search.query, search.chat_snapshot), ("results", "产品", None))
        self.assertEqual((search.target_choice, search.name), ("", "  草稿\n B  "))
        self.assertIsNone(search.target_chat_id)
        self.assertEqual(search.source, self.source)
        next_page = self.decode(card, "跳转", fork_page_v5="2")
        self.assertEqual((next_page.query, next_page.chat_page), ("研发", 2))
        changed_query = self.decode(card, "跳转", fork_query_v5="新关键词", fork_page_v5="1")
        self.assertEqual((changed_query.query, changed_query.chat_page), ("研发", 1))
        self.assertEqual(changed_query.query_input, "新关键词")
        self.assertEqual(changed_query.target_choice, self.chat.chat_id)
        self.assertFalse(changed_query.search_requested)
        self.assertEqual(encode_chat_snapshot(changed_query.chat_snapshot), encode_chat_snapshot(snapshot))
        blank_query = self.decode(card, "查找群聊", fork_query_v5="  ", fork_name_v5="")
        self.assertEqual((blank_query.query, blank_query.chat_snapshot, blank_query.name), ("", None, ""))
        self.assertEqual(self.decode(card).name, "草稿 B")
        self.assertEqual(self.decode(card).query, "研发")
        self.assertIsNone(self.decode(card).chat_snapshot)
        recreated = self.form(chat_snapshot=self.snapshot(chats=(self.chat,), query=search.query),
                              target_mode=search.target_mode, target_choice=search.target_choice, name=search.name)
        self.assertEqual(form_values(recreated)["fork_name_v5"], "  草稿\n B  ")
        self.assertEqual(form_values(recreated)["fork_target_v5"], "")
        recreated_page = self.form(chat_snapshot=changed_query.chat_snapshot, chat_page=changed_query.chat_page,
                                   target_mode=changed_query.target_mode,
                                   target_choice=changed_query.target_choice,
                                   query=changed_query.query, query_input=changed_query.query_input)
        self.assertEqual(form_values(recreated_page)["fork_query_v5"], "新关键词")
        self.assertEqual(callback(recreated_page, "跳转")["snapshot"]["query"], "研发")

    def test_empty_directory_still_keeps_current_chat_creation_and_search(self):
        card = self.form(notice="部分群暂不可用。")
        self.assertFalse(any(item["name"] == "fork_target_v5" for item in elements(card.card, "select_static")))
        self.assertEqual(self.decode(card).target_chat_id, self.scope.chat_id)
        self.assertIn("部分群暂不可用", str(card.card))
        self.assertFalse(any(button["text"]["content"] == "跳转" for button in elements(card.card, "button")))
        self.assertEqual(self.decode(card, "查找群聊").query, "")

    def test_new_empty_search_does_not_restore_current_chat_as_a_selected_target(self):
        initial = self.form()
        search = self.decode(initial, "查找群聊", fork_query_v5="没有匹配", fork_mode_v5="group")
        card = self.form(chat_snapshot=self.snapshot(chats=(), query=search.query),
                         target_choice=search.target_choice,
                         target_mode=search.target_mode,
                         target_chat_id=self.scope.chat_id)
        self.assertFalse(any(item["name"] == "fork_target_v5" for item in elements(card.card, "select_static")))
        self.assertIn("没有找到可选群聊", str(card.card))
        with self.assertRaises(ForkFormValidationError) as caught:
            self.decode(card)
        self.assertEqual(caught.exception.draft.target_choice, "")
        self.assertFalse(caught.exception.draft.search_requested)

    def test_redisplayed_form_renews_transport_nonce_for_navigation_and_create(self):
        first = self.form(chat_snapshot=self.snapshot())
        second = self.form(chat_snapshot=self.snapshot())
        for label in ("查找群聊", "跳转", "创建分支"):
            self.assertNotEqual(callback(first, label)["nonce"], callback(second, label)["nonce"])
        self.assertEqual(callback(first, "创建分支")["source"], callback(second, "创建分支")["source"])
        self.assertEqual(form_values(first), form_values(second))
        for button in elements(first.card, "button"):
            self.assertEqual(button["form_action_type"], "submit")
            self.assertEqual(button["behaviors"][0]["type"], "callback")
        # Empty name is a valid search draft, so only creation validates it.
        name_input = next(item for item in elements(first.card, "input") if item["name"] == "fork_name_v5")
        self.assertFalse(name_input.get("required", False))

    def test_name_limits_and_search_draft_validation(self):
        card = fork_form_card(self.scope, self.source, source_title="长" * 120, project_alias="work")
        name = self.decode(card).name
        self.assertEqual(len(name), 120)
        self.assertTrue(name.endswith(" · 分支"))
        for name in ("", "  "):
            with self.subTest(name=name), self.assertRaises(CardActionError):
                self.decode(card, fork_name_v5=name)
            self.assertEqual(self.decode(card, "查找群聊", fork_name_v5=name).name, name)
        for name in ("长" * 121, "bad\x00name", 42, False, []):
            for label in ("创建分支", "查找群聊"):
                with self.subTest(name=name, label=label), self.assertRaises(CardActionError):
                    self.decode(card, label, fork_name_v5=name)

    def test_native_omitted_or_null_optional_controls_are_unfinished_drafts(self):
        card = self.form()
        for form in (
            {"fork_mode_v5": "group"},
            {"fork_mode_v5": "group", "fork_name_v5": None, "fork_query_v5": None,
             "fork_target_v5": None, "fork_chat_id_v5": None},
        ):
            with self.subTest(form=form):
                search = decode_fork_action(self.scope, callback(card, "查找群聊"), form)
                self.assertEqual((search.name, search.query, search.target_choice, search.target_id),
                                 ("", "", "", ""))
                with self.assertRaises(ForkFormValidationError) as caught:
                    decode_fork_action(self.scope, callback(card, "创建分支"), form)
                self.assertEqual(caught.exception.draft.name, "")
                self.assertFalse(caught.exception.draft.search_requested)
                # With a destination selected, the empty name is likewise a
                # recoverable creation error, never a failed search callback.
                with self.assertRaisesRegex(ForkFormValidationError, "名称不能为空"):
                    decode_fork_action(self.scope, callback(card, "创建分支"),
                                       {**form, "fork_target_v5": self.scope.chat_id})

    def test_query_validation_does_not_replace_target_selection(self):
        card = self.form(chats=(self.chat,))
        for query in ("群" * 51, "研\n发", "\t研发", "\x7f", "\x85", False, []):
            with self.subTest(query=query), self.assertRaises(CardActionError):
                self.decode(card, "查找群聊", fork_query_v5=query)
        self.assertEqual(self.decode(card, "查找群聊", fork_query_v5=None).query, "")
        action = self.decode(card, fork_query_v5=self.chat.chat_id)
        self.assertEqual(action.target_chat_id, self.scope.chat_id)
        for target in ("", "not/a/chat", False, [], None, "x" * 193):
            with self.subTest(target=target), self.assertRaises(CardActionError):
                self.decode(card, fork_mode_v5="group", fork_target_v5=target)

    def test_wrong_scope_and_malformed_exact_source_are_rejected(self):
        card = self.form()
        value = callback(card, "创建分支")
        other_scope = FeishuScope("app", self.scope.chat_id, ScopeKind.TOPIC, "omt_other")
        with self.assertRaises(CardActionError):
            decode_fork_action(other_scope, value, form_values(card))
        changes = [
            lambda state: state.update(v=True),
            lambda state: state.update(v=99),
            lambda state: state.update(scope=[]),
            lambda state: state.update(unexpected=True),
            lambda state: state.update(action=[]),
            lambda state: state.update(action="select"),
            lambda state: state.update(source=None),
            lambda state: state["source"].update(binding_id="bad/id"),
            lambda state: state["source"].update(native_thread_id=None),
            lambda state: state["source"].update(settings_revision=0),
            lambda state: state["source"].update(context_revision=True),
            lambda state: state["source"].update(feedback_revision="4"),
            lambda state: state["source"].update(project_revision=-1),
            lambda state: state["source"].update(unexpected=True),
            lambda state: state["source"].pop("project_revision"),
            lambda state: state.update(target_chat_id=self.chat.chat_id),
            lambda state: state.update(nonce="invalid"),
        ]
        for change in changes:
            state = copy.deepcopy(value)
            change(state)
            with self.subTest(state=state), self.assertRaises(CardActionError):
                decode_fork_action(self.scope, state, form_values(card))
        changed = replace(self.source, settings_revision=6)
        card = fork_form_card(self.scope, changed, **self.display)
        self.assertEqual(self.decode(card).source, changed)

    def test_strict_callback_and_form_shapes(self):
        card = self.form()
        form = form_values(card)
        value = callback(card, "创建分支")
        forms = [None, [], {}, {**form, "new_project": "project"},
                 {"fork_target_v5": self.scope.chat_id}, {"fork_name_v5": "name"}]
        for malformed in forms:
            with self.subTest(form=malformed), self.assertRaises(CardActionError):
                decode_fork_action(self.scope, value, malformed)
        for malformed in (None, {}, [], "encoded", {"kind": "netizen_fork"}):
            with self.subTest(value=malformed), self.assertRaises(CardActionError):
                decode_fork_action(self.scope, malformed, form)
        no_query = decode_fork_action(self.scope, callback(card, "查找群聊"),
                                    {key: item for key, item in form.items() if key != "fork_query_v5"})
        self.assertEqual(no_query.query, "")
        for version in (1, 4):
            with self.assertRaisesRegex(CardActionError, "已过期"):
                decode_fork_action(self.scope, {**value, "v": version}, form)
        self.assertFalse(is_fork_card_action({}, {"new_project": "anything"}))

    def test_page_rejects_invalid_snapshot_page_and_transport_nonce(self):
        card = self.form(chat_snapshot=self.snapshot())
        value = callback(card, "跳转")
        invalid = [
            {**value, "nonce": "bad"}, {**value, "nonce": None},
            {**value, "snapshot": None}, {**value, "snapshot": {}},
            {**value, "snapshot": {**value["snapshot"], "query": " 研发 "}},
            {**value, "page_token": "old-cursor"},
            {**value, "unexpected": True},
        ]
        for item in invalid:
            with self.subTest(value=item), self.assertRaises(CardActionError):
                decode_fork_action(self.scope, item, form_values(card))
        for page in (None, 1, True, [], "-1", "3", "01", " 1", "１"):
            with self.subTest(page=page), self.assertRaises(CardActionError):
                self.decode(card, "跳转", fork_page_v5=page)

    def test_large_directory_keeps_every_option_within_json_and_element_limits(self):
        chats = tuple(AvailableChat(f"oc_group_{index:08}", "长群名" * 33 + str(index), None, False)
                      for index in range(81))
        snapshot = self.snapshot(chats=chats, avatar_keys={
            chat.chat_id: "img_v3_" + str(index) for index, chat in enumerate(chats)
        })
        card = self.form(chat_snapshot=snapshot, target_choice="", notice="列表较多。", name="长" * 120)
        self.assertEqual([item["value"] for item in self.choice(card)["options"]],
                         [chat.chat_id for chat in chats[:10]])
        self.assertEqual(callback(card, "跳转")["snapshot"], encode_chat_snapshot(snapshot))
        carrying = [button for button in elements(card.card, "button")
                    if "snapshot" in button["behaviors"][0]["value"]]
        self.assertEqual(len(carrying), 1)
        self.assertLessEqual(len(json.dumps(card.card, ensure_ascii=False).encode("utf-8")), 55_000)
        self.assertLessEqual(tagged_elements(card.card), 200)
        for item, chat in zip(self.choice(card)["options"], chats):
            self.assertEqual(item["text"]["content"], chat.name)
        oversized = self.snapshot(chats=tuple(replace(chat, name="长" * 1000) for chat in chats))
        with self.assertRaisesRegex(ForkCardCapacityError, "完整群聊结果"):
            self.form(chat_snapshot=oversized, target_choice="")

    def test_arbitrary_pages_keep_snapshot_selection_and_single_form(self):
        snapshot = self.snapshot()
        selected = snapshot.chats[1].chat_id
        card = self.form(chat_snapshot=snapshot, target_mode="group", target_choice=selected, name="分支草稿")
        for page in (2, 1, 0, 2):
            action = self.decode(card, "跳转", fork_page_v5=str(page))
            self.assertEqual(action.chat_page, page)
            self.assertFalse(action.search_requested)
            self.assertEqual(action.target_choice, selected)
            self.assertEqual(encode_chat_snapshot(action.chat_snapshot), encode_chat_snapshot(snapshot))
            card = self.form(chat_snapshot=action.chat_snapshot, chat_page=action.chat_page,
                             target_mode=action.target_mode,
                             target_choice=action.target_choice, name=action.name)
            expected = [chat.chat_id for chat in snapshot.page_chats(page)]
            if selected not in expected:
                expected.append(selected)
            self.assertEqual([option["value"] for option in self.choice(card)["options"]], expected)
            self.assertEqual(len(elements(card.card, "form")), 1)
            self.assertEqual(form_values(card)["fork_page_v5"], str(page))
            self.assertEqual(self.decode(card).target_chat_id, selected)
            self.assertEqual(self.decode(card).name, "分支草稿")

    def test_single_and_empty_snapshots_have_no_pagination_or_payload(self):
        for chats in ((), (self.chat,)):
            with self.subTest(chats=chats):
                card = self.form(chat_snapshot=self.snapshot(chats=chats), target_choice="")
                self.assertNotIn("fork_page_v5", form_values(card))
                self.assertEqual(len(elements(card.card, "button")), 2)
                self.assertFalse(any("snapshot" in button["behaviors"][0]["value"]
                                     for button in elements(card.card, "button")))
                search = self.decode(card, "查找群聊", fork_query_v5="新查询")
                self.assertEqual(search.query, "新查询")
                self.assertTrue(search.search_requested)

    def test_capacity_checks_later_pages_and_largest_cross_page_selection_upfront(self):
        snapshot = self.snapshot(chats=(
            *(replace(self.chat, chat_id=f"oc_short_{index}", name=f"短 {index}") for index in range(10)),
            replace(self.chat, chat_id="oc_long", name="长" * 1000),
        ))
        first = self.form(chat_snapshot=snapshot, target_choice="")
        first_size = len(json.dumps(first.card, ensure_ascii=False).encode("utf-8"))
        with patch("netizen_cli.cards.fork.TURN_FILE_CARD_JSON_LIMIT_BYTES", first_size + 1):
            with self.assertRaisesRegex(ForkCardCapacityError, "容量"):
                self.form(chat_snapshot=snapshot, target_choice="")

    def test_single_page_capacity_includes_selecting_its_longest_name(self):
        snapshot = self.snapshot(chats=(replace(self.chat, name="长" * 1000),))
        unselected = self.form(chat_snapshot=snapshot, target_choice="")
        size = len(json.dumps(unselected.card, ensure_ascii=False).encode("utf-8"))
        with patch("netizen_cli.cards.fork.TURN_FILE_CARD_JSON_LIMIT_BYTES", size + 1):
            with self.assertRaisesRegex(ForkCardCapacityError, "容量"):
                self.form(chat_snapshot=snapshot, target_choice="")

    def test_same_named_groups_on_different_pages_keep_disambiguation(self):
        snapshot = self.snapshot(chats=tuple(
            replace(self.chat, chat_id=f"oc_duplicate_{index:08}", name="研发群") for index in range(11)
        ))
        first = self.form(chat_snapshot=snapshot, target_choice="")
        last = self.form(chat_snapshot=snapshot, chat_page=1, target_mode="group", target_choice=snapshot.chats[0].chat_id)
        first_option = self.choice(first)["options"][0]
        self.assertEqual(first_option["text"]["content"], "研发群 · 00000000")
        self.assertEqual(self.choice(last)["options"][-1], first_option)

    def test_long_group_names_keep_searchable_suffixes(self):
        chat = replace(self.chat, name="项目" * 100 + "特别关键词")
        card = self.form(chat_snapshot=self.snapshot(chats=(chat,)))
        self.assertEqual(self.choice(card)["options"][0]["text"]["content"], chat.name)

    def test_actual_json_capacity_rejects_oversize_without_losing_options(self):
        snapshot = self.snapshot(chats=(self.chat,), avatar_keys={self.chat.chat_id: "x"})
        card = self.form(chat_snapshot=snapshot, target_mode="group", target_choice=self.chat.chat_id)
        base = len(json.dumps(card.card, ensure_ascii=False).encode("utf-8"))
        key = "x" * (55_000 - base + 1)
        at_limit = self.form(chat_snapshot=replace(snapshot, avatar_keys={self.chat.chat_id: key}),
                             target_mode="group", target_choice=self.chat.chat_id)
        self.assertEqual(len(json.dumps(at_limit.card, ensure_ascii=False).encode("utf-8")), 55_000)
        self.assertEqual(len(self.choice(at_limit)["options"]), 1)
        with self.assertRaisesRegex(CardActionError, "容量"):
            self.form(chat_snapshot=replace(snapshot, avatar_keys={self.chat.chat_id: key + "x"}),
                      target_mode="group", target_choice=self.chat.chat_id)

    async def test_public_sdk_event_preserves_button_context_and_all_form_fields(self):
        card = self.form(chat_snapshot=self.snapshot())
        channel = FeishuChannel(app_id="cli_contract", app_secret="test-secret")
        received: list[CardActionEvent] = []
        channel.on(Events.CARD_ACTION, received.append)
        for label, expected in (("创建分支", "create"), ("查找群聊", "results"), ("跳转", "results")):
            with self.subTest(label=label):
                value = callback(card, label)
                form = {**form_values(card), "fork_name_v5": "新名称", "fork_mode_v5": "group", "fork_target_v5": self.chat.chat_id}
                await channel._handle_interaction_event(P2CardActionTrigger({
                    "event": {
                        "operator": {"open_id": "ou_user"},
                        "context": {"open_message_id": "om_card", "open_chat_id": self.scope.chat_id},
                        "action": {"tag": "button", "value": value, "form_value": form},
                    },
                }))
                payload = received[-1].action
                self.assertEqual(payload.value, value)
                self.assertEqual(payload.form_value, form)
                decoded = decode_fork_action(self.scope, payload.value, payload.form_value)
                self.assertEqual(decoded.action, expected)
                self.assertEqual(decoded.target_choice, "" if label == "查找群聊" else self.chat.chat_id)
                self.assertEqual(decoded.target_chat_id, self.chat.chat_id if expected == "create" else None)
                self.assertEqual(decoded.name, "新名称")
                self.assertEqual(decoded.source, self.source)
        self.assertEqual(len(received), 3)

    def test_status_cards_never_recreate_or_retry_and_explain_partial_creation(self):
        display = {"name": "方案 B", **self.display}
        creating = fork_status_card(**display, status="creating")
        self.assertIn("请等待创建完成后再发送消息", str(creating.card))
        success = fork_status_card(**display, status="success", topic_url="https://example.test/topic")
        link = elements(success.card, "button")[0]
        self.assertEqual(link["behaviors"], [{"type": "open_url", "default_url": "https://example.test/topic"}])
        failed = fork_status_card(**display, status="failed", native_thread_id="native-created", detail="话题已有会话。")
        self.assertIn("原生分支已创建", str(failed.card))
        self.assertIn("绑定未完成", str(failed.card))
        self.assertIn("不要直接重试", str(failed.card))
        self.assertIn("native-created", str(failed.card))
        for card in (creating, success, failed):
            self.assertEqual(elements(card.card, "form"), [])
            self.assertFalse(any(item.get("type") == "callback"
                                 for button in elements(card.card, "button")
                                 for item in button.get("behaviors", [])))


if __name__ == "__main__":
    unittest.main()
