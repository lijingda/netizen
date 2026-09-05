from __future__ import annotations

import inspect
import unittest
from unittest.mock import MagicMock

import openai_codex
import openai_codex.types as public_types
from openai_codex import AsyncCodex, AsyncThread, AsyncTurnHandle, SkillInput
from openai_codex.types import (
    ThreadItem,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
)

from openai_codex.generated.v2_all import (
    CollabAgentTool,
    CollabAgentToolCallThreadItem,
    SubAgentActivityKind,
    SubAgentActivityThreadItem,
    Thread,
    ThreadSpawnSubAgentSource,
    Turn,
)

from netizen.sdk_gap_adapter import facade_migration_requirements


class CodexSdkCapabilityContractTest(unittest.TestCase):
    def test_pinned_public_surface_supports_dynamic_turn_model_settings(self) -> None:
        self.assertEqual(openai_codex.__version__, "0.147.0")
        self.assertTrue(callable(AsyncCodex.models))
        parameters = inspect.signature(AsyncThread.turn).parameters
        self.assertTrue({"model", "effort", "service_tier"}.issubset(parameters))

    def test_public_compaction_has_public_persisted_completion_surface(self) -> None:
        self.assertTrue(callable(AsyncThread.compact))
        self.assertEqual(
            tuple(inspect.signature(AsyncThread.compact).parameters),
            ("self",),
        )
        self.assertIn(
            "include_turns",
            inspect.signature(AsyncThread.read).parameters,
        )

    def test_public_read_handle_construction_does_not_resume_or_subscribe(self) -> None:
        codex = MagicMock(spec=AsyncCodex)
        thread = AsyncThread(codex, "child-thread")
        self.assertEqual(thread.id, "child-thread")
        self.assertEqual(codex.mock_calls, [])
        self.assertEqual(
            tuple(inspect.signature(AsyncThread).parameters), ("_codex", "id")
        )
        self.assertTrue(inspect.iscoroutinefunction(AsyncThread.read))
        parameters = inspect.signature(AsyncThread.read).parameters
        self.assertEqual(tuple(parameters), ("self", "include_turns"))
        self.assertIs(parameters["include_turns"].default, False)
        self.assertEqual(
            parameters["include_turns"].kind, inspect.Parameter.KEYWORD_ONLY
        )

    def test_typed_child_provenance_and_both_agent_item_shapes_are_available(self) -> None:
        # New enum values require a deliberate decision about child provenance,
        # including the currently ignored close/interrupt operations.
        self.assertEqual(
            {tool.value for tool in CollabAgentTool},
            {"spawnAgent", "sendInput", "resumeAgent", "wait", "closeAgent"},
        )
        self.assertEqual(
            {kind.value for kind in SubAgentActivityKind},
            {"started", "interacted", "interrupted"},
        )
        collab = ThreadItem.model_validate(
            {
                "type": "collabAgentToolCall",
                "id": "spawn",
                "senderThreadId": "parent",
                "receiverThreadIds": ["child"],
                "agentsStates": {},
                "tool": "spawnAgent",
                "status": "completed",
            }
        ).root
        activity = ThreadItem.model_validate(
            {
                "type": "subAgentActivity",
                "id": "started",
                "agentThreadId": "child",
                "agentPath": "/root/child",
                "kind": "started",
            }
        ).root
        source = ThreadSpawnSubAgentSource.model_validate(
            {"thread_spawn": {"parent_thread_id": "parent", "depth": 1}}
        )
        self.assertIsInstance(collab, CollabAgentToolCallThreadItem)
        self.assertEqual(collab.sender_thread_id, "parent")
        self.assertEqual(collab.receiver_thread_ids, ["child"])
        self.assertEqual(collab.tool.value, "spawnAgent")
        self.assertIsInstance(activity, SubAgentActivityThreadItem)
        self.assertEqual(activity.agent_thread_id, "child")
        self.assertEqual(activity.kind.value, "started")
        self.assertEqual(source.thread_spawn.parent_thread_id.root, "parent")
        self.assertTrue(
            {"parent_thread_id", "forked_from_id", "source", "turns"}.issubset(
                Thread.model_fields
            )
        )
        self.assertTrue({"id", "status", "items", "items_view"}.issubset(Turn.model_fields))

    def test_public_thread_list_supports_native_title_display(self) -> None:
        self.assertTrue(callable(AsyncCodex.thread_list))
        parameters = inspect.signature(AsyncCodex.thread_list).parameters
        self.assertTrue({"archived", "cursor", "limit"}.issubset(parameters))

    def test_public_thread_lifecycle_surface_and_delete_gap_are_explicit(self) -> None:
        self.assertTrue(callable(AsyncThread.set_name))
        self.assertTrue(callable(AsyncCodex.thread_archive))
        self.assertTrue(callable(AsyncCodex.thread_unarchive))
        self.assertFalse(hasattr(AsyncCodex, "thread_delete"))
        self.assertFalse(hasattr(AsyncThread, "delete"))

    def test_public_ephemeral_fork_and_side_facade_gap_are_explicit(self) -> None:
        self.assertTrue(callable(AsyncCodex.thread_fork))
        fork_parameters = inspect.signature(AsyncCodex.thread_fork).parameters
        self.assertIn("ephemeral", fork_parameters)
        self.assertTrue(callable(AsyncTurnHandle.run))
        self.assertFalse(hasattr(AsyncCodex, "thread_inject_items"))
        self.assertFalse(hasattr(AsyncCodex, "thread_unsubscribe"))
        self.assertFalse(hasattr(AsyncThread, "inject_items"))
        self.assertFalse(hasattr(AsyncThread, "unsubscribe"))

    def test_public_turn_stream_exposes_context_window_usage(self) -> None:
        self.assertTrue(callable(AsyncTurnHandle.stream))
        self.assertEqual(
            set(ThreadTokenUsage.model_fields),
            {"last", "model_context_window", "total"},
        )
        self.assertEqual(
            set(ThreadTokenUsageUpdatedNotification.model_fields),
            {"thread_id", "token_usage", "turn_id"},
        )

    def test_public_turn_items_expose_supported_file_references(self) -> None:
        file_item = ThreadItem.model_validate(
            {
                "type": "fileChange",
                "id": "file-change",
                "status": "completed",
                "changes": [
                    {
                        "path": "report.xlsx",
                        "diff": "",
                        "kind": {"type": "update", "move_path": "final.xlsx"},
                    }
                ],
            }
        ).root
        image_item = ThreadItem.model_validate(
            {
                "type": "imageGeneration",
                "id": "image-generation",
                "status": "completed",
                "result": "generated",
                "savedPath": "/project/trend.png",
            }
        ).root

        self.assertEqual(file_item.type, "fileChange")
        self.assertEqual(file_item.changes[0].path, "report.xlsx")
        self.assertEqual(file_item.changes[0].kind.root.move_path, "final.xlsx")
        self.assertEqual(image_item.type, "imageGeneration")
        self.assertEqual(image_item.saved_path.root, "/project/trend.png")

    def test_plan_control_remains_an_explicit_gap(self) -> None:
        turn_parameters = inspect.signature(AsyncThread.turn).parameters
        self.assertNotIn("collaboration_mode", turn_parameters)
        self.assertTrue(
            {"plan", "set_plan", "collaboration_mode"}.isdisjoint(
                name for name in dir(AsyncThread) if not name.startswith("_")
            )
        )
        self.assertFalse(hasattr(public_types, "TurnPlanUpdatedNotification"))
        self.assertTrue(
            {"notifications", "subscribe", "turn_plan"}.isdisjoint(
                name for name in dir(AsyncCodex) if not name.startswith("_")
            )
        )

    def test_background_terminal_cleanup_remains_a_high_level_gap(self) -> None:
        public_names = {
            name for name in dir(AsyncCodex) if not name.startswith("_")
        }
        self.assertFalse(
            any("terminal" in name.lower() for name in public_names)
        )

    def test_gap_adapter_capabilities_still_require_facade_migration(self) -> None:
        codex_methods = {
            name for name in dir(AsyncCodex) if not name.startswith("_")
        }
        thread_methods = {
            name for name in dir(AsyncThread) if not name.startswith("_")
        }
        self.assertTrue(
            {
                "goal",
                "goal_get",
                "goal_set",
                "goal_clear",
                "thread_goal_get",
                "thread_goal_set",
                "thread_goal_clear",
            }.isdisjoint(
                codex_methods | thread_methods
            )
        )
        self.assertTrue(
            {"skill", "skills", "skill_list", "skills_list"}.isdisjoint(
                codex_methods
            )
        )
        self.assertTrue(
            {"app", "apps", "app_list", "apps_list"}.isdisjoint(
                codex_methods
            )
        )

        # Structured attachment is public, but without public discovery Netizen
        # has no trustworthy name -> path mapping to build this input itself.
        self.assertEqual(
            tuple(inspect.signature(SkillInput).parameters),
            ("name", "path"),
        )
        self.assertEqual(facade_migration_requirements(), ())


if __name__ == "__main__":
    unittest.main()
