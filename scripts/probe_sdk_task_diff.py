#!/usr/bin/env python3
"""Synthetic gate for the pinned pre-router root-task diff observer."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


_ROOT_THREAD = "thread-root"
_ROOT_TURN = "turn-root"
_CHILD_THREAD = "thread-child"
_CHILD_TURN = "turn-child"
_PATH = "research.md"
_ZERO_OID = "0" * 40


def _send(payload: object) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")


def _blob_oid(content: str | None) -> str:
    if content is None:
        return _ZERO_OID
    data = content.encode("utf-8")
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _diff(old: str | None, new: str | None) -> str:
    lines = [f"diff --git a/{_PATH} b/{_PATH}\n"]
    if old is None:
        lines.append("new file mode 100644\n")
    lines.append(f"index {_blob_oid(old)}..{_blob_oid(new)}\n")
    lines.extend(
        difflib.unified_diff(
            [] if old is None else old.splitlines(keepends=True),
            [] if new is None else new.splitlines(keepends=True),
            fromfile="/dev/null" if old is None else f"a/{_PATH}",
            tofile="/dev/null" if new is None else f"b/{_PATH}",
            n=3,
        )
    )
    return "".join(lines)


def _thread(thread_id: str, *, parent: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "id": thread_id,
        "preview": "task diff probe",
        "ephemeral": False,
        "modelProvider": "openai",
        "createdAt": 1,
        "updatedAt": 2,
        "status": {"type": "idle"},
        "cwd": "/tmp",
        "cliVersion": "0.147.0",
        "source": "appServer",
        "turns": [],
        "sessionId": thread_id,
    }
    if parent is not None:
        result["parentThreadId"] = parent
    return result


def _turn(turn_id: str, status: str) -> dict[str, object]:
    return {"id": turn_id, "items": [], "status": status}


def _item(method: str, thread_id: str, turn_id: str, item: object) -> None:
    timestamp = "startedAtMs" if method == "item/started" else "completedAtMs"
    _send(
        {
            "method": method,
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                timestamp: 1 if method == "item/started" else 2,
                "item": item,
            },
        }
    )


def _file_change(item_id: str, *, kind: str = "update") -> dict[str, object]:
    return {
        "type": "fileChange",
        "id": item_id,
        "status": "completed",
        "changes": [
            {
                "path": _PATH,
                "kind": {"type": kind},
                "diff": "synthetic",
            }
        ],
    }


def _collab(status: str, receivers: list[str]) -> dict[str, object]:
    return {
        "type": "collabAgentToolCall",
        "id": "spawn-child",
        "tool": "spawnAgent",
        "status": status,
        "senderThreadId": _ROOT_THREAD,
        "receiverThreadIds": receivers,
        "agentsStates": {},
    }


def _emit_task_notifications() -> None:
    draft = "".join(f"line {index}\n" for index in range(300))
    child = "".join(f"line {index}\n" for index in range(337))
    final = "".join(f"line {index}\n" for index in range(310)) + "".join(
        f"replacement {index}\n" for index in range(32)
    )
    _send(
        {
            "method": "turn/started",
            "params": {
                "threadId": _ROOT_THREAD,
                "turn": _turn(_ROOT_TURN, "inProgress"),
            },
        }
    )
    _item("item/started", _ROOT_THREAD, _ROOT_TURN, _collab("inProgress", []))
    _send(
        {
            "method": "thread/started",
            "params": {"thread": _thread(_CHILD_THREAD, parent=_ROOT_THREAD)},
        }
    )
    _send(
        {
            "method": "turn/started",
            "params": {
                "threadId": _CHILD_THREAD,
                "turn": _turn(_CHILD_TURN, "inProgress"),
            },
        }
    )
    _item(
        "item/completed",
        _CHILD_THREAD,
        _CHILD_TURN,
        _file_change("child-add", kind="add"),
    )
    _send(
        {
            "method": "turn/diff/updated",
            "params": {
                "threadId": _CHILD_THREAD,
                "turnId": _CHILD_TURN,
                "diff": _diff(None, draft),
            },
        }
    )
    _item(
        "item/completed",
        _CHILD_THREAD,
        _CHILD_TURN,
        _file_change("child-update"),
    )
    _send(
        {
            "method": "turn/diff/updated",
            "params": {
                "threadId": _CHILD_THREAD,
                "turnId": _CHILD_TURN,
                "diff": _diff(None, child),
            },
        }
    )
    _send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": _CHILD_THREAD,
                "turn": _turn(_CHILD_TURN, "completed"),
            },
        }
    )
    _item(
        "item/completed",
        _ROOT_THREAD,
        _ROOT_TURN,
        _collab("completed", [_CHILD_THREAD]),
    )
    _item(
        "item/completed",
        _ROOT_THREAD,
        _ROOT_TURN,
        _file_change("root-patch"),
    )
    _send(
        {
            "method": "turn/diff/updated",
            "params": {
                "threadId": _ROOT_THREAD,
                "turnId": _ROOT_TURN,
                "diff": _diff(child, final),
            },
        }
    )
    _send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": _ROOT_THREAD,
                "turn": _turn(_ROOT_TURN, "completed"),
            },
        }
    )


def _fake_server() -> None:
    for raw in sys.stdin:
        message = json.loads(raw)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            _send(
                {
                    "id": request_id,
                    "result": {
                        "userAgent": "netizen-task-diff-probe/1",
                        "serverInfo": {"name": "task-diff-probe", "version": "1"},
                    },
                }
            )
        elif method == "thread/start":
            _send(
                {
                    "id": request_id,
                    "result": {
                        "thread": _thread(_ROOT_THREAD),
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
            _send(
                {
                    "id": request_id,
                    "result": {"turn": _turn(_ROOT_TURN, "inProgress")},
                }
            )
        elif method == "thread/read":
            # The extra request is a deterministic barrier: the pinned SDK has
            # registered the root Turn queue before the fake server releases
            # any completion notification.
            _emit_task_notifications()
            _send(
                {
                    "id": request_id,
                    "result": {"thread": _thread(_ROOT_THREAD)},
                }
            )
        sys.stdout.flush()


async def _client(cwd: Path) -> None:
    from openai_codex import AsyncCodex, CodexConfig

    from netizen.task_diff_observer import PinnedTaskDiffObserver

    final = "".join(f"line {index}\n" for index in range(310)) + "".join(
        f"replacement {index}\n" for index in range(32)
    )
    (cwd / _PATH).write_text(final, encoding="utf-8")
    config = CodexConfig(
        launch_args_override=(
            sys.executable,
            str(Path(__file__).resolve()),
            "--server",
        )
    )
    async with AsyncCodex(config) as codex:
        observer = PinnedTaskDiffObserver(codex)
        thread = await codex.thread_start(cwd=str(cwd))
        capture = observer.begin()
        handle = await thread.turn("compose child changes")
        read = await thread.read()
        if read.thread.id != thread.id:
            raise RuntimeError("thread/read barrier returned the wrong Thread")
        streamed = [notification async for notification in handle.stream()]
        result = observer.finish(
            capture,
            root_thread_id=thread.id,
            root_turn_id=handle.id,
            cwd=cwd,
        )
        if not result.complete or result.descendant_turns != 1:
            raise RuntimeError("root-task composition did not include one child Turn")
        if result.override is None:
            raise RuntimeError("root-task composition did not produce an override")
        if (result.override.additions, result.override.deletions) != (342, 0):
            raise RuntimeError("root-task composition did not produce +342 -0")
        if tuple(item.path for item in result.override.files) != (_PATH,):
            raise RuntimeError("root-task composition reported the wrong file")
        pending = codex._client._sync._router._pending_turn_notifications
        if _CHILD_TURN in pending:
            raise RuntimeError("pinned SDK unexpectedly retained the child Turn queue")
        if not any(item.method == "turn/diff/updated" for item in streamed):
            raise RuntimeError("root Turn stream did not preserve its diff notification")


def _driver(timeout: float) -> int:
    with tempfile.TemporaryDirectory(prefix="netizen-task-diff-probe-") as raw:
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--client",
                    "--cwd",
                    raw,
                ],
                check=True,
                timeout=timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.TimeoutExpired:
            print("FAIL: root-task diff observer probe blocked.", file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or "").strip()
            print(
                "ERROR: root-task diff observer probe failed"
                + (f": {detail[-1000:]}" if detail else "."),
                file=sys.stderr,
            )
            return 2
    print(
        "PASS: child notifications survived the pre-router tap and composed "
        "child Add→Update plus parent editing into +342 -0."
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--client", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.client and args.cwd is None:
        parser.error("--client requires --cwd")
    return args


def main() -> int:
    args = _parse_args()
    if args.server:
        _fake_server()
        return 0
    if args.client:
        asyncio.run(_client(args.cwd))
        return 0
    return _driver(args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
