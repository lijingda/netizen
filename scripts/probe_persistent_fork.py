#!/usr/bin/env python3
"""Validate persistent fork, cold resume and delete with disposable resources.

Uses one short model response, real Runtime/SQLite/native Threads and fake topic
identities; this does not verify Feishu delivery. Run serially in the service
account's login environment, with an external deadline such as
``gtimeout --signal=INT --kill-after=10s 420s .venv/bin/python
scripts/probe_persistent_fork.py`` (Linux: ``timeout``).

The public process-only configuration marks only this run's temporary Git cwd
trusted, preventing native thread/start from persisting its trust in user config.
Cleanup only deletes known owned IDs; unknown delete outcomes are never retried.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from typing import Any

import openai_codex
from openai_codex import AsyncCodex, AsyncThread, CodexConfig, InvalidRequestError

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen_cli.bindings import (  # noqa: E402
    BindingNotFound, BindingStore, BindingTaskFeedback, BindingTurnSettings,
)
from netizen_cli.codex_runtime import (  # noqa: E402
    CodexRuntime, ThreadLifecycleError, ThreadSubscriptionState,
)
from netizen_cli.domain import (  # noqa: E402
    FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind,
)
from netizen_cli.model_settings import ModelCatalog, STANDARD_SERVICE_TIER_ID  # noqa: E402
from netizen_cli.sdk_gap_adapter import (  # noqa: E402
    AppServerGoalControl, AppServerThreadDeleteControl,
    AppServerThreadSubscriptionControl, ThreadDeleteRejected,
)
from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup  # noqa: E402
from scripts.probe_python_sdk import (  # noqa: E402
    _final_response_from_turn, _public_terminal_turn, _status_value,
)


class _OwnedDeletes:
    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.control: AppServerThreadDeleteControl | None = None

    async def delete(self, thread_id: str) -> None:
        if self.control is None or self.states.get(thread_id) not in {"created", "rejected"}:
            raise AssertionError("delete must target an owned ID with a known outcome")
        self.states[thread_id] = "unknown"
        try:
            await self.control.delete(thread_id)
        except ThreadDeleteRejected:
            self.states[thread_id] = "rejected"
            raise
        else:
            self.states[thread_id] = "deleted"


def _runtime(codex: AsyncCodex, store: BindingStore, owned: _OwnedDeletes) -> CodexRuntime:
    cleanup = PinnedExperimentalTerminalCleanup(codex)
    owned.control = AppServerThreadDeleteControl(codex)
    return CodexRuntime(
        codex=codex, bindings=store, terminal_cleanup=cleanup,
        background_terminal_inspector=cleanup,
        goal_control=AppServerGoalControl(codex),
        thread_subscription_control=AppServerThreadSubscriptionControl(codex),
        thread_delete_control=owned, automatic_thread_naming=False,
    )


def _require_binding_absent(store: BindingStore, binding_id: str) -> None:
    try:
        store.get(binding_id)
    except BindingNotFound:
        return
    raise AssertionError("successful native delete did not remove the Binding")


def _config_digest() -> str | None:
    path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


async def _scenario(
    config: CodexConfig, cwd: Path, store: BindingStore, owned: _OwnedDeletes,
    evidence: dict[str, Any],
) -> None:
    store.bootstrap_project(alias="fork-probe", cwd=str(cwd))
    revision = store.get_project("fork-probe").revision
    source_scope = FeishuScope("probe-app", "probe-origin", ScopeKind.GROUP)
    target_scope = FeishuScope("probe-app", "probe-destination", ScopeKind.TOPIC, "probe-topic")
    marker = "FORK-SEED-" + secrets.token_hex(8)
    async with AsyncCodex(config) as codex:
        runtime = _runtime(codex, store, owned)
        try:
            model = ModelCatalog.from_response(await codex.models()).default_model
            source = store.create_channel_binding(
                scope=source_scope, project_alias="fork-probe", creator_id="probe-owner",
                turn_settings=BindingTurnSettings(
                    model.id, model.default_effort_id, STANDARD_SERVICE_TIER_ID,
                ),
                task_feedback=BindingTaskFeedback(True, True, True),
                message_context_mode=MentionContextMode.CATCH_UP,
                context_anchor=MessageContextAnchor("source-seed", 1000),
            )
            parent = await codex.thread_start(cwd=str(cwd), model=model.model)
            owned.states[parent.id] = "created"
            store.assign_native_thread_id(source.id, parent.id)
            source = store.get(source.id)
            seed = await parent.turn("Reply exactly: " + marker)
            final = await _public_terminal_turn(parent, seed.id, timeout=150)
            assert _status_value(final) == "completed"
            assert _final_response_from_turn(final) == marker
            async with runtime.track_fork_creation("fork-probe"):
                fork = await runtime.fork_exact(source, expected_project_revision=revision)
                owned.states[fork.id] = "created"
                branch = store.create_fork_binding(
                    scope=target_scope, source=source, native_thread_id=fork.id,
                    root_message_id="probe-root-card", creator_id="probe-owner",
                    expected_project_revision=revision,
                    context_anchor=MessageContextAnchor("destination-seed", 2000),
                )
                await runtime.adopt_fork(branch, fork)
                assert runtime._subscriptions[branch.id].thread is fork
                assert runtime.thread_subscription_snapshot(branch.id).state is ThreadSubscriptionState.RELEASE_PENDING
            assert fork.id != parent.id
            assert store.active_binding(source_scope.key).id == source.id
            assert store.active_binding(target_scope.key).id == branch.id
            assert branch.turn_settings == source.turn_settings
            assert branch.task_feedback == source.task_feedback
            assert branch.context_anchor == MessageContextAnchor("destination-seed", 2000)
            inherited = await fork.read(include_turns=True)
            assert [turn.id for turn in inherited.thread.turns] == [seed.id]
            evidence.update(
                same_fork_handle_adopted=True, source_current_unchanged=True,
                complete_destination_binding=True, explicit_intent_copied=True,
                destination_context_anchor=True, no_fork_own_turn=True,
            )
        finally:
            runtime.close_admission()
            await runtime.cancel_tasks()

    async with AsyncCodex(config) as cold:
        runtime = _runtime(cold, store, owned)
        try:
            restored = await runtime.activate_exact(
                branch.id, context_anchor=MessageContextAnchor("resume-message", 3000),
            )
            assert restored.native_thread_id == fork.id
            inherited = await AsyncThread(cold, fork.id).read(include_turns=True)
            assert [turn.id for turn in inherited.thread.turns] == [seed.id]
            assert _final_response_from_turn(inherited.thread.turns[0]) == marker
            evidence["cold_resume_before_first_own_turn"] = True
            try:
                await runtime.delete_exact(source.id, expected_native_thread_id=parent.id)
            except ThreadLifecycleError as error:
                assert isinstance(error.__cause__, ThreadDeleteRejected)
                native = error.__cause__.__cause__
                assert isinstance(native, InvalidRequestError) and native.code == -32600
                assert native.message == f"cannot delete thread {parent.id}: forked history still references it"
                assert store.get(source.id).native_thread_id == parent.id
                assert store.active_binding(source_scope.key).id == source.id
                assert runtime.lifecycle_state(source.id) is None
            else:
                raise AssertionError("native source delete did not reject the history reference")
            evidence["native_reference_rejection_keeps_source_binding"] = True
            await runtime.delete_exact(branch.id, expected_native_thread_id=fork.id)
            _require_binding_absent(store, branch.id)
            await runtime.delete_exact(source.id, expected_native_thread_id=parent.id)
            _require_binding_absent(store, source.id)
            evidence["branch_then_source_delete_ack_and_binding_removal"] = True
        finally:
            runtime.close_admission()
            await runtime.cancel_tasks()


async def probe(*, timeout: float) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "passed": False, "openai_codex_version": openai_codex.__version__,
        "real_feishu_calls": False, "process_only_fixture_trust": True,
    }
    before = _config_digest()
    owned = _OwnedDeletes()
    with tempfile.TemporaryDirectory(prefix="netizen-persistent-fork-probe-") as temporary:
        root = Path(temporary).resolve()
        cwd = root / "project"
        cwd.mkdir()
        subprocess.run(["git", "-C", str(cwd), "init", "--quiet"], check=True)
        config = CodexConfig(
            cwd=str(cwd),
            config_overrides=(f'projects.{json.dumps(str(cwd))}.trust_level="trusted"',),
        )
        store = BindingStore(root / "channel.sqlite3")
        try:
            async with asyncio.timeout(timeout):
                await _scenario(config, cwd, store, owned, evidence)
            evidence["passed"] = True
        except Exception as error:
            evidence["error_type"] = type(error).__name__
        finally:
            # A failed assertion is not permission to retry an unknown native
            # mutation. Cleanup traverses only this run's known IDs, fork first.
            remaining = [tid for tid, state in owned.states.items() if state in {"created", "rejected"}]
            if remaining:
                try:
                    async with AsyncCodex(config) as cleanup:
                        owned.control = AppServerThreadDeleteControl(cleanup)
                        for thread_id in reversed(remaining):
                            try:
                                async with asyncio.timeout(15):
                                    await owned.delete(thread_id)
                            except Exception:
                                pass
                except Exception as error:
                    evidence["cleanup_error_type"] = type(error).__name__
            store.close()
    evidence["owned_native_outcomes"] = owned.states
    evidence["global_config_bytes_unchanged"] = before == _config_digest()
    evidence["passed"] = bool(
        evidence["passed"] and evidence["global_config_bytes_unchanged"]
        and all(state == "deleted" for state in owned.states.values())
    )
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300, help="scenario deadline; still use an external deadline")
    arguments = parser.parse_args()
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    result = asyncio.run(probe(timeout=arguments.timeout))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
