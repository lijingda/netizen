"""Bounded, browser-bound device authorization attempts for Admin Web."""

from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Callable

from .auth import AdminAuth, AdminAuthError, LoginRejected, normalize_source_ip
from .credentials import AdminCredentials
from .feishu import AuthExpired, DeviceChallenge, FeishuClient, FeishuError, SlowDownError
from .users import AdminIdentity, AdminUsers


_START_WINDOW = 300
_STARTS_PER_SOURCE = 5
_STARTS_GLOBAL = 20
_MAX_ATTEMPTS = 128
_MAX_PENDING_PER_SOURCE = 4


@dataclass
class _Attempt:
    source: str
    nonce_digest: bytes = field(repr=False)
    revision: int
    deadline: float
    challenge: DeviceChallenge | None = field(default=None, repr=False)
    state: str = "pending"
    interval: float = 5
    next_poll: float = 0
    session_token: str | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _source(value: str) -> str:
    try:
        return normalize_source_ip(value)
    except ValueError:
        raise LoginRejected() from None


class AdminLogin:
    def __init__(self, auth: AdminAuth, users: AdminUsers, credentials: AdminCredentials,
                 client: FeishuClient, *, clock: Callable[[], float] = time.time) -> None:
        self.auth = auth
        self.users = users
        self.credentials = credentials
        self.client = client
        self._clock = clock
        self._attempts: dict[bytes, _Attempt] = {}
        # A single bounded window covers accepted starts, including cancelled
        # or failed requests. Concurrent-attempt capacity is a separate limit.
        self._starts: deque[tuple[float, str]] = deque()
        self._closed = False

    def _prune(self) -> None:
        now = self._clock()
        while self._starts and self._starts[0][0] <= now - _START_WINDOW:
            self._starts.popleft()
        for key, attempt in tuple(self._attempts.items()):
            if now >= attempt.deadline:
                del self._attempts[key]

    def _current(self, key: bytes, attempt: _Attempt) -> bool:
        return not self._closed and self._attempts.get(key) is attempt and self._clock() < attempt.deadline

    async def start(self, *, cookie: str | None, nonce: str, source: str) -> dict:
        source = _source(source)
        self._prune()
        pending = [attempt for attempt in self._attempts.values() if attempt.state == "pending"]
        if (self._closed or not cookie or len(pending) >= _MAX_ATTEMPTS
                or sum(attempt.source == source for attempt in pending) >= _MAX_PENDING_PER_SOURCE):
            raise LoginRejected()
        self.auth.consume_preauth(source_ip=source, cookie_token=cookie, form_nonce=nonce)
        if (len(self._starts) >= _STARTS_GLOBAL
                or sum(previous == source for _, previous in self._starts) >= _STARTS_PER_SOURCE):
            raise LoginRejected()
        self._starts.append((self._clock(), source))
        key = _digest(cookie)
        attempt = _Attempt(source, _digest(nonce), self.users.revision, self._clock() + 30)
        if len(self._attempts) >= _MAX_ATTEMPTS:
            # Retain terminal responses for browser retries while space permits.
            # Reclaiming one must not log out its already authenticated session.
            completed = next(key for key, previous in self._attempts.items()
                             if previous.state != "pending")
            del self._attempts[completed]
        self._attempts[key] = attempt
        try:
            challenge = await self.client.start_login()
            if not self._current(key, attempt):
                raise LoginRejected()
            attempt.challenge = challenge
            attempt.deadline = min(challenge.expires_at, self._clock() + 900)
            attempt.interval = max(1, challenge.interval)
            attempt.next_poll = self._clock() + attempt.interval
            return self._state(attempt)
        except BaseException:
            if self._attempts.get(key) is attempt:
                del self._attempts[key]
            raise

    async def status(self, *, cookie: str | None, source: str) -> tuple[dict, str | None]:
        source = _source(source)
        if self._closed or not cookie:
            return {"state": "idle"}, None
        key = _digest(cookie)
        attempt = self._attempts.get(key)
        if attempt is None or attempt.source != source:
            return {"state": "idle"}, None
        if self._clock() >= attempt.deadline:
            del self._attempts[key]
            return {"state": "expired", "message": "本次授权已过期，请重新开始。"}, None
        async with attempt.lock:
            if not self._current(key, attempt):
                return {"state": "idle"}, None
            if attempt.state == "pending" and attempt.challenge and self._clock() >= attempt.next_poll:
                attempt.next_poll = self._clock() + attempt.interval
                try:
                    tokens = await self.client.poll_login(attempt.challenge.device_code)
                    if tokens is not None:
                        profile = await self.client.user_info(tokens.access_token)
                        identity = AdminIdentity(self.users.app_id, profile.open_id)
                        member = self.users.get_membership(identity)
                        if member is None or member.add_revision > attempt.revision:
                            raise AuthExpired("not_administrator", "该用户不是当前实例管理员。")

                        def current_login() -> bool:
                            if not self._current(key, attempt):
                                return False
                            self.auth.precheck_for_session(
                                identity, source_ip=source,
                                expected_add_revision=member.add_revision,
                            )
                            return True

                        await self.credentials.save_login(
                            identity, tokens, expected_add_revision=member.add_revision,
                            is_current=current_login,
                        )
                        with self.users.lock:
                            if not self._current(key, attempt):
                                raise LoginRejected()
                            self.users.update_profile(identity, name=profile.name, avatar_url=profile.avatar_url,
                                                      expected_add_revision=member.add_revision)
                            issued = self.auth.issue_session(
                                identity, source_ip=source, expected_add_revision=member.add_revision,
                            )
                            attempt.session_token = issued.token
                            attempt.state = "authenticated"
                except SlowDownError:
                    attempt.interval += 5
                    attempt.next_poll = self._clock() + attempt.interval
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if self._current(key, attempt):
                        code = error.code if isinstance(error, FeishuError) else "login_failed"
                        attempt.state = "denied" if code in {"access_denied", "not_administrator"} else (
                            "expired" if code in {
                                "expired_token", "token_expired", "login_expired", "auth_expired",
                            } else "failed"
                        )
                        try:
                            self.auth.reject_login(source)
                        except AdminAuthError:
                            # reject_login records the failure by raising. The
                            # browser still receives this attempt's terminal state.
                            pass
            if not self._current(key, attempt):
                return {"state": "idle"}, None
            if attempt.session_token is not None:
                # Deletion/re-addition cannot reissue a stale browser session.
                try:
                    self.auth.authenticate(attempt.session_token)
                except Exception:
                    attempt.session_token = None
                    attempt.state = "expired"
            return self._state(attempt), attempt.session_token

    def _state(self, attempt: _Attempt) -> dict:
        result: dict = {"state": attempt.state, "interval": attempt.interval}
        messages = {
            "denied": "未获得管理员登录授权，请确认账号及管理员名单。",
            "expired": "本次授权已过期，请重新开始。",
            "failed": "登录未完成，请重新开始。",
        }
        if attempt.state in messages:
            result["message"] = messages[attempt.state]
        if attempt.challenge is not None and attempt.state == "pending":
            result.update(verificationUri=attempt.challenge.verification_uri_complete,
                          userCode=attempt.challenge.user_code, qrUrl="/api/v1/login/qr",
                          expiresAt=attempt.deadline)
        return result

    def verification_uri(self, cookie: str | None, source: str) -> str | None:
        source = _source(source)
        if self._closed or not cookie:
            return None
        attempt = self._attempts.get(_digest(cookie))
        if attempt is None or attempt.source != source or not self._current(_digest(cookie), attempt):
            return None
        return attempt.challenge.verification_uri_complete if attempt.challenge and attempt.state == "pending" else None

    def cancel(self, *, cookie: str | None, nonce: str, source: str) -> None:
        source = _source(source)
        if not cookie:
            raise LoginRejected()
        key = _digest(cookie)
        attempt = self._attempts.get(key)
        if attempt is None:
            return
        if attempt.source != source or not hmac.compare_digest(attempt.nonce_digest, _digest(nonce)):
            raise LoginRejected()
        self.discard(cookie)

    def discard(self, cookie: str | None) -> None:
        if cookie:
            attempt = self._attempts.pop(_digest(cookie), None)
            if attempt and attempt.session_token:
                self.auth.logout(attempt.session_token)

    def close(self) -> None:
        self._closed = True
        self._attempts.clear()
        self._starts.clear()
