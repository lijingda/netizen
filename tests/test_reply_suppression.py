"""Suppression belongs only to one exact physical Turn and reply lifetime."""

from __future__ import annotations

import unittest

from netizen_cli.channel.reply_suppression import ReplySuppression


class ReplySuppressionTest(unittest.TestCase):
    def test_exact_physical_turn_is_idempotent_and_does_not_affect_another_turn(self):
        suppression = ReplySuppression()
        suppression.suppress("native-one", "logical-one", "physical-one")
        suppression.suppress("native-one", "logical-one", "physical-one")
        self.assertTrue(suppression.is_suppressed("native-one", "logical-one", "physical-one"))
        self.assertFalse(suppression.is_suppressed("native-one", "logical-one", "physical-two"))
        self.assertFalse(suppression.is_suppressed("native-one", "logical-two", "physical-one"))
        self.assertFalse(suppression.is_suppressed("native-two", "logical-one", "physical-one"))

    def test_cleanup_is_scoped_to_exact_reply_and_thread(self):
        suppression = ReplySuppression()
        for thread, reply in (("one", "first"), ("one", "second"), ("two", "first")):
            suppression.suppress(thread, reply, "physical-one")
        suppression.discard("one", "first")
        self.assertFalse(suppression.is_suppressed("one", "first", "physical-one"))
        self.assertTrue(suppression.is_suppressed("one", "second", "physical-one"))
        self.assertTrue(suppression.is_suppressed("two", "first", "physical-one"))
        suppression.discard_thread("one")
        self.assertFalse(suppression.is_suppressed("one", "second", "physical-one"))
        self.assertTrue(suppression.is_suppressed("two", "first", "physical-one"))
        suppression.clear()
        self.assertFalse(suppression.is_suppressed("two", "first", "physical-one"))
