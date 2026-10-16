#!/usr/bin/env python3
"""Probe native external session input with a disposable local Responses server.

Runs the bundled App Server and public SDK, with no account or Feishu messages.
The provider scripts only model responses; native admission and persisted history
are real. Run explicitly because the probe needs a loopback HTTP socket.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable

from probe_sdk_partial_answers import (
    isolated_config,
    message_events,
    mock_provider,
    response_events,
)


def final_reply(name: str, text: str) -> tuple[int, bytes]:
    return 200, response_events(name, *message_events(name + "-answer", text, "final_answer"))


def native_json(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


async def until(predicate: Callable[[], bool], *, timeout: float = 15) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def external_message(marker: str) -> Any:
    from openai_codex import ExternalMessage

    return ExternalMessage(tool_name="send_message_to_thread", namespace="netizen", content=marker)


def check_external_item(items: list[dict[str, Any]], marker: str, *, history: bool) -> dict[str, Any]:
    matching = [item for item in items if marker in json.dumps(item)]
    assert len(matching) == 1, matching
    item = matching[0]
    expected_type = "functionCallOutput" if history else "function_call_output"
    assert item["type"] == expected_type and item.get("role") != "user", item
    assert item["name"] == "send_message_to_thread" and item["namespace"] == "netizen", item
    assert item["output"] == marker, item
    return {key: item[key] for key in ("type", "name", "namespace", "output")}


async def sdk_create_idle_case(root: Path) -> dict[str, Any]:
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

    markers = ("NATIVE-EXTERNAL-CREATE", "NATIVE-EXTERNAL-IDLE")
    replies = [final_reply("create", "CREATED"), final_reply("idle", "IDLE-REPLIED")]
    with mock_provider(replies) as provider:
        cwd, env = isolated_config(root, provider)
        env.pop("HOME", None)
        config = CodexConfig(cwd=str(cwd), env=env)
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_start(
                cwd=str(cwd), approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only,
            )
            turn_ids = []
            model_inputs = []
            for index, marker in enumerate(markers):
                handle = await thread.turn(external_message(marker))
                result = await handle.run()
                assert result.id == handle.id and result.status.value == "completed", result
                turn_ids.append(handle.id)
                model_inputs.append(check_external_item(provider.requests[index]["body"]["input"], marker, history=False))
            assert len(set(turn_ids)) == 2, turn_ids
        # Reopen the native process: the provenance must survive persistence.
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_resume(thread.id)
            snapshot = native_json(await thread.read(include_turns=True))["thread"]
            assert snapshot["status"]["type"] == "idle", snapshot
            assert [turn["id"] for turn in snapshot["turns"]] == turn_ids, snapshot
            history_items = [
                check_external_item(turn["items"], marker, history=True)
                for turn, marker in zip(snapshot["turns"], markers, strict=True)
            ]
            assert all(turn["status"] == "completed" for turn in snapshot["turns"])
            assert not provider.errors and not provider.replies and len(provider.requests) == 2, provider.errors
            return {
                "scenario": "sdk_create_and_idle", "passed": True,
                "thread_id": thread.id, "turn_ids": turn_ids,
                "native_started_distinct_turns": True, "model_inputs": model_inputs,
                "persisted_inputs": history_items, "cold_history_verified": True,
                "model_request_count": len(provider.requests),
            }


async def sdk_join_case(root: Path) -> dict[str, Any]:
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

    marker = "NATIVE-EXTERNAL-JOIN"
    with mock_provider([final_reply("initial", "INITIAL"), final_reply("joined", "JOIN-REPLIED")]) as provider:
        first_gate = threading.Event()
        provider.response_gates = {1: first_gate}
        cwd, env = isolated_config(root, provider)
        env.pop("HOME", None)
        async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
            thread = await codex.thread_start(
                cwd=str(cwd), approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only,
            )
            initial = await thread.turn("NATIVE-USER-SEED")
            initial_result = asyncio.create_task(initial.run())
            consumers = [initial_result]
            try:
                await until(lambda: len(provider.requests) == 1)
                joined = await thread.turn(external_message(marker))
                assert joined.id == initial.id, (joined.id, initial.id)
                joined_result = asyncio.create_task(joined.run())
                consumers.append(joined_result)
                first_gate.set()
                results = await asyncio.gather(initial_result, joined_result)
                assert all(result.id == initial.id and result.status.value == "completed" for result in results), results
                assert all(result.final_response == "JOIN-REPLIED" for result in results), results
                snapshot = native_json(await thread.read(include_turns=True))["thread"]
                assert len(snapshot["turns"]) == 1 and snapshot["turns"][0]["id"] == initial.id, snapshot
                turn = snapshot["turns"][0]
                persisted = check_external_item(turn["items"], marker, history=True)
                model_input = check_external_item(provider.requests[1]["body"]["input"], marker, history=False)
                assert len([item for item in turn["items"] if item["type"] == "userMessage"]) == 1, turn
                assert not provider.errors and not provider.replies and len(provider.requests) == 2, provider.errors
                return {
                    "scenario": "sdk_join_running", "passed": True,
                    "thread_id": thread.id, "original_turn_id": initial.id,
                    "joined_turn_id": joined.id, "native_joined_existing_turn": True,
                    "persisted_input": persisted, "model_input": model_input,
                    "history_turn_count": len(snapshot["turns"]),
                    "model_request_count": len(provider.requests),
                }
            finally:
                first_gate.set()
                for consumer in consumers:
                    if not consumer.done():
                        consumer.cancel()
                await asyncio.gather(*consumers, return_exceptions=True)


class ReadBarrier:
    """Delay one real public SDK history result, without changing its contents."""

    def __init__(self, codex: Any) -> None:
        self.codex = codex
        self.observed = asyncio.Event()
        self.release = asyncio.Event()
        self.claimed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.codex, name)

    async def thread_start(self, **kwargs: Any) -> Any:
        return ObservedThread(await self.codex.thread_start(**kwargs), self)

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> Any:
        return ObservedThread(await self.codex.thread_resume(thread_id, **kwargs), self)


class ObservedThread:
    def __init__(self, thread: Any, barrier: ReadBarrier) -> None:
        self.thread = thread
        self.barrier = barrier

    def __getattr__(self, name: str) -> Any:
        return getattr(self.thread, name)

    async def read(self, **kwargs: Any) -> Any:
        response = await self.thread.read(**kwargs)
        turns = native_json(response)["thread"].get("turns", [])
        if not self.barrier.claimed and any(turn["status"] == "inProgress" for turn in turns):
            self.barrier.claimed = True
            self.barrier.observed.set()
            await self.barrier.release.wait()
        return response


async def runtime_case(root: Path, *, terminal_race: bool) -> dict[str, Any]:
    from openai_codex import AsyncCodex, AsyncThread, CodexConfig

    source_root = str(Path(__file__).resolve().parents[1])
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from netizen_cli.bindings import BindingStore
    from netizen_cli.codex_runtime import CodexRuntime
    from netizen_cli.domain import FeishuScope, ScopeKind
    from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup

    markers = ("NATIVE-RUNTIME-INITIAL", "NATIVE-RUNTIME-FOLLOWUP")
    with mock_provider([final_reply("initial", "INITIAL-RESULT"), final_reply("followup", "FOLLOWUP-RESULT")]) as provider:
        first_gate = threading.Event()
        provider.response_gates = {1: first_gate}
        cwd, env = isolated_config(root, provider)
        env.pop("HOME", None)
        store = BindingStore(root / "channel.sqlite3")
        store.bootstrap_project(alias="session-probe", cwd=str(cwd))
        binding = store.create_channel_binding(
            scope=FeishuScope("probe-app", "probe-chat", ScopeKind.GROUP),
            project_alias="session-probe", creator_id="probe-owner",
        )
        outcomes: list[Any] = []
        completion = asyncio.Condition()

        async def on_completion(outcome: Any) -> None:
            async with completion:
                outcomes.append(outcome)
                completion.notify_all()

        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                barrier = ReadBarrier(codex)
                runtime = CodexRuntime(
                    codex=barrier if terminal_race else codex,
                    bindings=store, terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                    on_completion=on_completion, poll_interval_seconds=0.01,
                    automatic_thread_naming=False,
                )
                try:
                    initial = await runtime.submit_external(
                        binding=store.get(binding.id), cwd=cwd, message=external_message(markers[0]),
                        owner_id="initial-owner", origin="initial-origin",
                    )
                    assert initial.disposition.value == "started", initial
                    assert initial.release_receipt_attempt is not None
                    initial.release_receipt_attempt()
                    await until(lambda: len(provider.requests) == 1)
                    thread = AsyncThread(codex, initial.thread_id)
                    if terminal_race:
                        await asyncio.wait_for(barrier.observed.wait(), 10)
                        first_gate.set()
                        async with asyncio.timeout(10):
                            while True:
                                snapshot = native_json(await thread.read(include_turns=True))["thread"]
                                if snapshot["turns"][0]["status"] == "completed":
                                    break
                                await asyncio.sleep(0.01)
                        active = runtime.active_turn(binding.id)
                        assert active is not None and active.turn_id == initial.turn_id, active
                        assert not outcomes, "old completion must still be waiting for its real read response"
                    followup = await runtime.submit_external(
                        binding=store.get(binding.id), cwd=cwd, message=external_message(markers[1]),
                        owner_id="followup-owner", origin="followup-origin",
                    )
                    if terminal_race:
                        assert followup.disposition.value == "started" and followup.turn_id != initial.turn_id, followup
                        assert followup.release_receipt_attempt is not None
                        followup.release_receipt_attempt()
                    else:
                        assert followup.disposition.value == "joined" and followup.turn_id == initial.turn_id, followup
                        assert followup.release_receipt_attempt is None, followup
                    barrier.release.set()
                    first_gate.set()
                    expected_count = 2 if terminal_race else 1
                    async with asyncio.timeout(15):
                        async with completion:
                            await completion.wait_for(lambda: len(outcomes) >= expected_count)
                    assert await runtime.wait_idle(timeout=5), "runtime consumers did not drain"
                    assert len(outcomes) == expected_count, outcomes
                    assert len({outcome.turn_id for outcome in outcomes}) == expected_count, outcomes
                    assert all(outcome.error is None and outcome.status == "completed" for outcome in outcomes), outcomes
                    by_turn = {outcome.turn_id: outcome for outcome in outcomes}
                    assert by_turn[initial.turn_id].owner_id == "initial-owner", outcomes
                    assert by_turn[initial.turn_id].origin == "initial-origin", outcomes
                    if terminal_race:
                        assert by_turn[initial.turn_id].final_response == "INITIAL-RESULT", outcomes
                        assert by_turn[followup.turn_id].final_response == "FOLLOWUP-RESULT", outcomes
                        assert by_turn[followup.turn_id].owner_id == "followup-owner", outcomes
                        assert by_turn[followup.turn_id].origin == "followup-origin", outcomes
                    else:
                        assert by_turn[initial.turn_id].final_response == "FOLLOWUP-RESULT", outcomes
                    snapshot = native_json(await thread.read(include_turns=True))["thread"]
                    assert len(snapshot["turns"]) == expected_count, snapshot
                    items = [item for turn in snapshot["turns"] for item in turn["items"]]
                    persisted = [check_external_item(items, marker, history=True) for marker in markers]
                    assert not any(item["type"] == "userMessage" for item in items), items
                    assert not provider.errors and not provider.replies and len(provider.requests) == 2, provider.errors
                    return {
                        "scenario": "runtime_terminal_race" if terminal_race else "runtime_join_once",
                        "passed": True, "thread_id": initial.thread_id,
                        "initial_turn_id": initial.turn_id, "followup_turn_id": followup.turn_id,
                        "followup_disposition": followup.disposition.value,
                        "completion_count": len(outcomes), "history_turn_count": len(snapshot["turns"]),
                        "completion_turn_ids": [outcome.turn_id for outcome in outcomes],
                        "original_origin_preserved": True, "each_turn_completed_once": True,
                        "production_submit_external": True,
                        "real_history_read_delayed": terminal_race,
                        "persisted_inputs": persisted, "model_request_count": len(provider.requests),
                    }
                finally:
                    first_gate.set()
                    barrier.release.set()
                    await runtime.interrupt_all()
                    await runtime.cancel_tasks()
        finally:
            store.close()


class NamingRecorder:
    """Observe real public SDK calls; optionally delay an actual title write."""

    def __init__(self, codex: Any, *, hold_title: bool) -> None:
        self.codex = codex
        self.forks: list[dict[str, Any]] = []
        self.title_started = asyncio.Event()
        self.title_release = asyncio.Event()
        self.title_completed = asyncio.Event()
        if not hold_title:
            self.title_release.set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.codex, name)

    async def thread_start(self, **kwargs: Any) -> Any:
        return NamingObservedThread(await self.codex.thread_start(**kwargs), self)

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> Any:
        return NamingObservedThread(await self.codex.thread_resume(thread_id, **kwargs), self)

    async def thread_fork(self, thread_id: str, **kwargs: Any) -> Any:
        fork = await self.codex.thread_fork(thread_id, **kwargs)
        native = (await fork.read(include_turns=False)).thread
        self.forks.append({"thread_id": fork.id, "parent_id": thread_id,
                           "ephemeral": native.ephemeral, "path": native.path})
        return fork


class NamingObservedThread:
    def __init__(self, thread: Any, recorder: NamingRecorder) -> None:
        self.thread, self.recorder = thread, recorder

    def __getattr__(self, name: str) -> Any:
        return getattr(self.thread, name)

    async def set_name(self, name: str) -> Any:
        self.recorder.title_started.set()
        await self.recorder.title_release.wait()
        result = await self.thread.set_name(name)
        self.recorder.title_completed.set()
        return result


async def runtime_naming_case(root: Path, *, supplied_title: bool) -> dict[str, Any]:
    """Native external-input naming plus the production default history view."""
    from openai_codex import AsyncCodex, AsyncThread, CodexConfig

    source_root = str(Path(__file__).resolve().parents[1])
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from netizen_cli.bindings import BindingStore
    from netizen_cli.channel_app import ChannelApplication
    from netizen_cli.codex_runtime import CodexRuntime
    from netizen_cli.domain import FeishuScope, ScopeKind
    from netizen_cli.sdk_gap_adapter import AppServerThreadSubscriptionControl
    from netizen_cli.sessions.policy import SessionPolicyService
    from netizen_cli.sessions.service import SessionService
    from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup

    expected_title = "Provided collaboration title" if supplied_title else "Generated collaboration title"
    task_text = "NATIVE-EXTERNAL-NAMING: inspect <migration> & preserve &lt;literal&gt;."
    replies = [final_reply("work", "EXTERNAL-TASK-COMPLETED")]
    if not supplied_title:
        replies.append(final_reply("name", json.dumps({"title": expected_title})))
    with mock_provider(replies) as provider:
        work_gate, naming_gate = threading.Event(), threading.Event()
        provider.response_gates = {1: work_gate, **({2: naming_gate} if not supplied_title else {})}
        cwd, env = isolated_config(root, provider)
        env.pop("HOME", None)
        store = BindingStore(root / "channel.sqlite3")
        store.bootstrap_project(alias="session-probe", cwd=str(cwd))
        source_binding, target_binding = [store.create_channel_binding(
            scope=FeishuScope("probe-app", chat_id, ScopeKind.GROUP),
            project_alias="session-probe", creator_id="probe-owner",
        ) for chat_id in ("probe-source", "probe-target")]
        outcomes: list[Any] = []

        async def on_completion(outcome: Any) -> None:
            outcomes.append(outcome)

        async def unused_mutation(**_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("read_thread must not invoke a mutation")

        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                source_thread = await codex.thread_start(cwd=str(cwd))
                store.assign_native_thread_id(source_binding.id, source_thread.id)
                external = ChannelApplication._session_message(
                    store.get(source_binding.id), task_text, tool_name="create_thread",
                )
                envelope = external.content
                recorder = NamingRecorder(codex, hold_title=supplied_title)
                runtime = CodexRuntime(
                    codex=recorder, bindings=store,
                    terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                    thread_subscription_control=AppServerThreadSubscriptionControl(codex),
                    on_completion=on_completion, poll_interval_seconds=0.01,
                )
                try:
                    submitted = await runtime.submit_external(
                        binding=store.get(target_binding.id), cwd=cwd, message=external,
                        owner_id="probe-owner", origin="external-naming-probe",
                        title=expected_title if supplied_title else None,
                    )
                    assert submitted.disposition.value == "started" and submitted.release_receipt_attempt
                    submitted.release_receipt_attempt()
                    namer = runtime._thread_namer
                    assert namer is not None
                    job = namer._jobs.get(target_binding.id)
                    assert job is not None and job.task is not None and job.task not in runtime._tasks
                    if supplied_title:
                        await asyncio.wait_for(recorder.title_started.wait(), 10)
                        assert not recorder.title_completed.is_set(), "submission waited for the name write"
                    else:
                        await until(lambda: len(provider.requests) == 2)
                        inherited = [item for item in provider.requests[1]["body"]["input"]
                                     if item.get("type") == "function_call_output" and item.get("output") == envelope]
                        assert len(inherited) == 1, provider.requests[1]["body"]["input"]
                        assert inherited[0].get("namespace") == external.namespace
                        assert inherited[0].get("name") == external.tool_name
                        assert recorder.forks == [{"thread_id": job.thread.id, "parent_id": submitted.thread_id,
                                                   "ephemeral": True, "path": None}], recorder.forks
                    work_gate.set()
                    assert await runtime.wait_idle(timeout=10), "main task did not complete while naming was held"
                    assert not job.task.done(), "naming should still be waiting on its independent barrier"
                    assert len(outcomes) == 1 and outcomes[0].final_response == "EXTERNAL-TASK-COMPLETED", outcomes
                    assert outcomes[0].error is None and outcomes[0].status == "completed", outcomes
                    recorder.title_release.set()
                    naming_gate.set()
                    await asyncio.wait_for(asyncio.shield(job.task), 15)
                    assert recorder.title_completed.is_set()
                    assert len(recorder.forks) == (0 if supplied_title else 1), recorder.forks
                    assert target_binding.id not in namer._jobs, "naming resource ownership did not drain"
                    snapshot = native_json(await AsyncThread(codex, submitted.thread_id).read(include_turns=True))["thread"]
                    assert snapshot["name"] == expected_title, snapshot
                    assert snapshot["preview"] == task_text, snapshot
                    for sort_key in ("created_at", "updated_at"):
                        for sort_direction in ("asc", "desc"):
                            catalog = await runtime.thread_catalog_page(
                                archived=False, limit=100, sort_key=sort_key,
                                sort_direction=sort_direction,
                                deadline=asyncio.get_running_loop().time() + 5,
                            )
                            listed = next((item for item in catalog.threads if item.thread_id == submitted.thread_id), None)
                            assert listed is not None and listed.preview == task_text, catalog
                            assert type(listed.created_at) is int and type(listed.updated_at) is int, listed
                    archived = await runtime.thread_catalog_page(
                        archived=True, limit=100, deadline=asyncio.get_running_loop().time() + 5,
                    )
                    assert not archived.threads and archived.next_cursor is None, archived
                    assert [turn["id"] for turn in snapshot["turns"]] == [submitted.turn_id], snapshot
                    items = snapshot["turns"][0]["items"]
                    assert not any(item["type"] == "userMessage" for item in items), items
                    assert len([item for item in items if item["type"] == "functionCallOutput" and item.get("output") == envelope]) == 1
                    service = SessionService(bindings=store, runtime=runtime, codex=codex, app_id="probe-app",
                                             policy=SessionPolicyService(), create_thread=unused_mutation,
                                             send_message=unused_mutation)
                    history = await service.call("read_thread", {"threadId": submitted.thread_id}, source_thread.id)
                    assert "error" not in history, history
                    delegations = [item for item in history["turns"][0]["items"] if "codexDelegation" in item]
                    assert len(delegations) == 1, history
                    delegation = delegations[0]
                    assert delegation["codexDelegation"] == {"sourceThreadId": source_thread.id, "input": task_text}
                    assert "output" not in delegation, delegation
                    assert not provider.errors and not provider.replies, provider.errors
                    assert len(provider.requests) == (1 if supplied_title else 2)
                    return {"scenario": "runtime_external_supplied_title" if supplied_title else "runtime_external_generated_title",
                            "passed": True, "thread_id": submitted.thread_id, "turn_id": submitted.turn_id,
                            "title": snapshot["name"], "forks": recorder.forks,
                            "model_request_count": len(provider.requests), "completion_count": len(outcomes),
                            "submission_and_completion_independent_of_naming": True,
                            "parent_history_contains_only_original_turn": True,
                            "naming_inherits_exact_external_tool_input": not supplied_title,
                            "default_read_delegation": delegation, "raw_output_hidden": True,
                            "native_preview": snapshot["preview"], "native_state_db_listed": True,
                            "production_catalog_page_time_sorts": True, "empty_archived_page": True,
                            "production_runtime_and_read_service": True, "feishu_delivery_tested": False}
                finally:
                    work_gate.set()
                    naming_gate.set()
                    recorder.title_release.set()
                    await runtime.interrupt_all()
                    await runtime.cancel_tasks()
        finally:
            store.close()


def advertised_tools(tools: list[dict[str, Any]]) -> set[tuple[str, str]]:
    found = set()
    for tool in tools:
        if tool.get("type") == "namespace":
            found.update((tool["name"], nested["name"]) for nested in tool.get("tools", []))
        elif tool.get("type") == "function":
            found.add(("", tool["name"]))
    return found


async def mcp_registration_case(root: Path) -> dict[str, Any]:
    """Production MCP transport/config plus native per-call caller identity."""
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

    source_root = str(Path(__file__).resolve().parents[1])
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from netizen_cli.schedules.mcp import ScheduleMcpRunner
    from netizen_cli.sessions.mcp import SessionMcpRunner
    from netizen_cli.sessions.schema import TOOL_ARGUMENTS

    runner = SessionMcpRunner()
    existing = ScheduleMcpRunner()
    calls: list[dict[str, Any]] = []

    async def callback(name: str, arguments: dict[str, Any], source_id: str) -> dict[str, Any]:
        calls.append({"name": name, "arguments": arguments, "source_thread_id": source_id})
        return {"schemaVersion": 4, "threads": [], "probe": "NATIVE-SESSION-MCP-CALLBACK"}

    async def unused_callback(*_args: Any) -> dict[str, Any]:
        raise AssertionError("existing user tool should only be discovered")

    runner.attach(callback)
    existing.attach(unused_callback)
    try:
        await existing.bind()
        existing.open_admission()
        await runner.bind()
        runner.open_admission()
        call = {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "session-native-list", "name": "list_threads",
            "namespace": "mcp__" + runner.namespace, "arguments": json.dumps({"limit": 1}),
        }}
        with mock_provider([
            (200, response_events("mcp-tool", call)),
            final_reply("mcp-final", "NATIVE-SESSION-MCP-DONE"),
        ]) as provider:
            cwd, env = isolated_config(root, provider)
            env.pop("HOME", None)
            # Keep a distinct pre-existing user MCP entry in the config file.
            # Its real advertised tool proves the new override did not replace
            # the mcp_servers table. No schedule management callback is called.
            config_path = Path(env["CODEX_HOME"]) / "config.toml"
            existing_entry = existing.config_overrides[0].split("=", 1)[1]
            config_path.write_text("mcp_servers.user_existing=" + existing_entry + "\n" + config_path.read_text())
            original_config = config_path.read_bytes()
            config = CodexConfig(
                cwd=str(cwd), env={**env, **runner.app_server_env, **existing.app_server_env},
                config_overrides=runner.config_overrides,
            )
            async with AsyncCodex(config) as codex:
                thread = await codex.thread_start(
                    cwd=str(cwd), approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only,
                )
                handle = await thread.turn("NATIVE-SESSION-MCP-PROBE: list sessions using the provided tool.")
                result = await handle.run()
                assert result.status.value == "completed" and result.final_response == "NATIVE-SESSION-MCP-DONE", result
                advertised = advertised_tools(provider.requests[0]["body"]["tools"])
                expected = {("mcp__" + runner.namespace, name) for name in TOOL_ARGUMENTS}
                assert expected <= advertised, {"missing": sorted(expected - advertised), "advertised": sorted(advertised)}
                assert ("mcp__user_existing", "cron_manage") in advertised, advertised
                assert calls == [{"name": "list_threads", "arguments": {"limit": 1}, "source_thread_id": thread.id}], calls
                outputs = [item for item in provider.requests[1]["body"]["input"]
                           if item.get("type") == "function_call_output" and item.get("call_id") == "session-native-list"]
                assert len(outputs) == 1 and "NATIVE-SESSION-MCP-CALLBACK" in json.dumps(outputs[0]), outputs
                snapshot = native_json(await thread.read(include_turns=True))["thread"]
                native_calls = [item for turn in snapshot["turns"] for item in turn["items"]
                                if item.get("type") == "mcpToolCall"]
                assert len(native_calls) == 1 and native_calls[0]["status"] == "completed", native_calls
                assert native_calls[0]["server"] == runner.namespace and native_calls[0]["tool"] == "list_threads", native_calls
                assert not provider.errors and not provider.replies and len(provider.requests) == 2, provider.errors
            assert config_path.read_bytes() == original_config, "process override changed user config file"
            return {
                "scenario": "session_mcp_registration", "passed": True,
                "thread_id": thread.id, "turn_id": handle.id,
                "advertised_session_tools": sorted(TOOL_ARGUMENTS),
                "callback_calls": calls, "real_native_caller_metadata": True,
                "native_tool_result_observed": True, "existing_user_mcp_preserved": True,
                "config_file_unchanged": True,
                "config_sha256": hashlib.sha256(original_config).hexdigest(),
                "production_config_overrides_parsed": True,
                "mutation_approval_policy": "prompt", "default_tool_approval_policy": "approve",
                "tool_timeout_seconds": 145, "mutation_approval_interaction_tested": False,
                "full_timeout_elapsed_tested": False, "application_callback_fixture": True,
                "model_request_count": len(provider.requests),
            }
    finally:
        runner.close_admission()
        existing.close_admission()
        await runner.close()
        await existing.close()


async def run_probe(selected: str = "all") -> dict[str, Any]:
    from codex_cli_bin import bundled_codex_path

    version = subprocess.run(
        [str(bundled_codex_path()), "--version"], check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    cases = []
    with tempfile.TemporaryDirectory(prefix="netizen-session-collaboration-") as directory:
        names = ("sdk_create_idle", "sdk_join", "runtime_join", "runtime_terminal_race",
                 "runtime_external_name", "runtime_external_title", "session_mcp") if selected == "all" else (selected,)
        for name in names:
            root = Path(directory) / name
            root.mkdir()
            async with asyncio.timeout(45):
                if name == "session_mcp":
                    cases.append(await mcp_registration_case(root))
                elif name in {"runtime_external_name", "runtime_external_title"}:
                    cases.append(await runtime_naming_case(root, supplied_title=name == "runtime_external_title"))
                elif name.startswith("runtime_"):
                    cases.append(await runtime_case(root, terminal_race=name == "runtime_terminal_race"))
                else:
                    case = sdk_create_idle_case if name == "sdk_create_idle" else sdk_join_case
                    cases.append(await case(root))
    return {
        "sdk_version": importlib.metadata.version("openai-codex"),
        "bundled_version": version,
        "transport": "bundled App Server + local mock Responses HTTP SSE",
        "real_model_used": False, "feishu_delivery_tested": False, "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", "sdk_create_idle", "sdk_join", "runtime_join", "runtime_terminal_race",
                                          "runtime_external_name", "runtime_external_title", "session_mcp"), default="all")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file")
    args = parser.parse_args()
    result = asyncio.run(run_probe(args.case))
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
