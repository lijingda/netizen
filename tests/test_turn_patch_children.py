from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from openai_codex.generated.v2_all import ThreadItem, ThreadReadResponse, Turn

from netizen.turn_patch_children import (
    TaskPatchChildren,
    collect_turn_patch_children,
)


def _patch(item_id: str = "patch", *, status: str = "completed") -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "type": "fileChange",
            "id": item_id,
            "status": status,
            "changes": [
                {
                    "path": "/another-project/file.txt",
                    "kind": {"type": "add"},
                    "diff": "one\ntwo\n",
                }
            ],
        }
    )


def _collab(
    child_id: str,
    *,
    parent: str = "root",
    tool: str = "spawnAgent",
    status: str = "completed",
) -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "type": "collabAgentToolCall",
            "id": f"{parent}-{tool}-{child_id}",
            "senderThreadId": parent,
            "receiverThreadIds": [child_id],
            "agentsStates": {},
            "tool": tool,
            "status": status,
            "prompt": "private task prompt must not be retained in the result",
        }
    )


def _activity(child_id: str, *, kind: str = "started") -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "type": "subAgentActivity",
            "id": f"{kind}-{child_id}",
            "agentThreadId": child_id,
            "agentPath": f"/root/{child_id}",
            "kind": kind,
        }
    )


def _turn(
    turn_id: str,
    *items: ThreadItem,
    status: str = "completed",
    items_view: str = "full",
) -> Turn:
    return Turn.model_validate(
        {
            "id": turn_id,
            "status": status,
            "items": list(items),
            "itemsView": items_view,
        }
    )


def _view(
    thread_id: str,
    *turns: Turn,
    parent: str | None = None,
    cwd: str = "/project",
    forked_from: str | None = None,
) -> ThreadReadResponse:
    return ThreadReadResponse.model_validate(
        {
            "thread": {
                "id": thread_id,
                "cliVersion": "0.147.0",
                "createdAt": 1,
                "updatedAt": 2,
                "cwd": cwd,
                "ephemeral": False,
                "modelProvider": "openai",
                "preview": "",
                "sessionId": "session",
                "parentThreadId": parent,
                "forkedFromId": forked_from,
                "source": (
                    "appServer"
                    if parent is None
                    else {
                        "subAgent": {
                            "thread_spawn": {
                                "parent_thread_id": parent,
                                "depth": 1,
                            }
                        }
                    }
                ),
                "status": {"type": "idle"},
                "turns": list(turns),
            }
        }
    )


class TurnPatchChildrenTest(unittest.IsolatedAsyncioTestCase):
    async def _collect(
        self,
        items: list[ThreadItem],
        views: dict[str, ThreadReadResponse | Exception],
        *,
        root_final: ThreadReadResponse | Exception | None = None,
    ) -> tuple[TaskPatchChildren, list[str]]:
        calls = []
        codex = object()

        async def read(handle, *, include_turns=False):
            self.assertIs(handle._codex, codex)
            self.assertTrue(include_turns)
            calls.append(handle.id)
            value = views[handle.id]
            if handle.id == "root" and calls.count("root") > 1 and root_final is not None:
                value = root_final
            if isinstance(value, Exception):
                raise value
            return value

        with patch("netizen.turn_patch_children.AsyncThread.read", new=read):
            result = await collect_turn_patch_children(
                codex, thread_id="root", turn_id="root-turn", items=items
            )
        return result, calls

    async def test_no_child_does_not_read_root_or_keep_root_items(self) -> None:
        result, calls = await self._collect([_patch()], {})
        self.assertEqual(result, TaskPatchChildren())
        self.assertEqual(calls, [])

    async def test_v1_and_v2_children_keep_only_successful_file_changes(self) -> None:
        for edge in (_collab("child"), _activity("child")):
            with self.subTest(edge=edge.root.type):
                success = _patch()
                result, calls = await self._collect(
                    [edge],
                    {
                        "root": _view("root", _turn("root-turn", edge)),
                        "child": _view(
                            "child",
                            _turn(
                                "child-turn",
                                success,
                                _patch("failed", status="failed"),
                                _patch("declined", status="declined"),
                            ),
                            parent="root",
                            cwd="/other-project",
                        ),
                    },
                )
                self.assertTrue(result.complete)
                self.assertEqual(calls, ["root", "child", "root"])
                self.assertEqual(len(result.batches), 1)
                batch = result.batches[0]
                self.assertEqual(batch.thread_id, "child")
                self.assertEqual(batch.turn_id, "child-turn")
                self.assertEqual(batch.cwd, Path("/other-project"))
                self.assertEqual(batch.items, (success.root,))

    async def test_ancestor_turns_are_excluded_through_recursive_forks(self) -> None:
        spawn = _collab("child")
        root_history = _turn("old-root-turn", _patch("old-root"))
        root_turn = _turn("root-turn", _patch("root"), spawn)
        child_turn = _turn(
            "child-turn", _patch("child"), _collab("grandchild", parent="child")
        )
        result, calls = await self._collect(
            [spawn],
            {
                "root": _view("root", root_history, root_turn),
                "child": _view(
                    "child",
                    root_history,
                    root_turn,
                    child_turn,
                    parent="root",
                    forked_from="root",
                ),
                "grandchild": _view(
                    "grandchild",
                    root_history,
                    root_turn,
                    child_turn,
                    _turn("grandchild-turn", _patch("grandchild")),
                    parent="child",
                    forked_from="child",
                ),
            },
        )
        self.assertTrue(result.complete)
        self.assertEqual(calls, ["root", "child", "grandchild", "root"])
        self.assertEqual(
            [item.id for batch in result.batches for item in batch.items],
            ["child", "grandchild"],
        )

    async def test_spawn_duplicates_followups_and_sibling_messages_do_not_double_count(
        self,
    ) -> None:
        spawn = _collab("child")
        items = [
            spawn,
            spawn,
            _activity("child"),
            _collab("child", tool="sendInput"),
            _activity("child", kind="interacted"),
            _collab("other"),
        ]
        repeated_patch = _patch("first")
        result, calls = await self._collect(
            items,
            {
                "root": _view("root", _turn("root-turn", *items)),
                "child": _view(
                    "child",
                    _turn(
                        "first-turn",
                        repeated_patch,
                        repeated_patch,
                        _collab("other", parent="child", tool="sendInput"),
                    ),
                    _turn("followup-turn", _patch("second")),
                    parent="root",
                ),
                "other": _view("other", _turn("other-turn"), parent="root"),
            },
        )
        self.assertTrue(result.complete)
        self.assertEqual(calls, ["root", "child", "other", "root"])
        self.assertEqual(
            [item.id for batch in result.batches for item in batch.items],
            ["first", "second"],
        )

    async def test_old_child_references_are_partial_without_reading_history(self) -> None:
        edges = [
            _collab("old", tool="sendInput"),
            _collab("old", tool="wait"),
            _collab("old", tool="resumeAgent"),
            _activity("old", kind="interacted"),
        ]
        for edge in edges:
            with self.subTest(edge=edge):
                result, calls = await self._collect([edge], {})
                self.assertFalse(result.complete)
                self.assertEqual(result.batches, ())
                self.assertEqual(calls, [])

        items = [_collab("child"), *edges]
        result, calls = await self._collect(
            items,
            {
                "root": _view("root", _turn("root-turn", *items)),
                "child": _view(
                    "child", _turn("child-turn", _patch()), parent="root"
                ),
            },
        )
        self.assertFalse(result.complete)
        self.assertEqual(len(result.batches), 1)
        self.assertEqual(calls, ["root", "child", "root"])

    async def test_read_failure_preserves_other_children(self) -> None:
        items = [_collab("a"), _collab("b")]
        result, calls = await self._collect(
            items,
            {
                "root": _view("root", _turn("root-turn", *items)),
                "a": _view("a", _turn("a-turn", _patch()), parent="root"),
                "b": RuntimeError("deleted or ephemeral child"),
            },
        )
        self.assertFalse(result.complete)
        self.assertEqual([batch.thread_id for batch in result.batches], ["a"])
        self.assertEqual(calls, ["root", "a", "b", "root"])

    async def test_root_failure_or_newer_turn_prevents_guessing_child_history(self) -> None:
        spawn = _collab("child")
        cases = [
            RuntimeError("root read failed"),
            _view("root", _turn("different-turn")),
            _view("wrong-root", _turn("root-turn")),
            _view("root", _turn("root-turn"), _turn("new-turn")),
            _view("root", _turn("root-turn", items_view="summary")),
            _view("root", _turn("root-turn"), _turn("root-turn")),
        ]
        for root in cases:
            with self.subTest(root=root):
                result, calls = await self._collect([spawn], {"root": root})
                self.assertFalse(result.complete)
                self.assertEqual(result.batches, ())
                self.assertEqual(calls, ["root"])

    async def test_root_read_does_not_expand_the_supplied_turn_items(self) -> None:
        spawn = _collab("child")
        result, calls = await self._collect(
            [spawn],
            {
                "root": _view(
                    "root", _turn("root-turn", spawn, _collab("unobserved"))
                ),
                "child": _view("child", _turn("child-turn"), parent="root"),
            },
        )
        self.assertTrue(result.complete)
        self.assertEqual(calls, ["root", "child", "root"])

    async def test_root_rollover_or_failed_closing_read_discards_child_evidence(self) -> None:
        root = _view("root", _turn("root-turn"))
        finals = [
            _view("root", _turn("root-turn"), _turn("new-turn")),
            _view("root"),
            RuntimeError("closing root read failed"),
        ]
        for final in finals:
            with self.subTest(final=final):
                result, calls = await self._collect(
                    [_collab("child")],
                    {
                        "root": root,
                        "child": _view(
                            "child", _turn("child-turn", _patch()), parent="root"
                        ),
                    },
                    root_final=final,
                )
                self.assertFalse(result.complete)
                self.assertEqual(result.batches, ())
                self.assertEqual(calls, ["root", "child", "root"])

    async def test_parent_and_fork_identity_must_agree(self) -> None:
        spawn = _collab("child")
        good = _view("child", _turn("child-turn", _patch()), parent="root")
        conflicting = good.model_copy(deep=True)
        conflicting.thread.parent_thread_id = "other"
        cases = [
            _view("child", _turn("child-turn", _patch()), parent="other"),
            _view("child", _turn("child-turn", _patch())),
            _view("wrong-child", _turn("child-turn", _patch()), parent="root"),
            _view(
                "child",
                _turn("child-turn", _patch()),
                parent="root",
                forked_from="other",
            ),
            conflicting,
        ]
        for child in cases:
            with self.subTest(child=child):
                result, calls = await self._collect(
                    [spawn],
                    {"root": _view("root", _turn("root-turn")), "child": child},
                )
                self.assertFalse(result.complete)
                self.assertEqual(result.batches, ())
                self.assertEqual(calls, ["root", "child", "root"])

        source_only = good.model_copy(deep=True)
        source_only.thread.parent_thread_id = None
        result, _ = await self._collect(
            [spawn],
            {"root": _view("root", _turn("root-turn")), "child": source_only},
        )
        self.assertTrue(result.complete)
        self.assertEqual(len(result.batches), 1)

    async def test_running_or_partial_child_preserves_successful_patches(self) -> None:
        for status, items_view in [("inProgress", "full"), ("completed", "summary")]:
            with self.subTest(status=status, items_view=items_view):
                result, _ = await self._collect(
                    [_collab("child")],
                    {
                        "root": _view("root", _turn("root-turn")),
                        "child": _view(
                            "child",
                            _turn(
                                "child-turn",
                                _patch(),
                                _patch("pending", status="inProgress"),
                                status=status,
                                items_view=items_view,
                            ),
                            parent="root",
                        ),
                    },
                )
                self.assertFalse(result.complete)
                self.assertEqual(len(result.batches), 1)
                self.assertEqual([item.id for item in result.batches[0].items], ["patch"])

    async def test_failed_and_interrupted_child_turns_keep_applied_patches(self) -> None:
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                result, _ = await self._collect(
                    [_collab("child")],
                    {
                        "root": _view("root", _turn("root-turn")),
                        "child": _view(
                            "child",
                            _turn("child-turn", _patch(), status=status),
                            parent="root",
                        ),
                    },
                )
                self.assertTrue(result.complete)
                self.assertEqual(len(result.batches), 1)

    async def test_conflicting_duplicate_patch_identity_is_not_counted(self) -> None:
        original = _patch("conflict")
        conflicting = original.model_copy(deep=True)
        conflicting.root.changes[0].diff = "different content\n"
        result, _ = await self._collect(
            [_collab("child")],
            {
                "root": _view("root", _turn("root-turn")),
                "child": _view(
                    "child",
                    _turn(
                        "child-turn", original, conflicting, original, _patch("known")
                    ),
                    parent="root",
                ),
            },
        )
        self.assertFalse(result.complete)
        self.assertEqual([item.id for item in result.batches[0].items], ["known"])

    async def test_failed_spawn_is_not_read_and_pending_spawn_is_partial(self) -> None:
        for status, complete in [("failed", True), ("inProgress", False)]:
            with self.subTest(status=status):
                result, calls = await self._collect([_collab("child", status=status)], {})
                self.assertEqual(result.complete, complete)
                self.assertEqual(calls, [])

    async def test_cycle_and_conflicting_parent_do_not_read_a_thread_twice(self) -> None:
        items = [_collab("a"), _collab("b")]
        result, calls = await self._collect(
            items,
            {
                "root": _view("root", _turn("root-turn")),
                "a": _view(
                    "a",
                    _turn(
                        "a-turn",
                        _patch(),
                        _collab("root", parent="a"),
                        _collab("b", parent="a"),
                    ),
                    parent="root",
                ),
                "b": _view("b", _turn("b-turn"), parent="root"),
            },
        )
        self.assertFalse(result.complete)
        self.assertEqual(len(result.batches), 1)
        self.assertEqual(calls, ["root", "a", "b", "root"])

    async def test_empty_or_inherited_only_child_is_partial(self) -> None:
        for turns in [(), (_turn("root-turn", _patch()),)]:
            with self.subTest(turns=turns):
                result, _ = await self._collect(
                    [_collab("child")],
                    {
                        "root": _view("root", _turn("root-turn")),
                        "child": _view("child", *turns, parent="root"),
                    },
                )
                self.assertFalse(result.complete)
                self.assertEqual(result.batches, ())

    async def test_child_limit_bounds_reads_and_keeps_the_first_32(self) -> None:
        items = [_collab(f"child-{number:02}") for number in range(33)]
        views = {"root": _view("root", _turn("root-turn"))}
        views.update(
            {
                f"child-{number:02}": _view(
                    f"child-{number:02}",
                    _turn(f"child-turn-{number}", _patch()),
                    parent="root",
                )
                for number in range(33)
            }
        )
        result, calls = await self._collect(items, views)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.batches), 32)
        self.assertEqual(len(calls), 34)
        self.assertNotIn("child-32", calls)

    async def test_timeout_cancels_pending_read_and_discards_unconfirmed_child_evidence(
        self,
    ) -> None:
        calls = []
        cancelled = asyncio.Event()

        async def read(handle, *, include_turns=False):
            calls.append(handle.id)
            if handle.id == "root":
                return _view("root", _turn("root-turn"))
            if handle.id == "a":
                return _view("a", _turn("a-turn", _patch()), parent="root")
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch("netizen.turn_patch_children.AsyncThread.read", new=read),
            patch("netizen.turn_patch_children._READ_TIMEOUT_SECONDS", 0.01),
        ):
            result = await collect_turn_patch_children(
                object(),
                thread_id="root",
                turn_id="root-turn",
                items=[_collab("a"), _collab("b")],
            )
        self.assertFalse(result.complete)
        self.assertEqual(result.batches, ())
        self.assertEqual(calls, ["root", "a", "b"])
        self.assertTrue(cancelled.is_set())

    async def test_external_cancellation_is_not_swallowed(self) -> None:
        entered = asyncio.Event()

        async def read(handle, *, include_turns=False):
            entered.set()
            await asyncio.Event().wait()

        with patch("netizen.turn_patch_children.AsyncThread.read", new=read):
            task = asyncio.create_task(
                collect_turn_patch_children(
                    object(),
                    thread_id="root",
                    turn_id="root-turn",
                    items=[_collab("child")],
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_closing_root_read_shares_the_total_timeout(self) -> None:
        root_reads = 0

        async def read(handle, *, include_turns=False):
            nonlocal root_reads
            if handle.id == "child":
                return _view(
                    "child", _turn("child-turn", _patch()), parent="root"
                )
            root_reads += 1
            if root_reads == 1:
                return _view("root", _turn("root-turn"))
            await asyncio.Event().wait()

        with (
            patch("netizen.turn_patch_children.AsyncThread.read", new=read),
            patch("netizen.turn_patch_children._READ_TIMEOUT_SECONDS", 0.01),
        ):
            result = await collect_turn_patch_children(
                object(),
                thread_id="root",
                turn_id="root-turn",
                items=[_collab("child")],
            )
        self.assertFalse(result.complete)
        self.assertEqual(result.batches, ())
        self.assertEqual(root_reads, 2)


if __name__ == "__main__":
    unittest.main()
