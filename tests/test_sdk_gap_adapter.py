from __future__ import annotations

import gc
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import openai_codex
from openai_codex import AsyncCodex, CodexConfig

from netizen.sdk_gap_adapter import (
    AppServerGoalControl,
    AppServerSideBoundaryControl,
    AppServerSkillCatalog,
    AppServerThreadDeleteControl,
    AppServerThreadSubscriptionControl,
    GoalControlError,
    GoalMutationStateUnknown,
    GoalStatus,
    SIDE_THREAD_BOUNDARY,
    SdkGapCapabilityUnavailable,
    SideBoundaryStateUnknown,
    ThreadUnsubscribeStateUnknown,
    ThreadDeleteStateUnknown,
    ThreadUnsubscribeStatus,
    facade_migration_requirements,
)
from netizen.turn_activity import (
    TurnActivityKind,
    TurnActivityNotificationProjection,
    TurnActivityStatus,
)


_FAKE_SERVER = r'''
import json
import sys

log_path = sys.argv[1]
mode = sys.argv[2]
goal_status = "paused" if mode in {"resume", "resume-loss", "clear", "clear-loss", "wrong-goal-thread"} else None
objective = "existing objective"

def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()

def log(message):
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(message, sort_keys=True) + "\n")

def goal(status):
    return {
        "threadId": "thread-other" if mode == "wrong-goal-thread" else "thread-goal",
        "objective": objective,
        "status": status,
        "tokenBudget": None,
        "tokensUsed": 12,
        "timeUsedSeconds": 3,
        "createdAt": 1,
        "updatedAt": 2,
    }

def turn(turn_id, status):
    return {"id": turn_id, "items": [], "status": status}

def notify(method, params):
    send({"method": method, "params": params})

def thread(include_turns=False):
    return {
        "id": "thread-goal",
        "preview": "goal",
        "ephemeral": False,
        "modelProvider": "openai",
        "createdAt": 0,
        "updatedAt": 2,
        "status": {"type": "idle"},
        "cwd": "/tmp/project",
        "path": "/tmp/thread-goal.jsonl",
        "cliVersion": "0.0.0",
        "source": "appServer",
        "turns": [],
        "sessionId": "session-goal",
    }

for line in sys.stdin:
    message = json.loads(line)
    log(message)
    request_id = message.get("id")
    if request_id is None:
        continue
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        send({
            "id": request_id,
            "result": {
                "userAgent": "netizen-gap-contract/1",
                "serverInfo": {"name": "fake", "version": "1"},
            },
        })
    elif method == "skills/list":
        send({
            "id": request_id,
            "result": {
                "data": [{
                    "cwd": "/tmp/project",
                    "errors": [],
                    "skills": [
                        {
                            "name": "code-review",
                            "description": "Review code",
                            "enabled": True,
                            "path": "/tmp/project/.agents/skills/code-review/SKILL.md",
                            "scope": "repo",
                            "interface": {"displayName": "Code Review"},
                        },
                        {
                            "name": "test-triage",
                            "description": "Triage tests",
                            "enabled": True,
                            "path": "/tmp/project/.agents/skills/test-triage/SKILL.md",
                            "scope": "repo",
                        },
                    ],
                }],
            },
        })
    elif method == "thread/read":
        send({"id": request_id, "result": {"thread": thread()}})
    elif method == "thread/goal/get":
        send({
            "id": request_id,
            "result": {"goal": None if goal_status is None else goal(goal_status)},
        })
    elif method == "thread/goal/clear":
        goal_status = None
        notify("thread/goal/cleared", {"threadId": "thread-goal"})
        if mode == "clear-loss":
            sys.exit(0)
        send({"id": request_id, "result": {"cleared": True}})
    elif method == "thread/goal/set":
        if params.get("objective") is not None:
            objective = params["objective"]
        goal_status = params.get("status", goal_status)
        if goal_status == "active":
            notify(
                "thread/goal/updated",
                {"threadId": "thread-goal", "goal": goal("active")},
            )
            first = "turn-resume" if mode == "resume" else "turn-1"
            notify(
                "turn/started",
                {"threadId": "thread-goal", "turn": turn(first, "inProgress")},
            )
            if mode == "goal":
                notify(
                    "item/started",
                    {
                        "threadId": "thread-goal",
                        "turnId": first,
                        "startedAtMs": 1,
                        "item": {
                            "type": "commandExecution",
                            "id": "goal-command-one",
                            "command": "cat /tmp/private.txt",
                            "commandActions": [],
                            "cwd": "/tmp/project",
                            "status": "inProgress",
                        },
                    },
                )
            if mode in {"start-loss", "resume-loss"}:
                sys.exit(0)
            send({"id": request_id, "result": {"goal": goal("active")}})
            if mode == "resume":
                goal_status = "complete"
                notify(
                    "thread/goal/updated",
                    {"threadId": "thread-goal", "goal": goal("complete")},
                )
                notify(
                    "turn/completed",
                    {"threadId": "thread-goal", "turn": turn(first, "completed")},
                )
            elif mode not in {"pause", "pause-loss", "interrupt-loss"}:
                notify(
                    "turn/completed",
                    {"threadId": "thread-goal", "turn": turn("turn-1", "completed")},
                )
                notify(
                    "turn/started",
                    {"threadId": "thread-goal", "turn": turn("turn-2", "inProgress")},
                )
                goal_status = "complete"
                notify(
                    "thread/goal/updated",
                    {"threadId": "thread-goal", "goal": goal("complete")},
                )
                notify(
                    "turn/completed",
                    {"threadId": "thread-goal", "turn": turn("turn-2", "completed")},
                )
        else:
            notify(
                "thread/goal/updated",
                {"threadId": "thread-goal", "goal": goal(goal_status)},
            )
            if mode == "pause-loss":
                sys.exit(0)
            send({"id": request_id, "result": {"goal": goal(goal_status)}})
    elif method == "turn/interrupt":
        if mode == "interrupt-loss":
            sys.exit(0)
        send({"id": request_id, "result": {}})
        if mode == "pause":
            notify(
                "turn/completed",
                {"threadId": "thread-goal", "turn": turn("turn-1", "interrupted")},
            )
    elif method == "thread/delete":
        if mode == "delete-loss":
            sys.exit(0)
        send({"id": request_id, "result": {}})
    elif method == "thread/inject_items":
        if mode == "side-inject-loss":
            sys.exit(0)
        send({"id": request_id, "result": {}})
    elif method == "thread/unsubscribe":
        if mode == "side-unsubscribe-loss":
            sys.exit(0)
        status = {
            "side-not-loaded": "notLoaded",
            "side-not-subscribed": "notSubscribed",
        }.get(mode, "unsubscribed")
        send({"id": request_id, "result": {"status": status}})
    else:
        send({
            "id": request_id,
            "error": {"code": -32601, "message": "unexpected method: " + str(method)},
        })
'''


def _config(log_path: Path, mode: str = "skills") -> CodexConfig:
    return CodexConfig(
        launch_args_override=(
            sys.executable,
            "-u",
            "-c",
            _FAKE_SERVER,
            str(log_path),
            mode,
        ),
        experimental_api=False,
    )


def _messages(log_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]


def _close_probe_pipes(process: object) -> None:
    poll = getattr(process, "poll", None)
    wait = getattr(process, "wait", None)
    if callable(poll) and callable(wait) and poll() is None:
        try:
            wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            wait(timeout=1)
    for name in ("stdout", "stderr"):
        stream = getattr(process, name, None)
        if stream is not None:
            stream.close()


class SdkGapAdapterContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_side_control_exposes_only_fixed_boundary_and_unsubscribe(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path, "side")) as codex:
                process = codex._client._sync._proc
                boundary = AppServerSideBoundaryControl(codex)
                subscription = AppServerThreadSubscriptionControl(codex)
                self.assertFalse(hasattr(boundary, "request"))
                self.assertFalse(hasattr(subscription, "request"))
                await boundary.inject_boundary("thread-side")
                status = await subscription.unsubscribe("thread-side")
            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        self.assertEqual(status, ThreadUnsubscribeStatus.UNSUBSCRIBED)
        inject = next(
            item for item in messages if item.get("method") == "thread/inject_items"
        )
        self.assertEqual(
            inject["params"],
            {
                "threadId": "thread-side",
                "items": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": SIDE_THREAD_BOUNDARY}
                        ],
                    }
                ],
            },
        )
        unsubscribe = next(
            item for item in messages if item.get("method") == "thread/unsubscribe"
        )
        self.assertEqual(unsubscribe["params"], {"threadId": "thread-side"})
        self.assertEqual(
            [item.get("method") for item in messages if "id" in item],
            ["initialize", "thread/inject_items", "thread/unsubscribe"],
        )

    async def test_all_unsubscribe_terminal_statuses_are_success(self) -> None:
        cases = (
            ("side-not-loaded", ThreadUnsubscribeStatus.NOT_LOADED),
            ("side-not-subscribed", ThreadUnsubscribeStatus.NOT_SUBSCRIBED),
            ("side", ThreadUnsubscribeStatus.UNSUBSCRIBED),
        )
        for mode, expected in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                log_path = Path(raw) / "requests.jsonl"
                async with AsyncCodex(_config(log_path, mode)) as codex:
                    process = codex._client._sync._proc
                    status = await AppServerThreadSubscriptionControl(codex).unsubscribe(
                        "thread-side"
                    )
                _close_probe_pipes(process)
                gc.collect()
                self.assertEqual(status, expected)
                self.assertEqual(
                    [
                        item.get("method")
                        for item in _messages(log_path)
                        if item.get("method") == "thread/unsubscribe"
                    ],
                    ["thread/unsubscribe"],
                )

    async def test_side_mutation_response_loss_is_unknown_without_retry(self) -> None:
        cases = (
            ("side-inject-loss", "inject_boundary", SideBoundaryStateUnknown),
            (
                "side-unsubscribe-loss",
                "unsubscribe",
                ThreadUnsubscribeStateUnknown,
            ),
        )
        for mode, operation, expected_error in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                log_path = Path(raw) / "requests.jsonl"
                codex = AsyncCodex(_config(log_path, mode))
                await codex.__aenter__()
                process = codex._client._sync._proc
                try:
                    control = (
                        AppServerSideBoundaryControl(codex)
                        if operation == "inject_boundary"
                        else AppServerThreadSubscriptionControl(codex)
                    )
                    with self.assertRaises(expected_error):
                        await getattr(control, operation)("thread-side")
                finally:
                    with suppress(BrokenPipeError):
                        await codex.close()
                _close_probe_pipes(process)
                gc.collect()
                self.assertEqual(
                    sum(
                        1
                        for item in _messages(log_path)
                        if item.get("method")
                        in {"thread/inject_items", "thread/unsubscribe"}
                    ),
                    1,
                )

    async def test_thread_delete_uses_one_exact_typed_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path, "delete")) as codex:
                process = codex._client._sync._proc
                control = AppServerThreadDeleteControl(codex)
                self.assertFalse(hasattr(control, "request"))
                await control.delete("thread-goal")
            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        request = next(
            item for item in messages if item.get("method") == "thread/delete"
        )
        self.assertEqual(request["params"], {"threadId": "thread-goal"})
        self.assertEqual(
            [item.get("method") for item in messages if "id" in item],
            ["initialize", "thread/delete"],
        )

    async def test_thread_delete_response_loss_is_state_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            codex = AsyncCodex(_config(log_path, "delete-loss"))
            await codex.__aenter__()
            process = codex._client._sync._proc
            try:
                control = AppServerThreadDeleteControl(codex)
                with self.assertRaises(ThreadDeleteStateUnknown):
                    await control.delete("thread-goal")
            finally:
                with suppress(BrokenPipeError):
                    await codex.close()
            _close_probe_pipes(process)
            gc.collect()

    async def test_skills_list_uses_one_exact_typed_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path)) as codex:
                process = codex._client._sync._proc
                catalog = AppServerSkillCatalog(codex)
                self.assertFalse(hasattr(catalog, "request"))
                snapshot = await catalog.list(
                    Path("/tmp/project"),
                    force_reload=True,
                )
            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        canonical = Path("/tmp/project").resolve()
        self.assertEqual(snapshot.cwd, canonical)
        self.assertEqual(
            tuple(skill.name for skill in snapshot.skills),
            ("code-review", "test-triage"),
        )
        request = next(item for item in messages if item.get("method") == "skills/list")
        self.assertEqual(
            request["params"],
            {"cwds": [str(canonical)], "forceReload": True},
        )
        self.assertEqual(
            [item.get("method") for item in messages if "id" in item],
            ["initialize", "skills/list"],
        )

    async def test_goal_start_routes_immediate_multi_turn_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path, "goal")) as codex:
                process = codex._client._sync._proc
                control = AppServerGoalControl(codex)
                self.assertFalse(hasattr(control, "request"))
                handle = await control.start("thread-goal", "ship safely")
                activity: list[TurnActivityNotificationProjection | None] = []
                terminal = await handle.wait_terminal(activity.append)
            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        self.assertEqual(handle.id, "turn-1")
        self.assertEqual(terminal.logical_turn_id, "turn-1")
        self.assertEqual(terminal.final_physical_turn_id, "turn-2")
        self.assertEqual(terminal.turn_status, "completed")
        self.assertEqual(
            [
                (item.turn_id, item.turn_started, item.turn_completed)
                for item in activity
                if item is not None and (item.turn_started or item.turn_completed)
            ],
            [
                ("turn-1", True, False),
                ("turn-1", False, True),
                ("turn-2", True, False),
                ("turn-2", False, True),
            ],
        )
        command = next(
            item.event
            for item in activity
            if item is not None and item.event is not None
        )
        self.assertIs(command.kind, TurnActivityKind.COMMAND)
        self.assertIs(command.status, TurnActivityStatus.IN_PROGRESS)
        self.assertNotIn("private.txt", repr(command))
        methods = [item.get("method") for item in messages if "id" in item]
        self.assertEqual(
            methods,
            [
                "initialize",
                "thread/read",
                "thread/goal/clear",
                "thread/goal/set",
            ],
        )
        set_request = next(
            item for item in messages if item.get("method") == "thread/goal/set"
        )
        self.assertEqual(
            set_request["params"],
            {
                "threadId": "thread-goal",
                "objective": "ship safely",
                "status": "active",
            },
        )

    async def test_goal_activity_sink_failure_does_not_break_unique_stream(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path, "goal")) as codex:
                process = codex._client._sync._proc
                control = AppServerGoalControl(codex)
                handle = await control.start("thread-goal", "ship safely")

                def broken_sink(_projection) -> None:
                    raise RuntimeError("display unavailable")

                terminal = await handle.wait_terminal(broken_sink)
            _close_probe_pipes(process)
            gc.collect()

        self.assertEqual(terminal.logical_turn_id, "turn-1")
        self.assertEqual(terminal.final_physical_turn_id, "turn-2")

    async def test_goal_resume_registers_route_without_clearing_goal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path, "resume")) as codex:
                process = codex._client._sync._proc
                control = AppServerGoalControl(codex)
                before = await control.get("thread-goal")
                handle = await control.resume("thread-goal")
                terminal = await handle.wait_terminal()
            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        self.assertIsNotNone(before)
        self.assertEqual(before.status, GoalStatus.PAUSED)
        self.assertEqual(handle.id, "turn-resume")
        self.assertEqual(terminal.final_physical_turn_id, "turn-resume")
        methods = [item.get("method") for item in messages if "id" in item]
        self.assertNotIn("thread/goal/clear", methods)
        self.assertEqual(methods.count("thread/goal/set"), 1)

    async def test_goal_get_rejects_a_wrong_thread_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path, "wrong-goal-thread")) as codex:
                process = codex._client._sync._proc
                control = AppServerGoalControl(codex)
                with self.assertRaisesRegex(GoalControlError, "Thread 不一致"):
                    await control.get("thread-goal")
            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        methods = [item.get("method") for item in messages if "id" in item]
        self.assertEqual(methods, ["initialize", "thread/goal/get"])

    async def test_goal_pause_persists_before_interrupting_exact_turn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path, "pause")) as codex:
                process = codex._client._sync._proc
                control = AppServerGoalControl(codex)
                handle = await control.start("thread-goal", "pause safely")
                acknowledgement = await handle.pause()
                await handle.aclose()
            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        self.assertEqual(acknowledgement.goal.status, GoalStatus.PAUSED)
        self.assertEqual(acknowledgement.physical_turn_id, "turn-1")
        self.assertTrue(acknowledgement.interrupt_acknowledged)
        methods = [item.get("method") for item in messages if "id" in item]
        paused_index = next(
            index
            for index, item in enumerate(messages)
            if item.get("method") == "thread/goal/set"
            and item.get("params", {}).get("status") == "paused"
        )
        interrupt_index = next(
            index
            for index, item in enumerate(messages)
            if item.get("method") == "turn/interrupt"
        )
        self.assertLess(paused_index, interrupt_index)
        self.assertEqual(
            messages[interrupt_index]["params"],
            {"threadId": "thread-goal", "turnId": "turn-1"},
        )
        self.assertEqual(methods.count("turn/interrupt"), 1)

    async def test_goal_clear_uses_exact_thread_and_confirms_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path, "clear")) as codex:
                process = codex._client._sync._proc
                control = AppServerGoalControl(codex)
                self.assertTrue(await control.clear("thread-goal"))
                self.assertIsNone(await control.get("thread-goal"))
            _close_probe_pipes(process)
            gc.collect()
            messages = _messages(log_path)

        clear_request = next(
            item for item in messages if item.get("method") == "thread/goal/clear"
        )
        self.assertEqual(clear_request["params"], {"threadId": "thread-goal"})

    async def test_goal_mutation_response_loss_is_never_reported_as_success(self) -> None:
        for mode, operation, expected_turn_id in (
            ("pause-loss", "pause", None),
            ("interrupt-loss", "pause", "turn-1"),
            ("clear-loss", "clear", None),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                log_path = Path(raw) / "requests.jsonl"
                async with AsyncCodex(_config(log_path, mode)) as codex:
                    process = codex._client._sync._proc
                    control = AppServerGoalControl(codex)
                    handle = None
                    if operation == "pause":
                        handle = await control.start("thread-goal", "lose response")
                    with self.assertRaises(GoalMutationStateUnknown) as caught:
                        if operation == "pause":
                            assert handle is not None
                            await handle.pause()
                        else:
                            await control.clear("thread-goal")
                    if handle is not None:
                        await handle.aclose()
                _close_probe_pipes(process)
                gc.collect()

            self.assertEqual(caught.exception.physical_turn_id, expected_turn_id)

    async def test_goal_start_and_resume_response_loss_retain_unknown_ownership(
        self,
    ) -> None:
        for mode, operation in (("start-loss", "start"), ("resume-loss", "resume")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                log_path = Path(raw) / "requests.jsonl"
                codex = AsyncCodex(_config(log_path, mode))
                await codex.__aenter__()
                process = codex._client._sync._proc
                try:
                    control = AppServerGoalControl(codex)
                    with self.assertRaises(GoalMutationStateUnknown) as caught:
                        if operation == "start":
                            await control.start("thread-goal", "lose start")
                        else:
                            await control.resume("thread-goal")
                    if caught.exception.handle is not None:
                        await caught.exception.handle.aclose()
                finally:
                    with suppress(BrokenPipeError):
                        await codex.close()
                _close_probe_pipes(process)
                gc.collect()
                messages = _messages(log_path)

            active_sets = [
                item
                for item in messages
                if item.get("method") == "thread/goal/set"
                and item.get("params", {}).get("status") == "active"
            ]
            self.assertEqual(len(active_sets), 1)
            if operation == "resume":
                self.assertIsNotNone(caught.exception.handle)
            else:
                self.assertIsNone(caught.exception.handle)

    async def test_capabilities_have_no_version_or_experimental_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path)) as codex:
                process = codex._client._sync._proc
                with patch.object(openai_codex, "__version__", "99.0.0"):
                    self.assertIsInstance(
                        AppServerSkillCatalog(codex),
                        AppServerSkillCatalog,
                    )
                    self.assertIsInstance(
                        AppServerGoalControl(codex),
                        AppServerGoalControl,
                    )
                    self.assertIsInstance(
                        AppServerThreadDeleteControl(codex),
                        AppServerThreadDeleteControl,
                    )
                    self.assertIsInstance(
                        AppServerSideBoundaryControl(codex),
                        AppServerSideBoundaryControl,
                    )
                    self.assertIsInstance(
                        AppServerThreadSubscriptionControl(codex),
                        AppServerThreadSubscriptionControl,
                    )
            _close_probe_pipes(process)
            gc.collect()

    async def test_capability_shape_failures_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "requests.jsonl"
            async with AsyncCodex(_config(log_path)) as codex:
                process = codex._client._sync._proc
                with patch(
                    "netizen.sdk_gap_adapter._generated.SkillsListResponse",
                    None,
                ):
                    with self.assertRaises(SdkGapCapabilityUnavailable):
                        AppServerSkillCatalog(codex)
                    self.assertIsInstance(
                        AppServerGoalControl(codex),
                        AppServerGoalControl,
                    )
                with patch.object(
                    type(codex._client),
                    "cancel_goal_operation",
                    None,
                ):
                    with self.assertRaises(SdkGapCapabilityUnavailable):
                        AppServerGoalControl(codex)
                    self.assertIsInstance(
                        AppServerSkillCatalog(codex),
                        AppServerSkillCatalog,
                    )
                with patch(
                    "netizen.sdk_gap_adapter._generated.ThreadInjectItemsResponse",
                    None,
                ):
                    with self.assertRaises(SdkGapCapabilityUnavailable):
                        AppServerSideBoundaryControl(codex)
                    self.assertIsInstance(
                        AppServerSkillCatalog(codex),
                        AppServerSkillCatalog,
                    )
                with patch(
                    "netizen.sdk_gap_adapter._generated.ThreadUnsubscribeStatus",
                    type("MalformedStatus", (), {}),
                ):
                    with self.assertRaises(SdkGapCapabilityUnavailable):
                        AppServerThreadSubscriptionControl(codex)
                    self.assertIsInstance(
                        AppServerSkillCatalog(codex),
                        AppServerSkillCatalog,
                    )
                with patch(
                    "netizen.sdk_gap_adapter._generated.ThreadDeleteResponse",
                    None,
                ):
                    with self.assertRaises(SdkGapCapabilityUnavailable):
                        AppServerThreadDeleteControl(codex)
                    self.assertIsInstance(
                        AppServerSkillCatalog(codex),
                        AppServerSkillCatalog,
                    )
            _close_probe_pipes(process)
            gc.collect()

    def test_public_facade_inventory_is_a_migration_sentinel(self) -> None:
        self.assertEqual(facade_migration_requirements(), ())
        with patch.object(
            AsyncCodex,
            "thread_unsubscribe",
            object(),
            create=True,
        ):
            self.assertEqual(
                facade_migration_requirements(),
                (
                    "migration-required:thread-subscription:"
                    "AsyncCodex.thread_unsubscribe",
                ),
            )


if __name__ == "__main__":
    unittest.main()
