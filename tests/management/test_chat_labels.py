from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from netizen_cli.management import ChatLabelResolver


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
        self.member_gate: asyncio.Event | None = None
        self.info_started: asyncio.Queue[str] = asyncio.Queue()
        self.member_started: asyncio.Queue[str] = asyncio.Queue()
        self.clock = lambda: 0.0
        self.info_times: list[float] = []
        self.active = 0
        self.max_active = 0

    async def get_chat_info(self, chat_id: str):
        self.info_calls.append(chat_id)
        self.info_times.append(self.clock())
        self.info_started.put_nowait(chat_id)
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
        self.member_started.put_nowait(chat_id)
        if self.member_gate is not None:
            await self.member_gate.wait()
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

    async def test_default_success_ttls_for_group_topic_and_p2p(self) -> None:
        for mode, ttl in (("group", 1800), ("topic", 1800), ("p2p", 604800)):
            with self.subTest(mode=mode):
                now = [100.0]
                provider = FakeChatLabelProvider()
                provider.info["oc_chat"] = FakeChatInfo("Original", mode)
                provider.members["oc_chat"] = [FakeChatMember("ou_user", "Original")]
                resolver = ChatLabelResolver(provider, clock=lambda: now[0])
                self.addAsyncCleanup(resolver.aclose)

                first = await resolver.resolve_many(
                    ("oc_chat",), deadline=asyncio.get_running_loop().time() + 1,
                )
                provider.info["oc_chat"] = FakeChatInfo("Renamed", mode)
                provider.members["oc_chat"] = [FakeChatMember("ou_user", "Renamed")]
                now[0] += ttl - 1
                cached = await resolver.resolve_many(
                    ("oc_chat",), deadline=asyncio.get_running_loop().time() + 1,
                )
                self.assertEqual(cached, first)
                self.assertEqual(provider.info_calls, ["oc_chat"])
                now[0] += 1
                refreshed = await resolver.resolve_many(
                    ("oc_chat",), deadline=asyncio.get_running_loop().time() + 1,
                )
                self.assertEqual(refreshed["oc_chat"].display_name, "Renamed")
                self.assertEqual(provider.info_calls, ["oc_chat", "oc_chat"])

    async def test_p2p_failure_still_expires_after_thirty_seconds(self) -> None:
        now = [100.0]
        provider = FakeChatLabelProvider()
        provider.info["oc_direct"] = FakeChatInfo("", "p2p")
        provider.member_errors.add("oc_direct")
        resolver = ChatLabelResolver(provider, clock=lambda: now[0])
        self.addAsyncCleanup(resolver.aclose)
        failed = await resolver.resolve_many(
            ("oc_direct",), deadline=asyncio.get_running_loop().time() + 1,
        )
        provider.member_errors.clear()
        provider.members["oc_direct"] = [FakeChatMember("ou_user", "Recovered")]
        now[0] += 29
        cached = await resolver.resolve_many(
            ("oc_direct",), deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual(cached, failed)
        self.assertFalse(cached["oc_direct"].resolved)
        now[0] += 1
        recovered = await resolver.resolve_many(
            ("oc_direct",), deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual(recovered["oc_direct"].display_name, "Recovered")
        self.assertEqual(provider.member_calls, ["oc_direct", "oc_direct"])

    async def test_default_rate_limits_use_rolling_second_and_minute_windows(self) -> None:
        now = [100.25]
        provider = FakeChatLabelProvider()
        provider.clock = lambda: now[0]
        chat_ids = tuple(f"oc_{index}" for index in range(601))
        provider.info = {chat_id: FakeChatInfo("Group", "group") for chat_id in chat_ids}
        resolver = ChatLabelResolver(
            provider, clock=provider.clock, concurrency=1, lookup_budget_seconds=30,
        )
        self.addAsyncCleanup(resolver.aclose)

        async def advance(delay: float) -> None:
            now[0] += delay

        with patch("netizen_cli.management.chat_labels.asyncio.sleep", new=advance):
            labels = {}
            for offset in range(0, len(chat_ids), 100):
                labels.update(await resolver.resolve_many(
                    chat_ids[offset:offset + 100],
                    deadline=asyncio.get_running_loop().time() + 30,
                ))

        self.assertTrue(all(label.resolved for label in labels.values()))
        self.assertEqual(len(provider.info_calls), 601)
        self.assertEqual(provider.info_times[:30], [100.25] * 30)
        self.assertEqual(provider.info_times[30], 101.25)
        self.assertEqual(provider.info_times[599], 119.25)
        self.assertEqual(provider.info_times[600], 160.25)
        for index, started in enumerate(provider.info_times):
            if index >= 30:
                self.assertGreaterEqual(started - provider.info_times[index - 30], 1)
            if index >= 600:
                self.assertGreaterEqual(started - provider.info_times[index - 600], 60)

    async def test_rate_budget_is_shared_and_counts_chats_not_member_calls_or_cache_hits(self) -> None:
        now = [100.25]
        provider = FakeChatLabelProvider()
        provider.info = {
            "oc_direct": FakeChatInfo("", "p2p"),
            "oc_group": FakeChatInfo("Group", "group"),
            "oc_next": FakeChatInfo("Next", "group"),
        }
        provider.members["oc_direct"] = [FakeChatMember("ou_user", "User")]
        provider.gate = asyncio.Event()
        resolver = ChatLabelResolver(
            provider, clock=lambda: now[0], lookups_per_second=1, lookups_per_minute=2,
        )
        self.addAsyncCleanup(resolver.aclose)
        waits: asyncio.Queue[tuple[float, asyncio.Future[None]]] = asyncio.Queue()

        async def wait_for_advance(delay: float) -> None:
            gate = asyncio.get_running_loop().create_future()
            waits.put_nowait((delay, gate))
            await gate

        loop = asyncio.get_running_loop()
        with patch("netizen_cli.management.chat_labels.asyncio.sleep", new=wait_for_advance):
            first = asyncio.create_task(resolver.resolve_many(("oc_direct", "oc_direct"), deadline=loop.time() + 5))
            self.assertEqual(await asyncio.wait_for(provider.info_started.get(), 1), "oc_direct")
            duplicate = asyncio.create_task(resolver.resolve_many(("oc_direct",), deadline=loop.time() + 5))
            second = asyncio.create_task(resolver.resolve_many(("oc_group",), deadline=loop.time() + 5))
            delay, gate = await asyncio.wait_for(waits.get(), 1)
            self.assertEqual(delay, 1)
            provider.gate.set()
            first_labels, duplicate_labels = await asyncio.gather(first, duplicate)
            self.assertEqual(first_labels, duplicate_labels)
            self.assertEqual(provider.member_calls, ["oc_direct"])
            self.assertEqual(provider.info_calls, ["oc_direct"])
            self.assertEqual(await resolver.resolve_many(("oc_direct",), deadline=loop.time() + 1), first_labels)
            now[0] += delay
            gate.set_result(None)
            self.assertTrue((await second)["oc_group"].resolved)

            third = asyncio.create_task(resolver.resolve_many(("oc_next",), deadline=loop.time() + 5))
            delay, gate = await asyncio.wait_for(waits.get(), 1)
            self.assertEqual(delay, 59)
            now[0] += delay
            gate.set_result(None)
            self.assertTrue((await third)["oc_next"].resolved)
        self.assertEqual(provider.info_calls, ["oc_direct", "oc_group", "oc_next"])

    async def test_rate_wait_expires_without_starting_or_spending_a_chat(self) -> None:
        now = [100.25]
        provider = FakeChatLabelProvider()
        provider.info = {chat_id: FakeChatInfo("Group", "group") for chat_id in ("oc_first", "oc_waiting", "oc_next")}
        resolver = ChatLabelResolver(
            provider, clock=lambda: now[0], lookups_per_second=1, lookups_per_minute=2,
        )
        self.addAsyncCleanup(resolver.aclose)
        loop = asyncio.get_running_loop()
        await resolver.resolve_many(("oc_first",), deadline=loop.time() + 1)
        waiting = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait_until_cancelled(_delay: float) -> None:
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch("netizen_cli.management.chat_labels.asyncio.sleep", new=wait_until_cancelled):
            request = asyncio.create_task(resolver.resolve_many(("oc_waiting",), deadline=loop.time() + 0.2))
            await asyncio.wait_for(waiting.wait(), 1)
            labels = await request
            await asyncio.wait_for(cancelled.wait(), 1)
        self.assertFalse(labels["oc_waiting"].resolved)
        self.assertEqual(provider.info_calls, ["oc_first"])
        now[0] += 1
        labels = await resolver.resolve_many(("oc_next",), deadline=loop.time() + 1)
        self.assertTrue(labels["oc_next"].resolved)
        self.assertEqual(provider.info_calls, ["oc_first", "oc_next"])

    async def test_simultaneous_rate_waiters_recheck_the_shared_allowance(self) -> None:
        now = [100.25]
        provider = FakeChatLabelProvider()
        provider.info = {chat_id: FakeChatInfo("Group", "group") for chat_id in ("oc_first", "oc_a", "oc_b")}
        resolver = ChatLabelResolver(provider, clock=lambda: now[0], lookups_per_second=1)
        self.addAsyncCleanup(resolver.aclose)
        loop = asyncio.get_running_loop()
        await resolver.resolve_many(("oc_first",), deadline=loop.time() + 1)
        waits: asyncio.Queue[tuple[float, asyncio.Future[None]]] = asyncio.Queue()

        async def wait_for_advance(delay: float) -> None:
            gate = loop.create_future()
            waits.put_nowait((delay, gate))
            await gate

        with patch("netizen_cli.management.chat_labels.asyncio.sleep", new=wait_for_advance):
            requests = [asyncio.create_task(resolver.resolve_many((chat_id,), deadline=loop.time() + 5)) for chat_id in ("oc_a", "oc_b")]
            first_delay, first_gate = await asyncio.wait_for(waits.get(), 1)
            second_delay, second_gate = await asyncio.wait_for(waits.get(), 1)
            self.assertEqual((first_delay, second_delay), (1, 1))
            now[0] += 1
            first_gate.set_result(None)
            second_gate.set_result(None)
            delay, gate = await asyncio.wait_for(waits.get(), 1)
            self.assertEqual(delay, 1)
            await asyncio.wait_for(provider.info_started.get(), 1)  # oc_first
            self.assertIn(await asyncio.wait_for(provider.info_started.get(), 1), ("oc_a", "oc_b"))
            self.assertEqual(len(provider.info_calls), 2)
            now[0] += 1
            gate.set_result(None)
            labels = await asyncio.gather(*requests)
        self.assertTrue(all(label.resolved for page in labels for label in page.values()))
        self.assertCountEqual(provider.info_calls, ["oc_first", "oc_a", "oc_b"])

    async def test_member_lookup_keeps_chat_slot_and_queued_chat_expires(self) -> None:
        provider = FakeChatLabelProvider()
        provider.info = {
            "oc_direct": FakeChatInfo("", "p2p"),
            "oc_waiting": FakeChatInfo("Waiting", "group"),
            "oc_next": FakeChatInfo("Next", "group"),
        }
        provider.members["oc_direct"] = [FakeChatMember("ou_user", "User")]
        provider.member_gate = asyncio.Event()
        resolver = ChatLabelResolver(provider, concurrency=1)
        self.addAsyncCleanup(resolver.aclose)
        loop = asyncio.get_running_loop()
        first = asyncio.create_task(resolver.resolve_many(("oc_direct",), deadline=loop.time() + 5))
        self.assertEqual(await asyncio.wait_for(provider.member_started.get(), 1), "oc_direct")
        queued = await resolver.resolve_many(("oc_waiting",), deadline=loop.time() + 0.1)
        self.assertFalse(queued["oc_waiting"].resolved)
        self.assertEqual(provider.info_calls, ["oc_direct"])
        provider.member_gate.set()
        self.assertTrue((await first)["oc_direct"].resolved)
        self.assertTrue((await resolver.resolve_many(("oc_next",), deadline=loop.time() + 1))["oc_next"].resolved)
        self.assertEqual(provider.info_calls, ["oc_direct", "oc_next"])

    async def test_failed_lookup_spends_allowance_and_close_cancels_rate_wait(self) -> None:
        provider = FakeChatLabelProvider()
        provider.info["oc_waiting"] = FakeChatInfo("Waiting", "group")
        resolver = ChatLabelResolver(provider, lookups_per_second=1, lookups_per_minute=1)
        self.addAsyncCleanup(resolver.aclose)
        loop = asyncio.get_running_loop()
        failed = await resolver.resolve_many(("oc_missing",), deadline=loop.time() + 1)
        self.assertFalse(failed["oc_missing"].resolved)
        waiting = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait_until_cancelled(_delay: float) -> None:
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch("netizen_cli.management.chat_labels.asyncio.sleep", new=wait_until_cancelled):
            request = asyncio.create_task(resolver.resolve_many(("oc_waiting",), deadline=loop.time() + 5))
            await asyncio.wait_for(waiting.wait(), 1)
            await asyncio.wait_for(resolver.aclose(), 1)
            self.assertTrue(cancelled.is_set())
            self.assertFalse((await request)["oc_waiting"].resolved)
        self.assertEqual(provider.info_calls, ["oc_missing"])

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
