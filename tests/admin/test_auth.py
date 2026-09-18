from __future__ import annotations

import os
import secrets
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from netizen.admin.auth import (
    ActionCsrfRejected,
    AdmissionClosed,
    AdminAuth,
    AuthCapacityExceeded,
    AuthLimits,
    ConsumedActionGrant,
    CredentialFileError,
    ExpectationMode,
    ExpectedValue,
    InvalidActionPayload,
    LoginRejected,
    MalformedActionGrant,
    SessionRejected,
    StaleActionGrant,
    load_credential_snapshot,
    normalize_source_ip,
)


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True, slots=True)
class ExactTarget:
    resource: str
    target_id: str
    scope_key: str | None = None


@dataclass(frozen=True, slots=True)
class ExactPreconditions:
    active_pointer: ExpectedValue[str]
    settings_revision: ExpectedValue[int]
    runtime_revision: ExpectedValue[int]
    physical_turn_id: ExpectedValue[str]


@dataclass(slots=True)
class MutableTarget:
    target_id: str


@dataclass(frozen=True, slots=True)
class ShallowFrozenMutableTarget:
    values: list[str]


class AdminAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.credential_path = self.root / "admin-web-secret"
        self.credential = secrets.token_urlsafe(32)
        self.write_credential(self.credential)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_credential(
        self,
        credential: str | bytes,
        *,
        suffix: bytes = b"\n",
        path: Path | None = None,
        mode: int = 0o600,
    ) -> Path:
        destination = self.credential_path if path is None else path
        content = (
            credential.encode("ascii")
            if isinstance(credential, str)
            else credential
        )
        destination.write_bytes(content + suffix)
        destination.chmod(mode)
        return destination

    def auth(self, *, limits: AuthLimits | None = None) -> AdminAuth:
        return AdminAuth(
            self.credential_path,
            clock=self.clock,
            limits=limits,
        )

    def login(
        self,
        auth: AdminAuth,
        *,
        source: str = "192.0.2.10",
        credential: str | None = None,
    ):
        challenge = auth.issue_preauth(source)
        return auth.login(
            source_ip=source,
            cookie_token=challenge.cookie_token,
            form_nonce=challenge.form_nonce,
            credential=self.credential if credential is None else credential,
        )

    def preconditions(
        self,
        *,
        active: ExpectedValue[str] | None = None,
    ) -> ExactPreconditions:
        return ExactPreconditions(
            active_pointer=(
                ExpectedValue.expect_none() if active is None else active
            ),
            settings_revision=ExpectedValue.expect(7),
            runtime_revision=ExpectedValue.expect(11),
            physical_turn_id=ExpectedValue.expect("turn-physical-1"),
        )

    def test_loader_uses_no_follow_cloexec_and_one_descriptor(self) -> None:
        opened: list[tuple[int, int]] = []
        fstat_descriptors: list[int] = []
        read_descriptors: list[int] = []
        real_open = os.open
        real_fstat = os.fstat
        real_read = os.read

        def tracked_open(path: str, flags: int) -> int:
            descriptor = real_open(path, flags)
            opened.append((descriptor, flags))
            return descriptor

        def tracked_fstat(descriptor: int):
            fstat_descriptors.append(descriptor)
            return real_fstat(descriptor)

        def tracked_read(descriptor: int, amount: int) -> bytes:
            read_descriptors.append(descriptor)
            return real_read(descriptor, amount)

        with (
            patch("netizen.admin.auth.os.open", side_effect=tracked_open),
            patch("netizen.admin.auth.os.fstat", side_effect=tracked_fstat),
            patch("netizen.admin.auth.os.read", side_effect=tracked_read),
        ):
            snapshot = load_credential_snapshot(self.credential_path)

        self.assertEqual(len(opened), 1)
        descriptor, flags = opened[0]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_CLOEXEC)
        self.assertEqual(fstat_descriptors, [descriptor, descriptor])
        self.assertTrue(read_descriptors)
        self.assertEqual(set(read_descriptors), {descriptor})
        self.assertNotIn(self.credential, repr(snapshot))

    def test_loader_rejects_symlink_non_regular_and_non_exact_mode(self) -> None:
        symlink = self.root / "linked-secret"
        symlink.symlink_to(self.credential_path)
        with self.assertRaises(CredentialFileError):
            load_credential_snapshot(symlink)
        with self.assertRaises(CredentialFileError):
            load_credential_snapshot(self.root)

        for mode in (0o400, 0o640, 0o644, 0o700):
            with self.subTest(mode=oct(mode)):
                self.credential_path.chmod(mode)
                with self.assertRaises(CredentialFileError):
                    load_credential_snapshot(self.credential_path)
        self.credential_path.chmod(0o600)

    def test_loader_rejects_metadata_mutation_between_read_and_second_fstat(
        self,
    ) -> None:
        real_fstat = os.fstat
        calls = 0

        def changing_fstat(descriptor: int):
            nonlocal calls
            calls += 1
            result = real_fstat(descriptor)
            if calls == 1:
                return result
            values = list(result)
            values[stat.ST_MTIME] = result.st_mtime + 1
            return os.stat_result(values)

        with patch("netizen.admin.auth.os.fstat", side_effect=changing_fstat):
            with self.assertRaises(CredentialFileError):
                load_credential_snapshot(self.credential_path)
        self.assertEqual(calls, 2)

    def test_loader_accepts_only_canonical_token_and_optional_one_lf(self) -> None:
        self.write_credential(self.credential, suffix=b"")
        load_credential_snapshot(self.credential_path)
        self.write_credential(self.credential, suffix=b"\n")
        load_credential_snapshot(self.credential_path)

        invalid = {
            "empty": b"",
            "short": self.credential[:-1].encode("ascii"),
            "padded": (self.credential + "=").encode("ascii"),
            "space": (self.credential[:-1] + " ").encode("ascii"),
            "crlf": self.credential.encode("ascii") + b"\r\n",
            "two-lines": self.credential.encode("ascii") + b"\n\n",
            "extra-line": self.credential.encode("ascii") + b"\nother",
            "noncanonical-padding-bits": (
                self.credential[:-1] + "B"
            ).encode("ascii"),
            "oversized": self.credential.encode("ascii") + (b"x" * 100),
        }
        for label, content in invalid.items():
            with self.subTest(label=label):
                self.write_credential(content, suffix=b"")
                with self.assertRaises(CredentialFileError):
                    load_credential_snapshot(self.credential_path)

    def test_relative_missing_and_initial_illegal_files_fail_closed(self) -> None:
        with self.assertRaises(CredentialFileError):
            load_credential_snapshot("relative-secret")
        with self.assertRaises(CredentialFileError):
            AdminAuth(self.root / "missing")
        self.credential_path.chmod(0o644)
        with self.assertRaises(CredentialFileError):
            AdminAuth(self.credential_path)

    def test_loader_errors_do_not_retain_os_error_causes(self) -> None:
        missing = self.root / "missing"
        with self.assertRaises(CredentialFileError) as caught:
            load_credential_snapshot(missing)
        self.assertIsNone(caught.exception.__cause__)

        with patch("netizen.admin.auth.os.read", side_effect=OSError("raw detail")):
            with self.assertRaises(CredentialFileError) as caught:
                load_credential_snapshot(self.credential_path)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("raw detail", repr(caught.exception))

    def test_preauth_nonce_is_source_bound_and_single_use(self) -> None:
        auth = self.auth()
        challenge = auth.issue_preauth("192.0.2.10")
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip="192.0.2.11",
                cookie_token=challenge.cookie_token,
                form_nonce=challenge.form_nonce,
                credential=self.credential,
            )
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip="192.0.2.10",
                cookie_token=challenge.cookie_token,
                form_nonce=challenge.form_nonce,
                credential=self.credential,
            )

        challenge = auth.issue_preauth("192.0.2.10")
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip="192.0.2.10",
                cookie_token=challenge.cookie_token,
                form_nonce=challenge.form_nonce,
                credential=secrets.token_urlsafe(32),
            )
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip="192.0.2.10",
                cookie_token=challenge.cookie_token,
                form_nonce=challenge.form_nonce,
                credential=self.credential,
            )

    def test_preauth_nonce_survives_time_but_is_consumed_by_login(self) -> None:
        auth = self.auth()
        challenge = auth.issue_preauth("192.0.2.10")
        self.clock.advance(30 * 24 * 60 * 60)
        self.assertEqual(auth.state_counts().preauth, 1)
        session = auth.login(
            source_ip="192.0.2.10",
            cookie_token=challenge.cookie_token,
            form_nonce=challenge.form_nonce,
            credential=self.credential,
        )
        auth.authenticate(session.token)
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip="192.0.2.10",
                cookie_token=challenge.cookie_token,
                form_nonce=challenge.form_nonce,
                credential=self.credential,
            )

    def test_login_failure_is_uniform_for_nonce_secret_and_rate(self) -> None:
        auth = self.auth()
        errors: list[str] = []
        challenge = auth.issue_preauth("192.0.2.10")
        for cookie, nonce, credential in (
            ("malformed", challenge.form_nonce, self.credential),
            (challenge.cookie_token, challenge.form_nonce, "malformed"),
        ):
            with self.assertRaises(LoginRejected) as caught:
                auth.login(
                    source_ip="192.0.2.10",
                    cookie_token=cookie,
                    form_nonce=nonce,
                    credential=credential,
                )
            errors.append(str(caught.exception))
        self.assertEqual(len(set(errors)), 1)

    def test_malformed_credential_cannot_match_the_dummy_comparison_digest(
        self,
    ) -> None:
        zero_credential = "A" * 43
        self.write_credential(zero_credential)
        auth = self.auth()
        challenge = auth.issue_preauth("192.0.2.10")
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip="192.0.2.10",
                cookie_token=challenge.cookie_token,
                form_nonce=challenge.form_nonce,
                credential="malformed",
            )
        challenge = auth.issue_preauth("192.0.2.10")
        session = auth.login(
            source_ip="192.0.2.10",
            cookie_token=challenge.cookie_token,
            form_nonce=challenge.form_nonce,
            credential=zero_credential,
        )
        auth.authenticate(session.token)

    def test_exact_per_source_sliding_window_limit_and_ip_normalization(self) -> None:
        auth = self.auth()
        source = "192.0.2.10"
        challenges = [auth.issue_preauth(source) for _ in range(6)]
        for index, challenge in enumerate(challenges[:5]):
            with self.assertRaises(LoginRejected):
                auth.login(
                    source_ip=(source if index % 2 == 0 else "::ffff:192.0.2.10"),
                    cookie_token=challenge.cookie_token,
                    form_nonce=challenge.form_nonce,
                    credential=secrets.token_urlsafe(32),
                )
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip=source,
                cookie_token=challenges[5].cookie_token,
                form_nonce=challenges[5].form_nonce,
                credential=self.credential,
            )

        self.clock.advance(5 * 60)
        recovered = auth.issue_preauth(source)
        session = auth.login(
            source_ip=source,
            cookie_token=recovered.cookie_token,
            form_nonce=recovered.form_nonce,
            credential=self.credential,
        )
        auth.authenticate(session.token)
        self.assertEqual(normalize_source_ip(source), "192.0.2.10")
        self.assertEqual(
            normalize_source_ip("::ffff:192.0.2.10"),
            "192.0.2.10",
        )

    def test_exact_global_sliding_window_limit(self) -> None:
        auth = self.auth()
        failure_challenges: list[tuple[str, object]] = []
        for source_index in range(4):
            source = f"192.0.2.{source_index + 1}"
            failure_challenges.extend(
                (source, auth.issue_preauth(source)) for _ in range(5)
            )
        success_source = "192.0.2.100"
        success_challenge = auth.issue_preauth(success_source)
        for source, challenge in failure_challenges:
            with self.assertRaises(LoginRejected):
                auth.login(
                    source_ip=source,
                    cookie_token=challenge.cookie_token,
                    form_nonce=challenge.form_nonce,
                    credential=secrets.token_urlsafe(32),
                )
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip=success_source,
                cookie_token=success_challenge.cookie_token,
                form_nonce=success_challenge.form_nonce,
                credential=self.credential,
            )

        self.clock.advance(5 * 60)
        recovered = auth.issue_preauth(success_source)
        session = auth.login(
            source_ip=success_source,
            cookie_token=recovered.cookie_token,
            form_nonce=recovered.form_nonce,
            credential=self.credential,
        )
        auth.authenticate(session.token)

    def test_rate_source_cardinality_is_bounded_without_live_eviction(self) -> None:
        limits = AuthLimits(max_rate_sources=2)
        auth = self.auth(limits=limits)
        challenges = {
            source: auth.issue_preauth(source)
            for source in ("192.0.2.1", "192.0.2.2", "192.0.2.3")
        }
        for source in ("192.0.2.1", "192.0.2.2"):
            challenge = challenges[source]
            with self.assertRaises(LoginRejected):
                auth.login(
                    source_ip=source,
                    cookie_token=challenge.cookie_token,
                    form_nonce=challenge.form_nonce,
                    credential=secrets.token_urlsafe(32),
                )
        third = challenges["192.0.2.3"]
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip="192.0.2.3",
                cookie_token=third.cookie_token,
                form_nonce=third.form_nonce,
                credential=self.credential,
            )
        self.assertEqual(auth.state_counts().rate_sources, 2)
        self.clock.advance(5 * 60)
        recovered = auth.issue_preauth("192.0.2.3")
        session = auth.login(
            source_ip="192.0.2.3",
            cookie_token=recovered.cookie_token,
            form_nonce=recovered.form_nonce,
            credential=self.credential,
        )
        auth.authenticate(session.token)

    def test_identity_or_content_rotation_invalidates_all_bearer_state(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        target = ExactTarget("binding", "binding-1", "chat:1")
        grant = auth.issue_action(
            session.token,
            action_kind="binding.stop",
            target=target,
            preconditions=self.preconditions(),
        )
        old_challenge = auth.issue_preauth("192.0.2.20")

        replacement = self.root / "replacement"
        self.write_credential(
            self.credential,
            path=replacement,
        )
        replacement.replace(self.credential_path)
        self.assertTrue(auth.refresh_credential())
        self.assertEqual(auth.generation, 2)
        counts = auth.state_counts()
        self.assertEqual((counts.preauth, counts.sessions, counts.actions), (0, 0, 0))
        with self.assertRaises(SessionRejected):
            auth.authenticate(session.token)
        with self.assertRaises(SessionRejected):
            auth.redeem_action(
                session.token,
                csrf_token=grant.csrf_token,
                action_token=grant.action_token,
                action_kind="binding.stop",
                target=target,
            )
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip="192.0.2.20",
                cookie_token=old_challenge.cookie_token,
                form_nonce=old_challenge.form_nonce,
                credential=self.credential,
            )

        rotated = secrets.token_urlsafe(32)
        self.write_credential(rotated)
        self.assertTrue(auth.refresh_credential())
        self.assertEqual(auth.generation, 3)
        challenge = auth.issue_preauth("192.0.2.30")
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip="192.0.2.30",
                cookie_token=challenge.cookie_token,
                form_nonce=challenge.form_nonce,
                credential=self.credential,
            )
        challenge = auth.issue_preauth("192.0.2.30")
        new_session = auth.login(
            source_ip="192.0.2.30",
            cookie_token=challenge.cookie_token,
            form_nonce=challenge.form_nonce,
            credential=rotated,
        )
        auth.authenticate(new_session.token)

    def test_illegal_reload_latches_admission_closed_without_auto_recovery(
        self,
    ) -> None:
        auth = self.auth()
        session = self.login(auth)
        self.credential_path.chmod(0o644)
        with self.assertRaises(AdmissionClosed):
            auth.authenticate(session.token)
        self.assertFalse(auth.admission_open)
        self.credential_path.chmod(0o600)
        with self.assertRaises(AdmissionClosed):
            auth.issue_preauth("192.0.2.10")
        self.assertEqual(auth.state_counts().sessions, 0)

    def test_rate_counters_survive_legal_rotation(self) -> None:
        auth = self.auth()
        source = "192.0.2.10"
        challenges = [auth.issue_preauth(source) for _ in range(6)]
        for challenge in challenges[:4]:
            with self.assertRaises(LoginRejected):
                auth.login(
                    source_ip=source,
                    cookie_token=challenge.cookie_token,
                    form_nonce=challenge.form_nonce,
                    credential=secrets.token_urlsafe(32),
                )
        replacement = self.root / "replacement"
        self.write_credential(self.credential, path=replacement)
        replacement.replace(self.credential_path)
        auth.refresh_credential()
        fresh = [auth.issue_preauth(source) for _ in range(2)]
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip=source,
                cookie_token=fresh[0].cookie_token,
                form_nonce=fresh[0].form_nonce,
                credential=secrets.token_urlsafe(32),
            )
        with self.assertRaises(LoginRejected):
            auth.login(
                source_ip=source,
                cookie_token=fresh[1].cookie_token,
                form_nonce=fresh[1].form_nonce,
                credential=self.credential,
            )
        self.assertEqual(auth.state_counts().global_failures, 5)

    def test_restart_process_local_state_rejects_prior_session_and_preauth(self) -> None:
        first = self.auth()
        session = self.login(first)
        challenge = first.issue_preauth("192.0.2.10")
        second = self.auth()
        with self.assertRaises(SessionRejected):
            second.authenticate(session.token)
        with self.assertRaises(LoginRejected):
            second.login(
                source_ip="192.0.2.10",
                cookie_token=challenge.cookie_token,
                form_nonce=challenge.form_nonce,
                credential=self.credential,
            )

    def test_sessions_survive_inactivity_and_continued_use_without_time_expiry(
        self,
    ) -> None:
        auth = self.auth()
        idle = self.login(auth)
        active = self.login(auth)
        for _ in range(13):
            self.clock.advance(60 * 60)
            self.assertEqual(
                auth.authenticate(active.token).log_handle, active.log_handle
            )
        self.assertEqual(auth.authenticate(idle.token).log_handle, idle.log_handle)

        self.clock.advance(30 * 24 * 60 * 60)
        self.assertEqual(auth.state_counts().sessions, 2)
        for session in (idle, active):
            with self.subTest(session=session.log_handle):
                authenticated = auth.authenticate(session.token)
                self.assertEqual(authenticated.log_handle, session.log_handle)
                self.assertEqual(authenticated.generation, session.generation)
        target = ExactTarget("binding", "binding-1")
        grant = auth.issue_action(
            idle.token,
            action_kind="binding.rename",
            target=target,
            preconditions=self.preconditions(),
        )
        redeemed = auth.redeem_action(
            idle.token,
            csrf_token=grant.csrf_token,
            action_token=grant.action_token,
            action_kind="binding.rename",
            target=target,
        )
        self.assertEqual(redeemed.session_log_handle, idle.log_handle)

    def test_logout_revokes_session_and_its_actions_only(self) -> None:
        auth = self.auth()
        first = self.login(auth)
        second = self.login(auth)
        target = ExactTarget("binding", "binding-2")
        grant = auth.issue_action(
            second.token,
            action_kind="binding.rename",
            target=target,
            preconditions=self.preconditions(),
        )
        self.assertTrue(auth.logout(second.token))
        self.assertFalse(auth.logout(second.token))
        with self.assertRaises(SessionRejected):
            auth.authenticate(second.token)
        with self.assertRaises(SessionRejected):
            auth.redeem_action(
                second.token,
                csrf_token=grant.csrf_token,
                action_token=grant.action_token,
                action_kind="binding.rename",
                target=target,
            )
        self.assertEqual(auth.state_counts().actions, 0)
        self.assertEqual(auth.authenticate(first.token).log_handle, first.log_handle)
        self.assertNotIn(grant.action_token, repr(auth))

    def test_preauth_capacity_replaces_oldest_with_source_priority(self) -> None:
        for capacity, new_source, evicted_index in (
            (4, "192.0.2.2", 1),  # Only the source is full.
            (3, "192.0.2.2", 1),  # Both full: preserve the older other source.
            (3, "192.0.2.3", 0),  # Only global capacity is full.
        ):
            with self.subTest(capacity=capacity, new_source=new_source):
                auth = self.auth(limits=AuthLimits(
                    max_preauth=capacity, max_preauth_per_source=2,
                ))
                session = self.login(auth)
                target = ExactTarget("binding", "binding-1")
                grant = auth.issue_action(
                    session.token,
                    action_kind="binding.stop",
                    target=target,
                    preconditions=self.preconditions(),
                )
                challenges = [
                    (source, auth.issue_preauth(source))
                    for source in ("192.0.2.1", "192.0.2.2", "192.0.2.2")
                ]
                challenges.append((new_source, auth.issue_preauth(new_source)))
                self.assertEqual(auth.state_counts().preauth, 3)
                for index, (source, challenge) in enumerate(challenges):
                    login = dict(
                        source_ip=source,
                        cookie_token=challenge.cookie_token,
                        form_nonce=challenge.form_nonce,
                        credential=self.credential,
                    )
                    if index == evicted_index:
                        with self.assertRaises(LoginRejected):
                            auth.login(**login)
                    else:
                        auth.authenticate(auth.login(**login).token)
                self.assertEqual(auth.state_counts().preauth, 0)
                # Unauthenticated challenge issuance cannot revoke a session/action.
                auth.authenticate(session.token)
                auth.redeem_action(
                    session.token,
                    csrf_token=grant.csrf_token,
                    action_token=grant.action_token,
                    action_kind="binding.stop",
                    target=target,
                )

    def test_rate_limited_preauth_issuance_does_not_evict_pending_challenge(self) -> None:
        auth = self.auth(limits=AuthLimits(max_preauth=1, max_preauth_per_source=1))
        source = "192.0.2.10"
        challenge = auth.issue_preauth(source)
        for _ in range(5):
            with self.assertRaises(LoginRejected):
                auth.login(
                    source_ip=source,
                    cookie_token=None,
                    form_nonce=None,
                    credential=self.credential,
                )
        with self.assertRaises(LoginRejected):
            auth.issue_preauth(source)
        self.assertEqual(auth.state_counts().preauth, 1)
        self.clock.advance(5 * 60)
        session = auth.login(
            source_ip=source,
            cookie_token=challenge.cookie_token,
            form_nonce=challenge.form_nonce,
            credential=self.credential,
        )
        auth.authenticate(session.token)

    def test_action_capacity_is_strict_and_ttl_reclaims(
        self,
    ) -> None:
        limits = AuthLimits(
            action_ttl=10,
            max_sessions=2,
            max_sessions_per_source=1,
            max_actions=2,
            max_actions_per_session=1,
        )
        auth = self.auth(limits=limits)
        first = self.login(auth, source="192.0.2.10")
        second = self.login(auth, source="192.0.2.11")
        auth.issue_action(
            first.token,
            action_kind="binding.stop",
            target=ExactTarget("binding", "binding-1"),
            preconditions=self.preconditions(),
        )
        with self.assertRaises(AuthCapacityExceeded):
            auth.issue_action(
                first.token,
                action_kind="binding.release",
                target=ExactTarget("binding", "binding-1"),
                preconditions=self.preconditions(),
            )
        auth.issue_action(
            second.token,
            action_kind="binding.stop",
            target=ExactTarget("binding", "binding-2"),
            preconditions=self.preconditions(),
        )
        self.clock.advance(10)
        auth.issue_action(
            first.token,
            action_kind="binding.release",
            target=ExactTarget("binding", "binding-1"),
            preconditions=self.preconditions(),
        )

    def test_verified_login_replaces_oldest_session_at_source_or_global_capacity(
        self,
    ) -> None:
        auth = self.auth(limits=AuthLimits(max_sessions=3, max_sessions_per_source=2))
        other_source = self.login(auth, source="192.0.2.11")
        oldest_at_source = self.login(auth)
        retained = self.login(auth)
        for session in (other_source, oldest_at_source):
            auth.issue_action(
                session.token,
                action_kind="binding.stop",
                target=ExactTarget("binding", "binding-1"),
                preconditions=self.preconditions(),
            )

        replacement = self.login(auth)
        with self.assertRaises(SessionRejected):
            auth.authenticate(oldest_at_source.token)
        self.assertEqual(
            auth.authenticate(other_source.token).log_handle, other_source.log_handle
        )
        self.assertEqual(auth.state_counts().sessions, 3)
        self.assertEqual(auth.state_counts().actions, 1)

        newest = self.login(auth, source="192.0.2.12")
        with self.assertRaises(SessionRejected):
            auth.authenticate(other_source.token)
        for session in (retained, replacement, newest):
            self.assertEqual(
                auth.authenticate(session.token).log_handle, session.log_handle
            )
        self.assertEqual(auth.state_counts().sessions, 3)
        self.assertEqual(auth.state_counts().actions, 0)

    def test_failed_logins_at_capacity_preserve_existing_session_and_actions(
        self,
    ) -> None:
        limits = AuthLimits(max_sessions=1, max_sessions_per_source=1)
        auth = self.auth(limits=limits)
        session = self.login(auth)
        target = ExactTarget("binding", "binding-1")
        grant = auth.issue_action(
            session.token,
            action_kind="binding.stop",
            target=target,
            preconditions=self.preconditions(),
        )
        errors: list[tuple[type[BaseException], str]] = []
        for invalid_field in ("credential", "nonce"):
            challenge = auth.issue_preauth("192.0.2.11")
            with self.assertRaises(LoginRejected) as caught:
                auth.login(
                    source_ip="192.0.2.11",
                    cookie_token=challenge.cookie_token,
                    form_nonce=(
                        secrets.token_urlsafe(32) if invalid_field == "nonce"
                        else challenge.form_nonce
                    ),
                    credential=(
                        secrets.token_urlsafe(32) if invalid_field == "credential"
                        else self.credential
                    ),
                )
            errors.append((type(caught.exception), str(caught.exception)))
        self.assertEqual(errors[0], errors[1])
        self.assertEqual(auth.state_counts().global_failures, 2)
        for _ in range(3):
            with self.assertRaises(LoginRejected):
                self.login(
                    auth, source="192.0.2.11", credential=secrets.token_urlsafe(32)
                )
        # Even correct credentials cannot evict a session while rate-limited.
        with self.assertRaises(LoginRejected):
            self.login(auth, source="192.0.2.11")
        self.assertEqual(auth.authenticate(session.token).log_handle, session.log_handle)
        redeemed = auth.redeem_action(
            session.token,
            csrf_token=grant.csrf_token,
            action_token=grant.action_token,
            action_kind="binding.stop",
            target=target,
        )
        self.assertEqual(redeemed.session_log_handle, session.log_handle)

    def test_action_binds_session_kind_target_generation_and_exact_preconditions(
        self,
    ) -> None:
        auth = self.auth()
        first = self.login(auth, source="192.0.2.10")
        second = self.login(auth, source="192.0.2.11")
        target = ExactTarget("binding", "binding-1", "chat:1")
        preconditions = self.preconditions()
        grant = auth.issue_action(
            first.token,
            action_kind="binding.stop",
            target=target,
            preconditions=preconditions,
        )

        with self.assertRaises(StaleActionGrant):
            auth.redeem_action(
                second.token,
                csrf_token=grant.csrf_token,
                action_token=grant.action_token,
                action_kind="binding.stop",
                target=target,
            )
        with self.assertRaises(StaleActionGrant):
            auth.redeem_action(
                first.token,
                csrf_token=grant.csrf_token,
                action_token=grant.action_token,
                action_kind="binding.release",
                target=target,
            )
        with self.assertRaises(StaleActionGrant):
            auth.redeem_action(
                first.token,
                csrf_token=grant.csrf_token,
                action_token=grant.action_token,
                action_kind="binding.stop",
                target=ExactTarget("binding", "binding-2", "chat:1"),
            )
        redeemed = auth.redeem_action(
            first.token,
            csrf_token=grant.csrf_token,
            action_token=grant.action_token,
            action_kind="binding.stop",
            target=target,
        )
        self.assertEqual(redeemed.target, target)
        self.assertEqual(redeemed.preconditions, preconditions)
        self.assertIs(
            redeemed.preconditions.active_pointer.mode,
            ExpectationMode.EXPECT_NONE,
        )
        self.assertEqual(redeemed.preconditions.runtime_revision.value, 11)
        self.assertEqual(
            redeemed.preconditions.physical_turn_id.value,
            "turn-physical-1",
        )
        with self.assertRaises(ConsumedActionGrant):
            auth.redeem_action(
                first.token,
                csrf_token=grant.csrf_token,
                action_token=grant.action_token,
                action_kind="binding.stop",
                target=target,
            )

    def test_action_has_malformed_csrf_stale_expired_and_consumed_outcomes(
        self,
    ) -> None:
        limits = AuthLimits(action_ttl=10)
        auth = self.auth(limits=limits)
        session = self.login(auth)
        target = ExactTarget("binding", "binding-1")
        grant = auth.issue_action(
            session.token,
            action_kind="binding.rename",
            target=target,
            preconditions=self.preconditions(),
        )
        with self.assertRaises(MalformedActionGrant):
            auth.redeem_action(
                session.token,
                csrf_token=grant.csrf_token,
                action_token="not-a-token",
                action_kind="binding.rename",
                target=target,
            )
        with self.assertRaises(ActionCsrfRejected):
            auth.redeem_action(
                session.token,
                csrf_token="not-a-token",
                action_token=grant.action_token,
                action_kind="binding.rename",
                target=target,
            )
        with self.assertRaises(StaleActionGrant):
            auth.redeem_action(
                session.token,
                csrf_token=grant.csrf_token,
                action_token=secrets.token_urlsafe(32),
                action_kind="binding.rename",
                target=target,
            )
        self.clock.advance(10)
        with self.assertRaises(StaleActionGrant):
            auth.redeem_action(
                session.token,
                csrf_token=grant.csrf_token,
                action_token=grant.action_token,
                action_kind="binding.rename",
                target=target,
            )

    def test_concurrent_action_redemption_has_exactly_one_winner(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        target = ExactTarget("side", "side-1")
        grant = auth.issue_action(
            session.token,
            action_kind="side.close",
            target=target,
            preconditions=self.preconditions(),
        )
        barrier = threading.Barrier(3)

        def redeem() -> str:
            barrier.wait()
            try:
                auth.redeem_action(
                    session.token,
                    csrf_token=grant.csrf_token,
                    action_token=grant.action_token,
                    action_kind="side.close",
                    target=target,
                )
            except ConsumedActionGrant:
                return "consumed"
            return "won"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(redeem) for _ in range(2)]
            barrier.wait()
            outcomes = [future.result(timeout=5) for future in futures]
        self.assertCountEqual(outcomes, ["won", "consumed"])

    def test_action_payload_must_be_typed_and_deeply_immutable(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        for target in (
            "binding-1",
            MutableTarget("binding-1"),
            ShallowFrozenMutableTarget(["binding-1"]),
        ):
            with self.subTest(target=type(target).__name__):
                with self.assertRaises(InvalidActionPayload):
                    auth.issue_action(
                        session.token,
                        action_kind="binding.stop",
                        target=target,
                        preconditions=self.preconditions(),
                    )

    def test_expected_none_is_distinct_from_dont_check(self) -> None:
        expect_none = ExpectedValue[str].expect_none()
        dont_check = ExpectedValue[str].dont_check()
        self.assertNotEqual(expect_none, dont_check)
        self.assertIs(expect_none.mode, ExpectationMode.EXPECT_NONE)
        self.assertIs(dont_check.mode, ExpectationMode.DONT_CHECK)
        with self.assertRaisesRegex(ValueError, "ExpectationMode"):
            ExpectedValue("expect_none")  # type: ignore[arg-type]

    def test_raw_credentials_and_tokens_never_appear_in_repr_or_errors(self) -> None:
        auth = self.auth()
        snapshot = load_credential_snapshot(self.credential_path)
        challenge = auth.issue_preauth("192.0.2.10")
        session = auth.login(
            source_ip="192.0.2.10",
            cookie_token=challenge.cookie_token,
            form_nonce=challenge.form_nonce,
            credential=self.credential,
        )
        target = ExactTarget("binding", "binding-1")
        grant = auth.issue_action(
            session.token,
            action_kind="binding.stop",
            target=target,
            preconditions=self.preconditions(),
        )
        representations = "\n".join(
            (
                repr(auth),
                repr(snapshot),
                repr(challenge),
                repr(session),
                repr(grant),
                repr(auth._preauth),
                repr(auth._sessions),
                repr(auth._actions),
            )
        )
        for secret in (
            self.credential,
            challenge.cookie_token,
            challenge.form_nonce,
            session.token,
            grant.csrf_token,
            grant.action_token,
        ):
            self.assertNotIn(secret, representations)

        raw = secrets.token_urlsafe(32)
        with self.assertRaises(LoginRejected) as caught:
            second = auth.issue_preauth("192.0.2.10")
            auth.login(
                source_ip="192.0.2.10",
                cookie_token=second.cookie_token,
                form_nonce=second.form_nonce,
                credential=raw,
            )
        self.assertNotIn(raw, repr(caught.exception))
        self.assertNotIn(raw, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
