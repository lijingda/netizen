from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from netizen_cli.bindings import BindingStore, ProjectDeleting, ProjectInventoryConflict
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.schedules.models import ScheduleRule
from netizen_cli.schedules.store import _has_trigger, register_query_functions
from netizen_cli.session_settings import SessionSettings
from netizen_cli.worktrees.policy import WorkspacePolicy


class SQLiteQueryReadersTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = BindingStore(Path(directory.name) / "channel.sqlite3")
        self.addAsyncCleanup(self.store.aclose)
        self.store.register_project(alias="p", cwd=directory.name)
        self.scope = FeishuScope("app", "chat", ScopeKind.GROUP)
        self.binding = self.create_binding()

    def create_binding(self):
        return self.store.create_channel_binding(scope=self.scope, project_alias="p", creator_id="user")

    def assert_writer_available(self):
        acquired = self.store._lock.acquire(blocking=False)
        self.assertTrue(acquired, "advisory query holds the shared writer lock")
        if acquired:
            self.store._lock.release()
        self.assertEqual(self.store.get(self.binding.id), self.binding)

    async def held_preview(self):
        entered, release = threading.Event(), threading.Event()
        connection = self.store._query_connection
        self.assertIsNotNone(connection)

        def trace(sql):
            # The Project SELECT has already established the reader snapshot.
            if "FROM managed_worktrees WHERE project_alias" in sql:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("preview release timed out")

        connection.set_trace_callback(trace)
        task = asyncio.create_task(self.store.preview_project_delete("p", deadline_seconds=10))
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        self.addCleanup(connection.set_trace_callback, None)
        self.addCleanup(release.set)
        return task, release

    async def test_preview_snapshot_does_not_block_writer_and_stale_inventory_is_rejected(self):
        task, release = await self.held_preview()
        try:
            self.assert_writer_available()
            added = self.create_binding()
        finally:
            release.set()
        snapshot = await task
        self.assertEqual([item.binding.id for item in snapshot.bindings], [self.binding.id])
        with self.assertRaises(ProjectInventoryConflict):
            self.store.begin_project_delete(alias="p", expected_revision=snapshot.project.revision,
                expected_inventory_fingerprint=snapshot.fingerprint)
        self.assertFalse(self.store.project_delete_in_progress("p"))
        self.assertEqual(self.store.get(added.id), added)

    async def test_preview_rechecks_in_memory_deletion_intent_after_reader(self):
        original = await self.store.preview_project_delete("p")
        task, release = await self.held_preview()
        try:
            self.assert_writer_available()
            self.store.begin_project_delete(alias="p", expected_revision=original.project.revision,
                expected_inventory_fingerprint=original.fingerprint)
        finally:
            release.set()
        with self.assertRaises(ProjectDeleting):
            await task

    async def test_ended_function_runs_on_reader_and_keeps_filter_semantics(self):
        self.store.schedules.create(name="plan", instructions="task", project_alias="p", app_id="app",
            chat_id="chat", schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
            session_settings=SessionSettings(), workspace=WorkspacePolicy(), source="test",
            request_id="plan", now=100)
        self.assertEqual(await self.store.schedules.list(app_id="app", ended=True, now=100), ())
        entered, release = threading.Event(), threading.Event()
        threads = []
        connection = self.store._query_connection

        def held(*args):
            threads.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise AssertionError("list release timed out")
            return _has_trigger(*args)

        connection.create_function("netizen_schedule_has_trigger", 5, held, deterministic=True)
        task = asyncio.create_task(self.store.schedules.list(app_id="app", ended=False, now=100, deadline_seconds=10))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.assert_writer_available()
        finally:
            release.set()
        plans = await task
        register_query_functions(connection)
        self.assertEqual(len(plans), 1)
        self.assertTrue(all(thread != threading.get_ident() for thread in threads))


if __name__ == "__main__":
    unittest.main()
