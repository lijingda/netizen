#!/usr/bin/env python3
"""Black-box the public Python SDK against a real Codex installation.

The probe intentionally constructs ``AsyncCodex`` without a custom binary,
environment, model, sandbox, or config override.  Run it as the same OS user
and with the same HOME/CODEX_HOME that the service will use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from openai_codex import (
    AsyncCodex,
    InternalRpcError,
    InvalidRequestError,
    SkillInput,
    TextInput,
    is_retryable_error,
)
import openai_codex
from openai_codex.types import ThreadTokenUsageUpdatedNotification

from netizen.model_settings import ModelCatalog, STANDARD_SERVICE_TIER_ID
from netizen.prompt_projection import CurrentMessageProjection, render_plain_prompt
from netizen.sdk_gap_adapter import (
    AppServerGoalControl,
    AppServerSideBoundaryControl,
    AppServerSkillCatalog,
    AppServerThreadDeleteControl,
    AppServerThreadSubscriptionControl,
    GoalStatus,
    facade_migration_requirements,
)
from netizen.terminal_cleanup import PinnedExperimentalTerminalCleanup, TerminalCleanup
from netizen.turn_plan_observer import (
    PinnedTurnActivityObserver,
    TurnActivityObservation,
)
from netizen.turn_files import turn_diff_summary


_NOT_MATERIALIZED_SUFFIX = (
    "is not materialized yet; includeTurns is unavailable before first user message"
)
_FINAL_RESPONSE_MATERIALIZATION_RETRIES = 4


def _status_value(result: object) -> str | None:
    status = getattr(result, "status", None)
    value = getattr(status, "value", status)
    return value if isinstance(value, str) else None


def _thread_status_type(native_thread: object) -> str | None:
    status = getattr(native_thread, "status", None)
    root = getattr(status, "root", status)
    value = getattr(root, "type", None)
    return value if isinstance(value, str) else None


def _final_response_from_turn(turn: object) -> str | None:
    fallback: str | None = None
    for item in reversed(getattr(turn, "items", [])):
        root = getattr(item, "root", item)
        if getattr(root, "type", None) != "agentMessage":
            continue
        text = getattr(root, "text", None)
        if not isinstance(text, str):
            continue
        phase = getattr(getattr(root, "phase", None), "value", None)
        if phase == "final_answer":
            return text
        if phase is None and fallback is None:
            fallback = text
    return fallback


def _is_transient_read_error(
    error: BaseException,
    *,
    thread_id: str,
    include_turns: bool,
) -> bool:
    if isinstance(error, InternalRpcError) or is_retryable_error(error):
        return True
    return (
        include_turns
        and isinstance(error, InvalidRequestError)
        and error.code == -32600
        and error.message == f"thread {thread_id} {_NOT_MATERIALIZED_SUFFIX}"
    )


def _matching_processes(
    marker: str,
    *,
    proc_root: Path = Path("/proc"),
    platform_name: str | None = None,
) -> list[int]:
    """Return processes whose exact ``argv[0]`` is the probe marker.

    Sandbox wrappers retain the full command text in their own argv before the
    requested tool process starts.  Substring matching therefore admits a
    wrapper-only false positive and can interrupt the Turn before the marker
    process has ever existed.
    """

    selected_platform = sys.platform if platform_name is None else platform_name
    if not proc_root.exists() and selected_platform == "darwin":
        return _matching_processes_from_ps(marker)

    matches: list[int] = []
    marker_bytes = marker.encode()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv0 = (entry / "cmdline").read_bytes().split(b"\0", 1)[0]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if argv0 == marker_bytes:
            matches.append(int(entry.name))
    return sorted(matches)


def _matching_processes_from_ps(marker: str) -> list[int]:
    """Darwin fallback for exact probe argv[0] matching without ``/proc``."""

    result = subprocess.run(
        ["/bin/ps", "-ww", "-axo", "pid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    matches: list[int] = []
    for raw_line in result.stdout.splitlines():
        fields = raw_line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        argv0 = fields[1].split(maxsplit=1)[0]
        if argv0 == marker:
            matches.append(int(fields[0]))
    return sorted(matches)


async def _wait_for_process(marker: str, *, present: bool, timeout: float) -> list[int]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        matches = _matching_processes(marker)
        if bool(matches) is present:
            return matches
        if asyncio.get_running_loop().time() >= deadline:
            state = "appear" if present else "exit"
            raise AssertionError(f"process marker {marker!r} did not {state}")
        await asyncio.sleep(0.2)


async def _process_exited_within(marker: str, *, timeout: float) -> bool:
    try:
        await _wait_for_process(marker, present=False, timeout=timeout)
    except AssertionError:
        return False
    return True


async def _wait_for_process_overlap(
    markers: tuple[str, ...],
    *,
    timeout: float,
) -> tuple[list[int], ...]:
    """Return one sample in which every marker process is live."""

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        observed = tuple(_matching_processes(marker) for marker in markers)
        if all(observed):
            return observed
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "marker processes were never observed live concurrently: "
                f"{markers!r}"
            )
        await asyncio.sleep(0.2)


async def _record_phase(
    result: dict[str, Any],
    phase: str,
    operation: Awaitable[dict[str, Any]],
) -> None:
    print(f"[probe] {phase}: started", file=sys.stderr, flush=True)
    try:
        result[phase] = await operation
    except BaseException:
        print(f"[probe] {phase}: failed", file=sys.stderr, flush=True)
        raise
    print(f"[probe] {phase}: passed", file=sys.stderr, flush=True)


async def _find_listed_thread(
    codex: AsyncCodex,
    thread_id: str,
    *,
    archived: bool,
    use_state_db_only: bool | None = None,
) -> object | None:
    """Find one exact Thread without assuming the native catalog fits one page."""

    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        list_kwargs: dict[str, object] = {
            "archived": archived,
            "cursor": cursor,
            "limit": 100,
        }
        if use_state_db_only is not None:
            list_kwargs["use_state_db_only"] = use_state_db_only
        response = await codex.thread_list(
            **list_kwargs,
        )
        data = getattr(response, "data", None)
        if not isinstance(data, list):
            raise AssertionError("thread_list returned an invalid data page")
        matches = [item for item in data if getattr(item, "id", None) == thread_id]
        if len(matches) > 1:
            raise AssertionError("thread_list returned a duplicate native Thread ID")
        if matches:
            return matches[0]
        next_cursor = getattr(response, "next_cursor", None)
        if next_cursor is None:
            return None
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor in seen_cursors
        ):
            raise AssertionError("thread_list returned an invalid pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


async def _wait_for_thread_visibility(
    codex: AsyncCodex,
    thread_id: str,
    *,
    archived: bool,
    present: bool,
    use_state_db_only: bool | None = None,
    timeout: float = 15.0,
) -> object | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        found = await _find_listed_thread(
            codex,
            thread_id,
            archived=archived,
            use_state_db_only=use_state_db_only,
        )
        if (found is not None) is present:
            return found
        if asyncio.get_running_loop().time() >= deadline:
            location = "archived" if archived else "active"
            expectation = "appear in" if present else "disappear from"
            raise AssertionError(
                f"Thread {thread_id} did not {expectation} the {location} catalog"
            )
        await asyncio.sleep(0.2)


async def _smoke(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    request_text = "Reply exactly: SDK-SMOKE"
    prompt = render_plain_prompt(
        CurrentMessageProjection(
            message_id="probe-current-message",
            message_type="text",
            content_fidelity="full_text",
            sender={
                "display_name": "SDK Smoke User",
                "is_bot": False,
                "open_id": "probe-user",
            },
            request_text=request_text,
        )
    )
    thread = await codex.thread_start(cwd=str(cwd))
    handle = await thread.turn(prompt)
    result = await _public_terminal_turn(thread, handle.id)
    resumed = await codex.thread_resume(thread.id)
    listed = await codex.thread_list(limit=100)
    listed_thread = next(
        (item for item in listed.data if item.id == thread.id),
        None,
    )
    if resumed.id != thread.id:
        raise AssertionError("thread_resume changed the native Thread ID")
    if listed_thread is None:
        raise AssertionError("SDK-created Thread is absent from thread_list")
    preview = getattr(listed_thread, "preview", None)
    name = getattr(listed_thread, "name", None)
    if not isinstance(preview, str) or not preview.startswith(request_text):
        raise AssertionError(
            "thread_list preview did not start with the projected request: "
            f"{preview=!r}"
        )
    if name is not None and not isinstance(name, str):
        raise AssertionError(f"thread_list returned an invalid name: {name=!r}")
    status = _status_value(result)
    final_response = _final_response_from_turn(result)
    if (
        status != "completed"
        or final_response is None
        or "SDK-SMOKE" not in final_response
    ):
        raise AssertionError(
            f"smoke Turn did not complete as requested: {status=}, {final_response=}"
        )
    return {
        "thread_id": thread.id,
        "turn_id": handle.id,
        "status": status,
        "final_response": final_response,
        "listed": True,
        "name": name,
        "preview": preview,
        "preview_request_first": True,
    }


async def _context_usage(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    """Prove the exact active-read -> terminal-read -> stream-drain path."""

    thread = await codex.thread_start(cwd=str(cwd))
    handle = await thread.turn(
        "Use the terminal to run /bin/sleep 8 and wait for it. "
        "Then reply exactly: USAGE-PROBE"
    )
    cleanup = PinnedExperimentalTerminalCleanup(codex)
    observed_active = False
    deadline = asyncio.get_running_loop().time() + 45
    try:
        while asyncio.get_running_loop().time() < deadline:
            try:
                snapshot = await thread.read(include_turns=True)
            except Exception as error:
                if not _is_transient_read_error(
                    error,
                    thread_id=thread.id,
                    include_turns=True,
                ):
                    raise
                await asyncio.sleep(0.2)
                continue
            exact = next(
                (turn for turn in snapshot.thread.turns if turn.id == handle.id),
                None,
            )
            exact_status = _status_value(exact) if exact is not None else None
            if (
                _thread_status_type(snapshot.thread) == "active"
                and exact_status == "inProgress"
            ):
                observed_active = True
                break
            if exact_status in {"completed", "interrupted", "failed"}:
                raise AssertionError(
                    "usage probe Turn completed before exact active state was observed"
                )
            await asyncio.sleep(0.2)
        if not observed_active:
            raise TimeoutError("did not observe the usage probe Turn active")

        terminal = await _public_terminal_turn(thread, handle.id)
        if _status_value(terminal) != "completed":
            raise AssertionError("usage probe Turn did not complete")

        latest = None
        async for notification in handle.stream():
            payload = notification.payload
            if (
                isinstance(payload, ThreadTokenUsageUpdatedNotification)
                and payload.thread_id == thread.id
                and payload.turn_id == handle.id
            ):
                latest = payload.token_usage
        if latest is None:
            raise AssertionError("Turn stream omitted thread/tokenUsage/updated")
        used_tokens = latest.last.total_tokens
        context_window = latest.model_context_window
        if used_tokens < 0:
            raise AssertionError(f"invalid current-window usage: {used_tokens!r}")
        if context_window is None or context_window <= 0:
            raise AssertionError(f"invalid model context window: {context_window!r}")
        return {
            "thread_id": thread.id,
            "turn_id": handle.id,
            "observed_exact_active": observed_active,
            "status": _status_value(terminal),
            "used_tokens": used_tokens,
            "model_context_window": context_window,
        }
    except BaseException:
        await _cleanup_turns(
            (handle,),
            (),
            terminal_cleanup=cleanup,
        )
        raise


async def _thread_lifecycle_live(
    codex: AsyncCodex,
    cwd: Path,
) -> dict[str, Any]:
    """Exercise public lifecycle plus the approved narrow native delete."""

    thread = await codex.thread_start(cwd=str(cwd))
    turn = await thread.turn("Reply exactly: THREAD-LIFECYCLE-LIVE")
    terminal = await _public_terminal_turn(thread, turn.id)
    if _status_value(terminal) != "completed":
        raise AssertionError("lifecycle probe seed Turn did not complete")

    name = f"Netizen lifecycle probe {time.time_ns()}"
    await thread.set_name(name)
    active = await _wait_for_thread_visibility(
        codex,
        thread.id,
        archived=False,
        present=True,
    )
    if getattr(active, "name", None) != name:
        raise AssertionError("thread/name/set was not visible in thread_list")

    await codex.thread_archive(thread.id)
    archived = await _wait_for_thread_visibility(
        codex,
        thread.id,
        archived=True,
        present=True,
    )
    await _wait_for_thread_visibility(
        codex,
        thread.id,
        archived=False,
        present=False,
    )
    if getattr(archived, "name", None) != name:
        raise AssertionError("archive did not retain the native Thread name")

    restored = await codex.thread_unarchive(thread.id)
    if restored.id != thread.id:
        raise AssertionError("thread_unarchive changed the native Thread ID")
    await _wait_for_thread_visibility(
        codex,
        thread.id,
        archived=False,
        present=True,
    )
    await _wait_for_thread_visibility(
        codex,
        thread.id,
        archived=True,
        present=False,
    )
    delete_control = AppServerThreadDeleteControl(codex)
    await delete_control.delete(thread.id)
    await _prove_thread_absent_from_all_catalogs(codex, thread.id)
    archived_delete = await _archived_thread_delete_live(
        codex,
        cwd,
        delete_control,
    )
    running_delete = await _running_thread_delete_live(
        codex,
        cwd,
        delete_control,
    )
    return {
        "thread_id": thread.id,
        "turn_id": turn.id,
        "name": name,
        "rename_visible": True,
        "archive_visible": True,
        "unarchive_restored_same_id": True,
        "delete_acknowledged": True,
        "delete_absent_from_scan_and_state_db": True,
        "archived_delete": archived_delete,
        "running_delete": running_delete,
    }


async def _prove_thread_absent_from_all_catalogs(
    codex: AsyncCodex,
    thread_id: str,
) -> None:
    for use_state_db_only in (False, True):
        await _wait_for_thread_visibility(
            codex,
            thread_id,
            archived=False,
            present=False,
            use_state_db_only=use_state_db_only,
        )
        await _wait_for_thread_visibility(
            codex,
            thread_id,
            archived=True,
            present=False,
            use_state_db_only=use_state_db_only,
        )


async def _archived_thread_delete_live(
    codex: AsyncCodex,
    cwd: Path,
    delete_control: AppServerThreadDeleteControl,
) -> dict[str, Any]:
    thread = await codex.thread_start(cwd=str(cwd))
    handle = await thread.turn("Reply exactly: ARCHIVED-DELETE-LIVE")
    terminal = await _public_terminal_turn(thread, handle.id)
    if _status_value(terminal) != "completed":
        raise AssertionError("archived-delete seed Turn did not complete")
    await codex.thread_archive(thread.id)
    await _wait_for_thread_visibility(
        codex,
        thread.id,
        archived=True,
        present=True,
    )
    await _wait_for_thread_visibility(
        codex,
        thread.id,
        archived=False,
        present=False,
    )
    await delete_control.delete(thread.id)
    await _prove_thread_absent_from_all_catalogs(codex, thread.id)
    return {
        "thread_id": thread.id,
        "turn_id": handle.id,
        "archived_before_delete": True,
        "delete_acknowledged": True,
        "delete_absent_from_scan_and_state_db": True,
    }


async def _running_thread_delete_live(
    codex: AsyncCodex,
    cwd: Path,
    delete_control: AppServerThreadDeleteControl,
) -> dict[str, Any]:
    """Delete a persisted Thread while its exact Turn is still running."""

    marker = f"netizen-lifecycle-running-{time.time_ns()}"
    thread = await codex.thread_start(cwd=str(cwd))
    handle = await thread.turn(
        "Use the terminal to run exactly this command and wait for it: "
        f"/bin/bash -lc 'exec -a {marker} /bin/sleep 120'. "
        "After it exits, reply exactly: RUNNING-DELETE-LIVE"
    )
    delete_attempted = False
    try:
        observed_pids = await _wait_for_process(marker, present=True, timeout=45)
        await _wait_for_thread_visibility(
            codex,
            thread.id,
            archived=False,
            present=True,
        )
        delete_attempted = True
        await delete_control.delete(thread.id)
        await _prove_thread_absent_from_all_catalogs(codex, thread.id)
        await _wait_for_process(marker, present=False, timeout=20)
    except BaseException:
        # Cleanup is only for a failed disposable probe.  Once delete was sent,
        # never resend it after an uncertain response.
        await _cleanup_turns(
            (handle,),
            (),
            terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
        )
        if not delete_attempted:
            try:
                await delete_control.delete(thread.id)
            except Exception:
                pass
        raise
    orphan_pids = _matching_processes(marker)
    if orphan_pids:
        raise AssertionError(
            f"running-delete probe left marker processes: {orphan_pids}"
        )
    return {
        "thread_id": thread.id,
        "turn_id": handle.id,
        "running_marker_pids": observed_pids,
        "deleted_without_interrupt_cleanup_or_idle_read": True,
        "orphan_pids": orphan_pids,
        "delete_acknowledged": True,
        "delete_absent_from_scan_and_state_db": True,
    }


async def _release_live(cwd: Path) -> dict[str, Any]:
    """Prove unsubscribe, same-ID resume, and a fresh App Server resume."""

    thread_id: str | None = None
    first_turn_id: str | None = None
    same_server_turn_id: str | None = None
    second_server_turn_id: str | None = None
    first_status = None
    same_server_status = None
    second_server_status = None
    try:
        async with AsyncCodex() as first:
            subscription = AppServerThreadSubscriptionControl(first)
            inspector = PinnedExperimentalTerminalCleanup(first)
            thread = await first.thread_start(cwd=str(cwd))
            thread_id = thread.id
            first_turn = await thread.turn("Reply exactly: RELEASE-FIRST")
            first_turn_id = first_turn.id
            if await _public_final_response(thread, first_turn.id) != "RELEASE-FIRST":
                raise AssertionError("release seed Turn returned an unexpected response")
            if await inspector.has_running(thread.id):
                raise AssertionError("release seed unexpectedly left a background terminal")
            first_status = await subscription.unsubscribe(thread.id)

            resumed = await first.thread_resume(thread.id)
            if resumed.id != thread.id:
                raise AssertionError("same App Server resume changed Thread ID")
            same_server_turn = await resumed.turn(
                "Reply exactly: RELEASE-SAME-SERVER"
            )
            same_server_turn_id = same_server_turn.id
            if (
                await _public_final_response(resumed, same_server_turn.id)
                != "RELEASE-SAME-SERVER"
            ):
                raise AssertionError("same App Server resume Turn returned unexpectedly")
            if await inspector.has_running(thread.id):
                raise AssertionError("same App Server resume left a background terminal")
            same_server_status = await subscription.unsubscribe(thread.id)

        async with AsyncCodex() as second:
            subscription = AppServerThreadSubscriptionControl(second)
            inspector = PinnedExperimentalTerminalCleanup(second)
            resumed = await second.thread_resume(thread_id)
            if resumed.id != thread_id:
                raise AssertionError("fresh App Server resume changed Thread ID")
            second_server_turn = await resumed.turn(
                "Reply exactly: RELEASE-FRESH-SERVER"
            )
            second_server_turn_id = second_server_turn.id
            if (
                await _public_final_response(resumed, second_server_turn.id)
                != "RELEASE-FRESH-SERVER"
            ):
                raise AssertionError("fresh App Server resume Turn returned unexpectedly")
            if await inspector.has_running(thread_id):
                raise AssertionError("fresh App Server resume left a background terminal")
            second_server_status = await subscription.unsubscribe(thread_id)
            await second.thread_archive(thread_id)
    except BaseException:
        if thread_id is not None:
            try:
                async with AsyncCodex() as cleanup_codex:
                    await cleanup_codex.thread_archive(thread_id)
            except Exception:
                pass
        raise

    assert (
        thread_id is not None
        and first_status is not None
        and same_server_status is not None
        and second_server_status is not None
    )
    return {
        "thread_id": thread_id,
        "first_turn_id": first_turn_id,
        "first_unsubscribe_status": first_status.value,
        "same_server_turn_id": same_server_turn_id,
        "same_server_unsubscribe_status": same_server_status.value,
        "fresh_server_turn_id": second_server_turn_id,
        "fresh_server_unsubscribe_status": second_server_status.value,
        "same_id_resumed_by_fresh_app_server": True,
        "probe_left_archived": True,
    }


async def _side_live(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    """Exercise active-Parent fork, multi-turn Side, and fixed controls."""

    boundary = AppServerSideBoundaryControl(codex)
    subscription = AppServerThreadSubscriptionControl(codex)
    cleanup = PinnedExperimentalTerminalCleanup(codex)
    parent = await codex.thread_start(cwd=str(cwd))
    seed = None
    side = None
    side_handles: list[Any] = []
    parent_running = None
    parent_after = None
    parent_task: asyncio.Task[str] | None = None
    parent_marker = f"netizen-sdk-probe-side-parent-{time.time_ns()}"
    parent_overlap_pids: list[int] = []
    unsubscribe_status = None
    phase_succeeded = False
    try:
        seed = await parent.turn("Reply exactly: SIDE-PARENT-SEED")
        seed_response = await _public_final_response(parent, seed.id)
        if seed_response != "SIDE-PARENT-SEED":
            raise AssertionError(
                f"Side parent seed returned {seed_response!r}"
            )

        parent_running = await parent.turn(
            "Use the terminal to run exactly this command and wait for it: "
            f"/bin/bash -lc 'exec -a {parent_marker} /bin/sleep 30'. "
            "When it exits, reply exactly: SIDE-PARENT-RUNNING"
        )
        parent_task = asyncio.create_task(
            _public_final_response(parent, parent_running.id)
        )
        await _wait_for_process(parent_marker, present=True, timeout=45)

        # Fork while the materialized Parent has an exact active Turn.  Starting
        # the first Side Turn before re-checking the marker proves the two native
        # Turns overlap instead of merely proving that the fork RPC is accepted.
        side = await codex.thread_fork(parent.id, ephemeral=True)
        if side.id == parent.id:
            raise AssertionError("ephemeral fork reused the parent Thread ID")
        view = await side.read(include_turns=False)
        native = getattr(view, "thread", None)
        if getattr(native, "id", None) != side.id:
            raise AssertionError("Side fork read returned a different Thread ID")
        if getattr(native, "ephemeral", None) is not True:
            raise AssertionError("Side fork was not ephemeral")
        forked_from = getattr(native, "forked_from_id", None)
        if forked_from is not None and forked_from != parent.id:
            raise AssertionError("Side fork parent identity changed")
        await boundary.inject_boundary(side.id)

        responses: list[str | None] = []
        for expected in ("SIDE-ONE", "SIDE-TWO"):
            handle = await side.turn(f"Reply exactly: {expected}")
            side_handles.append(handle)
            if expected == "SIDE-ONE":
                parent_overlap_pids = await _wait_for_process(
                    parent_marker,
                    present=True,
                    timeout=2,
                )
            terminal = await handle.run()
            if _status_value(terminal) != "completed":
                raise AssertionError(
                    f"Side Turn ended with {_status_value(terminal)!r}"
                )
            response = getattr(terminal, "final_response", None)
            responses.append(response if isinstance(response, str) else None)
            if response != expected:
                raise AssertionError(
                    f"Side Turn returned {response!r}, expected {expected!r}"
                )

        parent_running_response = await parent_task
        if parent_running_response != "SIDE-PARENT-RUNNING":
            raise AssertionError(
                "active Parent Turn returned an unexpected response: "
                f"{parent_running_response!r}"
            )
        await _wait_for_process(parent_marker, present=False, timeout=10)
        await cleanup.clean_thread(side.id)
        unsubscribe_status = await subscription.unsubscribe(side.id)

        parent_after = await parent.turn("Reply exactly: SIDE-PARENT-AFTER")
        parent_response = await _public_final_response(parent, parent_after.id)
        if parent_response != "SIDE-PARENT-AFTER":
            raise AssertionError("parent Thread was not usable after Side unsubscribe")

        phase_succeeded = True
        return {
            "parent_thread_id": parent.id,
            "parent_seed_response": seed_response,
            "parent_running_turn_id": parent_running.id,
            "parent_running_response": parent_running_response,
            "parent_side_overlap_pids": parent_overlap_pids,
            "side_thread_id": side.id,
            "side_ephemeral": True,
            "side_turn_ids": [handle.id for handle in side_handles],
            "side_responses": responses,
            "unsubscribe_status": unsubscribe_status.value,
            "parent_after_response": parent_response,
            "parent_left_archived": True,
        }
    except BaseException:
        cleanup_handles = list(side_handles)
        if seed is not None:
            cleanup_handles.append(seed)
        if parent_running is not None:
            cleanup_handles.append(parent_running)
        if parent_after is not None:
            cleanup_handles.append(parent_after)
        await _cleanup_turns(
            tuple(cleanup_handles),
            (parent_task,) if parent_task is not None else (),
            terminal_cleanup=cleanup,
        )
        if side is not None:
            try:
                await cleanup.clean_thread(side.id)
            except Exception:
                pass
            try:
                await subscription.unsubscribe(side.id)
            except Exception:
                pass
        raise
    finally:
        try:
            await codex.thread_archive(parent.id)
        except Exception:
            if phase_succeeded:
                raise


async def _steer(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    marker = f"netizen-sdk-probe-steer-{time.time_ns()}"
    thread = await codex.thread_start(cwd=str(cwd))
    handle = await thread.turn(
        "Use the terminal to run exactly this command and wait for it: "
        f"/bin/bash -lc 'exec -a {marker} /bin/sleep 8'. "
        "When it exits, reply exactly: PROBE-ORIGINAL"
    )
    task = asyncio.create_task(_public_terminal_turn(thread, handle.id))
    try:
        observed = await _wait_for_process(marker, present=True, timeout=45)
        steered = await handle.steer(
            "Change the required final reply. Reply exactly: PROBE-STEERED"
        )
        result = await asyncio.wait_for(task, timeout=180)
    except BaseException:
        await _cleanup_turns((handle,), (task,))
        raise
    await _wait_for_process(marker, present=False, timeout=10)
    final_response = _final_response_from_turn(result)
    if final_response is None or "PROBE-STEERED" not in final_response:
        raise AssertionError(f"steer did not change the final response: {final_response!r}")
    return {
        "thread_id": thread.id,
        "turn_id": handle.id,
        "steer_turn_id": getattr(steered, "turn_id", None),
        "observed_pids": observed,
        "status": _status_value(result),
        "final_response": final_response,
    }


async def _wait_for_plan_observation(
    observer: PinnedTurnActivityObserver,
    *,
    thread_id: str,
    turn_id: str,
    after_cursor: int,
    timeout: float,
    require_activity: bool = False,
) -> TurnActivityObservation:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        observation = observer.observe(
            thread_id=thread_id,
            turn_id=turn_id,
            after_cursor=after_cursor,
        )
        if observation.plan_updated and (
            not require_activity or observation.events
        ):
            return observation
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("native turn/plan/updated was not observed")
        await asyncio.sleep(0.1)


async def _turn_plan_live(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    """Observe safe Activity and a checklist's post-steer full replacement."""

    from openai_codex.generated.v2_all import TurnPlanUpdatedNotification

    observer = PinnedTurnActivityObserver(codex)
    thread = await codex.thread_start(cwd=str(cwd))
    handle = await thread.turn(
        "First call update_plan with exactly these three steps: "
        "Publish the initial checklist; Wait for the bounded delay; "
        "Return the original phrase. Then use the terminal to run "
        "/bin/sleep 12 and wait for it. "
        "When it exits, reply exactly: PLAN-PROBE-ORIGINAL"
    )
    terminal_task = asyncio.create_task(_public_terminal_turn(thread, handle.id))
    try:
        initial = await _wait_for_plan_observation(
            observer,
            thread_id=thread.id,
            turn_id=handle.id,
            after_cursor=0,
            timeout=45,
            require_activity=True,
        )
        steered = await handle.steer(
            "Replace the complete checklist by calling update_plan with exactly "
            "these two steps: Acknowledge the steer; Return the steered phrase. "
            "Then reply exactly: PLAN-PROBE-STEERED"
        )
        refreshed = await _wait_for_plan_observation(
            observer,
            thread_id=thread.id,
            turn_id=handle.id,
            after_cursor=initial.next_cursor,
            timeout=60,
        )
        if not initial.steps or not refreshed.steps:
            raise AssertionError("plan probe observed an empty native checklist")
        terminal = await asyncio.wait_for(terminal_task, timeout=180)
        streamed = []
        async for notification in handle.stream():
            streamed.append(notification)
    except BaseException:
        await _cleanup_turns((handle,), (terminal_task,))
        raise
    final_response = _final_response_from_turn(terminal) or ""
    if "PLAN-PROBE-STEERED" not in final_response:
        raise AssertionError(
            f"plan probe steer did not change the final response: {final_response!r}"
        )
    streamed_plan_count = sum(
        isinstance(notification.payload, TurnPlanUpdatedNotification)
        for notification in streamed
    )
    if streamed_plan_count < 2:
        raise AssertionError(
            "public terminal stream did not retain the observed plan notifications"
        )
    observed_activity_kinds = sorted(
        {
            event.kind.value
            for observation in (initial, refreshed)
            for event in observation.events
        }
    )
    if not observed_activity_kinds:
        raise AssertionError("Activity probe observed no safe item lifecycle")
    return {
        "thread_id": thread.id,
        "turn_id": handle.id,
        "steer_turn_id": getattr(steered, "turn_id", None),
        "initial_plan": [item.step for item in initial.steps],
        "refreshed_plan": [item.step for item in refreshed.steps],
        "streamed_plan_notifications": streamed_plan_count,
        "activity_kinds": observed_activity_kinds,
        "status": _status_value(terminal),
        "final_response": final_response,
    }


async def _polling_completion(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    thread = await codex.thread_start(cwd=str(cwd))
    handle = await thread.turn(
        "Use the terminal to run /bin/sleep 8 and wait for it. "
        "Then reply exactly: POLL-ORIGINAL"
    )
    statuses: list[str] = []
    read_error_count = 0
    steered = False
    started = time.monotonic()
    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        try:
            summary = await thread.read(include_turns=False)
        except Exception as error:
            if not _is_transient_read_error(
                error,
                thread_id=thread.id,
                include_turns=False,
            ):
                raise
            read_error_count += 1
            await asyncio.sleep(0.5)
            continue

        thread_status = _thread_status_type(summary.thread)
        if thread_status == "notLoaded":
            if not statuses or statuses[-1] != thread_status:
                statuses.append(thread_status)
            await asyncio.sleep(0.5)
            continue
        if thread_status == "active":
            if not statuses or statuses[-1] != thread_status:
                statuses.append(thread_status)
            if not steered:
                response = await handle.steer(
                    "Change only the final reply to exactly: POLL-STEERED"
                )
                if getattr(response, "turn_id", None) != handle.id:
                    raise AssertionError("polling steer returned a different Turn ID")
                steered = True
            await asyncio.sleep(0.5)
            continue
        if thread_status == "systemError":
            raise RuntimeError("native Thread entered systemError")
        if thread_status != "idle":
            raise RuntimeError(f"unexpected native Thread status: {thread_status!r}")

        try:
            snapshot = await thread.read(include_turns=True)
        except Exception as error:
            if not _is_transient_read_error(
                error,
                thread_id=thread.id,
                include_turns=True,
            ):
                raise
            read_error_count += 1
            await asyncio.sleep(0.5)
            continue
        exact = next(
            (turn for turn in snapshot.thread.turns if turn.id == handle.id),
            None,
        )
        if exact is None:
            await asyncio.sleep(0.5)
            continue
        status = _status_value(exact)
        if not statuses or statuses[-1] != status:
            statuses.append(status or "unknown")
        if status == "inProgress":
            await asyncio.sleep(0.5)
            continue
        final_response = _final_response_from_turn(exact)
        if (
            status != "completed"
            or final_response is None
            or "POLL-STEERED" not in final_response
        ):
            raise AssertionError(
                "public polling returned an unexpected terminal Turn: "
                f"{status=}, {final_response=}"
            )
        return {
            "thread_id": thread.id,
            "turn_id": handle.id,
            "statuses": statuses,
            "read_error_count": read_error_count,
            "steered": steered,
            "status": status,
            "final_response": final_response,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    raise TimeoutError("public thread.read did not observe a terminal Turn")


async def _compact(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    """Verify public compaction start plus public completion observation."""

    thread = await codex.thread_start(cwd=str(cwd))
    before_handle = await thread.turn("Reply exactly: COMPACT-BEFORE")
    before_turn = await _public_terminal_turn(thread, before_handle.id)
    if _final_response_from_turn(before_turn) != "COMPACT-BEFORE":
        raise AssertionError("pre-compaction Turn returned an unexpected response")

    before_ids = {before_handle.id}
    started = time.monotonic()
    await thread.compact()
    request_elapsed = time.monotonic() - started

    statuses: list[str] = []
    compact_turn: object | None = None
    read_error_count = 0
    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        try:
            summary = await thread.read(include_turns=False)
        except Exception as error:
            if not _is_transient_read_error(
                error,
                thread_id=thread.id,
                include_turns=False,
            ):
                raise
            read_error_count += 1
            await asyncio.sleep(0.25)
            continue
        status = _thread_status_type(summary.thread)
        if not statuses or statuses[-1] != status:
            statuses.append(status or "unknown")
        if status == "systemError":
            raise RuntimeError("native Thread entered systemError during compaction")
        if status not in {"notLoaded", "active", "idle"}:
            raise RuntimeError(f"unexpected native Thread status: {status!r}")
        if status != "idle":
            await asyncio.sleep(0.25)
            continue

        try:
            snapshot = await thread.read(include_turns=True)
        except Exception as error:
            if not _is_transient_read_error(
                error,
                thread_id=thread.id,
                include_turns=True,
            ):
                raise
            read_error_count += 1
            await asyncio.sleep(0.25)
            continue
        candidates = []
        for turn in snapshot.thread.turns:
            item_types = {
                getattr(getattr(item, "root", item), "type", None)
                for item in turn.items
            }
            if turn.id not in before_ids and "contextCompaction" in item_types:
                candidates.append(turn)
        if len(candidates) > 1:
            raise AssertionError("multiple post-baseline compaction Turns observed")
        compact_turn = candidates[0] if candidates else None
        if (
            compact_turn is not None
            and status == "idle"
            and _status_value(compact_turn) != "inProgress"
        ):
            break
        await asyncio.sleep(0.25)
    else:
        raise TimeoutError("public thread.read did not observe compaction completion")

    compact_status = _status_value(compact_turn)
    if compact_status != "completed":
        raise AssertionError(f"compaction ended with {compact_status!r}")
    compact_item_types = [
        getattr(getattr(item, "root", item), "type", None)
        for item in getattr(compact_turn, "items", ())
    ]

    resumed = await codex.thread_resume(thread.id)
    after_handle = await resumed.turn("Reply exactly: COMPACT-AFTER")
    after_turn = await _public_terminal_turn(resumed, after_handle.id)
    after_response = _final_response_from_turn(after_turn)
    if after_response != "COMPACT-AFTER":
        raise AssertionError(
            f"post-compaction Turn returned an unexpected response: {after_response!r}"
        )
    return {
        "thread_id": thread.id,
        "request_elapsed_seconds": round(request_elapsed, 3),
        "observed_thread_statuses": statuses,
        "read_error_count": read_error_count,
        "compact_turn_id": getattr(compact_turn, "id", None),
        "compact_turn_status": compact_status,
        "compact_item_types": compact_item_types,
        "after_turn_id": after_handle.id,
        "after_response": after_response,
    }


async def _public_terminal_turn(
    thread: Any,
    turn_id: str,
    *,
    timeout: float = 180,
) -> Any:
    """Read one exact native terminal Turn without ``handle.run()``."""

    deadline = asyncio.get_running_loop().time() + timeout
    response_materialization_retries = _FINAL_RESPONSE_MATERIALIZATION_RETRIES
    while asyncio.get_running_loop().time() < deadline:
        try:
            summary = await thread.read(include_turns=False)
        except Exception as error:
            if not _is_transient_read_error(
                error,
                thread_id=thread.id,
                include_turns=False,
            ):
                raise
            await asyncio.sleep(0.5)
            continue

        thread_status = _thread_status_type(summary.thread)
        if thread_status in {"notLoaded", "active"}:
            await asyncio.sleep(0.5)
            continue
        if thread_status != "idle":
            raise RuntimeError(f"unexpected native Thread status: {thread_status!r}")

        try:
            snapshot = await thread.read(include_turns=True)
        except Exception as error:
            if not _is_transient_read_error(
                error,
                thread_id=thread.id,
                include_turns=True,
            ):
                raise
            await asyncio.sleep(0.5)
            continue
        exact = next(
            (turn for turn in snapshot.thread.turns if turn.id == turn_id),
            None,
        )
        if exact is None or _status_value(exact) == "inProgress":
            await asyncio.sleep(0.5)
            continue
        status = _status_value(exact)
        if status not in {"completed", "interrupted", "failed"}:
            raise AssertionError(f"native Turn ended with {status!r}")
        if (
            status == "completed"
            and _final_response_from_turn(exact) is None
            and response_materialization_retries > 0
        ):
            response_materialization_retries -= 1
            await asyncio.sleep(0.5)
            continue
        return exact
    raise TimeoutError("did not observe a terminal native Turn")


async def _public_final_response(
    thread: Any,
    turn_id: str,
    *,
    timeout: float = 180,
) -> str:
    exact = await _public_terminal_turn(thread, turn_id, timeout=timeout)
    status = _status_value(exact)
    if status != "completed":
        raise AssertionError(f"native Turn ended with {status!r}")
    final_response = _final_response_from_turn(exact)
    if final_response is None:
        raise AssertionError("native Turn has no final agent response")
    return final_response.strip()


async def _config_reload(cwd_root: Path) -> dict[str, Any]:
    """Distinguish same-process config reload from restart-only loading.

    The probe changes only a project-local config inside a temporary directory
    and removes that directory on exit. It never reads or writes the user's
    global ``config.toml``.
    """

    with tempfile.TemporaryDirectory(
        prefix=".netizen-config-probe-",
        dir=cwd_root,
    ) as temporary:
        project = Path(temporary)
        config_dir = project / ".codex"
        config_dir.mkdir(mode=0o700)
        config_path = config_dir / "config.toml"
        first_name = ".netizen-config-a"
        second_name = ".netizen-config-b"
        (project / first_name).write_text(
            "Reply to every user message with exactly CONFIG-A and nothing else.\n",
            encoding="utf-8",
        )
        config_path.write_text(
            f'project_doc_fallback_filenames = ["{first_name}"]\n',
            encoding="utf-8",
        )

        async with AsyncCodex() as codex:
            first_thread = await codex.thread_start(cwd=str(project))
            first_handle = await first_thread.turn("Follow the project instructions.")
            first_response = await _public_final_response(
                first_thread,
                first_handle.id,
            )
            (project / second_name).write_text(
                "Reply to every user message with exactly CONFIG-B and nothing else.\n",
                encoding="utf-8",
            )
            config_path.write_text(
                f'project_doc_fallback_filenames = ["{second_name}"]\n',
                encoding="utf-8",
            )
            same_thread = await codex.thread_start(cwd=str(project))
            same_handle = await same_thread.turn("Follow the project instructions.")
            same_process_response = await _public_final_response(
                same_thread,
                same_handle.id,
            )

        async with AsyncCodex() as restarted_codex:
            restarted_thread = await restarted_codex.thread_start(cwd=str(project))
            restarted_handle = await restarted_thread.turn(
                "Follow the project instructions."
            )
            restarted_response = await _public_final_response(
                restarted_thread,
                restarted_handle.id,
            )

        if first_response != "CONFIG-A":
            raise AssertionError(
                "initial project config was not applied: "
                f"{first_response!r}"
            )
        if same_process_response not in {"CONFIG-A", "CONFIG-B"}:
            raise AssertionError(
                "same-process response did not follow either probe config: "
                f"{same_process_response!r}"
            )
        if restarted_response != "CONFIG-B":
            raise AssertionError(
                "restarted App Server did not load the updated project config: "
                f"{restarted_response!r}"
            )
        return {
            "project_config_scope": "temporary",
            "global_config_modified": False,
            "first_response": first_response,
            "same_process_response": same_process_response,
            "restarted_response": restarted_response,
            "reload_behavior": (
                "hot-reloaded"
                if same_process_response == "CONFIG-B"
                else "restart-required"
            ),
        }


async def _sandbox_inheritance(cwd_root: Path) -> dict[str, Any]:
    """Classify effective write access with production-default SDK arguments.

    The probe does not write any Codex config and passes neither sandbox nor
    approval overrides. The result therefore describes the current user-facing
    behavior; it does not pretend to identify which upstream config layer or
    default selected that behavior.
    """

    with tempfile.TemporaryDirectory(
        prefix=".netizen-sandbox-probe-",
        dir=cwd_root,
    ) as temporary:
        project = Path(temporary)
        marker = project / "sandbox-marker"
        prompt = (
            "Use the terminal to run exactly this command once: "
            f"/usr/bin/touch {marker}. Then briefly report whether it worked."
        )

        async with AsyncCodex() as codex:
            thread = await codex.thread_start(cwd=str(project))
            handle = await thread.turn(prompt)
            response = await _public_final_response(thread, handle.id)
        marker_created = marker.is_file()

        return {
            "project_config_scope": "temporary",
            "global_config_modified": False,
            "sdk_sandbox_override": False,
            "sdk_approval_override": False,
            "thread_id": thread.id,
            "marker_created": marker_created,
            "effective_access": (
                "workspace-write-or-full"
                if marker_created
                else "read-only-or-denied"
            ),
            "response": response,
        }


async def _concurrency(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    terminal_cleanup = PinnedExperimentalTerminalCleanup(codex)
    markers = (
        f"netizen-sdk-probe-concurrent-a-{time.time_ns()}",
        f"netizen-sdk-probe-concurrent-b-{time.time_ns()}",
    )
    threads = await asyncio.gather(
        codex.thread_start(cwd=str(cwd)),
        codex.thread_start(cwd=str(cwd)),
    )
    if threads[0].id == threads[1].id:
        raise AssertionError("two thread_start calls returned the same native ID")
    handles = await asyncio.gather(
        threads[0].turn(
            "Use the terminal to run exactly this command and wait for it: "
            f"/bin/bash -lc 'exec -a {markers[0]} /bin/sleep 30'. "
            "Then reply exactly: PROBE-A"
        ),
        threads[1].turn(
            "Use the terminal to run exactly this command and wait for it: "
            f"/bin/bash -lc 'exec -a {markers[1]} /bin/sleep 30'. "
            "Then reply exactly: PROBE-B"
        ),
    )
    tasks = [
        asyncio.create_task(_public_terminal_turn(thread, handle.id))
        for thread, handle in zip(threads, handles, strict=True)
    ]
    try:
        observed = await _wait_for_process_overlap(markers, timeout=75)
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=90
        )
    except BaseException:
        await _cleanup_turns(
            tuple(handles),
            tuple(tasks),
            terminal_cleanup=terminal_cleanup,
        )
        raise
    await asyncio.gather(
        *(
            _wait_for_process(marker, present=False, timeout=10)
            for marker in markers
        )
    )
    statuses = [
        type(result).__name__
        if isinstance(result, BaseException)
        else _status_value(result)
        for result in results
    ]
    final_responses = [
        None
        if isinstance(result, BaseException)
        else _final_response_from_turn(result)
        for result in results
    ]
    if statuses != ["completed", "completed"]:
        raise AssertionError(f"concurrent Turns did not both complete: {statuses}")
    if any(
        expected not in (response or "")
        for expected, response in zip(("PROBE-A", "PROBE-B"), final_responses)
    ):
        raise AssertionError(
            f"concurrent Turns returned unexpected responses: {final_responses}"
        )
    return {
        "thread_ids": [thread.id for thread in threads],
        "turn_ids": [handle.id for handle in handles],
        "observed_pids": observed,
        "statuses": statuses,
        "final_responses": final_responses,
    }


async def _interrupt_orphan(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    terminal_cleanup = PinnedExperimentalTerminalCleanup(codex)
    background_cleanup_requested = False
    marker = f"netizen-sdk-probe-orphan-{time.time_ns()}"
    thread = await codex.thread_start(cwd=str(cwd))
    handle = await thread.turn(
        "Use the terminal to run exactly this command and wait for it: "
        f"/bin/bash -lc 'exec -a {marker} /bin/sleep 15'. "
        "After it exits, reply exactly: ORPHAN-PROBE-DONE"
    )
    task = asyncio.create_task(_public_terminal_turn(thread, handle.id))
    try:
        observed = await _wait_for_process(marker, present=True, timeout=45)
        await handle.interrupt()
        await terminal_cleanup.clean_thread(thread.id)
        background_cleanup_requested = True
        foreground_process_exited = await _process_exited_within(
            marker,
            timeout=5,
        )
        result = await asyncio.wait_for(task, timeout=60)
        if not foreground_process_exited:
            # The pinned App Server does not register a foreground tool in the
            # background-terminal registry. Wait for this bounded sleep to end
            # naturally so the compatibility probe never leaves its own child
            # behind, without pretending the cleanup RPC terminated it.
            await _wait_for_process(marker, present=False, timeout=20)
    except BaseException:
        await _cleanup_turns(
            (handle,),
            (task,),
            terminal_cleanup=terminal_cleanup,
        )
        raise
    status = _status_value(result)
    if status != "interrupted":
        raise AssertionError(f"interrupt probe ended with unexpected status: {status!r}")
    orphan_pids = _matching_processes(marker)
    if orphan_pids:
        raise AssertionError(f"cleanup left marker processes: {orphan_pids}")
    resumed = await codex.thread_resume(thread.id)
    if resumed.id != thread.id:
        raise AssertionError("thread_resume returned a different native Thread ID")
    resume_handle = await resumed.turn("Reply exactly: AFTER-CLEANUP")
    resume_response = await _public_final_response(resumed, resume_handle.id)
    if resume_response != "AFTER-CLEANUP":
        raise AssertionError(
            "the cleaned native Thread did not resume normally: "
            f"{resume_response!r}"
        )
    return {
        "thread_id": thread.id,
        "turn_id": handle.id,
        "observed_pids": observed,
        "status": status,
        "background_cleanup_requested": background_cleanup_requested,
        "foreground_process_exited_within_5s": foreground_process_exited,
        "orphan_pids": orphan_pids,
        "resume_turn_id": resume_handle.id,
        "resume_response": resume_response,
    }


async def _cleanup_turns(
    handles: tuple[Any, ...],
    tasks: tuple[asyncio.Task[Any], ...],
    *,
    terminal_cleanup: TerminalCleanup | None = None,
    operation_timeout: float = 10,
) -> None:
    """Best-effort native Turn cleanup for a failed probe phase."""

    try:
        await asyncio.wait_for(
            asyncio.gather(
                *(handle.interrupt() for handle in handles),
                return_exceptions=True,
            ),
            timeout=operation_timeout,
        )
    except TimeoutError:
        print(
            "[probe] cleanup: native interrupt timed out",
            file=sys.stderr,
            flush=True,
        )
    if terminal_cleanup is not None:
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        terminal_cleanup.clean_thread(handle.thread_id)
                        for handle in handles
                    ),
                    return_exceptions=True,
                ),
                timeout=operation_timeout,
            )
        except TimeoutError:
            print(
                "[probe] cleanup: terminal cleanup timed out",
                file=sys.stderr,
                flush=True,
            )
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=operation_timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def probe(cwd: Path, *, phases: tuple[str, ...]) -> dict[str, Any]:
    migrations = facade_migration_requirements()
    if migrations:
        raise AssertionError("; ".join(migrations))
    result: dict[str, Any] = {
        "cwd": str(cwd),
        "openai_codex_version": openai_codex.__version__,
        "sdk_gap_facade_migrations": list(migrations),
    }
    regular_phases = tuple(
        phase
        for phase in phases
        if phase not in {"config", "sandbox", "goal", "release"}
    )
    if regular_phases:
        async with AsyncCodex() as codex:
            account = await codex.account()
            result["account_type"] = getattr(
                getattr(account, "account", None),
                "type",
                None,
            )
            if "models" in regular_phases:
                await _record_phase(
                    result,
                    "models",
                    _model_catalog(codex),
                )
            if "turn-settings" in regular_phases:
                await _record_phase(
                    result,
                    "turn-settings",
                    _turn_settings(codex, cwd),
                )
            if "smoke" in regular_phases:
                await _record_phase(result, "smoke", _smoke(codex, cwd))
            if "usage" in regular_phases:
                await _record_phase(result, "usage", _context_usage(codex, cwd))
            if "steer" in regular_phases:
                await _record_phase(result, "steer", _steer(codex, cwd))
            if "plan" in regular_phases:
                await _record_phase(result, "plan", _turn_plan_live(codex, cwd))
            if "polling" in regular_phases:
                await _record_phase(
                    result,
                    "polling",
                    _polling_completion(codex, cwd),
                )
            if "compact" in regular_phases:
                await _record_phase(
                    result,
                    "compact",
                    _compact(codex, cwd),
                )
            if "concurrency" in regular_phases:
                await _record_phase(
                    result,
                    "concurrency",
                    _concurrency(codex, cwd),
                )
            if "interrupt" in regular_phases:
                await _record_phase(
                    result,
                    "interrupt",
                    _interrupt_orphan(codex, cwd),
                )
            if "skills" in regular_phases:
                await _record_phase(
                    result,
                    "skills",
                    _skills_live(codex, cwd),
                )
            if "lifecycle" in regular_phases:
                await _record_phase(
                    result,
                    "lifecycle",
                    _thread_lifecycle_live(codex, cwd),
                )
            if "side" in regular_phases:
                await _record_phase(result, "side", _side_live(codex, cwd))
    if "config" in phases:
        await _record_phase(result, "config", _config_reload(cwd))
    if "sandbox" in phases:
        await _record_phase(result, "sandbox", _sandbox_inheritance(cwd))
    if "goal" in phases:
        await _record_phase(result, "goal", _goal_live(cwd))
    if "release" in phases:
        await _record_phase(result, "release", _release_live(cwd))
    return result


async def _skills_live(codex: AsyncCodex, parent: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="netizen-skills-", dir=parent) as raw:
        cwd = Path(raw).resolve()
        definitions = {
            "netizen-probe-one": "NETIZEN-SKILL-ONE",
            "netizen-probe-two": "NETIZEN-SKILL-TWO",
        }
        for name, marker in definitions.items():
            root = cwd / ".agents" / "skills" / name
            root.mkdir(parents=True)
            (root / "SKILL.md").write_text(
                "---\n"
                f"name: {name}\n"
                f"description: Bounded Netizen live probe for {marker}.\n"
                "---\n\n"
                f"When explicitly invoked, include `{marker}` in the final answer.\n",
                encoding="utf-8",
            )
        catalog = AppServerSkillCatalog(codex)
        snapshot = await catalog.list(cwd, force_reload=True)
        by_name = {skill.name: skill for skill in snapshot.skills if skill.enabled}
        missing = sorted(set(definitions) - set(by_name))
        if missing or snapshot.errors:
            raise AssertionError(
                f"live Skills discovery failed: {missing=}, {snapshot.errors=}"
            )
        text = (
            "$netizen-probe-one $netizen-probe-two "
            "Apply both probe Skills and reply with both required markers."
        )
        native_input = [
            TextInput(text),
            *(
                SkillInput(name=name, path=by_name[name].path)
                for name in definitions
            ),
        ]
        thread = await codex.thread_start(cwd=str(cwd))
        handle = await thread.turn(native_input)
        terminal = await _public_terminal_turn(thread, handle.id)
        final = _final_response_from_turn(terminal) or ""
        if not all(marker in final for marker in definitions.values()):
            raise AssertionError(
                f"typed multi-Skill Turn did not expose both markers: {final!r}"
            )

        steer_marker = f"netizen-skill-steer-{time.time_ns()}"
        steer_handle = await thread.turn(
            "Use the terminal to run exactly this command and wait for it: "
            f"/bin/bash -lc 'exec -a {steer_marker} /bin/sleep 8'. "
            "Then reply exactly: SKILL-STEER-ORIGINAL"
        )
        steer_task = asyncio.create_task(
            _public_terminal_turn(thread, steer_handle.id)
        )
        try:
            await _wait_for_process(steer_marker, present=True, timeout=45)
            skill = by_name["netizen-probe-one"]
            await steer_handle.steer(
                [
                    TextInput(
                        "$netizen-probe-one Change the final reply and include "
                        "NETIZEN-SKILL-STEERED."
                    ),
                    SkillInput(name=skill.name, path=skill.path),
                ]
            )
            steer_terminal = await asyncio.wait_for(steer_task, timeout=180)
        except BaseException:
            await _cleanup_turns((steer_handle,), (steer_task,))
            raise
        steer_final = _final_response_from_turn(steer_terminal) or ""
        if "NETIZEN-SKILL-STEERED" not in steer_final:
            raise AssertionError(
                f"typed Skill steer did not change the final response: {steer_final!r}"
            )
        return {
            "cwd": str(cwd),
            "skill_names": sorted(definitions),
            "turn_id": handle.id,
            "turn_final_response": final,
            "steer_turn_id": steer_handle.id,
            "steer_final_response": steer_final,
            "text_marker_plus_typed_input": True,
        }


async def _goal_live(cwd: Path) -> dict[str, Any]:
    async with AsyncCodex() as codex:
        control = AppServerGoalControl(codex)
        terminal_cleanup = PinnedExperimentalTerminalCleanup(codex)
        thread = await codex.thread_start(cwd=str(cwd))
        try:
            zero_turn = await thread.read(include_turns=False)
        except Exception as error:
            raise AssertionError(
                "zero-Turn Thread is not publicly readable/persisted; "
                "Goal must remain disabled for this SDK/App Server build"
            ) from error
        native = zero_turn.thread
        if (
            _thread_status_type(native) != "idle"
            or getattr(native, "ephemeral", None) is not False
            or not getattr(native, "path", None)
        ):
            raise AssertionError(
                "zero-Turn Thread lacks idle, non-ephemeral persisted state; "
                "Goal must remain disabled"
            )

        goal_file_name = f"netizen-goal-diff-{time.time_ns()}.txt"
        goal_file = cwd / goal_file_name
        goal_file_content = "NETIZEN-GOAL-DIFF-LIVE"
        handle = await control.start(
            thread.id,
            "Run /bin/sleep 12 as a bounded first step. After any resume, "
            f"create {goal_file_name} in the current working directory with exact "
            f"content {goal_file_content}, then reply exactly "
            "GOAL-LIVE-DONE and complete this Goal.",
        )
        await asyncio.sleep(1)
        try:
            # A second SDK client has no local notification route for this
            # operation.  It must still observe the persisted active Goal
            # read-only, which is the live surface Runtime uses after restart
            # or when another Codex client owns continuation.
            async with AsyncCodex() as observer:
                observed = await AppServerGoalControl(observer).get(thread.id)
                if observed is None or observed.status is not GoalStatus.ACTIVE:
                    raise AssertionError(
                        "a second SDK client could not reconcile the active Goal"
                    )
            pause = await handle.pause()
            await terminal_cleanup.clean_thread(thread.id)
            first_terminal = await asyncio.wait_for(
                handle.wait_terminal(),
                timeout=60,
            )
            paused = await control.get(thread.id)
            if paused is None or paused.status is not GoalStatus.PAUSED:
                raise AssertionError("Goal pause was not persisted")
            resumed = await control.resume(thread.id)
            resumed_terminal = await asyncio.wait_for(
                resumed.wait_terminal(),
                timeout=300,
            )
            terminal_goal = await control.get(thread.id)
            if terminal_goal is None or not terminal_goal.status.terminal_or_paused:
                raise AssertionError("resumed Goal did not reach a terminal status")
            if resumed_terminal.turn_diff is None:
                raise AssertionError(
                    "resumed Goal final physical Turn omitted its aggregate diff"
                )
            summary = turn_diff_summary(resumed_terminal.turn_diff)
            file_stats = next(
                (
                    item
                    for item in summary.files
                    if item.path == goal_file_name
                ),
                None,
            )
            if (
                file_stats is None
                or file_stats.additions != 1
                or file_stats.deletions != 0
                or not goal_file.is_file()
                or goal_file.read_text(encoding="utf-8").strip() != goal_file_content
            ):
                raise AssertionError(
                    "resumed Goal diff did not identify the exact generated file"
                )
        except BaseException:
            for candidate in (locals().get("resumed"), handle):
                if candidate is None:
                    continue
                try:
                    await candidate.pause()
                except Exception:
                    pass
                try:
                    await terminal_cleanup.clean_thread(thread.id)
                except Exception:
                    pass
                try:
                    await candidate.aclose()
                except Exception:
                    pass
            raise

        snapshot = await thread.read(include_turns=True)
        goal_turn_ids = [turn.id for turn in snapshot.thread.turns]
        followup = await thread.turn("Reply exactly: AFTER-GOAL-LIVE")
        followup_terminal = await _public_terminal_turn(thread, followup.id)
        followup_final = _final_response_from_turn(followup_terminal)
        if followup_final != "AFTER-GOAL-LIVE":
            raise AssertionError("same Thread did not accept a normal Turn after Goal")
        return {
            "thread_id": thread.id,
            "zero_turn_persisted": True,
            "first_logical_turn_id": handle.id,
            "first_physical_terminal_id": first_terminal.final_physical_turn_id,
            "pause_turn_id": pause.physical_turn_id,
            "external_active_read_only_reconcile": True,
            "resumed_logical_turn_id": resumed.id,
            "resumed_physical_terminal_id": resumed_terminal.final_physical_turn_id,
            "resumed_diff_file": goal_file_name,
            "resumed_diff_additions": file_stats.additions,
            "resumed_diff_deletions": file_stats.deletions,
            "terminal_status": terminal_goal.status.value,
            "observed_turn_ids": goal_turn_ids,
            "same_thread_followup_turn_id": followup.id,
            "same_thread_followup_response": followup_final,
        }


async def _model_catalog(codex: AsyncCodex) -> dict[str, Any]:
    catalog = ModelCatalog.from_response(await codex.models())
    return {
        "default_model_id": catalog.default_model.id,
        "models": [
            {
                "id": model.id,
                "model": model.model,
                "is_default": model.is_default,
                "default_effort": model.default_effort_id,
                "efforts": [effort.id for effort in model.efforts],
                "default_speed": model.default_service_tier_id,
                "speeds": [
                    {"id": tier.id, "name": tier.name}
                    for tier in catalog.service_tier_options
                    if tier.id == STANDARD_SERVICE_TIER_ID
                    or any(native.id == tier.id for native in model.service_tiers)
                ],
            }
            for model in catalog.models
        ],
    }


async def _turn_settings(codex: AsyncCodex, cwd: Path) -> dict[str, Any]:
    """Exercise the same dynamic override on consecutive same-Thread Turns."""

    catalog = ModelCatalog.from_response(await codex.models())
    model = catalog.default_model
    effort = next(
        option
        for option in model.efforts
        if option.id == model.default_effort_id
    )
    thread = await codex.thread_start(cwd=str(cwd))
    configured = await thread.turn(
        "Reply exactly: TURN-SETTINGS-CONFIGURED",
        model=model.model,
        effort=effort.wire_value,
        service_tier=STANDARD_SERVICE_TIER_ID,
    )
    configured_terminal = await _public_terminal_turn(thread, configured.id)
    configured_final = _final_response_from_turn(configured_terminal) or ""
    if (
        _status_value(configured_terminal) != "completed"
        or "TURN-SETTINGS-CONFIGURED" not in configured_final
    ):
        raise AssertionError(
            "dynamic Turn settings override did not complete: "
            f"{configured_final!r}"
        )

    resumed = await codex.thread_resume(thread.id)
    reapplied = await resumed.turn(
        "Reply exactly: TURN-SETTINGS-REAPPLIED",
        model=model.model,
        effort=effort.wire_value,
        service_tier=STANDARD_SERVICE_TIER_ID,
    )
    reapplied_terminal = await _public_terminal_turn(resumed, reapplied.id)
    reapplied_final = _final_response_from_turn(reapplied_terminal) or ""
    if (
        _status_value(reapplied_terminal) != "completed"
        or "TURN-SETTINGS-REAPPLIED" not in reapplied_final
    ):
        raise AssertionError(
            "same Thread rejected the reapplied Turn settings override: "
            f"{reapplied_final!r}"
        )
    return {
        "thread_id": thread.id,
        "configured_turn_id": configured.id,
        "followup_turn_id": reapplied.id,
        "model_id": model.id,
        "model": model.model,
        "effort": effort.id,
        "speed": STANDARD_SERVICE_TIER_ID,
        "followup_reapplied_overrides": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "run models, turn-settings, smoke, usage, steer, plan, polling, compact, "
            "concurrency, interrupt, skills, lifecycle, side, goal, release, and "
            "sandbox phases"
        ),
    )
    parser.add_argument(
        "--phase",
        action="append",
        choices=(
            "models",
            "turn-settings",
            "smoke",
            "usage",
            "steer",
            "plan",
            "polling",
            "compact",
            "concurrency",
            "interrupt",
            "skills",
            "lifecycle",
            "side",
            "goal",
            "release",
            "config",
            "sandbox",
        ),
        help="run one phase; repeat to select multiple (default: smoke)",
    )
    args = parser.parse_args()
    if args.live and args.phase:
        parser.error("--live and --phase cannot be combined")
    return args


def main() -> None:
    args = parse_args()
    cwd = args.cwd.expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise SystemExit("--cwd must be a directory")
    phases = (
        (
            "models",
            "turn-settings",
            "smoke",
            "usage",
            "steer",
            "plan",
            "polling",
            "compact",
            "concurrency",
            "interrupt",
            "skills",
            "lifecycle",
            "side",
            "goal",
            "release",
            "sandbox",
        )
        if args.live
        else tuple(args.phase or ("smoke",))
    )
    print(
        json.dumps(
            asyncio.run(probe(cwd, phases=phases)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
