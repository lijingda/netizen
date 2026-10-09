from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lark_oapi.api.im.v1.model.get_chat_response import GetChatResponse
from lark_oapi.api.im.v1.model.is_in_chat_chat_members_response import IsInChatChatMembersResponse
from lark_oapi.api.im.v1.model.list_chat_response import ListChatResponse
from lark_oapi.api.im.v2.model.search_chat_response import SearchChatResponse

from netizen_cli.management.chat_directory import ChatDirectoryError, FeishuChatDirectory


def chat(chat_id="oc_group", **fields):
    return {"chat_id": chat_id, "name": "测试群", "chat_mode": "group", "chat_status": "normal", "external": False, **fields}


def listed(items, **fields):
    return ListChatResponse({"code": 0, "data": {"items": items, "has_more": False, **fields}})


def searched(items, **fields):
    return SearchChatResponse({"code": 0, "data": {
        "items": [{"id": "not-the-chat-id", "display_info": "<h>do not use HTML</h>", "meta_data": item} for item in items],
        "has_more": False, **fields,
    }})


def membership(value=True):
    return IsInChatChatMembersResponse({"code": 0, "data": {"is_in_chat": value}})


class ChatDirectoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.list = AsyncMock(return_value=listed([chat()]))
        self.search = AsyncMock(return_value=searched([chat()]))
        self.get = AsyncMock(return_value=GetChatResponse({"code": 0, "data": chat()}))
        self.member = AsyncMock(return_value=membership())
        self.client = SimpleNamespace(im=SimpleNamespace(
            v1=SimpleNamespace(chat=SimpleNamespace(alist=self.list, aget=self.get), chat_members=SimpleNamespace(ais_in_chat=self.member)),
            v2=SimpleNamespace(chat=SimpleNamespace(asearch=self.search)),
        ))
        self.directory = FeishuChatDirectory(self.client)

    async def test_avatars_flow_through_existing_list_search_and_validation_calls(self):
        url = "https://p3-lark-file.byteimg.com/img/avatar.jpg"
        self.list.return_value = listed([chat(avatar=url)])
        self.search.return_value = searched([chat(avatar=url)])
        self.get.return_value = GetChatResponse({"code": 0, "data": chat(avatar=url)})
        self.assertEqual((await self.directory.query()).items[0].avatar_url, url)
        self.assertEqual((await self.directory.query(query="测试")).items[0].avatar_url, url)
        self.assertEqual((await self.directory.validate("oc_group")).avatar_url, url)
        self.assertEqual((self.list.await_count, self.search.await_count, self.get.await_count), (1, 1, 1))
        self.assertEqual(self.member.await_count, 2)

    async def test_missing_or_untrusted_avatar_never_removes_a_valid_group(self):
        for url in (None, "", "https://example.com/avatar.jpg", "javascript:alert(1)",
                    "https://p3-lark-file.byteimg.com@localhost/avatar.jpg"):
            with self.subTest(avatar=url):
                self.list.return_value = listed([chat(avatar=url)])
                group = (await self.directory.query()).items[0]
                self.assertEqual(group.chat_id, "oc_group")
                self.assertIsNone(group.avatar_url)

    async def test_browse_uses_joined_group_list_and_one_bounded_page(self):
        self.list.return_value = listed([
            chat(chat_mode=None), chat("oc_dissolved", chat_status="dissolved"),
            chat("oc_kept", chat_status="dissolved_save"), chat("oc_direct", chat_mode="p2p"),
        ], has_more=True, page_token="next-page")
        page = await self.directory.query()
        self.assertEqual([item.chat_id for item in page.items], ["oc_group"])
        self.assertIsNone(page.items[0].chat_mode)
        self.assertEqual(page.next_page_token, "next-page")
        request = self.list.call_args.args[0]
        self.assertEqual(request.uri, "/open-apis/im/v1/chats")
        self.assertEqual(request.page_size, 20)
        self.assertEqual(request.sort_type, "ByCreateTimeAsc")
        self.assertIsNone(request.types)
        self.assertEqual(self.list.await_count, 1)
        self.search.assert_not_awaited()
        self.member.assert_not_awaited()
        self.get.assert_not_awaited()

    async def test_search_is_remote_paginated_name_search_and_filters_nonmembers(self):
        self.search.return_value = searched([chat(), chat("oc_public"), chat("oc_gone", chat_status="dissolved")], has_more=True, page_token="second", notice="部分结果可能不完整")
        self.member.side_effect = [membership(True), membership(False)]
        page = await self.directory.query(query="测试", page_token="first")
        self.assertEqual([item.chat_id for item in page.items], ["oc_group"])
        self.assertEqual(page.items[0].name, "测试群")
        self.assertEqual(page.notice, "部分结果可能不完整")
        self.assertEqual(page.next_page_token, "second")
        request = self.search.call_args.args[0]
        self.assertEqual(request.uri, "/open-apis/im/v2/chats/search")
        self.assertEqual(request.page_token, "first")
        self.assertEqual(request.page_size, 20)
        self.assertEqual(request.request_body.query, "测试")
        self.assertEqual(request.request_body.filter.search_types, ["private", "external", "public_joined"])
        self.assertTrue(request.request_body.filter.disable_search_by_user)
        self.assertEqual([call.args[0].chat_id for call in self.member.call_args_list], ["oc_group", "oc_public"])
        self.list.assert_not_awaited()

    async def test_filtered_empty_page_preserves_next_page_instead_of_scanning(self):
        self.search.return_value = searched([chat()], has_more=True, page_token="more")
        self.member.return_value = membership(False)
        page = await self.directory.query(query="群")
        self.assertEqual(page.items, ())
        self.assertEqual(page.next_page_token, "more")
        self.assertEqual(self.search.await_count, 1)

    async def test_search_unknown_subtype_preserves_membership_without_guessing_mode(self):
        for mode in ("DEFAULT", "UNDOCUMENTED_MODE", None):
            self.search.return_value = searched([
                chat("oc_joined", chat_mode=mode),
                chat("oc_not_joined", chat_mode=mode),
                chat("oc_gone", chat_mode=mode, chat_status="dissolved"),
            ])
            self.member.side_effect = [membership(True), membership(False)]
            with self.subTest(mode=mode):
                page = await self.directory.query(query="群")
            self.assertEqual([item.chat_id for item in page.items], ["oc_joined"])
            self.assertIsNone(page.items[0].chat_mode)
        self.get.assert_not_awaited()

    async def test_list_pagination_passes_platform_token(self):
        await self.directory.query(page_token="next-page")
        self.assertEqual(self.list.call_args.args[0].page_token, "next-page")

    async def test_invalid_inputs_never_call_platform(self):
        for query in ("x" * 51, " group", "group "):
            with self.subTest(query=query), self.assertRaises(ChatDirectoryError) as caught:
                await self.directory.query(query=query)
            self.assertEqual(caught.exception.code, "invalid_query")
        for token in ("", "x" * 1_025, "a\nb"):
            with self.subTest(token=token), self.assertRaises(ChatDirectoryError) as caught:
                await self.directory.query(page_token=token)
            self.assertEqual(caught.exception.code, "invalid_cursor")
        for chat_id in ("ou_user", "oc_", "oc_a/b", "oc_a?token=x"):
            with self.subTest(chat_id=chat_id), self.assertRaises(ChatDirectoryError) as caught:
                await self.directory.validate(chat_id)
            self.assertEqual(caught.exception.code, "invalid_chat_id")
        self.search.assert_not_awaited()
        self.list.assert_not_awaited()
        self.member.assert_not_awaited()

    async def test_selected_group_is_rechecked_and_uses_current_name(self):
        self.get.return_value = GetChatResponse({"code": 0, "data": chat(name="新名称", chat_mode="topic")})
        group = await self.directory.validate("oc_group")
        self.assertEqual(group.name, "新名称")
        self.assertEqual(group.chat_mode, "topic")
        self.assertEqual(self.get.call_args.args[0].chat_id, "oc_group")
        self.member.return_value = membership(False)
        with self.assertRaises(ChatDirectoryError) as caught:
            await self.directory.validate("oc_group")
        self.assertEqual(caught.exception.code, "chat_unavailable")
        self.assertEqual(self.get.await_count, 1)

    async def test_selected_dissolved_or_p2p_chat_cannot_become_group(self):
        for fields, code in [({"chat_mode": "p2p"}, "not_group_chat"), ({"chat_status": "dissolved_save"}, "chat_unavailable")]:
            self.get.return_value = GetChatResponse({"code": 0, "data": chat(**fields)})
            with self.subTest(fields=fields), self.assertRaises(ChatDirectoryError) as caught:
                await self.directory.validate("oc_group")
            self.assertEqual(caught.exception.code, code)

    async def test_selected_chat_type_takes_precedence_over_optional_mode(self):
        for mode in ("group", "topic", "p2p", None):
            self.get.return_value = GetChatResponse({"code": 0, "data": chat(chat_type="p2p", chat_mode=mode)})
            with self.subTest(chat_type="p2p", mode=mode), self.assertRaises(ChatDirectoryError) as caught:
                await self.directory.validate("oc_group")
            self.assertEqual(caught.exception.code, "not_group_chat")
        for mode, expected in ((None, None), ("p2p", None), ("group", "group"), ("topic", "topic")):
            self.get.return_value = GetChatResponse({"code": 0, "data": chat(chat_type="group", chat_mode=mode)})
            with self.subTest(chat_type="group", mode=mode):
                result = await self.directory.validate("oc_group")
            self.assertEqual(result.chat_mode, expected)

    async def test_visibility_type_does_not_establish_chat_kind(self):
        for visibility in ("public", "private"):
            self.get.return_value = GetChatResponse({"code": 0, "data": chat(chat_type=visibility, chat_mode="topic")})
            self.assertEqual((await self.directory.validate("oc_group")).chat_mode, "topic")
            self.get.return_value = GetChatResponse({"code": 0, "data": chat(chat_type=visibility, chat_mode=None)})
            with self.assertRaises(ChatDirectoryError) as caught:
                await self.directory.validate("oc_group")
            self.assertEqual(caught.exception.code, "chat_query_failed")

    async def test_upstream_errors_are_distinct_and_do_not_expose_text(self):
        for platform_code, http_status, expected in [
            (231020, 400, "invalid_cursor"), (231022, 400, "chat_search_limit"),
            (99991672, 400, "chat_permission_denied"), (232033, 400, "chat_permission_denied"),
            (1, 429, "chat_query_rate_limited"), (232006, 400, "chat_unavailable"),
            (123, 500, "chat_query_failed"),
        ]:
            self.search.return_value = SimpleNamespace(code=platform_code, msg="secret from upstream", raw=SimpleNamespace(status_code=http_status))
            with self.subTest(code=platform_code), self.assertRaises(ChatDirectoryError) as caught:
                await self.directory.query(query="群")
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("secret", str(caught.exception))
        self.search.side_effect = OSError("secret app credential")
        with self.assertRaises(ChatDirectoryError) as caught:
            await self.directory.query(query="群")
        self.assertNotIn("secret", str(caught.exception))

    async def test_membership_failure_does_not_turn_search_into_empty_result(self):
        self.member.return_value = SimpleNamespace(code=99991672)
        with self.assertRaises(ChatDirectoryError) as caught:
            await self.directory.query(query="群")
        self.assertEqual(caught.exception.code, "chat_permission_denied")

    async def test_missing_or_unexpected_response_fails_closed(self):
        for response in [
            SimpleNamespace(code=0, data=None),
            listed([chat(chat_status=None)]), listed([chat(chat_mode="unexpected")]),
            listed([chat(chat_id="ou_user")]), listed([chat()] * 21),
            listed([], has_more=True, page_token=None), listed([], has_more=True, page_token="again"),
        ]:
            self.list.return_value = response
            with self.subTest(response=response), self.assertRaises(ChatDirectoryError):
                await self.directory.query(page_token="again")
        self.member.return_value = SimpleNamespace(code=0, data=SimpleNamespace(is_in_chat=1))
        with self.assertRaises(ChatDirectoryError):
            await self.directory.query(query="群")

    async def test_timeout_cancels_membership_work_and_concurrency_is_bounded(self):
        self.search.return_value = searched([chat(f"oc_{i}") for i in range(20)])
        active = 0
        peak = 0

        async def blocked(_request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        self.member.side_effect = blocked
        directory = FeishuChatDirectory(self.client, query_seconds=0.05)
        with self.assertRaises(ChatDirectoryError) as caught:
            await directory.query(query="群")
        self.assertEqual(caught.exception.code, "chat_query_timeout")
        self.assertEqual(active, 0)
        self.assertEqual(peak, 4)

    async def test_request_cancellation_propagates_without_lingering_sdk_calls(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def blocked(_request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.member.side_effect = blocked
        task = asyncio.create_task(self.directory.query(query="群"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
