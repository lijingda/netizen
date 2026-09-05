from __future__ import annotations

import difflib
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openai_codex
from openai_codex import AsyncCodex
from openai_codex.generated import v2_all as generated
from openai_codex.models import Notification, UnknownNotification

from netizen import task_diff_observer
from netizen.task_diff_observer import (
    PinnedTaskDiffObserver,
    TaskDiffObservationUnavailable,
)


ZERO_OID = "0" * 40


def blob_oid(content: str | None) -> str:
    if content is None:
        return ZERO_OID
    data = content.encode()
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {len(data)}\0".encode())
    digest.update(data)
    return digest.hexdigest()


def native_diff(path: str, old: str | None, new: str | None) -> str:
    result = [f"diff --git a/{path} b/{path}\n"]
    if old is None:
        result.append("new file mode 100644\n")
    result.append(f"index {blob_oid(old)}..{blob_oid(new)}\n")
    result.extend(
        difflib.unified_diff(
            [] if old is None else old.splitlines(keepends=True),
            [] if new is None else new.splitlines(keepends=True),
            fromfile="/dev/null" if old is None else f"a/{path}",
            tofile="/dev/null" if new is None else f"b/{path}",
        )
    )
    return "".join(result)


def turn(turn_id: str, status: str) -> generated.Turn:
    return generated.Turn.model_validate(
        {"id": turn_id, "items": [], "status": status}
    )


def thread_started(thread_id: str, parent: str) -> Notification:
    thread = generated.Thread.model_validate(
        {
            "id": thread_id,
            "preview": "child",
            "ephemeral": False,
            "modelProvider": "openai",
            "createdAt": 1,
            "updatedAt": 1,
            "status": {"type": "active", "activeFlags": []},
            "cwd": "/tmp",
            "cliVersion": "0.147.0",
            "source": "appServer",
            "turns": [],
            "sessionId": thread_id,
            "parentThreadId": parent,
        }
    )
    return Notification(
        "thread/started",
        generated.ThreadStartedNotification(thread=thread),
    )


def turn_started(thread_id: str, turn_id: str) -> Notification:
    return Notification(
        "turn/started",
        generated.TurnStartedNotification(
            threadId=thread_id,
            turn=turn(turn_id, "inProgress"),
        ),
    )


def turn_completed(thread_id: str, turn_id: str) -> Notification:
    return Notification(
        "turn/completed",
        generated.TurnCompletedNotification(
            threadId=thread_id,
            turn=turn(turn_id, "completed"),
        ),
    )


def diff_updated(thread_id: str, turn_id: str, diff: str) -> Notification:
    return Notification(
        "turn/diff/updated",
        generated.TurnDiffUpdatedNotification(
            threadId=thread_id,
            turnId=turn_id,
            diff=diff,
        ),
    )


def file_change_completed(
    thread_id: str,
    turn_id: str,
    item_id: str,
    paths: tuple[str, ...] = ("shared.txt",),
) -> Notification:
    root = generated.FileChangeThreadItem.model_validate(
        {
            "changes": [
                {
                    "diff": "",
                    "kind": {"type": "update"},
                    "path": path,
                }
                for path in paths
            ],
            "id": item_id,
            "status": "completed",
            "type": "fileChange",
        }
    )
    return Notification(
        "item/completed",
        generated.ItemCompletedNotification(
            completedAtMs=2,
            item=generated.ThreadItem(root=root),
            threadId=thread_id,
            turnId=turn_id,
        ),
    )


def collab(
    phase: str,
    *,
    completed: bool,
    receivers: list[str] | None = None,
) -> Notification:
    root = generated.CollabAgentToolCallThreadItem.model_validate(
        {
            "agentsStates": {},
            "id": "spawn-one",
            "receiverThreadIds": (
                ["child"] if completed else []
            )
            if receivers is None
            else receivers,
            "senderThreadId": "root",
            "status": "completed" if completed else "inProgress",
            "tool": "spawnAgent",
            "type": "collabAgentToolCall",
        }
    )
    payload = {
        "threadId": "root",
        "turnId": "root-turn",
        "item": generated.ThreadItem(root=root),
    }
    if phase == "started":
        return Notification(
            "item/started",
            generated.ItemStartedNotification(startedAtMs=1, **payload),
        )
    return Notification(
        "item/completed",
        generated.ItemCompletedNotification(completedAtMs=2, **payload),
    )


class PinnedTaskDiffObserverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.codex = AsyncCodex()
        self.codex._initialized = True
        self.router = self.codex._client._sync._router

    def test_pre_router_tap_keeps_child_diff_after_sdk_discards_child_queue(
        self,
    ) -> None:
        observer = PinnedTaskDiffObserver(self.codex)
        self.router.register_turn("root-turn")
        capture = observer.begin()
        child = "first\nsecond\n"
        final = "first\nsecond\nthird\n"
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            (cwd / "shared.txt").write_text(final, encoding="utf-8")
            root_started = turn_started("root", "root-turn")
            notifications = (
                root_started,
                collab("started", completed=False),
                thread_started("child", "root"),
                turn_started("child", "child-turn"),
                file_change_completed("child", "child-turn", "child-patch"),
                diff_updated(
                    "child",
                    "child-turn",
                    native_diff("shared.txt", None, child),
                ),
                turn_completed("child", "child-turn"),
                collab("completed", completed=True),
                file_change_completed("root", "root-turn", "root-patch"),
                diff_updated(
                    "root",
                    "root-turn",
                    native_diff("shared.txt", child, final),
                ),
                turn_completed("root", "root-turn"),
            )
            for notification in notifications:
                self.router.route_notification(notification)

            result = observer.finish(
                capture,
                root_thread_id="root",
                root_turn_id="root-turn",
                cwd=cwd,
            )

        root_queue = self.router._turn_notifications["root-turn"]
        with root_queue.mutex:
            routed = tuple(root_queue.queue)
        self.assertIs(routed[0], root_started)
        self.assertNotIn("child-turn", self.router._pending_turn_notifications)
        self.assertTrue(result.complete)
        self.assertEqual(
            (result.override.additions, result.override.deletions),
            (3, 0),
        )

    def test_projection_failure_still_routes_the_exact_notification_once(self) -> None:
        PinnedTaskDiffObserver(self.codex)
        self.router.register_turn("root-turn")
        invalid = Notification(
            "turn/diff/updated",
            UnknownNotification(
                {"threadId": "root", "turnId": "root-turn", "diff": "bad"}
            ),
        )

        self.router.route_notification(invalid)

        queue = self.router._turn_notifications["root-turn"]
        with queue.mutex:
            routed = tuple(queue.queue)
        self.assertEqual(routed, (invalid,))

    def test_observer_local_failure_cannot_block_original_routing(self) -> None:
        PinnedTaskDiffObserver(self.codex)
        self.router.register_turn("root-turn")
        notification = turn_started("root", "root-turn")

        with (
            patch.object(
                PinnedTaskDiffObserver,
                "_observe_notification",
                side_effect=RuntimeError("projection failed"),
            ),
            patch.object(
                PinnedTaskDiffObserver,
                "_append_invalid",
                side_effect=RuntimeError("buffer failed"),
            ),
        ):
            self.router.route_notification(notification)

        queue = self.router._turn_notifications["root-turn"]
        with queue.mutex:
            routed = tuple(queue.queue)
        self.assertEqual(routed, (notification,))

    def test_oversized_file_change_is_rejected_before_projection_and_still_routed(
        self,
    ) -> None:
        observer = PinnedTaskDiffObserver(self.codex)
        self.router.register_turn("root-turn")
        capture = observer.begin()
        notification = file_change_completed(
            "root",
            "root-turn",
            "oversized",
            tuple(
                f"path-{index}.txt"
                for index in range(task_diff_observer._MAX_PROJECTED_FILE_CHANGES + 1)
            ),
        )

        self.router.route_notification(notification)

        queue = self.router._turn_notifications["root-turn"]
        with queue.mutex:
            routed = tuple(queue.queue)
        self.assertEqual(routed, (notification,))
        result = observer.finish(
            capture,
            root_thread_id="root",
            root_turn_id="root-turn",
            cwd=Path("/tmp"),
        )
        self.assertFalse(result.complete)
        self.assertIn("projection", result.reason)

    def test_oversized_collab_receiver_list_is_rejected_before_copying(self) -> None:
        notification = collab(
            "completed",
            completed=True,
            receivers=[
                f"child-{index}"
                for index in range(task_diff_observer._MAX_PROJECTED_RECEIVERS + 1)
            ],
        )

        projected = task_diff_observer._project_notification(notification)

        self.assertIsInstance(projected, task_diff_observer.TaskCaptureInvalid)

    def test_buffer_overflow_and_transport_failure_fail_closed(self) -> None:
        observer = PinnedTaskDiffObserver(self.codex, max_events=2)
        capture = observer.begin()
        self.router.route_notification(turn_started("root", "root-turn"))
        self.router.route_notification(diff_updated("root", "root-turn", ""))
        self.router.route_notification(turn_completed("root", "root-turn"))
        overflow = observer.finish(
            capture,
            root_thread_id="root",
            root_turn_id="root-turn",
            cwd=Path("/tmp"),
        )
        later = observer.begin()
        self.router.fail_all(RuntimeError("transport closed"))
        failed = observer.finish(
            later,
            root_thread_id="root",
            root_turn_id="root-turn",
            cwd=Path("/tmp"),
        )

        self.assertFalse(overflow.complete)
        self.assertFalse(failed.complete)

    def test_sdk_runtime_source_and_double_install_are_pinned(self) -> None:
        with patch.object(openai_codex, "__version__", "0.147.1"):
            with self.assertRaisesRegex(
                TaskDiffObservationUnavailable,
                "openai-codex==0.147.0",
            ):
                PinnedTaskDiffObserver(self.codex)
        with patch.object(
            task_diff_observer.importlib.metadata,
            "version",
            return_value="0.148.0",
        ):
            with self.assertRaisesRegex(
                TaskDiffObservationUnavailable,
                "openai-codex-cli-bin==0.147.0",
            ):
                PinnedTaskDiffObserver(self.codex)
        with patch.object(task_diff_observer, "_PACKAGE_SOURCE_FINGERPRINT", "0" * 64):
            with self.assertRaisesRegex(
                TaskDiffObservationUnavailable,
                "source fingerprint changed",
            ):
                PinnedTaskDiffObserver(self.codex)

        PinnedTaskDiffObserver(self.codex)
        with self.assertRaisesRegex(
            TaskDiffObservationUnavailable,
            "already installed",
        ):
            PinnedTaskDiffObserver(self.codex)


if __name__ == "__main__":
    unittest.main()
