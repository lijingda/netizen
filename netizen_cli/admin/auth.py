"""In-memory browser authentication and authorization for Admin Web.

This module deliberately contains no HTTP concepts. It owns opaque browser
tokens, login throttling, and one-shot action
authorization state; the HTTP adapter is responsible for cookies, Host,
Origin, and status-code mapping.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import math
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Generic, TypeVar, cast

from .users import AdminIdentity, AdminUsers, AdminUsersUnavailable


_TOKEN_BYTES = 32
_TOKEN_TEXT_BYTES = 43


class AdminAuthError(RuntimeError):
    """Base class for stable, non-sensitive Admin authentication failures."""

    code = "admin_auth_error"


class AdmissionClosed(AdminAuthError):
    code = "admin_admission_closed"

    def __init__(self) -> None:
        super().__init__("admin admission is closed")


class LoginRejected(AdminAuthError):
    """The intentionally uniform result for every failed login path."""

    code = "login_rejected"

    def __init__(self) -> None:
        super().__init__("admin login rejected")


class AuthCapacityExceeded(AdminAuthError):
    code = "auth_capacity_exceeded"

    def __init__(self) -> None:
        super().__init__("admin authentication capacity unavailable")


class SessionRejected(AdminAuthError):
    code = "session_rejected"

    def __init__(self) -> None:
        super().__init__("admin session rejected")


class ActionGrantError(AdminAuthError):
    pass


class MalformedActionGrant(ActionGrantError):
    code = "malformed_action_grant"

    def __init__(self) -> None:
        super().__init__("malformed action authorization")


class ActionCsrfRejected(ActionGrantError):
    code = "action_csrf_rejected"

    def __init__(self) -> None:
        super().__init__("action CSRF authorization rejected")


class StaleActionGrant(ActionGrantError):
    code = "stale_action_grant"

    def __init__(self) -> None:
        super().__init__("stale action authorization")


class ConsumedActionGrant(ActionGrantError):
    code = "consumed_action_grant"

    def __init__(self) -> None:
        super().__init__("action authorization was already consumed")


class InvalidActionPayload(ValueError):
    def __init__(self) -> None:
        super().__init__(
            "action target and preconditions must be typed immutable values"
        )


@dataclass(frozen=True, slots=True)
class AuthLimits:
    """Resource and lifetime limits for process-local authentication state."""

    action_ttl: float = 10 * 60
    login_window: float = 5 * 60
    login_failures_per_source: int = 5
    login_failures_global: int = 20
    max_rate_sources: int = 1_024
    max_preauth: int = 1_024
    max_preauth_per_source: int = 16
    max_sessions: int = 256
    max_sessions_per_source: int = 16
    # A 100-row Sessions page can expose several exact actions per row, and
    # cursor navigation keeps the previous page's one-shot grants alive until
    # their normal TTL. Keep this bounded while allowing several full pages.
    max_actions: int = 32_768
    max_actions_per_session: int = 8_192

    def __post_init__(self) -> None:
        durations = (
            self.action_ttl,
            self.login_window,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
            for value in durations
        ):
            raise ValueError("authentication TTLs must be positive finite numbers")
        capacities = (
            self.login_failures_per_source,
            self.login_failures_global,
            self.max_rate_sources,
            self.max_preauth,
            self.max_preauth_per_source,
            self.max_sessions,
            self.max_sessions_per_source,
            self.max_actions,
            self.max_actions_per_session,
        )
        if any(type(value) is not int or value <= 0 for value in capacities):
            raise ValueError("authentication capacities must be positive integers")
        if self.max_preauth_per_source > self.max_preauth:
            raise ValueError("per-source pre-auth capacity exceeds global capacity")
        if self.max_sessions_per_source > self.max_sessions:
            raise ValueError("per-source session capacity exceeds global capacity")
        if self.max_actions_per_session > self.max_actions:
            raise ValueError("per-session action capacity exceeds global capacity")


@dataclass(frozen=True, slots=True)
class IssuedPreauthChallenge:
    cookie_token: str = field(repr=False)
    form_nonce: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    token: str = field(repr=False)
    log_handle: str
    generation: int
    identity: AdminIdentity
    add_revision: int


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    log_handle: str
    generation: int
    identity: AdminIdentity
    add_revision: int


class ExpectationMode(str, Enum):
    """Three-state expectation; EXPECT_NONE is not DONT_CHECK."""

    DONT_CHECK = "dont_check"
    EXPECT_NONE = "expect_none"
    EXPECT_VALUE = "expect_value"


ExpectedT = TypeVar("ExpectedT")


@dataclass(frozen=True, slots=True)
class ExpectedValue(Generic[ExpectedT]):
    mode: ExpectationMode
    value: ExpectedT | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ExpectationMode):
            raise ValueError("expectation mode must be an ExpectationMode")
        if self.mode is ExpectationMode.EXPECT_VALUE:
            if self.value is None:
                raise ValueError("EXPECT_VALUE requires a value")
        elif self.value is not None:
            raise ValueError("only EXPECT_VALUE accepts a value")

    @classmethod
    def dont_check(cls) -> "ExpectedValue[ExpectedT]":
        return cls(ExpectationMode.DONT_CHECK)

    @classmethod
    def expect_none(cls) -> "ExpectedValue[ExpectedT]":
        return cls(ExpectationMode.EXPECT_NONE)

    @classmethod
    def expect(cls, value: ExpectedT) -> "ExpectedValue[ExpectedT]":
        return cls(ExpectationMode.EXPECT_VALUE, value)


TargetT = TypeVar("TargetT")
PreconditionsT = TypeVar("PreconditionsT")


@dataclass(frozen=True, slots=True)
class IssuedActionGrant:
    csrf_token: str = field(repr=False)
    action_token: str = field(repr=False)
    expires_at: float


@dataclass(frozen=True, slots=True)
class RedeemedAction(Generic[TargetT, PreconditionsT]):
    session_log_handle: str
    generation: int
    action_kind: str
    target: TargetT
    preconditions: PreconditionsT
    identity: AdminIdentity
    add_revision: int


@dataclass(frozen=True, slots=True)
class AuthStateCounts:
    admission_open: bool
    generation: int
    preauth: int
    sessions: int
    actions: int
    rate_sources: int
    global_failures: int


@dataclass(slots=True, repr=False)
class _PreauthRecord:
    form_digest: bytes = field(repr=False)
    source: str
    generation: int


@dataclass(slots=True, repr=False)
class _SessionRecord:
    source: str
    log_handle: str
    generation: int
    identity: AdminIdentity
    add_revision: int


@dataclass(slots=True, repr=False)
class _ActionRecord:
    session_digest: bytes = field(repr=False)
    csrf_digest: bytes = field(repr=False)
    generation: int
    action_kind: str
    target: object
    preconditions: object
    expires_at: float
    consumed: bool = False


def normalize_source_ip(source: str) -> str:
    """Return one stable spelling for a transport-provided source address."""

    if not isinstance(source, str) or not source or len(source) > 128:
        raise ValueError("invalid source address")
    if "%" in source:
        # A socket peer address already has a separate scope-id field.  Accepting
        # arbitrary textual zones here would let equivalent IPs occupy counters.
        raise ValueError("invalid source address")
    try:
        address = ipaddress.ip_address(source)
    except ValueError as error:
        raise ValueError("invalid source address") from error
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.compressed
    return address.compressed.lower()


def _opaque_digest(token: bytes) -> bytes | None:
    if len(token) != _TOKEN_TEXT_BYTES:
        return None
    if b"\n" in token or b"\r" in token:
        return None
    try:
        decoded = base64.b64decode(token + b"=", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(decoded) != _TOKEN_BYTES:
        return None
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=")
    if not hmac.compare_digest(canonical, token):
        return None
    return hashlib.sha256(decoded).digest()


def _token_digest(token: object) -> bytes | None:
    if not isinstance(token, str):
        return None
    try:
        encoded = token.encode("ascii")
    except UnicodeEncodeError:
        return None
    return _opaque_digest(encoded)


def _new_token() -> tuple[str, bytes]:
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    digest = _token_digest(token)
    if digest is None:  # pragma: no cover - protects the stdlib contract.
        raise RuntimeError("secure token generator returned a non-canonical token")
    return token, digest


class AdminAuth:
    """Thread-safe, process-local authentication and action grant owner."""

    def __init__(
        self,
        users: AdminUsers,
        *,
        clock: Callable[[], float] = time.monotonic,
        limits: AuthLimits | None = None,
    ) -> None:
        self._users = users
        self._clock = clock
        self._limits = AuthLimits() if limits is None else limits
        self._lock = users.lock
        self._generation = 1
        self._admission_open = True
        self._last_now: float | None = None
        self._preauth: dict[bytes, _PreauthRecord] = {}
        self._sessions: dict[bytes, _SessionRecord] = {}
        self._actions: dict[bytes, _ActionRecord] = {}
        self._rate_by_source: dict[str, deque[float]] = {}
        self._rate_global: deque[float] = deque()
        users.subscribe_revocation(self.revoke_identity)

    @property
    def users(self) -> AdminUsers:
        return self._users

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def admission_open(self) -> bool:
        with self._lock:
            return self._admission_open

    def issue_preauth(self, source_ip: str) -> IssuedPreauthChallenge:
        with self._lock:
            self._require_admission_locked()
            now = self._now_locked()
            self._prune_locked(now)
            source = self._normalize_login_source(source_ip)
            if not self._rate_allowed_locked(source, now):
                raise LoginRejected()
            # Challenges do not expire with time. Reclaim the oldest at capacity
            # so abandoned login pages cannot permanently prevent new logins.
            source_challenges = [
                digest
                for digest, record in self._preauth.items()
                if record.source == source
            ]
            if len(source_challenges) >= self._limits.max_preauth_per_source:
                self._preauth.pop(source_challenges[0])
            elif len(self._preauth) >= self._limits.max_preauth:
                self._preauth.pop(next(iter(self._preauth)))
            cookie_token, cookie_digest = self._unique_token_locked(self._preauth)
            form_nonce, form_digest = _new_token()
            while hmac.compare_digest(cookie_digest, form_digest):
                form_nonce, form_digest = _new_token()
            self._preauth[cookie_digest] = _PreauthRecord(
                form_digest=form_digest,
                source=source,
                generation=self._generation,
            )
            return IssuedPreauthChallenge(
                cookie_token=cookie_token,
                form_nonce=form_nonce,
            )

    def consume_preauth(
        self,
        *,
        source_ip: str,
        cookie_token: object,
        form_nonce: object,
    ) -> None:
        """Consume the browser-bound challenge before starting Feishu login."""

        with self._lock:
            self._require_admission_locked()
            now = self._now_locked()
            self._prune_locked(now)
            source = self._normalize_login_source(source_ip)
            rate_allowed = self._rate_allowed_locked(source, now)
            nonce_valid = self._consume_preauth_locked(
                source=source,
                cookie_token=cookie_token,
                form_nonce=form_nonce,
            )
            if not (rate_allowed and nonce_valid):
                if rate_allowed:
                    self._record_login_failure_locked(source, now)
                raise LoginRejected()

    def reject_login(self, source_ip: str) -> None:
        """Record a failed upstream identity attempt without revealing its reason."""
        with self._lock:
            self._require_admission_locked()
            source = self._normalize_login_source(source_ip)
            now = self._now_locked()
            if self._rate_allowed_locked(source, now):
                self._record_login_failure_locked(source, now)
            raise LoginRejected()

    def precheck_for_session(
        self, identity: AdminIdentity, *, source_ip: str, expected_add_revision: int,
    ) -> None:
        """Check the trusted login commit before persisting its user credential.

        The credential writer calls this under the shared membership lock after
        any per-user refresh wait. It neither allocates nor evicts a session.
        """
        with self._lock:
            self._require_admission_locked()
            source = self._normalize_login_source(source_ip)
            member = self._users.get_membership(identity)
            if (not self._rate_allowed_locked(source, self._now_locked())
                    or member is None or type(expected_add_revision) is not int
                    or member.add_revision != expected_add_revision):
                raise LoginRejected()

    def issue_session(
        self, identity: AdminIdentity, *, source_ip: str, expected_add_revision: int,
    ) -> IssuedSession:
        """Trusted server-only handoff after Feishu identity and attempt validation."""
        with self._lock:
            self._require_admission_locked()
            now = self._now_locked()
            self._prune_locked(now)
            source = self._normalize_login_source(source_ip)
            member = self._users.get_membership(identity)
            rate_allowed = self._rate_allowed_locked(source, now)
            if (not rate_allowed or member is None or type(expected_add_revision) is not int
                    or member.add_revision != expected_add_revision):
                if rate_allowed:
                    self._record_login_failure_locked(source, now)
                raise LoginRejected()
            # Sessions do not expire with time. A verified new login reclaims
            # the oldest session at capacity, including abandoned browser cookies.
            source_sessions = [
                digest
                for digest, record in self._sessions.items()
                if record.source == source
            ]
            if len(source_sessions) >= self._limits.max_sessions_per_source:
                self._drop_session_locked(source_sessions[0])
            elif len(self._sessions) >= self._limits.max_sessions:
                self._drop_session_locked(next(iter(self._sessions)))
            token, token_digest = self._unique_token_locked(self._sessions)
            log_handle = secrets.token_hex(16)
            self._sessions[token_digest] = _SessionRecord(
                source=source,
                log_handle=log_handle,
                generation=self._generation,
                identity=identity,
                add_revision=member.add_revision,
            )
            return IssuedSession(
                token=token,
                log_handle=log_handle,
                generation=self._generation,
                identity=identity,
                add_revision=member.add_revision,
            )

    def authenticate(
        self,
        session_token: object,
    ) -> AuthenticatedSession:
        """Authenticate a process-local session until it is explicitly revoked."""

        with self._lock:
            self._require_admission_locked()
            now = self._now_locked()
            self._prune_locked(now)
            _, record = self._authenticate_session_locked(session_token)
            return self._authenticated_session(record)

    def logout(self, session_token: object) -> bool:
        with self._lock:
            self._require_admission_locked()
            now = self._now_locked()
            self._prune_locked(now)
            digest = _token_digest(session_token)
            if digest is None or digest not in self._sessions:
                return False
            self._drop_session_locked(digest)
            return True

    def issue_action(
        self,
        session_token: object,
        *,
        action_kind: str,
        target: TargetT,
        preconditions: PreconditionsT,
    ) -> IssuedActionGrant:
        """Bind immutable exact facts to independent one-shot browser tokens."""

        self._validate_action_kind(action_kind)
        _require_typed_immutable_payload(target)
        _require_typed_immutable_payload(preconditions)
        with self._lock:
            self._require_admission_locked()
            now = self._now_locked()
            self._prune_locked(now)
            session_digest, _ = self._authenticate_session_locked(session_token)
            if len(self._actions) >= self._limits.max_actions:
                raise AuthCapacityExceeded()
            session_count = sum(
                hmac.compare_digest(record.session_digest, session_digest)
                for record in self._actions.values()
            )
            if session_count >= self._limits.max_actions_per_session:
                raise AuthCapacityExceeded()
            action_token, action_digest = self._unique_token_locked(self._actions)
            csrf_token, csrf_digest = _new_token()
            while hmac.compare_digest(action_digest, csrf_digest):
                csrf_token, csrf_digest = _new_token()
            expires_at = now + self._limits.action_ttl
            self._actions[action_digest] = _ActionRecord(
                session_digest=session_digest,
                csrf_digest=csrf_digest,
                generation=self._generation,
                action_kind=action_kind,
                target=target,
                preconditions=preconditions,
                expires_at=expires_at,
            )
            return IssuedActionGrant(
                csrf_token=csrf_token,
                action_token=action_token,
                expires_at=expires_at,
            )

    def redeem_action(
        self,
        session_token: object,
        *,
        csrf_token: object,
        action_token: object,
        action_kind: str,
        target: TargetT,
    ) -> RedeemedAction[TargetT, object]:
        """Atomically redeem an exact grant; at most one caller can succeed."""

        self._validate_action_kind(action_kind)
        _require_typed_immutable_payload(target)
        with self._lock:
            self._require_admission_locked()
            now = self._now_locked()
            self._prune_locked(now)
            session_digest, session = self._authenticate_session_locked(
                session_token,
            )
            action_digest = _token_digest(action_token)
            if action_digest is None:
                raise MalformedActionGrant()
            csrf_digest = _token_digest(csrf_token)
            if csrf_digest is None:
                raise ActionCsrfRejected()
            record = self._actions.get(action_digest)
            if record is None:
                raise StaleActionGrant()
            if (
                record.generation != self._generation
                or not hmac.compare_digest(record.session_digest, session_digest)
                or record.expires_at <= now
                or record.action_kind != action_kind
                or type(record.target) is not type(target)
                or record.target != target
            ):
                raise StaleActionGrant()
            if not hmac.compare_digest(record.csrf_digest, csrf_digest):
                raise ActionCsrfRejected()
            if record.consumed:
                raise ConsumedActionGrant()
            record.consumed = True
            return RedeemedAction(
                session_log_handle=session.log_handle,
                generation=self._generation,
                action_kind=record.action_kind,
                target=cast(TargetT, record.target),
                preconditions=record.preconditions,
                identity=session.identity,
                add_revision=session.add_revision,
            )

    def state_counts(self) -> AuthStateCounts:
        """Return non-sensitive process-local counts for tests and diagnostics."""

        with self._lock:
            now = self._now_locked()
            self._prune_locked(now)
            self._prune_rate_locked(now)
            return AuthStateCounts(
                admission_open=self._admission_open,
                generation=self._generation,
                preauth=len(self._preauth),
                sessions=len(self._sessions),
                actions=len(self._actions),
                rate_sources=len(self._rate_by_source),
                global_failures=len(self._rate_global),
            )

    def close(self) -> None:
        with self._lock:
            self._close_admission_locked()

    def _require_admission_locked(self) -> None:
        if not self._admission_open:
            raise AdmissionClosed()
        try:
            self._users.has_admins
        except AdminUsersUnavailable:
            self._close_admission_locked()
            raise AdmissionClosed() from None

    def revoke_identity(self, identity: AdminIdentity) -> None:
        """Revoke all browser sessions and action grants for one exact user."""
        with self._lock:
            for digest, record in tuple(self._sessions.items()):
                if record.identity == identity:
                    self._drop_session_locked(digest)

    def _close_admission_locked(self) -> None:
        self._admission_open = False
        self._clear_bearer_state_locked()

    def _clear_bearer_state_locked(self) -> None:
        self._preauth.clear()
        self._sessions.clear()
        self._actions.clear()

    def _now_locked(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise RuntimeError("monotonic clock returned a non-finite value")
        if self._last_now is not None and now < self._last_now:
            raise RuntimeError("monotonic clock moved backwards")
        self._last_now = now
        return now

    def _normalize_login_source(self, source_ip: str) -> str:
        try:
            return normalize_source_ip(source_ip)
        except ValueError:
            raise LoginRejected() from None

    def _prune_locked(self, now: float) -> None:
        expired_actions = [
            digest
            for digest, record in self._actions.items()
            if record.expires_at <= now
        ]
        for digest in expired_actions:
            self._actions.pop(digest, None)

    def _prune_rate_locked(self, now: float) -> None:
        cutoff = now - self._limits.login_window
        while self._rate_global and self._rate_global[0] <= cutoff:
            self._rate_global.popleft()
        empty: list[str] = []
        for source, failures in self._rate_by_source.items():
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                empty.append(source)
        for source in empty:
            self._rate_by_source.pop(source, None)

    def _rate_allowed_locked(self, source: str, now: float) -> bool:
        self._prune_rate_locked(now)
        if len(self._rate_global) >= self._limits.login_failures_global:
            return False
        failures = self._rate_by_source.get(source)
        if failures is not None:
            return len(failures) < self._limits.login_failures_per_source
        return len(self._rate_by_source) < self._limits.max_rate_sources

    def _record_login_failure_locked(self, source: str, now: float) -> None:
        self._rate_global.append(now)
        failures = self._rate_by_source.get(source)
        if failures is None:
            if len(self._rate_by_source) >= self._limits.max_rate_sources:
                return
            failures = deque()
            self._rate_by_source[source] = failures
        failures.append(now)

    def _consume_preauth_locked(
        self,
        *,
        source: str,
        cookie_token: object,
        form_nonce: object,
    ) -> bool:
        cookie_digest = _token_digest(cookie_token)
        form_digest = _token_digest(form_nonce)
        if cookie_digest is None or form_digest is None:
            return False
        record = self._preauth.pop(cookie_digest, None)
        if record is None:
            return False
        return (
            record.generation == self._generation
            and record.source == source
            and hmac.compare_digest(record.form_digest, form_digest)
        )

    def _authenticate_session_locked(
        self,
        session_token: object,
    ) -> tuple[bytes, _SessionRecord]:
        digest = _token_digest(session_token)
        if digest is None:
            raise SessionRejected()
        record = self._sessions.get(digest)
        if record is None or record.generation != self._generation:
            if record is not None:
                self._drop_session_locked(digest)
            raise SessionRejected()
        member = self._users.get_membership(record.identity)
        if member is None or member.add_revision != record.add_revision:
            self._drop_session_locked(digest)
            raise SessionRejected()
        return digest, record

    def _authenticated_session(
        self,
        record: _SessionRecord,
    ) -> AuthenticatedSession:
        return AuthenticatedSession(
            log_handle=record.log_handle,
            generation=record.generation,
            identity=record.identity,
            add_revision=record.add_revision,
        )

    def _drop_session_locked(self, session_digest: bytes) -> None:
        self._sessions.pop(session_digest, None)
        action_digests = [
            digest
            for digest, record in self._actions.items()
            if hmac.compare_digest(record.session_digest, session_digest)
        ]
        for digest in action_digests:
            self._actions.pop(digest, None)

    @staticmethod
    def _validate_action_kind(action_kind: str) -> None:
        if (
            not isinstance(action_kind, str)
            or not action_kind
            or len(action_kind.encode("utf-8")) > 128
            or any(ord(character) < 0x20 for character in action_kind)
        ):
            raise ValueError("action kind must be a bounded non-empty string")

    @staticmethod
    def _unique_token_locked(
        records: dict[bytes, object],
    ) -> tuple[str, bytes]:
        for _ in range(8):
            token, digest = _new_token()
            if digest not in records:
                return token, digest
        raise RuntimeError("secure token generator repeatedly collided")


def _require_typed_immutable_payload(value: object) -> None:
    if not is_dataclass(value) or isinstance(value, type):
        raise InvalidActionPayload()
    parameters = getattr(type(value), "__dataclass_params__", None)
    if parameters is None or not parameters.frozen:
        raise InvalidActionPayload()
    if not _is_deeply_immutable(value, active=set(), checked=set()):
        raise InvalidActionPayload()


def _is_deeply_immutable(
    value: object,
    *,
    active: set[int],
    checked: set[int],
) -> bool:
    if value is None or isinstance(value, (str, bytes, int, float, bool, Enum)):
        return True
    identity = id(value)
    if identity in checked:
        return True
    if identity in active:
        return False
    active.add(identity)
    try:
        if isinstance(value, tuple):
            valid = all(
                _is_deeply_immutable(item, active=active, checked=checked)
                for item in value
            )
        elif isinstance(value, frozenset):
            valid = all(
                _is_deeply_immutable(item, active=active, checked=checked)
                for item in value
            )
        elif is_dataclass(value) and not isinstance(value, type):
            parameters = getattr(type(value), "__dataclass_params__", None)
            valid = bool(parameters is not None and parameters.frozen) and all(
                _is_deeply_immutable(
                    getattr(value, item.name),
                    active=active,
                    checked=checked,
                )
                for item in fields(value)
            )
        else:
            valid = False
    finally:
        active.remove(identity)
    if valid:
        checked.add(identity)
    return valid
