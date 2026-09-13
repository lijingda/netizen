#!/usr/bin/env python3
"""Fail if the public SDK loses an immediate turn/completed notification."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path


_CLIENT_STAGE_PREFIX = "SDK_COMPLETION_STAGE="
_CLIENT_STAGES = frozenset({
    "sdk_import", "client_start", "thread_start", "turn_start",
    "handle_run", "thread_read", "client_close", "complete",
})


def _client_stage(stage: str) -> None:
    if stage not in _CLIENT_STAGES:
        raise ValueError("unknown completion probe stage")
    print(_CLIENT_STAGE_PREFIX + stage, file=sys.stderr, flush=True)


def _last_client_stage(stderr: str | bytes | None) -> str:
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    last = "unreported"
    for line in (stderr or "").splitlines():
        if line.startswith(_CLIENT_STAGE_PREFIX):
            stage = line[len(_CLIENT_STAGE_PREFIX):]
            if stage in _CLIENT_STAGES:
                last = stage
    return last


def _send(payload: object) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")


def _thread_payload(*, include_turns: bool) -> dict[str, object]:
    turn = {
        "id": "turn-race",
        "items": [
            {
                "id": "item-final",
                "type": "agentMessage",
                "text": "READ-RECOVERED",
                "phase": "final_answer",
            }
        ],
        "status": "completed",
        "startedAt": 1,
        "completedAt": 2,
        "durationMs": 1000,
    }
    return {
        "id": "thread-race",
        "preview": "fast completion",
        "ephemeral": False,
        "modelProvider": "openai",
        "createdAt": 0,
        "updatedAt": 2,
        "status": {"type": "idle"},
        "cwd": "/tmp",
        "cliVersion": "0.0.0",
        "source": "appServer",
        "turns": [turn] if include_turns else [],
        "sessionId": "session-race",
    }


def _usage_breakdown(total_tokens: int) -> dict[str, int]:
    return {
        "cachedInputTokens": 100,
        "inputTokens": total_tokens - 200,
        "outputTokens": 100,
        "reasoningOutputTokens": 0,
        "totalTokens": total_tokens,
    }


def _usage_thread_payload(
    *,
    turn_id: str | None,
    active: bool,
    include_turns: bool,
) -> dict[str, object]:
    turns: list[dict[str, object]] = []
    if include_turns and turn_id is not None:
        turns.append(
            {
                "id": turn_id,
                "items": [],
                "status": "inProgress" if active else "completed",
                "startedAt": 1,
                "completedAt": None if active else 2,
            }
        )
    return {
        "id": "thread-usage",
        "preview": "usage drain",
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
        "turns": turns,
        "sessionId": "session-usage",
    }


def _send_usage_notifications(
    *, turn_id: str, turn_number: int, completed: bool
) -> None:
    used_tokens = 1_000 + turn_number
    _send(
        {
            "method": "turn/diff/updated",
            "params": {
                "threadId": "thread-usage",
                "turnId": turn_id,
                "diff": f"diff --git a/{turn_id}.txt b/{turn_id}.txt\n",
            },
        }
    )
    _send(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-usage",
                "turnId": turn_id,
                "tokenUsage": {
                    "last": _usage_breakdown(used_tokens),
                    "total": _usage_breakdown(used_tokens * 2),
                    "modelContextWindow": 100_000,
                },
            },
        }
    )
    if completed:
        _send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-usage",
                    "turn": {
                        "id": turn_id,
                        "items": [],
                        "status": "completed",
                    },
                },
            }
        )


def _fake_server(*, usage_mode: bool = False) -> None:
    usage_turn_number = 0
    usage_turn_id: str | None = None
    usage_active = False
    close_wake_turn = False
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            _send(
                {
                    "id": request_id,
                    "result": {
                        "userAgent": "netizen-sdk-probe/1",
                        "serverInfo": {
                            "name": "netizen-sdk-probe",
                            "version": "1",
                        },
                    },
                }
            )
            sys.stdout.flush()
        elif method == "thread/start":
            thread = (
                _usage_thread_payload(
                    turn_id=None,
                    active=False,
                    include_turns=False,
                )
                if usage_mode
                else _thread_payload(include_turns=False)
            )
            _send(
                {
                    "id": request_id,
                    "result": {
                        "thread": thread,
                        "model": "test-model",
                        "modelProvider": "openai",
                        "cwd": "/tmp",
                        "approvalPolicy": "untrusted",
                        "approvalsReviewer": "auto_review",
                        "sandbox": {"type": "readOnly"},
                    },
                }
            )
            sys.stdout.flush()
        elif method == "turn/start":
            if usage_mode:
                usage_turn_number += 1
                usage_turn_id = f"turn-usage-{usage_turn_number}"
                usage_active = True
                raw_input = message.get("params", {}).get("input", [])
                close_wake_turn = "close-wake" in json.dumps(raw_input)
                if "immediate metadata" in json.dumps(raw_input):
                    _send_usage_notifications(
                        turn_id=usage_turn_id,
                        turn_number=usage_turn_number,
                        completed=True,
                    )
                    usage_active = False
                    sys.stdout.flush()
                _send(
                    {
                        "id": request_id,
                        "result": {
                            "turn": {
                                "id": usage_turn_id,
                                "items": [],
                                "status": "inProgress",
                            }
                        },
                    }
                )
                sys.stdout.flush()
                continue
            response = {
                "id": request_id,
                "result": {
                    "turn": {
                        "id": "turn-race",
                        "items": [],
                        "status": "inProgress",
                    }
                },
            }
            completed = {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-race",
                    "turn": {
                        "id": "turn-race",
                        "items": [],
                        "status": "completed",
                    },
                },
            }
            sys.stdout.write(json.dumps(response) + "\n" + json.dumps(completed) + "\n")
            sys.stdout.flush()
        elif method == "thread/read":
            include_turns = bool(message.get("params", {}).get("includeTurns"))
            if usage_mode:
                assert usage_turn_id is not None
                _send(
                    {
                        "id": request_id,
                        "result": {
                            "thread": _usage_thread_payload(
                                turn_id=usage_turn_id,
                                active=usage_active,
                                include_turns=include_turns,
                            ),
                        },
                    }
                )
                sys.stdout.flush()
                if usage_active:
                    _send_usage_notifications(
                        turn_id=usage_turn_id,
                        turn_number=usage_turn_number,
                        completed=not close_wake_turn,
                    )
                    if not close_wake_turn:
                        usage_active = False
                    sys.stdout.flush()
                continue
            _send(
                {
                    "id": request_id,
                    "result": {
                        "thread": _thread_payload(include_turns=include_turns),
                    },
                }
            )
            sys.stdout.flush()


async def _public_client() -> None:
    _client_stage("sdk_import")
    from openai_codex import AsyncCodex, CodexConfig

    config = CodexConfig(
        launch_args_override=(
            sys.executable,
            str(Path(__file__).resolve()),
            "--server",
        )
    )
    _client_stage("client_start")
    async with AsyncCodex(config) as codex:
        try:
            _client_stage("thread_start")
            thread = await codex.thread_start(cwd="/tmp")
            _client_stage("turn_start")
            turn = await thread.turn("fast completion")
            _client_stage("handle_run")
            result = await turn.run()
            assert result.id == "turn-race"
            assert result.status.value == "completed"
        finally:
            _client_stage("client_close")


async def _public_read_client() -> None:
    _client_stage("sdk_import")
    from openai_codex import AsyncCodex, CodexConfig

    config = CodexConfig(
        launch_args_override=(
            sys.executable,
            str(Path(__file__).resolve()),
            "--server",
        )
    )
    _client_stage("client_start")
    async with AsyncCodex(config) as codex:
        try:
            _client_stage("thread_start")
            thread = await codex.thread_start(cwd="/tmp")
            _client_stage("turn_start")
            handle = await thread.turn("fast completion")
            _client_stage("thread_read")
            snapshot = await thread.read(include_turns=True)
            exact = next(turn for turn in snapshot.thread.turns if turn.id == handle.id)
            assert exact.status.value == "completed"
            final = next(
                item.root.text
                for item in exact.items
                if getattr(item.root, "type", None) == "agentMessage"
            )
            assert final == "READ-RECOVERED"
        finally:
            _client_stage("client_close")


async def _usage_drain_client(*, attempts: int) -> None:
    from openai_codex import AsyncCodex, CodexConfig
    from openai_codex.types import (
        ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification,
    )

    config = CodexConfig(
        launch_args_override=(
            sys.executable,
            str(Path(__file__).resolve()),
            "--server",
            "--usage-server",
        )
    )
    codex = AsyncCodex(config)
    closed = False
    try:
        thread = await codex.thread_start(cwd="/tmp")
        for attempt in range(1, attempts + 1):
            immediate = attempt % 2 == 0
            prompt = "immediate metadata" if immediate else "usage drain"
            handle = await thread.turn(f"{prompt} {attempt}")
            assert handle.id == f"turn-usage-{attempt}"
            if not immediate:
                active = await thread.read(include_turns=True)
                exact = next(turn for turn in active.thread.turns if turn.id == handle.id)
                assert active.thread.status.root.type == "active"
                assert exact.status.value == "inProgress"

            # The immediate case's first read is already terminal; all metadata
            # arrived before the start response created the original handle.
            terminal = await thread.read(include_turns=True)
            exact = next(turn for turn in terminal.thread.turns if turn.id == handle.id)
            assert terminal.thread.id == handle.thread_id == "thread-usage"
            assert terminal.thread.status.root.type == "idle"
            assert exact.status.value == "completed"

            if immediate:
                events = [event async for event in handle.stream()]
                assert [event.method for event in events] == [
                    "turn/diff/updated",
                    "thread/tokenUsage/updated",
                    "turn/completed",
                ]
                diff, usage_event, completed = [event.payload for event in events]
                assert diff.thread_id == handle.thread_id and diff.turn_id == handle.id
                assert diff.diff == f"diff --git a/{handle.id}.txt b/{handle.id}.txt\n"
                assert isinstance(usage_event, ThreadTokenUsageUpdatedNotification)
                assert usage_event.thread_id == handle.thread_id
                assert usage_event.turn_id == handle.id
                assert isinstance(completed, TurnCompletedNotification)
                assert completed.thread_id == handle.thread_id
                assert completed.turn.id == handle.id
                assert completed.turn.status.value == "completed"
                usage = usage_event.token_usage
            else:
                result = await handle.run()
                assert result.id == handle.id
                assert result.status.value == "completed"
                usage = result.usage
            assert usage is not None
            assert usage.last.total_tokens == 1_000 + attempt
            assert usage.total.total_tokens == (1_000 + attempt) * 2
            assert usage.model_context_window == 100_000

        waiting = await thread.turn("close-wake")
        active = await thread.read(include_turns=True)
        exact = next(turn for turn in active.thread.turns if turn.id == waiting.id)
        assert active.thread.status.root.type == "active"
        assert exact.status.value == "inProgress"

        usage_notification = asyncio.Event()
        diff_notification = asyncio.Event()

        async def consume_until_transport_close() -> None:
            async for notification in waiting.stream():
                if notification.method == "turn/diff/updated":
                    payload = notification.payload
                    assert getattr(payload, "thread_id", None) == "thread-usage"
                    assert getattr(payload, "turn_id", None) == waiting.id
                    assert getattr(payload, "diff", None) == (
                        f"diff --git a/{waiting.id}.txt b/{waiting.id}.txt\n"
                    )
                    diff_notification.set()
                if isinstance(
                    notification.payload,
                    ThreadTokenUsageUpdatedNotification,
                ):
                    usage_notification.set()

        consumer = asyncio.create_task(consume_until_transport_close())
        await asyncio.wait_for(
            asyncio.gather(
                usage_notification.wait(),
                diff_notification.wait(),
            ),
            timeout=1,
        )
        await asyncio.sleep(0)
        await codex.close()
        closed = True
        result = await asyncio.gather(consumer, return_exceptions=True)
        assert len(result) == 1 and isinstance(result[0], BaseException)
    finally:
        if not closed:
            await codex.close()


def _driver(*, attempts: int, timeout: float, read_recovery: bool) -> int:
    for attempt in range(1, attempts + 1):
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--read-client" if read_recovery else "--client",
                ],
                check=True,
                timeout=timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.TimeoutExpired as error:
            print(
                "FAIL: completion probe client subprocess timed out "
                f"(attempt {attempt}; stage={_last_client_stage(error.stderr)}).",
                file=sys.stderr,
            )
            return 1
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or "").strip()
            print(
                "ERROR: completion probe client failed"
                + (f": {detail[-1000:]}" if detail else "."),
                file=sys.stderr,
            )
            return 2
    if read_recovery:
        print(
            f"PASS: {attempts} immediate completions were recovered through "
            "public thread.read."
        )
    else:
        print(f"PASS: {attempts} immediate completions reached the public SDK handle.")
    return 0


def _usage_driver(*, attempts: int, timeout: float) -> int:
    try:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--usage-client",
                "--attempts",
                str(attempts),
            ],
            check=True,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print(
            "FAIL: terminal usage drain or transport-close wake blocked the SDK "
            f"executor ({attempts} turns).",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        print(
            "ERROR: usage drain probe client failed"
            + (f": {detail[-1000:]}" if detail else "."),
            file=sys.stderr,
        )
        return 2
    print(
        f"PASS: {attempts} terminal metadata drains "
        f"({attempts // 2} completed before the start response); "
        "exact usage/diff/completion retained and transport close woke a blocked stream."
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument(
        "--read-recovery",
        action="store_true",
        help="verify public thread.read recovery instead of handle.run delivery",
    )
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--usage-server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--client", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--read-client", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--usage-client", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--usage-drain", action="store_true")
    args = parser.parse_args()
    if args.attempts < 1 or args.timeout <= 0:
        parser.error("--attempts and --timeout must be positive")
    return args


def main() -> int:
    args = _parse_args()
    if args.server:
        _fake_server(usage_mode=args.usage_server)
        return 0
    if args.client:
        asyncio.run(_public_client())
        _client_stage("complete")
        return 0
    if args.read_client:
        asyncio.run(_public_read_client())
        _client_stage("complete")
        return 0
    if args.usage_client:
        asyncio.run(_usage_drain_client(attempts=args.attempts))
        return 0
    if args.usage_drain:
        return _usage_driver(attempts=args.attempts, timeout=args.timeout)
    return _driver(
        attempts=args.attempts,
        timeout=args.timeout,
        read_recovery=args.read_recovery,
    )


if __name__ == "__main__":
    raise SystemExit(main())
