from __future__ import annotations

import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.port_config import ConfigFileSnapshot, write_configuration
from netizen_cli.admin.users import AdminUsers
from netizen_cli.admin.web import AdminWebApplication
from netizen_cli.instance import instance_digest
from netizen_cli.sessions.policy import SessionPolicyService
from tests.admin.test_instance_admin import _request
from tests.admin.test_web import _action_payload
from tests.support.channel_fixtures import channel_fixture


class SessionToolAccessWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fixture = await self.enterAsyncContext(channel_fixture())
        self.root = fixture.project_root
        self.path = self.root / "config.yaml"
        self.path.write_text("projects: {}\n")
        self.policy = SessionPolicyService(config_snapshot=ConfigFileSnapshot.read(self.path))
        self.users = AdminUsers(self.root, "cli_test")
        self.member = self.users.add_initial("ou_admin")
        self.auth = AdminAuth(self.users)
        self.app = AdminWebApplication(auth=self.auth, management=fixture.management,
            instance_root=self.root, session_policy=self.policy)
        self.app.configure_authorities(("127.0.0.1:8787",))
        self.app.open_admission()
        self.addCleanup(self.app.close_auth)
        self.cookie_name = f"netizen_admin_session_{instance_digest(self.root)}"
        self.session = self.issue_session()

    def issue_session(self) -> str:
        return self.auth.issue_session(self.member.identity, source_ip="127.0.0.1",
            expected_add_revision=self.member.add_revision).token

    async def request(self, method="GET", *, payload=None, session=None, origin="http://127.0.0.1:8787"):
        response = await self.app.handle(_request(method, "/api/v1/session-tool-access" + ("/save" if method == "POST" else ""),
            cookies={self.cookie_name: session or self.session}, origin=origin if method == "POST" else None, payload=payload))
        return response.status, json.loads(response.body)

    async def test_authorized_save_updates_both_rules_and_is_one_shot(self) -> None:
        status, view = await self.request()
        self.assertEqual(status, 200)
        self.assertEqual(view["discovery"], {"mode": "all", "chatIds": []})
        payload = _action_payload(view["saveAction"], discovery={"mode": "disabled"},
                                  readSend={"mode": "exclude", "chatIds": ["oc_private"]})
        status, saved = await self.request("POST", payload=payload)
        self.assertEqual(status, 200)
        self.assertNotEqual(saved["revision"], view["revision"])
        self.assertFalse(self.policy.allows_discovery("oc_public"))
        self.assertTrue(self.policy.allows_read_send("oc_public"))
        self.assertFalse(self.policy.allows_read_send("oc_private"))
        self.assertEqual((await self.request("POST", payload=payload))[0], 409)

    async def test_parallel_stale_grant_does_not_overwrite_new_rules(self) -> None:
        _, first = await self.request()
        _, stale = await self.request()
        rule = {"mode": "disabled"}
        self.assertEqual((await self.request("POST", payload=_action_payload(
            first["saveAction"], discovery=rule, readSend=rule)))[0], 200)
        status, result = await self.request("POST", payload=_action_payload(
            stale["saveAction"], discovery={"mode": "all"}, readSend={"mode": "all"}))
        self.assertEqual(status, 409)
        self.assertEqual(result["code"], "stale_session_tool_access")
        self.assertFalse(self.policy.allows_read_send("oc_any"))

    async def test_pending_save_keeps_event_loop_and_configuration_reads_available(self) -> None:
        _, before = await self.request()
        payload = _action_payload(before["saveAction"],
                                  discovery={"mode": "disabled"}, readSend={"mode": "disabled"})
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def slow_write(snapshot, loaded):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=5):
                raise AssertionError("test did not release the configuration writer")
            return write_configuration(snapshot, loaded)

        with patch("netizen_cli.admin.port_config.write_configuration", side_effect=slow_write):
            saving = asyncio.create_task(self.request("POST", payload=payload))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                status, during = await asyncio.wait_for(self.request(), timeout=2)
                self.assertEqual(status, 200)
                self.assertFalse(saving.done())
                for key in ("revision", "discovery", "readSend"):
                    self.assertEqual(during[key], before[key])
                self.assertTrue(self.policy.allows_read_send("oc_any"))
            finally:
                release.set()
                status, saved = await asyncio.wait_for(saving, timeout=2)

        self.assertEqual(status, 200, saved)
        self.assertNotEqual(saved["revision"], before["revision"])
        self.assertFalse(self.policy.allows_read_send("oc_any"))

    async def test_requires_identity_same_origin_and_browser_bound_grant(self) -> None:
        self.assertEqual((await self.request(session="not-a-session"))[0], 401)
        _, view = await self.request()
        payload = _action_payload(view["saveAction"], discovery={"mode": "disabled"}, readSend={"mode": "disabled"})
        self.assertEqual((await self.request("POST", payload=payload, origin="http://elsewhere.test"))[0], 403)
        self.assertEqual((await self.request("POST", payload=payload, session=self.issue_session()))[0], 409)
        self.assertTrue(self.policy.allows_read_send("oc_any"))

    async def test_invalid_payload_and_external_edit_preserve_current_policy(self) -> None:
        _, view = await self.request()
        status, _ = await self.request("POST", payload=_action_payload(view["saveAction"],
            discovery={"mode": "only", "chatIds": "oc_wrong"}, readSend={"mode": "all"}))
        self.assertEqual(status, 400)
        _, view = await self.request()
        changed = "projects: {other: /new}\n"
        self.path.write_text(changed)
        status, result = await self.request("POST", payload=_action_payload(view["saveAction"],
            discovery={"mode": "disabled"}, readSend={"mode": "disabled"}))
        self.assertEqual(status, 409)
        self.assertEqual(result["code"], "configuration_changed")
        self.assertEqual(self.path.read_text(), changed)
        self.assertTrue(self.policy.allows_read_send("oc_any"))
