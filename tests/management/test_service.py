from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen_cli.bindings import (
    BindingContextRevisionConflict,
    BindingFeedbackRevisionConflict,
    BindingNotFound,
    BindingQuery,
    BindingSettingsRevisionConflict,
    BindingStore,
    SideTopicState,
    SideTopicQuery,
    SideTopicInventoryRecord,
)
from netizen_cli.channel_app import ChannelApplication
from netizen_cli.codex_runtime import (
    ActiveGoalSnapshot,
    ActiveTurnSnapshot,
    BindingRuntimeSnapshot,
    CodexRuntime,
    NativeThreadCatalogState,
    NativeThreadCatalog,
    NativeThreadCatalogPage,
    NativeThreadMetadata,
    ReleaseDisposition,
    SideLifecycleOutcome,
    SideSessionNotFound,
    SideSessionSnapshot,
    SideSessionState,
    StopDisposition,
    ThreadLifecycleSnapshot,
    ThreadLifecycleState,
    ThreadLifecycleError,
    ThreadSubscriptionSnapshot,
    ThreadSubscriptionState,
    ThreadArchived,
    ThreadCatalogError,
    ThreadResumeNotFound,
    ThreadDeleteTargetChanged,
)
from netizen_cli.domain import (
    ActiveState,
    FeishuScope,
    GoalOperationState,
    GoalStatus,
    NativeCapability,
    MentionContextMode,
    MessageContextAnchor,
    ScopeKind,
)
from netizen_cli.management import (
    ActivePointerChanged,
    BindingScopeMismatch,
    ChatLabel,
    CurrentBindingChanged,
    CurrentBindingTarget,
    CurrentSideTarget,
    ExactBindingTarget,
    InstanceManagementService,
    ManagementRuntimePort,
    NativeThreadMissing,
    RuntimePrecondition,
    RuntimeStateChanged,
    ScopeCoordinator,
    SessionInventoryState,
    SessionQuery,
    SessionNativeCursor,
    SideIdentityMismatch,
    SideTopicInventoryItem,
    classify_native_thread_view,
)
from netizen_cli.management.chat_directory import AvailableChat, AvailableChatPage, ChatDirectoryError
from netizen_cli.chat_targets import ValidatedChatTarget
from netizen_cli.projects import ProjectRegistry
from netizen_cli.model_settings import ModelCatalogError
from netizen_cli.session_settings import BindingTaskFeedback, BindingTurnSettings, SessionSettingsError
from netizen_cli.sdk_gap_adapter import GoalControlError, GoalSnapshot
from netizen_cli.management.service import _project_binding_status


class FakeManagementRuntime:
    def __init__(self, store: BindingStore) -> None:
        self.native_delete_available = True
        self.store = store
        self.calls: list[tuple[object, ...]] = []
        self.archived: set[str] = set()
        self.missing: set[str] = set()
        self.rename_entered: asyncio.Event | None = None
        self.rename_release: asyncio.Event | None = None
        self.archive_entered: asyncio.Event | None = None
        self.archive_release: asyncio.Event | None = None
        self.side_missing = False
        self.active_metadata: dict[str, NativeThreadMetadata] = {}
        self.archived_metadata: dict[str, NativeThreadMetadata] = {}
        self.summary_metadata: dict[str, NativeThreadMetadata] = {}
        self.side_snapshots: dict[str, object] = {}
        self.runtime_snapshots: dict[str, BindingRuntimeSnapshot] = {}
        self.goal_snapshots: dict[str, GoalSnapshot | None] = {}
        self.goal_snapshot_after: dict[str, BindingRuntimeSnapshot] = {}
        self.goal_snapshot_errors: set[str] = set()
        self.goal_snapshot_gate: asyncio.Event | None = None
        self.goal_snapshot_eight_entered: asyncio.Event | None = None
        self.goal_snapshot_concurrency = 0
        self.goal_snapshot_max_concurrency = 0

    async def configure_context_exact(self, **values):
        self.calls.append(("configure", values["binding_id"]))
        return self.store.set_configuration(**values)

    async def resolve_turn_settings(self, **values):
        self.calls.append(("resolve-settings", values))
        return BindingTurnSettings(**values)

    async def activate_exact(self, binding_id: str, *, context_anchor=None):
        self.calls.append(("activate", binding_id))
        binding = self.store.get(binding_id)
        if binding.native_thread_id in self.archived:
            raise ThreadArchived("archived")
        if binding.native_thread_id in self.missing:
            raise ThreadResumeNotFound("missing")
        return self.store.activate(
            scope_key=binding.scope_key,
            binding_id=binding.id,
            context_anchor=context_anchor,
        )

    async def rename_exact(self, binding_id: str, name: str) -> str:
        self.calls.append(("rename", binding_id))
        if self.rename_entered is not None:
            self.rename_entered.set()
        if self.rename_release is not None:
            await self.rename_release.wait()
        return " ".join(name.split())

    async def archive_exact(self, binding_id: str):
        self.calls.append(("archive", binding_id))
        if self.archive_entered is not None:
            self.archive_entered.set()
        if self.archive_release is not None:
            await self.archive_release.wait()
        binding = self.store.get(binding_id)
        assert binding.native_thread_id is not None
        self.archived.add(binding.native_thread_id)
        return self.store.archive_binding(binding.id)

    async def restore_exact(self, binding_id: str):
        self.calls.append(("restore", binding_id))
        binding = self.store.get(binding_id)
        assert binding.native_thread_id is not None
        self.archived.discard(binding.native_thread_id)
        return binding

    async def restore_as_current_exact(self, binding_id: str):
        self.calls.append(("restore-current", binding_id))
        binding = await self.restore_exact(binding_id)
        return self.store.activate(
            scope_key=binding.scope_key,
            binding_id=binding.id,
        )

    async def delete_lazy_exact(self, binding_id: str):
        self.calls.append(("delete", binding_id))
        binding = self.store.get(binding_id)
        assert binding.native_thread_id is None
        return self.store.delete_binding(binding_id)

    async def delete_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str | None,
    ):
        binding = self.store.get(binding_id)
        if binding.native_thread_id != expected_native_thread_id:
            raise AssertionError("unexpected native Thread identity")
        self.calls.append(("delete", binding_id))
        return self.store.delete_binding(binding_id)

    async def delete_archived_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str,
    ):
        if expected_native_thread_id not in self.archived:
            raise AssertionError("expected an archived native Thread")
        self.calls.append(
            ("delete-archived", binding_id, expected_native_thread_id)
        )
        return self.store.delete_binding(binding_id)

    async def stop_exact(
        self,
        binding_id: str,
        *,
        acknowledge=None,
        expected_activity_revision: int | None = None,
        expected_turn_id: str | None = None,
    ):
        if expected_activity_revision is not None:
            snapshot = self.runtime_snapshot_exact(binding_id)
            if not isinstance(snapshot, BindingRuntimeSnapshot) or (
                snapshot.activity_revision != expected_activity_revision
                or (
                    snapshot.turn.turn_id if snapshot.turn is not None else None
                )
                != expected_turn_id
            ):
                raise RuntimeStateChanged("runtime state changed")
        self.calls.append(("stop", binding_id))
        if acknowledge is not None:
            await acknowledge()
        return StopDisposition.REQUESTED

    async def recheck_turn_exact(
        self,
        binding_id: str,
        *,
        expected_activity_revision: int,
        expected_turn_id: str,
    ):
        self.calls.append(
            (
                "recheck",
                binding_id,
                expected_activity_revision,
                expected_turn_id,
            )
        )
        return SimpleNamespace(turn_id=expected_turn_id)

    async def release_exact(self, binding_id: str):
        self.calls.append(("release", binding_id))
        return ReleaseDisposition.NOT_SUBSCRIBED

    async def close_side_exact(self, side_id: str, *, state: SideTopicState):
        self.calls.append(("close-side", side_id, state))
        if self.side_missing:
            raise SideSessionNotFound(side_id)
        record = self.store.transition_side_topic(side_id, state)
        return SideLifecycleOutcome(side_id=side_id, state=record.state)

    async def stop_side(self, side_id, **values):
        self.calls.append(("stop-side", side_id, values))
        return StopDisposition.REQUESTED

    async def binding_pointer_changed(self, previous, current) -> None:
        self.calls.append(("pointer", previous, current))

    async def thread_metadata_exact(
        self,
        thread_ids: tuple[str, ...],
        *,
        archived: bool,
        deadline: float,
        use_state_db_only: bool | None = None,
    ):
        self.calls.append(("metadata", archived, thread_ids, deadline, use_state_db_only))
        source = self.archived_metadata if archived else self.active_metadata
        return {thread_id: source[thread_id] for thread_id in thread_ids if thread_id in source}

    async def thread_summary_exact(self, thread_id: str):
        self.calls.append(("summary", thread_id))
        if thread_id not in self.summary_metadata:
            raise RuntimeError("summary unavailable")
        return self.summary_metadata[thread_id]

    async def thread_catalog_exact(
        self, *, archived: bool, deadline: float, use_state_db_only=None,
        sort_key=None, sort_direction=None,
    ):
        self.calls.append(("catalog", archived, deadline, use_state_db_only))
        if sort_key is not None or sort_direction is not None:
            self.calls.append(("catalog-order", archived, sort_key, sort_direction))
        source = self.archived_metadata if archived else self.active_metadata
        return NativeThreadCatalog(archived, tuple(source.values()))

    async def thread_catalog_page_exact(
        self, *, archived: bool, cursor=None, limit=20, sort_key="updated_at",
        sort_direction="desc", deadline: float,
    ):
        self.calls.append(("catalog-page", archived, cursor, limit, sort_key, sort_direction))
        source = self.archived_metadata if archived else self.active_metadata
        ordered = sorted(
            source.values(),
            key=lambda row: (getattr(row, sort_key) or 0, row.thread_id),
            reverse=sort_direction == "desc",
        )
        offset = int(cursor) if cursor is not None else 0
        selected = tuple(ordered[offset:offset + limit])
        next_offset = offset + len(selected)
        return NativeThreadCatalogPage(
            selected, str(next_offset) if next_offset < len(ordered) else None,
        )

    def runtime_snapshot_exact(self, binding_id: str):
        self.calls.append(("runtime-snapshot", binding_id))
        return self.runtime_snapshots.get(
            binding_id,
            BindingRuntimeSnapshot(
                binding_id,
                0,
                None,
                None,
                False,
                None,
                None,
                None,
            ),
        )

    async def goal_snapshot_exact(self, binding) -> GoalSnapshot | None:
        self.calls.append(("goal-snapshot", binding.id))
        if binding.id in self.goal_snapshot_errors:
            raise GoalControlError("unavailable")
        self.goal_snapshot_concurrency += 1
        self.goal_snapshot_max_concurrency = max(
            self.goal_snapshot_max_concurrency,
            self.goal_snapshot_concurrency,
        )
        if self.goal_snapshot_concurrency == 8 and self.goal_snapshot_eight_entered is not None:
            self.goal_snapshot_eight_entered.set()
        try:
            if self.goal_snapshot_gate is not None:
                await self.goal_snapshot_gate.wait()
            after = self.goal_snapshot_after.get(binding.id)
            if after is not None:
                self.runtime_snapshots[binding.id] = after
            return self.goal_snapshots.get(binding.id)
        finally:
            self.goal_snapshot_concurrency -= 1

    def side_snapshot_exact(self, side_id: str):
        self.calls.append(("side-snapshot", side_id))
        return self.side_snapshots.get(side_id)

    def side_ids_for_projects(self, project_aliases):
        self.calls.append(("side-project-ids", project_aliases))
        return tuple(
            side_id for side_id, snapshot in self.side_snapshots.items()
            if snapshot.project_alias in project_aliases
        )

    def project_has_fork_creation(self, alias: str) -> bool:
        return False

    def project_side_snapshots(self, alias: str, *, limit: int = 1000):
        self.calls.append(("project-side-snapshots", alias))
        snapshots = tuple(
            snapshot for snapshot in self.side_snapshots.values()
            if getattr(snapshot, "project_alias", None) == alias
        )
        if len(snapshots) > limit:
            raise ValueError("Project Side snapshot exceeds the limit")
        return snapshots


class FakeChatLabels:
    def __init__(self) -> None:
        self.info_calls: list[str] = []

    async def get_chat_info(self, chat_id: str):
        self.info_calls.append(chat_id)
        return SimpleNamespace(
            name=f"Name {chat_id}",
            chat_mode="group",
            chat_type="private",
        )

    async def get_chat_members(self, _chat_id: str, **_kwargs):
        return []


class InstanceManagementServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        project = root / "project"
        project.mkdir()
        ids = iter(
            (
                "aaaaaaaa-0000-0000-0000-000000000001",
                "bbbbbbbb-0000-0000-0000-000000000002",
                "cccccccc-0000-0000-0000-000000000003",
                "dddddddd-0000-0000-0000-000000000004",
                "eeeeeeee-0000-0000-0000-000000000005",
            )
        )
        self.store = BindingStore(id_factory=lambda: next(ids))
        self.projects = ProjectRegistry(
            store=self.store,
            project_root=root,
            projects={"test": project},
        )
        self.runtime = FakeManagementRuntime(self.store)
        self.coordinator = ScopeCoordinator()
        self.service = InstanceManagementService(
            bindings=self.store,
            projects=self.projects,
            runtime=self.runtime,  # type: ignore[arg-type]
            scope_coordinator=self.coordinator,
        )
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.other_scope = FeishuScope("cli_test", "oc_other", ScopeKind.DIRECT)

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_live_chat_queries_use_injected_read_port(self) -> None:
        chat = AvailableChat("oc_group", "群聊", "group", False)
        page = AvailableChatPage((chat,), "more")
        target = ValidatedChatTarget("oc_direct", "p2p")
        directory = SimpleNamespace(query=AsyncMock(return_value=page), validate=AsyncMock(return_value=chat),
                                    validate_target=AsyncMock(return_value=target))
        service = InstanceManagementService(
            bindings=self.store, projects=self.projects, runtime=self.runtime,
            scope_coordinator=self.coordinator, chat_directory=directory,
        )
        try:
            self.assertIs(await service.query_available_chats(query="群", page_token="first"), page)
            self.assertIs(await service.validate_available_chat("oc_group"), chat)
            self.assertIs(await service.validate_chat_target("oc_direct"), target)
            directory.query.assert_awaited_once_with(query="群", page_token="first", page_size=20)
            await service.query_available_chats(query="群", page_token="more", page_size=10)
            directory.query.assert_awaited_with(query="群", page_token="more", page_size=10)
            directory.validate.assert_awaited_once_with("oc_group")
            directory.validate_target.assert_awaited_once_with("oc_direct")
        finally:
            await service.close()

    async def test_missing_chat_directory_is_unavailable_not_an_empty_directory(self) -> None:
        with self.assertRaises(ChatDirectoryError) as caught:
            await self.service.query_available_chats()
        self.assertEqual(caught.exception.code, "chat_query_unavailable")

        with self.assertRaises(ChatDirectoryError) as caught:
            await self.service.validate_chat_target("oc_direct")
        self.assertEqual(caught.exception.code, "chat_query_unavailable")
        with self.assertRaises(ChatDirectoryError) as caught:
            await self.service.validate_available_chat("oc_group")
        self.assertEqual(caught.exception.code, "chat_query_unavailable")

    async def _create(self, scope: FeishuScope | None = None):
        return (
            await self.service.create_current_binding(
                scope=scope or self.scope,
                creator_id="ou_user",
                project_alias="test",
            )
        ).binding

    async def test_simultaneous_scheduled_cwd_checks_wait_for_bounded_io(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        original = self.projects.resolve_for_new

        def resolve(alias):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise TimeoutError("test filesystem gate")
            return original(alias)

        with patch.object(self.projects, "resolve_for_new", side_effect=resolve):
            tasks = [asyncio.create_task(self.service.resolve_new_project(
                "test", deadline=loop.time() + 3,
            )) for _ in range(3)]
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertTrue(all(not task.done() for task in tasks))
            finally:
                release.set()
                results = await asyncio.gather(*tasks)
        self.assertEqual([project.alias for project in results], ["test"] * 3)

    async def test_native_delete_availability_uses_the_narrow_runtime_projection(
        self,
    ) -> None:
        self.assertTrue(self.service.native_delete_available)
        self.runtime.native_delete_available = False
        self.assertFalse(self.service.native_delete_available)

    async def test_current_target_rejects_stale_binding_before_runtime(self) -> None:
        first = await self._create()
        second = await self._create()
        before = tuple(self.runtime.calls)

        with self.assertRaises(CurrentBindingChanged):
            await self.service.rename_current_binding(
                target=CurrentBindingTarget(self.scope.key, first.id),
                name="stale",
            )

        self.assertEqual(tuple(self.runtime.calls), before)
        self.assertEqual(self.store.active_binding(self.scope.key).id, second.id)

    async def _configure_binding(self, binding, **overrides):
        active = self.store.active_binding(binding.scope_key)
        return await self.service.configure_exact_binding(**{
            "target": ExactBindingTarget(binding.scope_key, binding.id, active.id if active else None),
            "expected_settings_revision": binding.settings_revision,
            "expected_context_revision": binding.context_revision,
            "expected_feedback_revision": binding.feedback_revision,
            "settings": binding.turn_settings,
            "task_feedback": binding.task_feedback,
            "message_context_mode": binding.message_context_mode,
            **overrides,
        })

    async def test_complete_configuration_updates_inactive_binding_without_switching(self):
        first = await self._create()
        current = await self._create()
        settings = BindingTurnSettings("model", "high", "priority")
        feedback = BindingTaskFeedback(True, True, False)

        configured = await self._configure_binding(first, settings=settings, task_feedback=feedback)

        self.assertEqual(configured.turn_settings, settings)
        self.assertEqual(configured.task_feedback, feedback)
        self.assertEqual((configured.settings_revision, configured.context_revision, configured.feedback_revision), (2, 1, 2))
        self.assertFalse(configured.active)
        self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)
        self.assertEqual(await self._configure_binding(configured), configured)

    async def test_complete_configuration_rejects_each_stale_revision_without_mutation(self):
        binding = await self._create()
        before = tuple(self.runtime.calls)
        for field, error in (
            ("expected_settings_revision", BindingSettingsRevisionConflict),
            ("expected_context_revision", BindingContextRevisionConflict),
            ("expected_feedback_revision", BindingFeedbackRevisionConflict),
        ):
            with self.subTest(field=field), self.assertRaises(error):
                await self._configure_binding(binding, **{field: 2}, task_feedback=BindingTaskFeedback(True, True, False))
            self.assertEqual(self.store.get(binding.id), binding)
            self.assertEqual(tuple(self.runtime.calls), before)

    async def test_complete_configuration_rejects_changed_pointer_and_wrong_scope(self):
        binding = await self._create()
        stale = ExactBindingTarget(binding.scope_key, binding.id, binding.id)
        current = await self._create()
        other = await self._create(self.other_scope)
        before = tuple(self.runtime.calls)
        with self.assertRaises(ActivePointerChanged):
            await self._configure_binding(binding, target=stale)
        with self.assertRaises(BindingScopeMismatch):
            await self._configure_binding(binding, target=ExactBindingTarget(other.scope_key, binding.id, other.id))
        self.assertEqual(tuple(self.runtime.calls), before)
        self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)

    async def test_complete_configuration_retains_or_clears_exact_catch_up_boundary(self):
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        anchor = MessageContextAnchor("om_boundary", 1000)
        binding = self.store.create_channel_binding(
            scope=scope, project_alias="test", creator_id="ou_user",
            message_context_mode=MentionContextMode.CATCH_UP, context_anchor=anchor,
        )
        configured = await self._configure_binding(binding, task_feedback=BindingTaskFeedback(True, True, False))
        self.assertEqual(configured.context_anchor, anchor)
        self.assertEqual(configured.context_revision, binding.context_revision)
        cleared = await self._configure_binding(configured, message_context_mode=MentionContextMode.CURRENT_ONLY)
        self.assertIsNone(cleared.context_anchor)
        self.assertEqual(cleared.context_revision, binding.context_revision + 1)
        self.assertEqual(cleared.task_feedback, configured.task_feedback)

    async def test_admin_cannot_enable_catch_up_without_message_boundary(self):
        for scope in (
            self.scope,
            FeishuScope("cli_test", "oc_group", ScopeKind.GROUP),
            FeishuScope("cli_test", "oc_group", ScopeKind.TOPIC, "omt_topic"),
        ):
            with self.subTest(kind=scope.kind):
                binding = await self._create(scope)
                before = tuple(self.runtime.calls)
                with self.assertRaisesRegex(SessionSettingsError, "/config"):
                    await self._configure_binding(
                        binding, settings=BindingTurnSettings("model", "high", "priority"),
                        task_feedback=BindingTaskFeedback(True, True, False), message_context_mode=MentionContextMode.CATCH_UP,
                    )
                self.assertEqual(self.store.get(binding.id), binding)
                self.assertEqual(tuple(self.runtime.calls), before)

    async def test_existing_model_can_save_feedback_when_catalog_is_unavailable(self):
        settings = BindingTurnSettings("saved-model", "high", "priority")
        binding = self.store.create_channel_binding(
            scope=self.scope, project_alias="test", creator_id="ou_user", turn_settings=settings,
        )
        with patch.object(self.runtime, "resolve_turn_settings", AsyncMock(side_effect=ModelCatalogError("unavailable"))) as resolve:
            configured = await self._configure_binding(binding, task_feedback=BindingTaskFeedback(True, True, False))
            resolve.assert_not_called()
            with self.assertRaises(ModelCatalogError):
                await self._configure_binding(configured, settings=BindingTurnSettings("new-model", "high", "priority"))
            self.assertEqual(self.store.get(binding.id), configured)
            inherited = await self._configure_binding(configured, settings=None)
            self.assertIsNone(inherited.turn_settings)
            self.assertEqual(inherited.task_feedback, configured.task_feedback)

    async def test_exact_inactive_rename_and_archive_preserve_other_pointer(self) -> None:
        first = await self._create()
        self.store.assign_native_thread_id(first.id, "native-first")
        second = await self._create()
        target = ExactBindingTarget(
            scope_key=self.scope.key,
            binding_id=first.id,
            expected_active_binding_id=second.id,
        )

        renamed = await self.service.rename_exact_binding(
            target=target,
            name="  inactive   thread  ",
        )
        archived = await self.service.archive_exact_binding(target=target)

        self.assertEqual(renamed.name, "inactive thread")
        self.assertEqual(archived.id, first.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, second.id)
        self.assertNotIn(("pointer", second.id, second.id), self.runtime.calls)

    async def test_active_archive_clears_pointer_and_notifies_after_commit(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-active")

        archived = await self.service.archive_current_binding(
            target=CurrentBindingTarget(self.scope.key, binding.id)
        )

        self.assertEqual(archived.id, binding.id)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        self.assertEqual(self.runtime.calls[-1], ("pointer", binding.id, None))

    async def test_exact_archive_ignores_stale_scope_pointer_projection(
        self,
    ) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-active")
        other = await self._create()

        archived = await self.service.archive_exact_binding(
            target=ExactBindingTarget(
                self.scope.key,
                binding.id,
                "stale-pointer",
            ),
        )

        self.assertEqual(archived.id, binding.id)
        self.assertIn(("archive", binding.id), self.runtime.calls)
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)

    async def test_native_archive_does_not_hold_the_scope_lock(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-active")
        self.runtime.archive_entered = asyncio.Event()
        self.runtime.archive_release = asyncio.Event()

        archiving = asyncio.create_task(
            self.service.archive_exact_binding(
                target=ExactBindingTarget(
                    self.scope.key,
                    binding.id,
                    binding.id,
                )
            )
        )
        await self.runtime.archive_entered.wait()

        replacement = await asyncio.wait_for(self._create(), timeout=0.1)
        self.assertEqual(
            self.store.active_binding(self.scope.key).id,
            replacement.id,
        )
        self.runtime.archive_release.set()
        archived = await archiving

        self.assertEqual(archived.id, binding.id)
        self.assertEqual(
            self.store.active_binding(self.scope.key).id,
            replacement.id,
        )
        self.assertEqual(
            self.runtime.calls.count(("pointer", binding.id, replacement.id)),
            1,
        )

    async def test_current_delete_requires_exact_native_identity(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-one")

        with self.assertRaises(CurrentBindingChanged):
            await self.service.delete_current_binding(
                target=CurrentBindingTarget(self.scope.key, binding.id),
                expected_native_thread_id=None,
            )

        deleted = await self.service.delete_current_binding(
            target=CurrentBindingTarget(self.scope.key, binding.id),
            expected_native_thread_id="native-one",
        )

        self.assertEqual(deleted.id, binding.id)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        self.assertEqual(
            self.runtime.calls[-2:],
            [("delete", binding.id), ("pointer", binding.id, None)],
        )

    async def test_exact_inactive_delete_preserves_other_pointer(self) -> None:
        target = await self._create()
        self.store.assign_native_thread_id(target.id, "native-target")
        current = await self._create()

        deleted = await self.service.delete_exact_binding(
            target=ExactBindingTarget(
                scope_key=self.scope.key,
                binding_id=target.id,
                expected_active_binding_id=current.id,
            ),
            expected_native_thread_id="native-target",
        )

        self.assertEqual(deleted.id, target.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)
        self.assertEqual(self.runtime.calls[-1], ("delete", target.id))
        self.assertNotIn(("pointer", current.id, current.id), self.runtime.calls)

    async def test_materialized_delete_ignores_stale_scope_pointer_projection(
        self,
    ) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-active")
        other = await self._create()

        deleted = await self.service.delete_exact_binding(
            target=ExactBindingTarget(
                self.scope.key,
                binding.id,
                "stale-pointer",
            ),
            expected_native_thread_id="native-active",
        )

        self.assertEqual(deleted.id, binding.id)
        self.assertIn(("delete", binding.id), self.runtime.calls)
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)

    async def test_archived_delete_and_recheck_forward_exact_preconditions(
        self,
    ) -> None:
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        self.store.archive_binding(archived.id)
        self.runtime.archived.add("native-archived")

        await self.service.delete_archived_exact_binding(
            target=ExactBindingTarget(self.scope.key, archived.id, None),
            expected_native_thread_id="native-archived",
        )
        self.assertIn(
            ("delete-archived", archived.id, "native-archived"),
            self.runtime.calls,
        )

        current = await self._create()
        await self.service.recheck_exact_turn(
            target=ExactBindingTarget(
                self.scope.key,
                current.id,
                current.id,
            ),
            runtime_precondition=RuntimePrecondition(9, "turn-nine"),
        )
        self.assertIn(
            ("recheck", current.id, 9, "turn-nine"),
            self.runtime.calls,
        )

    async def test_exact_current_lazy_delete_clears_pointer(self) -> None:
        target = await self._create()

        deleted = await self.service.delete_exact_binding(
            target=ExactBindingTarget(
                scope_key=self.scope.key,
                binding_id=target.id,
                expected_active_binding_id=target.id,
            ),
            expected_native_thread_id=None,
        )

        self.assertEqual(deleted.id, target.id)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        self.assertEqual(
            self.runtime.calls[-2:],
            [("delete", target.id), ("pointer", target.id, None)],
        )

    async def test_exact_delete_rejects_native_identity_change_before_runtime(
        self,
    ) -> None:
        target = await self._create()
        self.store.assign_native_thread_id(target.id, "native-target")
        before = tuple(self.runtime.calls)

        with self.assertRaises(ThreadDeleteTargetChanged):
            await self.service.delete_exact_binding(
                target=ExactBindingTarget(
                    scope_key=self.scope.key,
                    binding_id=target.id,
                    expected_active_binding_id=target.id,
                ),
                expected_native_thread_id=None,
            )

        self.assertEqual(tuple(self.runtime.calls), before)
        self.assertEqual(
            self.store.get(target.id).native_thread_id,
            "native-target",
        )

    async def test_exact_delete_rejects_cross_scope_target_before_runtime(
        self,
    ) -> None:
        current = await self._create()
        other = await self._create(self.other_scope)
        before = tuple(self.runtime.calls)

        with self.assertRaises(BindingScopeMismatch):
            await self.service.delete_exact_binding(
                target=ExactBindingTarget(
                    scope_key=self.scope.key,
                    binding_id=other.id,
                    expected_active_binding_id=current.id,
                ),
                expected_native_thread_id=None,
            )

        self.assertEqual(tuple(self.runtime.calls), before)
        self.assertEqual(self.store.get(other.id).id, other.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)

    async def test_exact_pointer_precondition_distinguishes_none(self) -> None:
        binding = await self._create()

        with self.assertRaises(ActivePointerChanged):
            await self.service.delete_exact_lazy_binding(
                target=ExactBindingTarget(
                    scope_key=self.scope.key,
                    binding_id=binding.id,
                    expected_active_binding_id=None,
                )
            )

        self.assertEqual(self.store.get(binding.id).id, binding.id)

    async def test_both_activation_entries_map_native_resume_not_found_and_keep_current(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-missing")
        current = await self._create()
        self.runtime.missing.add("native-missing")

        for exact in (False, True):
            with self.subTest(exact=exact):
                self.runtime.calls.clear()
                with self.assertRaisesRegex(NativeThreadMissing, "可恢复记录"):
                    if exact:
                        await self.service.activate_exact_binding(
                            target=ExactBindingTarget(self.scope.key, binding.id, current.id),
                        )
                    else:
                        await self.service.resume_current_binding(
                            scope_key=self.scope.key, reference=binding.short_id,
                        )
                self.assertEqual(self.runtime.calls, [("activate", binding.id)])
                self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)
                self.assertEqual(self.store.get(binding.id).native_thread_id, "native-missing")

    async def test_both_activation_entries_use_runtime_without_catalog_preflight(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-unlisted")
        current = await self._create()
        for exact in (False, True):
            with self.subTest(exact=exact):
                self.store.activate(scope_key=self.scope.key, binding_id=current.id)
                self.runtime.calls.clear()
                if exact:
                    activated = await self.service.activate_exact_binding(
                        target=ExactBindingTarget(self.scope.key, binding.id, current.id),
                    )
                else:
                    activated = await self.service.resume_current_binding(
                        scope_key=self.scope.key, reference=binding.short_id,
                    )
                self.assertEqual(activated.id, binding.id)
                self.assertEqual(self.runtime.calls, [
                    ("activate", binding.id), ("pointer", current.id, binding.id),
                ])

    async def test_both_activation_entries_keep_archived_rejection_explicit(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-archived")
        current = await self._create()
        self.runtime.archived.add("native-archived")
        for exact in (False, True):
            with self.subTest(exact=exact):
                self.runtime.calls.clear()
                with self.assertRaises(ThreadArchived):
                    if exact:
                        await self.service.activate_exact_binding(
                            target=ExactBindingTarget(self.scope.key, binding.id, current.id),
                        )
                    else:
                        await self.service.resume_current_binding(
                            scope_key=self.scope.key, reference=binding.short_id,
                        )
                self.assertEqual(self.runtime.calls, [("activate", binding.id)])
                self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)

    async def test_name_writer_wait_does_not_block_same_or_other_scope(self) -> None:
        first = await self._create()
        other = await self._create(self.other_scope)
        self.runtime.rename_entered = asyncio.Event()
        self.runtime.rename_release = asyncio.Event()

        rename = asyncio.create_task(
            self.service.rename_current_binding(
                target=CurrentBindingTarget(self.scope.key, first.id),
                name="held",
            )
        )
        await self.runtime.rename_entered.wait()
        same_scope = asyncio.create_task(self._create())
        other_scope = asyncio.create_task(
            self.service.release_current_binding(
                target=CurrentBindingTarget(self.other_scope.key, other.id)
            )
        )

        await asyncio.wait_for(other_scope, timeout=1)
        replacement = await asyncio.wait_for(same_scope, timeout=1)
        self.assertFalse(rename.done())
        self.runtime.rename_release.set()
        renamed = await asyncio.wait_for(rename, timeout=1)
        self.assertEqual(renamed.binding.id, first.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, replacement.id)

    async def test_session_native_page_hydrates_only_selected_catalog_rows(self) -> None:
        materialized = await self._create()
        self.store.assign_native_thread_id(materialized.id, "native-active")
        lazy = await self._create()
        metadata = NativeThreadMetadata(
            "native-active", "Active title", "preview", created_at=10, updated_at=20,
        )
        self.runtime.active_metadata[metadata.thread_id] = metadata
        self.runtime.calls.clear()

        with (
            patch.object(self.store, "query_binding_candidates", side_effect=AssertionError("full local scan")),
            patch.object(self.runtime, "thread_catalog_exact", side_effect=AssertionError("full native scan")),
            patch.object(self.runtime, "thread_summary_exact", side_effect=AssertionError("unexpected exact read")),
        ):
            page = await self.service.query_sessions(
                limit=20, deadline=asyncio.get_running_loop().time() + 1,
            )

        self.assertEqual([item.record.binding.id for item in page.items], [materialized.id])
        self.assertIs(page.items[0].native.metadata, metadata)
        self.assertIs(page.items[0].native.state, NativeThreadCatalogState.ACTIVE)
        self.assertNotIn(lazy.id, [item.record.binding.id for item in page.items])
        self.assertEqual(self.runtime.calls, [("catalog-page", False, None, 100, "updated_at", "desc")])

    async def test_session_native_time_order_and_cursor_preserve_all_page_tails(self) -> None:
        self.store._id_factory = lambda: str(uuid.uuid4())
        candidates = []
        for index in range(43):
            binding = await self._create(self.scope if index < 37 else self.other_scope)
            native_id = f"ordered-{index:02}"
            self.store.assign_native_thread_id(binding.id, native_id)
            # Native times deliberately disagree with local registration order;
            # ties straddle pages and must retain the native cursor's order.
            metadata = NativeThreadMetadata(
                native_id, f"Title {index}", "preview",
                created_at=1000 + (index * 13 % 17),
                updated_at=2000 + (index * 17 % 11),
            )
            self.runtime.active_metadata[native_id] = metadata
            if index < 37:
                candidates.append((binding.id, metadata))

        for sort_by in ("created_at", "updated_at"):
            for direction in ("asc", "desc"):
                with self.subTest(sort_by=sort_by, direction=direction):
                    self.runtime.calls.clear()
                    query = SessionQuery(
                        local=BindingQuery(chat_id=self.scope.chat_id),
                        sort_by=sort_by, sort_direction=direction,
                    )
                    cursor = None
                    items = []
                    pages = []
                    for _ in range(4):
                        page = await self.service.query_sessions(
                            query=query, cursor=cursor, limit=20,
                            deadline=asyncio.get_running_loop().time() + 2,
                        )
                        pages.append(page)
                        items.extend(page.items)
                        cursor = page.next_cursor
                        if cursor is None:
                            break
                        self.assertIsInstance(cursor, SessionNativeCursor)
                    expected = sorted(
                        candidates,
                        key=lambda row: (getattr(row[1], sort_by), row[1].thread_id),
                        reverse=direction == "desc",
                    )
                    self.assertEqual([item.record.binding.id for item in items], [row[0] for row in expected])
                    self.assertEqual([len(page.items) for page in pages], [20, 17])
                    self.assertIsNone(cursor)
                    self.assertTrue(all(call[1] is False and call[4:] == (sort_by, direction)
                                        for call in self.runtime.calls if call[0] == "catalog-page"))
                    self.assertFalse(any(call[0] in {"catalog", "metadata", "summary"} for call in self.runtime.calls))

    async def test_session_dense_native_page_reuses_raw_page_offset_without_losing_tail(self) -> None:
        self.store._id_factory = lambda: str(uuid.uuid4())
        bindings = []
        for index in range(125):
            binding = await self._create()
            native_id = f"dense-{index:03}"
            self.store.assign_native_thread_id(binding.id, native_id)
            self.runtime.active_metadata[native_id] = NativeThreadMetadata(native_id, None, "preview", updated_at=index)
            bindings.append(binding)
        self.runtime.calls.clear()
        first = await self.service.query_sessions(limit=20, deadline=asyncio.get_running_loop().time() + 2)
        self.assertEqual([item.record.binding.id for item in first.items], [binding.id for binding in reversed(bindings[105:])])
        self.assertEqual(self.runtime.calls, [("catalog-page", False, None, 100, "updated_at", "desc")])
        self.assertIsInstance(first.next_cursor, SessionNativeCursor)
        self.assertIsNone(first.next_cursor.cursor)
        self.assertEqual(first.next_cursor.offset, 20)
        items = list(first.items)
        cursor = first.next_cursor
        pages = 1
        while cursor is not None:
            page = await self.service.query_sessions(cursor=cursor, limit=20, deadline=asyncio.get_running_loop().time() + 2)
            pages += 1
            items.extend(page.items)
            cursor = page.next_cursor
            if pages == 2:
                self.assertIsNone(cursor.cursor)
                self.assertEqual(cursor.offset, 40)
            if pages == 5:
                self.assertEqual(cursor.cursor, "100")
                self.assertEqual(cursor.offset, 0)
            self.assertLessEqual(pages, 7)
        self.assertEqual(pages, 7)
        self.assertEqual([item.record.binding.id for item in items], [binding.id for binding in reversed(bindings)])
        self.assertTrue(all(call[3] == 100 for call in self.runtime.calls if call[0] == "catalog-page"))

    async def test_session_sparse_native_pages_fill_without_extra_match_lookahead(self) -> None:
        selected = []
        for index in range(3):
            binding = await self._create()
            native_id = f"sparse-{index}"
            self.store.assign_native_thread_id(binding.id, native_id)
            self.runtime.active_metadata[native_id] = NativeThreadMetadata(native_id, None, "preview", updated_at=100 - index * 3)
            selected.append(binding)
        for index, updated in enumerate((99, 98, 96, 95, 93, 92)):
            native_id = f"other-instance-{index}"
            self.runtime.active_metadata[native_id] = NativeThreadMetadata(native_id, None, "preview", updated_at=updated)
        self.runtime.calls.clear()
        first = await self.service.query_sessions(limit=2, deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual([item.record.binding.id for item in first.items], [item.id for item in selected[:2]])
        calls = [call for call in self.runtime.calls if call[0] == "catalog-page"]
        self.assertEqual([call[3] for call in calls], [100])
        self.assertIsNone(first.next_cursor.cursor)
        self.assertEqual(first.next_cursor.offset, 4)
        second = await self.service.query_sessions(cursor=first.next_cursor, limit=2, deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual([item.record.binding.id for item in second.items], [selected[2].id])
        self.assertIsNone(second.next_cursor)

    async def test_session_sparse_tail_uses_full_native_batches(self) -> None:
        self.store._id_factory = lambda: str(uuid.uuid4())
        bindings = []
        for index in range(19):
            binding = await self._create()
            native_id = f"sparse-tail-{index:05}"
            self.store.assign_native_thread_id(binding.id, native_id)
            bindings.append(binding)
        for index in range(10019):
            native_id = f"sparse-tail-{index:05}"
            self.runtime.active_metadata[native_id] = NativeThreadMetadata(
                native_id, None, "preview", updated_at=20000 - index,
            )
        self.runtime.calls.clear()
        page = await self.service.query_sessions(limit=20, deadline=asyncio.get_running_loop().time() + 10)
        self.assertEqual([item.record.binding.id for item in page.items], [binding.id for binding in bindings])
        self.assertIsNone(page.next_cursor)
        calls = [call for call in self.runtime.calls if call[0] == "catalog-page"]
        self.assertEqual(len(calls), 101)
        self.assertEqual({call[3] for call in calls}, {100})

    async def test_session_cursor_may_continue_to_an_empty_filtered_page(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "mine")
        self.runtime.active_metadata = {
            "mine": NativeThreadMetadata("mine", None, "preview", updated_at=20),
            "other": NativeThreadMetadata("other", None, "preview", updated_at=10),
        }
        first = await self.service.query_sessions(limit=1, deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(len(first.items), 1)
        self.assertIsNotNone(first.next_cursor)
        second = await self.service.query_sessions(cursor=first.next_cursor, limit=1, deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(second.items, ())
        self.assertIsNone(second.next_cursor)

    async def test_session_catalog_selection_is_single_and_archived_is_independent(self) -> None:
        for archived, state in ((False, SessionInventoryState.ACTIVE), (True, SessionInventoryState.ARCHIVED)):
            binding = await self._create()
            native_id = state.value
            self.store.assign_native_thread_id(binding.id, native_id)
            source = self.runtime.archived_metadata if archived else self.runtime.active_metadata
            source[native_id] = NativeThreadMetadata(native_id, state.value, "preview", created_at=10, updated_at=20)
        for archived, state in ((False, SessionInventoryState.ACTIVE), (True, SessionInventoryState.ARCHIVED)):
            with self.subTest(state=state):
                self.runtime.calls.clear()
                page = await self.service.query_sessions(
                    query=SessionQuery(inventory_state=state), limit=20,
                    deadline=asyncio.get_running_loop().time() + 1,
                )
                self.assertEqual([item.record.binding.native_thread_id for item in page.items], [state.value])
                self.assertIs(page.items[0].inventory_state, state)
                self.assertEqual(self.runtime.calls, [("catalog-page", archived, None, 100, "updated_at", "desc")])

    async def test_session_legacy_empty_preview_rows_are_not_backfilled(self) -> None:
        indexed = await self._create()
        legacy = await self._create()
        self.store.assign_native_thread_id(indexed.id, "indexed")
        self.store.assign_native_thread_id(legacy.id, "legacy")
        self.runtime.active_metadata["indexed"] = NativeThreadMetadata("indexed", "Known", "preview", updated_at=10)
        self.runtime.summary_metadata["legacy"] = NativeThreadMetadata("legacy", "Readable", "", updated_at=100)
        with patch.object(self.runtime, "thread_summary_exact", side_effect=AssertionError("legacy backfill")):
            page = await self.service.query_sessions(deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual([item.record.binding.id for item in page.items], [indexed.id])
        self.assertIsNone(page.next_cursor)
        self.assertIsNotNone(self.store.get(legacy.id))

    async def test_session_lazy_pages_keep_local_order_and_ignore_native_time_filters(self) -> None:
        first = await self._create()
        materialized = await self._create()
        self.store.assign_native_thread_id(materialized.id, "native")
        second = await self._create()
        self.runtime.calls.clear()
        query = SessionQuery(
            inventory_state=SessionInventoryState.LAZY,
            created_from=200, created_before=300, updated_from=200, updated_before=300,
        )
        first_page = await self.service.query_sessions(query=query, limit=1, deadline=asyncio.get_running_loop().time() + 1)
        second_page = await self.service.query_sessions(query=query, cursor=first_page.next_cursor, limit=1, deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual([item.record.binding.id for item in first_page.items], [second.id])
        self.assertEqual([item.record.binding.id for item in second_page.items], [first.id])
        self.assertTrue(all(item.native is None for item in (*first_page.items, *second_page.items)))
        self.assertIsNone(second_page.next_cursor)
        self.assertEqual(self.runtime.calls, [])

    async def test_session_native_creation_filter_does_not_use_binding_registration(self) -> None:
        older_native = await self._create()
        newer_native = await self._create()
        for binding, native_id, created, updated in (
            (older_native, "older-native", 10, 300),
            (newer_native, "newer-native", 30, 100),
        ):
            self.store.assign_native_thread_id(binding.id, native_id)
            self.runtime.active_metadata[native_id] = NativeThreadMetadata(
                native_id, None, "preview", created_at=created, updated_at=updated,
            )
        page = await self.service.query_sessions(
            query=SessionQuery(created_from=10, created_before=30),
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.record.binding.id for item in page.items], [older_native.id])

    async def test_session_creation_sort_does_not_stop_on_old_update_time(self) -> None:
        bindings = []
        for index, updated in enumerate((10, 100, 20)):
            binding = await self._create()
            native_id = f"created-{index}"
            self.store.assign_native_thread_id(binding.id, native_id)
            self.runtime.active_metadata[native_id] = NativeThreadMetadata(
                native_id, None, "preview", created_at=300 - index * 100, updated_at=updated,
            )
            bindings.append(binding)
        page = await self.service.query_sessions(
            query=SessionQuery(sort_by="created_at", updated_from=50), limit=1,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.record.binding.id for item in page.items], [bindings[1].id])
        self.assertIsNotNone(page.next_cursor)

    async def test_session_sorted_time_bounds_stop_only_after_matching_axis_is_exhausted(self) -> None:
        bindings = {}
        for timestamp in (1020, 1030, 1040):
            binding = await self._create()
            native_id = f"bounded-{timestamp:03}"
            self.store.assign_native_thread_id(binding.id, native_id)
            bindings[timestamp] = binding
        for sort_by in ("created_at", "updated_at"):
            for direction in ("asc", "desc"):
                # Keep an unconsumed native page past the requested time range.
                # Both ordering directions must stop without loading that page.
                times = range(1000, 1200) if direction == "asc" else range(860, 1060)
                self.runtime.active_metadata = {
                    f"bounded-{timestamp:03}": NativeThreadMetadata(
                        f"bounded-{timestamp:03}", None, "preview",
                        created_at=timestamp, updated_at=timestamp,
                    ) for timestamp in times
                }
                self.runtime.calls.clear()
                lower, upper = (1020, 1040) if direction == "asc" else (1030, 1050)
                query = SessionQuery(
                    sort_by=sort_by, sort_direction=direction,
                    **{f"{sort_by.removesuffix('_at')}_from": lower,
                       f"{sort_by.removesuffix('_at')}_before": upper},
                )
                with self.subTest(sort_by=sort_by, direction=direction):
                    page = await self.service.query_sessions(query=query, limit=3, deadline=asyncio.get_running_loop().time() + 2)
                    self.assertEqual([item.native.metadata.updated_at for item in page.items], [1020, 1030] if direction == "asc" else [1040, 1030])
                    self.assertIsNone(page.next_cursor)
                    self.assertEqual(len([call for call in self.runtime.calls if call[0] == "catalog-page"]), 1)

    async def test_session_native_page_failure_never_returns_partial_rows(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "mine")
        metadata = NativeThreadMetadata("mine", None, "preview", updated_at=20)
        with patch.object(self.runtime, "thread_catalog_page_exact", side_effect=[
            NativeThreadCatalogPage((metadata,), "more"),
            ThreadCatalogError("unavailable"),
        ]):
            with self.assertRaises(ThreadCatalogError):
                await self.service.query_sessions(limit=2, deadline=asyncio.get_running_loop().time() + 1)
        self.assertFalse(any(call[0] == "summary" for call in self.runtime.calls))

    async def test_session_native_query_expired_deadline_skips_catalog(self) -> None:
        self.runtime.calls.clear()
        with self.assertRaises(TimeoutError):
            await self.service.query_sessions(deadline=asyncio.get_running_loop().time() - 1)
        self.assertEqual(self.runtime.calls, [])

    async def test_session_pages_resolve_only_page_cache_misses(self) -> None:
        for index in range(3):
            await self._create(
                FeishuScope(
                    "cli_test",
                    f"oc_page_{index}",
                    ScopeKind.DIRECT,
                )
            )
        labels = FakeChatLabels()
        service = InstanceManagementService(
            bindings=self.store,
            projects=self.projects,
            runtime=self.runtime,  # type: ignore[arg-type]
            scope_coordinator=self.coordinator,
            chat_labels=labels,
        )
        try:
            first = await service.query_sessions(
                query=SessionQuery(inventory_state=SessionInventoryState.LAZY),
                limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            first_chat_ids = tuple(item.record.scope.chat_id for item in first.items)
            self.assertEqual(tuple(labels.info_calls), first_chat_ids)

            await service.query_sessions(
                query=SessionQuery(inventory_state=SessionInventoryState.LAZY),
                limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            self.assertEqual(tuple(labels.info_calls), first_chat_ids)

            second = await service.query_sessions(
                query=SessionQuery(inventory_state=SessionInventoryState.LAZY),
                cursor=first.next_cursor,
                limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            second_chat_ids = tuple(
                item.record.scope.chat_id for item in second.items
            )
            self.assertEqual(
                tuple(labels.info_calls),
                first_chat_ids + second_chat_ids,
            )
        finally:
            await service.close()

    async def test_scope_native_pages_preserve_scope_order_and_current_entry(self) -> None:
        self.store._id_factory = lambda: str(uuid.uuid4())
        other_project = self.projects.register(alias="another", path=None, create_directory=True)
        bindings = []
        for index in range(4):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, f"native-{index}")
            self.runtime.active_metadata[f"native-{index}"] = NativeThreadMetadata(
                f"native-{index}", f"Title {index}", "", updated_at=index,
            )
            bindings.append(binding)
        cross_project = self.store.create_channel_binding(
            scope=self.scope, project_alias=other_project.alias, creator_id="user",
        )
        self.store.assign_native_thread_id(cross_project.id, "cross-project")
        self.runtime.active_metadata["cross-project"] = NativeThreadMetadata(
            "cross-project", "Other Project", "", updated_at=10,
        )
        outside = await self._create(self.other_scope)
        self.store.assign_native_thread_id(outside.id, "outside")
        self.runtime.active_metadata["outside"] = NativeThreadMetadata("outside", None, "", updated_at=20)
        self.store.activate(scope_key=self.scope.key, binding_id=bindings[0].id)
        self.runtime.summary_metadata["native-0"] = self.runtime.active_metadata["native-0"]
        self.runtime.calls.clear()
        with patch.object(self.store, "list_bindings", side_effect=AssertionError("full local scan")):
            first = await self.service.query_scope_sessions(
                scope=self.scope, limit=2, deadline=asyncio.get_running_loop().time() + 1,
            )
            second = await self.service.query_scope_sessions(
                scope=self.scope, cursor=first.next_cursor, limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            third = await self.service.query_scope_sessions(
                scope=self.scope, cursor=second.next_cursor, limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
        self.assertEqual(
            [item.record.binding.native_thread_id for page in (first, second, third) for item in page.items],
            ["cross-project", "native-3", "native-2", "native-1", "native-0"],
        )
        self.assertTrue(all(page.current.record.binding.id == bindings[0].id for page in (first, second, third)))
        self.assertIsNone(third.next_cursor)
        self.assertTrue(all(item.inventory_state is SessionInventoryState.ACTIVE for page in (first, second, third) for item in page.items))
        self.assertFalse(any(call[0] in {"catalog", "metadata"} for call in self.runtime.calls))
        self.assertEqual([call for call in self.runtime.calls if call[0] == "summary"], [("summary", "native-0")] * 2)

    async def test_scope_archived_page_is_single_state_without_current_enrichment(self) -> None:
        for index in range(3):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, f"archived-{index}")
            self.runtime.archived_metadata[f"archived-{index}"] = NativeThreadMetadata(
                f"archived-{index}", "Archived", "", updated_at=index,
            )
        self.runtime.calls.clear()
        first = await self.service.query_scope_sessions(
            scope=self.scope, archived=True, limit=2, deadline=asyncio.get_running_loop().time() + 1,
        )
        second = await self.service.query_scope_sessions(
            scope=self.scope, archived=True, cursor=first.next_cursor, limit=2,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.record.binding.native_thread_id for item in (*first.items, *second.items)], ["archived-2", "archived-1", "archived-0"])
        self.assertIsNone(first.current)
        self.assertIsNone(second.next_cursor)
        self.assertTrue(all(call[0] == "catalog-page" and call[1] is True for call in self.runtime.calls))

    async def test_scope_catalog_failure_is_explicit_and_unknown_current_remains_exact(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "omitted")
        self.runtime.summary_metadata["omitted"] = NativeThreadMetadata("omitted", "Known ID", "")
        page = await self.service.query_scope_sessions(scope=self.scope, deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(page.items, ())
        self.assertEqual(page.current.record.binding.id, binding.id)
        self.assertEqual(page.current.inventory_state, SessionInventoryState.UNKNOWN)
        self.assertEqual(page.current.native.metadata.name, "Known ID")
        for archived in (False, True):
            with self.subTest(archived=archived), patch.object(
                self.runtime, "thread_catalog_page_exact", side_effect=ThreadCatalogError("unavailable"),
            ):
                with self.assertRaises(ThreadCatalogError):
                    await self.service.query_scope_sessions(
                        scope=self.scope, archived=archived, deadline=asyncio.get_running_loop().time() + 1,
                    )

    async def test_empty_and_lazy_scope_pages_do_not_read_native(self) -> None:
        self.runtime.calls.clear()
        empty = await self.service.query_scope_sessions(scope=self.scope, deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(empty.items, ())
        self.assertIsNone(empty.current)
        self.assertFalse(empty.has_lazy)
        bindings = [await self._create() for _ in range(3)]
        self.runtime.calls.clear()
        pages = []
        cursor = None
        for _ in range(2):
            page = await self.service.query_scope_sessions(
                scope=self.scope, lazy=True, cursor=cursor, limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            pages.append(page)
            cursor = page.next_cursor
        self.assertEqual({item.record.binding.id for page in pages for item in page.items}, {binding.id for binding in bindings})
        self.assertTrue(all(page.has_lazy for page in pages))
        self.assertTrue(all(page.current.record.binding.id == bindings[-1].id for page in pages))
        self.assertIsNone(cursor)
        self.assertEqual(self.runtime.calls, [])
        active = await self.service.query_scope_sessions(scope=self.scope, deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(active.items, ())
        self.assertEqual(active.current.inventory_state, SessionInventoryState.LAZY)
        self.assertTrue(active.has_lazy)

    async def test_session_query_accepts_one_hundred_only(self) -> None:
        page = await self.service.query_sessions(
            limit=100,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual(page.items, ())
        with self.assertRaises(ValueError):
            await self.service.query_sessions(
                limit=101,
                deadline=asyncio.get_running_loop().time() + 1,
            )

    async def test_project_options_include_disabled_and_paginate_without_native_reads(self) -> None:
        project = self.projects.register(alias="disabled", path=None, create_directory=True)
        self.projects.set_enabled(alias=project.alias, enabled=False, expected_revision=project.revision)
        self.runtime.calls.clear()
        first = await self.service.query_project_options(
            limit=1, deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.project.alias for item in first.items], ["disabled"])
        self.assertFalse(first.items[0].project.enabled)
        second = await self.service.query_project_options(
            limit=1, cursor=first.next_cursor, deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.project.alias for item in second.items], ["test"])
        self.assertIsNone(second.next_cursor)
        self.assertEqual(self.runtime.calls, [])

    async def test_exact_summary_reads_share_worker_concurrency(self) -> None:
        self.store._id_factory = lambda: str(uuid.uuid4())
        bindings = []
        for index in range(8):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, f"unindexed-{index}")
            bindings.append(binding)
        entered = asyncio.Event()
        release = asyncio.Event()
        concurrency = 0
        maximum = 0

        async def read(thread_id):
            nonlocal concurrency, maximum
            concurrency += 1
            maximum = max(maximum, concurrency)
            if concurrency == 4:
                entered.set()
            try:
                await release.wait()
                return NativeThreadMetadata(thread_id, "Summary", "")
            finally:
                concurrency -= 1

        with patch.object(self.runtime, "thread_summary_exact", side_effect=read):
            queries = [asyncio.create_task(self.service.binding_metadata_exact(
                binding.id, deadline=asyncio.get_running_loop().time() + 5,
            )) for binding in bindings]
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual(maximum, 4)
            finally:
                release.set()
                results = await asyncio.gather(*queries)
        self.assertTrue(all(item.name == "Summary" for item in results))
        self.assertLessEqual(maximum, 4)

    async def test_binding_metadata_exact_reads_original_binding_without_switching(self) -> None:
        parent = await self._create()
        self.store.assign_native_thread_id(parent.id, "original-parent")
        current = await self._create()
        self.store.assign_native_thread_id(current.id, "current-thread")
        parent = self.store.get(parent.id)
        metadata = NativeThreadMetadata("original-parent", "Parent name", "preview")
        self.runtime.summary_metadata[metadata.thread_id] = metadata
        self.runtime.calls.clear()

        result = await self.service.binding_metadata_exact(
            parent.id, deadline=asyncio.get_running_loop().time() + 1,
        )

        self.assertIs(result, metadata)
        self.assertEqual(self.runtime.calls, [("summary", "original-parent")])
        self.assertEqual(self.store.get(parent.id), parent)
        self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)

    async def test_binding_metadata_lazy_and_missing_bindings_do_not_read_native(self) -> None:
        binding = await self._create()
        self.runtime.calls.clear()
        deadline = asyncio.get_running_loop().time() + 1

        self.assertIsNone(await self.service.binding_metadata_exact(binding.id, deadline=deadline))
        with self.assertRaises(BindingNotFound):
            await self.service.binding_metadata_exact("missing-binding", deadline=deadline)

        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.store.get(binding.id), binding)

    async def test_binding_metadata_read_failure_preserves_binding(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "unavailable-summary")
        binding = self.store.get(binding.id)
        self.runtime.calls.clear()

        result = await self.service.binding_metadata_exact(
            binding.id, deadline=asyncio.get_running_loop().time() + 1,
        )

        self.assertIsNone(result)
        self.assertEqual(self.runtime.calls, [("summary", "unavailable-summary")])
        self.assertEqual(self.store.get(binding.id), binding)

    async def test_binding_metadata_deadline_and_cancellation_preserve_pending_read(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "parent-thread")
        metadata = NativeThreadMetadata("parent-thread", "Parent name", "")
        loop = asyncio.get_running_loop()

        for wait_result in ("deadline", "cancelled"):
            entered = asyncio.Event()
            release = asyncio.Event()

            async def read(thread_id):
                self.assertEqual(thread_id, "parent-thread")
                entered.set()
                await release.wait()
                return metadata

            with self.subTest(wait_result=wait_result), patch.object(
                self.runtime, "thread_summary_exact", side_effect=read,
            ):
                query = asyncio.create_task(self.service.binding_metadata_exact(
                    binding.id, deadline=loop.time() + (0.5 if wait_result == "deadline" else 5),
                ))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    pending = tuple(self.service._summary_reads)
                    self.assertEqual(len(pending), 1)
                    if wait_result == "deadline":
                        self.assertIsNone(await asyncio.wait_for(query, 1))
                    else:
                        query.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await query

                    self.assertEqual(self.service._summary_reads, set(pending))
                    self.assertTrue(all(not task.done() for task in pending))
                    release.set()
                    self.assertEqual(await asyncio.wait_for(asyncio.gather(*pending), 1), [metadata])
                    self.assertEqual(self.service._summary_reads, set())
                finally:
                    release.set()
                    await asyncio.wait_for(asyncio.gather(
                        query, *self.service._summary_reads, return_exceptions=True,
                    ), 1)

    async def test_summary_failures_are_isolated(self) -> None:
        bindings = []
        for thread_id in ("healthy", "failed"):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, thread_id)
            bindings.append(binding)

        async def read(thread_id):
            if thread_id == "failed":
                raise RuntimeError("summary unavailable")
            return NativeThreadMetadata(thread_id, "Healthy summary", "")

        with patch.object(self.runtime, "thread_summary_exact", side_effect=read):
            healthy, failed = await asyncio.gather(*(self.service.binding_metadata_exact(
                binding.id, deadline=asyncio.get_running_loop().time() + 2,
            ) for binding in bindings))
        self.assertEqual(healthy.name, "Healthy summary")
        self.assertIsNone(failed)

    async def test_summary_timeouts_keep_workers_counted(self) -> None:
        bindings = []
        for index in range(5):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, f"unindexed-{index}")
            bindings.append(binding)
        release = threading.Event()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        reads = []

        def blocking_read(thread_id):
            release.wait()
            return NativeThreadMetadata(thread_id, "Summary", "", updated_at=42)

        async def read(thread_id):
            reads.append(thread_id)
            if len(reads) == 4:
                entered.set()
            return await asyncio.to_thread(blocking_read, thread_id)

        queries = []
        try:
            with patch.object(self.runtime, "thread_summary_exact", side_effect=read):
                queries = [asyncio.create_task(self.service.binding_metadata_exact(
                    binding.id, deadline=loop.time() + 3,
                )) for binding in bindings[:4]]
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual(await asyncio.gather(*queries), [None] * 4)
                self.assertIsNone(await self.service.binding_metadata_exact(bindings[4].id, deadline=loop.time() + 0.1))
                self.assertEqual(len(reads), 4)
                self.assertEqual(len(self.service._summary_reads), 4)
                release.set()
                await asyncio.wait_for(asyncio.gather(*self.service._summary_reads), 1)
                metadata = await self.service.binding_metadata_exact(bindings[4].id, deadline=loop.time() + 2)
                self.assertEqual(metadata.updated_at, 42)
        finally:
            release.set()
            await asyncio.gather(*queries, *self.service._summary_reads, return_exceptions=True)

    async def test_summary_shutdown_preserves_pending_workers_and_rejects_new_reads(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "unindexed")
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        reads = []

        def blocking_read(thread_id):
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return NativeThreadMetadata(thread_id, "Summary", "")

        async def read(thread_id):
            reads.append(thread_id)
            return await asyncio.to_thread(blocking_read, thread_id)

        with patch.object(self.runtime, "thread_summary_exact", side_effect=read):
            # Request expiry is covered separately; keep it outside the
            # bounded event waits that establish this shutdown scenario.
            query = asyncio.create_task(
                self.service.binding_metadata_exact(binding.id, deadline=loop.time() + 30),
            )
            try:
                # Observe the real worker entering before closing admission.
                await asyncio.wait_for(entered.wait(), 5)
                pending = set(self.service._summary_reads)
                self.assertEqual(len(pending), 1)
                # Thread-pool shutdown is not the behavior under test. Let it
                # finish normally, with an outer timeout only to catch hangs.
                await asyncio.wait_for(self.service.close(), 5)
                self.assertEqual(self.service._summary_reads, pending)
                self.assertTrue(all(not task.done() for task in pending))
                self.assertFalse(query.done())

                release.set()
                page = await asyncio.wait_for(query, 5)
                self.assertEqual(page.name, "Summary")
                self.assertEqual(self.service._summary_reads, set())
                # Recheck with free concurrency slots, so saturation cannot
                # conceal a regression in the closed-admission check.
                page = await self.service.binding_metadata_exact(binding.id, deadline=loop.time() + 5)
                self.assertEqual(reads, ["unindexed"])
                self.assertIsNone(page)
            finally:
                release.set()
                await asyncio.wait_for(
                    asyncio.gather(query, *self.service._summary_reads, return_exceptions=True),
                    5,
                )

    async def test_projects_use_local_counts_without_native_catalog_dependencies(self) -> None:
        for thread_id in ("active", "archived", "unindexed"):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, thread_id)
        await self._create()
        self.runtime.calls.clear()
        with (
            patch.object(self.runtime, "thread_catalog_exact", side_effect=AssertionError("full native catalog")),
            patch.object(self.store, "project_aliases_for_native_threads", side_effect=AssertionError("catalog join")),
        ):
            page = await self.service.query_projects(deadline=asyncio.get_running_loop().time() + 1)
        item = page.items[0]
        self.assertEqual(item.aggregate.binding_count, 4)
        self.assertEqual(item.aggregate.materialized_binding_count, 3)
        self.assertEqual(item.aggregate.lazy_binding_count, 1)
        self.assertFalse(item.deleting)
        self.assertFalse(self.runtime.calls)

    async def test_runtime_snapshot_request_is_bounded_and_reports_missing_side(
        self,
    ) -> None:
        binding = await self._create()

        snapshots = self.service.runtime_snapshots(
            binding_ids=(binding.id,),
            side_ids=("side-missing",),
        )

        self.assertEqual(snapshots.bindings[0].binding_id, binding.id)
        self.assertIsNone(snapshots.bindings[0].primary_status)
        self.assertEqual(
            snapshots.bindings[0].primary_status_resolution.value,
            "deferred",
        )
        self.assertNotIn(("goal-snapshot", binding.id), self.runtime.calls)
        self.assertEqual(snapshots.sides, ())
        self.assertEqual(snapshots.missing_side_ids, ("side-missing",))
        with self.assertRaises(ValueError):
            self.service.runtime_snapshots(binding_ids=(binding.id, binding.id))

    async def test_primary_status_uses_one_canonical_priority(self) -> None:
        binding = await self._create()
        persisted = GoalSnapshot(
            "native-goal",
            "ship it",
            GoalStatus.PAUSED,
            None,
            10,
            2,
            1,
            2,
        )
        turn = ActiveTurnSnapshot(
            binding.id,
            "native-goal",
            "turn-1",
            "ou_user",
            ActiveState.RUNNING,
        )
        goal = ActiveGoalSnapshot(
            binding.id,
            "native-goal",
            "goal-1",
            "ou_user",
            GoalOperationState.RUNNING,
            persisted,
        )
        lifecycle = ThreadLifecycleSnapshot(
            binding.id,
            "native-goal",
            ThreadLifecycleState.ARCHIVING,
        )
        cases = (
            ((turn, goal, True, lifecycle), "archiving"),
            ((turn, goal, True, None), "running"),
            ((None, goal, True, None), "compacting"),
            ((None, goal, False, None), "goal-running"),
            ((None, None, False, None), "goal-paused"),
        )

        for (active, active_goal, compacting, active_lifecycle), expected in cases:
            with self.subTest(expected=expected):
                status = _project_binding_status(
                    binding=binding,
                    snapshot=BindingRuntimeSnapshot(
                        binding.id,
                        1,
                        active,
                        active_goal,
                        compacting,
                        active_lifecycle,
                        None,
                        None,
                    ),
                    persisted_goal=persisted,
                    persisted_goal_resolved=True,
                )
                self.assertEqual(status.primary_status, expected)

        idle = _project_binding_status(
            binding=binding,
            snapshot=BindingRuntimeSnapshot(
                binding.id,
                1,
                None,
                None,
                False,
                None,
                None,
                None,
            ),
            persisted_goal_resolved=True,
        )
        self.assertEqual(idle.primary_status, "idle")

    async def test_exact_status_resolves_persisted_goal_and_release_policy(
        self,
    ) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-goal")
        binding = self.store.get(binding.id)
        self.runtime.runtime_snapshots[binding.id] = BindingRuntimeSnapshot(
            binding.id,
            3,
            None,
            None,
            False,
            None,
            ThreadSubscriptionSnapshot(
                binding.id,
                "native-goal",
                ThreadSubscriptionState.SUBSCRIBED,
                None,
            ),
            None,
        )
        self.runtime.goal_snapshots[binding.id] = GoalSnapshot(
            "native-goal",
            "ship it",
            GoalStatus.PAUSED,
            None,
            10,
            2,
            1,
            2,
        )

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.ACTIVE,
        )

        self.assertEqual(status.primary_status, "goal-paused")
        self.assertEqual(status.primary_status_resolution.value, "resolved")
        self.assertFalse(status.can_stop)
        self.assertTrue(status.can_release)
        self.assertIn(("goal-snapshot", binding.id), self.runtime.calls)

    async def test_exact_status_reprojects_post_read_local_state(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-goal")
        binding = self.store.get(binding.id)
        persisted = GoalSnapshot(
            "native-goal",
            "external",
            GoalStatus.ACTIVE,
            None,
            1,
            1,
            1,
            2,
        )
        self.runtime.goal_snapshots[binding.id] = persisted
        self.runtime.goal_snapshot_after[binding.id] = BindingRuntimeSnapshot(
            binding.id,
            4,
            None,
            ActiveGoalSnapshot(
                binding.id,
                "native-goal",
                None,
                "external",
                GoalOperationState.EXTERNAL_ACTIVE,
                persisted,
            ),
            False,
            None,
            None,
            None,
        )

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.ACTIVE,
        )

        self.assertEqual(status.primary_status, "externally-active-goal")
        self.assertEqual(status.primary_status_resolution.value, "local")
        self.assertEqual(status.snapshot.activity_revision, 4)

    async def test_exact_status_batch_isolates_goal_read_failure(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-goal")
        binding = self.store.get(binding.id)
        self.runtime.goal_snapshot_errors.add(binding.id)

        for catalog_state in (NativeThreadCatalogState.ACTIVE, None):
            with self.subTest(catalog_state=catalog_state):
                self.runtime.calls.clear()
                (status,) = await self.service.binding_statuses_exact(
                    binding_ids=(binding.id,),
                    catalog_states={binding.id: catalog_state},
                    deadline=asyncio.get_running_loop().time() + 1,
                )

                self.assertIsNone(status.primary_status)
                self.assertEqual(status.primary_status_resolution.value, "unavailable")
                self.assertIn(("goal-snapshot", binding.id), self.runtime.calls)

    async def test_expired_status_budget_preserves_local_states_without_native_reads(self) -> None:
        native = await self._create()
        self.store.assign_native_thread_id(native.id, "native-goal")
        lazy = await self._create()
        compacting = await self._create()
        self.store.assign_native_thread_id(compacting.id, "native-compacting")
        self.runtime.runtime_snapshots[compacting.id] = BindingRuntimeSnapshot(
            compacting.id, 1, None, None, True, None, None, None,
        )
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        statuses = await self.service.binding_statuses_exact(
            binding_ids=(native.id, lazy.id, compacting.id, archived.id),
            catalog_states={archived.id: NativeThreadCatalogState.ARCHIVED},
            deadline=asyncio.get_running_loop().time() - 1,
        )
        self.assertEqual(
            [(status.primary_status, status.primary_status_resolution.value) for status in statuses],
            [
                (None, "unavailable"),
                ("idle", "resolved"),
                ("compacting", "local"),
                (None, "archived"),
            ],
        )
        self.assertFalse(any(call[0] == "goal-snapshot" for call in self.runtime.calls))

    async def test_exact_status_batch_bounds_native_goal_reads(self) -> None:
        self.store._id_factory = lambda: str(uuid.uuid4())
        bindings = []
        for index in range(18):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, f"native-{index}")
            bindings.append(self.store.get(binding.id))
        self.runtime.goal_snapshot_gate = asyncio.Event()
        self.runtime.goal_snapshot_eight_entered = asyncio.Event()
        resolving = tuple(
            asyncio.create_task(
                self.service.binding_statuses_exact(
                    binding_ids=tuple(item.id for item in batch),
                    catalog_states={
                        item.id: NativeThreadCatalogState.ACTIVE for item in batch
                    },
                    deadline=asyncio.get_running_loop().time() + 10,
                )
            )
            for batch in (bindings[:9], bindings[9:])
        )
        try:
            await asyncio.wait_for(self.runtime.goal_snapshot_eight_entered.wait(), timeout=5)
            self.assertEqual(self.runtime.goal_snapshot_max_concurrency, 8)
            self.assertEqual(
                sum(call[0] == "goal-snapshot" for call in self.runtime.calls),
                8,
            )
            self.runtime.goal_snapshot_gate.set()
            statuses = await asyncio.wait_for(asyncio.gather(*resolving), timeout=5)
            self.assertEqual(sum(map(len, statuses)), 18)
            self.assertEqual(
                sum(call[0] == "goal-snapshot" for call in self.runtime.calls),
                18,
            )
            self.assertEqual(self.runtime.goal_snapshot_max_concurrency, 8)
            self.assertEqual(self.runtime.goal_snapshot_concurrency, 0)
        finally:
            self.runtime.goal_snapshot_gate.set()
            for task in resolving:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*resolving, return_exceptions=True)

    async def test_archived_status_skips_persisted_goal_until_restored(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-archived")
        binding = self.store.get(binding.id)
        self.runtime.goal_snapshots[binding.id] = GoalSnapshot(
            "native-archived",
            "keep this goal",
            GoalStatus.PAUSED,
            None,
            10,
            2,
            1,
            2,
        )

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.ARCHIVED,
        )

        self.assertIsNone(status.primary_status)
        self.assertIsNone(status.persisted_goal_status)
        self.assertEqual(status.primary_status_resolution.value, "archived")
        self.assertFalse(status.can_stop)
        self.assertFalse(status.can_release)
        self.assertNotIn(("goal-snapshot", binding.id), self.runtime.calls)

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.ACTIVE,
        )

        self.assertEqual(status.primary_status, "goal-paused")
        self.assertEqual(status.primary_status_resolution.value, "resolved")
        self.assertIn(("goal-snapshot", binding.id), self.runtime.calls)

    async def test_archived_status_preserves_local_activity_without_goal_reads(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-archived")
        self.runtime.goal_snapshot_errors.add(binding.id)
        base = BindingRuntimeSnapshot(
            binding.id, 1, None, None, False, None, None, None,
        )
        turn = ActiveTurnSnapshot(
            binding.id, "native-archived", "turn-1", "ou_user", ActiveState.RUNNING,
        )
        goal = ActiveGoalSnapshot(
            binding.id, "native-archived", "goal-1", "ou_user",
            GoalOperationState.RUNNING, None,
        )
        lifecycle = ThreadLifecycleSnapshot(
            binding.id, "native-archived", ThreadLifecycleState.ARCHIVING,
        )
        unknown = ThreadLifecycleSnapshot(
            binding.id, "native-archived", ThreadLifecycleState.UNKNOWN,
        )
        cases = (
            ((turn, goal, True, lifecycle), "archiving"),
            ((turn, goal, True, unknown), "lifecycle-unknown"),
            ((turn, goal, True, None), "running"),
            ((None, goal, True, None), "compacting"),
            ((None, goal, False, None), "goal-running"),
        )
        for (active, active_goal, compacting, active_lifecycle), expected in cases:
            with self.subTest(expected=expected):
                self.runtime.runtime_snapshots[binding.id] = BindingRuntimeSnapshot(
                    binding.id, 1, active, active_goal, compacting, active_lifecycle,
                    None, None,
                )
                status = await self.service.binding_status_exact(
                    binding.id,
                    catalog_state=NativeThreadCatalogState.ARCHIVED,
                )
                self.assertEqual(status.primary_status, expected)
                self.assertEqual(status.primary_status_resolution.value, "local")

        self.runtime.runtime_snapshots[binding.id] = base
        status = await self.service.binding_status_exact(
            binding.id, catalog_state=NativeThreadCatalogState.ARCHIVED,
        )
        self.assertIsNone(status.primary_status)
        self.assertEqual(status.primary_status_resolution.value, "archived")
        self.assertFalse(any(call[0] == "goal-snapshot" for call in self.runtime.calls))

    async def test_archived_projection_is_not_an_unavailable_runtime_state(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-archived")
        binding = self.store.get(binding.id)

        status = _project_binding_status(
            binding=binding,
            snapshot=self.runtime.runtime_snapshot_exact(binding.id),
            catalog_state=NativeThreadCatalogState.ARCHIVED,
            primary_status_unavailable=True,
        )

        self.assertIsNone(status.primary_status)
        self.assertEqual(status.primary_status_resolution.value, "archived")

    async def test_missing_status_resolves_idle_without_goal_read(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-missing")
        binding = self.store.get(binding.id)

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.MISSING,
        )

        self.assertEqual(status.primary_status, "idle")
        self.assertNotIn(("goal-snapshot", binding.id), self.runtime.calls)

    def test_native_catalog_classification_is_shared_with_archived_precedence(
        self,
    ) -> None:
        active = {
            "active": NativeThreadMetadata("active", "Active", "preview")
        }
        archived = {
            "archived": NativeThreadMetadata("archived", "Old", "preview")
        }

        self.assertIs(
            classify_native_thread_view(
                "active", active=active, archived=archived
            ).state,
            NativeThreadCatalogState.ACTIVE,
        )
        self.assertIs(
            classify_native_thread_view(
                "archived", active=active, archived=archived
            ).state,
            NativeThreadCatalogState.ARCHIVED,
        )
        self.assertIs(
            classify_native_thread_view(
                "missing", active=active, archived=archived
            ).state,
            NativeThreadCatalogState.MISSING,
        )
        overlap = classify_native_thread_view(
            "active",
            active=active,
            archived={"active": active["active"]},
        )
        self.assertIs(overlap.state, NativeThreadCatalogState.ARCHIVED)

    async def test_exact_stop_rejects_a_new_runtime_revision(self) -> None:
        binding = await self._create()
        self.runtime.runtime_snapshot_exact = lambda binding_id: BindingRuntimeSnapshot(
            binding_id,
            7,
            None,
            None,
            False,
            None,
            None,
            None,
        )
        before = tuple(self.runtime.calls)

        with self.assertRaises(RuntimeStateChanged):
            await self.service.stop_exact_binding(
                target=ExactBindingTarget(
                    self.scope.key,
                    binding.id,
                    binding.id,
                ),
                runtime_precondition=RuntimePrecondition(6, None),
            )

        self.assertEqual(tuple(self.runtime.calls), before)

    def _side_record(self, parent, source="om_side"):
        record = self.store.create_side_topic(
            app_id=self.scope.app_id, chat_id=self.scope.chat_id,
            source_message_id=source, parent_binding_id=parent.id,
            creator_id="ou_user", requires_mention=False,
        )
        self.store.set_side_topic_root(record.id, "root-" + source)
        return self.store.open_side_topic(record.id, "topic-" + source)

    def _side_snapshot(self, record, *, project="test", state=SideSessionState.OPEN):
        return SideSessionSnapshot(
            side_id=record.id, parent_binding_id=record.parent_binding_id,
            parent_thread_id="parent-thread", thread_id="ephemeral-thread",
            project_alias=project, cwd=Path(self.tmp.name), creator_id="ou_user",
            state=state, topic_id=record.topic_id, root_message_id=record.root_message_id,
            turn_id=None, turn_state=None, last_activity=1, revision=7,
        )

    async def test_side_list_filters_live_project_before_pagination_after_parent_delete(self):
        parent = await self._create()
        orphan = self._side_record(parent)
        newer = self._side_record(parent, "om_newer")
        self.runtime.side_snapshots[orphan.id] = self._side_snapshot(orphan)
        self.runtime.side_snapshots[newer.id] = self._side_snapshot(newer)
        self.store.delete_binding(parent.id)
        self.projects.register(alias="other", path=None, create_directory=True)
        other_parent = (await self.service.create_current_binding(
            scope=self.scope, creator_id="ou_user", project_alias="other",
        )).binding
        other = self._side_record(other_parent, "om_other")
        self.runtime.side_snapshots[other.id] = self._side_snapshot(other, project="other")
        self.store.delete_binding(other_parent.id)
        self.runtime.calls.clear()

        page = await self.service.query_side_topics(
            query=SideTopicQuery(project_aliases=("test",), states=(SideTopicState.OPEN,)),
            limit=1, deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.record.side_topic.id for item in page.items], [newer.id])
        self.assertIsNotNone(page.next_cursor)
        self.assertEqual(page.items[0].record.project_alias, "test")
        self.assertIsNone(page.items[0].parent_binding)
        self.assertEqual(page.items[0].session_state, "open")
        self.assertTrue(page.items[0].can_close)
        self.assertFalse(any(call[0] in {"summary", "catalog"} for call in self.runtime.calls))

        following = await self.service.query_side_topics(
            query=SideTopicQuery(project_alias="test", project_aliases=("other",), state=SideTopicState.OPEN),
            cursor=page.next_cursor, limit=1,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.record.side_topic.id for item in following.items], [orphan.id])
        self.assertIsNone(following.next_cursor)
        other_page = await self.service.query_side_topics(
            query=SideTopicQuery(project_aliases=("other",)),
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.record.side_topic.id for item in other_page.items], [other.id])
        self.assertEqual(other_page.items[0].record.project_alias, "other")

        with patch.object(self.runtime, "side_ids_for_projects", side_effect=AssertionError("unexpected global scan")):
            unfiltered = await self.service.query_side_topics(
                deadline=asyncio.get_running_loop().time() + 1,
            )
        self.assertEqual(
            {item.record.side_topic.id: item.record.project_alias for item in unfiltered.items},
            {orphan.id: "test", newer.id: "test", other.id: "other"},
        )

    async def test_side_list_enriches_only_visible_exact_parents_and_isolates_failure(self):
        parent = await self._create()
        self.store.assign_native_thread_id(parent.id, "original-parent")
        side = self._side_record(parent)
        self.runtime.summary_metadata["original-parent"] = NativeThreadMetadata(
            "original-parent", "Source title", "Source preview",
        )
        replacement = await self._create()
        self.store.assign_native_thread_id(replacement.id, "replacement-parent")
        self._side_record(replacement, "om_newer")
        self.runtime.calls.clear()
        page = await self.service.query_side_topics(
            query=SideTopicQuery(parent_binding_id=parent.id), limit=1,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        item = page.items[0]
        self.assertEqual(item.record.side_topic.id, side.id)
        self.assertEqual(item.record.project_alias, "test")
        self.assertEqual(item.parent_binding.id, parent.id)
        self.assertEqual(item.parent_metadata.name, "Source title")
        self.assertEqual([call for call in self.runtime.calls if call[0] == "summary"], [
            ("summary", "original-parent"),
        ])
        self.assertFalse(any(call[0] == "catalog" for call in self.runtime.calls))
        self.runtime.summary_metadata.clear()
        failed = await self.service.query_side_topics(
            query=SideTopicQuery(parent_binding_id=parent.id),
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual(failed.items[0].parent_binding.id, parent.id)
        self.assertIsNone(failed.items[0].parent_metadata)

    async def test_side_metadata_and_session_metadata_share_pending_worker_budget(self):
        self.store._id_factory = lambda: str(uuid.uuid4())
        for index in range(5):
            parent = await self._create()
            self.store.assign_native_thread_id(parent.id, f"parent-{index}")
            self._side_record(parent, f"om-{index}")
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def read(thread_id):
            calls.append(thread_id)
            if len(calls) == 4:
                entered.set()
            await release.wait()
            return NativeThreadMetadata(thread_id, "Parent", "")

        with patch.object(self.runtime, "thread_summary_exact", side_effect=read):
            task = asyncio.create_task(self.service.query_side_topics(
                limit=100, deadline=asyncio.get_running_loop().time() + 0.3,
            ))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                summary = await self.service.binding_metadata_exact(
                    parent.id, deadline=asyncio.get_running_loop().time() + 0.01,
                )
                self.assertIsNone(summary)
                page = await asyncio.wait_for(task, 1)
                self.assertEqual(len(page.items), 5)
                self.assertEqual(len(calls), 4)
                self.assertEqual(len(self.service._summary_reads), 4)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(task, *self.service._summary_reads), 1)

    async def test_side_state_projection_and_controls_preserve_lifecycle_truth(self):
        parent = await self._create()
        record = self._side_record(parent)
        runtime = self._side_snapshot(record)
        item = SideTopicInventoryItem(
            SideTopicInventoryRecord(record, "test"),
            ChatLabel("oc_direct", "Chat", "p2p", True), runtime,
        )
        for route_state, runtime_state, expected, close in (
            (SideTopicState.CREATING, SideSessionState.OPEN, "creating", False),
            (SideTopicState.OPEN, SideSessionState.CLOSING, "closing", True),
            (SideTopicState.FAILED, SideSessionState.CLOSING, "closing", True),
        ):
            projected = replace(item,
                record=replace(item.record, side_topic=replace(record, state=route_state)),
                runtime=replace(runtime, state=runtime_state),
            )
            self.assertEqual(projected.session_state, expected)
            self.assertEqual(projected.can_close, close)
            self.assertFalse(projected.can_stop)
        self.assertEqual(replace(item, runtime=None).session_state, "unavailable")
        terminal = replace(item, runtime=None, record=replace(
            item.record, side_topic=replace(record, state=SideTopicState.CLOSED),
        ))
        self.assertEqual(terminal.session_state, "closed")
        self.assertFalse(terminal.can_close)
        running = replace(item, runtime=replace(runtime, turn_id="turn-1", turn_state=ActiveState.RUNNING))
        self.assertTrue(running.can_stop)

    async def test_shared_side_stop_checks_identity_and_forwards_exact_turn(self):
        parent = await self._create()
        record = self._side_record(parent)
        target = CurrentSideTarget(record.id, record.app_id, record.chat_id,
                                   record.topic_id, record.root_message_id)
        with self.assertRaises(SideIdentityMismatch):
            await self.service.stop_side(target=replace(target, chat_id="wrong"))
        self.assertFalse(any(call[0] == "stop-side" for call in self.runtime.calls))
        await self.service.stop_side(target=target, runtime_precondition=RuntimePrecondition(7, "turn-1"))
        self.assertEqual(self.runtime.calls[-1], ("stop-side", record.id, {
            "acknowledge": None, "expected_revision": 7, "expected_turn_id": "turn-1",
        }))
        self.store.transition_side_topic(record.id, SideTopicState.CLOSED)
        with self.assertRaises(RuntimeStateChanged):
            await self.service.stop_side(target=target)

    async def test_missing_side_session_commits_expired_tombstone(self) -> None:
        parent = await self._create()
        side = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_source",
            parent_binding_id=parent.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        side = self.store.set_side_topic_root(side.id, "om_root")
        side = self.store.open_side_topic(side.id, "omt_topic")
        self.runtime.side_missing = True

        closed = await self.service.close_side(
            target=CurrentSideTarget(
                side_id=side.id,
                app_id=side.app_id,
                chat_id=side.chat_id,
                topic_id=side.topic_id,
                root_message_id=side.root_message_id,
            )
        )

        self.assertTrue(closed.missing_runtime_session)
        self.assertEqual(closed.record.state, SideTopicState.EXPIRED)

    async def test_terminal_side_close_only_checks_local_runtime_snapshot(self) -> None:
        parent = await self._create()
        side = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_source_terminal",
            parent_binding_id=parent.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        side = self.store.transition_side_topic(side.id, SideTopicState.FAILED)
        before = tuple(self.runtime.calls)

        closed = await self.service.close_side(
            target=CurrentSideTarget(
                side_id=side.id,
                app_id=side.app_id,
                chat_id=side.chat_id,
                topic_id=side.topic_id,
                root_message_id=side.root_message_id,
            )
        )

        self.assertIsNone(closed.outcome)
        self.assertEqual(closed.record.state, SideTopicState.FAILED)
        self.assertEqual(
            tuple(self.runtime.calls),
            (*before, ("side-snapshot", side.id)),
        )

    async def test_side_close_rejects_exact_identity_mismatch(self) -> None:
        parent = await self._create()
        side = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_source_mismatch",
            parent_binding_id=parent.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        before = tuple(self.runtime.calls)

        with self.assertRaises(SideIdentityMismatch):
            await self.service.close_side(
                target=CurrentSideTarget(
                    side_id=side.id,
                    app_id=side.app_id,
                    chat_id="oc_wrong",
                    topic_id=side.topic_id,
                    root_message_id=side.root_message_id,
                )
            )

        self.assertEqual(tuple(self.runtime.calls), before)

    async def test_channel_uses_shared_management_scope_coordinator(self) -> None:
        class RuntimeWithCompletion(FakeManagementRuntime):
            def set_completion_handler(self, _handler) -> None:
                pass

            def set_question_handler(self, _handler) -> None:
                pass

        runtime = RuntimeWithCompletion(self.store)

        application = ChannelApplication(
            app_id="cli_test",
            channel=object(),  # type: ignore[arg-type]
            runtime=runtime,  # type: ignore[arg-type]
            bindings=self.store,
            projects=self.projects,
            management=self.service,
        )
        try:
            self.assertIs(application._management, self.service)
            self.assertIs(application._scope_coordinator, self.service.scope_coordinator)
        finally:
            await application.close()


class ManagementRuntimePortSurfaceTest(unittest.TestCase):
    def test_native_delete_availability_is_projected_without_exposing_runtime(self) -> None:
        available = ManagementRuntimePort(
            SimpleNamespace(
                available_capabilities=frozenset({NativeCapability.DELETE})
            )
        )
        unavailable = ManagementRuntimePort(
            SimpleNamespace(available_capabilities=frozenset())
        )

        self.assertTrue(available.native_delete_available)
        self.assertFalse(unavailable.native_delete_available)

    def test_forbidden_runtime_capabilities_are_not_exposed(self) -> None:
        port = ManagementRuntimePort(object())  # type: ignore[arg-type]

        for name in (
            "submit",
            "compact",
            "start_goal",
            "resume_goal",
            "clear_goal",
            "create_side",
            "delete_binding",
        ):
            self.assertFalse(hasattr(port, name), name)


if __name__ == "__main__":
    unittest.main()
