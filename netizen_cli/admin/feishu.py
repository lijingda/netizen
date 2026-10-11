"""Narrow Feishu Admin OAuth and selector boundary (ADR 0080).

The pinned SDK owns typed user-info calls. Its Device Flow endpoints do not
support this app, so the fixed accounts protocol and public selector-ticket
endpoint are the documented exceptions. No CLI runtime or SDK patch is used.
All expiry values crossing this boundary are Unix seconds; callers own token
storage, refresh serialization, browser sessions, and authorization decisions.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import math
import re
import secrets
import sys
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..feishu_app_onboarding import REQUIRED_USER_SCOPES


_DEVICE_URL = "https://accounts.feishu.cn/oauth/v1/device_authorization"
_TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
_TICKET_URL = "https://open.feishu.cn/open-apis/jssdk/ticket/get"
_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_-]{1,256}\Z")
_MAX_RESPONSE_BYTES = 65_536


class FeishuError(RuntimeError):
    """Only safe local codes/messages, never an upstream body or exception."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class SlowDownError(FeishuError):
    def __init__(self):
        super().__init__("slow_down", "飞书要求降低登录查询频率。")


class AuthExpired(FeishuError):
    """Authentication is rejected; only code token_expired permits refresh."""


@dataclass(frozen=True, slots=True)
class DeviceChallenge:
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    verification_uri_complete: str = field(repr=False)
    expires_at: float
    interval: int


@dataclass(frozen=True, slots=True)
class UserTokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: float
    refresh_expires_at: float | None
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UserProfile:
    open_id: str
    name: str
    avatar_url: str


def _invalid_response() -> FeishuError:
    return FeishuError("upstream_format", "飞书返回的数据不完整或格式不正确。")


def _string(value: object, *, maximum: int = 8192) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise _invalid_response()
    return value


def _seconds(value: object) -> int:
    if type(value) is not int or not 0 < value <= 2**31 - 1:
        raise _invalid_response()
    return value


def _raise_rejection(status: int, payload: dict[str, Any], *, identity: bool = False) -> None:
    error = payload.get("error")
    code = payload.get("code")
    # The public API's explicit token-expired code takes precedence over HTTP
    # 401, which alone only proves invalid authentication and cannot refresh.
    if error == "expired_token" or (identity and type(code) is int and code == 99991677):
        raise AuthExpired("token_expired", "飞书凭证已过期。")
    if error in ("access_denied", "invalid_grant", "invalid_token"):
        raise AuthExpired(str(error), "飞书身份授权已失效，请重新登录。")
    # user_info 20005 means invalid, not necessarily expired. Never infer that
    # arbitrary 403 responses or missing business scopes revoke the account.
    if identity and (status == 401 or code == 20005):
        raise AuthExpired("token_invalid", "飞书身份凭证无效，请重新登录。")
    if error in ("invalid_client", "invalid_scope", "unauthorized_client"):
        raise FeishuError(str(error), "飞书拒绝了当前应用或权限配置。")
    safe_code = f"upstream_{code}" if type(code) is int else f"upstream_http_{status}"
    raise FeishuError(safe_code, "飞书请求未成功，请稍后重试或检查应用配置。")


class FeishuClient:
    def __init__(
        self, app_id: str, app_secret: str, *, http_client: httpx.AsyncClient | None = None,
        sdk_client: Any = None, timeout: float = 20,
    ):
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Feishu request timeout must be between 0 and 60 seconds")
        self.app_id = _string(app_id, maximum=256)
        self._app_secret = _string(app_secret)
        self._timeout = timeout
        self._http = http_client
        self._owns_http = http_client is None
        self._sdk = sdk_client
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True
        if self._http is not None and self._owns_http:
            await self._http.aclose()

    def _require_open(self) -> None:
        if self._closed:
            raise FeishuError("client_closed", "飞书连接已关闭。")

    async def _request(self, url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self._require_open()
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._http.post(
                    url, timeout=self._timeout, follow_redirects=False, **kwargs,
                )
            if len(response.content) > _MAX_RESPONSE_BYTES:
                raise _invalid_response()
            payload = response.json()
        except (httpx.HTTPError, TimeoutError):
            raise FeishuError("upstream_unavailable", "暂时无法连接飞书，请稍后重试。") from None
        except (ValueError, UnicodeError):
            raise _invalid_response() from None
        if not isinstance(payload, dict):
            raise _invalid_response()
        return response.status_code, payload

    @staticmethod
    def _require_success(status: int, data: dict[str, Any], *, api: bool = False) -> None:
        code = data.get("code", None if api else 0)
        if status != 200 or data.get("error") or type(code) is not int or code != 0:
            _raise_rejection(status, data, identity=api)

    async def start_login(self) -> DeviceChallenge:
        status, data = await self._request(
            _DEVICE_URL,
            data={"client_id": self.app_id, "scope": " ".join(REQUIRED_USER_SCOPES)},
            auth=(self.app_id, self._app_secret),
        )
        self._require_success(status, data)
        uri = _string(data.get("verification_uri_complete") or data.get("verification_uri"))
        try:
            parsed = urlsplit(uri)
            trusted = (parsed.scheme == "https" and parsed.hostname == "accounts.feishu.cn"
                       and parsed.port in (None, 443) and not parsed.username and not parsed.password)
        except ValueError:
            trusted = False
        lifetime, interval = _seconds(data.get("expires_in")), _seconds(data.get("interval", 5))
        if not trusted or lifetime > 1800 or interval > 60:
            raise _invalid_response()
        return DeviceChallenge(
            _string(data.get("device_code")), _string(data.get("user_code"), maximum=256),
            uri, time.time() + lifetime, interval,
        )

    async def poll_login(self, device_code: str) -> UserTokens | None:
        status, data = await self._request(_TOKEN_URL, data={
            "client_id": self.app_id, "client_secret": self._app_secret,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": _string(device_code),
        })
        if status in (200, 400) and data.get("error") == "authorization_pending":
            return None
        if status in (200, 400) and data.get("error") == "slow_down":
            raise SlowDownError()
        self._require_success(status, data)
        return self._tokens(data)

    @staticmethod
    def _tokens(data: dict[str, Any]) -> UserTokens:
        if _string(data.get("token_type")).lower() != "bearer":
            raise _invalid_response()
        access_token = _string(data.get("access_token"))
        lifetime = _seconds(data.get("expires_in"))
        refresh_token = data.get("refresh_token")
        refresh_lifetime = None
        if refresh_token is not None:
            refresh_token = _string(refresh_token)
            refresh_lifetime = _seconds(data.get("refresh_token_expires_in"))
        scope = data.get("scope", "")
        if not isinstance(scope, str) or len(scope) > 8192:
            raise _invalid_response()
        now = time.time()
        return UserTokens(
            access_token, refresh_token, now + lifetime,
            now + refresh_lifetime if refresh_lifetime is not None else None,
            tuple(dict.fromkeys(scope.split())),
        )

    async def user_info(self, access_token: str) -> UserProfile:
        self._require_open()
        from lark_oapi import Client, RequestOption
        from lark_oapi.api.authen.v1 import GetUserInfoRequest

        if self._sdk is None:
            self._sdk = (Client.builder().app_id(self.app_id).app_secret(self._app_secret)
                         .enable_set_token(True).timeout(self._timeout).build())
        option = RequestOption.builder().user_access_token(_string(access_token)).build()
        try:
            async with asyncio.timeout(self._timeout):
                result = await self._sdk.authen.v1.user_info.aget(
                    GetUserInfoRequest.builder().build(), option,
                )
        except (httpx.HTTPError, TimeoutError):
            raise FeishuError("upstream_unavailable", "暂时无法连接飞书，请稍后重试。") from None
        except Exception:
            # SDK decoding errors may include the upstream body or token.
            raise _invalid_response() from None
        raw = getattr(result, "raw", None)
        status, code = getattr(raw, "status_code", 200), getattr(result, "code", None)
        if status != 200 or type(code) is not int or code != 0:
            _raise_rejection(status, {"code": code}, identity=True)
        data = getattr(result, "data", None)
        open_id = getattr(data, "open_id", None)
        if not isinstance(open_id, str) or not _OPEN_ID.fullmatch(open_id):
            raise _invalid_response()
        name, avatar = getattr(data, "name", None), getattr(data, "avatar_url", None)
        if name is None:
            name = "飞书用户"
        if avatar is None:
            avatar = ""
        if (not isinstance(name, str) or len(name) > 1024
                or not isinstance(avatar, str) or len(avatar) > 4096
                or any(ord(character) < 32 for character in name + avatar)):
            raise _invalid_response()
        return UserProfile(open_id, name, avatar)

    async def refresh(self, refresh_token: str) -> UserTokens:
        # This modern refresh protocol uses JSON; device-code polling uses form.
        # No retries: a lost response can already have consumed the one-use RT.
        status, data = await self._request(_TOKEN_URL, json={
            "client_id": self.app_id, "client_secret": self._app_secret,
            "grant_type": "refresh_token", "refresh_token": _string(refresh_token),
        })
        self._require_success(status, data)
        return self._tokens(data)

    async def selector_config(self, access_token: str, open_id: str, url: str) -> dict[str, Any]:
        if not isinstance(open_id, str) or not _OPEN_ID.fullmatch(open_id):
            raise FeishuError("invalid_identity", "组件需要当前登录者的准确身份。")
        try:
            parsed = urlsplit(_string(url))
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username or parsed.password):
                raise ValueError
            parsed.port  # Reject malformed ports before making an upstream call.
        except ValueError:
            raise FeishuError("invalid_page_url", "组件页面地址不正确。") from None
        # The ordinary web component signs the URL without query or fragment.
        signed_url = urlunsplit(parsed._replace(query="", fragment=""))
        status, data = await self._request(
            _TICKET_URL, headers={"Authorization": "Bearer " + _string(access_token)},
        )
        self._require_success(status, data, api=True)
        ticket_data = data.get("data")
        if not isinstance(ticket_data, dict):
            raise _invalid_response()
        ticket = _string(ticket_data.get("ticket"))
        lifetime = _seconds(ticket_data.get("expire_in"))
        now, nonce = time.time(), secrets.token_hex(16)
        timestamp = int(now * 1000)
        payload = f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={signed_url}"
        return {
            "appId": self.app_id, "openId": open_id, "url": signed_url,
            "signature": hashlib.sha1(payload.encode()).hexdigest(),
            "timestamp": timestamp, "nonceStr": nonce,
            "jsApiList": ["selector"], "locale": "zh-CN",
            "expiresAt": now + min(600, lifetime),
        }


def run_admin_binding(
    app_id: str, app_secret: str, on_verification: Callable[[str], None], *,
    client_factory: Callable[..., FeishuClient] | None = None,
) -> UserProfile:
    """Bounded trusted-local identity binding; no tokens leave this function."""
    async def bind() -> UserProfile:
        client = (client_factory or FeishuClient)(app_id, app_secret)
        try:
            async with asyncio.timeout(660):
                challenge = await client.start_login()
                on_verification(challenge.verification_uri_complete)
                print(f"Feishu verification code: {challenge.user_code}", file=sys.stderr, flush=True)
                deadline = time.monotonic() + max(0, challenge.expires_at - time.time())
                interval = challenge.interval
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AuthExpired("login_expired", "飞书登录已超时，请重新开始。")
                    await asyncio.sleep(min(interval, remaining))
                    if time.monotonic() >= deadline:
                        raise AuthExpired("login_expired", "飞书登录已超时，请重新开始。")
                    try:
                        tokens = await client.poll_login(challenge.device_code)
                    except SlowDownError:
                        interval += 5
                        continue
                    if tokens is not None:
                        return await client.user_info(tokens.access_token)
        except TimeoutError:
            raise AuthExpired("login_expired", "飞书登录已超时，请重新开始。") from None
        finally:
            await client.aclose()

    return asyncio.run(bind())
