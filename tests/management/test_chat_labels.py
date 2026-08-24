from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from netizen.management import ChatLabelResolver


@dataclass(frozen=True)
class FakeChatInfo:
    name: str | None
    chat_mode: str | None
    chat_type: str | None = "private"


@dataclass(frozen=True)
class FakeChatMember:
    id: str
    name: str | None
    id_type: str = "open_id"


class FakeChatLabelProvider:
    def __init__(self) -> None:
        self.info: dict[str, FakeChatInfo | None] = {}
        self.members: dict[str, list[FakeChatMember]] = {}
        self.info_calls: list[str] = []
        self.member_calls: list[str] = []
        self.member_errors: set[str] = set()
        self.gate: asyncio.Event | None = None
        self.active = 0
        self.max_active = 0

    async def get_chat_info(self, chat_id: str):
        self.info_calls.append(chat_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            return self.info.get(chat_id)
        finally:
            self.active -= 1

    async def get_chat_members(self, chat_id: str, **_kwargs):
        self.member_calls.append(chat_id)
        if chat_id in self.member_errors:
            raise RuntimeError("member lookup failed")
        return self.members.get(chat_id, [])


class ChatLabelResolverTest(unittest.IsolatedAsyncioTestCase):
    async def test_group_and_p2p_use_chat_mode_and_member_name(self) -> None:
        provider = FakeChatLabelProvider()
        provider.info = {
            "oc_group": FakeChatInfo("  Engineering  ", "group", "private"),
            "oc_topic": FakeChatInfo("Topic Team", "topic", "private"),
            "oc_direct": FakeChatInfo("", "p2p", "private"),
        }
        provider.members["oc_direct"] = [
            FakeChatMember("ou_alice", " Alice ")
        ]
        resolver = ChatLabelResolver(provider)
        self.addAsyncCleanup(resolver.aclose)

        labels = await resolver.resolve_many(
            ("oc_group", "oc_topic", "oc_direct"),
            deadline=asyncio.get_running_loop().time() + 1,
        )

        self.assertEqual(labels["oc_group"].display_name, "Engineering")
        self.assertEqual(labels["oc_group"].chat_mode, "group")
        self.assertEqual(labels["oc_group"].chat_type, "private")
        self.assertEqual(labels["oc_topic"].chat_mode, "topic")
        self.assertEqual(labels["oc_direct"].display_name, "Alice")
        self.assertEqual(labels["oc_direct"].chat_mode, "p2p")
        self.assertEqual(labels["oc_direct"].p2p_target_open_id, "ou_alice")
        self.assertEqual(provider.member_calls, ["oc_direct"])

    async def test_p2p_member_failure_keeps_mode_and_falls_back(self) -> None:
        provider = FakeChatLabelProvider()
        provider.info["oc_direct"] = FakeChatInfo("", "p2p", "private")
        provider.member_errors.add("oc_direct")
        resolver = ChatLabelResolver(provider)
        self.addAsyncCleanup(resolver.aclose)

        labels = await resolver.resolve_many(
            ("oc_direct",),
            deadline=asyncio.get_running_loop().time() + 1,
        )

        label = labels["oc_direct"]
        self.assertFalse(label.resolved)
        self.assertEqual(label.chat_mode, "p2p")
        self.assertEqual(label.chat_type, "private")

    async def test_page_deduplication_and_positive_cache_avoid_repeat_calls(self) -> None:
        provider = FakeChatLabelProvider()
        provider.info["oc_group"] = FakeChatInfo("Engineering", "group")
        resolver = ChatLabelResolver(provider)
        self.addAsyncCleanup(resolver.aclose)
        deadline = asyncio.get_running_loop().time() + 1

        first = await resolver.resolve_many(
            ("oc_group", "oc_group"),
            deadline=deadline,
        )
        second = await resolver.resolve_many(
            ("oc_group",),
            deadline=asyncio.get_running_loop().time() + 1,
        )

        self.assertEqual(first, second)
        self.assertEqual(provider.info_calls, ["oc_group"])

    async def test_negative_cache_expires_without_persisting_fallback(self) -> None:
        now = [100.0]
        provider = FakeChatLabelProvider()
        provider.info["oc_missing"] = None
        resolver = ChatLabelResolver(
            provider,
            clock=lambda: now[0],
            negative_ttl_seconds=30,
        )
        self.addAsyncCleanup(resolver.aclose)

        first = await resolver.resolve_many(
            ("oc_missing",),
            deadline=asyncio.get_running_loop().time() + 1,
        )
        await resolver.resolve_many(
            ("oc_missing",),
            deadline=asyncio.get_running_loop().time() + 1,
        )
        now[0] += 31
        await resolver.resolve_many(
            ("oc_missing",),
            deadline=asyncio.get_running_loop().time() + 1,
        )

        self.assertFalse(first["oc_missing"].resolved)
        self.assertEqual(first["oc_missing"].display_name, "oc_missing")
        self.assertEqual(provider.info_calls, ["oc_missing", "oc_missing"])

    async def test_positive_cache_expires_and_refreshes_the_label(self) -> None:
        now = [100.0]
        provider = FakeChatLabelProvider()
        provider.info["oc_group"] = FakeChatInfo("Engineering", "group")
        resolver = ChatLabelResolver(
            provider,
            clock=lambda: now[0],
            positive_ttl_seconds=600,
        )
        self.addAsyncCleanup(resolver.aclose)

        first = await resolver.resolve_many(
            ("oc_group",),
            deadline=asyncio.get_running_loop().time() + 1,
        )
        provider.info["oc_group"] = FakeChatInfo("Renamed", "group")
        now[0] += 599
        cached = await resolver.resolve_many(
            ("oc_group",),
            deadline=asyncio.get_running_loop().time() + 1,
        )
        now[0] += 2
        refreshed = await resolver.resolve_many(
            ("oc_group",),
            deadline=asyncio.get_running_loop().time() + 1,
        )

        self.assertEqual(first["oc_group"].display_name, "Engineering")
        self.assertEqual(cached["oc_group"].display_name, "Engineering")
        self.assertEqual(refreshed["oc_group"].display_name, "Renamed")
        self.assertEqual(provider.info_calls, ["oc_group", "oc_group"])

    async def test_lru_capacity_evicts_the_least_recently_used_chat(self) -> None:
        provider = FakeChatLabelProvider()
        for chat_id in ("oc_a", "oc_b", "oc_c"):
            provider.info[chat_id] = FakeChatInfo(chat_id.upper(), "group")
        resolver = ChatLabelResolver(provider, capacity=2)
        self.addAsyncCleanup(resolver.aclose)

        for chat_ids in (("oc_a", "oc_b"), ("oc_a",), ("oc_c",), ("oc_b",)):
            await resolver.resolve_many(
                chat_ids,
                deadline=asyncio.get_running_loop().time() + 1,
            )

        self.assertEqual(provider.info_calls.count("oc_a"), 1)
        self.assertEqual(provider.info_calls.count("oc_b"), 2)
        self.assertEqual(provider.info_calls.count("oc_c"), 1)

    async def test_concurrent_callers_share_one_lookup(self) -> None:
        provider = FakeChatLabelProvider()
        provider.info["oc_group"] = FakeChatInfo("Engineering", "group")
        provider.gate = asyncio.Event()
        resolver = ChatLabelResolver(provider)
        self.addAsyncCleanup(resolver.aclose)
        deadline = asyncio.get_running_loop().time() + 1

        first = asyncio.create_task(
            resolver.resolve_many(("oc_group",), deadline=deadline)
        )
        second = asyncio.create_task(
            resolver.resolve_many(("oc_group",), deadline=deadline)
        )
        for _ in range(100):
            if provider.info_calls:
                break
            await asyncio.sleep(0)
        provider.gate.set()
        first_labels, second_labels = await asyncio.gather(first, second)

        self.assertEqual(provider.info_calls, ["oc_group"])
        self.assertEqual(first_labels, second_labels)

    async def test_lookup_concurrency_is_bounded(self) -> None:
        provider = FakeChatLabelProvider()
        chat_ids = tuple(f"oc_{index}" for index in range(5))
        provider.info = {
            chat_id: FakeChatInfo(chat_id, "group") for chat_id in chat_ids
        }
        provider.gate = asyncio.Event()
        resolver = ChatLabelResolver(provider, concurrency=2)
        self.addAsyncCleanup(resolver.aclose)
        request = asyncio.create_task(
            resolver.resolve_many(
                chat_ids,
                deadline=asyncio.get_running_loop().time() + 1,
            )
        )
        for _ in range(100):
            if len(provider.info_calls) == 2:
                break
            await asyncio.sleep(0)

        self.assertEqual(len(provider.info_calls), 2)
        self.assertEqual(provider.max_active, 2)
        provider.gate.set()
        await request
        self.assertEqual(len(provider.info_calls), 5)
        self.assertEqual(provider.max_active, 2)


if __name__ == "__main__":
    unittest.main()
