from __future__ import annotations

import base64
import json
import unittest
from dataclasses import replace

from netizen_cli.cards.callbacks import CardActionError
from netizen_cli.cards.fork import (
    ForkSource,
    decode_fork_action,
    fork_chat_results_card,
    fork_chat_search_card,
    fork_confirm_card,
    fork_destination_card,
    fork_status_card,
    is_fork_card_action,
)
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management.chat_directory import AvailableChat, AvailableChatPage
from tests.support.channel_cards import callback, elements, form_values, option_value


class ForkCardsTest(unittest.TestCase):
    def setUp(self):
        self.scope = FeishuScope("app", "oc_source", ScopeKind.TOPIC, "omt_source")
        self.source = ForkSource("binding-1", "native-thread-1", 2, 3, 4, 5)
        self.display = {"source_title": "设计方案", "project_alias": "work"}
        self.chat = AvailableChat("oc_target_12345678", "研发群", "group", False)

    def destination(self):
        return fork_destination_card(self.scope, self.source, **self.display)

    def confirm(self, *, target=None):
        return fork_confirm_card(self.scope, self.source, target_chat=target, **self.display)

    @staticmethod
    def choice(card):
        return elements(card.card, "select_static")[0]

    @staticmethod
    def changed_state(form, update):
        result = dict(form)
        field = next(name for name in form if name.startswith("fork_choice_v1__"))
        state = option_value(result[field])
        update(state)
        result[field] = base64.urlsafe_b64encode(json.dumps(state).encode()).decode().rstrip("=")
        return result

    def test_default_destination_is_exact_current_chat_without_name_draft(self):
        card = self.destination()
        action = decode_fork_action(self.scope, {}, form_values(card))
        self.assertEqual(action.action, "select")
        self.assertEqual(action.target_chat_id, self.scope.chat_id)
        self.assertEqual(action.source, self.source)
        self.assertIsNone(action.name)
        self.assertEqual(elements(card.card, "input"), [])
        self.assertTrue(card.card["config"]["update_multi"])
        self.assertTrue(is_fork_card_action({}, form_values(card)))

    def test_other_group_opens_search_before_any_group_is_selected(self):
        card = self.destination()
        select = self.choice(card)
        form = {select["name"]: select["options"][1]["value"]}
        action = decode_fork_action(self.scope, {}, form)
        self.assertEqual(action.action, "search")
        self.assertIsNone(action.target_chat_id)
        self.assertIsNone(action.query)

    def test_search_normalizes_query_and_starts_at_first_page(self):
        card = fork_chat_search_card(self.scope, self.source, **self.display)
        form = {**form_values(card), "fork_query_v1": "  研发  "}
        action = decode_fork_action(self.scope, {}, form)
        self.assertEqual((action.action, action.query, action.page_token), ("results", "研发", None))
        self.assertEqual(action.source, self.source)
        self.assertIn("不按操作者的群成员身份筛选", str(card.card))
        self.assertIsNone(action.name)
        for query in ("", " " * 4, "群" * 51, "研\n发", False):
            with self.subTest(query=query), self.assertRaises(CardActionError):
                decode_fork_action(self.scope, {}, {**form, "fork_query_v1": query})

    def test_result_selection_keeps_source_and_exact_target_separate(self):
        same_name = AvailableChat("oc_other_87654321", self.chat.name, None, True)
        page = AvailableChatPage((self.chat, same_name), "cursor-2")
        card = fork_chat_results_card(self.scope, self.source, page, query="研发", **self.display)
        select = self.choice(card)
        self.assertNotIn("initial_option", select)
        with self.assertRaises(CardActionError):
            decode_fork_action(self.scope, {}, form_values(card))
        for option, chat in zip(select["options"], page.items):
            with self.subTest(chat=chat.chat_id):
                action = decode_fork_action(self.scope, {}, {select["name"]: option["value"]})
                self.assertEqual(action.action, "select")
                self.assertEqual(action.source, self.source)
                self.assertEqual(action.target_chat_id, chat.chat_id)
                self.assertIn(chat.chat_id[-8:], option["text"]["content"])
                self.assertEqual(option["text"]["tag"], "plain_text")
        self.assertIn("外部群", select["options"][1]["text"]["content"])
        next_page = decode_fork_action(self.scope, callback(card, "下一页"))
        self.assertEqual((next_page.action, next_page.query, next_page.page_token),
                         ("results", "研发", "cursor-2"))

    def test_empty_filtered_page_keeps_next_page_and_reset_navigation(self):
        page = AvailableChatPage((), "cursor-2", "部分群暂不可用。")
        card = fork_chat_results_card(self.scope, self.source, page, query="研发", **self.display)
        self.assertEqual(elements(card.card, "form"), [])
        self.assertIn("本页没有", str(card.card))
        self.assertIn(page.notice, str(card.card))
        self.assertEqual(decode_fork_action(self.scope, callback(card, "下一页")).page_token, "cursor-2")
        search = decode_fork_action(self.scope, callback(card, "重新搜索"))
        self.assertEqual(search.action, "search")
        self.assertIsNone(search.query)
        self.assertIsNone(search.page_token)
        self.assertEqual(decode_fork_action(self.scope, callback(card, "返回创建位置")).action, "destination")

    def test_result_avatars_follow_exact_chat_ids_and_preserve_selection(self):
        same_name = AvailableChat("oc_other_87654321", self.chat.name, None, True)
        page = AvailableChatPage((self.chat, same_name), None)
        plain_card = fork_chat_results_card(self.scope, self.source, page,
            query="研发", **self.display)
        card = fork_chat_results_card(self.scope, self.source, page, query="研发",
            avatar_keys={self.chat.chat_id: "img_v3_avatar", "oc_unrelated": "img_v3_other"},
            **self.display)
        select = self.choice(card)
        self.assertEqual(select["options"][0]["icon"],
            {"tag": "custom_icon", "img_key": "img_v3_avatar"})
        self.assertEqual(select["options"][1]["icon"],
            {"tag": "standard_icon", "token": "group_outlined"})
        for option, plain, chat in zip(select["options"], self.choice(plain_card)["options"], page.items):
            with self.subTest(chat=chat.chat_id):
                self.assertEqual(option["value"], plain["value"])
                self.assertEqual(option["text"], plain["text"])
                self.assertEqual(plain["icon"], {"tag": "standard_icon", "token": "group_outlined"})
                action = decode_fork_action(self.scope, {}, {select["name"]: option["value"]})
                self.assertEqual(action.target_chat_id, chat.chat_id)
                self.assertEqual(action.source, self.source)
        self.assertNotIn("img_v3_other", str(card.card))

    def test_confirm_avatar_preserves_final_submission_and_falls_back(self):
        for target in (None, self.chat):
            with self.subTest(target=target):
                plain_card = self.confirm(target=target)
                card = fork_confirm_card(self.scope, self.source, target_chat=target,
                    avatar_key="img_v3_avatar", **self.display)
                select = self.choice(card)
                option = select["options"][0]
                self.assertEqual(option["icon"], {"tag": "custom_icon", "img_key": "img_v3_avatar"})
                self.assertEqual(select["initial_option"], option["value"])
                plain_option = self.choice(plain_card)["options"][0]
                if target is not None:
                    self.assertEqual(plain_option["icon"], {"tag": "standard_icon", "token": "group_outlined"})
                else:
                    self.assertNotIn("icon", plain_option)
                self.assertEqual(form_values(card), form_values(plain_card))
                action = decode_fork_action(self.scope, {}, form_values(card))
                self.assertEqual(action.action, "create")
                self.assertEqual(action.target_chat_id, target.chat_id if target else self.scope.chat_id)
                self.assertEqual(action.source, self.source)

    def test_current_chat_does_not_mislabel_direct_or_unknown_topic_as_a_group(self):
        for kind in ScopeKind:
            with self.subTest(kind=kind):
                scope = FeishuScope("app", "oc_source", kind, "omt_source" if kind is ScopeKind.TOPIC else None)
                card = fork_confirm_card(scope, self.source, **self.display)
                option = self.choice(card)["options"][0]
                if kind is ScopeKind.GROUP:
                    self.assertEqual(option["icon"], {"tag": "standard_icon", "token": "group_outlined"})
                else:
                    self.assertNotIn("icon", option)

    def test_final_name_form_preserves_exact_source_revisions_and_cross_group_notice(self):
        card = self.confirm(target=self.chat)
        form = {**form_values(card), "fork_name_v1": "  方案\n B  "}
        action = decode_fork_action(self.scope, {}, form)
        self.assertEqual(action.action, "create")
        self.assertEqual(action.name, "方案 B")
        self.assertEqual(action.target_chat_id, self.chat.chat_id)
        self.assertEqual(action.source, self.source)
        self.assertIn("目标群的参与者", str(card.card))
        self.assertIn("后续回答可能引用", str(card.card))
        self.assertIn("共享项目文件", str(card.card))
        # No navigation drops an edited name. To choose another destination,
        # open /fork again before making a new final submission.
        self.assertEqual(len(elements(card.card, "button")), 1)
        self.assertNotIn("目标群的参与者", str(self.confirm().card))

    def test_name_limit_matches_ordinary_rename_and_default_remains_valid(self):
        card = fork_confirm_card(self.scope, self.source, source_title="长" * 120, project_alias="work")
        form = form_values(card)
        action = decode_fork_action(self.scope, {}, form)
        self.assertEqual(len(action.name), 120)
        self.assertTrue(action.name.endswith(" · 分支"))
        for name in ("", " ", "长" * 121, "bad\x00name", 42):
            with self.subTest(name=name), self.assertRaises(CardActionError):
                decode_fork_action(self.scope, {}, {**form, "fork_name_v1": name})

    def test_repeatable_navigation_changes_nonce_but_creation_does_not(self):
        first = self.destination()
        second = self.destination()
        self.assertNotEqual(self.choice(first)["name"], self.choice(second)["name"])
        first = fork_chat_search_card(self.scope, self.source, **self.display)
        second = fork_chat_search_card(self.scope, self.source, **self.display)
        self.assertNotEqual(callback(first, "返回创建位置")["nonce"],
                            callback(second, "返回创建位置")["nonce"])
        self.assertEqual(form_values(self.confirm()), form_values(self.confirm()))
        self.assertEqual(set(form_values(self.confirm())), {"fork_choice_v1__create", "fork_name_v1"})
        for card in (first, second, self.destination(), self.confirm()):
            forms = elements(card.card, "form")
            self.assertEqual(len(forms), 1)
            submits = [button for button in elements(forms[0], "button")
                       if button.get("form_action_type") == "submit"]
            self.assertEqual(len(submits), 1)
            self.assertNotIn("behaviors", submits[0])
            self.assertTrue(all(len(item["name"]) <= 100 for item in elements(forms[0], "select_static")))

    def test_wrong_scope_or_forged_source_shape_is_rejected(self):
        form = form_values(self.confirm())
        other_scope = FeishuScope("app", self.scope.chat_id, ScopeKind.TOPIC, "omt_other")
        with self.assertRaises(CardActionError):
            decode_fork_action(other_scope, {}, form)
        changes = [
            lambda state: state.update(v=True),
            lambda state: state.update(v=99),
            lambda state: state.update(unexpected=True),
            lambda state: state["source"].update(binding_id="bad/id"),
            lambda state: state["source"].update(native_thread_id=None),
            lambda state: state["source"].update(settings_revision=0),
            lambda state: state["source"].update(context_revision=True),
            lambda state: state["source"].update(feedback_revision="4"),
            lambda state: state["source"].update(project_revision=-1),
            lambda state: state["source"].update(unexpected=True),
            lambda state: state["source"].pop("project_revision"),
            lambda state: state.update(target_chat_id=""),
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(CardActionError):
                decode_fork_action(self.scope, {}, self.changed_state(form, change))
        # Parsing retains valid revisions; business freshness is checked by
        # Runtime/management, not guessed by the presentation adapter.
        changed = replace(self.source, settings_revision=6)
        card = fork_confirm_card(self.scope, changed, **self.display)
        self.assertEqual(decode_fork_action(self.scope, {}, form_values(card)).source, changed)

    def test_strict_forms_reject_mixed_actions_missing_fields_and_broken_encoding(self):
        form = form_values(self.confirm())
        invalid_forms = [
            {**form, "new_project": "project:v1:work:1"},
            {"fork_choice_v1__create": form["fork_choice_v1__create"]},
            {**form, "fork_choice_v1__" + "a" * 32: form["fork_choice_v1__create"]},
            {**form, "fork_choice_v1__create": "not base64"},
            {**form, "fork_choice_v1__create": "W10"},
            {"fork_choice_v1__bad": form["fork_choice_v1__create"], "fork_name_v1": "name"},
        ]
        for invalid in invalid_forms:
            with self.subTest(form=invalid), self.assertRaises(CardActionError):
                decode_fork_action(self.scope, {}, invalid)
        with self.assertRaises(CardActionError):
            decode_fork_action(self.scope, {"kind": "netizen_fork"}, form)
        with self.assertRaises(CardActionError):
            decode_fork_action(self.scope, {}, self.changed_state(form, lambda state: state.update(action="select")))

    def test_callbacks_cannot_create_or_smuggle_invalid_pagination(self):
        page = AvailableChatPage((), "cursor-2")
        card = fork_chat_results_card(self.scope, self.source, page, query="研发", **self.display)
        value = callback(card, "下一页")
        self.assertTrue(is_fork_card_action(value))
        invalid = [
            {**value, "nonce": "bad"}, {**value, "page_token": "x" * 1025},
            {**value, "query": ""}, {**value, "query": " 研发 "},
            {**value, "unexpected": True},
        ]
        create = option_value(form_values(self.confirm())["fork_choice_v1__create"])
        invalid.append({**create, "nonce": value["nonce"]})
        for item in invalid:
            with self.subTest(value=item), self.assertRaises(CardActionError):
                decode_fork_action(self.scope, item)
        self.assertFalse(is_fork_card_action({}, {"new_project": "anything"}))

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
