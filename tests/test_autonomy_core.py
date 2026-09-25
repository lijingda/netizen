from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from netizen.autonomy import (
    AutonomyConflict, AutonomyError, AutonomyService, AutonomyStore, Candidate,
    create_schema, require_schema,
)
from netizen.autonomy.config import load_config, parse_config
from netizen.autonomy.provider import SystemOneProvider, encode_request, parse_answer, request_fits


class Provider:
    def __init__(self):
        self.choice = "consume"
        self.requests = []
        self.started = asyncio.Event()
        self.release = None
        self.error = None

    async def decide(self, config, state):
        self.requests.append((config, state))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error:
            raise self.error
        return self.choice


class AutonomyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "decision.json"
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("CREATE TABLE scopes(scope_key TEXT PRIMARY KEY,kind TEXT)")
        self.connection.execute("INSERT INTO scopes VALUES('group','group')")
        self.connection.execute("CREATE TABLE bindings(binding_id TEXT PRIMARY KEY,scope_key TEXT,message_context_mode TEXT)")
        self.connection.executemany("INSERT INTO bindings VALUES(?,'group','current-only')", [("b",), ("other",)])
        create_schema(self.connection)
        self.store = AutonomyStore(self.connection)
        self.provider = Provider()
        self.service = AutonomyService(self.store, self.path, provider=self.provider)
        self.store.set_enabled("b", True)
        self.addCleanup(self.connection.close)

    async def configure(self, **changes):
        payload = {"provider": "laya", "expected_revision": self.service.get_status()["revision"]}
        payload.update(changes)
        return await self.service.configure(payload)

    def accepted(self, text="hello", identifier="a", turn="turn"):
        candidate = Candidate(identifier, text, "Alice")
        decision = self.service.prepare_explicit("b", candidate)
        self.assertTrue(self.service.record_accepted("b", candidate, turn, decision.token))
        return decision

    async def test_missing_config_does_not_break_explicit_input_or_startup(self):
        self.assertFalse(self.service.configured)
        self.assertEqual((await self.service.decide("b", Candidate("x", "hello"), "/tmp")).outcome, "unavailable")
        decision = self.accepted()
        self.assertFalse(self.service.token_current(decision.token))  # accepted anchor advanced
        self.assertEqual(len(self.store.context("b").records), 1)
        self.assertEqual(self.provider.requests, [])

    async def test_config_is_private_redacted_versioned_and_clear_removes_secret(self):
        result = await self.configure(provider="jev", api_key="super-secret")
        self.assertNotIn("super-secret", json.dumps(result))
        self.assertTrue(result["config"]["has_api_key"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(load_config(self.path)[1].api_key, "super-secret")
        with self.assertRaises(AutonomyConflict):
            await self.service.configure({"expected_revision": 0, "clear": True})
        await self.service.configure({"expected_revision": 1, "clear": True})
        self.assertNotIn("super-secret", self.path.read_text())
        self.assertFalse(self.service.configured)
        self.assertEqual(load_config(self.path), (2, None))

    async def test_key_preserved_only_same_destination_and_never_projected(self):
        await self.configure(provider="jev", api_key="private-key")
        await self.service.configure({"expected_revision": 1, "api_key": "", "timeout_seconds": 2})
        self.assertEqual(load_config(self.path)[1].api_key, "private-key")
        with self.assertRaises(AutonomyError):
            await self.service.configure({"expected_revision": 2, "base_url": "https://elsewhere.example"})
        await self.service.configure({"expected_revision": 2, "provider": "laya"})
        self.assertEqual(load_config(self.path)[1].api_key, "")

    async def test_failed_save_does_not_apply_running_configuration(self):
        await self.configure()
        with patch("netizen.autonomy.service.save_config", side_effect=AutonomyError("save failed")):
            with self.assertRaises(AutonomyError):
                await self.service.configure({"expected_revision": 1, "clear": True})
        self.assertTrue(self.service.configured)
        self.assertEqual(self.service.get_status()["revision"], 1)

    async def test_private_config_rejects_symlink_and_world_readable_files(self):
        target = self.path.with_name("target")
        target.write_text("not config")
        self.path.symlink_to(target)
        with self.assertRaises(AutonomyError):
            await self.configure()
        self.assertEqual(target.read_text(), "not config")
        invalid = AutonomyService(self.store, self.path)
        self.assertFalse(invalid.configured)
        self.path.unlink()
        await self.configure()
        os.chmod(self.path, 0o644)
        self.assertFalse(AutonomyService(self.store, self.path).configured)

    async def test_skip_is_transient_and_gap_excludes_current_and_later_receipts(self):
        await self.configure()
        self.accepted()
        self.provider.choice = "skip"
        skipped = await self.service.decide("b", Candidate("skip", "not for agent"), "/tmp")
        self.assertEqual(skipped.outcome, "skip")
        self.assertEqual(len(self.store.context("b").records), 1)
        self.provider.choice = "consume"
        candidate = Candidate("next", "please continue")
        decision = await self.service.decide("b", candidate, "/tmp")
        self.assertIn("1 条", decision.gap_hint)
        self.assertNotIn("not for agent", self.provider.requests[-1][1])
        self.service.prepare_explicit("b", Candidate("later", "later receipt"))
        self.assertIn("1 条", decision.gap_hint)
        self.service.record_accepted("b", candidate, "turn", decision.token)
        self.assertEqual(len(self.store.context("b").records), 2)

    async def test_first_receipt_and_zero_gap_have_no_hint(self):
        await self.configure()
        await self.service.decide("b", Candidate("first", "hey"), "/tmp")
        self.assertIsNone(self.accepted().gap_hint)
        self.assertIsNone(self.service.prepare_explicit("b", Candidate("b", "followup")).gap_hint)

    async def test_consumption_not_recorded_until_native_acceptance(self):
        await self.configure()
        candidate = Candidate("candidate", "do something")
        result = await self.service.decide("b", candidate, "/tmp")
        self.assertEqual(result.outcome, "consume")
        self.assertEqual(self.store.context("b").records, ())
        self.service.mark_unavailable("b", "admission_failed")
        self.assertEqual(self.store.context("b").records, ())
        self.service.record_accepted("b", candidate, "turn", result.token)
        await self.service.record_final("b", "turn", "final @real-user")
        await self.service.record_final("b", "turn", "duplicated delivery")
        self.assertEqual([r.kind for r in self.store.context("b").records], ["user", "final"])
        self.assertEqual(self.store.context("b").records[-1].text, "final @real-user")

    async def test_mode_cycle_preserves_records_but_invalidates_inflight_and_gap(self):
        await self.configure()
        self.accepted()
        self.provider.release = asyncio.Event()
        task = asyncio.create_task(self.service.decide("b", Candidate("waiting", "help"), "/tmp"))
        await self.provider.started.wait()
        self.store.set_enabled("b", False)
        self.store.set_enabled("b", True)
        self.provider.release.set()
        self.assertEqual((await task).outcome, "unavailable")
        self.assertEqual(len(self.store.context("b").records), 1)
        self.assertIsNone(self.service.prepare_explicit("b", Candidate("new", "hi")).gap_hint)

    async def test_configuration_change_keeps_inflight_snapshot_clear_invalidates(self):
        await self.configure()
        self.provider.release = asyncio.Event()
        task = asyncio.create_task(self.service.decide("b", Candidate("waiting", "help"), "/tmp"))
        await self.provider.started.wait()
        await self.configure(base_url="http://localhost:9000")
        self.provider.release.set()
        self.assertEqual((await task).outcome, "consume")
        self.assertEqual(self.provider.requests[0][0].base_url, "http://127.0.0.1:8000")
        self.provider.release.clear()
        self.provider.started.clear()
        task = asyncio.create_task(self.service.decide("b", Candidate("waiting2", "help"), "/tmp"))
        await self.provider.started.wait()
        await self.service.configure({"expected_revision": 2, "clear": True})
        await self.configure()
        self.provider.release.set()
        self.assertEqual((await task).outcome, "unavailable")

    async def test_timeout_and_invalid_choice_are_unavailable_not_skip(self):
        await self.configure(timeout_seconds=0.1)
        self.provider.release = asyncio.Event()
        decision = await self.service.decide("b", Candidate("slow", "hello"), "/tmp")
        self.assertEqual(decision.outcome, "unavailable")
        self.assertEqual(decision.reason, "decision_failed")
        self.provider.release = None
        self.provider.choice = "nonsense"
        self.assertEqual((await self.service.decide("b", Candidate("bad", "hello"), "/tmp")).outcome, "unavailable")
        self.provider.error = ValueError("secret bearer response body")
        await self.service.decide("b", Candidate("error", "hello"), "/tmp")
        self.assertNotIn("secret", json.dumps(self.service.get_status("b")))

    async def test_candidate_budget_fails_without_sending_truncated_text(self):
        await self.configure()
        result = await self.service.decide("b", Candidate("huge", "中" * 500), "/tmp")
        self.assertEqual(result.reason, "candidate_too_large")
        self.assertEqual(self.provider.requests, [])

    async def test_summary_preserves_concurrent_tail_and_failure_preserves_prefix(self):
        await self.configure()
        self.accepted("old " * 300)
        original = self.store.context("b")
        started, release = asyncio.Event(), asyncio.Event()
        async def summarize(cwd, text, max_tokens, *, key):
            self.assertEqual(key, "b")
            self.assertIn("old", text)
            started.set()
            await release.wait()
            return "previously asked for help"
        self.service.summarizer = summarize
        task = asyncio.create_task(self.service.decide("b", Candidate("new", "continue"), "/tmp"))
        await started.wait()
        await self.service.record_final("b", "turn", "recent result")
        release.set()
        decision = await task
        self.assertEqual(decision.outcome, "consume")
        context = self.store.context("b")
        self.assertEqual(context.summary, "previously asked for help")
        self.assertEqual([r.text for r in context.records], ["recent result"])
        self.assertNotEqual(context.summary_revision, original.summary_revision)
        self.accepted("another old " * 300, identifier="accepted-two")
        before = self.store.context("b")
        calls = []
        async def fail(*args, **kwargs):
            calls.append(1)
            raise ValueError("private contents")
        self.service.summarizer = fail
        for identifier in ("fail1", "fail2"):
            result = await self.service.decide("b", Candidate(identifier, "continue"), "/tmp")
            self.assertEqual(result.reason, "context_too_large")
        self.assertEqual(calls, [1])
        self.assertEqual(self.store.context("b"), before)

    async def test_test_connection_has_only_fixed_sample_and_detects_stale_revision(self):
        await self.configure()
        self.accepted("extremely confidential")
        result = await self.service.test_connection(1)
        self.assertTrue(result["ok"])
        self.assertNotIn("confidential", self.provider.requests[0][1])
        with self.assertRaises(AutonomyConflict):
            await self.service.test_connection(0)

    async def test_restart_cannot_claim_precise_old_gap_and_binding_delete_cascades(self):
        await self.configure()
        self.accepted()
        self.provider.choice = "skip"
        await self.service.decide("b", Candidate("skip", "chat"), "/tmp")
        restarted = AutonomyService(self.store, self.path)
        self.assertIsNone(restarted.prepare_explicit("b", Candidate("after", "hello")).gap_hint)
        self.assertEqual(len(self.store.context("b").records), 1)
        self.connection.execute("DELETE FROM bindings WHERE binding_id='b'")
        self.assertEqual(self.store.context("b").records, ())
        self.assertFalse(self.store.is_enabled("b"))
        require_schema(self.connection)

    async def test_disable_during_summary_leaves_original_context_and_no_provider_call(self):
        await self.configure()
        self.accepted("previous " * 250)
        before = self.store.context("b")
        entered, release = asyncio.Event(), asyncio.Event()
        async def summarize(*args, **kwargs):
            entered.set()
            await release.wait()
            return "short summary"
        self.service.summarizer = summarize
        task = asyncio.create_task(self.service.decide("b", Candidate("c", "continue"), "/tmp"))
        await entered.wait()
        self.store.set_enabled("b", False)
        release.set()
        self.assertEqual((await task).outcome, "unavailable")
        self.assertEqual(before, self.store.context("b"))
        self.assertEqual(self.provider.requests, [])

    async def test_close_invalidates_inflight_without_recording_and_keeps_other_binding_separate(self):
        await self.configure()
        self.store.set_enabled("other", True)
        self.accepted("only binding b")
        self.provider.release = asyncio.Event()
        task = asyncio.create_task(self.service.decide("other", Candidate("c", "hello"), "/tmp"))
        await self.provider.started.wait()
        self.assertNotIn("only binding b", self.provider.requests[-1][1])
        await self.service.aclose()
        self.provider.release.set()
        self.assertEqual((await task).outcome, "unavailable")
        self.assertFalse(self.service.record_accepted("b", Candidate("late", "ignored"), "turn"))

    async def test_transaction_rollback_is_owned_by_outer_store(self):
        before = self.store.settings("b")
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.set_enabled("b", False)
                raise RuntimeError("abort outer change")
        self.assertEqual(self.store.settings("b"), before)

    async def test_final_requires_accepted_turn_and_goal_uses_logical_identity(self):
        await self.configure()
        self.assertFalse(await self.service.record_final("b", "foreign", "not our input"))
        candidate = Candidate("goal", "steer current goal")
        token = self.service.prepare_explicit("b", candidate).token
        self.service.record_accepted("b", candidate, "physical-2", token, logical_turn_id="logical")
        self.assertTrue(await self.service.record_final("b", "physical-4", "goal complete", accepted_turn_id="logical"))
        self.assertFalse(await self.service.record_final("b", "physical-4", "goal complete", accepted_turn_id="logical"))
        self.accepted("turn before mode off", identifier="old", turn="old-turn")
        self.store.set_enabled("b", False)
        self.store.set_enabled("b", True)
        self.assertFalse(await self.service.record_final("b", "old-turn", "must not record"))

    async def test_schema_rejects_autonomy_on_direct_or_catch_up_binding(self):
        self.connection.execute("UPDATE scopes SET kind='direct'")
        with self.assertRaises(RuntimeError):
            require_schema(self.connection)
        self.connection.execute("UPDATE scopes SET kind='group'")
        self.connection.execute("UPDATE bindings SET message_context_mode='catch-up'")
        with self.assertRaises(RuntimeError):
            require_schema(self.connection)

    async def test_small_summary_budget_does_not_poison_later_short_candidate(self):
        await self.configure()
        self.accepted("history " * 400)
        calls = []
        async def summarize(cwd, text, max_tokens, *, key):
            calls.append(max_tokens)
            return "s" * 350
        self.service.summarizer = summarize
        first = await self.service.decide("b", Candidate("large", "x" * 250), "/tmp")
        self.assertEqual(first.outcome, "unavailable")
        self.assertEqual((await self.service.decide("b", Candidate("small", "hi"), "/tmp")).outcome, "consume")
        self.assertGreater(calls[-1], calls[0])

    async def test_summary_input_is_bounded_prefix_without_dropping_tail(self):
        await self.configure()
        for index in range(10):
            self.accepted("x" * 10000, identifier=f"big-{index}")
        calls = []
        async def summarize(cwd, text, budget, *, key):
            calls.append(text)
            self.assertLessEqual(len(text.encode()), 64 * 1024)
            return "earlier selected exchanges"
        self.service.summarizer = summarize
        first = await self.service.decide("b", Candidate("partial", "hi"), "/tmp")
        self.assertEqual(first.reason, "context_too_large")
        remaining = self.store.context("b").records
        self.assertTrue(0 < len(remaining) < 10)
        self.assertEqual(remaining[-1].reference, "big-9")
        second = await self.service.decide("b", Candidate("finish", "hi"), "/tmp")
        self.assertEqual(second.outcome, "consume")
        self.assertEqual(len(calls), 2)

    async def test_oversized_accepted_record_is_not_silently_truncated_for_summary(self):
        await self.configure()
        self.accepted("x" * (65 * 1024))
        calls = []
        async def summarize(*args, **kwargs):
            calls.append(1)
            return "summary"
        self.service.summarizer = summarize
        self.assertEqual((await self.service.decide("b", Candidate("new", "hi"), "/tmp")).outcome, "unavailable")
        self.assertEqual(calls, [])
        self.assertIn("x" * (65 * 1024), self.store.context("b").records[0].text)

    async def test_failed_summary_retries_new_candidate_after_cooldown(self):
        now = [0.0]
        self.service = AutonomyService(self.store, self.path, provider=self.provider, clock=lambda: now[0])
        await self.configure()
        self.accepted("history " * 400)
        calls = []
        async def summarize(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("temporarily unavailable")
            return "earlier selected exchanges"
        self.service.summarizer = summarize
        self.assertEqual((await self.service.decide("b", Candidate("first", "hi"), "/tmp")).outcome, "unavailable")
        now[0] = 29
        self.assertEqual((await self.service.decide("b", Candidate("second", "hi"), "/tmp")).outcome, "unavailable")
        self.assertEqual(calls, [1])
        now[0] = 31
        self.assertEqual((await self.service.decide("b", Candidate("third", "hi"), "/tmp")).outcome, "consume")
        self.assertEqual(calls, [1, 1])

    async def test_accepted_explicit_inputs_both_record_after_concurrent_anchor_change(self):
        first = Candidate("first", "first explicit")
        second = Candidate("second", "second explicit")
        first_token = self.service.prepare_explicit("b", first).token
        second_token = self.service.prepare_explicit("b", second).token
        self.assertTrue(self.service.record_accepted("b", first, "turn", first_token))
        self.assertFalse(self.service.token_current(second_token))
        self.assertTrue(self.service.record_accepted("b", second, "turn", second_token))
        self.assertEqual([r.reference for r in self.store.context("b").records], ["first", "second"])


class ProtocolTests(unittest.TestCase):
    def test_both_providers_use_narrow_choice_contract(self):
        for provider in ("jev", "laya"):
            config = parse_config({"provider": provider, "api_key": "key"})
            request = encode_request(config, "current")
            self.assertEqual(request["questions"]["consume_message"]["type"], "choice")
            self.assertEqual(set(request["questions"]["consume_message"]["criteria"]), {"consume", "skip"})
            self.assertTrue(request_fits(config, "current"))
            for choice in ("consume", "skip"):
                self.assertEqual(parse_answer({"answers": {"consume_message": {"type": "choice", "choice": choice, "action": {"act_probability": 0.1}}}}), choice)
        with self.assertRaises(AutonomyError):
            parse_answer({"answers": {"consume_message": {"type": "noul", "noul": 1}}})

    def test_laya_rejects_unknown_model_and_window_above_checkpoint(self):
        for payload in ({"model": "jev-1.13.0"}, {"model": "english", "input_budget": 1024}, {"input_budget": 4096}):
            with self.assertRaises(AutonomyError):
                parse_config({"provider": "laya", **payload})
        english = parse_config({"provider": "laya", "model": "english"})
        self.assertEqual(english.input_budget, 512)
        self.assertTrue(request_fits(english, "hello"))

    def test_malformed_config_types_are_safe_errors(self):
        for payload in (None, [], {"provider": []}, {"model": {}}, {"input_budget": True}, {"timeout_seconds": float("nan")}, {"api_key": "key\nline"}):
            with self.assertRaises(AutonomyError):
                parse_config(payload)


class AsyncProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_serializes_fixed_protocol_without_live_requests(self):
        config = parse_config({"provider": "jev", "api_key": "secret"})
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"answers": {"consume_message": {"type": "choice", "choice": "skip"}}})
        provider = SystemOneProvider(transport=httpx.MockTransport(handle))
        self.assertEqual(await provider.decide(config, "current"), "skip")
        request = requests[0]
        self.assertEqual(str(request.url), "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertEqual(json.loads(request.content)["state"], "current")
        self.assertEqual(len(requests), 1)

    async def test_transport_does_not_expose_remote_error_or_oversized_body(self):
        config = parse_config({"provider": "laya"})
        provider = SystemOneProvider(transport=httpx.MockTransport(lambda request: httpx.Response(401, text="private response")))
        with self.assertRaises(AutonomyError) as caught:
            await provider.decide(config, "current")
        self.assertEqual(str(caught.exception), "decision service HTTP 401")
        provider = SystemOneProvider(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 65537)))
        with self.assertRaises(AutonomyError):
            await provider.decide(config, "current")

    async def test_redirect_never_forwards_bearer_and_no_retry(self):
        requests = []
        def redirect(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://attacker.example"})
        provider = SystemOneProvider(transport=httpx.MockTransport(redirect))
        with self.assertRaises(AutonomyError):
            await provider.decide(parse_config({"provider": "jev", "api_key": "secret"}), "current")
        self.assertEqual(len(requests), 1)

    async def test_total_deadline_closes_slow_stream_without_executor(self):
        closed = asyncio.Event()
        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"{"
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        provider = SystemOneProvider(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowBody())))
        with patch("asyncio.to_thread", side_effect=AssertionError("must not use shared executor")):
            with self.assertRaises(AutonomyError):
                await provider.decide(parse_config({"provider": "laya", "timeout_seconds": 0.1}), "current")
        self.assertTrue(closed.is_set())


if __name__ == "__main__":
    unittest.main()
