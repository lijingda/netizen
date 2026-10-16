#!/usr/bin/env python3
"""Verify exact archive restoration with disposable bundled App Server resources.

Uses real CodexRuntime/SQLite and a loopback mock Responses provider, without a
real model account or Feishu calls. All native resources live in temporary
HOME/CODEX_HOME directories. Run explicitly with an external deadline, e.g.
``timeout --signal=INT --kill-after=5s 90s .venv/bin/python
scripts/probe_thread_restore.py --output /tmp/thread-restore.json``.
"""

from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any
import uuid

from openai_codex import (
    ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, InvalidRequestError, Sandbox,
)

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen_cli.bindings import BindingStore, ThreadBinding  # noqa: E402
from netizen_cli.codex_runtime import CodexRuntime, ThreadRestoreRejected  # noqa: E402
from netizen_cli.domain import FeishuScope, ScopeKind  # noqa: E402
from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup  # noqa: E402
from scripts.probe_sdk_partial_answers import (  # noqa: E402
    isolated_config, message_events, mock_provider, response_events,
)


# Task-local tracing permits the existing Turn consumer to continue observing
# while rejecting any catalog/read/Turn request by the restore operation itself.
_RESTORE_CALLS: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "restore_probe_calls", default=None,
)


class _ObservedThread:
    def __init__(self, thread: AsyncThread) -> None:
        self.id = thread.id
        self.thread = thread

    def __getattr__(self, name: str) -> Any:
        if _RESTORE_CALLS.get() is not None:
            raise AssertionError(f"restore unexpectedly accessed Thread.{name}")
        return getattr(self.thread, name)


class _ObservedCodex:
    """Observe the public Runtime port without replacing native responses."""

    def __init__(self, codex: AsyncCodex) -> None:
        self.codex = codex

    def __getattr__(self, name: str) -> Any:
        if _RESTORE_CALLS.get() is not None:
            raise AssertionError(f"restore unexpectedly accessed Codex.{name}")
        return getattr(self.codex, name)

    async def _call(self, method: str, thread_id: str, **kwargs: object) -> _ObservedThread:
        calls = _RESTORE_CALLS.get()
        call: dict[str, Any] = {"method": method, "thread_id": thread_id}
        if calls is not None:
            calls.append(call)
        try:
            thread = await getattr(self.codex, method)(thread_id, **kwargs)
        except InvalidRequestError as error:
            call.update(error_code=error.code, error_message=error.message)
            raise
        else:
            call["returned_id"] = thread.id
            return _ObservedThread(thread)

    async def thread_unarchive(self, thread_id: str) -> _ObservedThread:
        return await self._call("thread_unarchive", thread_id)

    async def thread_resume(self, thread_id: str, **kwargs: object) -> _ObservedThread:
        if _RESTORE_CALLS.get() is not None:
            assert kwargs == {"include_turns": False}, kwargs
        return await self._call("thread_resume", thread_id, **kwargs)


def _runtime(codex: AsyncCodex, store: BindingStore) -> CodexRuntime:
    return CodexRuntime(
        codex=_ObservedCodex(codex), bindings=store,
        terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
        automatic_thread_naming=False,
    )


def _binding(store: BindingStore, thread_id: str | None = None) -> ThreadBinding:
    binding = store.create_channel_binding(
        scope=FeishuScope("probe-app", "probe-chat", ScopeKind.DIRECT),
        project_alias="restore-probe", creator_id="probe-owner",
    )
    if thread_id is not None:
        store.assign_native_thread_id(binding.id, thread_id)
    return store.get(binding.id)


async def _restore(
    runtime: CodexRuntime, store: BindingStore, binding: ThreadBinding,
    name: str, *, rejection: str | None = None,
) -> dict[str, Any]:
    previous = store.active_binding(binding.scope_key)
    before = store.get(binding.id)
    calls: list[dict[str, Any]] = []
    token = _RESTORE_CALLS.set(calls)
    try:
        try:
            result = await runtime.restore_as_current_exact(binding.id)
        except ThreadRestoreRejected as error:
            assert rejection is not None, (name, error)
            assert not error.unarchived, (name, error)
            assert store.get(binding.id) == before
            assert store.active_binding(binding.scope_key) == previous
        else:
            assert rejection is None, name
            assert result.native_thread_id == binding.native_thread_id
            assert store.active_binding(binding.scope_key).id == binding.id
    finally:
        _RESTORE_CALLS.reset(token)
    assert runtime.lifecycle_state(binding.id) is None, name
    assert runtime._accepting, name
    assert [call["method"] for call in calls] == ["thread_unarchive", "thread_resume"], calls
    assert all(call["thread_id"] == binding.native_thread_id for call in calls), calls
    if rejection is not None:
        assert calls[1].get("error_code") == -32600, calls
        assert calls[1].get("error_message") == rejection, calls
    return {"scenario": name, "passed": True, "native_calls": calls,
            "restore_catalog_or_read_calls": 0}


async def _drain(handle: Any) -> None:
    async with asyncio.timeout(15):
        async for event in handle.stream():
            if event.method == "turn/completed":
                assert event.payload.turn.status.value == "completed", event.payload.turn
                return
    raise AssertionError("no terminal notification")


async def _scenario(root: Path) -> dict[str, Any]:
    replies = [(200, response_events(name, *message_events(
        name + "-message", text, "final_answer",
    ))) for name, text in (("seed", "RESTORE-SEED"), ("running", "RESTORE-DONE"))]
    cases: list[dict[str, Any]] = []
    with mock_provider(replies) as provider:
        cwd, env = isolated_config(root, provider)
        # Preserve the host's configured outbound proxy; only the disposable
        # loopback provider bypasses it.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
        for key in ("NO_PROXY", "no_proxy"):
            env[key] = os.environ.get(key, "") + ",127.0.0.1,localhost"
        config = CodexConfig(cwd=str(cwd), env=env)
        store = BindingStore(root / "channel.sqlite3")
        store.bootstrap_project(alias="restore-probe", cwd=str(cwd))
        try:
            async with AsyncCodex(config) as codex:
                runtime = _runtime(codex, store)
                try:
                    thread = await codex.thread_start(
                        cwd=str(cwd), approval_mode=ApprovalMode.deny_all,
                        sandbox=Sandbox.read_only,
                    )
                    seed = await thread.turn("Use the first scripted probe response.")
                    await _drain(seed)
                    binding = _binding(store, thread.id)
                    await codex.thread_archive(thread.id)
                    _binding(store)
                    cases.append(await _restore(runtime, store, binding, "archived"))
                    cases.append(await _restore(runtime, store, binding, "loaded_idle"))
                    assert [turn.id for turn in (await thread.read(include_turns=True)).thread.turns] == [seed.id]

                    gate = provider.response_gates[2] = threading.Event()
                    submission = await runtime.submit(
                        binding=store.get(binding.id), cwd=cwd,
                        input="Use the second scripted probe response.",
                        owner_id="probe-owner", origin=None,
                    )
                    assert submission.release_receipt_attempt is not None
                    submission.release_receipt_attempt()
                    async with asyncio.timeout(10):
                        while len(provider.requests) < 2:
                            await asyncio.sleep(0.01)
                    active = runtime._active[binding.id]
                    native_handle = active.handle
                    original_thread = active.thread
                    _binding(store)
                    cases.append(await _restore(runtime, store, binding, "running_rejoin"))
                    assert runtime._active[binding.id] is active
                    assert active.handle is native_handle and active.thread is original_thread
                    assert runtime._subscriptions[binding.id].thread.id == original_thread.id

                    async with AsyncCodex(config) as foreign:
                        foreign_store = BindingStore(root / "foreign.sqlite3")
                        foreign_store.bootstrap_project(alias="restore-probe", cwd=str(cwd))
                        foreign_runtime = _runtime(foreign, foreign_store)
                        try:
                            foreign_binding = _binding(foreign_store, thread.id)
                            _binding(foreign_store)
                            cases.append(await _restore(
                                foreign_runtime, foreign_store, foreign_binding, "foreign_writer",
                                rejection=f"thread {thread.id} already has an active writer",
                            ))
                        finally:
                            foreign_runtime.close_admission()
                            await foreign_runtime.cancel_tasks()
                            foreign_store.close()
                    snapshot = (await thread.read(include_turns=True)).thread
                    assert snapshot.turns[-1].id == submission.turn_id
                    assert snapshot.turns[-1].status.value == "inProgress"
                    assert runtime._active[binding.id].handle is native_handle
                    gate.set()
                    assert await runtime.wait_idle(timeout=15)
                    assert runtime.active_turn(binding.id) is None

                    missing = _binding(store, str(uuid.uuid4()))
                    _binding(store)
                    cases.append(await _restore(
                        runtime, store, missing, "missing",
                        rejection=f"no rollout found for thread id {missing.native_thread_id}",
                    ))
                finally:
                    for response_gate in provider.response_gates.values():
                        response_gate.set()
                    runtime.close_admission()
                    await runtime.cancel_tasks()

            async with AsyncCodex(config) as cold:
                runtime = _runtime(cold, store)
                try:
                    case = await _restore(runtime, store, binding, "cold_already_unarchived")
                    assert case["native_calls"][0].get("error_message") == (
                        f"no archived rollout found for thread id {thread.id}"
                    ), case
                    cases.append(case)
                    snapshot = (await AsyncThread(cold, thread.id).read(include_turns=True)).thread
                    assert [turn.id for turn in snapshot.turns] == [seed.id, submission.turn_id]
                    assert all(turn.status.value == "completed" for turn in snapshot.turns)
                finally:
                    runtime.close_admission()
                    await runtime.cancel_tasks()
        finally:
            store.close()
        assert not provider.errors and not provider.replies, (provider.errors, len(provider.replies))
        assert len(provider.requests) == 2, len(provider.requests)
    return {
        "passed": True, "sdk_version": importlib.metadata.version("openai-codex"),
        "cli_version": importlib.metadata.version("openai-codex-cli-bin"),
        "transport": "bundled App Server + local mock Responses HTTP SSE",
        "real_model_used": False, "feishu_delivery_tested": False,
        "model_request_count": 2, "running_handle_preserved": True,
        "native_history_exactly_two_turns": True, "cases": cases,
    }


async def probe(timeout: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="netizen-thread-restore-") as directory:
        async with asyncio.timeout(timeout):
            return await _scenario(Path(directory))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=60, help="Overall deadline in seconds")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    result = asyncio.run(probe(args.timeout))
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
