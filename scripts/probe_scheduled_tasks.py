#!/usr/bin/env python3
"""Disposable real-SDK scheduling compatibility probe; never sends to Feishu.

Run only after reviewing this script, e.g. ``python -m
scripts.probe_scheduled_tasks --phase all --model <compatible-native-model>``.
The model must support the account's inherited reasoning effort.
Each phase uses a new disposable Git cwd and read-only native sandbox startup
intent. If native Codex adds trust metadata, cleanup removes only the exact new
fixture Project entries, preserving other configuration text and values.
Wrap the command in the platform's timeout/gtimeout as an additional process
deadline; ``--timeout`` bounds the asynchronous scenario itself.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import fcntl
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import openai_codex
from openai_codex import AsyncCodex, AsyncThread, CodexConfig
from openai_codex.errors import JsonRpcError


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen.bindings import BindingNotFound, BindingStore  # noqa: E402
from netizen.channel_app import ChannelApplication  # noqa: E402
from netizen.codex_runtime import CodexRuntime, StopDisposition  # noqa: E402
from netizen.domain import FeishuScope, ScopeKind  # noqa: E402
from netizen.management.coordination import ScopeCoordinator  # noqa: E402
from netizen.management.service import InstanceManagementService, ManagementRuntimePort  # noqa: E402
from netizen.projects import ProjectRegistry  # noqa: E402
from netizen.runtime.contracts import TurnOutcome  # noqa: E402
from netizen.schedules.mcp import ScheduleMcpRunner  # noqa: E402
from netizen.schedules.models import ScheduleRule  # noqa: E402
from netizen.schedules.scheduler import Scheduler  # noqa: E402
from netizen.schedules.service import ScheduleService  # noqa: E402
from netizen.session_settings import SessionSettings  # noqa: E402
from netizen.sdk_gap_adapter import AppServerThreadSubscriptionControl  # noqa: E402
from netizen.terminal_cleanup import PinnedExperimentalTerminalCleanup  # noqa: E402
from scripts.probe_project_delete import _DeleteOnce  # noqa: E402
from scripts.probe_python_sdk import (  # noqa: E402
    _prove_thread_absent_from_all_catalogs, _status_value, _thread_status_type,
)


class ProbeFailure(RuntimeError):
    """Codes are fixed strings, safe to print without native exception text."""


class ProbeCompletionFailure(ProbeFailure):
    def __init__(self, code: str, evidence: dict[str, Any]) -> None:
        super().__init__(code)
        self.evidence = evidence


class ProbeStopFailure(ProbeFailure):
    def __init__(self, error: BaseException) -> None:
        super().__init__("ordinary_stop_rpc_rejected")
        self.rpc: dict[str, Any] = {}
        seen: set[int] = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            if isinstance(error, JsonRpcError):
                message = error.message.lower()
                self.rpc = {
                    "code": error.code,
                    "not_active": any(fragment in message for fragment in (
                        "not active", "no active turn", "not running", "already completed", "already finished",
                    )),
                }
                break
            error = error.__cause__ or error.__context__


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ProbeFailure(code)


def _safe_traceback(error: BaseException) -> list[dict[str, Any]]:
    """Expose code locations only, without source, locals or exception values."""
    result = []
    seen: set[int] = set()
    while error is not None and id(error) not in seen and len(result) < 8:
        seen.add(id(error))
        frames = []
        trace = error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            try:
                filename = Path(code.co_filename).resolve().relative_to(SOURCE_ROOT)
            except ValueError:
                pass
            else:
                frames.append({"filename": filename.as_posix(), "lineno": trace.tb_lineno,
                               "function": code.co_name})
            trace = trace.tb_next
        result.append({"exception_type": type(error).__name__, "frames": frames})
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return result


class FakeFeishu:
    """In-memory transport; there is deliberately no Feishu client or secret."""

    bot_identity = SimpleNamespace(open_id="probe-bot", name="Schedule probe")

    def __init__(self, *, create_topics: bool = True) -> None:
        self.sent: dict[str, object] = {}
        self.replies = 0
        self.create_topics = create_topics

    async def get_chat_info(self, chat_id: str) -> object:
        return SimpleNamespace(chat_type="group")

    async def send(self, to: str, content: object, opts: object = None) -> object:
        key = getattr(opts, "uuid", None)
        _require(isinstance(key, str) and bool(key), "fixture_send_requires_uuid")
        if key not in self.sent:
            message_id = "probe-root-" + key
            data = {"message_id": message_id, "chat_id": to}
            if self.create_topics:
                data.update(thread_id="probe-topic-" + key, root_id=message_id)
            self.sent[key] = SimpleNamespace(success=True, message_id=message_id, chunk_ids=[], raw={"code": 0, "data": data})
        return self.sent[key]

    async def reply(self, message: object, content: object, opts: object = None) -> object:
        self.replies += 1
        message_id = "probe-reply-" + str(self.replies)
        data = {
            "message_id": message_id,
            "chat_id": getattr(message, "chat_id", None),
            "thread_id": getattr(getattr(message, "conversation", None), "thread_id", None),
            "parent_id": getattr(opts, "reply_to", None) or getattr(message, "message_id", None),
        }
        return SimpleNamespace(success=True, message_id=message_id, chunk_ids=[], raw={"code": 0, "data": data})

    async def add_reaction(self, message_id: str, emoji_type: str) -> object:
        return SimpleNamespace(success=True, raw={"data": {"reaction_id": "probe-reaction"}})

    async def remove_reaction(self, message_id: str, reaction_id: str) -> object:
        return SimpleNamespace(success=True)

    async def update_card(self, message_id: str, card: dict[str, Any]) -> object:
        return SimpleNamespace(success=True)


class McpRecorder:
    """Restrict this probe to paused local plans and record identity evidence."""

    def __init__(self, service: ScheduleService) -> None:
        self.service = service
        self.expected_chats: dict[str, str] = {}
        self.expected_bindings: dict[str, str] = {}
        self.expected_target_kinds: dict[str, str] = {}
        self.created: dict[str, str] = {}
        self.create_requests: dict[str, str] = {}
        self.successful_modes: dict[str, set[str]] = {}

    async def manage(self, request: dict[str, Any], thread_id: str | None) -> dict[str, Any]:
        if thread_id not in self.expected_chats:
            return {"ok": False, "error": {"code": "probe_identity_missing", "message": "Native probe identity was not recognized."}}
        mode = request.get("mode")
        if (mode == "create" and request.get("enabled") is not False) or request.get("enabled") is True:
            return {"ok": False, "error": {"code": "probe_paused_only", "message": "This probe only permits paused plans."}}
        if any(key in request for key in ("chat_id", "project", "target_binding_id")) or request.get("all"):
            return {"ok": False, "error": {"code": "probe_defaults_required", "message": "Use this native Thread's default group, Project and Binding."}}
        target_kind = self.expected_target_kinds.get(thread_id)
        if mode == "create" and target_kind is not None and request.get("target_kind", "new_topic") != target_kind:
            return {"ok": False, "error": {"code": "probe_target_required", "message": "Use the requested target_kind: " + target_kind}}
        if mode == "create" and thread_id in self.created and request.get("request_id") != self.create_requests[thread_id]:
            return {"ok": False, "error": {"code": "probe_one_plan_only", "message": "This probe permits one paused plan per Thread."}}
        if mode in {"view", "update", "delete", "runs"} and request.get("plan_id") != self.created.get(thread_id):
            return {"ok": False, "error": {"code": "probe_owned_plan_only", "message": "Use the exact plan created by this probe Thread."}}
        result = await self.service.manage(request, native_thread_id=thread_id)
        if result.get("ok"):
            self.successful_modes.setdefault(thread_id, set()).add(mode)
            if mode == "create":
                self.created[thread_id] = result["plan"]["id"]
                self.create_requests[thread_id] = request["request_id"]
        return result


class ManualMcpRecorder:
    """Permit only lookup and one exact manual request on the owned fixture."""

    def __init__(self, service: ScheduleService, plan_id: str) -> None:
        self.service = service
        self.plan_id = plan_id
        self.thread_id: str | None = None
        self.request_id: str | None = None
        self.successful_modes: set[str] = set()
        self.receipts: list[dict[str, Any]] = []

    async def manage(self, request: dict[str, Any], thread_id: str | None) -> dict[str, Any]:
        if self.thread_id is None or thread_id != self.thread_id:
            return {"ok": False, "error": {"code": "probe_identity_missing", "message": "Use the owned probe Thread."}}
        mode = request.get("mode")
        if mode not in {"list", "view", "runs", "run_now"}:
            return {"ok": False, "error": {"code": "probe_manual_only", "message": "Only look up and manually run the existing paused plan."}}
        if "chat_id" in request or "project" in request or request.get("all"):
            return {"ok": False, "error": {"code": "probe_defaults_required", "message": "Use the current conversation's defaults."}}
        if mode != "list" and request.get("plan_id") != self.plan_id:
            return {"ok": False, "error": {"code": "probe_owned_plan_only", "message": "Use the exact plan found by this probe Thread."}}
        if mode == "run_now":
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                return {"ok": False, "error": {"code": "probe_request_id_required", "message": "Supply a stable request_id."}}
            if self.request_id is not None and request_id != self.request_id:
                return {"ok": False, "error": {"code": "probe_one_run_only", "message": "Retry the original request_id; do not start another run."}}
            self.request_id = request_id
        result = await self.service.manage(request, native_thread_id=thread_id)
        if result.get("ok"):
            self.successful_modes.add(mode)
            if mode == "run_now":
                self.receipts.append(result)
        return result


def _user_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"


def _read_config(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text()) if path.exists() else {}


def _remove_fixture_trust(path: Path, before: dict[str, Any], paths: set[str]) -> bool:
    """Remove only native-added fixture trust assignments, without TOML rewriting.

    Refuse unexpected fields, ambiguous source ranges, or replacement of the
    file while preparing the edit. Existing Project entries are never removed.
    """
    if not path.exists():
        return False
    with path.open("r+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = handle.read()
        current = tomllib.loads(original.decode())
        targets = paths & (current.get("projects", {}).keys() - before.get("projects", {}).keys())
        if not targets:
            return False
        _require(all(current["projects"][key] == {"trust_level": "trusted"} for key in targets), "fixture_project_config_unexpected")
        lines = original.decode().splitlines(keepends=True)
        headers = []
        for index, line in enumerate(lines):
            if not line.lstrip().startswith("["):
                continue
            try:
                parsed = tomllib.loads(line)
            except tomllib.TOMLDecodeError:
                continue
            headers.append((index, parsed))
        remove = set()
        matched = set()
        for offset, (index, parsed) in enumerate(headers):
            project = parsed.get("projects", {})
            if len(project) != 1 or not (set(project) & targets):
                continue
            key = next(iter(project))
            end = headers[offset + 1][0] if offset + 1 < len(headers) else len(lines)
            block = tomllib.loads("".join(lines[index:end]))
            _require(block == {"projects": {key: {"trust_level": "trusted"}}}, "fixture_project_source_ambiguous")
            remove.add(index)
            remove.update(number for number in range(index + 1, end) if lines[number].strip() and not lines[number].lstrip().startswith("#"))
            matched.add(key)
        _require(matched == targets, "fixture_project_source_not_found")
        updated = "".join(line for index, line in enumerate(lines) if index not in remove).encode()
        expected = copy.deepcopy(current)
        for key in targets:
            del expected["projects"][key]
        actual = tomllib.loads(updated.decode())
        if not expected.get("projects"):
            expected.pop("projects", None)
        if not actual.get("projects"):
            actual.pop("projects", None)
        _require(actual == expected, "fixture_cleanup_would_change_other_config")
        handle.seek(0)
        stat = path.stat()
        owned_stat = os.fstat(handle.fileno())
        _require((stat.st_dev, stat.st_ino) == (owned_stat.st_dev, owned_stat.st_ino) and handle.read() == original, "config_changed_during_fixture_cleanup")
        handle.seek(0)
        handle.write(updated)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _config(cwd: Path, model: str | None, runner: ScheduleMcpRunner | None = None) -> CodexConfig:
    overrides = ['allow_login_shell=false', 'sandbox_mode="read-only"']
    if model:
        overrides.append("model=" + json.dumps(model))
    if runner is not None:
        overrides.extend(runner.config_overrides)
    return CodexConfig(cwd=str(cwd), config_overrides=tuple(overrides),
                       env={**os.environ, **(runner.app_server_env if runner else {})})


async def _run_text(thread: Any, prompt: str) -> None:
    result = await thread.run(prompt)
    status = getattr(result.status, "value", result.status)
    _require(status == "completed", "native_turn_not_completed")


async def _wait_for_exact_active(thread: AsyncThread, turn_id: str, *, timeout: float = 30) -> None:
    """Read native readiness; a started handle alone is not interrupt evidence."""
    try:
        async with asyncio.timeout(timeout):
            while True:
                snapshot = await thread.read(include_turns=True)
                _require(snapshot.thread.id == thread.id, "stop_fixture_thread_identity_changed")
                exact = [turn for turn in snapshot.thread.turns if turn.id == turn_id]
                _require(len(exact) <= 1, "stop_fixture_turn_identity_conflict")
                status = _status_value(exact[0]) if exact else None
                if status in {"completed", "interrupted", "failed"}:
                    raise ProbeFailure("stop_fixture_terminal_before_active_observation")
                if status == "inProgress" and _thread_status_type(snapshot.thread) == "active":
                    return
                # This is bounded read polling, never a guessed startup delay.
                await asyncio.sleep(0.1)
    except TimeoutError as error:
        raise ProbeFailure("stop_fixture_active_observation_timeout") from error


async def _record_completion(application: Any, outcomes: asyncio.Queue, outcome: object) -> None:
    try:
        await application.handle_completion(outcome)
    except Exception as error:
        failure = ProbeCompletionFailure("completion_callback_failed", {"callback_error_type": type(error).__name__})
        failure.__cause__ = error
        outcomes.put_nowait(failure)
        raise
    if isinstance(outcome, TurnOutcome):
        outcomes.put_nowait(outcome)


async def _wait_for_completion(
    outcomes: asyncio.Queue, *, codex: AsyncCodex, runtime: CodexRuntime,
    store: BindingStore, binding_id: str, run_id: str, turn_id: str, stage: str,
) -> TurnOutcome:
    _require(stage in {"initial", "followup", "stop"}, "invalid_completion_stage")
    try:
        outcome = await asyncio.wait_for(outcomes.get(), 90)
    except TimeoutError as error:
        # Diagnose once without resuming, consuming a stream, changing a Run,
        # or extending the scenario's completion deadline.
        evidence: dict[str, Any] = {"stage": stage}
        active = runtime.active_turn(binding_id)
        state = getattr(getattr(active, "state", None), "value", None)
        evidence["runtime_state"] = state if state in {"running", "stopping", "turn-observation-unavailable"} else "unavailable"
        try:
            run = store.schedules.get_run(run_id)
            evidence.update(run_phase=run.phase, barrier=run.barrier, delivery_state=run.delivery_state)
            binding = store.get(binding_id)
            _require(binding.native_thread_id is not None, "completion_native_reference_missing")
            view = await asyncio.wait_for(AsyncThread(codex, binding.native_thread_id).read(include_turns=True), 3)
            _require(view.thread.id == binding.native_thread_id, "completion_read_identity_mismatch")
            state = _thread_status_type(view.thread)
            evidence["native_thread_state"] = state if state in {"notLoaded", "idle", "active", "systemError"} else "unavailable"
            exact = [turn for turn in view.thread.turns if turn.id == turn_id]
            state = _status_value(exact[0]) if len(exact) == 1 else None
            evidence["native_turn_state"] = state if state in {"inProgress", "completed", "interrupted", "failed"} else "unavailable"
        except Exception as read_error:
            evidence["read_error_type"] = type(read_error).__name__
        raise ProbeCompletionFailure("completion_timeout", evidence) from error
    if isinstance(outcome, ProbeFailure):
        raise outcome
    return outcome


async def _mcp_phase(codex: AsyncCodex, cwd: Path, store: BindingStore, recorder: McpRecorder) -> dict[str, bool]:
    threads = []
    for suffix, target_kind in (("a", "new_topic"), ("b", "binding")):
        thread = await codex.thread_start(cwd=str(cwd), ephemeral=True)
        threads.append(thread)
        chat = "probe-chat-" + suffix
        binding = store.create_channel_binding(scope=FeishuScope("probe", chat, ScopeKind.GROUP), project_alias="probe", creator_id="probe")
        store.assign_native_thread_id(binding.id, thread.id)
        recorder.expected_chats[thread.id] = chat
        recorder.expected_bindings[thread.id] = binding.id
        recorder.expected_target_kinds[thread.id] = target_kind
    _require(threads[0].id != threads[1].id, "native_threads_not_independent")
    nonce = uuid.uuid4().hex
    await asyncio.gather(*(
        _run_text(thread, f"请在当前群创建名为 schedule-probe-{nonce}-{index} 的定时计划：每天 UTC 09:00，仅回复 SCHEDULE-PROBE。"
                  + ("每次在新建的独立话题执行。" if index == 0 else "每次在当前会话继续执行，沿用本会话的上下文，使用原会话模式。")
                  + "必须保持暂停 enabled=false。使用 cron_manage；不要显式提供 target_binding_id、chat_id 或 project，使用当前会话默认值。只做这件事，不调用其他工具，不运行命令，不修改文件，简短回复结果。")
        for index, thread in enumerate(threads)
    ))
    _require(len(recorder.created) == 2, "two_paused_plans_not_created")
    for thread in threads:
        plan = store.schedules.get(recorder.created[thread.id])
        _require(plan.chat_id == recorder.expected_chats[thread.id] and plan.project_alias == "probe" and not plan.enabled, "native_default_identity_mismatch")
        _require(plan.target_kind == recorder.expected_target_kinds[thread.id], "mcp_target_kind_mismatch")
        if plan.target_kind == "binding":
            _require(plan.target_binding_id == recorder.expected_bindings[thread.id]
                     and plan.session_settings is None, "mcp_default_binding_identity_mismatch")
        else:
            _require(plan.target_binding_id is None and plan.session_settings is not None,
                     "mcp_new_topic_target_changed")
    await asyncio.gather(*(
        _run_text(thread, "请先列出当前群的定时计划，再查看刚创建的计划详情，然后把该计划执行指令修改为：仅回复 SCHEDULE-PROBE-UPDATED。保持暂停，不修改群或Project。使用 cron_manage 的 list、view、update，严格采用实际返回的ID和revision。不要显式填chat_id或project，不调用其他工具、不修改文件。")
        for thread in threads
    ))
    for thread in threads:
        plan = store.schedules.get(recorder.created[thread.id])
        _require(plan.revision >= 2 and not plan.enabled and "SCHEDULE-PROBE-UPDATED" in plan.instructions, "plan_update_not_persisted")
        _require(plan.target_kind == recorder.expected_target_kinds[thread.id]
                 and (plan.target_kind != "binding" or (
                     plan.target_binding_id == recorder.expected_bindings[thread.id]
                     and plan.session_settings is None)), "mcp_update_changed_target_identity")
    await asyncio.gather(*(
        _run_text(thread, "请删除刚才修改过的定时计划。使用 cron_manage 的 delete，使用实际plan_id、当前expected_revision和稳定request_id。无需另行确认；保留普通会话。不要操作其他计划或使用其他工具。")
        for thread in threads
    ))
    for thread in threads:
        plan = store.schedules.get(recorder.created[thread.id], include_deleted=True)
        _require(plan.deleted and not plan.instructions, "plan_not_tombstoned")
        _require({"create", "list", "view", "update", "delete"} <= recorder.successful_modes[thread.id], "management_mode_not_observed")
        _require(not store.schedules.list_runs(plan.id), "mcp_probe_created_a_run")
    return {"passed": True, "two_ephemeral_threads": True, "same_cwd_distinct_default_groups": True,
            "natural_language_crud": True, "natural_language_new_topic_crud": True,
            "natural_language_binding_crud": True, "native_identity_default_binding": True,
            "binding_without_independent_settings": True,
            "only_paused_plans": True, "scheduler_never_started": True}


def _cleanup_error(error: Exception, *, operation: str, thread_id: str | None = None) -> None:
    evidence: dict[str, Any] = {"operation": operation, "error_type": type(error).__name__}
    if thread_id is not None:
        evidence["thread_id"] = thread_id
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        if isinstance(current, JsonRpcError):
            evidence.update(
                rpc_code=current.code,
                fork_history_referenced="forked history still references it" in current.message.lower(),
                active_writer="already has an active writer" in current.message.lower(),
            )
            break
        current = current.__cause__ or current.__context__
    print(json.dumps({"probe_cleanup_failure": evidence}), file=sys.stderr, flush=True)


async def _close_probe_client(codex: AsyncCodex | None, runner: ScheduleMcpRunner | None,
                              owned: _DeleteOnce | None, *, delete_threads: bool = True,
                              delete_order: tuple[str, ...] = ()) -> bool:
    """Bound cleanup of only this probe's client, listener and native IDs."""
    cleanup_ok = True
    if runner:
        runner.close_admission()
    if owned is not None and delete_threads:
        for thread_id in dict.fromkeys((*delete_order, *sorted(owned.owned - owned.attempted))):
            if thread_id not in owned.owned or thread_id in owned.attempted:
                continue
            operation = "delete"
            try:
                await asyncio.wait_for(owned.delete(thread_id), 15)
                operation = "confirm_absent"
                await asyncio.wait_for(_prove_thread_absent_from_all_catalogs(codex, thread_id), 15)
            except Exception as error:
                _cleanup_error(error, operation=operation, thread_id=thread_id)
                cleanup_ok = False
    if codex is not None:
        try:
            # Ephemeral Threads are scoped to this dedicated App Server.
            await asyncio.wait_for(codex.__aexit__(None, None, None), 10)
        except Exception as error:
            _cleanup_error(error, operation="client_close")
            cleanup_ok = False
    if runner:
        try:
            await runner.close()
        except Exception as error:
            _cleanup_error(error, operation="listener_close")
            cleanup_ok = False
    return cleanup_ok


async def _mcp_recovery_phase(cwd: Path, model: str, store: BindingStore) -> dict[str, bool]:
    """Rotate process MCP state, cold-resume an exact history, then fork it."""
    recorder = McpRecorder(ScheduleService(
        bindings=store, runtime=None, app_id="probe", chat_info=FakeFeishu(), default_timezone="UTC",
    ))
    native_ids: set[str] = set()
    attempted: set[str] = set()
    delete_order: tuple[str, ...] = ()
    previous_endpoint = None
    parent_id = None
    parent_plan = None
    for generation in range(2):
        runner = ScheduleMcpRunner()
        codex = None
        owned = None
        preserve_for_resume = False
        try:
            runner.attach(recorder.manage)
            await runner.bind()
            runner.open_admission()
            endpoint = (runner.namespace, runner.url, next(iter(runner.app_server_env.values())))
            if previous_endpoint is not None:
                _require(all(old != new for old, new in zip(previous_endpoint, endpoint)), "recovery_endpoint_not_rotated")
            previous_endpoint = endpoint
            codex = AsyncCodex(_config(cwd, model, runner))
            await codex.__aenter__()
            owned = _DeleteOnce(codex)
            owned.owned = native_ids
            owned.attempted = attempted
            if generation == 0:
                thread = await codex.thread_start(cwd=str(cwd), ephemeral=False)
                parent_id = thread.id
                native_ids.add(parent_id)
                delete_order = (parent_id,)
                binding = store.create_channel_binding(
                    scope=FeishuScope("probe", "probe-recovery-parent", ScopeKind.GROUP),
                    project_alias="probe", creator_id="probe",
                )
                store.assign_native_thread_id(binding.id, parent_id)
                recorder.expected_chats[parent_id] = "probe-recovery-parent"
                await _run_text(thread,
                    "请使用 cron_manage 在当前群创建一个名为 RECOVERY-PROBE 的定时计划，每天UTC 09:00，"
                    "执行指令为仅回复 RECOVERY-PROBE-INITIAL。必须暂停 enabled=false，不显式传chat_id/project，"
                    "使用当前会话默认值。不调用其他工具、不运行命令、不修改文件。")
                _require(parent_id in recorder.created, "recovery_parent_plan_not_created")
                parent_plan = store.schedules.get(recorder.created[parent_id])
                _require(not parent_plan.enabled and parent_plan.chat_id == "probe-recovery-parent", "recovery_parent_default_mismatch")
                _require((await thread.read()).thread.ephemeral is False, "recovery_parent_not_persistent")
                preserve_for_resume = True
            else:
                thread = await codex.thread_resume(
                    parent_id, cwd=str(cwd), include_turns=False
                )
                _require(thread.id == parent_id, "recovery_resume_identity_mismatch")
                recorder.successful_modes[parent_id].clear()
                await _run_text(thread,
                    "请列出当前群的定时计划，查看 RECOVERY-PROBE 的最新详情，然后将它的执行指令更新为"
                    "仅回复 RECOVERY-PROBE-RESUMED。使用 cron_manage 和实际plan_id/revision/request_id，"
                    "保持暂停，不显式填chat_id/project。不调用其他工具、不运行命令、不修改文件。")
                updated = store.schedules.get(parent_plan.id)
                _require({"list", "view", "update"} <= recorder.successful_modes[parent_id], "recovery_management_not_observed")
                _require(not updated.enabled and updated.revision > parent_plan.revision
                         and updated.chat_id == "probe-recovery-parent"
                         and "RECOVERY-PROBE-RESUMED" in updated.instructions, "recovery_update_not_persisted")
                fork = await codex.thread_fork(
                    parent_id, cwd=str(cwd), ephemeral=False, include_turns=False
                )
                _require(fork.id != parent_id, "recovery_fork_identity_not_independent")
                native_ids.add(fork.id)
                # A persisted fork references its parent's paginated history.
                # Remove this known fixture fork before its parent; never retry
                # an uncertain parent delete to work around native protection.
                delete_order = (fork.id, parent_id)
                binding = store.create_channel_binding(
                    scope=FeishuScope("probe", "probe-recovery-fork", ScopeKind.GROUP),
                    project_alias="probe", creator_id="probe",
                )
                store.assign_native_thread_id(binding.id, fork.id)
                recorder.expected_chats[fork.id] = "probe-recovery-fork"
                await _run_text(fork,
                    "请在本会话当前群创建一个新的、独立的 RECOVERY-FORK-PROBE 定时计划，每天UTC 10:00，"
                    "指令为仅回复 RECOVERY-FORK-PROBE。必须暂停 enabled=false，使用cron_manage，"
                    "省略chat_id/project以采用本会话默认值；不要更改已有计划。不调用其他工具、不运行命令、不修改文件。")
                _require(fork.id in recorder.created, "recovery_fork_plan_not_created")
                fork_plan = store.schedules.get(recorder.created[fork.id])
                _require(fork_plan.id != parent_plan.id and not fork_plan.enabled
                         and fork_plan.chat_id == "probe-recovery-fork" and fork_plan.project_alias == "probe",
                         "recovery_fork_default_identity_mismatch")
                _require(store.schedules.get(parent_plan.id) == updated, "recovery_fork_changed_parent_plan")
                for plan in (updated, fork_plan):
                    _require(not store.schedules.list_runs(plan.id), "recovery_probe_created_a_run")
        finally:
            cleanup_ok = await _close_probe_client(
                codex, runner, owned, delete_threads=not preserve_for_resume,
                delete_order=delete_order,
            )
            if native_ids - attempted and (not preserve_for_resume or not cleanup_ok):
                # A failed first shutdown cannot proceed to resume; a second
                # startup may fail before acquiring its delete controller.
                # Clean only exact unattempted owned IDs in either case.
                cleanup_client = AsyncCodex(_config(cwd, model))
                cleanup_owned = None
                try:
                    await asyncio.wait_for(cleanup_client.__aenter__(), 10)
                    cleanup_owned = _DeleteOnce(cleanup_client)
                    cleanup_owned.owned = native_ids
                    cleanup_owned.attempted = attempted
                finally:
                    cleanup_ok = await _close_probe_client(
                        cleanup_client, None, cleanup_owned, delete_order=delete_order,
                    ) and cleanup_ok
            _require(cleanup_ok, "owned_native_cleanup_not_confirmed")
    return {"passed": True, "cold_resume_exact_identity": True, "same_store_retained": True,
            "namespace_url_token_rotated": True, "cold_resume_list_update": True,
            "fork_default_group_identity": True, "only_paused_plans": True,
            "scheduler_never_started": True, "owned_native_threads_deleted": True,
            "deferred_tool_search_tested": False, "compacted_thread_tested": False}


async def _dispatch_phase(codex: AsyncCodex, cwd: Path, store: BindingStore, owned: _DeleteOnce) -> dict[str, bool]:
    projects = ProjectRegistry(store=store, project_root=cwd.parent, projects={"probe": cwd})
    channel = FakeFeishu()
    runtime = CodexRuntime(codex=codex, bindings=store, terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                           thread_subscription_control=AppServerThreadSubscriptionControl(codex), thread_delete_control=owned)
    management = InstanceManagementService(bindings=store, projects=projects, runtime=ManagementRuntimePort(runtime), scope_coordinator=ScopeCoordinator())
    application = ChannelApplication(app_id="probe", channel=channel, runtime=runtime, bindings=store, projects=projects, management=management)
    outcomes: asyncio.Queue[TurnOutcome | ProbeCompletionFailure] = asyncio.Queue()

    async def completed(outcome: object) -> None:
        await _record_completion(application, outcomes, outcome)

    runtime.set_completion_handler(completed)
    clock = [time.time()]
    scheduler = Scheduler(store, runtime, "probe", application.dispatch_scheduled_run, lambda: clock[0])
    source = store.create_channel_binding(scope=FeishuScope("probe", "probe-chat", ScopeKind.GROUP), project_alias="probe", creator_id="probe")
    topic_binding = None
    try:
        await scheduler.recover()
        # The fake clock makes a single interval due immediately. The timer is
        # closed as soon as it claims; no future occurrence is ever launched.
        plan_id = store.schedules.create(name="Dispatch probe", instructions="仅回复 SCHEDULE-PROBE-INITIAL；不调用工具，不运行命令，不修改文件。",
            project_alias="probe", app_id="probe", chat_id="probe-chat", request_id=str(uuid.uuid4()),
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=clock[0]), now=clock[0]).plan_id
        scheduler.start()
        clock[0] += 60
        _require(await scheduler.tick() == 1, "scheduled_occurrence_not_claimed")
        await scheduler.close()
        _require(await scheduler.drain(asyncio.get_running_loop().time() + 45), "dispatch_did_not_drain")
        run = store.schedules.list_runs(plan_id)[0]
        _require(run.binding_id is not None and run.initial_turn_id is not None, "scheduled_native_reference_missing")
        topic_binding = store.get(run.binding_id)
        _require(topic_binding.native_thread_id is not None, "scheduled_native_thread_missing")
        owned.owned.add(topic_binding.native_thread_id)
        first = await _wait_for_completion(outcomes, codex=codex, runtime=runtime, store=store,
            binding_id=topic_binding.id, run_id=run.id, turn_id=run.initial_turn_id, stage="initial")
        _require(first.status == "completed" and first.error is None and first.turn_id == run.initial_turn_id, "initial_turn_not_completed")
        _require(first.final_response is not None and "SCHEDULE-PROBE-INITIAL" in first.final_response, "initial_result_mismatch")
        run = store.schedules.get_run(run.id)
        _require(run.barrier == "released" and run.delivery_state == "sent", "initial_barrier_or_delivery_not_completed")
        observed = await runtime.read_scheduled_turn(
            topic_binding.id, run.initial_turn_id,
            deadline=asyncio.get_running_loop().time() + 10,
        )
        _require(observed == "completed", "exact_initial_read_not_completed")
        _require(store.active_binding(source.scope_key).id == source.id, "source_active_binding_changed")
        scope = store.get_scope(topic_binding.scope_key)
        _require(scope.kind is ScopeKind.TOPIC and scope.topic_id == run.topic_id and scope.chat_id == "probe-chat", "fresh_topic_identity_mismatch")
        view = await AsyncThread(codex, topic_binding.native_thread_id).read(include_turns=True)
        _require(view.thread.ephemeral is False, "scheduled_thread_not_persistent")
        origin = SimpleNamespace(id="probe-followup", message_id="probe-followup", chat_id="probe-chat", conversation=SimpleNamespace(thread_id=run.topic_id))
        followup = await runtime.submit(binding=store.get(topic_binding.id), cwd=cwd, input="仅回复 SCHEDULE-PROBE-FOLLOWUP；不调用工具或修改文件。", owner_id="probe", origin=origin)
        followup.release_receipt_attempt()
        second = await _wait_for_completion(outcomes, codex=codex, runtime=runtime, store=store,
            binding_id=topic_binding.id, run_id=run.id, turn_id=followup.turn_id, stage="followup")
        _require(second.status == "completed" and second.thread_id == topic_binding.native_thread_id and second.turn_id != run.initial_turn_id, "ordinary_followup_failed")
        _require(store.schedules.get_run(run.id).barrier == "released", "followup_reopened_initial_barrier")
        stopped = await runtime.submit(binding=store.get(topic_binding.id), cwd=cwd, input="请逐项列出从1到1000的整数，只输出文本，不调用工具、不修改文件。", owner_id="probe", origin=origin)
        stopped.release_receipt_attempt()
        await _wait_for_exact_active(AsyncThread(codex, topic_binding.native_thread_id), stopped.turn_id)
        try:
            disposition = await runtime.stop_exact(topic_binding.id, expected_turn_id=stopped.turn_id)
        except Exception as error:
            # This exact fixture owns the Thread and attempted stop. Only expose
            # the public RPC's numeric code and a fixed classification boolean.
            raise ProbeStopFailure(error) from error
        _require(disposition in {StopDisposition.REQUESTED, StopDisposition.STOPPING}, "ordinary_stop_not_requested")
        stop_outcome = await _wait_for_completion(outcomes, codex=codex, runtime=runtime, store=store,
            binding_id=topic_binding.id, run_id=run.id, turn_id=stopped.turn_id, stage="stop")
        _require(stop_outcome.status == "interrupted" and stop_outcome.turn_id == stopped.turn_id, "ordinary_stop_not_observed")
        await runtime.archive_exact(topic_binding.id)
        await runtime.delete_archived_exact(topic_binding.id, expected_native_thread_id=topic_binding.native_thread_id)
        await asyncio.wait_for(_prove_thread_absent_from_all_catalogs(codex, topic_binding.native_thread_id), 20)
        return {"passed": True, "new_feishu_topic_fixture": True, "source_active_unchanged": True,
                "ordinary_persistent_thread": True, "initial_barrier_released": True, "ordinary_followup": True,
                "exact_initial_read_completed": True, "stop_exact_active_observed": True, "ordinary_stop": True,
                "ordinary_archive_delete": True, "real_feishu_calls": False}
    finally:
        await scheduler.close()
        await scheduler.drain(asyncio.get_running_loop().time() + 3)
        # Register only IDs in this disposable Store, including a Thread that
        # materialized before a later assertion/timeout failed.
        for plan_run in store.schedules.list_runs(plan_id) if "plan_id" in locals() else ():
            if plan_run.binding_id:
                try:
                    native_id = store.get(plan_run.binding_id).native_thread_id
                except BindingNotFound:
                    continue
                if native_id:
                    owned.owned.add(native_id)
        await asyncio.wait_for(runtime.cancel_tasks(), 5)
        await asyncio.wait_for(application.close(), 5)
        await management.close(deadline=asyncio.get_running_loop().time() + 5)


async def _manual_phase(
    codex: AsyncCodex, cwd: Path, store: BindingStore, owned: _DeleteOnce,
    runner: ScheduleMcpRunner,
) -> dict[str, bool]:
    projects = ProjectRegistry(store=store, project_root=cwd.parent, projects={"probe": cwd})
    channel = FakeFeishu()
    runtime = CodexRuntime(codex=codex, bindings=store, terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                           thread_subscription_control=AppServerThreadSubscriptionControl(codex), thread_delete_control=owned)
    management = InstanceManagementService(bindings=store, projects=projects, runtime=ManagementRuntimePort(runtime), scope_coordinator=ScopeCoordinator())
    application = ChannelApplication(app_id="probe", channel=channel, runtime=runtime, bindings=store, projects=projects, management=management)
    outcomes: asyncio.Queue[TurnOutcome | ProbeCompletionFailure] = asyncio.Queue()

    async def completed(outcome: object) -> None:
        await _record_completion(application, outcomes, outcome)

    runtime.set_completion_handler(completed)
    clock = [time.time()]
    scheduler = Scheduler(store, runtime, "probe", application.dispatch_scheduled_run, lambda: clock[0])
    service = ScheduleService(bindings=store, runtime=runtime, app_id="probe", chat_info=channel,
                              wall_clock=lambda: clock[0], default_timezone="UTC")
    service.set_run_now_handler(scheduler.run_now)
    service.set_refresh_handler(scheduler.refresh)
    plan_id = None
    try:
        await scheduler.recover()
        name = "MANUAL-PROBE-" + uuid.uuid4().hex
        plan_id = store.schedules.create(
            name=name, instructions="仅回复 SCHEDULE-PROBE-MANUAL；不调用工具，不运行命令，不修改文件。",
            project_alias="probe", app_id="probe", chat_id="probe-manual-chat", enabled=False,
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=clock[0]),
            request_id=str(uuid.uuid4()), now=clock[0],
        ).plan_id
        saved = store.schedules.get(plan_id)
        recorder = ManualMcpRecorder(service, plan_id)
        runner.attach(recorder.manage)
        runner.open_admission()
        thread = await codex.thread_start(cwd=str(cwd), ephemeral=True)
        recorder.thread_id = thread.id
        source = store.create_channel_binding(
            scope=FeishuScope("probe", "probe-manual-chat", ScopeKind.GROUP), project_alias="probe", creator_id="probe",
        )
        store.assign_native_thread_id(source.id, thread.id)
        scheduler.start()
        await _run_text(thread,
            f"请把「{name}」定时计划现在跑一次。保持暂停，原定时安排不要变。"
            "只使用定时任务管理工具，不调用其他工具、不运行命令、不修改文件。简短告知受理状态和结果投递位置。")
        _require({"list", "run_now"} <= recorder.successful_modes, "manual_management_not_observed")
        _require(bool(recorder.receipts) and all(receipt.get("accepted") is True for receipt in recorder.receipts), "manual_receipt_not_accepted")
        clock[0] += 60
        _require(await scheduler.tick() == 0, "paused_manual_plan_claimed_by_timer")
        await scheduler.close()
        _require(await scheduler.drain(asyncio.get_running_loop().time() + 45), "manual_dispatch_did_not_drain")
        runs = store.schedules.list_runs(plan_id)
        _require(len(runs) == 1 and runs[0].trigger_source == "manual", "manual_run_not_unique")
        run = runs[0]
        _require(all(receipt.get("run_id") == run.id for receipt in recorder.receipts), "manual_receipt_run_mismatch")
        _require(run.plan_revision == saved.revision and run.project_alias == saved.project_alias
                 and run.chat_id == saved.chat_id, "manual_snapshot_identity_mismatch")
        _require(run.binding_id is not None and run.initial_turn_id is not None, "manual_native_reference_missing")
        binding = store.get(run.binding_id)
        _require(binding.native_thread_id is not None and binding.native_thread_id != thread.id, "manual_native_thread_not_independent")
        owned.owned.add(binding.native_thread_id)
        _require(SessionSettings.from_binding(binding) == saved.session_settings, "manual_saved_settings_mismatch")
        first = await _wait_for_completion(outcomes, codex=codex, runtime=runtime, store=store,
            binding_id=binding.id, run_id=run.id, turn_id=run.initial_turn_id, stage="initial")
        _require(first.status == "completed" and first.error is None and first.turn_id == run.initial_turn_id
                 and first.final_response is not None and "SCHEDULE-PROBE-MANUAL" in first.final_response, "manual_initial_result_mismatch")
        run = store.schedules.get_run(run.id)
        _require(run.barrier == "released" and run.delivery_state == "sent", "manual_barrier_or_delivery_not_completed")
        _require(store.schedules.get(plan_id) == saved and not saved.enabled, "manual_changed_saved_plan_or_cursors")
        _require(store.active_binding(source.scope_key).id == source.id, "manual_changed_source_binding")
        scope = store.get_scope(binding.scope_key)
        _require(scope.kind is ScopeKind.TOPIC and scope.topic_id == run.topic_id and scope.chat_id == saved.chat_id,
                 "manual_topic_identity_mismatch")
        view = await AsyncThread(codex, binding.native_thread_id).read(include_turns=True)
        _require(view.thread.ephemeral is False, "manual_result_thread_not_persistent")
        _require(any(turn.id == run.initial_turn_id and _status_value(turn) == "completed" for turn in view.thread.turns),
                 "manual_exact_initial_read_not_completed")
        return {"passed": True, "natural_language_run_now": True, "one_manual_run": True,
                "paused_plan_and_cursors_unchanged": True, "saved_instructions_and_settings": True,
                "source_active_unchanged": True, "independent_persistent_result_thread": True,
                "initial_barrier_released": True, "exact_initial_read_completed": True, "real_feishu_calls": False}
    finally:
        service.close_admission()
        runner.close_admission()
        await scheduler.close()
        await scheduler.drain(asyncio.get_running_loop().time() + 3)
        for run in store.schedules.list_runs(plan_id) if plan_id is not None else ():
            if run.binding_id:
                try:
                    native_id = store.get(run.binding_id).native_thread_id
                except BindingNotFound:
                    continue
                if native_id:
                    owned.owned.add(native_id)
        await asyncio.wait_for(runtime.cancel_tasks(), 5)
        await asyncio.wait_for(application.close(), 5)
        await management.close(deadline=asyncio.get_running_loop().time() + 5)


async def _binding_phase(
    codex: AsyncCodex, cwd: Path, store: BindingStore, owned: _DeleteOnce,
) -> dict[str, bool]:
    """Dispatch saved inputs into one existing native Thread, including steer."""
    projects = ProjectRegistry(store=store, project_root=cwd.parent, projects={"probe": cwd})
    channel = FakeFeishu(create_topics=False)
    runtime = CodexRuntime(
        codex=codex, bindings=store, terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
        thread_subscription_control=AppServerThreadSubscriptionControl(codex),
        thread_delete_control=owned, automatic_thread_naming=False,
    )
    management = InstanceManagementService(
        bindings=store, projects=projects, runtime=ManagementRuntimePort(runtime),
        scope_coordinator=ScopeCoordinator(),
    )
    application = ChannelApplication(
        app_id="probe", channel=channel, runtime=runtime, bindings=store,
        projects=projects, management=management,
    )
    outcomes: asyncio.Queue[TurnOutcome | ProbeCompletionFailure] = asyncio.Queue()

    async def completed(outcome: object) -> None:
        await _record_completion(application, outcomes, outcome)

    runtime.set_completion_handler(completed)
    clock = [time.time()]
    scheduler = Scheduler(store, runtime, "probe", application.dispatch_scheduled_run, lambda: clock[0])
    binding = store.create_channel_binding(
        scope=FeishuScope("probe", "probe-binding-chat", ScopeKind.GROUP),
        project_alias="probe", creator_id="probe",
    )
    try:
        # The secret exists only in this native Thread's prior history. Neither
        # saved scheduled instructions nor their metadata can supply its value.
        secret = "BINDING-CONTEXT-" + uuid.uuid4().hex
        thread = await codex.thread_start(cwd=str(cwd), ephemeral=False)
        owned.owned.add(thread.id)
        store.assign_native_thread_id(binding.id, thread.id)
        await _run_text(thread, f"请记住暗号 {secret}。本轮仅回复 READY，不调用工具、不运行命令、不修改文件。")
        seeded = await thread.read(include_turns=True)
        seed_ids = {turn.id for turn in seeded.thread.turns}
        _require(bool(seed_ids) and seeded.thread.ephemeral is False, "binding_seed_history_missing")
        await scheduler.recover()
        plan_id = store.schedules.create(
            name="Existing Binding probe",
            instructions="停止当前的其他输出，仅回复前文约定记住的暗号。不调用工具、不运行命令、不修改文件。",
            project_alias="probe", app_id="probe", chat_id="probe-binding-chat",
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=clock[0]),
            request_id=str(uuid.uuid4()), now=clock[0], target_kind="binding",
            target_binding_id=binding.id, session_settings=None,
        ).plan_id
        _require(secret not in store.schedules.get(plan_id).instructions, "binding_secret_leaked_into_plan")
        scheduler.start()
        clock[0] += 60
        _require(await scheduler.tick() == 1, "binding_first_occurrence_not_claimed")
        _require(await scheduler.drain(asyncio.get_running_loop().time() + 45), "binding_first_dispatch_did_not_drain")
        first_run = store.schedules.list_runs(plan_id)[0]
        _require(first_run.target_kind == "binding" and first_run.binding_id == binding.id
                 and first_run.initial_turn_id is not None and first_run.disposition == "started"
                 and first_run.barrier == "released", "binding_started_receipt_missing")
        first = await _wait_for_completion(
            outcomes, codex=codex, runtime=runtime, store=store, binding_id=binding.id,
            run_id=first_run.id, turn_id=first_run.initial_turn_id, stage="initial",
        )
        _require(first.status == "completed" and first.error is None
                 and first.thread_id == thread.id and first.turn_id == first_run.initial_turn_id,
                 "binding_first_turn_identity_mismatch")
        _require(first.final_response is not None and secret in first.final_response,
                 "binding_prior_context_not_retained")
        _require(getattr(first.origin, "message_id", None) == first_run.origin_message_id,
                 "binding_first_completion_anchor_mismatch")
        origin = SimpleNamespace(
            id="probe-binding-user", message_id="probe-binding-user", chat_id="probe-binding-chat",
            conversation=SimpleNamespace(chat_id="probe-binding-chat", chat_type="group", thread_id=None),
        )
        running = await runtime.submit(
            binding=store.get(binding.id), cwd=cwd,
            input="请逐项列出从1到1000的整数，只输出文本，不调用工具、不运行命令、不修改文件。",
            owner_id="probe-user", origin=origin,
        )
        running.release_receipt_attempt()
        await _wait_for_exact_active(thread, running.turn_id)
        clock[0] += 60
        _require(await scheduler.tick() == 1, "binding_steer_occurrence_not_claimed")
        _require(await scheduler.drain(asyncio.get_running_loop().time() + 45), "binding_steer_dispatch_did_not_drain")
        await scheduler.close()
        runs = store.schedules.list_runs(plan_id)
        _require(len(runs) == 2, "binding_occurrences_not_independent")
        steer_run = next(run for run in runs if run.id != first_run.id)
        _require(steer_run.binding_id == binding.id and steer_run.initial_turn_id == running.turn_id
                 and steer_run.disposition == "steered" and steer_run.barrier == "released",
                 "binding_exact_steer_receipt_missing")
        second = await _wait_for_completion(
            outcomes, codex=codex, runtime=runtime, store=store, binding_id=binding.id,
            run_id=steer_run.id, turn_id=running.turn_id, stage="followup",
        )
        _require(second.status == "completed" and second.error is None
                 and second.thread_id == thread.id and second.turn_id == running.turn_id
                 and second.owner_id == "probe-user" and second.origin is origin,
                 "binding_steer_changed_completion_identity")
        _require(second.final_response is not None and secret in second.final_response,
                 "binding_steer_input_not_consumed")
        view = await thread.read(include_turns=True)
        _require({turn.id for turn in view.thread.turns} == seed_ids | {first.turn_id, running.turn_id},
                 "binding_steer_created_another_turn")
        _require(store.get(binding.id).native_thread_id == thread.id
                 and store.active_binding(binding.scope_key).id == binding.id,
                 "binding_target_identity_changed")
        _require(len(channel.sent) == 2 and all(run.topic_id is None for run in runs),
                 "binding_anchor_transport_mismatch")
        return {
            "passed": True, "existing_persistent_thread": True, "prior_native_context_retained": True,
            "scheduler_channel_ordinary_submit": True, "exact_running_turn_steered": True,
            "steer_consumed_without_extra_turn": True, "original_completion_origin_retained": True,
            "independent_input_receipts": True, "group_main_anchor_fixture": True,
            "real_feishu_calls": False, "natural_language_binding_create_tested": False,
            "catch_up_tested": False,
        }
    finally:
        await scheduler.close()
        await scheduler.drain(asyncio.get_running_loop().time() + 3)
        # Register only this fixture Binding, including partial native startup.
        native_id = store.get(binding.id).native_thread_id
        if native_id:
            owned.owned.add(native_id)
        await asyncio.wait_for(runtime.cancel_tasks(), 5)
        await asyncio.wait_for(application.close(), 5)
        await management.close(deadline=asyncio.get_running_loop().time() + 5)


async def probe(*, phase: str, model: str, timeout: float) -> dict[str, Any]:
    _require(openai_codex.__version__ == "0.155.1", "sdk_pin_mismatch")
    config_path = _user_config_path()
    before = _read_config(config_path)
    result: dict[str, Any] = {"passed": False, "read_only_startup_override": True, "real_feishu_calls": False}
    fixture_paths: set[str] = set()
    try:
        async with asyncio.timeout(timeout):
            with tempfile.TemporaryDirectory(prefix="netizen-schedule-probe-") as temporary:
                for selected in (("mcp", "mcp-recovery", "dispatch", "manual", "binding") if phase == "all" else (phase,)):
                    cwd = (Path(temporary) / selected).resolve()
                    cwd.mkdir()
                    subprocess.run(["git", "-C", str(cwd), "init", "--quiet"], check=True, capture_output=True)
                    fixture_paths.add(str(cwd))
                    store = BindingStore(Path(temporary) / (selected + ".sqlite3"))
                    store.register_project(alias="probe", cwd=str(cwd))
                    runner = ScheduleMcpRunner() if selected in {"mcp", "manual"} else None
                    codex = None
                    owned = None
                    try:
                        if selected == "mcp-recovery":
                            result[selected] = await _mcp_recovery_phase(cwd, model, store)
                            continue
                        if selected == "mcp":
                            recorder = McpRecorder(ScheduleService(bindings=store, runtime=None, app_id="probe", chat_info=FakeFeishu(), default_timezone="UTC"))
                            runner.attach(recorder.manage)
                            await runner.bind()
                            runner.open_admission()
                        elif runner:
                            await runner.bind()
                        codex = AsyncCodex(_config(cwd, model, runner))
                        await codex.__aenter__()
                        owned = _DeleteOnce(codex) if selected in {"dispatch", "manual", "binding"} else None
                        if selected == "manual":
                            result[selected] = await _manual_phase(codex, cwd, store, owned, runner)
                        elif selected == "binding":
                            result[selected] = await _binding_phase(codex, cwd, store, owned)
                        else:
                            result[selected] = await (_mcp_phase(codex, cwd, store, recorder) if selected == "mcp" else _dispatch_phase(codex, cwd, store, owned))
                    finally:
                        cleanup_ok = await _close_probe_client(codex, runner, owned)
                        store.close()
                        _require(cleanup_ok, "owned_native_cleanup_not_confirmed")
    finally:
        result["fixture_trust_cleaned"] = _remove_fixture_trust(config_path, before, fixture_paths)
    after = _read_config(config_path)
    _require(before.get("mcp_servers", {}) == after.get("mcp_servers", {}), "user_mcp_config_changed")
    _require(before.get("projects", {}) == after.get("projects", {}), "user_project_config_changed")
    result.update(passed=True, user_mcp_config_unchanged=True, project_trust_unchanged=True,
                  owned_native_resources_cleaned=True, ephemeral_cleanup_via_app_server=phase in {"mcp", "manual", "all"})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("mcp", "mcp-recovery", "dispatch", "manual", "binding", "all"), default="all")
    parser.add_argument("--model", required=True, help="explicit compatible model override for this test App Server only")
    parser.add_argument("--timeout", type=float, default=300, help="total async scenario deadline in seconds; cleanup is bounded separately")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 10 <= args.timeout <= 1800:
        raise SystemExit("--timeout must be between 10 and 1800 seconds")
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(probe(phase=args.phase, model=args.model, timeout=args.timeout))
    except Exception as error:
        failure = {"passed": False, "phase": args.phase,
                   "error_code": str(error) if isinstance(error, ProbeFailure) else type(error).__name__,
                   "traceback": _safe_traceback(error)}
        if isinstance(error, ProbeStopFailure):
            failure["stop_rpc"] = error.rpc
        if isinstance(error, ProbeCompletionFailure):
            failure["completion"] = error.evidence
        print(json.dumps(failure))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
