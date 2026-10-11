"""Login persistence admission after upstream and per-user credential waits."""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen_cli.admin.auth import AdminAuth, LoginRejected
from netizen_cli.admin.credentials import AdminCredentials
from netizen_cli.admin.feishu import DeviceChallenge, UserProfile, UserTokens
from netizen_cli.admin.login import AdminLogin
from netizen_cli.admin.users import AdminIdentity, AdminUsers


class LoginCommitTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.source = "192.0.2.100"
        self.users = AdminUsers(self.root, "cli_test")
        self.member = self.users.add_initial("ou_alice")
        self.auth = AdminAuth(self.users, clock=lambda: self.now)
        self.client = SimpleNamespace(
            start_login=AsyncMock(return_value=DeviceChallenge(
                "device", "code", "https://accounts.feishu.cn/verify", self.now + 600, 5)),
            poll_login=AsyncMock(return_value=self.tokens("new-login")),
            user_info=AsyncMock(return_value=UserProfile("ou_alice", "Alice", "")),
            refresh=AsyncMock(),
        )
        self.credentials = AdminCredentials(self.root, self.users, self.client, clock=lambda: self.now)
        self.login = AdminLogin(self.auth, self.users, self.credentials, self.client, clock=lambda: self.now)
        self.addCleanup(self.auth.close)
        self.addCleanup(self.credentials.close)
        self.addCleanup(self.login.close)
        self.path = self.root / "credentials/admin-user-tokens.json"

    def tokens(self, label, *, expired=False):
        return UserTokens("access-" + label, "refresh-" + label,
                          self.now - 1 if expired else self.now + 300,
                          self.now + 1000, ("offline_access", "component:selector"))

    async def start(self):
        preauth = self.auth.issue_preauth(self.source)
        await self.login.start(cookie=preauth.cookie_token, nonce=preauth.form_nonce, source=self.source)
        self.now += 5
        return preauth

    async def status(self, preauth):
        return await self.login.status(cookie=preauth.cookie_token, source=self.source)

    def saturate_failures(self):
        for suffix in range(1, 5):
            for _ in range(5):
                with self.assertRaises(LoginRejected):
                    self.auth.reject_login(f"192.0.2.{suffix}")

    async def test_rate_rejection_after_identity_wait_does_not_persist_tokens(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def profile(_token):
            entered.set()
            await release.wait()
            return UserProfile("ou_alice", "Alice", "")

        self.client.user_info.side_effect = profile
        preauth = await self.start()
        pending = asyncio.create_task(self.status(preauth))
        await asyncio.wait_for(entered.wait(), 1)
        self.saturate_failures()
        release.set()
        result, session = await pending
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(session)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.auth.state_counts().sessions, 0)

    async def test_rate_rechecked_after_shared_refresh_wait_preserves_rotated_credential(self):
        await self.credentials.save_login(self.member.identity, self.tokens("old", expired=True),
                                          expected_add_revision=self.member.add_revision)
        entered, release, identified = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def refresh(_token):
            entered.set()
            await release.wait()
            return self.tokens("renewed")

        async def profile(_token):
            identified.set()
            return UserProfile("ou_alice", "Alice", "")

        self.client.refresh.side_effect = refresh
        self.client.user_info.side_effect = profile
        refreshing = asyncio.create_task(self.credentials.access_token(self.member.identity, self.member.add_revision))
        await asyncio.wait_for(entered.wait(), 1)
        preauth = await self.start()
        pending = asyncio.create_task(self.status(preauth))
        await asyncio.wait_for(identified.wait(), 1)
        self.saturate_failures()
        release.set()
        self.assertEqual(await refreshing, "access-renewed")
        result, session = await pending
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(session)
        self.assertEqual(json.loads(self.path.read_text())["users"][0]["accessToken"], "access-renewed")

    async def test_other_thread_delete_readd_after_token_save_cannot_issue_stale_session(self):
        bob = self.users.add(AdminIdentity("cli_test", "ou_bob"), actor=self.member.identity,
                             expected_revision=self.users.revision)
        save_login = self.credentials.save_login

        def remove_and_readd():
            self.users.remove(self.member.identity, actor=bob.identity, expected_revision=self.users.revision)
            self.users.add(self.member.identity, actor=bob.identity, expected_revision=self.users.revision)

        async def save_then_revoke(*args, **kwargs):
            await save_login(*args, **kwargs)
            errors = []

            def run():
                try:
                    remove_and_readd()
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive(), "membership change did not finish")
            self.assertEqual(errors, [])

        preauth = await self.start()
        with patch.object(self.credentials, "save_login", side_effect=save_then_revoke):
            result, session = await self.status(preauth)
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(session)
        self.assertEqual(self.auth.state_counts().sessions, 0)
        self.assertEqual(json.loads(self.path.read_text())["users"], [])
        self.assertGreater(self.users.get_membership(self.member.identity).add_revision, self.member.add_revision)


if __name__ == "__main__":
    unittest.main()
