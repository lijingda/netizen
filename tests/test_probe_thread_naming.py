from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from scripts import probe_thread_naming as probe


class NamingProbeTest(unittest.IsolatedAsyncioTestCase):
    def test_title_requires_inherited_identifier_and_no_tool_items(self) -> None:
        result = SimpleNamespace(
            status="completed", final_response='{"title":"NAMING-unique 自动命名"}',
            items=[SimpleNamespace(root=SimpleNamespace(type="agentMessage"))],
        )
        self.assertEqual(
            probe._validate_title(result, "NAMING-unique"),
            ("NAMING-unique 自动命名", ["agentMessage"]),
        )
        for title, kind in (
            ("自动命名", "agentMessage"),
            ("NAMING-unique\n自动命名", "agentMessage"),
            ("NAMING-unique " + "a" * 120, "agentMessage"),
            ("NAMING-unique 自动命名", "commandExecution"),
            ("NAMING-unique 自动命名", "mcpToolCall"),
        ):
            with self.subTest(title=title, kind=kind):
                result.final_response = json.dumps({"title": title})
                result.items = [SimpleNamespace(root=SimpleNamespace(type=kind))]
                with self.assertRaises(AssertionError):
                    probe._validate_title(result, "NAMING-unique")

    def test_title_requires_valid_structured_output(self) -> None:
        for response in (
            None, 'NAMING-unique 自动命名', '{"title":', '[]', '{}',
            '{"title":null}', '{"title":"NAMING-unique","extra":true}',
        ):
            with self.subTest(response=response):
                result = SimpleNamespace(status="completed", final_response=response, items=[])
                with self.assertRaises(AssertionError):
                    probe._validate_title(result, "NAMING-unique")

    async def test_active_fork_is_interrupted_and_drained_before_unsubscribe(self) -> None:
        events = []
        terminal = asyncio.Event()

        async def consume():
            await terminal.wait()
            events.append("terminal")
            return SimpleNamespace(status="interrupted")

        async def interrupt():
            events.append("interrupt")
            terminal.set()

        async def clean(_thread_id):
            events.append("cleanup")

        async def unsubscribe(_thread_id):
            events.append("unsubscribe")
            return SimpleNamespace(value="unsubscribed")

        task = asyncio.create_task(consume())
        result = await probe._release_fork(
            SimpleNamespace(id="probe-fork"),
            SimpleNamespace(interrupt=interrupt), task,
            SimpleNamespace(clean_thread=clean),
            SimpleNamespace(unsubscribe=unsubscribe),
        )
        self.assertEqual(events, ["interrupt", "terminal", "cleanup", "unsubscribe"])
        self.assertEqual(result["terminal_status_during_cleanup"], "interrupted")
        self.assertEqual(result["unsubscribe_status"], "unsubscribed")

    async def test_cleanup_failure_still_releases_subscription(self) -> None:
        cleanup = SimpleNamespace(clean_thread=AsyncMock(side_effect=RuntimeError("offline")))
        subscription = SimpleNamespace(
            unsubscribe=AsyncMock(return_value=SimpleNamespace(value="unsubscribed")),
        )
        with self.assertRaisesRegex(AssertionError, "terminal-cleanup"):
            await probe._release_fork(
                SimpleNamespace(id="probe-fork"), None, None, cleanup, subscription,
            )
        subscription.unsubscribe.assert_awaited_once_with("probe-fork")

    async def test_interruption_probe_releases_fork_when_validation_fails(self) -> None:
        fork = SimpleNamespace(id="probe-fork", turn=AsyncMock())
        codex = SimpleNamespace(thread_fork=AsyncMock(return_value=fork))
        cleanup, subscription = object(), object()
        with (
            patch.object(probe, "_prove_ephemeral", new=AsyncMock(side_effect=ValueError("shape"))),
            patch.object(probe, "_release_fork", new=AsyncMock()) as release,
        ):
            with self.assertRaisesRegex(ValueError, "shape"):
                await probe._interrupted_fork(codex, "parent", cleanup, subscription)
        release.assert_awaited_once_with(fork, None, None, cleanup, subscription)
        fork.turn.assert_not_awaited()

    async def test_default_probe_runs_production_path_and_interruption_check(self) -> None:
        client = AsyncMock()
        with (
            patch.object(probe, "facade_migration_requirements", return_value=()),
            patch.object(probe, "AsyncCodex", return_value=client),
            patch.object(probe, "_runtime_scenario", new=AsyncMock(return_value={})) as scenario,
        ):
            for runtime_only in (False, True):
                await probe.probe("fixture-cwd", runtime_only=runtime_only)
                scenario.assert_awaited_with(
                    client.__aenter__.return_value, "fixture-cwd", model=None,
                    check_interruption=not runtime_only,
                )


if __name__ == "__main__":
    unittest.main()
