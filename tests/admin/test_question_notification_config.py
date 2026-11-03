from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

import yaml

from netizen_cli.admin.port_config import ConfigFileEditor, ConfigFileSnapshot
from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.transport import Request
from netizen_cli.admin.users import AdminIdentity, AdminUsers
from netizen_cli.admin.web import AdminWebApplication
from netizen_cli.question_notifications import QuestionNotificationConfigService, QuestionNotificationSettings
from tests.support.channel_fixtures import channel_fixture


def _action_payload(envelope):
    return {key: envelope[key] for key in ("csrfToken", "actionToken", "target")}


class AdminQuestionNotificationConfigTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        environment = await self.enterAsyncContext(channel_fixture())
        self.management = environment.management
        self.path = environment.project_root / "config.yaml"
        self.path.write_text("instance: {}\n")
        self.config = QuestionNotificationConfigService(config_editor=ConfigFileEditor(ConfigFileSnapshot.read(self.path)))
        self.management._question_notification_config = self.config
        users = AdminUsers(environment.project_root, "cli_test")
        self.member = users.add_initial("ou_admin")
        self.auth = AdminAuth(users)
        self.application = AdminWebApplication(auth=self.auth, management=self.management)
        self.addCleanup(self.application.close_auth)
        self.addAsyncCleanup(self._drain)
        self.application.configure_authorities(("127.0.0.1:8787",))
        self.application.open_admission()

    async def _drain(self):
        await self.application.drain(asyncio.get_running_loop().time() + 1)

    async def login(self):
        return self.auth.issue_session(
            AdminIdentity("cli_test", "ou_admin"), source_ip="127.0.0.1",
            expected_add_revision=self.member.add_revision,
        ).token

    async def request(self, method, target, *, headers=(), body=b""):
        response = await self.application.handle(Request(
            method=method.encode(), target=target.encode(), http_version=b"1.1",
            raw_headers=((b"Host", b"127.0.0.1:8787"),
                         *((name.encode(), value.encode()) for name, value in headers)),
            body=body, peer=("127.0.0.1", 12345), sockname=("127.0.0.1", 8787),
            request_id="question-notifications-config-test",
        ))
        return response.status, response.headers, response.body

    async def json_get(self, target, session):
        status, headers, body = await self.request("GET", target, headers=[
            ("Cookie", f"netizen_admin_session={session}"),
        ])
        return status, headers, json.loads(body) if body else None

    async def json_post(self, target, session, payload):
        status, headers, body = await self.request("POST", target, headers=[
            ("Cookie", f"netizen_admin_session={session}"),
            ("Origin", "http://127.0.0.1:8787"),
            ("Content-Type", "application/json"),
        ], body=json.dumps(payload).encode())
        return status, headers, json.loads(body) if body else None

    async def settings_view(self, session):
        status, _, result = await self.json_get("/api/v1/question-notifications/settings", session)
        self.assertEqual(status, 200, result)
        return result

    async def test_settings_require_authentication_and_save_uses_single_use_grant(self):
        status, _, _ = await self.request("GET", "/static/question-notifications.js")
        self.assertEqual(status, 401)
        status, _, _ = await self.json_get("/api/v1/question-notifications/settings", "invalid")
        self.assertEqual(status, 401)
        status, _, _ = await self.json_post("/api/v1/question-notifications/settings", "invalid", {})
        self.assertEqual(status, 401)
        session = await self.login()
        status, _, content = await self.request("GET", "/static/question-notifications.js", headers=[
            ("Cookie", f"netizen_admin_session={session}"),
        ])
        self.assertEqual(status, 200)
        self.assertTrue(content)
        before = await self.settings_view(session)
        settings = QuestionNotificationSettings(mention_initiator=True)
        body = {**_action_payload(before["actions"]["save"]), "settings": settings.to_dict()}
        for invalid, expected in (
            ({**body, "csrfToken": "invalid"}, 403),
            ({**body, "revision": "invented"}, 400),
            ({**body, "target": {"resource": "question-notifications-settings", "targetId": "other"}}, 409),
        ):
            status, _, _ = await self.json_post("/api/v1/question-notifications/settings", session, invalid)
            self.assertEqual(status, expected)
            self.assertEqual(self.config.current, QuestionNotificationSettings())
        status, _, result = await self.json_post("/api/v1/question-notifications/settings", session, body)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["settings"], settings.to_dict())
        self.assertNotEqual(result["revision"], before["revision"])
        self.assertNotEqual(result["actions"]["save"], before["actions"]["save"])
        self.assertEqual(yaml.safe_load(self.path.read_text())["questionNotifications"], settings.to_dict())
        status, _, _ = await self.json_post("/api/v1/question-notifications/settings", session, body)
        self.assertEqual(status, 409)

    async def test_stale_settings_grant_does_not_overwrite_newer_save(self):
        session = await self.login()
        old = await self.settings_view(session)
        current = await self.settings_view(session)
        settings = QuestionNotificationSettings(mention_initiator=True)
        body = {**_action_payload(current["actions"]["save"]), "settings": settings.to_dict()}
        self.assertEqual((await self.json_post("/api/v1/question-notifications/settings", session, body))[0], 200)
        status, _, error = await self.json_post("/api/v1/question-notifications/settings", session, {
            **_action_payload(old["actions"]["save"]), "settings": QuestionNotificationSettings().to_dict(),
        })
        self.assertEqual(status, 409)
        self.assertEqual(error["code"], "stale_question_notification_settings")
        self.assertEqual(self.config.current, settings)

    async def test_invalid_settings_are_rejected_before_persistence(self):
        session = await self.login()
        default = QuestionNotificationSettings().to_dict()
        for value in (None, {}, {**default, "unexpected": True}, {**default, "mentionInitiator": 1}):
            with self.subTest(value=value):
                view = await self.settings_view(session)
                status, _, error = await self.json_post("/api/v1/question-notifications/settings", session, {
                    **_action_payload(view["actions"]["save"]), "settings": value,
                })
                self.assertEqual(status, 400)
                self.assertEqual(error["code"], "invalid_question_notification_settings")
        self.assertEqual(self.config.current, QuestionNotificationSettings())
        self.assertEqual(yaml.safe_load(self.path.read_text()), {"instance": {}})

    async def test_write_failure_and_external_edit_preserve_current_configuration(self):
        session = await self.login()
        settings = QuestionNotificationSettings(mention_initiator=True).to_dict()
        view = await self.settings_view(session)
        with patch("netizen_cli.admin.port_config.os.replace", side_effect=OSError("read-only")):
            status, _, error = await self.json_post("/api/v1/question-notifications/settings", session, {
                **_action_payload(view["actions"]["save"]), "settings": settings,
            })
        self.assertEqual(status, 503)
        self.assertEqual(error["code"], "configuration_unavailable")
        view = await self.settings_view(session)
        self.path.write_text("instance: {}\n# manual change\n")
        status, _, error = await self.json_post("/api/v1/question-notifications/settings", session, {
            **_action_payload(view["actions"]["save"]), "settings": settings,
        })
        self.assertEqual(status, 409)
        self.assertEqual(error["code"], "configuration_changed")
        self.assertEqual(self.config.current, QuestionNotificationSettings())

    async def test_readonly_configuration_has_no_save_action(self):
        self.management._question_notification_config = QuestionNotificationConfigService()
        view = await self.settings_view(await self.login())
        self.assertFalse(view["editable"])
        self.assertEqual(view["actions"], {})
