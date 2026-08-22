#!/usr/bin/env python3
"""Synthetic gate for ADR 0020's non-consuming active-Turn plan observer."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path


_THREAD_ID = "thread-plan"
_TURN_ID = "turn-plan"


def _send(payload: object) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")


def _thread_payload(*, active: bool, include_turns: bool) -> dict[str, object]:
    turn = {
        "id": _TURN_ID,
        "items": [],
        "status": "inProgress" if active else "completed",
        "startedAt": 1,
        "completedAt": None if active else 2,
    }
    return {
        "id": _THREAD_ID,
        "preview": "plan probe",
        "ephemeral": False,
        "modelProvider": "openai",
        "createdAt": 0,
        "updatedAt": 2,
        "status": (
            {"type": "active", "activeFlags": []}
            if active
            else {"type": "idle"}
        ),
        "cwd": "/tmp",
        "cliVersion": "0.0.0",
        "source": "appServer",
        "turns": [turn] if include_turns else [],
        "sessionId": "session-plan",
    }


def _fake_server() -> None:
    active = False
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            _send(
                {
                    "id": request_id,
                    "result": {
                        "userAgent": "netizen-plan-probe/1",
                        "serverInfo": {"name": "plan-probe", "version": "1"},
                    },
                }
            )
        elif method == "thread/start":
            _send(
                {
                    "id": request_id,
                    "result": {
                        "thread": _thread_payload(
                            active=False,
                            include_turns=False,
                        ),
                        "model": "test-model",
                        "modelProvider": "openai",
                        "cwd": "/tmp",
                        "approvalPolicy": "untrusted",
                        "approvalsReviewer": "auto_review",
                        "sandbox": {"type": "readOnly"},
                    },
                }
            )
        elif method == "turn/start":
            active = True
            _send(
                {
                    "id": request_id,
                    "result": {
                        "turn": {
                            "id": _TURN_ID,
                            "items": [],
                            "status": "inProgress",
                        }
                    },
                }
            )
            _send(
                {
                    "method": "turn/plan/updated",
                    "params": {
                        "threadId": _THREAD_ID,
                        "turnId": _TURN_ID,
                        "explanation": "synthetic plan",
                        "plan": [
                            {"step": "inspect", "status": "completed"},
                            {"step": "verify", "status": "inProgress"},
                            {"step": "ship", "status": "pending"},
                        ],
                    },
                }
            )
        elif method == "thread/read":
            include_turns = bool(message.get("params", {}).get("includeTurns"))
            active = False
            _send(
                {
                    "id": request_id,
                    "result": {
                        "thread": _thread_payload(
                            active=False,
                            include_turns=include_turns,
                        )
                    },
                }
            )
            _send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": _THREAD_ID,
                        "turn": {
                            "id": _TURN_ID,
                            "items": [],
                            "status": "completed",
                        },
                    },
                }
            )
        sys.stdout.flush()


async def _client() -> None:
    from openai_codex import AsyncCodex, CodexConfig
    from openai_codex.generated.v2_all import TurnPlanUpdatedNotification

    from netizen.turn_plan_observer import (
        PinnedTurnPlanObserver,
        TurnPlanStepState,
    )

    config = CodexConfig(
        launch_args_override=(
            sys.executable,
            str(Path(__file__).resolve()),
            "--server",
        )
    )
    async with AsyncCodex(config) as codex:
        observer = PinnedTurnPlanObserver(codex)
        thread = await codex.thread_start(cwd="/tmp")
        handle = await thread.turn("publish a checklist")
        for _ in range(100):
            observation = observer.observe(
                thread_id=thread.id,
                turn_id=handle.id,
                after_cursor=0,
            )
            if observation.plan_updated:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("plan notification did not reach the exact Turn queue")

        assert observation.next_cursor == 1
        assert tuple((item.step, item.status) for item in observation.steps) == (
            ("inspect", TurnPlanStepState.COMPLETED),
            ("verify", TurnPlanStepState.IN_PROGRESS),
            ("ship", TurnPlanStepState.PENDING),
        )
        turn_queue = codex._client._sync._router._turn_notifications[handle.id]
        with turn_queue.mutex:
            before = tuple(turn_queue.queue)

        terminal = await thread.read(include_turns=True)
        exact = next(turn for turn in terminal.thread.turns if turn.id == handle.id)
        assert exact.status.value == "completed"
        streamed = []
        async for notification in handle.stream():
            streamed.append(notification)
        plan = next(
            notification
            for notification in streamed
            if isinstance(notification.payload, TurnPlanUpdatedNotification)
        )
        assert plan is before[0]
        assert plan.payload.turn_id == handle.id


def _driver(timeout: float) -> int:
    try:
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--client"],
            check=True,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print(
            "FAIL: Turn plan observer or subsequent public stream drain blocked.",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        print(
            "ERROR: Turn plan observer probe failed"
            + (f": {detail[-1000:]}" if detail else "."),
            file=sys.stderr,
        )
        return 2
    print(
        "PASS: native plan was observed without consuming the exact Turn queue; "
        "the public stream then drained it to completion."
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--client", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main() -> int:
    args = _parse_args()
    if args.server:
        _fake_server()
        return 0
    if args.client:
        asyncio.run(_client())
        return 0
    return _driver(args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
