from __future__ import annotations

import asyncio
import tempfile
import json
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from openai_codex.errors import InvalidRequestError

from netizen.bindings import BindingStore
from netizen.channel_app import ChannelApplication
from netizen.domain import ActiveState, ScheduledConversation, ScheduledOrigin
from netizen.schedules.service import ScheduleService
from scripts.probe_scheduled_tasks import (
    FakeFeishu, McpRecorder, ProbeCompletionFailure, ProbeFailure, ProbeStopFailure, _mcp_phase,
    _mcp_recovery_phase, _record_completion, _remove_fixture_trust, _safe_traceback,
    _wait_for_completion, _wait_for_exact_active,
)


class FixtureTrustCleanupTest(unittest.TestCase):
    def test_diagnostics_expose_locations_without_error_values_or_locals(self):
        private_value = "do-not-print-this-secret"
        try:
            try:
                raise AttributeError(private_value)
            except AttributeError as error:
                raise RuntimeError(private_value) from error
        except RuntimeError as error:
            diagnostic = _safe_traceback(error)
        self.assertEqual([entry["exception_type"] for entry in diagnostic], ["RuntimeError", "AttributeError"])
        self.assertNotIn(private_value, json.dumps(diagnostic))
        for entry in diagnostic:
            for frame in entry["frames"]:
                self.assertEqual(set(frame), {"filename", "lineno", "function"})
                self.assertEqual(frame["filename"], "tests/test_probe_scheduled_tasks.py")

    def test_removes_only_new_exact_fixture_table_and_preserves_other_bytes(self):
        before_text = '# original\nmodel = "test"\n[projects."/existing"]\ntrust_level = "trusted"\n'
        unrelated = '\n# preserve comment\n[mcp_servers.owned_by_user]\nurl = "http://example.test"\n'
        addition = '\n[projects."/tmp/probe"]\ntrust_level = "trusted"\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = before_text + addition + unrelated
            path.write_text(original)
            self.assertTrue(_remove_fixture_trust(path, tomllib.loads(before_text), {"/tmp/probe", "/existing"}))
            self.assertEqual(path.read_text(), before_text + "\n" + unrelated)
            self.assertFalse(_remove_fixture_trust(path, tomllib.loads(before_text), {"/tmp/probe"}))

    def test_unexpected_fixture_fields_fail_without_writing_anything(self):
        original = '[projects."/tmp/probe"]\ntrust_level = "trusted"\nextra = true\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(original)
            with self.assertRaisesRegex(ProbeFailure, "fixture_project_config_unexpected"):
                _remove_fixture_trust(path, {}, {"/tmp/probe"})
            self.assertEqual(path.read_text(), original)

    def test_concurrent_unrelated_config_changes_are_preserved(self):
        before = {"model": "old"}
        original = 'model = "new"\n[projects."/tmp/probe"]\ntrust_level = "trusted"\n[projects."/new-user-project"]\ntrust_level = "trusted"\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(original)
            self.assertTrue(_remove_fixture_trust(path, before, {"/tmp/probe"}))
            self.assertEqual(tomllib.loads(path.read_text()), {
                "model": "new", "projects": {"/new-user-project": {"trust_level": "trusted"}},
            })


class FakeMcpProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_completion_callback_exception_fails_fast_without_exposing_error_text(self):
        secret = "DO-NOT-EXPOSE-CALLBACK-DETAILS"
        application = SimpleNamespace(handle_completion=AsyncMock(side_effect=ValueError(secret)))
        outcomes = asyncio.Queue()
        with self.assertRaises(ValueError):
            await _record_completion(application, outcomes, object())
        with self.assertRaises(ProbeCompletionFailure) as caught:
            await _wait_for_completion(outcomes, codex=None, runtime=None, store=None,
                binding_id="binding", run_id="run", turn_id="turn", stage="initial")
        self.assertEqual(str(caught.exception), "completion_callback_failed")
        self.assertEqual(caught.exception.evidence, {"callback_error_type": "ValueError"})
        self.assertNotIn(secret, json.dumps(caught.exception.evidence) + json.dumps(_safe_traceback(caught.exception)))
        self.assertTrue(outcomes.empty())

    async def test_completion_timeout_reads_exact_public_turn_once_without_mutation_or_secret_output(self):
        secret = "DO-NOT-EXPOSE-NATIVE-DETAILS"
        native_id, initial_id = secret + "-thread", secret + "-initial"
        codex = SimpleNamespace(thread_resume=AsyncMock(), thread_start=AsyncMock())
        runtime = SimpleNamespace(active_turn=lambda _: SimpleNamespace(state=ActiveState.OBSERVATION_UNAVAILABLE))
        run = SimpleNamespace(phase="handed_off", barrier="held", delivery_state=None, instructions=secret)
        store = SimpleNamespace(schedules=SimpleNamespace(get_run=lambda _: run, set_run=AsyncMock()),
            get=lambda _: SimpleNamespace(native_thread_id=native_id))
        view = SimpleNamespace(thread=SimpleNamespace(id=native_id, status=SimpleNamespace(type="active"), turns=[
            SimpleNamespace(id=initial_id, status="inProgress", final_response=secret),
            SimpleNamespace(id="later-manual", status="completed"),
        ]))
        wait_for = asyncio.wait_for
        for native_result in (view, ValueError(secret)):
            with self.subTest(read_error=isinstance(native_result, Exception)):
                thread = SimpleNamespace(read=AsyncMock(), stream=AsyncMock())
                if isinstance(native_result, Exception):
                    thread.read.side_effect = native_result
                else:
                    thread.read.return_value = native_result
                with patch("scripts.probe_scheduled_tasks.AsyncThread", return_value=thread) as constructor, \
                     patch("scripts.probe_scheduled_tasks.asyncio.wait_for", new=AsyncMock(wraps=wait_for)) as bounded:
                    with self.assertRaises(ProbeCompletionFailure) as caught:
                        await _wait_for_completion(SimpleNamespace(get=AsyncMock(side_effect=TimeoutError(secret))),
                            codex=codex, runtime=runtime, store=store, binding_id="binding", run_id="run",
                            turn_id=initial_id, stage="initial")
                constructor.assert_called_once_with(codex, native_id)
                thread.read.assert_awaited_once_with(include_turns=True)
                self.assertEqual([call.args[1] for call in bounded.await_args_list], [90, 3])
                thread.stream.assert_not_called()
                evidence = caught.exception.evidence
                self.assertEqual(evidence["runtime_state"], "turn-observation-unavailable")
                self.assertEqual(evidence["barrier"], "held")
                if isinstance(native_result, Exception):
                    self.assertEqual(evidence["read_error_type"], "ValueError")
                else:
                    self.assertEqual(evidence["native_turn_state"], "inProgress")
                self.assertNotIn(secret, json.dumps(evidence) + json.dumps(_safe_traceback(caught.exception)))
        codex.thread_resume.assert_not_called()
        codex.thread_start.assert_not_called()
        store.schedules.set_run.assert_not_called()

    async def test_dispatch_fixture_reply_proves_exact_production_completion_destination(self):
        channel = FakeFeishu()
        application = object.__new__(ChannelApplication)
        application._channel = channel
        origin = ScheduledOrigin(
            "probe", "probe-chat", "probe-root", ScheduledConversation("probe-topic"),
            "probe-plan", "probe-run",
        )
        result = await ChannelApplication._reply_to_origin(application, origin, "completed")
        self.assertTrue(await ChannelApplication._scheduled_reply_confirmed(application, origin, result))
        self.assertEqual(result.raw["data"]["parent_id"], origin.message_id)
        self.assertEqual(channel.replies, 1)
        other_topic = ScheduledOrigin(
            "probe", "probe-chat", "probe-root", ScheduledConversation("other-topic"),
            "probe-plan", "probe-run",
        )
        self.assertFalse(await ChannelApplication._scheduled_reply_confirmed(application, other_topic, result))

    async def test_cold_resume_uses_new_endpoint_and_fork_owns_distinct_default_group(self):
        await self._exercise_mcp_recovery(fail_first_close=False)

    async def test_failed_first_shutdown_cleans_retained_thread_without_resuming(self):
        await self._exercise_mcp_recovery(fail_first_close=True)

    async def _exercise_mcp_recovery(self, *, fail_first_close):
        clients = []
        runners = []
        case = self

        class Runner:
            def __init__(self):
                index = str(len(runners))
                self.namespace = "runner-" + index
                self.url = "http://127.0.0.1:" + str(3000 + len(runners))
                self.app_server_env = {"PROBE_TOKEN_" + index: "token-" + index}
                self.config_overrides = ()
                self.closed = False
                runners.append(self)

            def attach(self, callback):
                self.callback = callback

            async def bind(self):
                pass

            def open_admission(self):
                pass

            def close_admission(self):
                pass

            async def close(self):
                self.closed = True

        class Thread:
            def __init__(self, client, thread_id):
                self.client = client
                self.id = thread_id

            async def read(self):
                return SimpleNamespace(thread=SimpleNamespace(ephemeral=False))

            async def run(self, prompt):
                callback = self.client.runner.callback
                if self.client.generation == 1 and self.id == "owned-parent":
                    listed = await callback({"mode": "list"}, self.id)
                    case.assertEqual(len(listed["plans"]), 1)
                    plan = listed["plans"][0]
                    viewed = await callback({"mode": "view", "plan_id": plan["id"]}, self.id)
                    case.assertTrue(viewed["ok"])
                    result = await callback({"mode": "update", "plan_id": plan["id"],
                        "expected_revision": plan["revision"], "request_id": "resume-update",
                        "instructions": "RECOVERY-PROBE-RESUMED", "enabled": False}, self.id)
                else:
                    result = await callback({"mode": "create", "name": self.id, "instructions": "RECOVERY-PROBE-INITIAL",
                        "enabled": False, "schedule": {"kind": "daily", "at": "09:00", "timezone": "UTC"},
                        "request_id": self.id + "-create"}, self.id)
                case.assertTrue(result["ok"], result)
                return SimpleNamespace(status="completed")

        class Codex:
            def __init__(self, config):
                self.generation = len(clients)
                self.runner = runners[-1]
                self.closed = False
                if clients:
                    case.assertTrue(clients[-1].closed)
                    case.assertTrue(clients[-1].runner.closed)
                clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.closed = True
                if fail_first_close and self.generation == 0:
                    raise TimeoutError("fixture shutdown failed")

            async def thread_start(self, **kwargs):
                case.assertFalse(kwargs["ephemeral"])
                return Thread(self, "owned-parent")

            async def thread_resume(self, thread_id, **kwargs):
                case.assertEqual((thread_id, self.generation), ("owned-parent", 1))
                return Thread(self, thread_id)

            async def thread_fork(self, thread_id, **kwargs):
                case.assertEqual(thread_id, "owned-parent")
                case.assertFalse(kwargs["ephemeral"])
                return Thread(self, "owned-fork")

        class Delete:
            def __init__(self, client):
                self.client = client
                self.owned = set()
                self.attempted = set()

            async def delete(self, thread_id):
                case.assertEqual(self.client.generation, 1)
                case.assertIn(thread_id, self.owned)
                case.assertNotIn(thread_id, self.attempted)
                self.attempted.add(thread_id)

        with tempfile.TemporaryDirectory() as directory:
            store = BindingStore(Path(directory) / "probe.sqlite3")
            self.addCleanup(store.close)
            store.register_project(alias="probe", cwd=directory)
            with patch("scripts.probe_scheduled_tasks.ScheduleMcpRunner", Runner), \
                 patch("scripts.probe_scheduled_tasks.AsyncCodex", Codex), \
                 patch("scripts.probe_scheduled_tasks._DeleteOnce", Delete), \
                 patch("scripts.probe_scheduled_tasks._prove_thread_absent_from_all_catalogs", new_callable=AsyncMock) as absent:
                if fail_first_close:
                    with self.assertRaisesRegex(ProbeFailure, "owned_native_cleanup_not_confirmed"):
                        await _mcp_recovery_phase(Path(directory), "test-model", store)
                else:
                    result = await _mcp_recovery_phase(Path(directory), "test-model", store)
                    self.assertTrue(result["cold_resume_exact_identity"])
                    self.assertTrue(result["fork_default_group_identity"])
                    self.assertFalse(result["deferred_tool_search_tested"])
                    self.assertFalse(result["compacted_thread_tested"])
            self.assertEqual(absent.await_count, 1 if fail_first_close else 2)
            self.assertEqual(len(runners), 1 if fail_first_close else 2)
            self.assertEqual(len(clients), 2)
            self.assertTrue(all(client.closed for client in clients))
            self.assertTrue(all(runner.closed for runner in runners))

    async def test_stop_waits_for_exact_turn_and_active_thread_and_rejects_terminal(self):
        snapshots = [
            ("active", [SimpleNamespace(id="other", status="inProgress")]),
            ("idle", [SimpleNamespace(id="owned-turn", status="inProgress")]),
            ("active", [SimpleNamespace(id="owned-turn", status="inProgress")]),
        ]

        async def read(**kwargs):
            self.assertEqual(kwargs, {"include_turns": True})
            state, turns = snapshots.pop(0)
            return SimpleNamespace(thread=SimpleNamespace(
                id="owned-thread", status=SimpleNamespace(type=state), turns=turns,
            ))

        thread = SimpleNamespace(id="owned-thread", read=read)
        await _wait_for_exact_active(thread, "owned-turn")
        self.assertFalse(snapshots)
        snapshots.append(("idle", [SimpleNamespace(id="owned-turn", status="completed")]))
        with self.assertRaisesRegex(ProbeFailure, "stop_fixture_terminal_before_active_observation"):
            await _wait_for_exact_active(thread, "owned-turn")

    async def test_stop_error_reports_only_numeric_code_and_classification(self):
        error = ProbeStopFailure(InvalidRequestError(-32600, "No active turn for SECRET-NATIVE-ID"))
        self.assertEqual(error.rpc, {"code": -32600, "not_active": True})
        self.assertNotIn("SECRET", str(error) + json.dumps(error.rpc))

    async def test_probe_rounds_validate_real_service_responses_and_default_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            store = BindingStore(cwd / "probe.sqlite3")
            self.addCleanup(store.close)
            store.register_project(alias="probe", cwd=str(cwd))
            recorder = McpRecorder(ScheduleService(
                bindings=store, runtime=None, app_id="probe", chat_info=FakeFeishu(), default_timezone="UTC",
            ))
            case = self

            class Thread:
                def __init__(self, thread_id):
                    self.id = thread_id
                    self.round = 0

                async def run(self, prompt):
                    self.round += 1
                    if self.round == 1:
                        request = {"mode": "create", "name": self.id, "instructions": "SCHEDULE-PROBE",
                                   "enabled": False, "schedule": {"kind": "daily", "at": "09:00", "timezone": "UTC"},
                                   "request_id": self.id + "-create"}
                        result = await recorder.manage(request, self.id)
                    elif self.round == 2:
                        listed = await recorder.manage({"mode": "list"}, self.id)
                        case.assertEqual([plan["id"] for plan in listed["plans"]], [recorder.created[self.id]])
                        view = await recorder.manage({"mode": "view", "plan_id": recorder.created[self.id]}, self.id)
                        result = await recorder.manage({
                            "mode": "update", "plan_id": view["plan"]["id"], "expected_revision": view["plan"]["revision"],
                            "instructions": "SCHEDULE-PROBE-UPDATED", "enabled": False, "request_id": self.id + "-update",
                        }, self.id)
                    else:
                        plan = store.schedules.get(recorder.created[self.id])
                        result = await recorder.manage({"mode": "delete", "plan_id": plan.id,
                            "expected_revision": plan.revision, "request_id": self.id + "-delete"}, self.id)
                    case.assertTrue(result["ok"], result)
                    return SimpleNamespace(status="completed")

            class Codex:
                count = 0

                async def thread_start(self, **kwargs):
                    case.assertEqual(kwargs, {"cwd": str(cwd), "ephemeral": True})
                    self.count += 1
                    return Thread("native-" + str(self.count))

            result = await _mcp_phase(Codex(), cwd, store, recorder)
            self.assertTrue(result["natural_language_crud"])
            self.assertEqual(len(recorder.created), 2)
            rejected = await recorder.manage({"mode": "create", "enabled": True}, "native-1")
            self.assertEqual(rejected["error"]["code"], "probe_paused_only")
            rejected = await recorder.manage({"mode": "create", "enabled": False, "request_id": "extra"}, "native-1")
            self.assertEqual(rejected["error"]["code"], "probe_one_plan_only")
            rejected = await recorder.manage({"mode": "list"}, "unknown")
            self.assertEqual(rejected["error"]["code"], "probe_identity_missing")


if __name__ == "__main__":
    unittest.main()
