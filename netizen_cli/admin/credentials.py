"""Private, user-scoped Feishu credentials; never browser authentication."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .feishu import AuthExpired, FeishuClient, UserProfile, UserTokens
from .users import (
    AdminIdentity, AdminUsers, AdminUsersUnavailable, PrivateJSONError,
    delete_private_json, read_private_json, write_private_json,
)


@dataclass(frozen=True)
class _Credential:
    add_revision: int
    tokens: UserTokens = field(repr=False)
    refresh_attempted: bool = False


def credential_path(root: Path, identity: AdminIdentity) -> Path:
    """Stable, bounded filename for the exact application/user identity."""
    digest = hashlib.sha256(f"{identity.app_id}\n{identity.open_id}".encode("utf-8")).hexdigest()
    return root / "credentials" / f"admin-user-{digest}.json"


def _token(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


class AdminCredentials:
    """On-demand private credentials, with single-flight per-user renewal.

    All asynchronous calls belong to the Admin event loop. The roster's lock
    protects membership checks and durable commits; no network call holds it.
    Tokens live only in the operation using them, never in a resident cache.
    """

    def __init__(self, root: Path, users: AdminUsers, client: FeishuClient,
                 *, clock: Callable[[], float] = time.time) -> None:
        self.users = users
        self.client = client
        self._clock = clock
        self._root = root
        self._locks: dict[AdminIdentity, tuple[asyncio.Lock, int]] = {}
        self._closed = False
        users.subscribe_revocation(self._revoke)

    def _read(self, identity: AdminIdentity) -> _Credential | None:
        data = read_private_json(credential_path(self._root, identity), missing_ok=True, maximum=None)
        if data is None:
            return None
        try:
            if (set(data) != {
                "version", "appId", "openId", "addRevision", "accessToken", "refreshToken",
                "expiresAt", "refreshExpiresAt", "scopes", "refreshAttempted",
            } or type(data["version"]) is not int or data["version"] != 1
                    or AdminIdentity(data["appId"], data["openId"]) != identity):
                raise ValueError
            revision = data["addRevision"]
            if type(revision) is not int or revision < 1 or type(data["refreshAttempted"]) is not bool:
                raise ValueError
            refresh = data["refreshToken"]
            if not _token(data["accessToken"]) or (refresh is not None and not _token(refresh)):
                raise ValueError
            for key in ("expiresAt", "refreshExpiresAt"):
                value = data[key]
                if value is None and key == "refreshExpiresAt":
                    continue
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError
            scopes = data["scopes"]
            if not isinstance(scopes, list) or any(not isinstance(s, str) or not s for s in scopes):
                raise ValueError
            return _Credential(revision, UserTokens(
                access_token=data["accessToken"], refresh_token=refresh,
                expires_at=data["expiresAt"], refresh_expires_at=data["refreshExpiresAt"],
                scopes=tuple(scopes),
            ), data["refreshAttempted"])
        except (ValueError, TypeError, KeyError, OverflowError):
            raise PrivateJSONError() from None

    def _write(self, identity: AdminIdentity, record: _Credential) -> None:
        write_private_json(credential_path(self._root, identity), {
            "version": 1, "appId": identity.app_id, "openId": identity.open_id,
            "addRevision": record.add_revision,
            "accessToken": record.tokens.access_token,
            "refreshToken": record.tokens.refresh_token,
            "expiresAt": record.tokens.expires_at,
            "refreshExpiresAt": record.tokens.refresh_expires_at,
            "scopes": list(record.tokens.scopes),
            "refreshAttempted": record.refresh_attempted,
        }, maximum=None)

    def _member(self, identity: AdminIdentity, revision: int) -> None:
        member = self.users.get_membership(identity)
        if self._closed or member is None or member.add_revision != revision:
            raise AuthExpired("auth_expired", "请重新登录。")

    @asynccontextmanager
    async def _single_user(self, identity: AdminIdentity):
        lock, count = self._locks.get(identity, (asyncio.Lock(), 0))
        self._locks[identity] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            _, count = self._locks[identity]
            if count == 1:
                del self._locks[identity]
            else:
                self._locks[identity] = (lock, count - 1)

    async def save_login(self, identity: AdminIdentity, tokens: UserTokens, *,
                         expected_add_revision: int,
                         is_current: Callable[[], bool] = lambda: True) -> None:
        async with self._single_user(identity):
            with self.users.lock:
                self._member(identity, expected_add_revision)
                if not is_current():
                    raise AuthExpired("auth_expired", "本次登录已失效。")
                # There is no older cached pair to invalidate on an uncertain
                # write: the next operation re-reads the actual private file.
                self._write(identity, _Credential(expected_add_revision, tokens))

    async def access_token(self, identity: AdminIdentity, add_revision: int, *, force_refresh: bool = False,
                           rejected_token: str | None = None) -> str:
        async with self._single_user(identity):
            with self.users.lock:
                self._member(identity, add_revision)
                try:
                    record = self._read(identity)
                except PrivateJSONError:
                    raise AuthExpired("auth_expired", "飞书凭证不可用，请重新登录。") from None
                if record is None or record.add_revision != add_revision:
                    raise AuthExpired("auth_expired", "请重新登录。")
                tokens = record.tokens
                if tokens.expires_at > self._clock() and (
                    not force_refresh or (rejected_token is not None and tokens.access_token != rejected_token)
                ):
                    return tokens.access_token
                if record.refresh_attempted or not tokens.refresh_token or (
                    tokens.refresh_expires_at is not None and tokens.refresh_expires_at <= self._clock()
                ):
                    raise AuthExpired("auth_expired", "飞书凭证已过期，请重新登录。")
            try:
                with self.users.lock:
                    self._member(identity, add_revision)
                    record = _Credential(add_revision, tokens, True)
                    # Persist before sending: timeout/cancellation/restart may
                    # otherwise consume the same single-use refresh token twice.
                    self._write(identity, record)
                renewed = await self.client.refresh(tokens.refresh_token)
                with self.users.lock:
                    self._member(identity, add_revision)
                    if self._read(identity) != record:
                        raise AuthExpired("auth_expired", "飞书凭证已变化，请重新登录。")
                    self._write(identity, _Credential(add_revision, renewed))
                    return renewed.access_token
            except asyncio.CancelledError:
                raise
            except Exception:
                # Deliberately one expiry path; do not delete the shared
                # credential or coordinate other browsers on refresh failure.
                raise AuthExpired("auth_expired", "飞书凭证已过期，请重新登录。") from None

    async def profile(self, identity: AdminIdentity, add_revision: int) -> UserProfile:
        token = await self.access_token(identity, add_revision)
        try:
            profile = await self.client.user_info(token)
        except AuthExpired as error:
            if error.code != "token_expired":
                raise
            token = await self.access_token(identity, add_revision, force_refresh=True, rejected_token=token)
            profile = await self.client.user_info(token)
        with self.users.lock:
            self._member(identity, add_revision)
            if profile.open_id != identity.open_id:
                raise AuthExpired("auth_expired", "飞书身份不匹配，请重新登录。")
            self.users.update_profile(identity, name=profile.name, avatar_url=profile.avatar_url,
                                      expected_add_revision=add_revision)
        return profile

    async def selector_config(self, identity: AdminIdentity, add_revision: int, url: str) -> dict:
        token = await self.access_token(identity, add_revision)
        try:
            result = await self.client.selector_config(token, identity.open_id, url)
        except AuthExpired as error:
            if error.code != "token_expired":
                raise
            token = await self.access_token(identity, add_revision, force_refresh=True, rejected_token=token)
            result = await self.client.selector_config(token, identity.open_id, url)
        with self.users.lock:
            self._member(identity, add_revision)
        return result

    def _revoke(self, identity: AdminIdentity) -> None:
        # Invoked under the roster lock after removal. Membership is already
        # revoked even if cleanup fails; re-addition gets a new incarnation.
        try:
            self.users.get_membership(identity)
        except AdminUsersUnavailable:
            # A roster IO failure closes authorization, but is not an explicit
            # deletion of every user's durable credential. Preserve disk state.
            return
        delete_private_json(credential_path(self._root, identity))

    def close(self) -> None:
        self._closed = True
