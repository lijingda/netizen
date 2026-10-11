from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from openai_codex.generated.v2_all import ThreadItem, TurnStatus

from scripts import probe_skill_execution as probe


class SkillExecutionProofTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.fixture = probe._fixture(self.directory)

    def _turn(self, *, token: str | None = None, output: bool = True):
        items = [ThreadItem.model_validate({
            "type": "agentMessage", "id": "answer", "phase": "final_answer",
            "text": self.fixture.expected,
        })]
        if output:
            items.insert(0, ThreadItem.model_validate({
                "type": "commandExecution", "id": "read-resource", "command": "cat token.txt",
                "commandActions": [], "cwd": str(self.fixture.cwd), "exitCode": 0,
                "status": "completed", "aggregatedOutput": token or self.fixture.token,
            }))
        return SimpleNamespace(id="own-turn", status=TurnStatus.completed, items=items)

    def test_proof_requires_final_and_successful_resource_read_in_same_turn(self) -> None:
        self.assertEqual(probe._verify(self._turn(), self.fixture)["turn_id"], "own-turn")
        with self.assertRaisesRegex(AssertionError, "no successful command"):
            probe._verify(self._turn(output=False), self.fixture)
        with self.assertRaisesRegex(AssertionError, "no successful command"):
            probe._verify(self._turn(token="STALE-RESOURCE"), self.fixture)
        failed_read = self._turn()
        failed_read.items[0].root.exit_code = 1
        with self.assertRaisesRegex(AssertionError, "no successful command"):
            probe._verify(failed_read, self.fixture)
        stale_answer = self._turn()
        self.fixture.rotate()
        with self.assertRaisesRegex(AssertionError, "final does not match"):
            probe._verify(stale_answer, self.fixture)

    def test_catalog_and_requests_cannot_reveal_expected_answer(self) -> None:
        catalog = self.fixture.skill_path.read_text().split("---", 2)[1]
        for marker in (self.fixture.body_marker, self.fixture.token):
            self.assertNotIn(marker, catalog)
            self.assertNotIn(marker, self.fixture.prompt)
        previous = self.fixture.token
        self.fixture.rotate()
        self.assertNotEqual(self.fixture.token, previous)
        self.assertNotIn(self.fixture.token, self.fixture.skill_path.read_text())

    def test_isolation_uses_same_cwd_name_and_request_but_distinct_answers(self) -> None:
        other = probe._fixture(self.directory / "other", other=self.fixture)
        self.assertEqual(other.cwd, self.fixture.cwd)
        self.assertEqual(other.name, self.fixture.name)
        self.assertEqual(other.prompt, self.fixture.prompt)
        self.assertNotEqual(other.roots, self.fixture.roots)
        self.assertNotEqual(other.expected, self.fixture.expected)

    def test_trust_uses_exact_inline_toml_path_with_dots(self) -> None:
        self.fixture.cwd = self.directory / "a.b" / 'quoted"project'
        config = probe._config(self.fixture, "test-model")
        trust = config.config_overrides[0]
        self.assertEqual(trust.split("=", 1)[0], "projects")
        parsed = tomllib.loads(trust)
        self.assertEqual(parsed, {"projects": {str(self.fixture.cwd): {"trust_level": "trusted"}}})


class SkillExecutionCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_side_optional_parent_metadata_and_own_cleanup_boundary(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        fixture = probe._fixture(directory)
        cases = (
            ("missing-parent", None, "side", True, None),
            ("matched-parent", "parent", "side", True, None),
            ("wrong-parent", "unrelated", "side", True, "parent identity"),
            ("wrong-read-id", None, "unrelated", True, "different Thread identity"),
            ("not-ephemeral", None, "side", False, "ephemeral identity"),
            ("read-failed", None, "side", True, "read failed"),
            ("cleanup-failed", None, "side", True, "cleanup failed"),
        )
        for name, parent_id, read_id, ephemeral, failure in cases:
            with self.subTest(name=name):
                native = SimpleNamespace(id=read_id, ephemeral=ephemeral, forked_from_id=parent_id)
                fork = SimpleNamespace(
                    id="side", read=AsyncMock(return_value=SimpleNamespace(thread=native)),
                    turn=AsyncMock(return_value=SimpleNamespace(run=AsyncMock(return_value=object()))),
                )
                if name == "read-failed":
                    fork.read.side_effect = RuntimeError("read failed")
                codex = SimpleNamespace(thread_fork=AsyncMock(return_value=fork))
                cleanup = SimpleNamespace(clean_thread=AsyncMock())
                if name == "cleanup-failed":
                    cleanup.clean_thread.side_effect = RuntimeError("cleanup failed")
                subscription = SimpleNamespace(unsubscribe=AsyncMock(
                    return_value=SimpleNamespace(value="unsubscribed"),
                ))
                boundary = SimpleNamespace(inject_boundary=AsyncMock())
                proof = Mock(return_value={"verified": True})

                @asynccontextmanager
                async def server(_fixture, _args):
                    yield codex

                evidence = {"owned_threads": {}}
                with (
                    patch.object(probe, "_server", server),
                    patch.object(probe, "_start", AsyncMock(return_value=SimpleNamespace(id="parent"))),
                    patch.object(probe, "_turn", AsyncMock(return_value=SimpleNamespace(status="completed"))),
                    patch.object(probe, "_final_response_from_turn", return_value="READY"),
                    patch.object(probe, "_verify", proof),
                    patch.object(probe, "AppServerSideBoundaryControl", return_value=boundary),
                    patch.object(probe, "PinnedExperimentalTerminalCleanup", return_value=cleanup),
                    patch.object(probe, "AppServerThreadSubscriptionControl", return_value=subscription),
                ):
                    if failure:
                        with self.assertRaisesRegex((AssertionError, RuntimeError), failure):
                            await probe._scenario(
                                "side", fixture, argparse.Namespace(model=None), evidence, lambda: None,
                            )
                    else:
                        await probe._scenario(
                            "side", fixture, argparse.Namespace(model=None), evidence, lambda: None,
                        )
                cleanup.clean_thread.assert_awaited_once_with("side")
                subscription.unsubscribe.assert_awaited_once_with("side")
                self.assertEqual(evidence["side_unsubscribe"], "unsubscribed")
                if failure and name != "cleanup-failed":
                    boundary.inject_boundary.assert_not_awaited()
                    proof.assert_not_called()
                else:
                    self.assertEqual(
                        evidence["fork_parent_field"], "matched" if parent_id else "missing",
                    )
                    boundary.inject_boundary.assert_awaited_once_with("side")
                    proof.assert_called_once()

    async def test_cleanup_only_archives_created_ids_once_in_reverse_order(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        fixture = probe._fixture(directory)
        codex = SimpleNamespace(thread_archive=AsyncMock(side_effect=[TimeoutError(), None]))

        @asynccontextmanager
        async def server(_config):
            yield codex

        evidence = {"owned_threads": {
            "root": "created", "fork": "created", "previous-unknown": "archive_unknown",
        }}
        with patch.object(probe, "_codex", server):
            await probe._cleanup(fixture, argparse.Namespace(model=None), evidence, lambda: None)
            await probe._cleanup(fixture, argparse.Namespace(model=None), evidence, lambda: None)
        self.assertEqual(
            [call.args[0] for call in codex.thread_archive.await_args_list], ["fork", "root"],
        )
        self.assertEqual(evidence["owned_threads"], {
            "root": "archive_acknowledged", "fork": "archive_unknown",
            "previous-unknown": "archive_unknown",
        })
