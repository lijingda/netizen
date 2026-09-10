from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lark_channel import OutboundCard

from netizen.cards import turn_progress_card
from netizen.channel.reply_presenter import _ReplyCardPresenter
from netizen.runtime.contracts import ActiveState, TurnActivitySnapshot


class ScheduledReplyPresenterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = {
            "binding_id": "binding-scheduled",
            "thread_id": "native-scheduled",
            "turn_id": "initial-turn",
        }
        self.activity = TurnActivitySnapshot(
            **self.identity,
            revision=1,
            state=ActiveState.RUNNING,
            steer_count=0,
            plan_available=True,
            plan_generated=False,
            plan_may_be_stale=False,
            steps=(),
        )
        self.result = SimpleNamespace(success=True, message_id="om_progress")
        self.channel = SimpleNamespace(
            reply=AsyncMock(return_value=self.result),
            update_card=AsyncMock(return_value=SimpleNamespace(success=True)),
        )
        self.runtime = SimpleNamespace(
            turn_activity=Mock(return_value=self.activity),
            lifecycle_state=Mock(return_value=None),
        )
        self.presenter = _ReplyCardPresenter(
            self.channel,  # type: ignore[arg-type]
            self.runtime,  # type: ignore[arg-type]
            poll_seconds=60,
        )
        self.addAsyncCleanup(self.presenter.close)
        self.origin = object()

    async def start(self, **kwargs):
        return await self.presenter.start(
            **self.identity,
            origin=self.origin,
            **kwargs,
        )

    async def finish(self, **kwargs):
        render = kwargs.pop("render", lambda snapshot: turn_progress_card(
            snapshot=snapshot,
            final_response="Finished scheduled work",
            terminal_status="completed",
            collapsed=True,
        ))
        return await self.presenter.finish(
            **self.identity,
            activity=self.activity,
            render=render,
            **kwargs,
        )

    async def test_default_route_and_terminal_update_remain_ordinary(self):
        self.assertTrue(await self.start())
        self.channel.reply.assert_awaited_once()
        origin, card = self.channel.reply.await_args.args
        self.assertIs(origin, self.origin)
        self.assertIsInstance(card, OutboundCard)
        attempt = await self.finish()
        self.assertIsNotNone(attempt)
        self.assertTrue(attempt.updated)
        self.assertEqual(attempt.message_id, "om_progress")
        self.assertIs(attempt.result, self.channel.update_card.return_value)
        self.channel.update_card.assert_awaited_once()
        self.assertEqual(self.channel.update_card.await_args.args[0], "om_progress")

    async def test_custom_reply_is_validated_before_terminal_uses_its_exact_id(self):
        events = []
        routed_result = SimpleNamespace(success=True, message_id="om_topic_progress")

        async def reply(card):
            self.assertIsInstance(card, OutboundCard)
            events.append("reply")
            return routed_result

        async def validate(result):
            self.assertIs(result, routed_result)
            events.append("validate")
            return True

        self.assertTrue(await self.start(reply=reply, validate_reply=validate))
        self.assertEqual(events, ["reply", "validate"])
        self.channel.reply.assert_not_awaited()
        attempt = await self.finish()
        self.assertIsNotNone(attempt)
        self.assertTrue(attempt.updated)
        self.assertEqual(attempt.message_id, "om_topic_progress")
        self.assertIs(attempt.result, self.channel.update_card.return_value)
        self.channel.update_card.assert_awaited_once()
        self.assertEqual(
            self.channel.update_card.await_args.args[0], "om_topic_progress"
        )

    async def test_failed_validation_never_starts_polling_or_retains_a_session(self):
        for error in (None, RuntimeError("topic confirmation unavailable")):
            with self.subTest(error=error):
                validate = AsyncMock(return_value=False, side_effect=error)
                with patch.object(self.presenter, "_poll", new_callable=AsyncMock) as poll:
                    if error is None:
                        started = await self.start(validate_reply=validate)
                    else:
                        with self.assertLogs("netizen.channel.reply_presenter", "ERROR"):
                            started = await self.start(validate_reply=validate)
                    self.assertFalse(started)
                    poll.assert_not_called()
                validate.assert_awaited_once_with(self.result)
                self.assertIsNone(await self.finish())
                self.channel.update_card.assert_not_awaited()

    async def test_validation_obeys_initial_reply_operation_deadline(self):
        presenter = _ReplyCardPresenter(
            self.channel,  # type: ignore[arg-type]
            self.runtime,  # type: ignore[arg-type]
            operation_timeout_seconds=0.01,
        )
        self.addAsyncCleanup(presenter.close)
        entered = asyncio.Event()

        async def validate(result):
            entered.set()
            await asyncio.Event().wait()
            return True

        with patch.object(presenter, "_poll", new_callable=AsyncMock) as poll:
            with self.assertLogs("netizen.channel.reply_presenter", "ERROR"):
                self.assertFalse(await presenter.start(
                    **self.identity, origin=self.origin, validate_reply=validate,
                ))
            poll.assert_not_called()
        self.assertTrue(entered.is_set())

    async def test_terminal_update_retains_failed_or_unknown_single_attempt(self):
        for error in (None, RuntimeError("update unavailable")):
            with self.subTest(error=error):
                self.assertTrue(await self.start())
                self.channel.update_card.reset_mock()
                self.channel.update_card.return_value = SimpleNamespace(success=False)
                self.channel.update_card.side_effect = error
                with self.assertLogs("netizen.channel.reply_presenter", "ERROR"):
                    attempt = await self.finish()
                self.assertIsNotNone(attempt)
                self.assertEqual(attempt.message_id, "om_progress")
                self.assertFalse(attempt.updated)
                self.assertIs(attempt.result, self.channel.update_card.return_value if error is None else None)
                self.assertIsNone(await self.finish())
                self.channel.update_card.assert_awaited_once()

    async def test_render_failure_does_not_attempt_terminal_update(self):
        self.assertTrue(await self.start())
        render = Mock(side_effect=RuntimeError("invalid card"))
        with self.assertLogs("netizen.channel.reply_presenter", "ERROR"):
            self.assertIsNone(await self.finish(render=render))
        render.assert_called_once_with(self.activity)
        self.assertIsNone(await self.finish())
        self.channel.update_card.assert_not_awaited()
        self.channel.reply.assert_awaited_once()

    async def test_terminal_preserves_unconfirmed_sdk_response_for_caller(self):
        self.assertTrue(await self.start())
        # Public update_card can report success without a raw acknowledgment.
        # Ordinary display retains its projection; Channel can inspect the same
        # response before confirming scheduled terminal delivery.
        result = SimpleNamespace(success=True, message_id="om_progress", raw=None)
        self.channel.update_card.return_value = result
        attempt = await self.finish()
        self.assertIsNotNone(attempt)
        self.assertTrue(attempt.updated)
        self.assertIs(attempt.result, result)
        self.assertIsNone(await self.finish())
        self.channel.update_card.assert_awaited_once()
        self.channel.reply.assert_awaited_once()

    async def test_cancellation_of_reply_or_validation_propagates_without_polling(self):
        for operation in ("reply", "validate_reply"):
            with self.subTest(operation=operation):
                callback = AsyncMock(side_effect=asyncio.CancelledError())
                with patch.object(self.presenter, "_poll", new_callable=AsyncMock) as poll:
                    with self.assertRaises(asyncio.CancelledError):
                        await self.start(**{operation: callback})
                    poll.assert_not_called()
                self.assertIsNone(await self.finish())
        self.channel.update_card.assert_not_awaited()

    async def test_cancellation_of_update_propagates_without_another_attempt(self):
        self.assertTrue(await self.start())
        self.channel.update_card.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.finish()
        self.assertIsNone(await self.finish())
        self.channel.update_card.assert_awaited_once()

    async def test_terminal_update_timeout_retains_an_unknown_attempt(self):
        self.assertTrue(await self.start())
        self.presenter._operation_timeout_seconds = 0.01
        entered = asyncio.Event()

        async def pending_update(*args):
            entered.set()
            await asyncio.Event().wait()

        self.channel.update_card.side_effect = pending_update
        with self.assertLogs("netizen.channel.reply_presenter", "ERROR"):
            attempt = await self.finish()
        self.assertTrue(entered.is_set())
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.message_id, "om_progress")
        self.assertIsNone(attempt.result)
        self.assertFalse(attempt.updated)
        self.assertIsNone(await self.finish())
        self.channel.update_card.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
