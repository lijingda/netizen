from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.support import channel_fixtures


class ChannelFixtureLifetimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_application_initialization_failure_releases_existing_resources(self):
        management = SimpleNamespace(close=AsyncMock())
        captured = {}

        def fail_application(**kwargs):
            captured["store"] = kwargs["bindings"]
            captured["project"] = Path(kwargs["bindings"].get_project("test").cwd)
            raise RuntimeError("application initialization failed")

        with (
            patch.object(channel_fixtures, "InstanceManagementService", return_value=management),
            patch.object(channel_fixtures, "ChannelApplication", side_effect=fail_application),
            self.assertRaisesRegex(RuntimeError, "application initialization failed"),
        ):
            async with channel_fixtures.channel_fixture():
                self.fail("fixture must not be entered")

        management.close.assert_awaited_once()
        self.assertFalse(captured["project"].parent.exists())
        with self.assertRaises(sqlite3.ProgrammingError):
            captured["store"].list_projects()

    async def test_application_close_failure_still_releases_management_database_and_directory(self):
        close_app = channel_fixtures.ChannelApplication.close
        close_management = channel_fixtures.InstanceManagementService.close
        closed_management = []

        async def close_then_fail(app):
            await close_app(app)
            raise RuntimeError("application close failed")

        async def observe_management_close(management):
            await close_management(management)
            closed_management.append(management)

        with (
            patch.object(channel_fixtures.ChannelApplication, "close", close_then_fail),
            patch.object(channel_fixtures.InstanceManagementService, "close", observe_management_close),
            self.assertRaisesRegex(RuntimeError, "application close failed"),
        ):
            async with channel_fixtures.channel_fixture() as fixture:
                self.assertTrue(fixture.project_root.exists())

        self.assertEqual(closed_management, [fixture.management])
        self.assertFalse(fixture.project_root.exists())
        with self.assertRaises(sqlite3.ProgrammingError):
            fixture.store.list_projects()
