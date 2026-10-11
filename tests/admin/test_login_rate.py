"""Login-start quotas and terminal handoff behavior, without network traffic."""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from netizen_cli.admin.auth import AdminAuth, LoginRejected
from netizen_cli.admin.feishu import AuthExpired, DeviceChallenge, FeishuError, UserProfile, UserTokens
from netizen_cli.admin.login import AdminLogin
from netizen_cli.admin.users import AdminUsers, PrivateJSONError


class AdminLoginRateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = 1000.0
        self.users = AdminUsers(Path(temporary.name), "cli_test")
        self.users.add_initial("ou_alice")
        self.auth = AdminAuth(self.users, clock=lambda: self.now)
        self.addCleanup(self.auth.close)
        self.client = SimpleNamespace(
            start_login=AsyncMock(side_effect=lambda: DeviceChallenge(
                "private-device", "USER-CODE", "https://accounts.feishu.cn/verify",
                self.now + 600, 5,
            )),
            poll_login=AsyncMock(return_value=UserTokens("private-access", None, 2000, None, ())),
            user_info=AsyncMock(return_value=UserProfile("ou_alice", "Alice", "")),
        )
        self.credentials = SimpleNamespace(save_login=AsyncMock())
        self.login = AdminLogin(self.auth, self.users, self.credentials, self.client,
                                clock=lambda: self.now)
        self.addCleanup(self.login.close)

    async def start(self, source="192.0.2.1"):
        preauth = self.auth.issue_preauth(source)
        result = await self.login.start(cookie=preauth.cookie_token, nonce=preauth.form_nonce, source=source)
        self.assertEqual(result["state"], "pending")
        return preauth

    def cancel(self, preauth, source="192.0.2.1"):
        self.login.cancel(cookie=preauth.cookie_token, nonce=preauth.form_nonce, source=source)

    async def authenticate(self, source="192.0.2.1"):
        preauth = await self.start(source)
        self.now += 5
        result = await self.login.status(cookie=preauth.cookie_token, source=source)
        self.assertEqual(result[0]["state"], "authenticated")
        self.assertIsNotNone(result[1])
        return preauth, result

    async def test_cancel_does_not_refund_source_quota_and_window_expires(self):
        for _ in range(5):
            self.cancel(await self.start())
        with self.assertRaises(LoginRejected):
            await self.start()
        self.assertEqual(self.client.start_login.await_count, 5)
        self.now += 299
        with self.assertRaises(LoginRejected):
            await self.start()
        self.now += 1
        await self.start()
        self.assertEqual(self.client.start_login.await_count, 6)

    async def test_global_quota_covers_distinct_sources_and_releases_at_window_boundary(self):
        for suffix in range(1, 5):
            source = f"192.0.2.{suffix}"
            for _ in range(5):
                self.cancel(await self.start(source), source)
        with self.assertRaises(LoginRejected):
            await self.start("192.0.2.100")
        self.assertEqual(self.client.start_login.await_count, 20)
        self.now += 300
        await self.start("192.0.2.100")
        self.assertEqual(self.client.start_login.await_count, 21)

    async def test_ipv4_mapped_address_cannot_bypass_source_quota(self):
        for index in range(5):
            source = "192.0.2.1" if index % 2 else "::ffff:192.0.2.1"
            self.cancel(await self.start(source), source)
        with self.assertRaises(LoginRejected):
            await self.start("::ffff:c000:201")
        self.assertEqual(self.client.start_login.await_count, 5)

    async def test_upstream_start_failure_does_not_refund_quota(self):
        self.client.start_login.side_effect = FeishuError("upstream_unavailable", "不可用")
        for _ in range(5):
            with self.assertRaises(FeishuError):
                await self.start()
        with self.assertRaises(LoginRejected):
            await self.start()
        self.assertEqual(self.client.start_login.await_count, 5)

    async def test_concurrent_attempt_capacity_is_separate_from_start_quota(self):
        attempts = [await self.start() for _ in range(4)]
        with self.assertRaises(LoginRejected):
            await self.start()
        self.assertEqual(self.client.start_login.await_count, 4)
        self.cancel(attempts[0])
        await self.start()
        self.assertEqual(self.client.start_login.await_count, 5)
        self.now += 601
        await self.start()
        self.assertEqual(self.client.start_login.await_count, 6)

    async def test_completed_logins_release_capacity_after_start_window(self):
        completed = [await self.authenticate() for _ in range(4)]
        self.now += 300
        await self.start()
        for preauth, result in completed:
            self.assertEqual(await self.login.status(cookie=preauth.cookie_token, source="192.0.2.1"), result)
            self.auth.authenticate(result[1])
        self.assertEqual(self.client.start_login.await_count, 5)
        self.assertEqual(self.client.poll_login.await_count, 4)

    async def test_completed_logins_do_not_refund_start_quota(self):
        for _ in range(5):
            await self.authenticate()
        with self.assertRaises(LoginRejected):
            await self.start()
        self.assertEqual(self.client.start_login.await_count, 5)

    async def test_terminal_retention_is_bounded_without_revoking_sessions_or_pending(self):
        with patch("netizen_cli.admin.login._MAX_ATTEMPTS", 4):
            pending = await self.start("192.0.2.2")
            completed = [await self.authenticate() for _ in range(3)]
            for _ in range(2):
                completed.append(await self.authenticate())
                self.assertEqual(len(self.login._attempts), 4)
            self.assertEqual(self.login.verification_uri(pending.cookie_token, "192.0.2.2"),
                             "https://accounts.feishu.cn/verify")
            for preauth, result in completed[:2]:
                self.assertEqual(await self.login.status(cookie=preauth.cookie_token, source="192.0.2.1"),
                                 ({"state": "idle"}, None))
                self.auth.authenticate(result[1])
            for preauth, result in completed[2:]:
                self.assertEqual(await self.login.status(cookie=preauth.cookie_token, source="192.0.2.1"), result)
                self.auth.authenticate(result[1])

    async def test_global_pending_capacity_never_reclaims_active_attempts(self):
        with patch("netizen_cli.admin.login._MAX_ATTEMPTS", 4):
            attempts = [(f"192.0.2.{suffix}", await self.start(f"192.0.2.{suffix}"))
                        for suffix in range(1, 5)]
            with self.assertRaises(LoginRejected):
                await self.start("192.0.2.5")
            for source, preauth in attempts:
                self.assertEqual(self.login.verification_uri(preauth.cookie_token, source),
                                 "https://accounts.feishu.cn/verify")
            self.assertEqual(self.client.start_login.await_count, 4)

    async def test_denial_returns_terminal_on_first_response_and_counts_failure_once(self):
        self.client.user_info.return_value = UserProfile("ou_stranger", "Stranger", "")
        preauth = await self.start()
        self.now += 5
        first = await self.login.status(cookie=preauth.cookie_token, source="192.0.2.1")
        self.assertEqual(first[0]["state"], "denied")
        self.assertIsNone(first[1])
        self.assertEqual(self.auth.state_counts().global_failures, 1)
        self.assertEqual(await self.login.status(cookie=preauth.cookie_token, source="192.0.2.1"), first)
        self.assertEqual(self.auth.state_counts().global_failures, 1)

    async def test_expiry_errors_return_terminal_expired_without_raising_rejection(self):
        for code in ("expired_token", "token_expired", "login_expired", "auth_expired"):
            with self.subTest(code=code):
                self.client.poll_login.side_effect = AuthExpired(code, "已过期")
                preauth = await self.start()
                self.now += 5
                result, session = await self.login.status(cookie=preauth.cookie_token, source="192.0.2.1")
                self.assertEqual(result["state"], "expired")
                self.assertIsNone(session)
        await self.start()
        self.assertEqual(self.client.start_login.await_count, 5)

    async def test_profile_save_failure_never_issues_or_leaves_an_orphan_session(self):
        preauth = await self.start()
        self.now += 5
        with patch.object(self.users, "update_profile", side_effect=PrivateJSONError()), \
                patch.object(self.auth, "issue_session", wraps=self.auth.issue_session) as issue:
            result, session = await self.login.status(cookie=preauth.cookie_token, source="192.0.2.1")
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(session)
        issue.assert_not_called()
        self.assertEqual(self.auth.state_counts().sessions, 0)
