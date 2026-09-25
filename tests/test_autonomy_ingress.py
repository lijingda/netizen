from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from netizen.autonomy import AutonomyService
from netizen.codex_runtime import SteerRace, Submission, SubmitDisposition, TurnOutcome
from netizen.domain import FeishuScope, ScopeKind
from tests.support.channel_cards import config_form_values, new_form_values
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage, plain_prompt_projection
from tests.support.channel_results import completed_turn_result


class DecisionProvider:
    def __init__(self) -> None:
        self.choice = "consume"
        self.states: list[str] = []
        self.error: Exception | None = None
        self.before_result = None

    async def decide(self, config: Any, state: str) -> str:
        self.states.append(state)
        if self.before_result is not None:
            await self.before_result()
        if self.error is not None:
            raise self.error
        return self.choice


class AutonomyIngressTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fixture = await self.enterAsyncContext(channel_fixture())
        self.fixture = fixture
        self.store, self.app, self.runtime, self.channel = fixture.store, fixture.app, fixture.runtime, fixture.channel
        self.scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        self.provider = DecisionProvider()
        self.autonomy = AutonomyService(self.store.autonomy, fixture.project_root / "decision.json", provider=self.provider)
        self.addAsyncCleanup(self.autonomy.aclose)
        self.app._autonomy = self.autonomy
        await self.autonomy.configure({"expected_revision": 0, "provider": "jev", "api_key": "test-not-a-real-secret"})
        self.binding = self.store.create_channel_binding(
            scope=self.scope, project_alias="test", creator_id="ou_user", autonomy_enabled=True,
        )
        self.released: list[str] = []
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED, self.binding.id, "native-one", "turn-one", lambda: self.released.append("turn-one"),
        )

    def message(self, text: str = "please help", *, message_id: str = "om_prompt", **kwargs: Any) -> FakeMessage:
        return FakeMessage(text, message_id=message_id, **{
            "chat_id": "oc_group", "chat_type": "group", "mentioned_bot": False, **kwargs,
        })

    def assert_silent(self) -> None:
        self.assertEqual(self.channel.replies, [])
        self.assertEqual(self.channel.reactions, [])
        self.assertEqual(self.channel.updates, [])
        self.assertEqual(self.channel.send_calls, [])

    async def clear_config(self) -> None:
        await self.autonomy.configure({"expected_revision": self.autonomy.get_status()["revision"], "clear": True})

    def card_event(self, values: dict[str, Any]) -> Any:
        self.channel.fetched_messages["om_card"] = {"data": {"items": [{"chat_id": "oc_group", "thread_id": None}]}}
        self.channel.chat_types["oc_group"] = "group"
        return SimpleNamespace(message_id="om_card", chat_id="oc_group", operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(tag="button", value={}, form_value=values))

    async def test_disabled_and_unbound_groups_do_not_call_the_model_or_respond(self) -> None:
        with self.store._transaction():
            self.store.autonomy.set_enabled(self.binding.id, False)
        await self.app.handle_message(self.message())
        await self.app.handle_message(self.message(chat_id="oc_unbound"))
        self.assertEqual(self.provider.states, [])
        self.assertEqual(self.runtime.capture_calls, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assert_silent()

    async def test_skip_is_silent_and_never_saved_as_context(self) -> None:
        self.provider.choice = "skip"
        await self.app.handle_message(self.message("skip-this-private-noise"))
        self.assertEqual(len(self.provider.states), 1)
        self.assertIn("skip-this-private-noise", self.provider.states[0])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.store.autonomy.context(self.binding.id).records, ())
        self.assert_silent()

    async def test_consumed_message_uses_existing_native_input_and_captures_admission_before_model(self) -> None:
        async def assert_admission_captured() -> None:
            self.assertEqual(self.runtime.capture_calls, [self.binding.id])
            self.assertEqual(self.runtime.submit_calls, [])

        self.provider.before_result = assert_admission_captured
        message = self.message("selected request")
        await self.app.handle_message(message)
        self.assertEqual(len(self.runtime.submit_calls), 1)
        call = self.runtime.submit_calls[0]
        text, metadata = plain_prompt_projection(call["input"])
        self.assertEqual(text, "selected request")
        self.assertEqual(metadata["message_id"], message.id)
        self.assertIs(call["origin"], message)
        self.assertEqual(call["binding"].id, self.binding.id)
        self.assertEqual(call["admission"].binding_id, self.binding.id)
        self.assertIsNone(call["context_commit"])
        self.assertEqual(self.fixture.message_history.read_calls, [])
        self.assertEqual(self.released, ["turn-one"])
        self.assertEqual([record.reference for record in self.store.autonomy.context(self.binding.id).records], [message.id])

    async def test_explicit_mention_bypasses_even_unconfigured_model_and_records_accepted_input(self) -> None:
        await self.clear_config()
        self.provider.error = AssertionError("explicit mention must not consult the model")
        message = self.message("explicit request", mentioned_bot=True)
        await self.app.handle_message(message)
        self.assertEqual(self.provider.states, [])
        self.assertEqual(len(self.runtime.submit_calls), 1)
        self.assertEqual(self.released, ["turn-one"])
        self.assertEqual([record.reference for record in self.store.autonomy.context(self.binding.id).records], [message.id])

    async def test_other_bots_and_unmentioned_commands_never_enter_the_model(self) -> None:
        cases = (
            self.message(is_bot=True),
            self.message(sender_type="app"),
            self.message("/stop"),
            self.message("  /config"),
        )
        for message in cases:
            await self.app.handle_message(message)
        self.assertEqual(self.provider.states, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.runtime.stop_calls, [])
        self.assert_silent()
        await self.app.handle_message(self.message("/status", mentioned_bot=True))
        self.assertEqual(self.provider.states, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertTrue(self.channel.replies)

    async def test_provider_failure_has_no_chat_feedback_or_fallback_submission(self) -> None:
        self.provider.error = RuntimeError("upstream raw secret must not escape")
        await self.app.handle_message(self.message())
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.store.autonomy.context(self.binding.id).records, ())
        status = self.autonomy.get_status(self.binding.id)
        self.assertEqual(status["state"], "unavailable")
        self.assertNotIn("upstream raw secret", str(status))
        self.assert_silent()

    async def test_decision_returned_after_binding_switch_is_discarded(self) -> None:
        async def switch_binding() -> None:
            self.store.create_channel_binding(scope=self.scope, project_alias="test", creator_id="ou_user")

        self.provider.before_result = switch_binding
        await self.app.handle_message(self.message())
        self.assertNotEqual(self.store.active_binding(self.scope.key).id, self.binding.id)
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.store.autonomy.context(self.binding.id).records, ())
        self.assert_silent()

    async def test_decision_returned_after_clearing_configuration_is_discarded(self) -> None:
        self.provider.before_result = self.clear_config
        await self.app.handle_message(self.message())
        self.assertFalse(self.autonomy.configured)
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.store.autonomy.context(self.binding.id).records, ())
        self.assert_silent()

    async def test_native_rejection_is_not_recorded_as_consumption_or_reported_in_chat(self) -> None:
        with patch.object(self.runtime, "submit", new=AsyncMock(side_effect=SteerRace("exact admission changed"))):
            await self.app.handle_message(self.message())
        self.assertEqual(self.store.autonomy.context(self.binding.id).records, ())
        self.assertEqual(self.released, [])
        self.assert_silent()

    async def test_input_guard_rechecks_configuration_at_native_submission(self) -> None:
        submit = self.runtime.submit

        async def clear_before_submit(**kwargs: Any) -> Any:
            await self.clear_config()
            return await submit(**kwargs)

        with patch.object(self.runtime, "submit", side_effect=clear_before_submit):
            await self.app.handle_message(self.message())
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.store.autonomy.context(self.binding.id).records, ())
        self.assert_silent()

    async def test_accepted_record_failure_does_not_strand_native_receipt(self) -> None:
        with patch.object(self.autonomy, "record_accepted", side_effect=OSError("storage unavailable")):
            await self.app.handle_message(self.message())
        self.assertEqual(len(self.runtime.submit_calls), 1)
        self.assertEqual(self.released, ["turn-one"])
        self.assertEqual(self.store.autonomy.context(self.binding.id).records, ())

    async def test_skip_gap_is_only_added_to_codex_input_and_skipped_body_stays_out_of_later_context(self) -> None:
        await self.app.handle_message(self.message("first selected", message_id="om_first"))
        self.provider.choice = "skip"
        await self.app.handle_message(self.message("skipped-body-one", message_id="om_skip1"))
        await self.app.handle_message(self.message("skipped-body-two", message_id="om_skip2"))
        self.provider.choice = "consume"
        self.runtime.submission = Submission(SubmitDisposition.STEERED, self.binding.id, "native-one", "turn-one")
        await self.app.handle_message(self.message("next selected", message_id="om_next"))
        last_input = self.runtime.submit_calls[-1]["input"]
        self.assertIn("有 2 条用户消息", str(last_input))
        self.assertIn("<netizen_autonomous_context>", str(last_input))
        self.assertNotIn("skipped-body", str(last_input))
        self.assertNotIn("有 2 条用户消息", str(self.channel.replies) + str(self.channel.updates))
        self.assertNotIn("skipped-body", self.provider.states[-1])
        self.assertEqual([record.reference for record in self.store.autonomy.context(self.binding.id).records], ["om_first", "om_next"])
        self.assertEqual(self.fixture.message_history.read_calls, [])

    async def test_only_accepted_turn_final_body_enters_context_and_model_authored_mentions_survive(self) -> None:
        message = self.message("selected request")
        await self.app.handle_message(message)
        await self.app.handle_completion(TurnOutcome(
            binding_id=self.binding.id, thread_id="native-one", turn_id="unaccepted-turn", owner_id="ou_user", origin=message,
            result=completed_turn_result(final_response="unrelated result"),
        ))
        self.assertEqual(len(self.store.autonomy.context(self.binding.id).records), 1)
        final = "Done. @Alice please review."
        await self.app.handle_completion(TurnOutcome(
            binding_id=self.binding.id, thread_id="native-one", turn_id="turn-one", owner_id="ou_user", origin=message,
            result=completed_turn_result(final_response=final),
        ))
        records = self.store.autonomy.context(self.binding.id).records
        self.assertEqual([(record.kind, record.text) for record in records if record.kind == "final"], [("final", final)])
        self.assertEqual(len(self.provider.states), 1)
        self.provider.choice = "skip"
        await self.app.handle_message(self.message("follow-up", message_id="om_after"))
        self.assertIn(final, self.provider.states[-1])
        self.assertNotIn("unrelated result", self.provider.states[-1])

    async def test_missing_model_configuration_rejects_new_card_without_creating_binding(self) -> None:
        await self.clear_config()
        await self.app.handle_message(self.message("/new", mentioned_bot=True))
        card = self.channel.replies[-1][1]
        values = new_form_values(card)
        values["new_context_mode"] = "autonomy:v1:enabled"
        await self.app.handle_card_action(self.card_event(values))
        self.assertEqual([binding.id for binding in self.store.list_bindings(self.scope.key)], [self.binding.id])
        self.assertEqual(self.store.active_binding(self.scope.key).id, self.binding.id)
        self.assertIn("Admin", str(self.channel.replies) + str(self.channel.updates))
        self.assertIn("未配置", str(self.channel.replies) + str(self.channel.updates))
        self.assertEqual(self.runtime.submit_calls, [])

    async def test_config_card_retains_old_settings_when_decision_configuration_is_missing(self) -> None:
        await self.clear_config()
        await self.app.handle_message(self.message("/config", mentioned_bot=True))
        values = config_form_values(self.channel.replies[-1][1])
        values["config_context_mode"] = "autonomy:v1:enabled"
        await self.app.handle_card_action(self.card_event(values))
        self.assertEqual(self.store.get(self.binding.id), self.binding)
        self.assertIn("未配置", str(self.channel.updates))
        self.assertEqual(self.runtime.configure_settings_calls, [])


if __name__ == "__main__":
    unittest.main()
