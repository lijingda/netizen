from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from lark_oapi.api.im.v1.model.get_message_request import GetMessageRequest
from lark_oapi.api.im.v1.model.list_message_request import ListMessageRequest

from netizen.domain import FeishuScope, MessageContextAnchor, ScopeKind
from netizen.message_history import (
    FeishuMessageHistoryReader,
    MessageHistoryContractError,
    MessageHistoryUnavailable,
)


class _MetadataMessage:
    def __init__(
        self,
        message_id: str,
        create_time: int | str,
        *,
        chat_id: str = "oc_group",
        thread_id: str | None = None,
        msg_type: str | None = "text",
        deleted: bool | None = False,
        sender_type: str | None = "user",
        sender_id: str | None = "ou_alice",
        sender_name: str | None = "Alice",
        sender_id_type: str | None = "open_id",
    ) -> None:
        self.message_id = message_id
        self.create_time = create_time
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.msg_type = msg_type
        self.deleted = deleted
        self.sender = SimpleNamespace(
            sender_type=sender_type,
            id=sender_id,
            sender_name=sender_name,
            id_type=sender_id_type,
        )

    @property
    def body(self) -> object:
        raise AssertionError("history reader must not inspect message bodies")

    @property
    def message_position(self) -> object:
        raise AssertionError("history reader must not depend on position")

    @property
    def thread_message_position(self) -> object:
        raise AssertionError("history reader must not depend on thread position")


class _Response:
    def __init__(
        self,
        *,
        items: list[Any] | None = None,
        has_more: bool | None = None,
        page_token: str | None = None,
        successful: bool = True,
        data_present: bool = True,
    ) -> None:
        self._successful = successful
        self.data = (
            SimpleNamespace(
                items=items,
                has_more=has_more,
                page_token=page_token,
            )
            if data_present
            else None
        )

    def success(self) -> bool:
        return self._successful


class _FakeMessageApi:
    def __init__(
        self,
        exact: dict[str, Any],
        pages: list[Any] | None = None,
        *,
        get_delay: float = 0.0,
        page_delay: float = 0.0,
    ) -> None:
        self.exact = exact
        self.pages = list(pages or [])
        self.get_delay = get_delay
        self.page_delay = page_delay
        self.get_requests: list[GetMessageRequest] = []
        self.list_requests: list[ListMessageRequest] = []

    async def aget(self, request: GetMessageRequest) -> _Response:
        self.get_requests.append(request)
        if self.get_delay:
            await asyncio.sleep(self.get_delay)
        result = self.exact[request.message_id]
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, _Response):
            return result
        return _Response(items=[result])

    async def alist(self, request: ListMessageRequest) -> _Response:
        self.list_requests.append(request)
        if self.page_delay:
            await asyncio.sleep(self.page_delay)
        if not self.pages:
            raise AssertionError("unexpected list request")
        result = self.pages.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _client(api: _FakeMessageApi) -> Any:
    return SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=api)),
    )


class FeishuMessageHistoryReaderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.group = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        self.topic = FeishuScope(
            "cli_test",
            "oc_group",
            ScopeKind.TOPIC,
            "omt_topic",
        )

    async def test_constructor_cannot_expand_safety_budgets(self) -> None:
        api = _FakeMessageApi({})
        for kwargs in (
            {"get_timeout_seconds": 10.1},
            {"page_timeout_seconds": 10.1},
            {"total_timeout_seconds": 60.1},
            {"max_pages": 11},
            {"max_raw_messages": 501},
            {"page_size": 51},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                FeishuMessageHistoryReader(_client(api), **kwargs)

    async def test_resolve_anchor_uses_typed_exact_get_and_never_reads_body(
        self,
    ) -> None:
        api = _FakeMessageApi(
            {"om_anchor": _MetadataMessage("om_anchor", "1700000000123")}
        )
        reader = FeishuMessageHistoryReader(_client(api))

        anchor = await reader.resolve_anchor(self.group, "om_anchor")

        self.assertEqual(
            anchor,
            MessageContextAnchor("om_anchor", 1_700_000_000_123),
        )
        self.assertEqual(len(api.get_requests), 1)
        request = api.get_requests[0]
        self.assertIsInstance(request, GetMessageRequest)
        self.assertEqual(request.message_id, "om_anchor")
        self.assertEqual(request.user_id_type, "open_id")
        self.assertTrue(request.with_sender_name)

    async def test_group_window_is_exact_deduplicated_and_oldest_first(self) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)
        api = _FakeMessageApi(
            {
                "om_lower": _MetadataMessage("om_lower", 1_000),
                "om_upper": _MetadataMessage("om_upper", 4_000),
            },
            [
                _Response(
                    items=[
                        _MetadataMessage("om_future", 4_500),
                        _MetadataMessage("om_upper", 4_000),
                        _MetadataMessage(
                            "om_new",
                            3_000,
                            sender_id="ou_new",
                            sender_name="New",
                        ),
                        _MetadataMessage(
                            "om_topic_reply",
                            2_900,
                            thread_id="omt_other",
                        ),
                        _MetadataMessage(
                            "om_new",
                            3_000,
                            sender_id="ou_new",
                            sender_name="New",
                        ),
                    ],
                    has_more=True,
                    page_token="page-two",
                ),
                _Response(
                    items=[
                        _MetadataMessage("om_system", 2_800, msg_type="system"),
                        _MetadataMessage("om_bot", 2_700, sender_type="bot"),
                        _MetadataMessage("om_no_name", 2_600, sender_name=None),
                        _MetadataMessage("om_deleted", 2_500, deleted=True),
                        _MetadataMessage(
                            "om_old",
                            2_000,
                            sender_id="ou_old",
                            sender_name="Old",
                            msg_type="post",
                        ),
                        _MetadataMessage("om_lower", 1_000),
                        _MetadataMessage("om_too_old", 900),
                    ],
                    has_more=False,
                ),
            ],
        )
        reader = FeishuMessageHistoryReader(_client(api))

        window = await reader.read_window(self.group, lower, "om_upper")

        self.assertEqual([ref.message_id for ref in window.candidates], [
            "om_old",
            "om_new",
        ])
        self.assertEqual(window.candidates[0].sender_id, "ou_old")
        self.assertEqual(window.candidates[0].sender_name, "Old")
        self.assertEqual(window.candidates[0].message_type, "post")
        self.assertEqual(window.upper, MessageContextAnchor("om_upper", 4_000))
        self.assertEqual(window.stats.pages_scanned, 2)
        self.assertEqual(window.stats.raw_messages_scanned, 11)
        self.assertEqual(window.stats.duplicate_messages, 1)
        self.assertEqual(window.stats.ignored_after_upper, 1)
        self.assertEqual(window.stats.omitted_messages, 5)
        self.assertFalse(window.stats.truncated_before)
        self.assertFalse(window.stats.scan_limit_hit)

        self.assertEqual(len(api.get_requests), 2)
        self.assertEqual(
            [request.message_id for request in api.get_requests],
            ["om_lower", "om_upper"],
        )
        first, second = api.list_requests
        for request in (first, second):
            self.assertIsInstance(request, ListMessageRequest)
            self.assertEqual(request.container_id_type, "chat")
            self.assertEqual(request.container_id, "oc_group")
            self.assertEqual(request.end_time, "5")
            self.assertEqual(request.sort_type, "ByCreateTimeDesc")
            self.assertEqual(request.page_size, 50)
            self.assertTrue(request.with_sender_name)
            self.assertIsNone(request.start_time)
        self.assertIsNone(first.page_token)
        self.assertEqual(second.page_token, "page-two")

    async def test_topic_uses_thread_container_without_time_filters(self) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)

        def topic_message(message_id: str, create_time: int) -> _MetadataMessage:
            return _MetadataMessage(
                message_id,
                create_time,
                thread_id="omt_topic",
            )

        api = _FakeMessageApi(
            {
                "om_lower": topic_message("om_lower", 1_000),
                "om_upper": topic_message("om_upper", 3_000),
            },
            [
                _Response(
                    items=[
                        topic_message("om_upper", 3_000),
                        topic_message("om_middle", 2_000),
                        topic_message("om_lower", 1_000),
                    ],
                    has_more=False,
                )
            ],
        )

        window = await FeishuMessageHistoryReader(_client(api)).read_window(
            self.topic,
            lower,
            "om_upper",
        )

        self.assertEqual([ref.message_id for ref in window.candidates], [
            "om_middle"
        ])
        request = api.list_requests[0]
        self.assertEqual(request.container_id_type, "thread")
        self.assertEqual(request.container_id, "omt_topic")
        self.assertIsNone(request.start_time)
        self.assertIsNone(request.end_time)

    async def test_missing_lower_returns_only_known_newer_refs_and_truncates(
        self,
    ) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)
        api = _FakeMessageApi(
            {
                "om_lower": _MetadataMessage("om_lower", 1_000),
                "om_upper": _MetadataMessage("om_upper", 5_000),
            },
            [
                _Response(
                    items=[
                        _MetadataMessage("om_upper", 5_000),
                        _MetadataMessage("om_middle", 3_000),
                        _MetadataMessage("om_before_lower", 900),
                    ],
                    has_more=False,
                )
            ],
        )

        window = await FeishuMessageHistoryReader(_client(api)).read_window(
            self.group,
            lower,
            "om_upper",
        )

        self.assertEqual([ref.message_id for ref in window.candidates], [
            "om_middle"
        ])
        self.assertTrue(window.stats.truncated_before)
        self.assertFalse(window.stats.scan_limit_hit)
        self.assertEqual(window.stats.raw_messages_scanned, 3)

    async def test_page_budget_marks_window_truncated_without_an_extra_call(
        self,
    ) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)
        api = _FakeMessageApi(
            {
                "om_lower": _MetadataMessage("om_lower", 1_000),
                "om_upper": _MetadataMessage("om_upper", 5_000),
            },
            [
                _Response(
                    items=[
                        _MetadataMessage("om_upper", 5_000),
                        _MetadataMessage("om_three", 4_000),
                    ],
                    has_more=True,
                    page_token="two",
                ),
                _Response(
                    items=[
                        _MetadataMessage("om_two", 3_000),
                        _MetadataMessage("om_one", 2_000),
                    ],
                    has_more=True,
                    page_token="three",
                ),
            ],
        )
        reader = FeishuMessageHistoryReader(
            _client(api),
            max_pages=2,
            max_raw_messages=4,
            page_size=2,
        )

        window = await reader.read_window(self.group, lower, "om_upper")

        self.assertEqual(
            [ref.message_id for ref in window.candidates],
            ["om_one", "om_two", "om_three"],
        )
        self.assertTrue(window.stats.truncated_before)
        self.assertTrue(window.stats.scan_limit_hit)
        self.assertEqual(window.stats.pages_scanned, 2)
        self.assertEqual(window.stats.raw_messages_scanned, 4)
        self.assertEqual(len(api.list_requests), 2)

    async def test_raw_message_budget_stops_inside_a_page(self) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)
        api = _FakeMessageApi(
            {
                "om_lower": _MetadataMessage("om_lower", 1_000),
                "om_upper": _MetadataMessage("om_upper", 5_000),
            },
            [
                _Response(
                    items=[
                        _MetadataMessage("om_upper", 5_000),
                        _MetadataMessage("om_new", 4_000),
                        _MetadataMessage("om_not_scanned", 3_000),
                    ],
                    has_more=True,
                    page_token="unused",
                )
            ],
        )
        reader = FeishuMessageHistoryReader(
            _client(api),
            max_pages=10,
            max_raw_messages=2,
            page_size=3,
        )

        window = await reader.read_window(self.group, lower, "om_upper")

        self.assertEqual([ref.message_id for ref in window.candidates], ["om_new"])
        self.assertEqual(window.stats.raw_messages_scanned, 2)
        self.assertTrue(window.stats.truncated_before)
        self.assertTrue(window.stats.scan_limit_hit)
        self.assertEqual(len(api.list_requests), 1)

    async def test_repeated_page_token_fails_closed(self) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)
        api = _FakeMessageApi(
            {
                "om_lower": _MetadataMessage("om_lower", 1_000),
                "om_upper": _MetadataMessage("om_upper", 5_000),
            },
            [
                _Response(
                    items=[_MetadataMessage("om_upper", 5_000)],
                    has_more=True,
                    page_token="loop",
                ),
                _Response(
                    items=[_MetadataMessage("om_middle", 3_000)],
                    has_more=True,
                    page_token="loop",
                ),
            ],
        )

        with self.assertRaises(MessageHistoryContractError):
            await FeishuMessageHistoryReader(_client(api)).read_window(
                self.group,
                lower,
                "om_upper",
            )

    async def test_missing_upper_fails_closed(self) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)
        api = _FakeMessageApi(
            {
                "om_lower": _MetadataMessage("om_lower", 1_000),
                "om_upper": _MetadataMessage("om_upper", 5_000),
            },
            [
                _Response(
                    items=[
                        _MetadataMessage("om_future", 6_000),
                        _MetadataMessage("om_middle", 3_000),
                        _MetadataMessage("om_lower", 1_000),
                    ],
                    has_more=False,
                ),
                _Response(
                    items=[_MetadataMessage("om_future", 6_000)],
                    has_more=False,
                ),
            ],
        )

        with self.assertRaises(MessageHistoryUnavailable):
            await FeishuMessageHistoryReader(_client(api)).read_window(
                self.group,
                lower,
                "om_upper",
            )
        self.assertEqual(len(api.list_requests), 2)

    async def test_upper_can_appear_on_one_bounded_fresh_snapshot(self) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)
        api = _FakeMessageApi(
            {
                "om_lower": _MetadataMessage("om_lower", 1_000),
                "om_upper": _MetadataMessage("om_upper", 5_000),
            },
            [
                _Response(
                    items=[
                        _MetadataMessage("om_future", 6_000),
                        _MetadataMessage("om_middle", 3_000),
                        _MetadataMessage("om_lower", 1_000),
                    ],
                    has_more=False,
                ),
                _Response(
                    items=[
                        _MetadataMessage("om_future", 6_000),
                        _MetadataMessage("om_upper", 5_000),
                        _MetadataMessage("om_middle", 3_000),
                        _MetadataMessage("om_lower", 1_000),
                    ],
                    has_more=False,
                ),
            ],
        )

        window = await FeishuMessageHistoryReader(_client(api)).read_window(
            self.group,
            lower,
            "om_upper",
        )

        self.assertEqual([ref.message_id for ref in window.candidates], [
            "om_middle"
        ])
        self.assertEqual(window.stats.pages_scanned, 2)
        self.assertEqual(window.stats.raw_messages_scanned, 7)
        self.assertEqual(window.stats.duplicate_messages, 0)
        self.assertEqual(len(api.list_requests), 2)

    async def test_exact_anchor_scope_and_persisted_time_are_fail_closed(self) -> None:
        wrong_chat_api = _FakeMessageApi(
            {
                "om_anchor": _MetadataMessage(
                    "om_anchor",
                    1_000,
                    chat_id="oc_other",
                )
            }
        )
        with self.assertRaises(MessageHistoryUnavailable):
            await FeishuMessageHistoryReader(_client(wrong_chat_api)).resolve_anchor(
                self.group,
                "om_anchor",
            )

        lower = MessageContextAnchor("om_lower", 999)
        stale_api = _FakeMessageApi(
            {
                "om_lower": _MetadataMessage("om_lower", 1_000),
                "om_upper": _MetadataMessage("om_upper", 2_000),
            }
        )
        with self.assertRaises(MessageHistoryUnavailable):
            await FeishuMessageHistoryReader(_client(stale_api)).read_window(
                self.group,
                lower,
                "om_upper",
            )
        self.assertEqual(len(stale_api.get_requests), 1)
        self.assertEqual(stale_api.list_requests, [])

    async def test_topic_cross_scope_page_fails_closed(self) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)

        def topic_message(
            message_id: str,
            create_time: int,
            thread_id: str = "omt_topic",
        ) -> _MetadataMessage:
            return _MetadataMessage(
                message_id,
                create_time,
                thread_id=thread_id,
            )

        api = _FakeMessageApi(
            {
                "om_lower": topic_message("om_lower", 1_000),
                "om_upper": topic_message("om_upper", 3_000),
            },
            [
                _Response(
                    items=[
                        topic_message("om_upper", 3_000),
                        topic_message("om_wrong", 2_000, "omt_other"),
                    ],
                    has_more=False,
                )
            ],
        )

        with self.assertRaises(MessageHistoryContractError):
            await FeishuMessageHistoryReader(_client(api)).read_window(
                self.topic,
                lower,
                "om_upper",
            )

    async def test_direct_scope_never_calls_openapi(self) -> None:
        direct = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        api = _FakeMessageApi({})

        with self.assertRaises(MessageHistoryContractError):
            await FeishuMessageHistoryReader(_client(api)).resolve_anchor(
                direct,
                "om_message",
            )

        self.assertEqual(api.get_requests, [])
        self.assertEqual(api.list_requests, [])

    async def test_sdk_failure_and_get_timeout_are_safe_errors(self) -> None:
        failed_api = _FakeMessageApi(
            {"om_anchor": _Response(items=[], successful=False)}
        )
        with self.assertRaisesRegex(MessageHistoryUnavailable, "本条消息未执行"):
            await FeishuMessageHistoryReader(_client(failed_api)).resolve_anchor(
                self.group,
                "om_anchor",
            )

        slow_api = _FakeMessageApi(
            {"om_anchor": _MetadataMessage("om_anchor", 1_000)},
            get_delay=0.02,
        )
        with self.assertRaisesRegex(MessageHistoryUnavailable, "本条消息未执行"):
            await FeishuMessageHistoryReader(
                _client(slow_api),
                get_timeout_seconds=0.001,
            ).resolve_anchor(self.group, "om_anchor")

    async def test_page_timeout_and_total_timeout_are_bounded(self) -> None:
        lower = MessageContextAnchor("om_lower", 1_000)

        def exact() -> dict[str, _MetadataMessage]:
            return {
                "om_lower": _MetadataMessage("om_lower", 1_000),
                "om_upper": _MetadataMessage("om_upper", 2_000),
            }

        page_api = _FakeMessageApi(
            exact(),
            [_Response(items=[])],
            page_delay=0.02,
        )
        with self.assertRaises(MessageHistoryUnavailable):
            await FeishuMessageHistoryReader(
                _client(page_api),
                page_timeout_seconds=0.001,
            ).read_window(self.group, lower, "om_upper")

        total_api = _FakeMessageApi(
            exact(),
            [_Response(items=[])],
            get_delay=0.02,
        )
        with self.assertRaises(MessageHistoryUnavailable):
            await FeishuMessageHistoryReader(
                _client(total_api),
                get_timeout_seconds=0.1,
                total_timeout_seconds=0.001,
            ).read_window(self.group, lower, "om_upper")


if __name__ == "__main__":
    unittest.main()
