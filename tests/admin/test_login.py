from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from netizen_cli.admin.auth import AdminAuth, LoginRejected, SessionRejected
from netizen_cli.admin.credentials import AdminCredentials
from netizen_cli.admin.feishu import DeviceChallenge, SlowDownError, UserProfile, UserTokens
from netizen_cli.admin.login import AdminLogin
from netizen_cli.admin.users import AdminIdentity, AdminUsers


class AdminLoginTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.source = "192.0.2.1"
        self.users = AdminUsers(self.root, "cli_test")
        self.alice = self.users.add_initial("ou_alice")
        self.auth = AdminAuth(self.users)
        self.tokens = UserTokens("private-access", "private-refresh", self.now + 100,
                                 self.now + 1000, ("offline_access", "component:selector"))
        self.client = SimpleNamespace(
            start_login=AsyncMock(return_value=DeviceChallenge(
                "private-device-code", "USER-CODE", "https://accounts.feishu.cn/verify",
                self.now + 100, 5)),
            poll_login=AsyncMock(return_value=self.tokens),
            user_info=AsyncMock(return_value=UserProfile("ou_alice", "Alice", "")),
            refresh=AsyncMock(),
        )
        self.credentials = AdminCredentials(self.root, self.users, self.client, clock=lambda: self.now)
        self.addCleanup(self.credentials.close)
        self.login = AdminLogin(self.auth, self.users, self.credentials, self.client,
                                clock=lambda: self.now)
        self.addCleanup(self.login.close)
        self.path = self.root / "credentials/admin-user-tokens.json"

    async def start(self):
        preauth = self.auth.issue_preauth(self.source)
        result = await self.login.start(cookie=preauth.cookie_token, nonce=preauth.form_nonce,
                                        source=self.source)
        self.assertEqual(result["state"], "pending")
        self.now += 5
        return preauth

    async def status(self, preauth):
        return await self.login.status(cookie=preauth.cookie_token, source=self.source)

    async def test_verified_member_gets_opaque_session_without_browser_token_exposure(self) -> None:
        preauth = await self.start()
        result, session = await self.status(preauth)
        self.assertEqual(result["state"], "authenticated")
        self.assertEqual(self.auth.authenticate(session).identity, self.alice.identity)
        self.assertNotIn(session, json.dumps(result))
        for secret in (self.tokens.access_token, self.tokens.refresh_token, "private-device-code"):
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, repr(self.client.start_login.return_value))
        self.assertTrue(self.path.exists())

    async def test_mismatched_browser_nonce_and_replay_never_start_oauth(self) -> None:
        first = self.auth.issue_preauth(self.source)
        second = self.auth.issue_preauth(self.source)
        with self.assertRaises(LoginRejected):
            await self.login.start(cookie=first.cookie_token, nonce=second.form_nonce, source=self.source)
        self.client.start_login.assert_not_awaited()
        good = await self.start()
        with self.assertRaises(LoginRejected):
            await self.login.start(cookie=good.cookie_token, nonce=good.form_nonce, source=self.source)
        self.client.start_login.assert_awaited_once()

    async def test_other_browser_or_source_cannot_poll_or_cancel_attempt(self) -> None:
        owner = await self.start()
        stranger = self.auth.issue_preauth(self.source)
        self.assertEqual(await self.status(stranger), ({"state": "idle"}, None))
        self.assertEqual(await self.login.status(cookie=owner.cookie_token, source="192.0.2.2"),
                         ({"state": "idle"}, None))
        with self.assertRaises(LoginRejected):
            self.login.cancel(cookie=owner.cookie_token, nonce=stranger.form_nonce, source=self.source)
        with self.assertRaises(LoginRejected):
            self.login.cancel(cookie=owner.cookie_token, nonce=owner.form_nonce, source="192.0.2.2")
        self.client.poll_login.assert_not_awaited()
        self.assertEqual((await self.status(owner))[0]["state"], "authenticated")

    async def test_nonadministrator_finishes_denied_without_persisting_credentials(self) -> None:
        self.client.user_info.return_value = UserProfile("ou_stranger", "Stranger", "")
        preauth = await self.start()
        result, session = await self.status(preauth)
        self.assertEqual(result["state"], "denied")
        self.assertIsNone(session)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.auth.state_counts().sessions, 0)

    async def test_upstream_failure_never_exposes_tokens_or_device_codes(self) -> None:
        self.client.poll_login.side_effect = RuntimeError(
            "private-access private-refresh private-device-code",
        )
        preauth = await self.start()
        result, session = await self.status(preauth)
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(session)
        for secret in ("private-access", "private-refresh", "private-device-code"):
            self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(self.path.exists())

    async def test_cancelled_inflight_identity_cannot_save_tokens_or_session(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def profile(_token):
            entered.set()
            await release.wait()
            return UserProfile("ou_alice", "Alice", "")

        self.client.user_info.side_effect = profile
        preauth = await self.start()
        pending = asyncio.create_task(self.status(preauth))
        await asyncio.wait_for(entered.wait(), 1)
        self.login.cancel(cookie=preauth.cookie_token, nonce=preauth.form_nonce, source=self.source)
        release.set()
        self.assertEqual(await pending, ({"state": "idle"}, None))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.auth.state_counts().sessions, 0)

    async def test_expired_inflight_identity_cannot_save_tokens_or_session(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def profile(_token):
            entered.set()
            await release.wait()
            return UserProfile("ou_alice", "Alice", "")

        self.client.user_info.side_effect = profile
        preauth = await self.start()
        pending = asyncio.create_task(self.status(preauth))
        await asyncio.wait_for(entered.wait(), 1)
        self.now += 200
        release.set()
        result, session = await pending
        self.assertNotEqual(result["state"], "authenticated")
        self.assertIsNone(session)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.auth.state_counts().sessions, 0)

    async def test_delete_readd_during_identity_lookup_rejects_old_attempt(self) -> None:
        bob = self.users.add(AdminIdentity("cli_test", "ou_bob"), actor=self.alice.identity,
                             expected_revision=self.users.revision)
        entered, release = asyncio.Event(), asyncio.Event()

        async def profile(_token):
            entered.set()
            await release.wait()
            return UserProfile("ou_alice", "Alice", "")

        self.client.user_info.side_effect = profile
        preauth = await self.start()
        pending = asyncio.create_task(self.status(preauth))
        await asyncio.wait_for(entered.wait(), 1)
        self.users.remove(self.alice.identity, actor=bob.identity, expected_revision=self.users.revision)
        self.users.add(self.alice.identity, actor=bob.identity, expected_revision=self.users.revision)
        release.set()
        result, session = await pending
        self.assertEqual(result["state"], "denied")
        self.assertIsNone(session)
        self.assertFalse(self.path.exists())

    async def test_interval_slow_down_and_concurrent_polling_follow_one_upstream_attempt(self) -> None:
        self.client.poll_login.side_effect = [SlowDownError(), self.tokens]
        preauth = await self.start()
        first, session = await self.status(preauth)
        self.assertEqual(first["interval"], 10)
        self.assertIsNone(session)
        await self.status(preauth)
        self.client.poll_login.assert_awaited_once()
        self.now += 10
        results = await asyncio.gather(self.status(preauth), self.status(preauth))
        self.assertEqual(self.client.poll_login.await_count, 2)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.auth.state_counts().sessions, 1)

    async def test_close_while_waiting_for_device_challenge_discards_late_result(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def challenge():
            entered.set()
            await release.wait()
            return DeviceChallenge("secret", "code", "https://accounts.feishu.cn/verify", self.now + 100, 5)

        self.client.start_login.side_effect = challenge
        preauth = self.auth.issue_preauth(self.source)
        pending = asyncio.create_task(self.login.start(cookie=preauth.cookie_token,
                        nonce=preauth.form_nonce, source=self.source))
        await asyncio.wait_for(entered.wait(), 1)
        self.login.close()
        release.set()
        with self.assertRaises(LoginRejected):
            await pending
        self.assertFalse(self.path.exists())

    async def test_reset_after_success_revokes_only_attempt_session(self) -> None:
        first = await self.start()
        _, first_session = await self.status(first)
        second = await self.start()
        _, second_session = await self.status(second)
        before = self.path.read_bytes()
        self.login.discard(first.cookie_token)
        with self.assertRaises(SessionRejected):
            self.auth.authenticate(first_session)
        self.assertEqual(self.auth.authenticate(second_session).identity, self.alice.identity)
        self.assertEqual(self.path.read_bytes(), before)
