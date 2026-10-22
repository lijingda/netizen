#!/usr/bin/env python3
"""Verify scheduled auto-archive against a disposable bundled App Server.

Uses production Scheduler/ChannelApplication/Runtime/management and SQLite with
an in-memory Feishu transport and loopback scripted Responses provider. No real
model account, Feishu messages or existing Codex state are used. Run explicitly:
``timeout --signal=INT --kill-after=5s 120s .venv/bin/python
scripts/probe_scheduled_auto_archive.py --output /tmp/scheduled-auto-archive.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from typing import Any
import uuid

from openai_codex import AsyncCodex, AsyncThread, CodexConfig

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen_cli.bindings import BindingStore  # noqa: E402
from netizen_cli.channel_app import ChannelApplication  # noqa: E402
from netizen_cli.codex_runtime import CodexRuntime  # noqa: E402
from netizen_cli.domain import FeishuScope, ScopeKind  # noqa: E402
from netizen_cli.management.coordination import ScopeCoordinator  # noqa: E402
from netizen_cli.management.service import (  # noqa: E402
    ExactBindingTarget, InstanceManagementService, ManagementRuntimePort,
)
from netizen_cli.projects import ProjectRegistry  # noqa: E402
from netizen_cli.runtime.contracts import TurnOutcome  # noqa: E402
from netizen_cli.schedules.models import ScheduleRule  # noqa: E402
from netizen_cli.schedules.scheduler import Scheduler  # noqa: E402
from netizen_cli.schedules.service import ScheduleService  # noqa: E402
from netizen_cli.sdk_gap_adapter import AppServerThreadSubscriptionControl  # noqa: E402
from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup  # noqa: E402
from netizen_cli.worktrees.policy import WorkspacePolicy  # noqa: E402
from scripts.probe_scheduled_tasks import FakeFeishu  # noqa: E402
from scripts.probe_sdk_partial_answers import (  # noqa: E402
    isolated_config, message_events, mock_provider, response_events,
)


ANSWER = "SCHEDULE-AUTO-ARCHIVE-RESULT"


class DeliveryTransport(FakeFeishu):
    """Hold the result ACK while ordinary native follow-up remains possible."""

    def __init__(self) -> None:
        super().__init__()
        self.result_started = asyncio.Event()
        self.allow_result_ack = asyncio.Event()
        self.result_contents: list[object] = []
        self.events: list[str] = []

    async def reply(self, message: object, content: object, opts: object = None) -> object:
        if content == ANSWER:
            self.result_contents.append(content)
            self.result_started.set()
            await self.allow_result_ack.wait()
            self.events.append("result_ack")
        return await super().reply(message, content, opts)


class ObservedCodex:
    """Observe the public archive call without replacing any native result."""

    def __init__(self, codex: AsyncCodex, channel: DeliveryTransport) -> None:
        self.codex = codex
        self.channel = channel
        self.archived_ids: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.codex, name)

    async def thread_archive(self, thread_id: str) -> None:
        assert self.channel.events == ["result_ack"], self.channel.events
        self.channel.events.append("archive")
        self.archived_ids.append(thread_id)
        await self.codex.thread_archive(thread_id)


async def _catalog(codex: AsyncCodex, thread_id: str, *, archived: bool) -> bool:
    # Each fixture has exactly one Thread; no production pagination is replaced.
    result = await codex.thread_list(archived=archived, limit=100)
    assert result.next_cursor is None
    return any(thread.id == thread_id for thread in result.data)


async def _case(root: Path, name: str, *, manual: bool, auto_archive: bool,
                edit_after_claim: bool = False, followup: bool = False) -> dict[str, Any]:
    replies = [(200, response_events("initial", *message_events(
        "initial-answer", ANSWER, "final_answer",
    )))]
    if followup:
        replies.append((200, response_events("followup", *message_events(
            "followup-answer", "FOLLOWUP-DONE", "final_answer",
        ))))
    with mock_provider(replies) as provider:
        if followup:
            provider.response_gates[2] = threading.Event()
        cwd, env = isolated_config(root, provider)
        # Keep the host proxy configuration; only this local fixture bypasses it.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
        for key in ("NO_PROXY", "no_proxy"):
            env[key] = os.environ.get(key, "") + ",127.0.0.1,localhost"
        store = BindingStore(root / "channel.sqlite3")
        channel = DeliveryTransport()
        clock = [1_800_000_000.0]
        store.bootstrap_project(alias="probe", cwd=str(cwd))
        projects = ProjectRegistry(store=store, project_root=root, projects={"probe": cwd})
        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                observed = ObservedCodex(codex, channel)
                runtime = CodexRuntime(
                    codex=observed, bindings=store,
                    terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                    thread_subscription_control=AppServerThreadSubscriptionControl(codex),
                    automatic_thread_naming=False,
                )
                management = InstanceManagementService(
                    bindings=store, projects=projects, runtime=ManagementRuntimePort(runtime),
                    scope_coordinator=ScopeCoordinator(), chat_directory=channel,
                )
                application = ChannelApplication(
                    app_id="probe", channel=channel, runtime=runtime, bindings=store,
                    projects=projects, management=management,
                )
                outcomes: asyncio.Queue[TurnOutcome | BaseException] = asyncio.Queue()

                async def completed(outcome: object) -> None:
                    try:
                        await application.handle_completion(outcome)
                    except BaseException as error:
                        outcomes.put_nowait(error)
                        raise
                    if isinstance(outcome, TurnOutcome):
                        outcomes.put_nowait(outcome)

                runtime.set_completion_handler(completed)
                scheduler = Scheduler(store, runtime, "probe", application.dispatch_scheduled_run, lambda: clock[0])
                service = ScheduleService(
                    bindings=store, runtime=runtime, app_id="probe", chat_info=channel,
                    wall_clock=lambda: clock[0], default_timezone="UTC",
                    chat_target_validator=channel.validate_target,
                )
                service.set_run_now_handler(scheduler.run_now)
                service.set_refresh_handler(scheduler.refresh)
                source = store.create_channel_binding(
                    scope=FeishuScope("probe", "probe-chat", ScopeKind.GROUP),
                    project_alias="probe", creator_id="probe-owner",
                )
                try:
                    await scheduler.recover()
                    plan_id = store.schedules.create(
                        name=name, instructions="Return the scripted probe response without tools.",
                        project_alias="probe", app_id="probe", chat_id="probe-chat",
                        workspace=WorkspacePolicy(), enabled=not manual, auto_archive=auto_archive,
                        schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=clock[0]),
                        request_id=str(uuid.uuid4()), now=clock[0],
                    ).plan_id
                    saved = store.schedules.get(plan_id)
                    scheduler.start()
                    if manual:
                        receipt = await service.manage({
                            "mode": "run_now", "plan_id": plan_id,
                            "expected_revision": saved.revision, "request_id": str(uuid.uuid4()),
                        }, scope_key=source.scope_key, source="probe")
                        assert receipt.get("ok") and receipt.get("accepted"), receipt
                    else:
                        clock[0] += 60
                        assert await scheduler.tick() == 1
                    await scheduler.close()
                    assert await scheduler.drain(asyncio.get_running_loop().time() + 15)
                    await asyncio.wait_for(channel.result_started.wait(), 15)
                    run = store.schedules.list_runs(plan_id)[0]
                    assert run.trigger_source == ("manual" if manual else "scheduled")
                    binding = store.get(run.binding_id)
                    thread_id = binding.native_thread_id
                    assert thread_id and run.initial_turn_id
                    assert not observed.archived_ids
                    assert await _catalog(codex, thread_id, archived=False)
                    assert store.active_binding(source.scope_key).id == source.id
                    if edit_after_claim:
                        store.schedules.update(
                            plan_id, expected_revision=saved.revision,
                            request_id=str(uuid.uuid4()), now=clock[0],
                            changes={"auto_archive": False},
                        )
                        assert not store.schedules.get(plan_id).auto_archive
                    followup_id = None
                    if followup:
                        origin = SimpleNamespace(
                            id="followup", message_id="followup", chat_id="probe-chat",
                            conversation=SimpleNamespace(thread_id=run.topic_id),
                        )
                        submission = await runtime.submit(
                            binding=store.get(binding.id), cwd=cwd,
                            input="Use the second scripted probe response.",
                            owner_id="probe-owner", origin=origin,
                        )
                        submission.release_receipt_attempt()
                        followup_id = submission.turn_id
                        async with asyncio.timeout(10):
                            while len(provider.requests) < 2:
                                await asyncio.sleep(0.01)
                        assert runtime.active_turn(binding.id).turn_id == followup_id
                    channel.allow_result_ack.set()
                    outcome = await asyncio.wait_for(outcomes.get(), 20)
                    if isinstance(outcome, BaseException):
                        raise outcome
                    assert outcome.turn_id == run.initial_turn_id, outcome
                    assert outcome.status == "completed" and outcome.error is None
                    assert outcome.final_response == ANSWER
                    assert channel.result_contents == [ANSWER]
                    completed_run = store.schedules.get_run(run.id)
                    assert completed_run.barrier == "released" and completed_run.delivery_state == "sent"
                    assert store.active_binding(source.scope_key).id == source.id
                    assert observed.archived_ids == ([thread_id] if auto_archive else [])
                    assert await _catalog(codex, thread_id, archived=auto_archive)
                    assert not await _catalog(codex, thread_id, archived=not auto_archive)
                    if auto_archive:
                        assert store.active_binding(binding.scope_key) is None
                        restored = await management.restore_exact_binding_as_current(target=ExactBindingTarget(
                            scope_key=binding.scope_key, binding_id=binding.id, expected_active_binding_id=None,
                        ))
                        assert restored.native_thread_id == thread_id
                        assert store.active_binding(binding.scope_key).id == binding.id
                        assert await _catalog(codex, thread_id, archived=False)
                        assert not await _catalog(codex, thread_id, archived=True)
                        turns = (await AsyncThread(codex, thread_id).read(include_turns=True)).thread.turns
                        assert any(turn.id == run.initial_turn_id and turn.status.value == "completed" for turn in turns)
                    else:
                        assert store.active_binding(binding.scope_key).id == binding.id
                    if manual and not edit_after_claim:
                        assert store.schedules.get(plan_id) == saved
                    if followup:
                        provider.response_gates[2].set()
                    assert not provider.replies
                    assert all(error == "model response connection closed early" and followup for error in provider.errors), provider.errors
                    return {
                        "scenario": name, "passed": True, "trigger_source": run.trigger_source,
                        "auto_archive": auto_archive, "thread_id": thread_id, "turn_id": run.initial_turn_id,
                        "events": channel.events, "delivery_state": completed_run.delivery_state,
                        "result_unchanged": True, "source_current_unchanged": True,
                        "restored_through_existing_management": auto_archive,
                        "edited_after_claim": edit_after_claim, "followup_started_before_ack": followup_id is not None,
                        "model_request_count": len(provider.requests),
                    }
                finally:
                    channel.allow_result_ack.set()
                    for gate in provider.response_gates.values():
                        gate.set()
                    service.close_admission()
                    await scheduler.close()
                    await scheduler.drain(asyncio.get_running_loop().time() + 3)
                    await asyncio.wait_for(runtime.cancel_tasks(), 5)
                    await asyncio.wait_for(application.close(), 5)
                    await management.close(deadline=asyncio.get_running_loop().time() + 5)
        finally:
            store.close()


async def probe(timeout: float) -> dict[str, Any]:
    cases = []
    with tempfile.TemporaryDirectory(prefix="netizen-auto-archive-") as directory:
        async with asyncio.timeout(timeout):
            for name, options in (
                ("automatic_enabled", {"manual": False, "auto_archive": True}),
                ("manual_enabled", {"manual": True, "auto_archive": True}),
                ("automatic_disabled", {"manual": False, "auto_archive": False}),
                ("claim_option_frozen", {"manual": True, "auto_archive": True, "edit_after_claim": True}),
                ("followup_before_delivery", {"manual": True, "auto_archive": True, "followup": True}),
            ):
                root = Path(directory) / name
                root.mkdir()
                cases.append(await _case(root, name, **options))
    return {
        "passed": True, "sdk_version": importlib.metadata.version("openai-codex"),
        "cli_version": importlib.metadata.version("openai-codex-cli-bin"),
        "native_app_server": True, "real_model_account": False, "real_feishu_calls": False,
        "isolated_codex_state": True, "scenarios": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    result = asyncio.run(probe(args.timeout))
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
