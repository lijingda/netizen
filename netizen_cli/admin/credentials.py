"""Private, user-scoped Feishu credentials; never browser authentication."""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .feishu import AuthExpired, FeishuClient, UserProfile, UserTokens
from .users import (
    AdminIdentity, AdminUsers, AdminUsersUnavailable, PrivateJSONError, read_private_json, write_private_json,
)


@dataclass(frozen=True)
class _Credential:
    add_revision: int
    tokens: UserTokens = field(repr=False)
    refresh_attempted: bool = False


class AdminCredentials:
    """One current credential per member, with single-flight per-user renewal.

    All asynchronous calls belong to the Admin event loop. The roster's lock
    protects membership checks and durable commits; no network call holds it.
    """

    def __init__(self, root: Path, users: AdminUsers, client: FeishuClient,
                 *, clock: Callable[[], float] = time.time) -> None:
        self.users = users
        self.client = client
        self._clock = clock
        self._path = root / "credentials" / "admin-user-tokens.json"
        self._records: dict[AdminIdentity, _Credential] = {}
        self._locks: dict[AdminIdentity, tuple[asyncio.Lock, int]] = {}
        self._closed = False
        self._load()
        users.subscribe_revocation(self._revoke)

    def _load(self) -> None:
        data = read_private_json(self._path, missing_ok=True)
        if data is None:
            return
        try:
            if set(data) != {"version", "appId", "users"} or type(data["version"]) is not int or data["version"] != 1:
                raise ValueError
            if not isinstance(data["appId"], str) or not isinstance(data["users"], list):
                raise ValueError
            seen: set[str] = set()
            for row in data["users"]:
                if not isinstance(row, dict) or set(row) != {
                    "openId", "addRevision", "accessToken", "refreshToken",
                    "expiresAt", "refreshExpiresAt", "scopes", "refreshAttempted",
                }:
                    raise ValueError
                identity = AdminIdentity(data["appId"], row["openId"])
                if identity.open_id in seen:
                    raise ValueError
                seen.add(identity.open_id)
                revision = row["addRevision"]
                if type(revision) is not int or revision < 1 or type(row["refreshAttempted"]) is not bool:
                    raise ValueError
                if not isinstance(row["accessToken"], str) or not row["accessToken"] or len(row["accessToken"]) > 16384:
                    raise ValueError
                refresh = row["refreshToken"]
                if refresh is not None and (not isinstance(refresh, str) or not refresh or len(refresh) > 16384):
                    raise ValueError
                for key in ("expiresAt", "refreshExpiresAt"):
                    value = row[key]
                    if value is None and key == "refreshExpiresAt":
                        continue
                    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                        raise ValueError
                scopes = row["scopes"]
                if not isinstance(scopes, list) or len(scopes) > 256 or any(not isinstance(s, str) or not s or len(s) > 256 for s in scopes):
                    raise ValueError
                member = self.users.get_membership(identity)
                if member is not None and member.add_revision == revision:
                    self._records[identity] = _Credential(revision, UserTokens(
                        access_token=row["accessToken"], refresh_token=refresh,
                        expires_at=row["expiresAt"], refresh_expires_at=row["refreshExpiresAt"],
                        scopes=tuple(scopes),
                    ), row["refreshAttempted"])
        except (ValueError, TypeError, KeyError):
            raise PrivateJSONError() from None

    def _write(self, records: dict[AdminIdentity, _Credential]) -> None:
        write_private_json(self._path, {
            "version": 1, "appId": self.users.app_id,
            "users": [{
                "openId": identity.open_id, "addRevision": record.add_revision,
                "accessToken": record.tokens.access_token,
                "refreshToken": record.tokens.refresh_token,
                "expiresAt": record.tokens.expires_at,
                "refreshExpiresAt": record.tokens.refresh_expires_at,
                "scopes": list(record.tokens.scopes),
                "refreshAttempted": record.refresh_attempted,
            } for identity, record in sorted(records.items(), key=lambda item: item[0].open_id)],
        })

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
                updated = dict(self._records)
                updated[identity] = _Credential(expected_add_revision, tokens)
                try:
                    self._write(updated)
                except Exception:
                    # The rename may have committed. Never refresh an older
                    # in-memory RT when the persisted version is uncertain.
                    old = self._records.get(identity)
                    if old is not None:
                        self._records[identity] = _Credential(old.add_revision, old.tokens, True)
                    raise
                self._records = updated

    async def access_token(self, identity: AdminIdentity, add_revision: int, *, force_refresh: bool = False,
                           rejected_token: str | None = None) -> str:
        async with self._single_user(identity):
            with self.users.lock:
                self._member(identity, add_revision)
                record = self._records.get(identity)
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
                    self._records[identity] = record
                    # Persist before sending: timeout/cancellation/restart may
                    # otherwise consume the same single-use refresh token twice.
                    self._write(self._records)
                renewed = await self.client.refresh(tokens.refresh_token)
                with self.users.lock:
                    self._member(identity, add_revision)
                    if self._records.get(identity) is not record:
                        raise AuthExpired("auth_expired", "飞书凭证已变化，请重新登录。")
                    updated = dict(self._records)
                    updated[identity] = _Credential(add_revision, renewed)
                    self._write(updated)
                    self._records = updated
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
        # Invoked after roster removal. Memory is invalidated even when the
        # physical cleanup fails. The member incarnation also gates reload.
        try:
            self.users.get_membership(identity)
        except AdminUsersUnavailable:
            # A roster IO failure closes authorization, but is not an explicit
            # deletion of every user's durable credential. Preserve disk state.
            return
        if identity in self._records:
            del self._records[identity]
            self._write(self._records)

    def close(self) -> None:
        self._closed = True
        self._records.clear()  # Restart intentionally does not touch disk.
