from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import tomllib
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen.turn_patch_children import TaskPatchChildren, TurnPatchBatch
from scripts import probe_child_files


class ChildFilesProbeHistoryTest(unittest.TestCase):
    def test_child_owned_public_view_and_inherited_view_both_keep_parent_patches_out(
        self,
    ) -> None:
        for native_ids in (("child-turn",), ("seed", "current", "child-turn")):
            with self.subTest(native_ids=native_ids):
                child = SimpleNamespace(
                    parent_thread_id="root", forked_from_id="root",
                    source=SimpleNamespace(root=None),
                    turns=[SimpleNamespace(id=turn_id) for turn_id in native_ids],
                )
                batch = TurnPatchBatch("child", "child-turn", Path("/probe"), ())
                inherited = probe_child_files._verify_child_history(
                    child, "root", {"seed", "current"}, TaskPatchChildren((batch,))
                )
                self.assertEqual(inherited, sorted(set(native_ids) & {"seed", "current"}))
                for invalid_id in ("seed", "current"):
                    with self.assertRaisesRegex(AssertionError, "parent Turn leaked"):
                        probe_child_files._verify_child_history(
                            child, "root", {"seed", "current"},
                            TaskPatchChildren((
                                TurnPatchBatch("child", invalid_id, Path("/probe"), ()),
                            )),
                        )
                child.forked_from_id = "another-root"
                with self.assertRaisesRegex(AssertionError, "fork and parent provenance"):
                    probe_child_files._verify_child_history(
                        child, "root", {"seed", "current"}, TaskPatchChildren((batch,))
                    )


class ChildFilesProbeCleanupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.cwd = directory / "fixture"
        self.cwd.mkdir()
        self.config_path = directory / "config.toml"
        self.original_config = (
            '# keep user formatting\nmodel = "test"\n'
            '[projects."/existing"]\ntrust_level = "trusted"\n'
        )
        self.config_path.write_text(self.original_config)
        self.enterContext(patch.object(
            probe_child_files, "_user_config_path", return_value=self.config_path,
        ))
        self.enterContext(patch.object(
            probe_child_files.tempfile, "TemporaryDirectory",
            return_value=nullcontext(str(self.cwd)),
        ))
        self.codex = AsyncMock()
        self.codex.__aenter__.return_value = self.codex
        self.enterContext(patch.object(probe_child_files, "AsyncCodex", return_value=self.codex))

    def _add_fixture_trust(self, *, extra: str = "") -> None:
        with self.config_path.open("a") as config:
            config.write(
                f'\n[projects.{json.dumps(str(self.cwd))}]\ntrust_level = "trusted"\n'
                + extra
            )

    async def _run(self, scenario):
        with patch.object(probe_child_files, "_scenario", side_effect=scenario):
            return await probe_child_files.probe(argparse.Namespace(model=None, timeout=5))

    async def test_archive_delegates_the_owned_subtree_once_even_after_failure(self) -> None:
        for scenario_fails, archive_fails in (
            (False, False), (False, True), (True, False), (True, True)
        ):
            with self.subTest(scenario_fails=scenario_fails, archive_fails=archive_fails):
                self.config_path.write_text(self.original_config)
                self.codex.thread_archive.reset_mock()
                self.codex.thread_archive.side_effect = (
                    RuntimeError("response lost") if archive_fails else None
                )
                unrelated = (
                    '\n# concurrent user edit\n[projects."/unknown-project"]\n'
                    'trust_level = "trusted"\n[mcp_servers.user]\nurl = "https://example.test"\n'
                )

                async def scenario(client, cwd, args, evidence, owned):
                    self.assertEqual(cwd, self.cwd)
                    self._add_fixture_trust()
                    with self.config_path.open("a") as config:
                        config.write(unrelated)
                    evidence["root_thread_id"] = "owned-root"
                    owned.update(("owned-root", "owned-child", "owned-grandchild"))
                    if scenario_fails:
                        raise AssertionError("probe evidence incomplete")

                result = await self._run(scenario)

                self.codex.thread_archive.assert_awaited_once_with("owned-root")
                self.assertTrue(result["fixture_trust_cleaned"])
                self.assertEqual(self.config_path.read_text(), self.original_config + "\n" + unrelated)
                self.assertEqual(
                    result["status"],
                    "failed" if scenario_fails or archive_fails else "passed",
                )
                self.assertEqual(len(result["cleanup"]), 1)
                self.assertEqual(
                    result["cleanup"][0]["status"],
                    "unknown" if archive_fails else "archive_acknowledged",
                )

    async def test_initialization_and_client_close_failures_still_remove_fixture_trust(self) -> None:
        for stage in ("initialize", "close"):
            with self.subTest(stage=stage):
                self.config_path.write_text(self.original_config)
                self.codex.__aenter__.side_effect = None
                self.codex.__aexit__.side_effect = None

                async def fail(*args):
                    self._add_fixture_trust()
                    raise RuntimeError("private failure text")

                method = self.codex.__aenter__ if stage == "initialize" else self.codex.__aexit__
                method.side_effect = fail
                result = await self._run(AsyncMock())
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["fixture_trust_cleaned"])
                self.assertNotIn("private", json.dumps(result))
                self.assertEqual(tomllib.loads(self.config_path.read_text()), tomllib.loads(self.original_config))

    async def test_cancellation_at_any_client_stage_still_removes_fixture_trust(self) -> None:
        for stage in ("initialize", "scenario", "archive", "close"):
            with self.subTest(stage=stage):
                self.config_path.write_text(self.original_config)
                for method in (self.codex.__aenter__, self.codex.__aexit__, self.codex.thread_archive):
                    method.side_effect = None

                async def cancel(*args):
                    if stage == "initialize":
                        self._add_fixture_trust()
                    raise asyncio.CancelledError()

                async def scenario(client, cwd, args, evidence, owned):
                    self._add_fixture_trust()
                    evidence["root_thread_id"] = "owned-root"
                    owned.add("owned-root")
                    if stage == "scenario":
                        raise asyncio.CancelledError()

                if stage != "scenario":
                    {"initialize": self.codex.__aenter__, "archive": self.codex.thread_archive,
                     "close": self.codex.__aexit__}[stage].side_effect = cancel
                with self.assertRaises(asyncio.CancelledError):
                    await self._run(scenario)
                self.assertEqual(tomllib.loads(self.config_path.read_text()), tomllib.loads(self.original_config))

    async def test_existing_fixture_configuration_is_preserved(self) -> None:
        self._add_fixture_trust()
        before = self.config_path.read_bytes()
        result = await self._run(AsyncMock())
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["fixture_trust_cleaned"])
        self.assertEqual(self.config_path.read_bytes(), before)

    async def test_unexpected_fixture_configuration_fails_without_removing_user_data(self) -> None:
        async def scenario(*args):
            self._add_fixture_trust(extra='user_setting = "private-value"\n')

        result = await self._run(scenario)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["fixture_trust_cleanup_error"], {"type": "ProbeFailure"})
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(
            tomllib.loads(self.config_path.read_text())["projects"][str(self.cwd)],
            {"trust_level": "trusted", "user_setting": "private-value"},
        )
