from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from scripts import probe_python_sdk


class ProcessProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_usage_probe_retries_transient_initial_active_read(self) -> None:
        class Handle:
            id = "turn-1"
            thread_id = "thread-1"

            async def stream(self):
                yield SimpleNamespace(
                    payload=SimpleNamespace(
                        thread_id="thread-1",
                        turn_id="turn-1",
                        token_usage=SimpleNamespace(
                            last=SimpleNamespace(total_tokens=17),
                            model_context_window=128_000,
                        ),
                    )
                )

        handle = Handle()
        active = SimpleNamespace(
            thread=SimpleNamespace(
                status=SimpleNamespace(root=SimpleNamespace(type="active")),
                turns=[SimpleNamespace(id="turn-1", status="inProgress")],
            )
        )
        thread = SimpleNamespace(
            id="thread-1",
            turn=AsyncMock(return_value=handle),
            read=AsyncMock(
                side_effect=(
                    probe_python_sdk.InternalRpcError(-32603, "rollout is empty"),
                    active,
                )
            ),
        )
        codex = SimpleNamespace(thread_start=AsyncMock(return_value=thread))

        with (
            patch.object(
                probe_python_sdk,
                "PinnedExperimentalTerminalCleanup",
                return_value=AsyncMock(),
            ),
            patch.object(
                probe_python_sdk,
                "_public_terminal_turn",
                new=AsyncMock(return_value=SimpleNamespace(status="completed")),
            ),
            patch.object(
                probe_python_sdk,
                "ThreadTokenUsageUpdatedNotification",
                SimpleNamespace,
            ),
            patch.object(
                probe_python_sdk.asyncio,
                "sleep",
                new=AsyncMock(),
            ),
        ):
            result = await probe_python_sdk._context_usage(
                codex,
                Path("/project"),
            )

        self.assertEqual(thread.read.await_count, 2)
        self.assertTrue(result["observed_exact_active"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["used_tokens"], 17)
        self.assertEqual(result["model_context_window"], 128_000)

    async def test_exact_thread_lookup_paginates_with_explicit_archive_filter(
        self,
    ) -> None:
        codex = AsyncMock()
        codex.thread_list.side_effect = [
            SimpleNamespace(
                data=[SimpleNamespace(id="other")],
                next_cursor="page-two",
            ),
            SimpleNamespace(
                data=[SimpleNamespace(id="target", name="Archived")],
                next_cursor=None,
            ),
        ]

        found = await probe_python_sdk._find_listed_thread(
            codex,
            "target",
            archived=True,
        )

        self.assertEqual(found.id, "target")
        self.assertEqual(
            codex.thread_list.await_args_list,
            [
                call(archived=True, cursor=None, limit=100),
                call(
                    archived=True,
                    cursor="page-two",
                    limit=100,
                ),
            ],
        )

    async def test_lifecycle_probe_uses_public_lifecycle_and_thin_delete(
        self,
    ) -> None:
        thread = SimpleNamespace(
            id="thread-1",
            turn=AsyncMock(return_value=SimpleNamespace(id="turn-1")),
            set_name=AsyncMock(),
        )
        codex = SimpleNamespace(
            thread_start=AsyncMock(return_value=thread),
            thread_archive=AsyncMock(),
            thread_unarchive=AsyncMock(
                return_value=SimpleNamespace(id="thread-1")
            ),
        )
        visible = AsyncMock(
            side_effect=(
                SimpleNamespace(id="thread-1", name="Netizen lifecycle probe 7"),
                SimpleNamespace(id="thread-1", name="Netizen lifecycle probe 7"),
                None,
                SimpleNamespace(id="thread-1", name="Netizen lifecycle probe 7"),
                None,
                None,
                None,
                None,
                None,
            )
        )
        delete = AsyncMock()
        archived_delete = {
            "thread_id": "thread-archived",
            "turn_id": "turn-archived",
            "archived_before_delete": True,
            "delete_acknowledged": True,
            "delete_absent_from_scan_and_state_db": True,
        }
        running_delete = {
            "thread_id": "thread-running",
            "turn_id": "turn-running",
            "running_marker_pids": [123],
            "deleted_without_interrupt_cleanup_or_idle_read": True,
            "orphan_pids": [],
            "delete_acknowledged": True,
            "delete_absent_from_scan_and_state_db": True,
        }
        with (
            patch.object(
                probe_python_sdk,
                "_public_terminal_turn",
                new=AsyncMock(return_value=SimpleNamespace(status="completed")),
            ),
            patch.object(
                probe_python_sdk,
                "_wait_for_thread_visibility",
                new=visible,
            ),
            patch.object(
                probe_python_sdk,
                "AppServerThreadDeleteControl",
                return_value=SimpleNamespace(delete=delete),
            ),
            patch.object(
                probe_python_sdk,
                "_archived_thread_delete_live",
                new=AsyncMock(return_value=archived_delete),
            ) as archived_live,
            patch.object(
                probe_python_sdk,
                "_running_thread_delete_live",
                new=AsyncMock(return_value=running_delete),
            ) as running_live,
            patch.object(probe_python_sdk.time, "time_ns", return_value=7),
        ):
            result = await probe_python_sdk._thread_lifecycle_live(
                codex,
                Path("/project"),
            )

        self.assertEqual(
            result,
            {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "name": "Netizen lifecycle probe 7",
                "rename_visible": True,
                "archive_visible": True,
                "unarchive_restored_same_id": True,
                "delete_acknowledged": True,
                "delete_absent_from_scan_and_state_db": True,
                "archived_delete": archived_delete,
                "running_delete": running_delete,
            },
        )
        thread.set_name.assert_awaited_once_with("Netizen lifecycle probe 7")
        codex.thread_archive.assert_awaited_once_with("thread-1")
        codex.thread_unarchive.assert_awaited_once_with("thread-1")
        delete.assert_awaited_once_with("thread-1")
        archived_live.assert_awaited_once()
        running_live.assert_awaited_once()
        self.assertEqual(
            [item.kwargs.get("use_state_db_only") for item in visible.await_args_list[-4:]],
            [False, False, True, True],
        )

    async def test_archived_delete_live_deletes_without_unarchive(self) -> None:
        handle = SimpleNamespace(id="turn-archived")
        thread = SimpleNamespace(
            id="thread-archived",
            turn=AsyncMock(return_value=handle),
        )
        codex = SimpleNamespace(
            thread_start=AsyncMock(return_value=thread),
            thread_archive=AsyncMock(),
        )
        delete = AsyncMock()
        visible = AsyncMock(
            side_effect=(SimpleNamespace(id=thread.id), None, None, None, None, None)
        )

        with (
            patch.object(
                probe_python_sdk,
                "_public_terminal_turn",
                new=AsyncMock(return_value=SimpleNamespace(status="completed")),
            ),
            patch.object(
                probe_python_sdk,
                "_wait_for_thread_visibility",
                new=visible,
            ),
        ):
            result = await probe_python_sdk._archived_thread_delete_live(
                codex,
                Path("/project"),
                SimpleNamespace(delete=delete),
            )

        self.assertTrue(result["archived_before_delete"])
        codex.thread_archive.assert_awaited_once_with(thread.id)
        delete.assert_awaited_once_with(thread.id)
        self.assertFalse(hasattr(codex, "thread_unarchive"))

    async def test_running_delete_live_delegates_without_local_quiescence(
        self,
    ) -> None:
        handle = SimpleNamespace(
            id="turn-running",
            thread_id="thread-running",
            interrupt=AsyncMock(),
        )
        thread = SimpleNamespace(
            id="thread-running",
            turn=AsyncMock(return_value=handle),
        )
        codex = SimpleNamespace(thread_start=AsyncMock(return_value=thread))
        delete = AsyncMock()
        prove_absent = AsyncMock()
        wait_for_process = AsyncMock(side_effect=([123], []))
        wait_for_visibility = AsyncMock(return_value=SimpleNamespace(id=thread.id))

        with (
            patch.object(
                probe_python_sdk,
                "_wait_for_thread_visibility",
                new=wait_for_visibility,
            ),
            patch.object(
                probe_python_sdk,
                "_wait_for_process",
                new=wait_for_process,
            ),
            patch.object(
                probe_python_sdk,
                "_matching_processes",
                return_value=[],
            ),
            patch.object(
                probe_python_sdk,
                "_prove_thread_absent_from_all_catalogs",
                new=prove_absent,
            ),
            patch.object(probe_python_sdk.time, "time_ns", return_value=9),
        ):
            result = await probe_python_sdk._running_thread_delete_live(
                codex,
                Path("/project"),
                SimpleNamespace(delete=delete),
            )

        self.assertEqual(result["running_marker_pids"], [123])
        self.assertTrue(result["deleted_without_interrupt_cleanup_or_idle_read"])
        handle.interrupt.assert_not_awaited()
        delete.assert_awaited_once_with(thread.id)
        prove_absent.assert_awaited_once_with(codex, thread.id)
        self.assertEqual(wait_for_process.await_count, 2)
        wait_for_visibility.assert_awaited_once_with(
            codex,
            thread.id,
            archived=False,
            present=True,
        )

    def test_matching_processes_requires_exact_argv0(self) -> None:
        marker = "netizen-exact-marker"
        with tempfile.TemporaryDirectory() as raw_root:
            proc_root = Path(raw_root)
            for pid, cmdline in (
                (101, b"bwrap\0--\0exec -a netizen-exact-marker /bin/sleep 30\0"),
                (202, b"netizen-exact-marker\x0030\x00"),
                (303, b"netizen-exact-marker-wrapper\0"),
                (404, b"/bin/sleep\0netizen-exact-marker\0"),
                (505, b""),
            ):
                process = proc_root / str(pid)
                process.mkdir()
                (process / "cmdline").write_bytes(cmdline)
            (proc_root / "self").mkdir()

            matches = probe_python_sdk._matching_processes(
                marker,
                proc_root=proc_root,
            )

        self.assertEqual([202], matches)

    def test_matching_processes_uses_exact_darwin_ps_argv0(self) -> None:
        marker = "netizen-exact-marker"
        result = subprocess.CompletedProcess(
            ["/bin/ps"],
            0,
            """
              101 bwrap -- exec -a netizen-exact-marker /bin/sleep 30
              202 netizen-exact-marker 30
              303 netizen-exact-marker-wrapper 30
              404 /bin/sleep netizen-exact-marker
            """,
            "",
        )
        with (
            tempfile.TemporaryDirectory() as raw_root,
            patch.object(
                probe_python_sdk.subprocess,
                "run",
                return_value=result,
            ) as run,
        ):
            matches = probe_python_sdk._matching_processes(
                marker,
                proc_root=Path(raw_root) / "missing-proc",
                platform_name="darwin",
            )

        self.assertEqual([202], matches)
        run.assert_called_once_with(
            ["/bin/ps", "-ww", "-axo", "pid=,command="],
            check=True,
            capture_output=True,
            text=True,
        )

    async def test_process_exit_classification_has_true_and_false_results(
        self,
    ) -> None:
        with patch.object(
            probe_python_sdk,
            "_wait_for_process",
            new=AsyncMock(return_value=[]),
        ) as wait_for_process:
            self.assertTrue(
                await probe_python_sdk._process_exited_within("marker", timeout=5)
            )
            wait_for_process.assert_awaited_once_with(
                "marker",
                present=False,
                timeout=5,
            )

        with patch.object(
            probe_python_sdk,
            "_wait_for_process",
            new=AsyncMock(side_effect=AssertionError("still running")),
        ):
            self.assertFalse(
                await probe_python_sdk._process_exited_within("marker", timeout=5)
            )

    async def test_overlap_wait_ignores_disjoint_marker_observations(self) -> None:
        observations = (
            [101],
            [],
            [],
            [202],
            [101],
            [202],
        )

        with (
            patch.object(
                probe_python_sdk,
                "_matching_processes",
                side_effect=observations,
            ) as matching,
            patch.object(
                probe_python_sdk.asyncio,
                "sleep",
                new=AsyncMock(),
            ),
        ):
            observed = await probe_python_sdk._wait_for_process_overlap(
                ("marker-a", "marker-b"),
                timeout=1,
            )

        self.assertEqual(([101], [202]), observed)
        self.assertEqual(6, matching.call_count)

    async def test_failed_phase_cleanup_is_bounded(self) -> None:
        never = asyncio.Event()

        async def wait_forever(*_args: object) -> None:
            await never.wait()

        handle = AsyncMock()
        handle.thread_id = "thread-1"
        handle.interrupt.side_effect = wait_forever
        terminal_cleanup = AsyncMock()
        terminal_cleanup.clean_thread.side_effect = wait_forever
        task = asyncio.create_task(never.wait())
        stderr = StringIO()

        with redirect_stderr(stderr):
            await probe_python_sdk._cleanup_turns(
                (handle,),
                (task,),
                terminal_cleanup=terminal_cleanup,
                operation_timeout=0.01,
            )

        self.assertTrue(task.done())
        self.assertIn("native interrupt timed out", stderr.getvalue())
        self.assertIn("terminal cleanup timed out", stderr.getvalue())

    async def test_failed_phase_cleanup_accepts_no_consumer_tasks(self) -> None:
        handle = AsyncMock()
        handle.thread_id = "thread-1"
        terminal_cleanup = AsyncMock()

        await probe_python_sdk._cleanup_turns(
            (handle,),
            (),
            terminal_cleanup=terminal_cleanup,
        )

        handle.interrupt.assert_awaited_once_with()
        terminal_cleanup.clean_thread.assert_awaited_once_with("thread-1")

    async def test_phase_progress_is_reported_to_stderr(self) -> None:
        async def operation() -> dict[str, bool]:
            return {"ok": True}

        result: dict[str, object] = {}
        stderr = StringIO()
        with redirect_stderr(stderr):
            await probe_python_sdk._record_phase(
                result,
                "sample",
                operation(),
            )

        self.assertEqual({"sample": {"ok": True}}, result)
        self.assertEqual(
            ["[probe] sample: started", "[probe] sample: passed"],
            stderr.getvalue().splitlines(),
        )
