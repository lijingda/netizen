from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from lark_oapi.api.authen.v1 import GetUserInfoResponse

from netizen_cli.admin.auth import AdminAuth, AdmissionClosed, SessionRejected
from netizen_cli.admin.credentials import AdminCredentials
from netizen_cli.admin.feishu import AuthExpired, FeishuClient, FeishuError, UserProfile, UserTokens
from netizen_cli.admin.users import (
    AdminIdentity, AdminUsers, AdminUsersUnavailable, PrivateJSONError, write_private_json,
)


class AdminCredentialsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.users = AdminUsers(self.root, "cli_test")
        self.alice = self.users.add_initial("ou_alice")
        self.bob = self.users.add(
            AdminIdentity("cli_test", "ou_bob"), actor=self.alice.identity,
            expected_revision=self.users.revision,
        )
        self.client = SimpleNamespace(
            refresh=AsyncMock(), user_info=AsyncMock(), selector_config=AsyncMock(),
        )
        self.credentials = AdminCredentials(self.root, self.users, self.client,
                                            clock=lambda: self.now)
        self.addCleanup(self.credentials.close)
        self.path = self.root / "credentials/admin-user-tokens.json"

    def tokens(self, label: str, *, expired: bool = False) -> UserTokens:
        return UserTokens(f"private-access-{label}", f"private-refresh-{label}",
                          self.now - 1 if expired else self.now + 100,
                          self.now + 1000, ("offline_access", "component:selector"))

    async def save(self, tokens: UserTokens) -> None:
        await self.credentials.save_login(
            self.alice.identity, tokens, expected_add_revision=self.alice.add_revision,
        )

    async def access(self) -> str:
        return await self.credentials.access_token(self.alice.identity, self.alice.add_revision)

    def remove_alice(self) -> None:
        self.users.remove(self.alice.identity, actor=self.bob.identity,
                          expected_revision=self.users.revision)

    def add_alice_again(self):
        return self.users.add(self.alice.identity, actor=self.bob.identity,
                              expected_revision=self.users.revision)

    async def protocol_credentials(self, status: int, code: int):
        """Exercise the production protocol adapter with synthetic API responses."""
        read_tokens, refresh_requests = [], []

        def api_response(token, success):
            read_tokens.append(token)
            if len(read_tokens) == 1:
                return status, {"code": code, "msg": "private-upstream-error"}
            return 200, {"code": 0, "data": success}

        async def user_info(_request, option):
            response_status, payload = api_response(option.user_access_token, {
                "open_id": "ou_alice", "name": "Alice", "avatar_url": "",
            })
            response = GetUserInfoResponse(payload)
            response.raw = SimpleNamespace(status_code=response_status)
            return response

        def handle(request):
            if request.url.path == "/oauth/v3/token":
                refresh_requests.append(request)
                return httpx.Response(200, json={
                    "access_token": "private-access-renewed", "refresh_token": "private-refresh-renewed",
                    "expires_in": 7200, "refresh_token_expires_in": 86400,
                    "token_type": "Bearer", "scope": "component:selector offline_access",
                })
            self.assertEqual(request.url.path, "/open-apis/jssdk/ticket/get")
            response_status, payload = api_response(request.headers["Authorization"].removeprefix("Bearer "), {
                "ticket": "private-ticket", "expire_in": 7200,
            })
            return httpx.Response(response_status, json=payload)

        http = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        sdk = SimpleNamespace(authen=SimpleNamespace(v1=SimpleNamespace(
            user_info=SimpleNamespace(aget=user_info),
        )))
        client = FeishuClient("cli_test", "private-app-secret", http_client=http, sdk_client=sdk)
        self.addAsyncCleanup(client.aclose)
        credentials = AdminCredentials(self.root, self.users, client, clock=lambda: self.now)
        self.addCleanup(credentials.close)
        return credentials, read_tokens, refresh_requests

    async def test_private_tokens_survive_restart_without_restoring_browser_sessions(self) -> None:
        tokens = self.tokens("first")
        await self.save(tokens)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        auth = AdminAuth(self.users)
        session = auth.issue_session(self.alice.identity, source_ip="192.0.2.1",
                                     expected_add_revision=self.alice.add_revision)
        self.credentials.close()
        restored = AdminCredentials(self.root, self.users, self.client, clock=lambda: self.now)
        self.addCleanup(restored.close)
        self.assertEqual(await restored.access_token(self.alice.identity, self.alice.add_revision),
                         tokens.access_token)
        with self.assertRaises(SessionRejected):
            AdminAuth(self.users).authenticate(session.token)
        self.client.refresh.assert_not_awaited()

    async def test_concurrent_requests_refresh_one_time_and_persist_rotated_pair(self) -> None:
        await self.save(self.tokens("old", expired=True))
        entered, release = asyncio.Event(), asyncio.Event()
        renewed = self.tokens("renewed")

        async def refresh(_token):
            self.assertTrue(json.loads(self.path.read_text())["users"][0]["refreshAttempted"])
            entered.set()
            await release.wait()
            return renewed

        self.client.refresh.side_effect = refresh
        first = asyncio.create_task(self.access())
        await asyncio.wait_for(entered.wait(), 1)
        requests = [first, *(asyncio.create_task(self.access()) for _ in range(8))]
        release.set()
        self.assertEqual(await asyncio.gather(*requests), [renewed.access_token] * 9)
        self.client.refresh.assert_awaited_once_with("private-refresh-old")
        stored = json.loads(self.path.read_text())["users"][0]
        self.assertEqual((stored["accessToken"], stored["refreshToken"]),
                         (renewed.access_token, renewed.refresh_token))
        self.assertFalse(stored["refreshAttempted"])

    async def test_new_login_waits_for_refresh_then_becomes_current(self) -> None:
        await self.save(self.tokens("old", expired=True))
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def refresh(_token):
            entered.set()
            await release.wait()
            return self.tokens("renewed")

        async def login():
            queued.set()
            await self.save(self.tokens("new-login"))

        self.client.refresh.side_effect = refresh
        refreshing = asyncio.create_task(self.access())
        await asyncio.wait_for(entered.wait(), 1)
        logging_in = asyncio.create_task(login())
        await asyncio.wait_for(queued.wait(), 1)
        self.assertFalse(logging_in.done())
        release.set()
        await asyncio.gather(refreshing, logging_in)
        self.assertEqual(await self.access(), "private-access-new-login")

    async def test_delete_and_readd_rejects_inflight_refresh_and_old_incarnation_on_restart(self) -> None:
        await self.save(self.tokens("old", expired=True))
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(_token):
            entered.set()
            await release.wait()
            return self.tokens("late")

        self.client.refresh.side_effect = refresh
        refreshing = asyncio.create_task(self.access())
        await asyncio.wait_for(entered.wait(), 1)
        self.remove_alice()
        replacement = self.add_alice_again()
        release.set()
        with self.assertRaises(AuthExpired):
            await refreshing
        restored = AdminCredentials(self.root, self.users, self.client, clock=lambda: self.now)
        self.addCleanup(restored.close)
        with self.assertRaises(AuthExpired):
            await restored.access_token(replacement.identity, replacement.add_revision)
        self.assertNotIn("private-access-late", self.path.read_text())

    async def test_logout_leaves_shared_credentials_and_other_browser_valid(self) -> None:
        await self.save(self.tokens("shared"))
        auth = AdminAuth(self.users)
        first, second = [auth.issue_session(self.alice.identity, source_ip=source,
                         expected_add_revision=self.alice.add_revision)
                         for source in ("192.0.2.1", "192.0.2.2")]
        before = self.path.read_bytes()
        auth.logout(first.token)
        with self.assertRaises(SessionRejected):
            auth.authenticate(first.token)
        self.assertEqual(auth.authenticate(second.token).identity, self.alice.identity)
        self.assertEqual(await self.access(), "private-access-shared")
        self.assertEqual(self.path.read_bytes(), before)

    async def test_nonmember_or_cancelled_login_never_writes_credentials(self) -> None:
        with self.assertRaises(AuthExpired):
            await self.credentials.save_login(AdminIdentity("cli_test", "ou_stranger"),
                                             self.tokens("stranger"), expected_add_revision=1)
        with self.assertRaises(AuthExpired):
            await self.credentials.save_login(self.alice.identity, self.tokens("cancelled"),
                                             expected_add_revision=self.alice.add_revision,
                                             is_current=lambda: False)
        self.assertFalse(self.path.exists())

    async def test_failed_private_write_never_publishes_new_login_token(self) -> None:
        await self.save(self.tokens("current"))
        with patch("netizen_cli.admin.credentials.write_private_json", side_effect=PrivateJSONError()):
            with self.assertRaises(PrivateJSONError):
                await self.save(self.tokens("uncommitted"))
        self.assertEqual(await self.access(), "private-access-current")
        self.assertNotIn("private-access-uncommitted", self.path.read_text())

    async def test_roster_io_failure_closes_access_without_deleting_durable_credentials(self) -> None:
        await self.save(self.tokens("alice"))
        await self.credentials.save_login(self.bob.identity, self.tokens("bob"),
                                         expected_add_revision=self.bob.add_revision)
        saved_tokens = self.path.read_bytes()
        auth = AdminAuth(self.users)
        sessions = [auth.issue_session(member.identity, source_ip="192.0.2.1",
                                       expected_add_revision=member.add_revision)
                    for member in (self.alice, self.bob)]
        self.client.user_info.return_value = UserProfile("ou_alice", "Updated Alice", "")
        real_replace = os.replace

        def fail_only_roster_replace(source, destination):
            if Path(destination) == self.users.path:
                raise OSError("simulated roster write failure")
            return real_replace(source, destination)

        # Credential-file writes remain possible: an incorrect revocation
        # callback would really erase the tokens and fail the byte comparison.
        with patch("netizen_cli.admin.users.os.replace", side_effect=fail_only_roster_replace):
            with self.assertRaises(AdminUsersUnavailable):
                await self.credentials.profile(self.alice.identity, self.alice.add_revision)
        with self.assertRaises(AdminUsersUnavailable):
            self.users.list()
        for session in sessions:
            with self.assertRaises(AdmissionClosed):
                auth.authenticate(session.token)
        self.assertEqual(self.path.read_bytes(), saved_tokens)

        self.credentials.close()
        restored_users = AdminUsers(self.root, "cli_test")
        restored = AdminCredentials(self.root, restored_users, self.client, clock=lambda: self.now)
        self.addCleanup(restored.close)
        for member, expected in ((self.alice, "private-access-alice"),
                                 (self.bob, "private-access-bob")):
            self.assertEqual(await restored.access_token(member.identity, member.add_revision), expected)
        self.assertEqual(restored_users.get_membership(self.alice.identity).name, "")
        restored.close()

        # A later offline roster edit can leave the credential file untouched;
        # startup must use the persisted membership incarnation, not that file's
        # mere presence, when deciding which credentials can still be read.
        offline_users = AdminUsers(self.root, "cli_test")
        offline_users.remove(self.alice.identity, actor=self.bob.identity,
                             expected_revision=offline_users.revision)
        replacement = offline_users.add(self.alice.identity, actor=self.bob.identity,
                                        expected_revision=offline_users.revision)
        self.assertEqual(self.path.read_bytes(), saved_tokens)
        changed = AdminCredentials(self.root, offline_users, self.client, clock=lambda: self.now)
        self.addCleanup(changed.close)
        with self.assertRaises(AuthExpired):
            await changed.access_token(replacement.identity, replacement.add_revision)
        self.assertEqual(await changed.access_token(self.bob.identity, self.bob.add_revision),
                         "private-access-bob")
        self.client.refresh.assert_not_awaited()

    async def test_failed_refresh_never_retries_one_use_token_in_later_requests_or_restart(self) -> None:
        await self.save(self.tokens("old", expired=True))
        self.client.refresh.side_effect = TimeoutError("private-refresh-old")
        for _ in range(2):
            with self.assertRaises(AuthExpired) as raised:
                await self.access()
            self.assertNotIn("private-refresh-old", str(raised.exception))
        restored = AdminCredentials(self.root, self.users, self.client, clock=lambda: self.now)
        self.addCleanup(restored.close)
        with self.assertRaises(AuthExpired):
            await restored.access_token(self.alice.identity, self.alice.add_revision)
        self.client.refresh.assert_awaited_once()

    async def test_cancelled_inflight_refresh_never_reuses_potentially_consumed_token(self) -> None:
        await self.save(self.tokens("old", expired=True))
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(_token):
            entered.set()
            await release.wait()
            return self.tokens("renewed")

        self.client.refresh.side_effect = refresh
        refreshing = asyncio.create_task(self.access())
        await asyncio.wait_for(entered.wait(), 1)
        refreshing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await refreshing
        release.set()
        with self.assertRaises(AuthExpired):
            await self.access()
        self.client.refresh.assert_awaited_once()

    async def test_rotation_save_failure_keeps_consumed_refresh_unusable_after_restart(self) -> None:
        await self.save(self.tokens("old", expired=True))
        self.client.refresh.return_value = self.tokens("renewed")

        def write_except_renewal(path, value):
            if any(row["accessToken"] == "private-access-renewed" for row in value["users"]):
                raise PrivateJSONError()
            write_private_json(path, value)

        with patch("netizen_cli.admin.credentials.write_private_json", side_effect=write_except_renewal):
            with self.assertRaises(AuthExpired):
                await self.access()
        restored = AdminCredentials(self.root, self.users, self.client, clock=lambda: self.now)
        self.addCleanup(restored.close)
        with self.assertRaises(AuthExpired):
            await restored.access_token(self.alice.identity, self.alice.add_revision)
        self.client.refresh.assert_awaited_once()

    async def test_attempt_record_save_failure_sends_no_refresh_request(self) -> None:
        await self.save(self.tokens("old", expired=True))
        with patch("netizen_cli.admin.credentials.write_private_json", side_effect=PrivateJSONError()):
            with self.assertRaises(AuthExpired):
                await self.access()
        self.client.refresh.assert_not_awaited()

    async def test_explicit_api_expiry_refreshes_and_retries_profile_and_ticket_once(self) -> None:
        for endpoint in ("profile", "selector_config"):
            for status in (200, 401):
                with self.subTest(endpoint=endpoint, status=status):
                    await self.save(self.tokens("apparently-valid"))
                    credentials, read_tokens, refresh_requests = await self.protocol_credentials(status, 99991677)
                    if endpoint == "profile":
                        result = await credentials.profile(self.alice.identity, self.alice.add_revision)
                        self.assertEqual(result, UserProfile("ou_alice", "Alice", ""))
                    else:
                        result = await credentials.selector_config(
                            self.alice.identity, self.alice.add_revision, "http://localhost:8787/",
                        )
                        self.assertEqual((result["openId"], result["url"]), ("ou_alice", "http://localhost:8787/"))
                    self.assertEqual(read_tokens, ["private-access-apparently-valid", "private-access-renewed"])
                    request, = refresh_requests
                    self.assertEqual(json.loads(request.content), {
                        "client_id": "cli_test", "client_secret": "private-app-secret",
                        "grant_type": "refresh_token", "refresh_token": "private-refresh-apparently-valid",
                    })
                    stored, = json.loads(self.path.read_text())["users"]
                    self.assertEqual((stored["accessToken"], stored["refreshToken"]),
                                     ("private-access-renewed", "private-refresh-renewed"))
                    self.assertFalse(stored["refreshAttempted"])

    async def test_concurrent_upstream_expiry_reuses_one_rotated_credential(self) -> None:
        await self.save(self.tokens("apparently-valid"))
        both_rejected = asyncio.Event()
        rejected = 0

        async def profile(token):
            nonlocal rejected
            if token == "private-access-apparently-valid":
                rejected += 1
                if rejected == 2:
                    both_rejected.set()
                await both_rejected.wait()
                raise AuthExpired("token_expired", "Expired")
            return UserProfile("ou_alice", "Alice", "")

        self.client.user_info.side_effect = profile
        self.client.refresh.return_value = self.tokens("renewed")
        requests = [self.credentials.profile(self.alice.identity, self.alice.add_revision)
                    for _ in range(2)]
        results = await asyncio.wait_for(asyncio.gather(*requests), 3)
        self.assertEqual([p.name for p in results], ["Alice", "Alice"])
        self.client.refresh.assert_awaited_once_with("private-refresh-apparently-valid")
        self.assertEqual(self.client.user_info.await_count, 4)

    async def test_profile_still_expired_after_refresh_does_not_loop(self) -> None:
        await self.save(self.tokens("apparently-valid"))
        self.client.user_info.side_effect = AuthExpired("token_expired", "Expired")
        self.client.refresh.return_value = self.tokens("renewed")
        with self.assertRaises(AuthExpired):
            await self.credentials.profile(self.alice.identity, self.alice.add_revision)
        self.client.refresh.assert_awaited_once()
        self.assertEqual(self.client.user_info.await_count, 2)

    async def test_invalid_api_token_and_ordinary_error_never_trigger_refresh(self) -> None:
        for endpoint in ("profile", "selector_config"):
            for status, code, auth_error in ((200, 20005, True), (403, 99991672, False)):
                with self.subTest(endpoint=endpoint, status=status, code=code):
                    await self.save(self.tokens("valid"))
                    before = self.path.read_bytes()
                    credentials, read_tokens, refresh_requests = await self.protocol_credentials(status, code)
                    with self.assertRaises(FeishuError) as caught:
                        if endpoint == "profile":
                            await credentials.profile(self.alice.identity, self.alice.add_revision)
                        else:
                            await credentials.selector_config(
                                self.alice.identity, self.alice.add_revision, "http://localhost:8787/",
                            )
                    self.assertEqual(isinstance(caught.exception, AuthExpired), auth_error)
                    self.assertNotEqual(caught.exception.code, "token_expired")
                    self.assertNotIn("private", str(caught.exception))
                    self.assertEqual(read_tokens, ["private-access-valid"])
                    self.assertEqual(refresh_requests, [])
                    self.assertEqual(self.path.read_bytes(), before)

    async def test_new_login_restores_refresh_after_failed_rotation(self) -> None:
        await self.save(self.tokens("old", expired=True))
        self.client.refresh.side_effect = TimeoutError("secret")
        with self.assertRaises(AuthExpired):
            await self.access()
        await self.save(self.tokens("new-login", expired=True))
        self.client.refresh.side_effect = None
        self.client.refresh.return_value = self.tokens("renewed")
        self.assertEqual(await self.access(), "private-access-renewed")
        self.assertEqual([call.args[0] for call in self.client.refresh.await_args_list],
                         ["private-refresh-old", "private-refresh-new-login"])

    async def test_invalid_identity_token_is_not_assumed_expired(self) -> None:
        await self.save(self.tokens("valid"))
        self.client.user_info.side_effect = AuthExpired("token_invalid", "Invalid")
        with self.assertRaises(AuthExpired):
            await self.credentials.profile(self.alice.identity, self.alice.add_revision)
        self.client.refresh.assert_not_awaited()

    async def test_late_profile_after_delete_readd_cannot_update_replacement(self) -> None:
        await self.save(self.tokens("old"))
        entered, release = asyncio.Event(), asyncio.Event()

        async def profile(_token):
            entered.set()
            await release.wait()
            return UserProfile("ou_alice", "Stale profile", "")

        self.client.user_info.side_effect = profile
        request = asyncio.create_task(self.credentials.profile(self.alice.identity, self.alice.add_revision))
        await asyncio.wait_for(entered.wait(), 1)
        self.remove_alice()
        replacement = self.add_alice_again()
        release.set()
        with self.assertRaises(AuthExpired):
            await request
        self.assertEqual(self.users.get_membership(replacement.identity).name, "")

    async def test_token_representations_do_not_expose_secrets(self) -> None:
        tokens = self.tokens("repr")
        await self.save(tokens)
        for value in (repr(tokens), repr(self.credentials)):
            self.assertNotIn(tokens.access_token, value)
            self.assertNotIn(tokens.refresh_token, value)
