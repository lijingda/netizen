#!/usr/bin/env python3
"""Exercise candidate Project deletion with disposable native histories only.

Run with the service account's interactive login environment and the checkout's
exact pinned SDK. No service, existing database, credentials, or business cwd is
modified. Feishu route identities are local fixtures: this does not validate
Feishu topic creation or the browser transport. Bound the whole command with
``timeout --signal=INT --kill-after=10s 420s`` (``gtimeout`` on macOS).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import openai_codex
from openai_codex import AsyncCodex


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen.bindings import (  # noqa: E402
    BindingNotFound, BindingStore, BindingTurnSettings, SideTopicState,
)
from netizen.codex_runtime import CodexRuntime, SideSessionNotFound  # noqa: E402
from netizen.domain import FeishuScope, ScopeKind  # noqa: E402
from netizen.management.coordination import ScopeCoordinator  # noqa: E402
from netizen.management.service import (  # noqa: E402
    ExactBindingTarget,
    InstanceManagementService,
    ManagementRuntimePort,
)
from netizen.model_settings import ModelCatalog, STANDARD_SERVICE_TIER_ID  # noqa: E402
from netizen.projects import ProjectRegistry  # noqa: E402
from netizen.runtime.contracts import SideTurnOutcome  # noqa: E402
from netizen.sdk_gap_adapter import (  # noqa: E402
    AppServerSideBoundaryControl,
    AppServerThreadDeleteControl,
    AppServerThreadSubscriptionControl,
)
from netizen.terminal_cleanup import PinnedExperimentalTerminalCleanup  # noqa: E402
from scripts.probe_python_sdk import (  # noqa: E402
    _prove_thread_absent_from_all_catalogs,
    _public_final_response,
    _wait_for_thread_visibility,
)


class _DeleteOnce:
    """Track exact owned IDs; never retry an uncertain native mutation."""

    def __init__(self, codex: AsyncCodex) -> None:
        self.control = AppServerThreadDeleteControl(codex)
        self.owned: set[str] = set()
        self.attempted: set[str] = set()

    async def delete(self, thread_id: str) -> None:
        if thread_id not in self.owned:
            raise AssertionError("probe cannot delete a Thread it did not create")
        if thread_id in self.attempted:
            raise AssertionError("probe must not retry native delete")
        self.attempted.add(thread_id)
        await self.control.delete(thread_id)


async def _scenario(
    codex: AsyncCodex, root: Path, *, orphan_side: bool, model: str | None,
    binding_settings: BindingTurnSettings | None,
) -> dict[str, Any]:
    label = "orphan-side" if orphan_side else "mixed-sessions"
    print(f"[probe] project-delete/{label}: started", file=sys.stderr, flush=True)
    cwd = root / label
    cwd.mkdir()
    subprocess.run(["git", "-C", str(cwd), "init", "--quiet"], check=True)
    marker = cwd / "preserve-this-file.txt"
    marker.write_text("Project deletion preserves its code directory.\n")
    store = BindingStore(root / f"{label}.sqlite3")
    projects = ProjectRegistry(store=store, project_root=root, projects={label: cwd})
    delete = _DeleteOnce(codex)
    side_results: asyncio.Queue[SideTurnOutcome] = asyncio.Queue()

    async def on_completion(outcome: object) -> None:
        if isinstance(outcome, SideTurnOutcome):
            side_results.put_nowait(outcome)

    runtime = CodexRuntime(
        codex=codex,
        bindings=store,
        terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
        side_boundary_control=AppServerSideBoundaryControl(codex),
        thread_subscription_control=AppServerThreadSubscriptionControl(codex),
        thread_delete_control=delete,
        on_completion=on_completion,
    )
    service = InstanceManagementService(
        bindings=store,
        projects=projects,
        runtime=ManagementRuntimePort(runtime),
        scope_coordinator=ScopeCoordinator(),
    )
    bindings = []
    persisted_ids: list[str] = []
    passed = False
    try:
        kinds = ("active",) if orphan_side else ("lazy", "active", "archived")
        for kind in kinds:
            scope = FeishuScope("probe-local", f"probe-{label}-{kind}", ScopeKind.DIRECT)
            binding = store.create_channel_binding(
                scope=scope, project_alias=label, creator_id="probe-owner",
                turn_settings=binding_settings,
            )
            if kind != "lazy":
                thread = await codex.thread_start(cwd=str(cwd), model=model)
                delete.owned.add(thread.id)
                persisted_ids.append(thread.id)
                store.assign_native_thread_id(binding.id, thread.id)
                handle = await thread.turn("Reply exactly: PROJECT-DELETE-SEED")
                try:
                    response = await _public_final_response(thread, handle.id)
                except Exception:
                    # Inspect only this probe's exact failed Turn. Do not emit
                    # configuration, credentials, tool calls, or other history.
                    view = await thread.read(include_turns=True)
                    exact = next((turn for turn in view.thread.turns if turn.id == handle.id), None)
                    failure = getattr(exact, "error", None)
                    message = getattr(failure, "message", None)
                    if isinstance(message, str):
                        print(f"[probe] seed failure: {message}", file=sys.stderr, flush=True)
                    raise
                if response != "PROJECT-DELETE-SEED":
                    raise AssertionError("seed Turn did not return its exact final response")
                if kind == "archived":
                    await codex.thread_archive(thread.id)
                await _wait_for_thread_visibility(
                    codex, thread.id, archived=kind == "archived", present=True,
                )
            bindings.append(store.get(binding.id))

        parent = bindings[0] if orphan_side else bindings[1]
        route = store.create_side_topic(
            app_id="probe-local", chat_id=f"probe-{label}-active",
            source_message_id="probe-source", parent_binding_id=parent.id,
            creator_id="probe-owner", requires_mention=False,
        )
        side = await runtime.create_side(
            side_id=route.id, binding=parent, cwd=cwd, creator_id="probe-owner",
        )
        store.set_side_topic_root(route.id, "probe-root")
        await runtime.attach_side_topic(
            side_id=route.id, topic_id="probe-topic", root_message_id="probe-root",
        )
        store.open_side_topic(route.id, "probe-topic")
        submission = await runtime.submit_side(
            side_id=route.id, input="Reply exactly: PROJECT-DELETE-SIDE",
            owner_id="probe-owner", origin=None,
        )
        if submission.release_receipt_attempt is None:
            raise AssertionError("Side fixture did not start a new Turn")
        submission.release_receipt_attempt()
        outcome = await asyncio.wait_for(side_results.get(), 90)
        if (
            outcome.error is not None
            or outcome.status != "completed"
            or outcome.final_response != "PROJECT-DELETE-SIDE"
            or outcome.thread_id != side.thread_id
            or outcome.turn_id != submission.turn_id
        ):
            print(
                f"[probe] Side seed failure: status={outcome.status!r}; "
                f"error={outcome.error!s}",
                file=sys.stderr, flush=True,
            )
            raise AssertionError("Side seed did not complete on its exact ephemeral Thread")

        if orphan_side:
            await service.delete_exact_binding(
                target=ExactBindingTarget(parent.scope_key, parent.id, None),
                expected_native_thread_id=parent.native_thread_id,
            )
            # The Side remains a real runtime session after the ordinary Parent
            # was removed; a Binding join must not hide it from Project delete.
            if runtime.side_snapshot(route.id).thread_id != side.thread_id:
                raise AssertionError("orphan fixture lost the exact Side identity")

        preview = await service.preview_project_delete(
            alias=label, expected_revision=projects.resolve(label).revision,
            deadline=asyncio.get_running_loop().time() + 20,
        )
        expected_bindings = 0 if orphan_side else 3
        if len(preview.bindings) != expected_bindings or len(preview.sides) != 1:
            raise AssertionError("confirmed Project inventory omitted a Session or Side")
        result = await service.delete_project(
            alias=label, expected_revision=preview.project.revision,
            expected_inventory_fingerprint=preview.fingerprint,
        )
        if (
            not result.deleted
            or result.code != "deleted"
            or result.deleted_session_count != expected_bindings
            or result.remaining_sessions
            or result.remaining_side_count
        ):
            raise AssertionError(f"Project delete did not complete: {result!r}")
        for thread_id in persisted_ids:
            await _prove_thread_absent_from_all_catalogs(codex, thread_id)
        for binding in bindings:
            try:
                store.get(binding.id)
            except BindingNotFound:
                pass
            else:
                raise AssertionError("deleted Project retains an ordinary Binding")
            if store.active_binding(binding.scope_key) is not None:
                raise AssertionError("deleted Binding retains a current pointer")
        if projects.list():
            raise AssertionError("deleted Project remains in the Registry")
        if store.get_side_topic(route.id).state is not SideTopicState.CLOSED:
            raise AssertionError("Side close did not preserve its terminal tombstone")
        try:
            runtime.side_snapshot(route.id)
        except SideSessionNotFound:
            pass
        else:
            raise AssertionError("deleted Project retains a runtime Side")
        if marker.read_text() != "Project deletion preserves its code directory.\n":
            raise AssertionError("Project deletion changed the code directory")
        # Reopen the actual SQLite file and replay YAML bootstrap. The tombstone
        # must prevent the just-deleted Project from returning after restart.
        await service.close()
        await runtime.cancel_tasks()
        store.close()
        store = BindingStore(root / f"{label}.sqlite3")
        restarted = ProjectRegistry(store=store, project_root=root, projects={label: cwd})
        if restarted.list() or store.get_side_topic(route.id).state is not SideTopicState.CLOSED:
            raise AssertionError("restart lost the Project or Side tombstone")
        passed = True
        print(f"[probe] project-delete/{label}: passed", file=sys.stderr, flush=True)
        return {
            "passed": True,
            "confirmed_sessions": expected_bindings,
            "confirmed_sides": 1,
            "native_threads_deleted_once": len(delete.attempted),
            "native_absent_from_four_views": True,
            "runtime_side_closed": True,
            "side_tombstone_survives_restart": True,
            "project_tombstone_survives_bootstrap": True,
            "cwd_preserved": True,
        }
    finally:
        if not passed:
            # Only failed disposable fixtures need shutdown cleanup. Never use
            # this before the lifecycle operation under test or resend delete.
            try:
                await asyncio.wait_for(runtime.interrupt_all(), 20)
            except Exception as error:
                print(f"[probe] fixture shutdown failed: {type(error).__name__}", file=sys.stderr)
            for thread_id in delete.owned - delete.attempted:
                try:
                    await asyncio.wait_for(delete.delete(thread_id), 20)
                    await asyncio.wait_for(_prove_thread_absent_from_all_catalogs(codex, thread_id), 20)
                except Exception as error:
                    print(f"[probe] fixture cleanup failed: {type(error).__name__}", file=sys.stderr)
        await runtime.cancel_tasks()
        await service.close()
        store.close()


async def probe(*, model: str | None = None) -> dict[str, Any]:
    if openai_codex.__version__ != "0.147.0":
        raise RuntimeError("this candidate probe requires pinned openai-codex==0.147.0")
    with tempfile.TemporaryDirectory(prefix="netizen-project-delete-probe-") as temporary:
        async with AsyncCodex() as codex:
            root = Path(temporary)
            binding_settings = None
            if model is not None:
                catalog = ModelCatalog.from_response(await codex.models())
                selected = next((item for item in catalog.models if item.model == model), None)
                if selected is None:
                    raise ValueError("requested fixture model is absent from the native catalog")
                binding_settings = BindingTurnSettings(
                    model_id=selected.id, effort_id=selected.default_effort_id,
                    service_tier_id=STANDARD_SERVICE_TIER_ID,
                )
            mixed = await _scenario(
                codex, root, orphan_side=False, model=model, binding_settings=binding_settings,
            )
            orphan = await _scenario(
                codex, root, orphan_side=True, model=model, binding_settings=binding_settings,
            )
            return {
                "openai_codex_version": openai_codex.__version__,
                "fixture_model": model,
                "fixture_binding_model_intent": binding_settings is not None,
                "mixed_sessions": mixed,
                "orphan_side": orphan,
                "feishu_topic_and_browser_transport_tested": False,
            }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", help="optional model intent on disposable Threads and Bindings only",
    )
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(probe(model=arguments.model)), indent=2))
