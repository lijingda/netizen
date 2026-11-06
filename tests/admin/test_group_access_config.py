from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.port_config import ConfigFileEditor, ConfigFileSnapshot
from netizen_cli.admin.transport import MAX_BODY_BYTES, Request
from netizen_cli.admin.users import AdminUsers
from netizen_cli.admin.web import AdminWebApplication
from netizen_cli.group_access import GroupAccessRule, GroupAccessService
from netizen_cli.management.chat_labels import ChatLabelResolver
from tests.support.channel_fixtures import channel_fixture


class GroupAccessWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.path = self.fixture.project_root / "config.yaml"
        self.path.write_text("projects: {}\n")
        self.service = GroupAccessService(config_editor=ConfigFileEditor(ConfigFileSnapshot.read(self.path)))
        users = AdminUsers(self.fixture.project_root, "cli_test")
        member = users.add_initial("ou_admin")
        self.auth = AdminAuth(users)
        self.app = AdminWebApplication(auth=self.auth, management=self.fixture.management, group_access=self.service)
        self.app.configure_authorities(("127.0.0.1:8787",))
        self.app.open_admission()
        self.addCleanup(self.app.close_auth)
        self.addAsyncCleanup(self.drain)
        self.session = self.auth.issue_session(member.identity, source_ip="127.0.0.1",
                                              expected_add_revision=member.add_revision).token

    async def drain(self):
        await self.app.drain(asyncio.get_running_loop().time() + 1)

    async def request(self, method="GET", *, payload=None, session=None, origin="http://127.0.0.1:8787"):
        headers = [(b"Host", b"127.0.0.1:8787"), (b"Cookie", f"netizen_admin_session={session or self.session}".encode())]
        if method == "POST":
            headers.extend(((b"Origin", origin.encode()), (b"Content-Type", b"application/json")))
        response = await self.app.handle(Request(
            method=method.encode(), target=("/api/v1/group-access" + ("/save" if method == "POST" else "")).encode(),
            http_version=b"1.1", raw_headers=tuple(headers), body=json.dumps(payload).encode() if payload else b"",
            peer=("127.0.0.1", 12345), sockname=("127.0.0.1", 8787), request_id="group-access-test",
        ))
        return response.status, json.loads(response.body)

    @staticmethod
    def payload(view, mode="only", ids=("oc_group",)):
        return {**{key: view["saveAction"][key] for key in ("csrfToken", "actionToken", "target")},
                "mode": mode, "chatIds": list(ids)}

    async def test_save_preserves_list_in_all_mode_and_requires_single_use_grant(self):
        status, before = await self.request()
        self.assertEqual(status, 200)
        self.assertEqual(before["mode"], "all")
        payload = self.payload(before)
        status, saved = await self.request("POST", payload=payload)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["chatIds"], ["oc_group"])
        self.assertNotEqual(before["revision"], saved["revision"])
        self.assertEqual((await self.request("POST", payload=payload))[0], 409)
        status, saved = await self.request("POST", payload=self.payload(saved, "all"))
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.service.snapshot()[0], GroupAccessRule("all", ("oc_group",)))

    async def test_auth_origin_conflict_validation_and_write_failures(self):
        self.assertEqual((await self.request(session="invalid"))[0], 401)
        _, first = await self.request()
        _, old = await self.request()
        self.assertEqual((await self.request("POST", payload=self.payload(first), origin="http://elsewhere.test"))[0], 403)
        self.assertEqual((await self.request("POST", payload=self.payload(first)))[0], 200)
        status, error = await self.request("POST", payload=self.payload(old))
        self.assertEqual((status, error["code"]), (409, "stale_group_access"))
        _, view = await self.request()
        invalid = self.payload(view)
        invalid["mode"] = "exclude"
        self.assertEqual((await self.request("POST", payload=invalid))[0], 400)
        _, view = await self.request()
        before = self.service.snapshot()
        with patch("netizen_cli.admin.port_config.os.replace", side_effect=OSError("read only")):
            status, error = await self.request("POST", payload=self.payload(view, "all"))
        self.assertEqual((status, error["code"]), (503, "configuration_unavailable"))
        self.assertEqual(before, self.service.snapshot())
        _, view = await self.request()
        self.path.write_text(self.path.read_text() + "# manual change\n")
        status, error = await self.request("POST", payload=self.payload(view, "all"))
        self.assertEqual((status, error["code"]), (409, "configuration_changed"))
        self.assertEqual(before, self.service.snapshot())

    async def test_readonly_rule_has_no_save_grant(self):
        self.app._group_access = GroupAccessService()
        status, view = await self.request()
        self.assertEqual(status, 200)
        self.assertIsNone(view["saveAction"])

    async def test_name_lookups_are_bounded_and_refresh_makes_progress(self):
        saved_ids = tuple(f"oc_{index:032x}" for index in range(1000))
        provider = SimpleNamespace(get_chat_info=AsyncMock(return_value=SimpleNamespace(name="群名", chat_mode="group")))
        resolver = ChatLabelResolver(provider)
        self.addAsyncCleanup(resolver.aclose)
        self.fixture.management._chat_labels = resolver
        # A saved label outside the lookup page is still displayed from cache.
        await resolver.resolve_many(saved_ids[-1:], deadline=asyncio.get_running_loop().time() + 1)
        provider.get_chat_info.reset_mock()
        self.service.update(GroupAccessRule("all", saved_ids), expected_revision=self.service.snapshot()[1])
        status, view = await self.request()
        self.assertEqual(status, 200)
        self.assertEqual(provider.get_chat_info.await_count, 20)
        first = {call.args[0] for call in provider.get_chat_info.await_args_list}
        self.assertEqual(first, set(saved_ids[:20]))
        self.assertEqual(view["chats"][0], {"chatId": saved_ids[0], "name": "群名"})
        self.assertEqual(view["chats"][20], {"chatId": saved_ids[20], "name": saved_ids[20]})
        self.assertEqual(view["chats"][-1], {"chatId": saved_ids[-1], "name": "群名"})
        provider.get_chat_info.reset_mock()
        status, view = await self.request()
        self.assertEqual(status, 200)
        second = {call.args[0] for call in provider.get_chat_info.await_args_list}
        self.assertEqual(second, set(saved_ids[20:40]))
        provider.get_chat_info.reset_mock()
        status, view = await self.request("POST", payload=self.payload(view, ids=saved_ids))
        self.assertEqual(status, 200)
        self.assertEqual(view["chatIds"], list(saved_ids))
        self.assertEqual({call.args[0] for call in provider.get_chat_info.await_args_list}, set(saved_ids[40:60]))

    async def test_thousand_real_ids_save_within_transport_limit_and_over_limit_is_clear(self):
        _, view = await self.request()
        chat_ids = tuple(f"oc_{index:032x}" for index in range(1000))
        payload = self.payload(view, ids=chat_ids + chat_ids[:1])
        self.assertLess(len(json.dumps(payload).encode()), MAX_BODY_BYTES)
        status, saved = await self.request("POST", payload=payload)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["chatIds"], list(chat_ids))
        payload = self.payload(saved, ids=chat_ids + ("oc_ffffffffffffffffffffffffffffffff",))
        self.assertLess(len(json.dumps(payload).encode()), MAX_BODY_BYTES)
        status, error = await self.request("POST", payload=payload)
        self.assertEqual((status, error["code"]), (400, "invalid_group_access"))
        self.assertIn("1000", str(error))
