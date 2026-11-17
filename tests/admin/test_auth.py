from __future__ import annotations

import random
import secrets
import tempfile
import threading
import unittest
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from netizen_cli.admin.auth import (
    ActionCsrfRejected, AdmissionClosed, AdminAuth, AuthCapacityExceeded, AuthLimits,
    ConsumedActionGrant, ExpectationMode, ExpectedValue, InvalidActionPayload,
    IssuedActionGrant, IssuedSession,
    LoginRejected, MalformedActionGrant, SessionRejected, StaleActionGrant,
    _new_token, _token_digest, normalize_source_ip,
)
from netizen_cli.admin.users import (
    AdminIdentity, AdminUsers, AdminUsersUnavailable,
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
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.users = AdminUsers(self.root, "cli_test")
        self.member = self.users.add_initial("ou_alice")
        self.identity = self.member.identity
        self.clock = FakeClock()

    def auth(self, *, limits: AuthLimits | None = None) -> AdminAuth:
        return AdminAuth(self.users, clock=self.clock, limits=limits)

    def login(self, auth: AdminAuth, *, source: str = "192.0.2.10"):
        challenge = auth.issue_preauth(source)
        auth.consume_preauth(source_ip=source, cookie_token=challenge.cookie_token,
                             form_nonce=challenge.form_nonce)
        return auth.issue_session(self.identity, source_ip=source,
                                  expected_add_revision=self.member.add_revision)

    def preconditions(self, *, active: ExpectedValue[str] | None = None) -> ExactPreconditions:
        return ExactPreconditions(
            active_pointer=ExpectedValue.expect_none() if active is None else active,
            settings_revision=ExpectedValue.expect(7),
            runtime_revision=ExpectedValue.expect(11),
            physical_turn_id=ExpectedValue.expect("turn-physical-1"),
        )

    def assert_action_index_consistent(self, auth: AdminAuth) -> None:
        expected: dict[bytes, set[bytes]] = {}
        for digest, record in auth._actions.items():
            self.assertIn(record.session_digest, auth._sessions)
            expected.setdefault(record.session_digest, set()).add(digest)
        self.assertEqual(auth._actions_by_session, expected)
        expiries = [record.expires_at for record in auth._actions.values()]
        self.assertEqual(expiries, sorted(expiries))

    def issue_test_action(self, auth: AdminAuth, session: IssuedSession) -> IssuedActionGrant:
        return auth.issue_action(
            session.token, action_kind="test", target=ExactTarget("test", "one"),
            preconditions=self.preconditions(),
        )

    def test_batch_actions_preserve_order_facts_ttl_and_single_use(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        requests = tuple(
            (f"test.{index}", ExactTarget("test", str(index)), self.preconditions())
            for index in range(3)
        )
        with (
            patch.object(auth, "_prune_locked", wraps=auth._prune_locked) as prune,
            patch.object(auth, "_require_admission_locked", wraps=auth._require_admission_locked) as admission,
            patch.object(auth, "_authenticate_session_locked", wraps=auth._authenticate_session_locked) as authenticate,
        ):
            issued = auth.issue_actions(session.token, requests)
        prune.assert_called_once()
        admission.assert_called_once()
        authenticate.assert_called_once_with(session.token)
        self.assertIsInstance(issued, tuple)
        self.assertEqual(len(issued), len(requests))
        for grant, (kind, target, preconditions) in zip(issued, requests, strict=True):
            self.assertEqual(grant.expires_at, self.clock.now + auth._limits.action_ttl)
            redeemed = auth.redeem_action(
                session.token, csrf_token=grant.csrf_token, action_token=grant.action_token,
                action_kind=kind, target=target,
            )
            self.assertEqual(redeemed.preconditions, preconditions)
            with self.assertRaises(ConsumedActionGrant):
                auth.redeem_action(
                    session.token, csrf_token=grant.csrf_token, action_token=grant.action_token,
                    action_kind=kind, target=target,
                )
        self.assert_action_index_consistent(auth)

    def test_batch_capacity_rejection_writes_neither_table_nor_index(self) -> None:
        for global_limit, session_limit in ((4, 4), (10, 2)):
            with self.subTest(global_limit=global_limit, session_limit=session_limit):
                auth = self.auth(limits=AuthLimits(
                    max_actions=global_limit, max_actions_per_session=session_limit,
                ))
                session, other = self.login(auth), self.login(auth)
                self.issue_test_action(auth, other)
                self.issue_test_action(auth, other)
                self.issue_test_action(auth, session)
                before = dict(auth._actions)
                index_before = {key: set(value) for key, value in auth._actions_by_session.items()}
                request = ("test", ExactTarget("test", "new"), self.preconditions())
                with self.assertRaises(AuthCapacityExceeded):
                    auth.issue_actions(session.token, (request, request))
                self.assertEqual(auth._actions, before)
                self.assertEqual(auth._actions_by_session, index_before)
                self.assert_action_index_consistent(auth)
                # A failed batch leaves its remaining one-grant capacity usable.
                self.assertEqual(len(auth.issue_actions(session.token, (request,))), 1)

    def test_batch_validates_every_kind_and_payload_before_writing(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        self.issue_test_action(auth, session)
        valid = ("test", ExactTarget("test", "one"), self.preconditions())
        for invalid, error in (
            (("", valid[1], valid[2]), ValueError),
            (("test", MutableTarget("two"), valid[2]), InvalidActionPayload),
            (("test", valid[1], ShallowFrozenMutableTarget([])), InvalidActionPayload),
        ):
            with self.subTest(invalid=invalid):
                before = dict(auth._actions)
                index_before = {key: set(value) for key, value in auth._actions_by_session.items()}
                with self.assertRaises(error):
                    auth.issue_actions(session.token, (valid, invalid, valid))
                self.assertEqual(auth._actions, before)
                self.assertEqual(auth._actions_by_session, index_before)

    def test_batch_tokens_retry_existing_and_in_batch_collisions(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        existing = self.issue_test_action(auth, session)
        first, second = _new_token(), _new_token()
        request = ("test", ExactTarget("test", "one"), self.preconditions())
        with patch("netizen_cli.admin.auth._new_token", side_effect=(
            (existing.action_token, _token_digest(existing.action_token)),
            first, _new_token(), first, second, _new_token(),
        )):
            issued = auth.issue_actions(session.token, (request, request))
        self.assertEqual([item.action_token for item in issued], [first[0], second[0]])
        self.assertEqual(len({existing.action_token, *(item.action_token for item in issued)}), 3)
        self.assert_action_index_consistent(auth)

    def test_batch_token_generation_failure_leaves_no_partial_grants(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        first = _new_token()
        request = ("test", ExactTarget("test", "one"), self.preconditions())
        with patch("netizen_cli.admin.auth._new_token", side_effect=(
            first, _new_token(), *(first for _ in range(8)),
        )), self.assertRaisesRegex(RuntimeError, "collided"):
            auth.issue_actions(session.token, (request, request))
        self.assertEqual(auth._actions, {})
        self.assertEqual(auth._actions_by_session, {})

    def test_empty_batch_requires_valid_session_even_at_capacity(self) -> None:
        auth = self.auth(limits=AuthLimits(max_actions=1, max_actions_per_session=1))
        session = self.login(auth)
        self.issue_test_action(auth, session)
        self.assertEqual(auth.issue_actions(session.token, ()), ())
        self.assertEqual(auth.state_counts().actions, 1)
        with self.assertRaises(SessionRejected):
            auth.issue_actions(None, ())
        auth.logout(session.token)
        with self.assertRaises(SessionRejected):
            auth.issue_actions(session.token, ())
        auth.close()
        with self.assertRaises(AdmissionClosed):
            auth.issue_actions(session.token, ())

    def test_concurrent_batches_reserve_capacity_as_a_whole(self) -> None:
        auth = self.auth(limits=AuthLimits(max_actions=3, max_actions_per_session=3))
        session = self.login(auth)
        ready = threading.Barrier(2)
        request = ("test", ExactTarget("test", "one"), self.preconditions())

        def issue():
            ready.wait(timeout=5)
            try:
                return len(auth.issue_actions(session.token, (request, request)))
            except AuthCapacityExceeded:
                return 0

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(issue) for _ in range(2)]
            self.assertEqual(sorted(future.result(timeout=5) for future in futures), [0, 2])
        self.assertEqual(auth.state_counts().actions, 2)
        self.assert_action_index_consistent(auth)

    def test_preauth_nonce_is_source_bound_and_single_use(self) -> None:
        auth = self.auth()
        challenge = auth.issue_preauth("192.0.2.10")
        for source in ("192.0.2.11", "192.0.2.10"):
            with self.assertRaises(LoginRejected):
                auth.consume_preauth(source_ip=source, cookie_token=challenge.cookie_token,
                                     form_nonce=challenge.form_nonce)
        challenge = auth.issue_preauth("192.0.2.10")
        for nonce in (secrets.token_urlsafe(32), challenge.form_nonce):
            with self.assertRaises(LoginRejected):
                auth.consume_preauth(source_ip="192.0.2.10", cookie_token=challenge.cookie_token,
                                     form_nonce=nonce)

    def test_preauth_survives_time_and_cannot_replay(self) -> None:
        auth = self.auth()
        challenge = auth.issue_preauth("192.0.2.10")
        self.clock.advance(30 * 24 * 60 * 60)
        auth.consume_preauth(source_ip="192.0.2.10", cookie_token=challenge.cookie_token,
                             form_nonce=challenge.form_nonce)
        with self.assertRaises(LoginRejected):
            auth.consume_preauth(source_ip="192.0.2.10", cookie_token=challenge.cookie_token,
                                 form_nonce=challenge.form_nonce)

    def test_exact_sliding_window_source_and_global_limits(self) -> None:
        auth = self.auth()
        for index in range(5):
            with self.assertRaises(LoginRejected):
                auth.reject_login("192.0.2.10" if index % 2 else "::ffff:192.0.2.10")
        with self.assertRaises(LoginRejected):
            self.login(auth)
        self.assertEqual(auth.state_counts().global_failures, 5)
        for source in ("192.0.2.11", "192.0.2.12", "192.0.2.13"):
            for _ in range(5):
                with self.assertRaises(LoginRejected):
                    auth.reject_login(source)
        with self.assertRaises(LoginRejected):
            self.login(auth, source="192.0.2.100")
        self.assertEqual(auth.state_counts().global_failures, 20)
        self.clock.advance(5 * 60)
        auth.authenticate(self.login(auth).token)
        self.assertEqual(normalize_source_ip("::ffff:192.0.2.10"), "192.0.2.10")

    def test_rate_sources_are_bounded_without_eviction(self) -> None:
        auth = self.auth(limits=AuthLimits(max_rate_sources=2))
        for source in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
            with self.assertRaises(LoginRejected):
                auth.reject_login(source)
        self.assertEqual(auth.state_counts().rate_sources, 2)
        with self.assertRaises(LoginRejected):
            self.login(auth, source="192.0.2.3")
        self.clock.advance(5 * 60)
        auth.authenticate(self.login(auth, source="192.0.2.3").token)

    def test_preauth_capacity_reclaims_source_first_and_preserves_sessions(self) -> None:
        for capacity, new_source, evicted in ((4, "192.0.2.2", 1), (3, "192.0.2.2", 1), (3, "192.0.2.3", 0)):
            with self.subTest(capacity=capacity, new_source=new_source):
                auth = self.auth(limits=AuthLimits(max_preauth=capacity, max_preauth_per_source=2))
                session = self.login(auth)
                grant = auth.issue_action(session.token, action_kind="test",
                                          target=ExactTarget("test", "one"),
                                          preconditions=self.preconditions())
                challenges = [(source, auth.issue_preauth(source))
                              for source in ("192.0.2.1", "192.0.2.2", "192.0.2.2", new_source)]
                for index, (source, challenge) in enumerate(challenges):
                    arguments = dict(source_ip=source, cookie_token=challenge.cookie_token,
                                     form_nonce=challenge.form_nonce)
                    if index == evicted:
                        with self.assertRaises(LoginRejected):
                            auth.consume_preauth(**arguments)
                    else:
                        auth.consume_preauth(**arguments)
                self.assertEqual(auth.state_counts().preauth, 0)
                auth.redeem_action(session.token, action_kind="test", target=ExactTarget("test", "one"),
                                   csrf_token=grant.csrf_token, action_token=grant.action_token)

    def test_rate_limited_preauth_does_not_evict_or_extend_failures(self) -> None:
        auth = self.auth(limits=AuthLimits(max_preauth=1, max_preauth_per_source=1))
        challenge = auth.issue_preauth("192.0.2.10")
        for _ in range(5):
            with self.assertRaises(LoginRejected):
                auth.reject_login("192.0.2.10")
        for _ in range(5):
            with self.assertRaises(LoginRejected):
                auth.issue_preauth("192.0.2.10")
        self.assertEqual(auth.state_counts().preauth, 1)
        self.assertEqual(auth.state_counts().global_failures, 5)
        self.clock.advance(300)
        auth.consume_preauth(source_ip="192.0.2.10", cookie_token=challenge.cookie_token,
                             form_nonce=challenge.form_nonce)

    def test_failed_identity_does_not_evict_sessions_or_grants(self) -> None:
        auth = self.auth(limits=AuthLimits(max_sessions=1, max_sessions_per_source=1))
        session = self.login(auth)
        auth.issue_action(session.token, action_kind="test", target=ExactTarget("test", "one"),
                          preconditions=self.preconditions())
        for identity, revision in ((AdminIdentity("cli_test", "ou_other"), 1),
                                   (AdminIdentity("cli_other", "ou_alice"), 1),
                                   (self.identity, 99)):
            with self.assertRaises(LoginRejected):
                auth.issue_session(identity, source_ip="192.0.2.11",
                                   expected_add_revision=revision)
        self.assertEqual(auth.state_counts().sessions, 1)
        self.assertEqual(auth.state_counts().actions, 1)
        auth.authenticate(session.token)

    def test_login_commit_precheck_neither_allocates_nor_evicts_sessions(self) -> None:
        auth = self.auth(limits=AuthLimits(max_sessions=1, max_sessions_per_source=1))
        existing = self.login(auth)
        before = auth.state_counts()
        auth.precheck_for_session(self.identity, source_ip="192.0.2.10",
                                  expected_add_revision=self.member.add_revision)
        with self.assertRaises(LoginRejected):
            auth.precheck_for_session(self.identity, source_ip="192.0.2.10",
                                      expected_add_revision=self.member.add_revision + 1)
        self.assertEqual(auth.state_counts(), before)
        auth.authenticate(existing.token)

    def test_delete_revokes_all_user_sessions_and_grants_and_readd_does_not_revive(self) -> None:
        auth = self.auth()
        first, second = self.login(auth), self.login(auth, source="192.0.2.11")
        bob = self.users.add(AdminIdentity("cli_test", "ou_bob"), actor=self.identity,
                             expected_revision=self.users.revision)
        other = auth.issue_session(bob.identity, source_ip="192.0.2.12",
                                   expected_add_revision=bob.add_revision)
        for session in (first, second):
            auth.issue_action(session.token, action_kind="test", target=ExactTarget("test", "one"),
                              preconditions=self.preconditions())
        self.users.remove(self.identity, actor=bob.identity, expected_revision=self.users.revision)
        self.assertEqual(auth.state_counts().sessions, 1)
        self.assertEqual(auth.state_counts().actions, 0)
        self.assert_action_index_consistent(auth)
        replacement = self.users.add(self.identity, actor=bob.identity, expected_revision=self.users.revision)
        for session in (first, second):
            with self.assertRaises(SessionRejected):
                auth.authenticate(session.token)
        with self.assertRaises(LoginRejected):
            auth.issue_session(self.identity, source_ip="192.0.2.10",
                               expected_add_revision=self.member.add_revision)
        new = auth.issue_session(self.identity, source_ip="192.0.2.10",
                                 expected_add_revision=replacement.add_revision)
        self.assertEqual(auth.authenticate(new.token).identity, self.identity)
        auth.authenticate(other.token)

    def test_authenticate_and_redeem_check_current_membership(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        grant = auth.issue_action(session.token, action_kind="test", target=ExactTarget("test", "one"),
                                  preconditions=self.preconditions())
        # Admission must independently recheck even if a notification is missed.
        with patch.object(self.users, "get_membership", return_value=None):
            with self.assertRaises(SessionRejected):
                auth.redeem_action(session.token, action_kind="test", target=ExactTarget("test", "one"),
                                   csrf_token=grant.csrf_token, action_token=grant.action_token)
        self.assertEqual(auth.state_counts().actions, 0)
        self.assert_action_index_consistent(auth)

    def test_restart_and_close_discard_local_browser_proofs(self) -> None:
        first = self.auth()
        session = self.login(first)
        first.issue_action(session.token, action_kind="test", target=ExactTarget("test", "one"),
                           preconditions=self.preconditions())
        challenge = first.issue_preauth("192.0.2.10")
        second = self.auth()
        with self.assertRaises(SessionRejected):
            second.authenticate(session.token)
        with self.assertRaises(LoginRejected):
            second.consume_preauth(source_ip="192.0.2.10", cookie_token=challenge.cookie_token,
                                   form_nonce=challenge.form_nonce)
        first.close()
        with self.assertRaises(AdmissionClosed):
            first.authenticate(session.token)
        self.assertEqual(first.state_counts().sessions, 0)
        self.assertEqual(first.state_counts().actions, 0)
        self.assert_action_index_consistent(first)

    def test_failed_store_write_revokes_and_latches_auth_admission_closed(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        auth.issue_action(session.token, action_kind="test", target=ExactTarget("test", "one"),
                          preconditions=self.preconditions())
        self.users.path.chmod(0o644)
        with self.assertRaises(AdminUsersUnavailable):
            self.users.update_profile(self.identity, name="Alice", avatar_url=None)
        with self.assertRaises(AdmissionClosed):
            auth.authenticate(session.token)
        self.assertEqual(auth.state_counts().sessions, 0)
        self.assertEqual(auth.state_counts().actions, 0)
        self.assert_action_index_consistent(auth)

    def test_bearer_tokens_never_appear_in_repr_or_errors(self) -> None:
        auth = self.auth()
        challenge = auth.issue_preauth("192.0.2.10")
        session = self.login(auth)
        grant = auth.issue_action(session.token, action_kind="test", target=ExactTarget("test", "one"),
                                  preconditions=self.preconditions())
        representations = repr((auth, challenge, session, grant, auth._preauth, auth._sessions, auth._actions))
        for token in (challenge.cookie_token, challenge.form_nonce, session.token,
                      grant.csrf_token, grant.action_token):
            self.assertNotIn(token, representations)
        with self.assertRaises(SessionRejected) as caught:
            auth.authenticate(secrets.token_urlsafe(32))
        self.assertEqual(str(caught.exception), "admin session rejected")

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

    def test_action_capacity_rejection_leaves_table_and_index_unchanged(self) -> None:
        auth = self.auth(limits=AuthLimits(max_actions=3, max_actions_per_session=2))
        first, second = self.login(auth), self.login(auth)
        self.issue_test_action(auth, first)
        self.issue_test_action(auth, first)
        for session in (first, second):
            before = dict(auth._actions)
            index_before = {key: set(value) for key, value in auth._actions_by_session.items()}
            with self.assertRaises(AuthCapacityExceeded):
                self.issue_test_action(auth, session)
            self.assertEqual(auth._actions, before)
            self.assertEqual(auth._actions_by_session, index_before)
            self.assert_action_index_consistent(auth)
            if session is first:
                # A full session does not consume another session's allowance.
                self.issue_test_action(auth, second)

    def test_action_expiry_reclaims_only_expired_session_and_global_capacity(self) -> None:
        auth = self.auth(limits=AuthLimits(
            action_ttl=10, max_actions=5, max_actions_per_session=3,
        ))
        first, second = self.login(auth), self.login(auth)
        for _ in range(3):
            self.issue_test_action(auth, first)
        expired = set(auth._actions)
        self.clock.advance(5)
        for _ in range(2):
            self.issue_test_action(auth, second)
        retained = set(auth._actions) - expired
        self.clock.advance(5)
        # Issuance itself must reclaim capacity, without a diagnostic prune first.
        for _ in range(3):
            self.issue_test_action(auth, first)
        self.assertEqual(auth.state_counts().actions, 5)
        self.assertTrue(expired.isdisjoint(auth._actions))
        self.assertTrue(retained.issubset(auth._actions))
        self.assert_action_index_consistent(auth)
        self.clock.advance(5)
        self.assertEqual(auth.state_counts().actions, 3)
        self.assertNotIn(_token_digest(second.token), auth._actions_by_session)
        self.issue_test_action(auth, second)
        self.assert_action_index_consistent(auth)

    def test_consumed_actions_retain_capacity_and_expiry_order(self) -> None:
        auth = self.auth(limits=AuthLimits(
            action_ttl=10, max_actions=2, max_actions_per_session=1,
        ))
        first, second = self.login(auth), self.login(auth)
        grant = self.issue_test_action(auth, first)
        self.clock.advance(5)
        self.issue_test_action(auth, second)
        order = list(auth._actions)
        arguments = dict(
            csrf_token=grant.csrf_token, action_token=grant.action_token,
            action_kind="test", target=ExactTarget("test", "one"),
        )
        auth.redeem_action(first.token, **arguments)
        self.assertEqual(list(auth._actions), order)
        with self.assertRaises(ConsumedActionGrant):
            auth.redeem_action(first.token, **arguments)
        with self.assertRaises(AuthCapacityExceeded):
            self.issue_test_action(auth, first)
        self.assertTrue(auth.logout(second.token))
        # The consumed grant still occupies the per-session allowance.
        with self.assertRaises(AuthCapacityExceeded):
            self.issue_test_action(auth, first)
        self.assertEqual(auth.state_counts().actions, 1)
        self.assert_action_index_consistent(auth)
        self.clock.advance(5)
        self.issue_test_action(auth, first)
        with self.assertRaises(StaleActionGrant):
            auth.redeem_action(first.token, **arguments)
        self.assert_action_index_consistent(auth)

    def test_session_cleanup_removes_index_entries_and_reclaims_capacity(self) -> None:
        bob = self.users.add(AdminIdentity("cli_test", "ou_bob"), actor=self.identity,
                             expected_revision=self.users.revision)
        for operation in ("logout", "revoke", "source_eviction", "global_eviction", "clear", "close"):
            with self.subTest(operation=operation):
                auth = self.auth(limits=AuthLimits(
                    max_sessions=3, max_sessions_per_source=2,
                    max_actions=6, max_actions_per_session=2,
                ))
                first, second = self.login(auth), self.login(auth)
                other = auth.issue_session(bob.identity, source_ip="192.0.2.11",
                                           expected_add_revision=bob.add_revision)
                for _ in range(2):
                    for session in (first, second, other):
                        self.issue_test_action(auth, session)
                before = dict(auth._actions)
                removed = {_token_digest(first.token)}
                replacement = None
                if operation == "logout":
                    self.assertTrue(auth.logout(first.token))
                elif operation == "revoke":
                    auth.revoke_identity(self.identity)
                    removed.add(_token_digest(second.token))
                elif operation in ("source_eviction", "global_eviction"):
                    replacement = self.login(
                        auth, source="192.0.2.10" if operation == "source_eviction" else "192.0.2.12",
                    )
                elif operation == "clear":
                    with auth._lock:
                        auth._clear_bearer_state_locked()
                    removed = {_token_digest(s.token) for s in (first, second, other)}
                else:
                    auth.close()
                    removed = {_token_digest(s.token) for s in (first, second, other)}
                expected = {key: value for key, value in before.items()
                            if value.session_digest not in removed}
                self.assertEqual(auth._actions, expected)
                self.assertTrue(removed.isdisjoint(auth._actions_by_session))
                self.assert_action_index_consistent(auth)
                self.assertEqual(auth.state_counts().actions, len(expected))
                if operation == "close":
                    with self.assertRaises(AdmissionClosed):
                        self.issue_test_action(auth, first)
                else:
                    with self.assertRaises(SessionRejected):
                        auth.authenticate(first.token)
                    replacement = replacement or self.login(auth)
                    self.issue_test_action(auth, replacement)
                    self.issue_test_action(auth, replacement)
                    self.assert_action_index_consistent(auth)

    def test_action_generation_mismatch_is_stale_without_consuming_grant(self) -> None:
        auth = self.auth()
        session = self.login(auth)
        grant = self.issue_test_action(auth, session)
        record = auth._actions[_token_digest(grant.action_token)]
        record.generation -= 1
        with self.assertRaises(StaleActionGrant):
            auth.redeem_action(
                session.token, csrf_token=grant.csrf_token, action_token=grant.action_token,
                action_kind="test", target=ExactTarget("test", "one"),
            )
        self.assertFalse(record.consumed)
        self.assert_action_index_consistent(auth)

    def test_action_index_stays_consistent_through_random_operations(self) -> None:
        rng = random.Random(462019)
        auth = self.auth(limits=AuthLimits(
            action_ttl=10, max_sessions=6, max_sessions_per_source=2,
            max_actions=12, max_actions_per_session=4,
        ))
        bob = self.users.add(AdminIdentity("cli_test", "ou_bob"), actor=self.identity,
                             expected_revision=self.users.revision)
        members = (self.member, bob)
        sessions = [self.login(auth)]
        grants: list[tuple[IssuedSession, IssuedActionGrant]] = []
        issued: dict[bytes, tuple[bytes, float]] = {}
        consumed: set[bytes] = set()
        for step in range(600):
            operation = rng.choice(("issue", "issue", "redeem", "advance", "login", "logout", "revoke"))
            with self.subTest(step=step, operation=operation):
                active_sessions = [s for s in sessions if _token_digest(s.token) in auth._sessions]
                session = rng.choice(active_sessions if active_sessions and rng.random() < 0.8 else sessions)
                session_digest = _token_digest(session.token)
                if operation == "issue":
                    error = None
                    if session_digest not in auth._sessions:
                        error = SessionRejected
                    elif (len(auth._actions) >= auth._limits.max_actions
                          or sum(s == session_digest for s, _ in issued.values()
                                 if s in auth._sessions) >= auth._limits.max_actions_per_session):
                        error = AuthCapacityExceeded
                    if error is not None:
                        with self.assertRaises(error):
                            self.issue_test_action(auth, session)
                    else:
                        grant = self.issue_test_action(auth, session)
                        grants.append((session, grant))
                        issued[_token_digest(grant.action_token)] = (session_digest, grant.expires_at)
                elif operation == "redeem" and grants:
                    active_grants = [(owner, grant) for owner, grant in grants
                                     if _token_digest(grant.action_token) in issued]
                    owner, grant = rng.choice(active_grants if active_grants and rng.random() < 0.7 else grants)
                    digest = _token_digest(grant.action_token)
                    arguments = dict(
                        csrf_token=grant.csrf_token, action_token=grant.action_token,
                        action_kind="test", target=ExactTarget("test", "one"),
                    )
                    error = None
                    if _token_digest(owner.token) not in auth._sessions:
                        error = SessionRejected
                    elif digest not in issued:
                        error = StaleActionGrant
                    elif digest in consumed:
                        error = ConsumedActionGrant
                    if error is not None:
                        with self.assertRaises(error):
                            auth.redeem_action(owner.token, **arguments)
                    else:
                        auth.redeem_action(owner.token, **arguments)
                        consumed.add(digest)
                elif operation == "advance":
                    self.clock.advance(rng.choice((0, 1, 5, 10, 11)))
                    expired = {key for key, value in auth._actions.items()
                               if value.expires_at <= self.clock.now}
                    before = set(auth._actions)
                    auth.state_counts()
                    self.assertEqual(before - set(auth._actions), expired)
                elif operation == "login":
                    member = rng.choice(members)
                    sessions.append(auth.issue_session(
                        member.identity, source_ip=f"192.0.2.{rng.randrange(10, 14)}",
                        expected_add_revision=member.add_revision,
                    ))
                elif operation == "logout":
                    was_active = session_digest in auth._sessions
                    self.assertEqual(auth.logout(session.token), was_active)
                elif operation == "revoke":
                    auth.revoke_identity(rng.choice(members).identity)
                issued = {key: value for key, value in issued.items()
                          if value[0] in auth._sessions and value[1] > self.clock.now}
                self.assertEqual(set(auth._actions), set(issued))
                self.assertEqual({key for key, value in auth._actions.items() if value.consumed},
                                 consumed.intersection(issued))
                self.assert_action_index_consistent(auth)
                self.assertEqual(auth.state_counts().actions, len(issued))

    def test_action_issue_and_session_drop_do_not_scan_other_sessions_grants(self) -> None:
        class CountingActions(OrderedDict):
            visits = 0

            def __iter__(self):
                for digest in super().__iter__():
                    self.visits += 1
                    yield digest

            def items(self):
                for item in super().items():
                    self.visits += 1
                    yield item

            def values(self):
                for value in super().values():
                    self.visits += 1
                    yield value

            def pop(self, digest, *args):
                self.visits += 1
                return super().pop(digest, *args)

        for size in (8, 1_024):
            with self.subTest(size=size):
                auth = self.auth()
                first, second = self.login(auth), self.login(auth)
                for index in range(size):
                    self.issue_test_action(auth, second)
                    if index < 3:
                        self.issue_test_action(auth, first)
                actions = CountingActions(auth._actions)
                auth._actions = actions
                actions.visits = 0
                self.issue_test_action(auth, second)
                self.assertLessEqual(actions.visits, 2)
                actions.visits = 0
                self.assertTrue(auth.logout(first.token))
                self.assertLessEqual(actions.visits, 4)
                self.assertEqual(len(actions), size + 1)
                self.assert_action_index_consistent(auth)

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


if __name__ == "__main__":
    unittest.main()
