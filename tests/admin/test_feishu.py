"""Network-free protocol and secret-boundary checks for Admin Feishu login."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs

import httpx
from lark_oapi.api.authen.v1 import GetUserInfoResponse

from netizen_cli.admin import feishu


def device_response(**changes):
    return {
        "device_code": "synthetic-device-code", "user_code": "TEST-CODE",
        "verification_uri_complete": "https://accounts.feishu.cn/device?user_code=TEST-CODE",
        "expires_in": 600, "interval": 5, **changes,
    }


def token_response(**changes):
    return {
        "access_token": "synthetic-access-token", "refresh_token": "synthetic-refresh-token",
        "expires_in": 7200, "refresh_token_expires_in": 86400,
        "token_type": "Bearer", "scope": "component:selector offline_access", **changes,
    }


class FeishuClientTest(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, handler=None, *, sdk_result=None, timeout=20):
        self.requests = []

        async def handle(request):
            self.requests.append(request)
            if handler is None:
                raise AssertionError("unexpected HTTP request")
            result = handler(request)
            return await result if hasattr(result, "__await__") else result

        http = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=True,
        ))
        self.sdk_get = AsyncMock(return_value=sdk_result)
        sdk = SimpleNamespace(authen=SimpleNamespace(v1=SimpleNamespace(
            user_info=SimpleNamespace(aget=self.sdk_get),
        )))
        client = feishu.FeishuClient("cli_test_app", "synthetic-app-secret",
                                    http_client=http, sdk_client=sdk, timeout=timeout)
        self.addAsyncCleanup(client.aclose)
        return client, http

    async def test_device_start_requests_only_required_scopes_with_basic_auth(self):
        client, _ = await self.make_client(lambda _: httpx.Response(200, json=device_response()))
        with patch.object(feishu.time, "time", return_value=1000):
            challenge = await client.start_login()
        request, = self.requests
        self.assertEqual(str(request.url), "https://accounts.feishu.cn/oauth/v1/device_authorization")
        self.assertEqual(request.method, "POST")
        self.assertEqual(parse_qs(request.content.decode()), {
            "client_id": ["cli_test_app"], "scope": ["component:selector offline_access"],
        })
        expected = base64.b64encode(b"cli_test_app:synthetic-app-secret").decode()
        self.assertEqual(request.headers["Authorization"], "Basic " + expected)
        self.assertEqual(request.headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(challenge.expires_at, 1600)
        self.assertEqual(challenge.interval, 5)
        self.assertEqual(challenge.user_code, "TEST-CODE")
        self.assertNotIn("synthetic-device", repr(challenge))
        self.assertNotIn("TEST-CODE", repr(challenge))

    async def test_challenge_rejects_untrusted_urls_and_invalid_values(self):
        for changes in (
            {"verification_uri_complete": "https://attacker.invalid/"},
            {"verification_uri_complete": "http://accounts.feishu.cn/"},
            {"verification_uri_complete": "https://user@accounts.feishu.cn/"},
            {"verification_uri_complete": "https://accounts.feishu.cn:8443/"},
            {"verification_uri_complete": "https://accounts.feishu.cn:bad/"},
            {"device_code": ""}, {"user_code": ""}, {"expires_in": True},
            {"expires_in": 1801}, {"interval": 0}, {"interval": 61},
        ):
            with self.subTest(changes=changes):
                client, _ = await self.make_client(lambda _: httpx.Response(200, json=device_response(**changes)))
                with self.assertRaises(feishu.FeishuError) as caught:
                    await client.start_login()
                self.assertEqual(caught.exception.code, "upstream_format")

    async def test_challenge_accepts_plain_verification_uri_and_default_interval(self):
        data = device_response(verification_uri_complete=None,
                               verification_uri="https://accounts.feishu.cn/device")
        del data["interval"]
        client, _ = await self.make_client(lambda _: httpx.Response(200, json=data))
        challenge = await client.start_login()
        self.assertEqual(challenge.verification_uri_complete, "https://accounts.feishu.cn/device")
        self.assertEqual(challenge.interval, 5)

    async def test_poll_pending_slow_down_and_token_result(self):
        responses = iter([
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json=token_response()),
        ])
        client, _ = await self.make_client(lambda _: next(responses))
        self.assertIsNone(await client.poll_login("synthetic-device-code"))
        with self.assertRaises(feishu.SlowDownError):
            await client.poll_login("synthetic-device-code")
        with patch.object(feishu.time, "time", return_value=1000):
            tokens = await client.poll_login("synthetic-device-code")
        self.assertEqual(tokens.expires_at, 8200)
        self.assertEqual(tokens.refresh_expires_at, 87400)
        self.assertEqual(tokens.scopes, ("component:selector", "offline_access"))
        self.assertNotIn("synthetic", repr(tokens))
        for request in self.requests:
            self.assertEqual(str(request.url), "https://accounts.feishu.cn/oauth/v3/token")
            self.assertEqual(parse_qs(request.content.decode()), {
                "client_id": ["cli_test_app"], "client_secret": ["synthetic-app-secret"],
                "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
                "device_code": ["synthetic-device-code"],
            })

    async def test_tokens_require_real_expiries_and_bearer_but_allow_absent_refresh(self):
        for changes in (
            {"expires_in": None}, {"expires_in": True}, {"expires_in": 0},
            {"refresh_token_expires_in": None}, {"access_token": ""},
            {"token_type": "DPoP"}, {"scope": ["offline_access"]},
        ):
            with self.subTest(changes=changes):
                client, _ = await self.make_client(lambda _: httpx.Response(200, json=token_response(**changes)))
                with self.assertRaises(feishu.FeishuError):
                    await client.poll_login("synthetic-device-code")
        data = token_response()
        del data["refresh_token"]
        del data["refresh_token_expires_in"]
        del data["scope"]
        client, _ = await self.make_client(lambda _: httpx.Response(200, json=data))
        tokens = await client.poll_login("synthetic-device-code")
        self.assertIsNone(tokens.refresh_token)
        self.assertIsNone(tokens.refresh_expires_at)
        self.assertEqual(tokens.scopes, ())

    async def test_refresh_uses_json_and_returns_rotated_credentials(self):
        client, _ = await self.make_client(lambda _: httpx.Response(200, json=token_response(
            access_token="new-access", refresh_token="new-refresh",
        )))
        tokens = await client.refresh("previous-refresh")
        request, = self.requests
        self.assertEqual(str(request.url), "https://accounts.feishu.cn/oauth/v3/token")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(request.content), {
            "client_id": "cli_test_app", "client_secret": "synthetic-app-secret",
            "grant_type": "refresh_token", "refresh_token": "previous-refresh",
        })
        self.assertEqual((tokens.access_token, tokens.refresh_token), ("new-access", "new-refresh"))

    async def test_refresh_response_loss_does_not_retry_or_leak_exception(self):
        def handle(request):
            raise httpx.ReadError("contains synthetic-refresh-token", request=request)

        client, _ = await self.make_client(handle)
        with self.assertRaises(feishu.FeishuError) as caught:
            await client.refresh("synthetic-refresh-token")
        self.assertEqual(caught.exception.code, "upstream_unavailable")
        self.assertNotIsInstance(caught.exception, feishu.AuthExpired)
        self.assertNotIn("synthetic", str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_no_redirect_even_with_injected_client_configured_to_follow(self):
        client, _ = await self.make_client(lambda _: httpx.Response(
            307, headers={"Location": "https://attacker.invalid/"}, json={"error": "redirect"},
        ))
        with self.assertRaises(feishu.FeishuError):
            await client.refresh("synthetic-refresh-token")
        self.assertEqual(len(self.requests), 1)

    async def test_safe_auth_errors_do_not_echo_upstream_payload(self):
        for error in ("access_denied", "invalid_grant", "invalid_token", "expired_token"):
            with self.subTest(error=error):
                client, _ = await self.make_client(lambda _: httpx.Response(400, json={
                    "error": error, "error_description": "synthetic-private-token",
                }))
                with self.assertRaises(feishu.AuthExpired) as caught:
                    await client.poll_login("synthetic-device-code")
                self.assertNotIn("synthetic", str(caught.exception))
        client, _ = await self.make_client(lambda _: httpx.Response(403, json={
            "code": 99991672, "msg": "synthetic-private-token",
        }))
        with self.assertRaises(feishu.FeishuError) as caught:
            await client.refresh("synthetic-refresh-token")
        self.assertNotIsInstance(caught.exception, feishu.AuthExpired)

    async def test_invalid_upstream_payload_is_redacted(self):
        for response in (
            httpx.Response(500, text="synthetic-private-token"),
            httpx.Response(200, json=["synthetic-private-token"]),
            httpx.Response(200, content=b"x" * 65537),
        ):
            with self.subTest(response=response.status_code):
                client, _ = await self.make_client(lambda _: response)
                with self.assertRaises(feishu.FeishuError) as caught:
                    await client.start_login()
                self.assertEqual(caught.exception.code, "upstream_format")
                self.assertNotIn("synthetic", str(caught.exception))

    async def test_request_has_total_timeout_in_addition_to_transport_timeout(self):
        async def handle(_):
            await asyncio.Event().wait()

        client, _ = await self.make_client(handle, timeout=0.01)
        with self.assertRaises(feishu.FeishuError) as caught:
            await client.start_login()
        self.assertEqual(caught.exception.code, "upstream_unavailable")
        self.assertEqual(self.requests[0].extensions["timeout"]["read"], 0.01)

    async def test_user_info_uses_public_typed_sdk_and_does_not_require_tenant(self):
        result = GetUserInfoResponse({"code": 0, "data": {
            "open_id": "ou_test_user", "name": "测试用户", "avatar_url": "https://example.invalid/avatar",
        }})
        client, _ = await self.make_client(sdk_result=result)
        profile = await client.user_info("synthetic-access-token")
        request, option = self.sdk_get.call_args.args
        self.assertEqual(request.uri, "/open-apis/authen/v1/user_info")
        self.assertEqual(option.user_access_token, "synthetic-access-token")
        self.assertEqual(profile, feishu.UserProfile("ou_test_user", "测试用户", "https://example.invalid/avatar"))
        self.assertEqual(self.requests, [])

    async def test_user_info_invalid_token_is_not_classified_as_merely_expired(self):
        client, _ = await self.make_client(sdk_result=GetUserInfoResponse({
            "code": 20005, "msg": "synthetic-private-token",
        }))
        with self.assertRaises(feishu.AuthExpired) as caught:
            await client.user_info("synthetic-access-token")
        self.assertEqual(caught.exception.code, "token_invalid")
        self.assertNotIn("synthetic", str(caught.exception))

    async def test_explicit_api_token_expiry_precedes_http_status_for_profile_and_ticket(self):
        for endpoint in ("profile", "ticket"):
            for status in (200, 400, 401):
                with self.subTest(endpoint=endpoint, status=status):
                    payload = {"code": 99991677, "msg": "synthetic-private-token"}
                    result = GetUserInfoResponse(payload)
                    result.raw = SimpleNamespace(status_code=status)
                    client, _ = await self.make_client(
                        lambda _: httpx.Response(status, json=payload), sdk_result=result,
                    )
                    with self.assertRaises(feishu.AuthExpired) as caught:
                        if endpoint == "profile":
                            await client.user_info("synthetic-access-token")
                        else:
                            await client.selector_config("synthetic-access-token", "ou_test_user",
                                                         "http://localhost:8787/")
                    self.assertEqual(caught.exception.code, "token_expired")
                    self.assertNotIn("synthetic", str(caught.exception))

    async def test_identity_401_revokes_but_ordinary_403_does_not(self):
        for status in (401, 403):
            with self.subTest(status=status):
                result = GetUserInfoResponse({"code": 999, "msg": "synthetic-private-token"})
                result.raw = SimpleNamespace(status_code=status)
                client, _ = await self.make_client(sdk_result=result)
                with self.assertRaises(feishu.FeishuError) as caught:
                    await client.user_info("synthetic-access-token")
                self.assertEqual(isinstance(caught.exception, feishu.AuthExpired), status == 401)

    async def test_selector_scope_and_ordinary_403_errors_are_not_assumed_to_be_expiry(self):
        for failure in ({"code": 99991672}, {"error": "invalid_scope"}):
            with self.subTest(failure=failure):
                client, _ = await self.make_client(lambda _: httpx.Response(403, json={
                    **failure, "msg": "synthetic-private-token",
                }))
                with self.assertRaises(feishu.FeishuError) as caught:
                    await client.selector_config("synthetic-access-token", "ou_test_user",
                                                 "http://localhost:8787/")
                self.assertNotIsInstance(caught.exception, feishu.AuthExpired)
                self.assertNotIn("synthetic", str(caught.exception))

    async def test_user_info_bad_identity_and_decoder_exception_are_redacted(self):
        client, _ = await self.make_client(sdk_result=GetUserInfoResponse({
            "code": 0, "data": {"open_id": "on_wrong_namespace"},
        }))
        with self.assertRaises(feishu.FeishuError):
            await client.user_info("synthetic-access-token")
        self.sdk_get.side_effect = ValueError("synthetic-private-token")
        with self.assertRaises(feishu.FeishuError) as caught:
            await client.user_info("synthetic-access-token")
        self.assertNotIn("synthetic", str(caught.exception))

    async def test_selector_signature_uses_milliseconds_and_queryless_page_url(self):
        client, _ = await self.make_client(lambda _: httpx.Response(200, json={
            "code": 0, "data": {"ticket": "synthetic-private-ticket", "expire_in": 7200},
        }))
        with patch.object(feishu.time, "time", return_value=1000.5), \
                patch.object(feishu.secrets, "token_hex", return_value="synthetic-nonce"):
            config = await client.selector_config("synthetic-access-token", "ou_test_user",
                                                  "http://localhost:8787/?tab=admin#selected")
        request, = self.requests
        self.assertEqual(str(request.url), "https://open.feishu.cn/open-apis/jssdk/ticket/get")
        self.assertEqual(request.headers["Authorization"], "Bearer synthetic-access-token")
        payload = ("jsapi_ticket=synthetic-private-ticket&noncestr=synthetic-nonce"
                   "&timestamp=1000500&url=http://localhost:8787/")
        self.assertEqual(config["signature"], hashlib.sha1(payload.encode()).hexdigest())
        self.assertEqual(config["timestamp"], 1000500)
        self.assertEqual(config["expiresAt"], 1600.5)
        self.assertEqual(config["jsApiList"], ["selector"])
        self.assertEqual(config["openId"], "ou_test_user")
        self.assertNotIn("synthetic-access-token", repr(config))
        self.assertNotIn("synthetic-private-ticket", repr(config))

    async def test_selector_rejects_invalid_page_before_requesting_ticket(self):
        client, _ = await self.make_client()
        for url in ("javascript:alert(1)", "http://user:password@localhost/", "http://localhost:invalid/"):
            with self.subTest(url=url), self.assertRaises(feishu.FeishuError):
                await client.selector_config("synthetic-access-token", "ou_test_user", url)
        self.assertEqual(self.requests, [])

    async def test_close_does_not_close_borrowed_http_client(self):
        client, http = await self.make_client()
        await client.aclose()
        self.assertFalse(http.is_closed)
        with self.assertRaises(feishu.FeishuError) as caught:
            await client.start_login()
        self.assertEqual(caught.exception.code, "client_closed")


class AdminBindingTest(unittest.TestCase):
    def test_binding_waits_respects_slow_down_returns_only_profile_and_closes(self):
        client = Mock()
        client.start_login = AsyncMock(return_value=feishu.DeviceChallenge(
            "synthetic-device", "TEST-CODE", "https://accounts.feishu.cn/device", 1600, 5,
        ))
        client.poll_login = AsyncMock(side_effect=[None, feishu.SlowDownError(), feishu.UserTokens(
            "synthetic-access", "synthetic-refresh", 9000, 9999, (),
        )])
        profile = feishu.UserProfile("ou_test_user", "测试", "")
        client.user_info = AsyncMock(return_value=profile)
        client.aclose = AsyncMock()
        callback, sleep, stderr = Mock(), AsyncMock(), io.StringIO()
        with patch.object(feishu.time, "time", return_value=1000), \
                patch.object(feishu.asyncio, "sleep", sleep), patch.object(feishu.sys, "stderr", stderr):
            result = feishu.run_admin_binding("cli_test", "synthetic-secret", callback,
                                              client_factory=lambda *_: client)
        self.assertEqual(result, profile)
        callback.assert_called_once_with("https://accounts.feishu.cn/device")
        self.assertIn("TEST-CODE", stderr.getvalue())
        self.assertNotIn("synthetic", stderr.getvalue())
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 5, 10])
        client.user_info.assert_awaited_once_with("synthetic-access")
        client.aclose.assert_awaited_once()

    def test_binding_expires_without_polling_and_closes(self):
        client = Mock()
        client.start_login = AsyncMock(return_value=feishu.DeviceChallenge(
            "synthetic-device", "TEST-CODE", "https://accounts.feishu.cn/device", 500, 5,
        ))
        client.poll_login, client.aclose = AsyncMock(), AsyncMock()
        with patch.object(feishu.time, "time", return_value=1000):
            with self.assertRaises(feishu.AuthExpired):
                feishu.run_admin_binding("cli_test", "synthetic-secret", Mock(),
                                         client_factory=lambda *_: client)
        client.poll_login.assert_not_awaited()
        client.aclose.assert_awaited_once()

    def test_binding_denial_closes_without_issuing_a_second_challenge(self):
        client = Mock()
        client.start_login = AsyncMock(return_value=feishu.DeviceChallenge(
            "synthetic-device", "TEST-CODE", "https://accounts.feishu.cn/device", 1600, 5,
        ))
        client.poll_login = AsyncMock(side_effect=feishu.AuthExpired("access_denied", "已拒绝"))
        client.aclose = AsyncMock()
        with patch.object(feishu.time, "time", return_value=1000), \
                patch.object(feishu.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(feishu.AuthExpired):
                feishu.run_admin_binding("cli_test", "synthetic-secret", Mock(),
                                         client_factory=lambda *_: client)
        client.start_login.assert_awaited_once()
        client.aclose.assert_awaited_once()
