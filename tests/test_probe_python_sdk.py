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

from openai_codex import MethodNotFoundError

from scripts import probe_python_sdk


def _read_view(status: str, *turns: object, thread_id: str = "thread-1") -> object:
    return SimpleNamespace(thread=SimpleNamespace(
        id=thread_id, status=SimpleNamespace(root=SimpleNamespace(type=status)),
        turns=list(turns),
    ))


class ProcessProbeTest(unittest.IsolatedAsyncioTestCase):
    def test_paginated_history_error_retry_is_limited_to_exact_full_read_templates(self) -> None:
        for operation in ("list_turns", "list_items"):
            for include_turns in (False, True):
                with self.subTest(operation=operation, include_turns=include_turns):
                    error = MethodNotFoundError(-32601, f"{operation} is not supported yet")
                    self.assertEqual(
                        probe_python_sdk._is_transient_read_error(
                            error, thread_id="thread-1", include_turns=include_turns,
                        ),
                        include_turns,
                    )
        for error in (
            MethodNotFoundError(-32601, "thread/items/list is not supported yet"),
            MethodNotFoundError(-32601, "list_turns is not supported yet; unrelated error"),
            MethodNotFoundError(-32602, "list_items is not supported yet"),
            RuntimeError("list_turns is not supported yet"),
        ):
            with self.subTest(error=error):
                self.assertFalse(
                    probe_python_sdk._is_transient_read_error(
                        error, thread_id="thread-1", include_turns=True,
                    )
                )

    async def test_terminal_read_confirms_each_interrupted_candidate_once(self) -> None:
        interrupted = SimpleNamespace(id="turn-1", status="interrupted", items=[])
        for status in ("interrupted", "completed", "failed", "inProgress"):
            with self.subTest(status=status):
                confirmed = SimpleNamespace(
                    id="turn-1", status=status,
                    items=[SimpleNamespace(root=SimpleNamespace(
                        type="agentMessage", phase=None, text="finished",
                    ))] if status == "completed" else [],
                )
                snapshots = [
                    _read_view("idle"), _read_view("idle", interrupted),
                    _read_view("active" if status == "inProgress" else "idle", confirmed),
                ]
                expected = confirmed
                expected_calls = [call(include_turns=False), call(include_turns=True), call(include_turns=True)]
                expected_delays = [call(2.0)]
                if status == "inProgress":
                    # A later interrupted candidate for the same Turn needs its
                    # own single confirmation; no Turn-wide exemption survives.
                    snapshots.extend([
                        _read_view("idle"), _read_view("idle", interrupted),
                        _read_view("idle", interrupted),
                    ])
                    expected = interrupted
                    expected_calls *= 2
                    expected_delays.extend([call(0.5), call(2.0)])
                thread = SimpleNamespace(id="thread-1", read=AsyncMock(side_effect=snapshots))
                with patch.object(probe_python_sdk.asyncio, "sleep", new=AsyncMock()) as sleep:
                    result = await probe_python_sdk._public_terminal_turn(thread, "turn-1")
                self.assertIs(result, expected)
                self.assertEqual(thread.read.await_args_list, expected_calls)
                self.assertEqual(sleep.await_args_list, expected_delays)

    async def test_terminal_confirmation_failure_does_not_accept_stale_interruption(self) -> None:
        interrupted = SimpleNamespace(id="turn-1", status="interrupted", items=[])
        cases = (
            (RuntimeError("read unavailable"), RuntimeError, "read unavailable"),
            (_read_view("idle", SimpleNamespace(id="other", status="interrupted")), AssertionError, "one exact Turn"),
            (_read_view("idle", interrupted, interrupted), AssertionError, "one exact Turn"),
            (_read_view("idle", interrupted, thread_id="other"), AssertionError, "Thread ID"),
        )
        for confirmation, error_type, message in cases:
            with self.subTest(confirmation=confirmation):
                thread = SimpleNamespace(id="thread-1", read=AsyncMock(side_effect=[
                    _read_view("idle"), _read_view("idle", interrupted), confirmation,
                ]))
                with patch.object(probe_python_sdk.asyncio, "sleep", new=AsyncMock()) as sleep:
                    with self.assertRaisesRegex(error_type, message):
                        await probe_python_sdk._public_terminal_turn(thread, "turn-1")
                self.assertEqual(thread.read.await_count, 3)
                sleep.assert_awaited_once_with(2.0)

    async def test_terminal_confirmation_uses_existing_phase_deadline(self) -> None:
        interrupted = SimpleNamespace(id="turn-1", status="interrupted", items=[])
        thread = SimpleNamespace(id="thread-1", read=AsyncMock(side_effect=[
            _read_view("idle"), _read_view("idle", interrupted),
        ]))
        blocked = asyncio.Event()

        async def wait_for_confirmation(delay: float) -> None:
            self.assertEqual(delay, 2.0)
            self.assertEqual(thread.read.await_count, 2)
            await blocked.wait()

        with patch.object(probe_python_sdk.asyncio, "sleep", side_effect=wait_for_confirmation) as sleep:
            with self.assertRaises(TimeoutError):
                await probe_python_sdk._public_terminal_turn(thread, "turn-1", timeout=0.01)
        sleep.assert_awaited_once_with(2.0)
        self.assertEqual(thread.read.await_count, 2)

    async def test_usage_probe_retries_transient_read_and_confirms_interruption_before_active(self) -> None:
        class Handle:
            id = "turn-1"
            thread_id = "thread-1"

            async def stream(self):
                yield SimpleNamespace(payload=SimpleNamespace(
                    thread_id="thread-1", turn_id="turn-1",
                    token_usage=SimpleNamespace(
                        last=SimpleNamespace(total_tokens=17),
                        model_context_window=128_000,
                    ),
                ))

        thread = SimpleNamespace(
            id="thread-1", turn=AsyncMock(return_value=Handle()),
            read=AsyncMock(side_effect=[
                probe_python_sdk.InternalRpcError(-32603, "rollout is empty"),
                _read_view("idle", SimpleNamespace(id="turn-1", status="interrupted", items=[])),
                _read_view("active", SimpleNamespace(id="turn-1", status="inProgress")),
            ]),
        )
        codex = SimpleNamespace(thread_start=AsyncMock(return_value=thread))
        with (
            patch.object(probe_python_sdk, "PinnedExperimentalTerminalCleanup", return_value=AsyncMock()),
            patch.object(probe_python_sdk, "_public_terminal_turn", new=AsyncMock(
                return_value=SimpleNamespace(status="completed"),
            )),
            patch.object(probe_python_sdk, "ThreadTokenUsageUpdatedNotification", SimpleNamespace),
            patch.object(probe_python_sdk.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            result = await probe_python_sdk._context_usage(codex, Path("/project"))
        self.assertEqual(thread.read.await_args_list, [call(include_turns=True)] * 3)
        self.assertEqual(sleep.await_args_list, [call(0.2), call(2.0)])
        self.assertTrue(result["observed_exact_active"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["used_tokens"], 17)
        self.assertEqual(result["model_context_window"], 128_000)

    async def test_polling_probe_confirms_interruption_then_steers_running_turn(self) -> None:
        handle = SimpleNamespace(
            id="turn-1", steer=AsyncMock(return_value=SimpleNamespace(turn_id="turn-1")),
        )
        interrupted = SimpleNamespace(id="turn-1", status="interrupted", items=[])
        completed = SimpleNamespace(
            id="turn-1", status="completed",
            items=[SimpleNamespace(root=SimpleNamespace(
                type="agentMessage", phase=None, text="POLL-STEERED",
            ))],
        )
        thread = SimpleNamespace(
            id="thread-1", turn=AsyncMock(return_value=handle),
            read=AsyncMock(side_effect=[
                _read_view("idle"), _read_view("idle", interrupted),
                _read_view("active", SimpleNamespace(id="turn-1", status="inProgress")),
                _read_view("active"), _read_view("idle"), _read_view("idle", completed),
            ]),
        )
        codex = SimpleNamespace(thread_start=AsyncMock(return_value=thread))
        with patch.object(probe_python_sdk.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = await probe_python_sdk._polling_completion(codex, Path("/project"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_response"], "POLL-STEERED")
        self.assertTrue(result["steered"])
        handle.steer.assert_awaited_once()
        self.assertEqual(thread.read.await_args_list, [
            call(include_turns=False), call(include_turns=True), call(include_turns=True),
            call(include_turns=False), call(include_turns=False), call(include_turns=True),
        ])
        self.assertEqual(sleep.await_args_list, [call(2.0), call(0.5), call(0.5)])

    async def test_compact_probe_uses_actual_turn_state_after_single_confirmation(self) -> None:
        def compact_turn(status: str) -> object:
            return SimpleNamespace(
                id="compact-turn", status=status,
                items=[SimpleNamespace(root=SimpleNamespace(type="contextCompaction"))],
            )

        for status in ("completed", "interrupted", "failed", "inProgress", "missing_item", "multiple", "systemError"):
            with self.subTest(status=status):
                snapshots = [
                    _read_view("idle"), _read_view("idle", compact_turn("interrupted")),
                    _read_view("active" if status == "inProgress" else "idle", compact_turn(status)),
                ]
                if status == "missing_item":
                    snapshots[-1] = _read_view("idle", SimpleNamespace(
                        id="compact-turn", status="completed", items=[],
                    ))
                elif status == "multiple":
                    other = compact_turn("completed")
                    other.id = "other-compact-turn"
                    snapshots[-1] = _read_view("idle", compact_turn("completed"), other)
                elif status == "systemError":
                    snapshots[-1] = _read_view("systemError", compact_turn("completed"))
                expected_delays = [call(2.0)]
                if status == "inProgress":
                    snapshots.extend([_read_view("idle"), _read_view("idle", compact_turn("completed"))])
                    expected_delays.append(call(0.25))
                thread = SimpleNamespace(
                    id="thread-1", turn=AsyncMock(return_value=SimpleNamespace(id="before-turn")),
                    compact=AsyncMock(), read=AsyncMock(side_effect=snapshots),
                )
                resumed = SimpleNamespace(turn=AsyncMock(return_value=SimpleNamespace(id="after-turn")))
                codex = SimpleNamespace(
                    thread_start=AsyncMock(return_value=thread),
                    thread_resume=AsyncMock(return_value=resumed),
                )
                responses = [SimpleNamespace(items=[SimpleNamespace(root=SimpleNamespace(
                    type="agentMessage", phase=None, text=text,
                ))]) for text in ("COMPACT-BEFORE", "COMPACT-AFTER")]
                with (
                    patch.object(probe_python_sdk, "_public_terminal_turn", new=AsyncMock(side_effect=responses)),
                    patch.object(probe_python_sdk.asyncio, "sleep", new=AsyncMock()) as sleep,
                ):
                    failures = {
                        "interrupted": (AssertionError, "compaction ended with 'interrupted'"),
                        "failed": (AssertionError, "compaction ended with 'failed'"),
                        "missing_item": (AssertionError, "lost the compaction Turn"),
                        "multiple": (AssertionError, "multiple post-baseline compaction Turns"),
                        "systemError": (RuntimeError, "unexpected compaction confirmation status"),
                    }
                    if status in failures:
                        with self.assertRaisesRegex(*failures[status]):
                            await probe_python_sdk._compact(codex, Path("/project"))
                        codex.thread_resume.assert_not_awaited()
                    else:
                        result = await probe_python_sdk._compact(codex, Path("/project"))
                        self.assertEqual(result["compact_turn_id"], "compact-turn")
                        self.assertEqual(result["compact_turn_status"], "completed")
                        self.assertEqual(result["after_response"], "COMPACT-AFTER")
                        codex.thread_resume.assert_awaited_once_with("thread-1", include_turns=False)
                self.assertEqual(thread.read.await_count, len(snapshots))
                self.assertEqual(sleep.await_args_list, expected_delays)
                thread.compact.assert_awaited_once()

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
