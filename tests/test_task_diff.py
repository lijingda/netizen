from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netizen import task_diff
from netizen.task_diff import (
    TaskCollabToolCall,
    TaskFileChangeCompleted,
    TaskThreadStarted,
    TaskTurnCompleted,
    TaskTurnDiffUpdated,
    TaskTurnStarted,
    compose_task_diff,
)


ZERO_OID = "0" * 40


def blob_oid(content: str | None) -> str:
    if content is None:
        return ZERO_OID
    data = content.encode("utf-8")
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {len(data)}\0".encode())
    digest.update(data)
    return digest.hexdigest()


def native_diff(path: str, old: str | None, new: str | None) -> str:
    result = [f"diff --git a/{path} b/{path}\n"]
    if old is None:
        result.append("new file mode 100644\n")
    elif new is None:
        result.append("deleted file mode 100644\n")
    result.append(f"index {blob_oid(old)}..{blob_oid(new)}\n")
    result.extend(
        difflib.unified_diff(
            [] if old is None else old.splitlines(keepends=True),
            [] if new is None else new.splitlines(keepends=True),
            fromfile="/dev/null" if old is None else f"a/{path}",
            tofile="/dev/null" if new is None else f"b/{path}",
            n=3,
        )
    )
    return "".join(result)


def reported_paths(diff: str) -> tuple[str, ...]:
    return tuple(
        line.split(" b/", 1)[1]
        for line in diff.splitlines()
        if line.startswith("diff --git a/") and " b/" in line
    )


def root_with_child_events(
    child_diffs: tuple[str, ...],
    root_diffs: tuple[str, ...] = (),
    *,
    child_completed: bool = True,
) -> tuple[object, ...]:
    events: list[object] = [TaskTurnStarted(0, "root", "root-turn")]
    events.extend(
        (
            TaskCollabToolCall(
                1,
                "root",
                "root-turn",
                "spawn-one",
                "started",
                "spawnAgent",
                "inProgress",
                "root",
                (),
            ),
            TaskThreadStarted(2, "child", "root"),
            TaskTurnStarted(3, "child", "child-turn"),
        )
    )
    sequence = 4
    for index, diff in enumerate(child_diffs):
        events.append(
            TaskFileChangeCompleted(
                sequence,
                "child",
                "child-turn",
                f"child-patch-{index}",
                "completed",
                reported_paths(diff),
            )
        )
        sequence += 1
        events.append(TaskTurnDiffUpdated(sequence, "child", "child-turn", diff))
        sequence += 1
    if child_completed:
        events.append(
            TaskTurnCompleted(sequence, "child", "child-turn", "completed")
        )
        sequence += 1
    events.append(
        TaskCollabToolCall(
            sequence,
            "root",
            "root-turn",
            "spawn-one",
            "completed",
            "spawnAgent",
            "completed",
            "root",
            ("child",),
        )
    )
    sequence += 1
    for index, diff in enumerate(root_diffs):
        events.append(
            TaskFileChangeCompleted(
                sequence,
                "root",
                "root-turn",
                f"root-patch-{index}",
                "completed",
                reported_paths(diff),
            )
        )
        sequence += 1
        events.append(TaskTurnDiffUpdated(sequence, "root", "root-turn", diff))
        sequence += 1
    events.append(TaskTurnCompleted(sequence, "root", "root-turn", "completed"))
    return tuple(events)


class TaskDiffComposerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def compose(
        self,
        events: tuple[object, ...] | list[object],
        *,
        root_thread_id: str = "root",
        root_turn_id: str = "root-turn",
        include_prior_root_turns: bool = False,
    ) -> task_diff.TaskDiffComposition:
        return compose_task_diff(
            events,
            root_thread_id=root_thread_id,
            root_turn_id=root_turn_id,
            cwd=self.cwd,
            include_prior_root_turns=include_prior_root_turns,
        )

    def test_child_add_then_parent_update_is_one_root_task_net_addition(self) -> None:
        draft = "".join(f"line {index}\n" for index in range(300))
        child = "".join(f"line {index}\n" for index in range(337))
        final = "".join(f"line {index}\n" for index in range(310)) + "".join(
            f"replacement {index}\n" for index in range(32)
        )
        (self.cwd / "research.md").write_text(final, encoding="utf-8")
        events = root_with_child_events(
            (
                native_diff("research.md", None, draft),
                native_diff("research.md", None, child),
            ),
            (native_diff("research.md", child, final),),
        )

        composed = self.compose(events)

        self.assertTrue(composed.complete)
        self.assertEqual(composed.descendant_turns, 1)
        self.assertEqual(
            (composed.override.additions, composed.override.deletions),
            (342, 0),
        )
        self.assertEqual(
            [(item.path, item.additions, item.deletions) for item in composed.override.files],
            [("research.md", 342, 0)],
        )

    def test_final_turn_snapshots_cannot_hide_cross_turn_reentry(self) -> None:
        baseline = "base\n"
        parent_interim = "parent interim\nextra\n"
        final = "final\n"
        (self.cwd / "shared.txt").write_text(final, encoding="utf-8")
        events = (
            TaskTurnStarted(0, "root", "root-turn"),
            TaskCollabToolCall(1, "root", "root-turn", "spawn-one", "started", "spawnAgent", "inProgress", "root", ()),
            TaskFileChangeCompleted(2, "root", "root-turn", "root-first", "completed", ("shared.txt",)),
            TaskTurnDiffUpdated(3, "root", "root-turn", native_diff("shared.txt", baseline, parent_interim)),
            TaskThreadStarted(4, "child", "root"),
            TaskTurnStarted(5, "child", "child-turn"),
            TaskFileChangeCompleted(6, "child", "child-turn", "child-revert", "completed", ("shared.txt",)),
            TaskTurnDiffUpdated(7, "child", "child-turn", native_diff("shared.txt", parent_interim, baseline)),
            TaskTurnCompleted(8, "child", "child-turn", "completed"),
            TaskCollabToolCall(9, "root", "root-turn", "spawn-one", "completed", "spawnAgent", "completed", "root", ("child",)),
            TaskFileChangeCompleted(10, "root", "root-turn", "root-final", "completed", ("shared.txt",)),
            TaskTurnDiffUpdated(11, "root", "root-turn", native_diff("shared.txt", baseline, final)),
            TaskTurnCompleted(12, "root", "root-turn", "completed"),
        )

        composed = self.compose(events)

        self.assertFalse(composed.complete)
        self.assertIn("linear", composed.reason)

    def test_aggregate_snapshot_can_repeat_an_unchanged_task_path(self) -> None:
        primary = "primary\n"
        secondary = "secondary\n"
        (self.cwd / "primary.txt").write_text(primary, encoding="utf-8")
        (self.cwd / "secondary.txt").write_text(secondary, encoding="utf-8")
        first = native_diff("primary.txt", None, primary)
        aggregate = first + native_diff("secondary.txt", None, secondary)
        events = list(root_with_child_events((first, aggregate)))
        second_change = next(
            event
            for event in events
            if isinstance(event, TaskFileChangeCompleted)
            and event.item_id == "child-patch-1"
        )
        events[events.index(second_change)] = TaskFileChangeCompleted(
            second_change.sequence,
            second_change.thread_id,
            second_change.turn_id,
            second_change.item_id,
            second_change.status,
            ("secondary.txt",),
        )

        composed = self.compose(tuple(events))

        self.assertTrue(composed.complete)
        self.assertEqual(
            (composed.override.additions, composed.override.deletions),
            (2, 0),
        )

    def test_incomparable_interleaved_turn_terminals_fail_closed(
        self,
    ) -> None:
        first = "one\n"
        middle = "one\ntwo\n"
        final = "one\ntwo\nthree\n"
        (self.cwd / "shared.txt").write_text(final, encoding="utf-8")
        # Put the root's first-touch snapshot between the child's two aggregate
        # snapshots to model two physical Turns interleaving on one path.
        events = (
            TaskTurnStarted(0, "root", "root-turn"),
            TaskCollabToolCall(1, "root", "root-turn", "spawn-one", "started", "spawnAgent", "inProgress", "root", ()),
            TaskThreadStarted(2, "child", "root"),
            TaskTurnStarted(3, "child", "child-turn"),
            TaskFileChangeCompleted(4, "child", "child-turn", "child-patch-1", "completed", ("shared.txt",)),
            TaskTurnDiffUpdated(5, "child", "child-turn", native_diff("shared.txt", None, first)),
            TaskFileChangeCompleted(6, "root", "root-turn", "root-patch", "completed", ("shared.txt",)),
            TaskTurnDiffUpdated(7, "root", "root-turn", native_diff("shared.txt", first, middle)),
            TaskFileChangeCompleted(8, "child", "child-turn", "child-patch-2", "completed", ("shared.txt",)),
            TaskTurnDiffUpdated(9, "child", "child-turn", native_diff("shared.txt", None, final)),
            TaskTurnCompleted(10, "child", "child-turn", "completed"),
            TaskCollabToolCall(11, "root", "root-turn", "spawn-one", "completed", "spawnAgent", "completed", "root", ("child",)),
            TaskTurnCompleted(12, "root", "root-turn", "completed"),
        )

        composed = self.compose(events)

        self.assertFalse(composed.complete)
        self.assertIn("linear", composed.reason)

    def test_oid_chain_not_notification_order_selects_task_baseline(self) -> None:
        baseline = "base\n"
        child = "base\nchild\n"
        parent_one = "base\nchild\nparent-one\n"
        final = "base\nchild\nparent-one\nparent-two\n"
        (self.cwd / "shared.txt").write_text(final, encoding="utf-8")
        events = (
            TaskTurnStarted(0, "root", "root-turn"),
            TaskCollabToolCall(
                1,
                "root",
                "root-turn",
                "spawn-one",
                "started",
                "spawnAgent",
                "inProgress",
                "root",
                (),
            ),
            TaskThreadStarted(2, "child", "root"),
            TaskTurnStarted(3, "child", "child-turn"),
            # The child committed first, but its per-Thread notification reached
            # this connection after the parent's first aggregate snapshot.
            TaskFileChangeCompleted(
                4,
                "root",
                "root-turn",
                "parent-one",
                "completed",
                ("shared.txt",),
            ),
            TaskTurnDiffUpdated(
                5,
                "root",
                "root-turn",
                native_diff("shared.txt", child, parent_one),
            ),
            TaskFileChangeCompleted(
                6,
                "child",
                "child-turn",
                "child-patch",
                "completed",
                ("shared.txt",),
            ),
            TaskTurnDiffUpdated(
                7,
                "child",
                "child-turn",
                native_diff("shared.txt", baseline, child),
            ),
            TaskFileChangeCompleted(
                8,
                "root",
                "root-turn",
                "parent-two",
                "completed",
                ("shared.txt",),
            ),
            TaskTurnDiffUpdated(
                9,
                "root",
                "root-turn",
                native_diff("shared.txt", child, final),
            ),
            TaskCollabToolCall(
                10,
                "root",
                "root-turn",
                "spawn-one",
                "completed",
                "spawnAgent",
                "completed",
                "root",
                ("child",),
            ),
            # This is the usual spawn lifecycle: the tool call has returned,
            # while the child finishes later but before its parent Turn.
            TaskTurnCompleted(11, "child", "child-turn", "completed"),
            TaskTurnCompleted(12, "root", "root-turn", "completed"),
        )

        composed = self.compose(events)

        self.assertTrue(composed.complete)
        self.assertEqual(
            (composed.override.additions, composed.override.deletions),
            (3, 0),
        )

    def test_existing_file_deleted_by_child_counts_only_task_baseline(self) -> None:
        original = "alpha\nbeta\n"
        events = root_with_child_events(
            (native_diff("gone.txt", original, None),),
        )

        composed = self.compose(events)

        self.assertTrue(composed.complete)
        self.assertEqual(
            (composed.override.additions, composed.override.deletions),
            (0, 2),
        )
        self.assertEqual(composed.override.files, ())

    def test_root_without_descendants_preserves_native_physical_diff(self) -> None:
        composed = self.compose(
            (
                TaskTurnStarted(0, "root", "root-turn"),
                TaskTurnCompleted(1, "root", "root-turn", "completed"),
            ),
        )

        self.assertTrue(composed.complete)
        self.assertIsNone(composed.override)

    def test_busy_composer_fails_closed_without_waiting(self) -> None:
        final = "value\n"
        (self.cwd / "file.txt").write_text(final, encoding="utf-8")
        events = root_with_child_events(
            (native_diff("file.txt", None, final),)
        )
        self.assertTrue(task_diff._COMPOSE_LOCK.acquire(blocking=False))
        try:
            composed = self.compose(events)
            single_turn = self.compose(
                (
                    TaskTurnStarted(100, "other", "other-turn"),
                    TaskTurnCompleted(101, "other", "other-turn", "completed"),
                ),
                root_thread_id="other",
                root_turn_id="other-turn",
            )
        finally:
            task_diff._COMPOSE_LOCK.release()

        self.assertFalse(composed.complete)
        self.assertIn("busy", composed.reason)
        self.assertTrue(single_turn.complete)
        self.assertIsNone(single_turn.override)

    def test_resume_only_does_not_claim_a_child_turn(self) -> None:
        composed = self.compose(
            (
                TaskTurnStarted(0, "root", "root-turn"),
                TaskCollabToolCall(
                    1,
                    "root",
                    "root-turn",
                    "resume",
                    "started",
                    "resumeAgent",
                    "inProgress",
                    "root",
                    ("child",),
                ),
                TaskCollabToolCall(
                    2,
                    "root",
                    "root-turn",
                    "resume",
                    "completed",
                    "resumeAgent",
                    "completed",
                    "root",
                    ("child",),
                ),
                TaskTurnCompleted(3, "root", "root-turn", "completed"),
            ),
        )

        self.assertTrue(composed.complete)
        self.assertIsNone(composed.override)

    def test_resume_then_send_input_claims_only_the_started_child_turn(self) -> None:
        final = "child\n"
        (self.cwd / "file.txt").write_text(final, encoding="utf-8")
        events = (
            TaskTurnStarted(0, "root", "root-turn"),
            TaskCollabToolCall(1, "root", "root-turn", "resume", "started", "resumeAgent", "inProgress", "root", ("child",)),
            TaskCollabToolCall(2, "root", "root-turn", "resume", "completed", "resumeAgent", "completed", "root", ("child",)),
            TaskCollabToolCall(3, "root", "root-turn", "send", "started", "sendInput", "inProgress", "root", ("child",)),
            TaskTurnStarted(4, "child", "child-turn"),
            TaskCollabToolCall(5, "root", "root-turn", "send", "completed", "sendInput", "completed", "root", ("child",)),
            TaskFileChangeCompleted(6, "child", "child-turn", "patch", "completed", ("file.txt",)),
            TaskTurnDiffUpdated(7, "child", "child-turn", native_diff("file.txt", None, final)),
            TaskTurnCompleted(8, "child", "child-turn", "completed"),
            TaskTurnCompleted(9, "root", "root-turn", "completed"),
        )

        composed = self.compose(events)

        self.assertTrue(composed.complete)
        self.assertEqual(composed.descendant_turns, 1)
        self.assertEqual((composed.override.additions, composed.override.deletions), (1, 0))

    def test_spawn_then_send_input_claims_two_turns_on_the_same_child(self) -> None:
        first = "first\n"
        final = "first\nsecond\n"
        (self.cwd / "file.txt").write_text(final, encoding="utf-8")
        events = (
            TaskTurnStarted(0, "root", "root-turn"),
            TaskCollabToolCall(1, "root", "root-turn", "spawn", "started", "spawnAgent", "inProgress", "root", ()),
            TaskThreadStarted(2, "child", "root"),
            TaskTurnStarted(3, "child", "child-turn-1"),
            TaskCollabToolCall(4, "root", "root-turn", "spawn", "completed", "spawnAgent", "completed", "root", ("child",)),
            TaskFileChangeCompleted(5, "child", "child-turn-1", "patch-1", "completed", ("file.txt",)),
            TaskTurnDiffUpdated(6, "child", "child-turn-1", native_diff("file.txt", None, first)),
            TaskTurnCompleted(7, "child", "child-turn-1", "completed"),
            TaskCollabToolCall(8, "root", "root-turn", "send", "started", "sendInput", "inProgress", "root", ("child",)),
            TaskTurnStarted(9, "child", "child-turn-2"),
            TaskCollabToolCall(10, "root", "root-turn", "send", "completed", "sendInput", "completed", "root", ("child",)),
            TaskFileChangeCompleted(11, "child", "child-turn-2", "patch-2", "completed", ("file.txt",)),
            TaskTurnDiffUpdated(12, "child", "child-turn-2", native_diff("file.txt", first, final)),
            TaskTurnCompleted(13, "child", "child-turn-2", "completed"),
            TaskTurnCompleted(14, "root", "root-turn", "completed"),
        )

        composed = self.compose(events)

        self.assertTrue(composed.complete)
        self.assertEqual(composed.descendant_turns, 2)
        self.assertEqual((composed.override.additions, composed.override.deletions), (2, 0))

    def test_missing_child_terminal_fails_closed(self) -> None:
        content = "child\n"

        incomplete = self.compose(
            root_with_child_events(
                (native_diff("file.txt", None, content),),
                child_completed=False,
            ),
        )

        self.assertFalse(incomplete.complete)
        self.assertEqual(incomplete.override.files, ())

    def test_unsupported_rename_and_malformed_oid_fail_closed(self) -> None:
        (self.cwd / "new.txt").write_text("new\n", encoding="utf-8")
        old_oid = blob_oid("old\n")
        new_oid = blob_oid("new\n")
        rename = (
            "diff --git a/old.txt b/new.txt\n"
            f"index {old_oid}..{new_oid}\n"
            "--- a/old.txt\n"
            "+++ b/new.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        malformed = native_diff("new.txt", None, "new\n").replace(
            blob_oid("new\n"),
            "1234567",
        )

        for diff in (rename, malformed):
            with self.subTest(diff=diff.splitlines()[0]):
                composed = self.compose(
                    root_with_child_events((diff,)),
                )
                self.assertFalse(composed.complete)
                self.assertEqual(composed.override.files, ())

    def test_empty_or_missing_snapshot_after_file_change_fails_closed(self) -> None:
        (self.cwd / "file.txt").write_text("same\n", encoding="utf-8")
        base = list(root_with_child_events(()))
        child_completion = next(
            index
            for index, event in enumerate(base)
            if isinstance(event, TaskTurnCompleted) and event.thread_id == "child"
        )
        ambiguous = (
            *base[:child_completion],
            TaskFileChangeCompleted(4, "child", "child-turn", "patch", "completed", ("file.txt",)),
            TaskTurnDiffUpdated(5, "child", "child-turn", ""),
            TaskTurnCompleted(6, "child", "child-turn", "completed"),
            TaskCollabToolCall(7, "root", "root-turn", "spawn-one", "completed", "spawnAgent", "completed", "root", ("child",)),
            TaskTurnCompleted(8, "root", "root-turn", "completed"),
        )
        missing = tuple(event for event in ambiguous if not isinstance(event, TaskTurnDiffUpdated))

        for events in (ambiguous, missing):
            composed = self.compose(events)
            self.assertFalse(composed.complete)
            self.assertEqual(composed.override.files, ())

    def test_noncompleted_file_change_fails_closed(self) -> None:
        final = "value\n"
        (self.cwd / "file.txt").write_text(final, encoding="utf-8")
        events = list(
            root_with_child_events((native_diff("file.txt", None, final),))
        )
        change = next(
            event for event in events if isinstance(event, TaskFileChangeCompleted)
        )
        events[events.index(change)] = TaskFileChangeCompleted(
            change.sequence,
            change.thread_id,
            change.turn_id,
            change.item_id,
            "failed",
            change.paths,
        )

        composed = self.compose(tuple(events))

        self.assertFalse(composed.complete)
        self.assertIn("successfully", composed.reason)

    def test_terminal_anchor_rejects_external_rollback_to_an_old_blob(self) -> None:
        original = "before\n"
        changed = "after\n"
        (self.cwd / "file.txt").write_text(original, encoding="utf-8")

        composed = self.compose(
            root_with_child_events((native_diff("file.txt", original, changed),)),
        )

        self.assertFalse(composed.complete)
        self.assertIn("terminal mutation", composed.reason)

    def test_only_final_snapshot_hunks_are_reconstructed(self) -> None:
        original = "base\n"
        first = "branch\n"
        final = "final\n"
        (self.cwd / "file.txt").write_text(final, encoding="utf-8")
        corrupt = native_diff("file.txt", original, first).replace(
            "-base\n",
            "-wrong\n",
        )

        composed = self.compose(
            root_with_child_events(
                (corrupt, native_diff("file.txt", original, final)),
            ),
        )

        self.assertTrue(composed.complete)
        self.assertEqual(
            (composed.override.additions, composed.override.deletions),
            (1, 1),
        )

    def test_path_disappearing_from_final_snapshot_fails_closed(self) -> None:
        original = "base\n"
        branch = "branch\n"
        created = "created\n"
        (self.cwd / "primary.txt").write_text(original, encoding="utf-8")
        (self.cwd / "secondary.txt").write_text(created, encoding="utf-8")
        first = native_diff("primary.txt", original, branch)
        second = native_diff("secondary.txt", None, created)
        events = list(root_with_child_events((first, second)))
        second_change = next(
            event
            for event in events
            if isinstance(event, TaskFileChangeCompleted)
            and event.item_id == "child-patch-1"
        )
        events[events.index(second_change)] = TaskFileChangeCompleted(
            second_change.sequence,
            second_change.thread_id,
            second_change.turn_id,
            second_change.item_id,
            second_change.status,
            ("primary.txt", "secondary.txt"),
        )

        composed = self.compose(tuple(events))

        self.assertFalse(composed.complete)
        self.assertIn("aggregate snapshot", composed.reason)

    def test_unicode_line_separator_is_content_not_a_line_ending(self) -> None:
        original = "a\u2028b\n"
        final = "a\nb\n"
        (self.cwd / "unicode.txt").write_text(final, encoding="utf-8")
        diff = (
            "diff --git a/unicode.txt b/unicode.txt\n"
            f"index {blob_oid(original)}..{blob_oid(final)}\n"
            "--- a/unicode.txt\n"
            "+++ b/unicode.txt\n"
            "@@ -1 +1,2 @@\n"
            "-a\u2028b\n"
            "+a\n"
            "+b\n"
        )

        composed = self.compose(
            root_with_child_events((diff,)),
        )

        self.assertTrue(composed.complete)
        self.assertEqual((composed.override.additions, composed.override.deletions), (2, 1))

    def test_myers_work_budget_is_shared_across_task_paths(self) -> None:
        first = native_diff("first.txt", "old\n", "new\n")
        second = native_diff("second.txt", "before\n", "after\n")
        (self.cwd / "first.txt").write_text("new\n", encoding="utf-8")
        (self.cwd / "second.txt").write_text("after\n", encoding="utf-8")

        with patch.object(task_diff, "_MAX_MYERS_WORK", 7):
            composed = self.compose(
                root_with_child_events((first + second,)),
            )

        self.assertFalse(composed.complete)
        self.assertIn("work bound", composed.reason)

    def test_reconstruction_work_has_a_task_level_bound(self) -> None:
        original = "old\n"
        final = "new\n"
        (self.cwd / "file.txt").write_text(final, encoding="utf-8")

        with patch.object(task_diff, "_MAX_TEXT_WORK", 5):
            composed = self.compose(
                root_with_child_events(
                    (native_diff("file.txt", original, final),)
                ),
            )

        self.assertFalse(composed.complete)
        self.assertIn("reconstruction", composed.reason)

    def test_empty_file_addition_uses_presence_not_line_count(self) -> None:
        (self.cwd / "empty.txt").write_bytes(b"")

        composed = self.compose(
            root_with_child_events((native_diff("empty.txt", None, ""),)),
        )

        self.assertTrue(composed.complete)
        self.assertEqual((composed.override.additions, composed.override.deletions), (0, 0))
        self.assertEqual(tuple(item.path for item in composed.override.files), ("empty.txt",))

    def test_symlink_and_oversized_final_anchor_fail_closed(self) -> None:
        target = self.cwd / "target.txt"
        target.write_text("value\n", encoding="utf-8")
        (self.cwd / "link.txt").symlink_to(target)
        outside = self.cwd.parent / f"{self.cwd.name}-outside"
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (outside / "nested.txt").write_text("value\n", encoding="utf-8")
        self.addCleanup((outside / "nested.txt").unlink)
        (self.cwd / "linked-directory").symlink_to(outside, target_is_directory=True)
        os.mkfifo(self.cwd / "pipe.txt")
        link_events = root_with_child_events(
            (native_diff("link.txt", None, "value\n"),)
        )
        ancestor_link_events = root_with_child_events(
            (native_diff("linked-directory/nested.txt", None, "value\n"),)
        )
        pipe_events = root_with_child_events(
            (native_diff("pipe.txt", None, "value\n"),)
        )

        linked = self.compose(link_events)
        ancestor_linked = self.compose(ancestor_link_events)
        special = self.compose(pipe_events)
        with patch.object(task_diff, "_MAX_FILE_BYTES", 2):
            oversized = self.compose(
                root_with_child_events(
                    (native_diff("target.txt", None, "value\n"),)
                ),
            )

        self.assertFalse(linked.complete)
        self.assertFalse(ancestor_linked.complete)
        self.assertFalse(special.complete)
        self.assertFalse(oversized.complete)

    def test_ambiguous_child_and_changed_receiver_identity_fail_closed(self) -> None:
        base = list(root_with_child_events((native_diff("file.txt", None, "x\n"),)))
        root_completion = base.pop()
        base.extend(
            (
                TaskTurnStarted(root_completion.sequence, "child", "second-child-turn"),
                TaskTurnCompleted(root_completion.sequence + 1, "child", "second-child-turn", "completed"),
                TaskTurnCompleted(root_completion.sequence + 2, "root", "root-turn", "completed"),
            )
        )
        started = next(
            event
            for event in base
            if isinstance(event, TaskCollabToolCall) and event.phase == "started"
        )
        changed_receivers = list(root_with_child_events((native_diff("file.txt", None, "x\n"),)))
        changed_receivers[changed_receivers.index(started)] = TaskCollabToolCall(
            started.sequence,
            started.thread_id,
            started.turn_id,
            started.item_id,
            started.phase,
            started.tool,
            started.status,
            started.sender_thread_id,
            ("different-child",),
        )

        for events in (tuple(base), tuple(changed_receivers)):
            composed = self.compose(events)
            self.assertFalse(composed.complete)

    def test_noncausal_same_path_conflicts_but_disjoint_path_does_not(self) -> None:
        final = "task\n"
        (self.cwd / "task.txt").write_text(final, encoding="utf-8")
        task_events = list(
            root_with_child_events((native_diff("task.txt", None, final),))
        )
        root_completion = task_events.pop()

        def with_external(path: str) -> tuple[object, ...]:
            external = native_diff(path, None, "external\n")
            return (
                *task_events,
                TaskFileChangeCompleted(
                    root_completion.sequence,
                    "external",
                    "external-turn",
                    "external-patch",
                    "completed",
                    (path,),
                ),
                TaskTurnDiffUpdated(
                    root_completion.sequence + 1,
                    "external",
                    "external-turn",
                    external,
                ),
                TaskTurnCompleted(
                    root_completion.sequence + 2,
                    "root",
                    "root-turn",
                    "completed",
                ),
            )

        conflict = self.compose(
            with_external("task.txt"),
        )
        disjoint = self.compose(
            with_external("other.txt"),
        )

        self.assertFalse(conflict.complete)
        self.assertTrue(disjoint.complete)
        self.assertEqual(disjoint.override.additions, 1)

    def test_goal_scope_includes_prior_root_physical_turns(self) -> None:
        child = "one\n"
        final = "one\ntwo\n"
        (self.cwd / "goal.txt").write_text(final, encoding="utf-8")
        events = (
            TaskTurnStarted(0, "root", "physical-1"),
            TaskCollabToolCall(1, "root", "physical-1", "spawn", "started", "spawnAgent", "inProgress", "root", ()),
            TaskThreadStarted(2, "child", "root"),
            TaskTurnStarted(3, "child", "child-turn"),
            TaskFileChangeCompleted(4, "child", "child-turn", "child-patch", "completed", ("goal.txt",)),
            TaskTurnDiffUpdated(5, "child", "child-turn", native_diff("goal.txt", None, child)),
            TaskTurnCompleted(6, "child", "child-turn", "completed"),
            TaskCollabToolCall(7, "root", "physical-1", "spawn", "completed", "spawnAgent", "completed", "root", ("child",)),
            TaskTurnCompleted(8, "root", "physical-1", "completed"),
            TaskTurnStarted(9, "root", "physical-2"),
            TaskFileChangeCompleted(10, "root", "physical-2", "root-patch", "completed", ("goal.txt",)),
            TaskTurnDiffUpdated(11, "root", "physical-2", native_diff("goal.txt", child, final)),
            TaskTurnCompleted(12, "root", "physical-2", "completed"),
        )

        composed = self.compose(
            events,
            root_turn_id="physical-2",
            include_prior_root_turns=True,
        )

        self.assertTrue(composed.complete)
        self.assertEqual(composed.descendant_turns, 1)
        self.assertEqual((composed.override.additions, composed.override.deletions), (2, 0))

    def test_goal_scope_composes_multiple_root_turns_without_a_child(self) -> None:
        first = "one\n"
        final = "one\ntwo\n"
        (self.cwd / "goal.txt").write_text(final, encoding="utf-8")
        events = (
            TaskTurnStarted(0, "root", "physical-1"),
            TaskFileChangeCompleted(
                1,
                "root",
                "physical-1",
                "first-patch",
                "completed",
                ("goal.txt",),
            ),
            TaskTurnDiffUpdated(
                2,
                "root",
                "physical-1",
                native_diff("goal.txt", None, first),
            ),
            TaskTurnCompleted(3, "root", "physical-1", "completed"),
            TaskTurnStarted(4, "root", "physical-2"),
            TaskFileChangeCompleted(
                5,
                "root",
                "physical-2",
                "second-patch",
                "completed",
                ("goal.txt",),
            ),
            TaskTurnDiffUpdated(
                6,
                "root",
                "physical-2",
                native_diff("goal.txt", first, final),
            ),
            TaskTurnCompleted(7, "root", "physical-2", "completed"),
        )

        composed = self.compose(
            events,
            root_turn_id="physical-2",
            include_prior_root_turns=True,
        )

        self.assertTrue(composed.complete)
        self.assertEqual(composed.descendant_turns, 0)
        self.assertEqual((composed.override.additions, composed.override.deletions), (2, 0))

    def test_goal_scope_rejects_unclaimed_child_from_an_earlier_root_turn(
        self,
    ) -> None:
        final = "root\n"
        (self.cwd / "goal.txt").write_text(final, encoding="utf-8")
        events = (
            TaskTurnStarted(0, "root", "physical-1"),
            TaskThreadStarted(1, "unclaimed-child", "root"),
            TaskTurnStarted(2, "unclaimed-child", "unclaimed-turn"),
            TaskTurnCompleted(3, "unclaimed-child", "unclaimed-turn", "completed"),
            TaskTurnCompleted(4, "root", "physical-1", "completed"),
            TaskTurnStarted(5, "root", "physical-2"),
            TaskFileChangeCompleted(
                6,
                "root",
                "physical-2",
                "root-patch",
                "completed",
                ("goal.txt",),
            ),
            TaskTurnDiffUpdated(
                7,
                "root",
                "physical-2",
                native_diff("goal.txt", None, final),
            ),
            TaskTurnCompleted(8, "root", "physical-2", "completed"),
        )

        composed = self.compose(
            events,
            root_turn_id="physical-2",
            include_prior_root_turns=True,
        )

        self.assertFalse(composed.complete)
        self.assertIn("unclaimed", composed.reason)

    def test_goal_scope_rejects_incomplete_prior_root_lifecycle(self) -> None:
        first = "one\n"
        final = "one\ntwo\n"
        (self.cwd / "goal.txt").write_text(final, encoding="utf-8")
        events = (
            TaskTurnStarted(0, "root", "physical-1"),
            TaskFileChangeCompleted(
                1,
                "root",
                "physical-1",
                "first-patch",
                "completed",
                ("goal.txt",),
            ),
            TaskTurnDiffUpdated(
                2,
                "root",
                "physical-1",
                native_diff("goal.txt", None, first),
            ),
            TaskTurnStarted(3, "root", "physical-2"),
            TaskFileChangeCompleted(
                4,
                "root",
                "physical-2",
                "second-patch",
                "completed",
                ("goal.txt",),
            ),
            TaskTurnDiffUpdated(
                5,
                "root",
                "physical-2",
                native_diff("goal.txt", first, final),
            ),
            TaskTurnCompleted(6, "root", "physical-2", "completed"),
        )

        composed = self.compose(
            events,
            root_turn_id="physical-2",
            include_prior_root_turns=True,
        )

        self.assertFalse(composed.complete)
        self.assertIn("lifecycle", composed.reason)

    def test_missing_child_turn_start_cannot_fall_back_to_parent_diff(self) -> None:
        final = "root\n"
        (self.cwd / "root.txt").write_text(final, encoding="utf-8")
        events = (
            TaskTurnStarted(0, "root", "root-turn"),
            TaskThreadStarted(1, "child", "root"),
            TaskFileChangeCompleted(
                2,
                "root",
                "root-turn",
                "root-patch",
                "completed",
                ("root.txt",),
            ),
            TaskTurnDiffUpdated(
                3,
                "root",
                "root-turn",
                native_diff("root.txt", None, final),
            ),
            TaskTurnCompleted(4, "root", "root-turn", "completed"),
        )

        composed = self.compose(events)

        self.assertFalse(composed.complete)
        self.assertIn("Turn start", composed.reason)


if __name__ == "__main__":
    unittest.main()
