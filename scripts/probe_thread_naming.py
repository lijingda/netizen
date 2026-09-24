#!/usr/bin/env python3
"""Probe automatic naming using only this run's disposable native Threads.

Run in the service account's interactive login environment with the pinned SDK.
The temporary Git cwd and ordinary parent Thread belong to this probe; no
existing Thread or user configuration is changed. Bound the entire command with
``timeout --signal=INT --kill-after=10s 420s`` (``gtimeout`` on macOS).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import uuid

import openai_codex
from openai_codex import AsyncCodex


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen.sdk_gap_adapter import (  # noqa: E402
    AppServerThreadSubscriptionControl,
    facade_migration_requirements,
)
from netizen.bindings import BindingStore, BindingTurnSettings  # noqa: E402
from netizen.codex_runtime import CodexRuntime, SubmitDisposition  # noqa: E402
from netizen.domain import FeishuScope, ScopeKind  # noqa: E402
from netizen.model_settings import ModelCatalog, STANDARD_SERVICE_TIER_ID  # noqa: E402
from netizen.runtime.thread_naming import NAMING_OUTPUT_SCHEMA, NAMING_PROMPT  # noqa: E402
from netizen.terminal_cleanup import PinnedExperimentalTerminalCleanup  # noqa: E402
from scripts.probe_python_sdk import (  # noqa: E402
    _prove_thread_absent_from_all_catalogs,
    _public_final_response,
    _status_value,
)


def _progress(message: str) -> None:
    print(f"[probe] thread-naming: {message}", file=sys.stderr, flush=True)


def _validate_title(result: Any, marker: str) -> tuple[str, list[str]]:
    if _status_value(result) != "completed":
        raise AssertionError("naming Turn did not complete")
    response = getattr(result, "final_response", None)
    if not isinstance(response, str):
        raise AssertionError("naming Turn returned no structured response")
    try:
        payload = json.loads(response)
    except (ValueError, RecursionError) as error:
        raise AssertionError("naming Turn returned invalid JSON") from error
    if (
        not isinstance(payload, dict)
        or payload.keys() != {"title"}
        or not isinstance(payload["title"], str)
    ):
        raise AssertionError("naming Turn returned an invalid title object")
    title = payload["title"]
    title = title.strip()
    if marker not in title or len(title) > 120 or len(title.splitlines()) != 1:
        raise AssertionError(
            "naming fork did not return a single-line title containing the "
            "unique project identifier from the parent's first user message"
        )
    item_types = [
        getattr(getattr(item, "root", item), "type", None)
        for item in result.items
    ]
    if any(kind not in {"userMessage", "agentMessage", "reasoning"} for kind in item_types):
        raise AssertionError(f"naming Turn executed a tool: {item_types!r}")
    return title, item_types


async def _release_fork(
    fork: Any,
    handle: Any | None,
    consumer: asyncio.Task[Any] | None,
    cleanup: Any,
    subscription: Any,
) -> dict[str, str | None]:
    """Bound cleanup and never let one failed step omit unsubscribe."""

    terminal_status = None
    failures: list[str] = []
    if handle is not None and consumer is not None and not consumer.done():
        try:
            await asyncio.wait_for(handle.interrupt(), timeout=10)
            terminal = await asyncio.wait_for(asyncio.shield(consumer), timeout=15)
            terminal_status = _status_value(terminal)
        except Exception as error:
            failures.append(f"interrupt/drain: {type(error).__name__}")
    try:
        await asyncio.wait_for(cleanup.clean_thread(fork.id), timeout=10)
    except Exception as error:
        failures.append(f"terminal-cleanup: {type(error).__name__}")
    unsubscribe_status = None
    try:
        status = await asyncio.wait_for(subscription.unsubscribe(fork.id), timeout=10)
        unsubscribe_status = status.value
    except Exception as error:
        failures.append(f"unsubscribe: {type(error).__name__}")
    if consumer is not None and not consumer.done():
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    if failures:
        raise AssertionError("; ".join(failures))
    return {
        "terminal_status_during_cleanup": terminal_status,
        "unsubscribe_status": unsubscribe_status,
    }


async def _prove_ephemeral(codex: AsyncCodex, fork: Any, parent_id: str) -> None:
    native = (await fork.read(include_turns=False)).thread
    if (
        native.id != fork.id
        or fork.id == parent_id
        or native.ephemeral is not True
        or native.path is not None
        or getattr(native, "forked_from_id", parent_id) not in {None, parent_id}
    ):
        raise AssertionError("fork identity, ephemeral flag, or persistence path changed")
    await _prove_thread_absent_from_all_catalogs(codex, fork.id)


async def probe(
    cwd: Path, *, model: str | None = None, runtime_only: bool = False,
) -> dict[str, Any]:
    migrations = facade_migration_requirements()
    if migrations:
        raise AssertionError("; ".join(migrations))
    result: dict[str, Any] = {
        "openai_codex_version": openai_codex.__version__,
        "sdk_gap_facade_migrations": list(migrations),
        "model_override": model,
    }
    _progress("SDK initialization starting")
    async with AsyncCodex() as codex:
        result["runtime"] = await _runtime_scenario(
            codex, cwd, model=model, check_interruption=not runtime_only,
        )
    return result


async def _interrupted_fork(
    codex: AsyncCodex, parent_id: str, cleanup: Any, subscription: Any,
) -> dict[str, Any]:
    """Keep the distinct native interruption check, sharing the probe's parent."""
    _progress("interrupted naming fork starting")
    fork = await codex.thread_fork(parent_id, ephemeral=True, include_turns=False)
    handle = consumer = None
    try:
        await _prove_ephemeral(codex, fork, parent_id)
        handle = await fork.turn(NAMING_PROMPT, output_schema=NAMING_OUTPUT_SCHEMA)
        consumer = asyncio.create_task(handle.run())
    finally:
        released = await _release_fork(fork, handle, consumer, cleanup, subscription)
    if released["terminal_status_during_cleanup"] != "interrupted":
        raise AssertionError("did not observe an exact interrupted naming Turn")
    await _prove_thread_absent_from_all_catalogs(codex, fork.id)
    return {"fork_thread_id": fork.id, "turn_id": handle.id, **released}


class _RecordingSubscriptions(AppServerThreadSubscriptionControl):
    """Record successful real releases without changing the provider behavior."""

    def __init__(self, codex: AsyncCodex) -> None:
        super().__init__(codex)
        self.released: dict[str, str] = {}

    async def unsubscribe(self, thread_id: str):
        status = await super().unsubscribe(thread_id)
        self.released[thread_id] = status.value
        return status


async def _runtime_scenario(
    codex: AsyncCodex, cwd: Path, *, model: str | None, check_interruption: bool,
) -> dict[str, Any]:
    """Exercise production submission/automatic naming without injecting a job."""

    _progress("production Runtime automatic naming starting")
    marker = f"NAMING-{uuid.uuid4().hex[:12]}"
    project = cwd / "runtime"
    project.mkdir()
    subprocess.run(["git", "-C", str(project), "init", "--quiet"], check=True)
    settings = None
    if model is not None:
        catalog = ModelCatalog.from_response(await codex.models())
        selected = next((item for item in catalog.models if item.model == model), None)
        if selected is None:
            raise ValueError("requested fixture model is absent from the native catalog")
        settings = BindingTurnSettings(
            model_id=selected.id, effort_id=selected.default_effort_id,
            service_tier_id=STANDARD_SERVICE_TIER_ID,
        )
    store = BindingStore()
    store.register_project(alias="naming-probe", cwd=str(project))
    scope = FeishuScope("probe-local", "probe-naming", ScopeKind.DIRECT)
    binding = store.create_channel_binding(
        scope=scope, project_alias="naming-probe", creator_id="probe-owner",
        turn_settings=settings,
    )
    outcomes: list[Any] = []

    async def completed(outcome: Any) -> None:
        outcomes.append(outcome)

    cleanup = PinnedExperimentalTerminalCleanup(codex)
    subscriptions = _RecordingSubscriptions(codex)
    runtime = CodexRuntime(
        codex=codex, bindings=store, terminal_cleanup=cleanup,
        thread_subscription_control=subscriptions, on_completion=completed,
    )
    result: dict[str, Any] = {}
    try:
        first = await runtime.submit(
            binding=binding, cwd=project, owner_id="probe-owner", origin=object(),
            input=(
                f"本次会话只讨论项目 {marker}，这是项目的完整名称。"
                "请先通过终端运行 /bin/sleep 8 并等待结束，然后只回复 RUNTIME-NAMING-FIRST。"
            ),
        )
        if first.release_receipt_attempt is not None:
            first.release_receipt_attempt()
        if first.disposition is not SubmitDisposition.STARTED:
            raise AssertionError("fresh Runtime input did not start a new Turn")
        namer = runtime._thread_namer
        if namer is None:
            raise AssertionError("production Runtime did not enable automatic naming")
        job = namer._jobs.get(binding.id)
        if job is None or job.task is None or job.task in runtime._tasks:
            raise AssertionError("naming job was absent or entered the main wait_idle task set")
        if not await runtime.wait_idle(timeout=120):
            raise AssertionError("parent Runtime Turn did not become idle")
        result["naming_pending_when_parent_idle"] = not job.task.done()
        result["wait_idle_excludes_naming_task"] = True
        if len(outcomes) != 1 or outcomes[0].turn_id != first.turn_id:
            raise AssertionError("background naming leaked a completion outcome")
        if await _public_final_response(job.parent, first.turn_id) != "RUNTIME-NAMING-FIRST":
            raise AssertionError("automatic naming changed the parent response")

        async with asyncio.timeout(150):
            while namer._jobs:
                await asyncio.sleep(0.05)
        native = (await job.parent.read(include_turns=True)).thread
        if not isinstance(native.name, str) or not native.name.strip():
            raise AssertionError("production Runtime did not automatically write a name")
        if {turn.id for turn in native.turns} != {first.turn_id}:
            raise AssertionError("naming prompt entered the parent history")
        if job.thread is None or job.thread.id not in subscriptions.released:
            raise AssertionError("Runtime did not release its internal naming fork")
        if job.run is None or not job.run.done():
            raise AssertionError("Runtime naming consumer did not reach a terminal")
        generated_title, item_types = _validate_title(job.run.result(), marker)
        if native.name != generated_title:
            raise AssertionError("Runtime wrote a name different from its generated result")
        await _prove_ephemeral(codex, job.thread, first.thread_id)
        inventory = store.preview_project_delete("naming-probe")
        if len(inventory.bindings) != 1 or inventory.sides:
            raise AssertionError("naming created a Binding or persisted Side")
        if runtime.project_side_snapshots("naming-probe"):
            raise AssertionError("naming created a Runtime Side session")
        expected_released = {job.thread.id}
        if check_interruption:
            result["interrupted"] = await _interrupted_fork(
                codex, first.thread_id, cleanup, subscriptions,
            )
            expected_released.add(result["interrupted"]["fork_thread_id"])
        after = await runtime.submit(
            binding=store.get(binding.id), cwd=project, owner_id="probe-owner",
            origin=object(), input="不要调用工具，只回复 RUNTIME-NAMING-AFTER。",
        )
        if after.release_receipt_attempt is not None:
            after.release_receipt_attempt()
        if after.disposition is not SubmitDisposition.STARTED:
            raise AssertionError("parent followup was not a fresh Turn")
        if not await runtime.wait_idle(timeout=120):
            raise AssertionError("parent continuation did not become idle")
        if await _public_final_response(job.parent, after.turn_id) != "RUNTIME-NAMING-AFTER":
            raise AssertionError("parent could not continue after automatic naming")
        async with asyncio.timeout(20):
            while namer._jobs:
                await asyncio.sleep(0.05)
        native_after = (await job.parent.read(include_turns=True)).thread
        if {turn.id for turn in native_after.turns} != {first.turn_id, after.turn_id}:
            raise AssertionError("parent history includes an internal naming Turn")
        if len(outcomes) != 2 or native_after.name != native.name:
            raise AssertionError("named followup changed the title or completion count")
        if set(subscriptions.released) != expected_released:
            raise AssertionError("named followup generated another naming fork")
        result.update({
            "parent_thread_id": first.thread_id,
            "fork_thread_id": job.thread.id,
            "title": native.name,
            "first_message_in_fork": True,
            "naming_item_types": item_types,
            "unsubscribe_status": subscriptions.released[job.thread.id],
            "binding_count": 1,
            "side_count": 0,
            "parent_only_contains_original_turns": True,
            "parent_continued_after_cleanup": True,
            "completion_count": 2,
        })
        _progress("production Runtime automatic naming passed")
    finally:
        try:
            await asyncio.wait_for(runtime.interrupt_all(), timeout=30)
        finally:
            try:
                await asyncio.wait_for(runtime.cancel_tasks(), timeout=30)
            finally:
                try:
                    parent_id = store.get(binding.id).native_thread_id
                    if parent_id is not None:
                        try:
                            await asyncio.wait_for(cleanup.clean_thread(parent_id), timeout=10)
                        finally:
                            await asyncio.wait_for(codex.thread_archive(parent_id), timeout=10)
                            result["parent_left_archived"] = True
                finally:
                    store.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="optional model for this probe's own Thread")
    parser.add_argument(
        "--runtime-only", action="store_true",
        help="run only the production Runtime scenario after an isolated implementation change",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="netizen-naming-probe-") as raw:
        cwd = Path(raw).resolve()
        subprocess.run(["git", "-C", str(cwd), "init", "--quiet"], check=True)
        print(json.dumps(asyncio.run(probe(
            cwd, model=args.model, runtime_only=args.runtime_only,
        )), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
