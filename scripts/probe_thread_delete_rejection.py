#!/usr/bin/env python3
"""Verify observation after native delete rejection in disposable Threads.

Uses CodexRuntime, the real bundled App Server and a loopback mock provider.
Temporary HOME/CODEX_HOME/cwd isolate all resources; no account or Feishu calls.
The attached fork uses the public wire protocol only as a probe fixture, since
Netizen's ordinary fork does not request attachToSource.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any

from openai_codex import AsyncCodex, CodexConfig, InvalidRequestError
from openai_codex.generated.v2_all import ThreadForkResponse

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen_cli.bindings import BindingStore  # noqa: E402
from netizen_cli.codex_runtime import CodexRuntime, ThreadLifecycleError  # noqa: E402
from netizen_cli.domain import FeishuScope, ScopeKind  # noqa: E402
from netizen_cli.runtime.contracts import (  # noqa: E402
    GoalOutcome, StopDisposition, ThreadActivityDiscardedOutcome,
    ThreadSubscriptionState, TurnOutcome,
)
from netizen_cli.sdk_gap_adapter import (  # noqa: E402
    AppServerGoalControl, AppServerThreadDeleteControl,
    AppServerThreadSubscriptionControl,
)
from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup  # noqa: E402
from scripts.probe_sdk_partial_answers import (  # noqa: E402
    isolated_config, message_events, mock_provider, response_events,
)
from scripts.source_evidence import source_evidence  # noqa: E402


CASES = ("attached_completion", "attached_stop", "fork_reference", "attached_goal")


def _reply(name: str) -> tuple[int, bytes]:
    return 200, response_events(name, *message_events(name + "-message", name, "final_answer"))


def _native_error(error: BaseException) -> InvalidRequestError:
    while error.__cause__ is not None:
        error = error.__cause__
    assert isinstance(error, InvalidRequestError), type(error).__name__
    assert error.code == -32600, error.code
    return error


async def _case(root: Path, name: str) -> dict[str, Any]:
    goal_case = name == "attached_goal"
    attached = name != "fork_reference"
    replies = [_reply("seed"), _reply("after-delete")]
    if not attached:
        replies.append(_reply("resumed-after-rejection"))
    if goal_case:
        complete_tool = {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "complete-goal", "name": "update_goal",
            "arguments": json.dumps({"status": "complete"}),
        }}
        replies[1] = (200, response_events("complete-goal", complete_tool))
        replies.append(_reply("after-delete"))
    with mock_provider(replies) as provider:
        gate = provider.response_gates[2] = threading.Event()
        cwd, env = isolated_config(root, provider)
        if goal_case:
            config = Path(env["CODEX_HOME"]) / "config.toml"
            config.write_text("features.goals = true\n" + config.read_text(), encoding="utf-8")
        store = BindingStore(root / "channel.sqlite3")
        store.bootstrap_project(alias="delete-probe", cwd=str(cwd))
        binding = store.create_channel_binding(
            scope=FeishuScope("probe-app", "probe-chat", ScopeKind.DIRECT),
            project_alias="delete-probe", creator_id="probe-owner",
        )
        outcomes: list[Any] = []
        completed = asyncio.Condition()

        async def capture(outcome: Any) -> None:
            async with completed:
                outcomes.append(outcome)
                completed.notify_all()

        async def wait_outcomes(count: int) -> None:
            async with asyncio.timeout(15):
                async with completed:
                    await completed.wait_for(lambda: len(outcomes) >= count)

        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                control = AppServerGoalControl(codex) if goal_case else None
                cleanup = PinnedExperimentalTerminalCleanup(codex)
                runtime = CodexRuntime(
                    codex=codex, bindings=store,
                    terminal_cleanup=cleanup,
                    thread_delete_control=AppServerThreadDeleteControl(codex),
                    thread_subscription_control=AppServerThreadSubscriptionControl(codex),
                    background_terminal_inspector=cleanup,
                    goal_control=control, on_completion=capture,
                    poll_interval_seconds=0.01, automatic_thread_naming=False,
                )
                try:
                    async def submit(text: str):
                        submission = await runtime.submit(
                            binding=store.get(binding.id), cwd=cwd, input=text,
                            owner_id="probe-owner", origin=None,
                        )
                        assert submission.release_receipt_attempt is not None
                        submission.release_receipt_attempt()
                        return submission

                    seed = await submit("Use the scripted seed response.")
                    await wait_outcomes(1)
                    assert await runtime.wait_idle(timeout=5)
                    assert isinstance(outcomes[0], TurnOutcome) and outcomes[0].error is None
                    thread_id = seed.thread_id
                    if attached:
                        # Probe-only setup of an external native relationship.
                        branch = await codex._client.request(
                            "thread/fork", {"threadId": thread_id,
                                            "attachToSource": True, "excludeTurns": True},
                            response_model=ThreadForkResponse,
                        )
                        child_id = branch.thread.id
                    else:
                        branch_thread = await codex.thread_fork(thread_id, include_turns=False)
                        child_id = branch_thread.id
                    assert child_id != thread_id

                    if goal_case:
                        submission = await runtime.start_goal(
                            binding=store.get(binding.id), cwd=cwd,
                            objective="Complete the scripted task and mark the goal complete.",
                            owner_id="probe-owner", origin=None,
                        )
                        assert submission.release_receipt_attempt is not None
                        submission.release_receipt_attempt()
                        active = runtime._goals[binding.id]
                    else:
                        submission = await submit("Use the scripted final response.")
                        active = runtime._active[binding.id]
                    observer = active.task
                    assert observer is not None
                    async with asyncio.timeout(10):
                        while len(provider.requests) < 2:
                            await asyncio.sleep(0.01)
                    before = store.get(binding.id)
                    try:
                        await runtime.delete_exact(binding.id, expected_native_thread_id=thread_id)
                    except ThreadLifecycleError as error:
                        native = _native_error(error)
                    else:
                        raise AssertionError("native delete unexpectedly succeeded")
                    expected = (
                        f"cannot delete thread while attached thread {child_id} exists; "
                        "delete the attached thread first"
                        if attached else
                        f"cannot delete thread {thread_id}: forked history still references it"
                    )
                    assert native.message == expected, native.message
                    assert store.get(binding.id) == before
                    assert runtime.lifecycle_state(binding.id) is None
                    assert runtime._accepting
                    assert not observer.cancelled(), "delete rejection cancelled the original observer"
                    assert not any(isinstance(item, ThreadActivityDiscardedOutcome) for item in outcomes)
                    if attached:
                        slot = runtime._goals if goal_case else runtime._active
                        assert slot.get(binding.id) is active
                        assert active.task is observer and not observer.done()
                        assert active.activity_observation_enabled
                        snapshot = (await codex._client.thread_read(thread_id, include_turns=False)).thread
                        assert snapshot.status.root.type == "active", snapshot.status
                    stop_result = None
                    if name == "attached_stop":
                        stop_result = await runtime.stop_exact(binding.id)
                        assert stop_result is StopDisposition.REQUESTED, stop_result
                    gate.set()
                    await wait_outcomes(2)
                    assert await runtime.wait_idle(timeout=5)
                    assert len(outcomes) == 2
                    outcome = outcomes[-1]
                    assert not observer.cancelled()
                    if goal_case:
                        assert isinstance(outcome, GoalOutcome), type(outcome).__name__
                        assert outcome.error is None and outcome.finalization_error is None
                        assert outcome.final_turn_status == "completed"
                        assert outcome.final_response == "after-delete"
                        assert outcome.finalization.value == "cleared"
                        assert control is not None and await control.get(thread_id) is None
                        terminal = outcome.final_turn_status
                    else:
                        assert isinstance(outcome, TurnOutcome), type(outcome).__name__
                        assert outcome.thread_id == thread_id and outcome.turn_id == submission.turn_id
                        assert outcome.error is None, type(outcome.error).__name__
                        assert outcome.result is not None
                        terminal = outcome.result.status.value
                        expected_terminal = "completed" if name == "attached_completion" else "interrupted"
                        assert terminal == expected_terminal, terminal
                        if terminal == "completed":
                            assert outcome.result.final_response == "after-delete"
                    subscription = runtime.thread_subscription_snapshot(binding.id)
                    assert subscription is not None
                    assert subscription.state is ThreadSubscriptionState.RELEASE_PENDING
                    assert subscription.release_in_seconds is not None
                    if not attached:
                        # The retained subscription handle may refer to a
                        # shutdown runtime. Ordinary submission must resume
                        # the exact native Thread before its next Turn.
                        continued = await submit("Use the scripted response after native shutdown.")
                        assert continued.thread_id == thread_id
                        assert continued.turn_id != submission.turn_id
                        await wait_outcomes(3)
                        assert await runtime.wait_idle(timeout=5)
                        assert len(outcomes) == 3
                        resumed = outcomes[-1]
                        assert isinstance(resumed, TurnOutcome)
                        assert resumed.thread_id == thread_id and resumed.turn_id == continued.turn_id
                        assert resumed.error is None and resumed.result is not None
                        assert resumed.result.status.value == "completed"
                        assert resumed.result.final_response == "resumed-after-rejection"
                    if terminal == "interrupted":
                        # Native shutdown/interrupt cancels the gated HTTP
                        # request; the mock may observe the resulting reset.
                        assert all(error == "model response connection closed early"
                                   for error in provider.errors), provider.errors
                    else:
                        assert not provider.errors, provider.errors
                    assert len(provider.requests) == (3 if goal_case or not attached else 2)
                    assert not provider.replies
                    return {
                        "scenario": name, "passed": True,
                        "native_rejection": "attached_thread" if attached else "forked_history",
                        "rejection_before_shutdown": attached,
                        "binding_preserved": True, "lifecycle_released": True,
                        "observer_not_cancelled": True, "discard_events": 0,
                        "idle_release_scheduled_after_terminal": True,
                        "exact_terminal_delivered": terminal,
                        "stop_result": stop_result.value if stop_result is not None else None,
                        "goal_finalization": "cleared" if goal_case else None,
                        "same_thread_followup_completed": True if not attached else None,
                        "model_request_count": len(provider.requests),
                        "interrupted_http_disconnects": len(provider.errors),
                    }
                finally:
                    gate.set()
                    runtime.close_admission()
                    await runtime.cancel_tasks()
        finally:
            store.close()


async def probe(timeout: float) -> dict[str, Any]:
    cases = []
    async with asyncio.timeout(timeout):
        for name in CASES:
            with tempfile.TemporaryDirectory(prefix="netizen-delete-rejection-") as directory:
                cases.append(await _case(Path(directory), name))
    return {
        "passed": True, **source_evidence(), "cases": cases,
        "sdk_version": importlib.metadata.version("openai-codex"),
        "cli_version": importlib.metadata.version("openai-codex-cli-bin"),
        "real_model_used": False, "feishu_delivery_tested": False,
        "transport": "bundled App Server + local mock Responses HTTP SSE",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        result = asyncio.run(probe(args.timeout))
    except Exception as error:
        if args.output is not None:
            args.output.write_text(json.dumps({
                "passed": False, **source_evidence(), "error_type": type(error).__name__,
            }, indent=2) + "\n", encoding="utf-8")
        raise
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
