"""Verify native cwd persistence and Runtime's cross-client cwd adoption.

Uses the bundled App Server and a loopback mock model, never a real account,
existing Project, or service. Run with an external 90-second process deadline.
The selected directory need not be a Git repository: Git ownership is tested
by the Worktree service; this probe isolates the native cwd contract.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import logging
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openai_codex
from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, Sandbox

from netizen_cli.bindings import BindingStore
from netizen_cli.codex_runtime import CodexRuntime, _native_cwd
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.runtime.contracts import TurnOutcome
from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup
from probe_sdk_partial_answers import (
    isolated_config, message_events, mock_provider, response_events,
)
from scripts.probe_scheduled_tasks import _safe_traceback, source_evidence, write_result


async def native_case(root: Path) -> dict[str, object]:
    replies = [
        (200, response_events(name, *message_events(name + "-answer", text, "final_answer")))
        for name, text in (
            ("cwd-probe", "CWD-PROBE-OK"),
            ("cwd-other-client", "CWD-OTHER-CLIENT-OK"),
            ("cwd-runtime", "CWD-RUNTIME-OK"),
        )
    ]
    with mock_provider(replies) as provider:
        default_cwd, environment = isolated_config(root, provider)
        environment.pop("HOME", None)
        selected_cwd = root / "selected-worktree"
        selected_cwd.mkdir()
        config = CodexConfig(cwd=str(default_cwd), env=environment)
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_start(
                cwd=str(selected_cwd), approval_mode=ApprovalMode.deny_all,
                sandbox=Sandbox.read_only,
            )
            created = (await thread.read(include_turns=False)).thread
            assert _native_cwd(created, thread.id) == selected_cwd
            native_cwd_type = type(created.cwd).__name__
            handle = await thread.turn("Return the scripted fixture response.")
            result = await handle.run()
            assert result.status.value == "completed"
            assert result.final_response == "CWD-PROBE-OK"
            persisted = (await thread.read(include_turns=False)).thread
            assert _native_cwd(persisted, thread.id) == selected_cwd
        # A second App Server has a different default cwd. Neither readonly
        # discovery nor resume without a cwd override may reset the Thread.
        async with AsyncCodex(config) as codex:
            cold = (await AsyncThread(codex, thread.id).read(include_turns=False)).thread
            assert _native_cwd(cold, thread.id) == selected_cwd
            resumed = await codex.thread_resume(thread.id, include_turns=False)
            assert resumed.id == thread.id
            after = (await resumed.read(include_turns=False)).thread
            assert _native_cwd(after, thread.id) == selected_cwd
        changed_cwd = root / "changed-worktree"
        changed_cwd.mkdir()
        store = BindingStore(root / "channel.sqlite3")
        try:
            store.bootstrap_project(alias="cwd-probe", cwd=str(default_cwd))
            binding = store.create_channel_binding(
                scope=FeishuScope("probe-app", "probe-chat", ScopeKind.GROUP),
                project_alias="cwd-probe", creator_id="probe-owner",
            )
            store.assign_native_thread_id(binding.id, thread.id)
            outcomes: list[TurnOutcome] = []
            completed = asyncio.Event()

            async def on_completion(outcome: TurnOutcome) -> None:
                outcomes.append(outcome)
                completed.set()

            async with AsyncCodex(config) as observer:
                runtime = CodexRuntime(
                    codex=observer, bindings=store,
                    terminal_cleanup=PinnedExperimentalTerminalCleanup(observer),
                    on_completion=on_completion, poll_interval_seconds=0.01,
                    automatic_thread_naming=False,
                )
                try:
                    # A separate read-only query must not acquire the native
                    # writer or become authority for a later submission.
                    observed_cwd = await runtime.resolve_binding_cwd(
                        binding.id, default_cwd=default_cwd,
                    )
                    assert observed_cwd == selected_cwd
                    admission = await runtime.capture_submission_admission(binding.id)
                    async with AsyncCodex(config) as other_client:
                        changed = await other_client.thread_resume(
                            thread.id, cwd=str(changed_cwd), include_turns=False,
                        )
                        changed_native = (await changed.read(include_turns=False)).thread
                        assert _native_cwd(changed_native, thread.id) == changed_cwd
                        result = await (await changed.turn("Persist the changed cwd.")).run()
                        assert result.status.value == "completed"
                        assert result.final_response == "CWD-OTHER-CLIENT-OK"
                    # The other client has exited. Production resume must adopt
                    # B and submit this input once. Existing Thread submissions
                    # do not need a caller cwd, even after an earlier query of A.
                    request_count = len(provider.requests)
                    assert request_count == 2
                    input_marker = "CWD-INPUT-PREPARED-BEFORE-CROSS-CLIENT-CHANGE"
                    submission = await runtime.submit(
                        binding=store.get(binding.id),
                        input=input_marker, owner_id="probe-owner",
                        origin="cross-client-cwd-probe", admission=admission,
                    )
                    assert submission.disposition.value == "started"
                    assert submission.release_receipt_attempt is not None
                    submission.release_receipt_attempt()
                    await asyncio.wait_for(completed.wait(), 15)
                    assert await runtime.wait_idle(timeout=5)
                    assert len(outcomes) == 1
                    outcome = outcomes[0]
                    assert outcome.error is None and outcome.status == "completed"
                    assert outcome.cwd == changed_cwd
                    assert outcome.thread_id == thread.id and outcome.turn_id == submission.turn_id
                    assert outcome.final_response == "CWD-RUNTIME-OK"
                    assert len(provider.requests) == request_count + 1
                    assert json.dumps(provider.requests).count(input_marker) == 1
                    after_completion = (
                        await AsyncThread(observer, thread.id).read(include_turns=True)
                    ).thread
                    assert _native_cwd(after_completion, thread.id) == changed_cwd
                    assert len(after_completion.turns) == 3
                    assert after_completion.turns[-1].id == submission.turn_id
                    assert store.get(binding.id).working_cwd == str(changed_cwd)
                    # A public admission token after completion demonstrates
                    # the process-wide admission boundary remains open.
                    await runtime.capture_submission_admission(binding.id)
                finally:
                    await runtime.interrupt_all()
                    await runtime.cancel_tasks()
        finally:
            store.close()
        assert len(provider.requests) == 3 and not provider.errors and not provider.replies
        return {
            "sdk": openai_codex.__version__, "passed": True,
            "transport": "bundled App Server + loopback mock Responses",
            "native_cwd_type": native_cwd_type,
            "start_selected_cwd": True, "public_read_selected_cwd": True,
            "fresh_server_default_differs": True, "cold_read_selected_cwd": True,
            "resume_without_cwd_retains_native_cwd": True,
            "production_runtime": True, "prepared_cwd_read_before_cross_client_change": True,
            "existing_thread_submission_without_cwd": True,
            "other_client_persisted_changed_cwd_and_exited": True,
            "input_prepared_before_change_completed": True,
            "runtime_input_mock_requests": 1, "runtime_input_submitted_once": True,
            "native_changed_cwd_not_overwritten": True,
            "binding_workspace_refreshed": True, "admission_remains_open": True,
            "completion_cwd_matches_native": True,
            "mock_model_requests": len(provider.requests), "real_model_requests": 0,
        }


async def probe() -> dict[str, object]:
    async with asyncio.timeout(60):
        with tempfile.TemporaryDirectory(prefix="netizen-worktree-cwd-probe-") as temporary:
            result = await native_case(Path(temporary))
    return {**result, "temporary_state_cleaned": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="save sanitized result and checkout identity as JSON")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    evidence = source_evidence()
    try:
        result = asyncio.run(probe())
    except Exception as error:
        write_result({**evidence, "passed": False, "error_type": type(error).__name__,
                      "traceback": _safe_traceback(error)}, args.output)
        return 1
    write_result({**evidence, **result}, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
