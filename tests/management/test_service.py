from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from netizen.bindings import BindingQuery, BindingStore, SideTopicState
from netizen.channel_app import ChannelApplication
from netizen.codex_runtime import (
    BindingRuntimeSnapshot,
    NativeThreadCatalogState,
    NativeThreadCatalog,
    NativeThreadMetadata,
    ReleaseDisposition,
    SideLifecycleOutcome,
    SideSessionNotFound,
    StopDisposition,
    ThreadArchived,
    ThreadCatalogIdentityMissing,
)
from netizen.domain import FeishuScope, ScopeKind
from netizen.management import (
    ActivePointerChanged,
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
    SessionQuery,
    SideIdentityMismatch,
)
from netizen.projects import ProjectRegistry


class FakeManagementRuntime:
    def __init__(self, store: BindingStore) -> None:
        self.store = store
        self.calls: list[tuple[object, ...]] = []
        self.archived: set[str] = set()
        self.missing: set[str] = set()
        self.rename_entered: asyncio.Event | None = None
        self.rename_release: asyncio.Event | None = None
        self.side_missing = False
        self.active_metadata: dict[str, NativeThreadMetadata] = {}
        self.archived_metadata: dict[str, NativeThreadMetadata] = {}
        self.side_snapshots: dict[str, object] = {}

    async def configure_exact(self, **values):
        self.calls.append(("configure", values["binding_id"]))
        return self.store.set_turn_settings(**values)

    async def activate_exact(self, binding_id: str):
        self.calls.append(("activate", binding_id))
        binding = self.store.get(binding_id)
        if binding.native_thread_id in self.archived:
            raise ThreadArchived("archived")
        if binding.native_thread_id in self.missing:
            raise ThreadCatalogIdentityMissing("missing")
        return self.store.activate(
            scope_key=binding.scope_key,
            binding_id=binding.id,
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
        binding = self.store.get(binding_id)
        assert binding.native_thread_id is not None
        self.archived.add(binding.native_thread_id)
        return self.store.deactivate_if_active(
            scope_key=binding.scope_key,
            binding_id=binding.id,
        )

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

    async def stop_exact(self, binding_id: str, *, acknowledge=None):
        self.calls.append(("stop", binding_id))
        if acknowledge is not None:
            await acknowledge()
        return StopDisposition.REQUESTED

    async def release_exact(self, binding_id: str):
        self.calls.append(("release", binding_id))
        return ReleaseDisposition.NOT_SUBSCRIBED

    async def close_side_exact(self, side_id: str, *, state: SideTopicState):
        self.calls.append(("close-side", side_id, state))
        if self.side_missing:
            raise SideSessionNotFound(side_id)
        record = self.store.transition_side_topic(side_id, state)
        return SideLifecycleOutcome(side_id=side_id, state=record.state)

    async def binding_pointer_changed(self, previous, current) -> None:
        self.calls.append(("pointer", previous, current))

    async def is_thread_archived(self, thread_id: str) -> bool:
        self.calls.append(("is-archived", thread_id))
        return thread_id in self.archived

    async def thread_catalog_state_exact(
        self,
        thread_id: str,
    ) -> NativeThreadCatalogState:
        self.calls.append(("catalog-state", thread_id))
        if thread_id in self.archived:
            return NativeThreadCatalogState.ARCHIVED
        if thread_id in self.missing:
            return NativeThreadCatalogState.MISSING
        return NativeThreadCatalogState.ACTIVE

    async def thread_metadata_exact(
        self,
        thread_ids: tuple[str, ...],
        *,
        archived: bool,
        deadline: float,
    ):
        self.calls.append(("metadata", archived, thread_ids, deadline))
        source = self.archived_metadata if archived else self.active_metadata
        return {thread_id: source[thread_id] for thread_id in thread_ids if thread_id in source}

    async def thread_catalog_exact(self, *, archived: bool, deadline: float):
        self.calls.append(("catalog", archived, deadline))
        source = self.archived_metadata if archived else self.active_metadata
        return NativeThreadCatalog(archived, tuple(source.values()))

    def runtime_snapshot_exact(self, binding_id: str):
        self.calls.append(("runtime-snapshot", binding_id))
        return ("binding", binding_id)

    def side_snapshot_exact(self, side_id: str):
        self.calls.append(("side-snapshot", side_id))
        return self.side_snapshots.get(side_id)


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
        default = root / "default"
        project = root / "project"
        default.mkdir()
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
            default_cwd=default,
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

    async def _create(self, scope: FeishuScope | None = None):
        return (
            await self.service.create_current_binding(
                scope=scope or self.scope,
                creator_id="ou_user",
                project_alias="test",
            )
        ).binding

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
        self.assertIn(("pointer", second.id, second.id), self.runtime.calls)

    async def test_active_archive_clears_pointer_and_notifies_after_commit(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-active")

        archived = await self.service.archive_current_binding(
            target=CurrentBindingTarget(self.scope.key, binding.id)
        )

        self.assertEqual(archived.id, binding.id)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        self.assertEqual(self.runtime.calls[-1], ("pointer", binding.id, None))

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

    async def test_exact_activate_rejects_missing_native_catalog_identity(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-missing")
        self.store.deactivate_if_active(
            scope_key=self.scope.key,
            binding_id=binding.id,
        )
        self.runtime.missing.add("native-missing")

        with self.assertRaises(NativeThreadMissing):
            await self.service.activate_exact_binding(
                target=ExactBindingTarget(
                    scope_key=self.scope.key,
                    binding_id=binding.id,
                    expected_active_binding_id=None,
                )
            )

        self.assertIsNone(self.store.active_binding(self.scope.key))

    async def test_same_scope_waits_while_other_scope_can_mutate(self) -> None:
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
        await asyncio.sleep(0)
        self.assertFalse(same_scope.done())
        self.runtime.rename_release.set()
        await asyncio.gather(rename, same_scope)

    async def test_admin_lazy_create_supports_inactive_and_current_modes(self) -> None:
        current = await self._create()
        revision = self.projects.resolve_for_new("test").revision

        inactive = await self.service.create_exact_lazy_binding(
            scope_key=self.scope.key,
            project_alias="test",
            expected_project_revision=revision,
            expected_active_binding_id=current.id,
            activate=False,
        )
        self.assertFalse(inactive.binding.active)
        self.assertIsNone(inactive.binding.activated_at)
        self.assertEqual(inactive.binding.creator_id, "admin:web")
        self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)

        selected = await self.service.create_exact_lazy_binding(
            scope_key=self.scope.key,
            project_alias="test",
            expected_project_revision=revision,
            expected_active_binding_id=current.id,
            activate=True,
        )
        self.assertTrue(selected.binding.active)
        self.assertIsNotNone(selected.binding.activated_at)
        self.assertEqual(
            self.store.active_binding(self.scope.key).id,
            selected.binding.id,
        )
        self.assertIn(
            ("pointer", current.id, selected.binding.id),
            self.runtime.calls,
        )

        with self.assertRaises(ActivePointerChanged):
            await self.service.create_exact_lazy_binding(
                scope_key=self.scope.key,
                project_alias="test",
                expected_project_revision=revision,
                expected_active_binding_id=current.id,
                activate=False,
            )

    async def test_session_query_hydrates_only_materialized_page_records(self) -> None:
        materialized = await self._create()
        self.store.assign_native_thread_id(materialized.id, "native-active")
        lazy = await self._create()
        self.runtime.active_metadata["native-active"] = NativeThreadMetadata(
            "native-active",
            "Active title",
            "preview",
        )

        page = await self.service.query_sessions(
            deadline=asyncio.get_running_loop().time() + 1,
        )

        by_id = {item.record.binding.id: item for item in page.items}
        self.assertEqual(
            by_id[materialized.id].native.state,
            NativeThreadCatalogState.ACTIVE,
        )
        self.assertEqual(
            by_id[materialized.id].native.metadata.name,
            "Active title",
        )
        self.assertIsNone(by_id[lazy.id].native)
        metadata_calls = [call for call in self.runtime.calls if call[0] == "metadata"]
        self.assertEqual(len(metadata_calls), 2)
        self.assertEqual(metadata_calls[0][2], ("native-active",))

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
                limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            first_chat_ids = tuple(item.record.scope.chat_id for item in first.items)
            self.assertEqual(tuple(labels.info_calls), first_chat_ids)

            await service.query_sessions(
                limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            self.assertEqual(tuple(labels.info_calls), first_chat_ids)

            second = await service.query_sessions(
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

    async def test_native_state_filter_uses_complete_catalog_and_keeps_missing(
        self,
    ) -> None:
        active = await self._create()
        self.store.assign_native_thread_id(active.id, "native-active")
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        missing = await self._create()
        self.store.assign_native_thread_id(missing.id, "native-missing")
        self.runtime.active_metadata["native-active"] = NativeThreadMetadata(
            "native-active", None, "active"
        )
        self.runtime.archived_metadata["native-archived"] = NativeThreadMetadata(
            "native-archived", None, "archived"
        )
        deadline = asyncio.get_running_loop().time() + 1

        archived_page = await self.service.query_sessions(
            query=SessionQuery(
                local=BindingQuery(project_alias="test"),
                native_state=NativeThreadCatalogState.ARCHIVED,
            ),
            deadline=deadline,
        )
        missing_page = await self.service.query_sessions(
            query=SessionQuery(
                native_state=NativeThreadCatalogState.MISSING,
            ),
            deadline=asyncio.get_running_loop().time() + 1,
        )

        self.assertEqual(
            [item.record.binding.id for item in archived_page.items],
            [archived.id],
        )
        self.assertEqual(
            [item.record.binding.id for item in missing_page.items],
            [missing.id],
        )
        self.assertGreaterEqual(
            len([call for call in self.runtime.calls if call[0] == "catalog"]),
            4,
        )

    async def test_project_query_merges_complete_archived_catalog_counts(self) -> None:
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        self.runtime.archived_metadata["native-archived"] = NativeThreadMetadata(
            "native-archived", "Old", "archived"
        )

        page = await self.service.query_projects(
            deadline=asyncio.get_running_loop().time() + 1,
        )

        by_alias = {item.aggregate.project.alias: item for item in page.items}
        self.assertEqual(by_alias["test"].archived_binding_count, 1)
        self.assertEqual(by_alias["none"].archived_binding_count, 0)

    async def test_runtime_snapshot_request_is_bounded_and_reports_missing_side(
        self,
    ) -> None:
        binding = await self._create()

        snapshots = self.service.runtime_snapshots(
            binding_ids=(binding.id,),
            side_ids=("side-missing",),
        )

        self.assertEqual(snapshots.bindings, (("binding", binding.id),))
        self.assertEqual(snapshots.sides, ())
        self.assertEqual(snapshots.missing_side_ids, ("side-missing",))
        with self.assertRaises(ValueError):
            self.service.runtime_snapshots(binding_ids=(binding.id, binding.id))

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

    async def test_terminal_side_close_is_idempotent_without_runtime_call(self) -> None:
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
        self.assertEqual(tuple(self.runtime.calls), before)

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

    async def test_channel_rejects_a_different_scope_coordinator(self) -> None:
        class RuntimeWithCompletion(FakeManagementRuntime):
            def set_completion_handler(self, _handler) -> None:
                pass

        runtime = RuntimeWithCompletion(self.store)

        with self.assertRaisesRegex(ValueError, "share one ScopeCoordinator"):
            ChannelApplication(
                app_id="cli_test",
                channel=object(),  # type: ignore[arg-type]
                runtime=runtime,  # type: ignore[arg-type]
                bindings=self.store,
                projects=self.projects,
                scope_coordinator=ScopeCoordinator(),
                management=self.service,
            )


class ManagementRuntimePortSurfaceTest(unittest.TestCase):
    def test_forbidden_runtime_capabilities_are_not_exposed(self) -> None:
        port = ManagementRuntimePort(object())  # type: ignore[arg-type]

        for name in (
            "submit",
            "compact",
            "start_goal",
            "resume_goal",
            "clear_goal",
            "create_side",
            "stop_side",
            "delete_binding",
        ):
            self.assertFalse(hasattr(port, name), name)


if __name__ == "__main__":
    unittest.main()
