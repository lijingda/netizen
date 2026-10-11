from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.credentials import AdminCredentials
from netizen_cli.admin.feishu import AuthExpired, FeishuError, UserTokens
from netizen_cli.admin.users import AdminIdentity, AdminUsers
from netizen_cli.admin.web import AdminWebApplication
from netizen_cli.instance import instance_digest
from tests.admin.test_instance_admin import _request, _response_cookies
from tests.admin.test_web import FakeFeishuClient, FakeManagement, _action_payload


class AdminUsersWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.users = AdminUsers(self.root, "cli_test")
        self.member = self.users.add_initial("ou_admin", name="Snapshot")
        self.auth = AdminAuth(self.users)
        self.client = FakeFeishuClient(clock=lambda: 1_000.0)
        self.credentials = AdminCredentials(self.root, self.users, self.client, clock=lambda: 1_000.0)
        self.app = AdminWebApplication(
            auth=self.auth, management=FakeManagement(self.root), instance_root=self.root,
            credentials=self.credentials,
        )
        self.app.configure_authorities(("127.0.0.1:8787",))
        self.app.open_admission()
        self.addCleanup(self.app.close_auth)
        self.cookie_name = f"netizen_admin_session_{instance_digest(self.root)}"
        self.session = await self.login("ou_admin")

    async def login(self, open_id: str) -> str:
        identity = AdminIdentity("cli_test", open_id)
        member = self.users.get_membership(identity)
        assert member is not None
        await self.credentials.save_login(identity, UserTokens(
            f"synthetic-access-{open_id}", f"synthetic-refresh-{open_id}", 1_600, 4_600,
            ("component:selector", "offline_access"),
        ), expected_add_revision=member.add_revision)
        return self.auth.issue_session(identity, source_ip="127.0.0.1",
                                       expected_add_revision=member.add_revision).token

    async def request(self, method: str, path: str, *, session: str | None = None,
                      payload: dict | None = None):
        response = await self.app.handle(_request(
            method, path, cookies={self.cookie_name: session or self.session},
            origin="http://127.0.0.1:8787" if method == "POST" else None, payload=payload,
        ))
        return response, json.loads(response.body) if response.body else None

    async def roster(self, *, session: str | None = None) -> dict:
        response, data = await self.request("GET", "/api/v1/admin-users", session=session)
        self.assertEqual(response.status, 200)
        return data

    async def add(self, open_id: str, **selection):
        data = await self.roster()
        return await self.request("POST", "/api/v1/admin-users/add", payload=_action_payload(
            data["addAction"], revision=data["revision"], openId=open_id,
            **{"entityId": open_id, "type": "user", "name": "Selected", "avatarUrl": None,
               **selection},
        ))

    async def test_roster_displays_snapshots_and_action_grants_without_querying_feishu(self) -> None:
        self.client.user_info = AsyncMock(side_effect=AssertionError("no directory lookup"))
        data = await self.roster()
        self.assertEqual(data["revision"], 1)
        self.assertEqual(data["currentOpenId"], "ou_admin")
        self.assertEqual(data["users"], [{
            "openId": "ou_admin", "name": "Snapshot", "avatarUrl": None, "removeAction": None,
        }])
        self.assertIn("actionToken", data["addAction"])
        self.assertIn("csrfToken", data["selectorAction"])
        self.client.user_info.assert_not_awaited()

    async def test_add_requires_user_selection_and_consistent_id_and_is_one_shot(self) -> None:
        for selection in ({"type": "department"}, {"entityId": "ou_different"}):
            with self.subTest(selection=selection):
                response, data = await self.add("ou_other", **selection)
                self.assertEqual(response.status, 400)
                self.assertEqual(data["code"], "invalid_selection")
                self.assertEqual(self.users.revision, 1)
        page = await self.roster()
        payload = _action_payload(page["addAction"], revision=page["revision"],
                                 openId="ou_other", entityId="ou_other", type="user",
                                 name="Other", avatarUrl="javascript:alert(1)")
        response, _ = await self.request("POST", "/api/v1/admin-users/add", payload=payload)
        self.assertEqual(response.status, 200)
        member = self.users.get_membership(AdminIdentity("cli_test", "ou_other"))
        self.assertEqual(member.name, "Other")
        self.assertIsNone(member.avatar_url)
        response, _ = await self.request("POST", "/api/v1/admin-users/add", payload=payload)
        self.assertEqual(response.status, 409)

    async def test_action_is_bound_to_browser_and_csrf(self) -> None:
        second = await self.login("ou_admin")
        page = await self.roster()
        payload = _action_payload(page["addAction"], revision=page["revision"],
                                 openId="ou_other", entityId="ou_other", type="user",
                                 name="Other", avatarUrl=None)
        response, _ = await self.request("POST", "/api/v1/admin-users/add", session=second, payload=payload)
        self.assertEqual(response.status, 409)
        response, _ = await self.request("POST", "/api/v1/admin-users/add",
                                         payload={**payload, "csrfToken": secrets.token_urlsafe(32)})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.users.revision, 1)

    async def test_profile_update_does_not_invalidate_roster_revision_or_pending_add(self) -> None:
        page = await self.roster()
        response, data = await self.request("GET", "/api/v1/identity")
        self.assertEqual(response.status, 200)
        self.assertEqual(data["name"], "Admin")
        self.assertEqual(self.users.revision, page["revision"])
        response, _ = await self.request("POST", "/api/v1/admin-users/add", payload=_action_payload(
            page["addAction"], revision=page["revision"], openId="ou_other",
            entityId="ou_other", type="user", name="Other", avatarUrl=None,
        ))
        self.assertEqual(response.status, 200)

    async def test_selector_config_uses_current_identity_and_server_bound_url(self) -> None:
        self.client.selector_config = AsyncMock(return_value={"signature": "synthetic-signature"})
        page = await self.roster()
        response, data = await self.request("POST", "/api/v1/admin-users/selector-config",
                                            payload=_action_payload(page["selectorAction"]))
        self.assertEqual(response.status, 200)
        self.assertEqual(data, {"signature": "synthetic-signature"})
        self.client.selector_config.assert_awaited_once_with(
            "synthetic-access-ou_admin", "ou_admin", "http://127.0.0.1:8787/",
        )
        self.assertNotIn("synthetic-access-ou_admin", response.body.decode())

    async def test_self_delete_revokes_all_browsers_and_tokens_but_preserves_other_admin(self) -> None:
        response, _ = await self.add("ou_other")
        self.assertEqual(response.status, 200)
        another_browser = await self.login("ou_admin")
        other_admin = await self.login("ou_other")
        page = await self.roster()
        grant = next(row["removeAction"] for row in page["users"] if row["openId"] == "ou_admin")
        response, data = await self.request("POST", "/api/v1/admin-users/remove", payload=_action_payload(
            grant, revision=page["revision"], openId="ou_admin",
        ))
        self.assertEqual(response.status, 200)
        self.assertTrue(data["loggedOut"])
        self.assertEqual(_response_cookies(response)[self.cookie_name], "")
        for session in (self.session, another_browser):
            response, _ = await self.request("GET", "/api/v1/admin-users", session=session)
            self.assertEqual(response.status, 401)
        self.assertEqual((await self.roster(session=other_admin))["currentOpenId"], "ou_other")
        saved = json.loads((self.root / "credentials/admin-user-tokens.json").read_text())
        self.assertEqual([row["openId"] for row in saved["users"]], ["ou_other"])

    async def test_concurrent_old_delete_grant_cannot_remove_final_administrator(self) -> None:
        self.assertEqual((await self.add("ou_other"))[0].status, 200)
        page = await self.roster()
        grants = {row["openId"]: row["removeAction"] for row in page["users"]}
        for open_id, expected_status in (("ou_other", 200), ("ou_admin", 409)):
            response, _ = await self.request("POST", "/api/v1/admin-users/remove", payload=_action_payload(
                grants[open_id], revision=page["revision"], openId=open_id,
            ))
            self.assertEqual(response.status, expected_status)
        self.assertEqual([member.identity.open_id for member in self.users.list()], ["ou_admin"])
        self.assertIsNone((await self.roster())["users"][0]["removeAction"])

    async def test_profile_timeout_is_generic_error_and_preserves_session(self) -> None:
        for error, status in ((FeishuError("upstream_unavailable", "temporarily unavailable"), 502),
                              (TimeoutError("synthetic upstream timeout"), 504)):
            with self.subTest(error=type(error).__name__):
                self.client.user_info = AsyncMock(side_effect=error)
                response, _ = await self.request("GET", "/api/v1/identity")
                self.assertEqual(response.status, status)
                self.assertEqual((await self.roster())["currentOpenId"], "ou_admin")

    async def test_expired_identity_logs_out_only_current_browser(self) -> None:
        another_browser = await self.login("ou_admin")
        self.client.user_info = AsyncMock(side_effect=AuthExpired("token_invalid", "invalid"))
        response, data = await self.request("GET", "/api/v1/identity")
        self.assertEqual(response.status, 401)
        self.assertEqual(data["code"], "auth_expired")
        response, _ = await self.request("GET", "/api/v1/admin-users")
        self.assertEqual(response.status, 401)
        self.assertEqual((await self.roster(session=another_browser))["currentOpenId"], "ou_admin")
