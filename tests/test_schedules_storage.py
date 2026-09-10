from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from netizen.bindings import (
    BindingStore, ProjectDeleting, ProjectDisabled, ProjectInventoryConflict,
    ScopeConflict, ScopeNotFound, SideTopicState, validate_channel_database,
)
from netizen.domain import FeishuScope, ScopeKind, MentionContextMode, MessageContextAnchor
from netizen.session_settings import SessionSettings, BindingTurnSettings, BindingTaskFeedback
from netizen.schedules.models import (
    ScheduleConflict, ScheduleError, ScheduleNotFound, ScheduleRequestConflict,
    ScheduleRevisionConflict, ScheduleRule, plan_lifecycle,
)


def stamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


class ScheduleRuleTest(unittest.TestCase):
    def test_daily_dst_skips_missing_time_and_uses_first_repeated_time(self):
        spring = ScheduleRule("daily", "America/New_York", at="02:30")
        self.assertEqual(spring.preview(stamp("2026-03-07T03:00:00-05:00"), 2), (
            stamp("2026-03-09T02:30:00-04:00"), stamp("2026-03-10T02:30:00-04:00"),
        ))
        autumn = ScheduleRule("daily", "America/New_York", at="01:30")
        first, second = autumn.preview(stamp("2026-11-01T00:00:00-04:00"), 2)
        self.assertEqual(first, stamp("2026-11-01T01:30:00-04:00"))
        self.assertEqual(second, stamp("2026-11-02T01:30:00-05:00"))
        self.assertEqual(autumn.next_after(first), second)

    def test_weekly_normalizes_selection_and_interval_does_not_follow_dst(self):
        weekly = ScheduleRule("weekly", "Asia/Shanghai", at="09:00", weekdays=(4, 0, 4))
        self.assertEqual(weekly.weekdays, (0, 4))
        self.assertEqual(weekly.next_after(stamp("2026-09-07T09:00:00+08:00")), stamp("2026-09-11T09:00:00+08:00"))
        start = stamp("2026-03-07T09:00:00-05:00")
        interval = ScheduleRule("interval", "America/New_York", every_minutes=1440, anchor=start)
        self.assertEqual(interval.next_after(start), stamp("2026-03-08T10:00:00-04:00"))
        self.assertEqual(interval.through(start + 86400, start + 86400 * 100000), (start + 86400 * 100000, 100000))

    def test_once_requires_absolute_minute_and_rule_roundtrip(self):
        once = ScheduleRule("once", "Asia/Shanghai", at="2026-09-09T09:00+08:00")
        self.assertEqual(once.preview(stamp("2026-09-08T09:00+08:00")), (stamp("2026-09-09T09:00+08:00"),))
        self.assertIsNone(once.next_after(stamp("2026-09-09T09:00+08:00")))
        self.assertEqual(ScheduleRule.from_dict(once.to_dict()), once)
        for payload in (
            dict(kind="once", timezone="UTC", at="2026-11-01T01:30"),
            dict(kind="once", timezone="UTC", at="2026-11-01T01:30:01Z"),
            dict(kind="weekly", timezone="UTC", at="09:00", weekdays=[]),
            dict(kind="weekly", timezone="UTC", at="09:00", weekdays=[True]),
            dict(kind="daily", timezone="UTC", at="25:00"),
            dict(kind="daily", timezone="bad/timezone", at="09:00"),
            dict(kind="interval", timezone="UTC", every_minutes=0, anchor=100),
            dict(kind="interval", timezone="UTC", every_minutes=True, anchor=100),
            dict(kind="daily", timezone="UTC", at="09:00", cron="* * * * *"),
        ):
            with self.subTest(payload=payload), self.assertRaises(ScheduleError):
                ScheduleRule.from_dict(payload)

    def test_unrepresentable_dates_and_wrong_types_are_schedule_errors(self):
        for payload in (
            dict(kind="interval", timezone="UTC", every_minutes=1, anchor=10 ** 1000),
            dict(kind="interval", timezone="UTC", every_minutes=1, anchor=10 ** 20),
            dict(kind="interval", timezone="UTC", every_minutes=1, anchor=float("inf")),
            dict(kind="interval", timezone="UTC", every_minutes=10 ** 1000, anchor=100),
            dict(kind="interval", timezone="UTC", every_minutes=1, anchor="100"),
            dict(kind="once", timezone="UTC", at=100),
            dict(kind="once", timezone="UTC", at={"hour": 9}),
            dict(kind="daily", timezone="UTC", at=[9, 0]),
            dict(kind="once", timezone="UTC", at="0001-01-01T00:00+01:00"),
            dict(kind="once", timezone="Asia/Shanghai", at="9999-12-31T23:59Z"),
        ):
            with self.subTest(payload=payload), self.assertRaises(ScheduleError):
                ScheduleRule.from_dict(payload)
        long_interval = ScheduleRule("interval", "UTC", every_minutes=3_000_000_000, anchor=100)
        with self.assertRaises(ScheduleError):
            long_interval.preview(100)

    def test_recurring_cutoff_is_inclusive_for_all_rules_and_missed_ranges(self):
        first = stamp("2026-09-07T09:00+08:00")
        cutoff = "2026-09-14T09:00+08:00"
        for rule, expected in (
            (ScheduleRule("daily", "Asia/Shanghai", at="09:00", end_at=cutoff), 8),
            (ScheduleRule("weekly", "Asia/Shanghai", at="09:00", weekdays=(0,), end_at=cutoff), 2),
            (ScheduleRule("interval", "Asia/Shanghai", every_minutes=1440, anchor=first - 86400, end_at=cutoff), 8),
        ):
            with self.subTest(kind=rule.kind):
                self.assertEqual(rule.next_after(first - 1), first)
                self.assertEqual(rule.next_after(stamp(cutoff) - 1), stamp(cutoff))
                self.assertIsNone(rule.next_after(stamp(cutoff)))
                self.assertEqual(rule.through(first, stamp("2027-01-01T00:00Z")), (stamp(cutoff), expected))
                self.assertEqual(rule.through(stamp(cutoff) + 1, stamp(cutoff) + 86400)[1], 0)
                self.assertEqual(len(rule.preview(first - 1, 100)), expected)
                self.assertEqual(ScheduleRule.from_dict(rule.to_dict()), rule)

    def test_recurring_cutoff_rejects_invalid_types_and_once_rules(self):
        base = dict(kind="daily", timezone="UTC", at="09:00")
        for value in (1, {}, "not-a-time", "2026-09-09T09:00", "2026-09-09T09:00:01Z",
                      "2026-09-09T09:00:00.000001Z", "0001-01-01T00:00+01:00"):
            with self.subTest(end_at=value), self.assertRaises(ScheduleError):
                ScheduleRule.from_dict(base | {"end_at": value})
        with self.assertRaises(ScheduleError):
            ScheduleRule("once", "UTC", at="2026-09-08T09:00Z", end_at="2026-09-09T09:00Z")


class ScheduleStorageTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.store = BindingStore(wall_clock=lambda: self.now)
        self.store.register_project(alias="p", cwd="/tmp/project-p")
        self.store.register_project(alias="q", cwd="/tmp/project-q")
        self.schedules = self.store.schedules
        self.rule = ScheduleRule("interval", "UTC", every_minutes=1, anchor=100)
        self.requests = 0

    def tearDown(self):
        self.store.close()

    def request(self):
        self.requests += 1
        return f"request-{self.requests}"

    def create(self, **overrides):
        values = dict(name="daily report", instructions="Inspect the exact repository.", project_alias="p", app_id="app", chat_id="chat", schedule=self.rule, request_id=self.request(), now=self.now)
        values.update(overrides)
        return self.schedules.create(**values)

    def claim(self, plan_id, at=160):
        self.now = at
        return self.schedules.claim_due(plan_id, app_id="app", now=at)

    def binding(self, claim, topic="topic"):
        self.schedules.set_run(claim.run.id, phase="publishing_topic", root_message_id=f"root-{topic}", topic_id=topic, origin_message_id=f"seed-{topic}")
        return self.store.create_scheduled_binding(run_id=claim.run.id, scope=FeishuScope("app", "chat", ScopeKind.TOPIC, topic))

    def lifecycle(self, plan_id, *, now):
        return plan_lifecycle(
            self.schedules.get(plan_id), now=now,
            has_pending=self.schedules.pending_for_plan(plan_id) is not None,
        )

    def test_last_cutoff_claim_waits_for_exact_terminal_before_ending(self):
        plan_id = self.create(schedule=dataclasses.replace(self.rule, end_at="1970-01-01T00:03Z")).plan_id
        claim = self.claim(plan_id)
        plan = self.schedules.get(plan_id)
        self.assertIsNone(plan.next_due_at)
        binding = self.binding(claim)
        self.store.begin_scheduled_initial(claim.run.id, binding.id)
        self.store.mark_scheduled_turn_started(claim.run.id, binding.id, "last-turn")
        self.schedules.set_run(claim.run.id, barrier="unknown")
        state = self.lifecycle(plan_id, now=220)
        self.assertEqual((state.ended, state.has_future, state.can_toggle), (False, False, False))
        self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", ended=False, now=220)], [plan_id])
        self.assertIsNone(self.claim(plan_id, at=220))
        self.store.release_scheduled_initial_turn(binding.id, "different")
        self.assertFalse(self.lifecycle(plan_id, now=220).ended)
        self.store.release_scheduled_initial_turn(binding.id, "last-turn")
        self.assertTrue(self.lifecycle(plan_id, now=220).ended)
        self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", ended=True, now=220)], [plan_id])
        self.assertEqual(len(self.schedules.list_runs(plan_id)), 1)

    def test_pause_and_cutoff_edits_restore_future_without_replaying_processed_time(self):
        rule = ScheduleRule("interval", "UTC", every_minutes=1, anchor=0, end_at="1970-01-01T00:03Z")
        plan_id = self.create(schedule=rule).plan_id
        first = self.claim(plan_id, at=180)
        self.schedules.release(first.run.id)
        self.schedules.update(plan_id, expected_revision=1, request_id=self.request(), changes={"enabled": False}, now=181)
        self.schedules.update(plan_id, expected_revision=2, request_id=self.request(), changes={
            "schedule": dataclasses.replace(rule, end_at="1970-01-01T00:02Z"), "name": "already ended",
        }, now=181)
        self.assertTrue(self.lifecycle(plan_id, now=181).ended)
        with self.assertRaises(ScheduleError):
            self.schedules.update(plan_id, expected_revision=3, request_id=self.request(), changes={"enabled": True}, now=181)
        self.schedules.update(plan_id, expected_revision=3, request_id=self.request(), changes={
            "schedule": dataclasses.replace(rule, end_at="1970-01-01T00:04Z"), "enabled": True,
        }, now=181)
        self.assertEqual(self.schedules.get(plan_id).next_due_at, 240)
        second = self.claim(plan_id, at=240)
        self.schedules.update(plan_id, expected_revision=4, request_id=self.request(), changes={"schedule": rule}, now=241)
        self.assertFalse(self.lifecycle(plan_id, now=241).ended)
        self.schedules.release(second.run.id)
        self.assertTrue(self.lifecycle(plan_id, now=241).ended)
        self.schedules.update(plan_id, expected_revision=5, request_id=self.request(), changes={
            "schedule": dataclasses.replace(rule, end_at="1970-01-01T00:05Z"),
        }, now=100)
        restored = self.schedules.get(plan_id)
        self.assertEqual((restored.processed_through, restored.next_due_at), (240, 300))
        self.assertIsNone(self.claim(plan_id, at=240))
        self.assertEqual(self.claim(plan_id, at=300).run.due_at, 300)

    def test_cutoff_claim_grace_missed_range_and_edit_to_past(self):
        rule = ScheduleRule("interval", "UTC", every_minutes=1, anchor=0, end_at="1970-01-01T00:03Z")
        grace = self.create(schedule=rule).plan_id
        missed = self.create(schedule=rule).plan_id
        paused = self.create(schedule=rule, enabled=False).plan_id
        claim = self.claim(grace, at=240)
        self.assertEqual(claim.run.due_at, 180)
        self.assertIsNone(self.schedules.get(grace).next_due_at)
        self.schedules.release(claim.run.id)
        self.assertTrue(self.lifecycle(grace, now=240).ended)
        self.assertIsNone(self.claim(missed, at=241))
        missed_run = self.schedules.list_runs(missed)[0]
        self.assertEqual((missed_run.due_at, missed_run.missed_from, missed_run.missed_count), (180, 120, 2))
        self.assertTrue(self.lifecycle(paused, now=181).ended)
        unbounded = self.create().plan_id
        self.schedules.update(unbounded, expected_revision=1, request_id=self.request(), changes={"schedule": rule}, now=241)
        self.assertTrue(self.lifecycle(unbounded, now=241).ended)
        self.assertIsNone(self.schedules.get(unbounded).next_due_at)
        with self.assertRaises(ScheduleError):
            self.create(schedule=rule)

    def test_metadata_and_unchanged_form_preserve_due_grace_even_when_project_disabled(self):
        once_rule = ScheduleRule("once", "UTC", at="1970-01-01T00:03Z")
        for rule, due in ((self.rule, 160), (once_rule, 180)):
            with self.subTest(kind=rule.kind):
                self.now = 100
                self.store.set_project_enabled(alias="p", enabled=True, expected_revision=self.store.get_project("p").revision)
                plan_id = self.create(schedule=rule).plan_id
                self.store.set_project_enabled(alias="p", enabled=False, expected_revision=self.store.get_project("p").revision)
                self.schedules.update(plan_id, expected_revision=1, request_id=self.request(), changes={
                    "name": "new name", "instructions": "new instructions", "project_alias": "p",
                    "enabled": True, "schedule": rule,
                    "session_settings": {"progress_card_enabled": True},
                }, now=due + 1)
                current = self.schedules.get(plan_id)
                self.assertEqual((current.next_due_at, current.processed_through), (due, None))
                self.store.set_project_enabled(alias="p", enabled=True, expected_revision=self.store.get_project("p").revision)
                self.assertEqual(self.claim(plan_id, at=due + 1).run.due_at, due)

    def test_cutoff_ended_filter_precedes_pagination_and_reads_do_not_write(self):
        rule = dataclasses.replace(self.rule, end_at="1970-01-01T00:03Z")
        plan_ids = sorted(self.create(schedule=rule).plan_id for _ in range(8))
        ended = []
        for index, plan_id in enumerate(plan_ids):
            claim = self.claim(plan_id)
            if index % 2:
                self.schedules.release(claim.run.id)
                ended.append(plan_id)
        before = tuple(self.store._connection.iterdump())
        changes = self.store._connection.total_changes
        for _ in range(2):
            first = self.schedules.list(app_id="app", ended=True, now=220, limit=2)
            second = self.schedules.list(app_id="app", enabled=True, ended=True, now=220, limit=2, after=first[-1].id)
            self.assertEqual([plan.id for plan in (*first, *second)], ended)
            self.assertTrue(all(not self.lifecycle(plan_id, now=220).has_trigger for plan_id in plan_ids))
        self.assertEqual(self.store._connection.total_changes, changes)
        self.assertEqual(tuple(self.store._connection.iterdump()), before)

    def test_paused_lifecycle_depends_on_the_rule_not_the_enabled_switch(self):
        recurring = self.create(enabled=False).plan_id
        once = self.create(enabled=False, schedule=ScheduleRule("once", "UTC", at="1970-01-01T00:03Z")).plan_id
        for plan_id in (recurring, once):
            before = self.lifecycle(plan_id, now=100)
            self.assertEqual((before.ended, before.has_future, before.has_trigger, before.can_toggle), (False, True, True, True))
        after = self.lifecycle(once, now=181)
        self.assertEqual((after.ended, after.has_future, after.has_trigger, after.can_toggle), (True, False, False, False))
        self.assertFalse(self.lifecycle(recurring, now=181).ended)
        self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", enabled=False, ended=True, now=181)], [once])
        self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", enabled=False, ended=False, now=181)], [recurring])

    def test_due_grace_and_expiry_are_read_only_and_use_one_explicit_clock(self):
        once = self.create(schedule=ScheduleRule("once", "UTC", at="1970-01-01T00:03Z")).plan_id
        before = tuple(self.store._connection.iterdump())
        changes = self.store._connection.total_changes
        for now, expected in ((179, (False, True, True)), (180, (False, False, True)),
                              (240, (False, False, True)), (240.001, (True, False, False))):
            with self.subTest(now=now):
                state = self.lifecycle(once, now=now)
                self.assertEqual((state.ended, state.has_future, state.has_trigger), expected)
                for _ in range(2):
                    self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", ended=state.ended, now=now)], [once])
                    self.assertEqual(self.schedules.list(app_id="app", ended=not state.ended, now=now), ())
        self.now = 240.001
        self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", ended=True)], [once])
        self.assertEqual(self.store._connection.total_changes, changes)
        self.assertEqual(tuple(self.store._connection.iterdump()), before)

    def test_processed_high_water_prevents_clock_rollback_from_reviving_once(self):
        once = self.create(schedule=ScheduleRule("once", "UTC", at="1970-01-01T00:03Z")).plan_id
        claimed = self.claim(once, at=180)
        self.schedules.release(claimed.run.id)
        for now in (100, 180, 181):
            with self.subTest(now=now):
                state = self.lifecycle(once, now=now)
                self.assertEqual((state.ended, state.has_future, state.has_trigger), (True, False, False))
                self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", ended=True, now=now)], [once])
        # Even a newly selected time after the rolled-back wall clock remains
        # consumed when it is earlier than the durable processed boundary.
        with self.assertRaises(ScheduleError):
            self.schedules.update(once, expected_revision=1, request_id=self.request(),
                changes={"schedule": ScheduleRule("once", "UTC", at="1970-01-01T00:02Z")}, now=100)
        self.assertEqual(self.schedules.get(once).processed_through, 180)
        self.assertTrue(self.lifecycle(once, now=100).ended)
        self.assertEqual(self.schedules.list(app_id="app", ended=False, now=100), ())

    def test_last_claim_is_unfinished_through_handoff_and_unknown_until_exact_release(self):
        once = self.create(schedule=ScheduleRule("once", "UTC", at="1970-01-01T00:03Z")).plan_id
        claimed = self.claim(once, at=180)

        def assert_pending():
            state = self.lifecycle(once, now=300)
            self.assertEqual((state.ended, state.has_future, state.has_trigger, state.can_toggle), (False, False, False, False))
            self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", ended=False, now=300)], [once])
            self.assertEqual(self.schedules.list(app_id="app", ended=True, now=300), ())

        assert_pending()
        self.schedules.begin_publication(claimed.run.id)
        assert_pending()
        binding = self.binding(claimed)
        assert_pending()
        self.store.begin_scheduled_initial(claimed.run.id, binding.id)
        assert_pending()
        self.store.mark_scheduled_turn_started(claimed.run.id, binding.id, "initial")
        assert_pending()
        self.schedules.set_run(claimed.run.id, barrier="unknown", error_code="observation_unavailable")
        assert_pending()
        self.store.release_scheduled_initial_turn(binding.id, "different-turn")
        assert_pending()
        self.store.release_scheduled_initial_turn(binding.id, "initial")
        self.assertTrue(self.lifecycle(once, now=300).ended)
        self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", ended=True, now=300)], [once])

    def test_recent_skipped_occurrence_does_not_hide_an_older_pending_run(self):
        plan_id = self.create().plan_id
        claimed = self.claim(plan_id, at=160)
        self.assertIsNone(self.claim(plan_id, at=220))
        self.schedules.update(plan_id, expected_revision=1, request_id=self.request(),
            changes={"schedule": ScheduleRule("once", "UTC", at="1970-01-01T00:04Z")}, now=221)
        self.assertIsNone(self.claim(plan_id, at=240))
        self.assertEqual(self.schedules.list_runs(plan_id, limit=1)[0].error_code, "skipped_busy")
        for barrier in ("held", "unknown"):
            self.schedules.set_run(claimed.run.id, barrier=barrier)
            state = self.lifecycle(plan_id, now=301)
            self.assertEqual((state.ended, state.has_trigger), (False, False))
            self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", ended=False, now=301)], [plan_id])
        self.schedules.release(claimed.run.id)
        self.assertTrue(self.lifecycle(plan_id, now=301).ended)
        self.assertEqual([plan.id for plan in self.schedules.list(app_id="app", ended=True, now=301)], [plan_id])

    def test_ended_and_enabled_filters_apply_before_limit_with_other_filters(self):
        once = ScheduleRule("once", "UTC", at="1970-01-01T00:03Z")
        plan_ids = sorted(self.create(schedule=once).plan_id for _ in range(12))
        expected = {}
        for index, plan_id in enumerate(plan_ids):
            kind = index % 4
            expected[plan_id] = (kind < 2, kind in {0, 2})
            if kind == 0:
                self.schedules.release(self.claim(plan_id, at=180).run.id)
            elif kind == 2:
                self.schedules.update(plan_id, expected_revision=1, request_id=self.request(), changes={"enabled": False}, now=181)
            elif kind == 3:
                self.schedules.update(plan_id, expected_revision=1, request_id=self.request(),
                    changes={"enabled": False, "schedule": self.rule}, now=181)
        for overrides in ({"app_id": "other"}, {"chat_id": "other"}, {"project_alias": "q"}, {"name": "unrelated"}):
            self.create(**overrides)
        for enabled in (None, False, True):
            for ended in (None, False, True):
                with self.subTest(enabled=enabled, ended=ended):
                    wanted = [plan_id for plan_id, state in expected.items()
                              if (enabled is None or state[0] == enabled) and (ended is None or state[1] == ended)]
                    seen, after = [], None
                    while True:
                        page = self.schedules.list(app_id="app", chat_id="chat", project_alias="p", name="daily",
                            enabled=enabled, ended=ended, now=181, after=after, limit=2)
                        self.assertEqual([plan.id for plan in page], wanted[len(seen):len(seen) + 2])
                        seen.extend(plan.id for plan in page)
                        if len(page) < 2:
                            break
                        after = page[-1].id
                    self.assertEqual(seen, wanted)

    def test_ended_filter_rejects_non_boolean_values(self):
        for invalid in (0, 1, "true", "false", [], {}):
            with self.subTest(ended=invalid), self.assertRaises(ScheduleError):
                self.schedules.list(app_id="app", ended=invalid)

    def test_scheduled_binding_rejects_terminal_side_topic_or_root_without_mutation(self):
        parent = self.store.create_channel_binding(
            scope=FeishuScope("app", "chat", ScopeKind.GROUP),
            project_alias="p", creator_id="user",
        )
        for match in ("topic", "root"):
            with self.subTest(identity=match):
                side = self.store.create_side_topic(
                    app_id="app", chat_id="chat", source_message_id=f"source-{match}",
                    parent_binding_id=parent.id, creator_id="user", requires_mention=True,
                )
                self.store.set_side_topic_root(side.id, f"side-root-{match}")
                self.store.open_side_topic(side.id, f"side-topic-{match}")
                self.store.transition_side_topic(side.id, SideTopicState.CLOSED)
                claim = self.claim(self.create().plan_id, at=self.now + 60)
                topic = f"side-topic-{match}" if match == "topic" else "different-topic"
                root = f"side-root-{match}" if match == "root" else "different-root"
                scope = FeishuScope("app", "chat", ScopeKind.TOPIC, topic)
                self.schedules.set_run(
                    claim.run.id, phase="publishing_topic", root_message_id=root,
                    topic_id=topic, origin_message_id=f"seed-{match}",
                )
                with self.assertRaises(ScheduleConflict):
                    self.store.create_scheduled_binding(run_id=claim.run.id, scope=scope)
                with self.assertRaises(ScopeNotFound):
                    self.store.get_scope(scope.key)
                self.assertEqual(self.store.list_bindings(scope.key), [])
                self.assertIsNone(self.schedules.get_run(claim.run.id).binding_id)
                self.assertEqual(self.store.get_side_topic(side.id).state, SideTopicState.CLOSED)

    def test_invalid_write_is_atomic_and_idempotency_retains_original_identity(self):
        with self.assertRaises(ScheduleError):
            self.create(instructions="", request_id="failed")
        self.assertEqual(self.schedules.list(app_id="app"), ())
        first = self.create(request_id="stable")
        repeated = self.create(request_id="stable")
        self.assertEqual(repeated.plan_id, first.plan_id)
        self.assertTrue(repeated.replayed)
        with self.assertRaises(ScheduleRequestConflict):
            self.create(name="changed", request_id="stable")
        self.schedules.update(first.plan_id, expected_revision=1, request_id="edit", changes={"name": "new name"})
        self.assertEqual(self.create(request_id="stable").revision, 1)
        with self.assertRaises(ScheduleRevisionConflict):
            self.schedules.delete(first.plan_id, expected_revision=1, request_id="delete")
        self.schedules.delete(first.plan_id, expected_revision=2, request_id="delete")
        replay = self.schedules.delete(first.plan_id, expected_revision=2, request_id="delete")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.revision, 3)
        tombstone = self.schedules.get(first.plan_id, include_deleted=True)
        self.assertTrue(tombstone.deleted)
        self.assertEqual(tombstone.instructions, "")
        self.assertIsNone(tombstone.schedule)
        with self.assertRaises(ScheduleNotFound):
            self.schedules.update(first.plan_id, expected_revision=3, request_id="revive", changes={"enabled": True})

    def test_preview_overflow_rejects_create_and_update_without_mutation(self):
        invalid = ScheduleRule("interval", "UTC", every_minutes=3_000_000_000, anchor=100)
        with self.assertRaises(ScheduleError):
            self.create(schedule=invalid, request_id="invalid-create")
        self.assertEqual(self.schedules.list(app_id="app"), ())
        plan = self.create()
        before = self.schedules.get(plan.plan_id)
        with self.assertRaises(ScheduleError):
            self.schedules.update(plan.plan_id, expected_revision=1, request_id="invalid-update", changes={"schedule": invalid})
        self.assertEqual(self.schedules.get(plan.plan_id), before)
        self.assertEqual(self.store._connection.execute("SELECT COUNT(*) FROM schedule_requests").fetchone()[0], 1)

    def test_instruction_byte_limit_counts_chinese_and_json_escaping(self):
        for accepted, rejected in (
            ("中" * 16383 + "a", "中" * 16383 + "ab"),
            ('"' * 24575, '"' * 24576),
        ):
            with self.subTest(length=len(accepted)):
                plan = self.create(instructions=accepted)
                self.assertEqual(self.schedules.get(plan.plan_id).instructions, accepted)
                with self.assertRaisesRegex(ScheduleError, "执行指令过长，请缩短或引用文件/文档"):
                    self.create(instructions=rejected)
                with self.assertRaises(ScheduleError):
                    self.schedules.update(plan.plan_id, expected_revision=1, request_id=self.request(), changes={"instructions": rejected})
                self.assertEqual(self.schedules.get(plan.plan_id).revision, 1)
        ascii_plan = self.create(instructions="a" * 32000)
        self.assertEqual(len(self.schedules.get(ascii_plan.plan_id).instructions), 32000)

    def test_canonical_raw_request_allows_default_anchor_retry_and_expires_after_seven_days(self):
        payload = {"mode": "create", "schedule": {"kind": "interval", "every_minutes": 1}}
        first = self.create(request_id="raw", request_payload=payload)
        self.now = 200
        lookup = self.schedules.lookup_request("raw", "create", payload)
        self.assertEqual(lookup.plan_id, first.plan_id)
        retry = self.create(request_id="raw", request_payload=payload, schedule=dataclasses.replace(self.rule, anchor=200))
        self.assertTrue(retry.replayed)
        self.now = 100 + 7 * 86400
        self.assertIsNone(self.schedules.lookup_request("raw", "create", payload))
        for invalid in (float("inf"), float("nan"), object()):
            with self.subTest(invalid=invalid), self.assertRaises(ScheduleError):
                self.schedules.lookup_request("invalid", "create", {"anchor": invalid})
        self.assertEqual(self.store._connection.execute("SELECT COUNT(*) FROM schedule_requests WHERE request_id='invalid'").fetchone()[0], 0)

    def test_claim_freezes_definition_across_edit_pause_and_delete(self):
        plan = self.create()
        claim = self.claim(plan.plan_id)
        self.schedules.update(plan.plan_id, expected_revision=1, request_id=self.request(), changes={"instructions": "new task", "project_alias": "q", "enabled": False})
        self.schedules.delete(plan.plan_id, expected_revision=2, request_id=self.request())
        self.assertEqual(claim.plan.instructions, "Inspect the exact repository.")
        self.assertEqual(claim.plan.project_alias, "p")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            claim.plan.instructions = "rewrite"
        binding = self.binding(claim)
        self.assertEqual(binding.project_alias, "p")
        self.store.begin_scheduled_initial(claim.run.id, binding.id)

    def test_claimed_session_settings_are_independent_of_later_plan_and_binding_edits(self):
        copied = SessionSettings(
            BindingTurnSettings("model-a", "high", "priority"),
            BindingTaskFeedback(True, True), MentionContextMode.CATCH_UP,
        )
        plan = self.create(session_settings=copied)
        claim = self.claim(plan.plan_id)
        self.schedules.update(plan.plan_id, expected_revision=1, request_id=self.request(), changes={
            "session_settings": {"turn_settings": None, "reaction_pulse_enabled": False, "message_context_mode": "current-only"},
        })
        changed = self.schedules.get(plan.plan_id)
        self.assertIsNone(changed.session_settings.turn_settings)
        self.assertEqual(changed.session_settings.task_feedback, BindingTaskFeedback(False, True))
        self.assertEqual(claim.plan.session_settings, copied)
        self.schedules.set_run(claim.run.id, phase="publishing_topic", root_message_id="root", topic_id="topic", origin_message_id="seed")
        anchor = MessageContextAnchor("seed", 123456)
        binding = self.store.create_scheduled_binding(
            run_id=claim.run.id, scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "topic"),
            session_settings=claim.plan.session_settings, context_anchor=anchor,
        )
        self.assertEqual(SessionSettings.from_binding(binding), copied)
        self.assertEqual(binding.context_anchor, anchor)
        self.store.set_turn_settings(binding_id=binding.id, expected_revision=binding.settings_revision, settings=None)
        self.assertEqual(self.schedules.get(plan.plan_id).session_settings, changed.session_settings)

    def test_settings_validation_is_atomic_and_tombstone_clears_saved_intent(self):
        plan = self.create(session_settings=SessionSettings(task_feedback=BindingTaskFeedback(True, True)))
        with self.assertRaises(ScheduleError):
            self.schedules.update(plan.plan_id, expected_revision=1, request_id=self.request(), changes={"session_settings": {"progress_card_enabled": 1}})
        self.assertEqual(self.schedules.get(plan.plan_id).revision, 1)
        self.schedules.delete(plan.plan_id, expected_revision=1, request_id=self.request())
        self.assertIsNone(self.store._connection.execute("SELECT session_settings_json FROM schedule_plans WHERE plan_id=?", (plan.plan_id,)).fetchone()[0])
        self.assertEqual(self.schedules.get(plan.plan_id, include_deleted=True).session_settings, SessionSettings())

    def test_catch_up_execution_requires_its_own_exact_anchor(self):
        settings = SessionSettings(message_context_mode=MentionContextMode.CATCH_UP)
        claim = self.claim(self.create(session_settings=settings).plan_id)
        self.schedules.set_run(claim.run.id, phase="publishing_topic", root_message_id="root", topic_id="topic", origin_message_id="seed")
        with self.assertRaises(ValueError):
            self.store.create_scheduled_binding(
                run_id=claim.run.id, scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "topic"),
                session_settings=claim.plan.session_settings,
            )
        self.assertIsNone(self.schedules.get_run(claim.run.id).binding_id)

    def test_busy_unknown_and_other_plans_are_independent(self):
        plan, other = self.create(), self.create()
        first = self.claim(plan.plan_id)
        self.assertIsNotNone(self.claim(other.plan_id))
        self.assertIsNone(self.claim(plan.plan_id, 220))
        self.assertEqual(self.schedules.list_runs(plan.plan_id)[0].error_code, "skipped_busy")
        self.schedules.set_run(first.run.id, barrier="unknown", error_code="turn_start_unknown")
        self.assertIsNone(self.claim(plan.plan_id, 280))
        self.assertEqual(self.schedules.list_runs(plan.plan_id)[0].error_code, "blocked_unknown")
        self.schedules.update(plan.plan_id, expected_revision=1, request_id=self.request(), changes={"enabled": False})
        self.schedules.update(plan.plan_id, expected_revision=2, request_id=self.request(), changes={"enabled": True})
        self.assertEqual(self.schedules.pending_for_plan(plan.plan_id).id, first.run.id)

    def test_clock_jump_compacts_missed_range_and_only_claims_latest_grace(self):
        plan = self.create()
        claim = self.claim(plan.plan_id, 600_105)
        self.assertEqual(claim.run.due_at, 600_100)
        records = self.schedules.list_runs(plan.plan_id)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1].error_code, "missed")
        self.assertEqual(records[1].missed_count, 9999)
        self.assertEqual(records[1].missed_from, 160)
        self.assertEqual(self.schedules.get(plan.plan_id).processed_through, 600_100)

    def test_restart_does_not_backfill_even_inside_grace_and_once_is_exhausted(self):
        plan = self.create()
        self.assertIsNone(self.schedules.claim_due(plan.plan_id, app_id="app", now=165, recover=True))
        self.assertEqual(self.schedules.get(plan.plan_id).next_due_at, 220)
        self.assertEqual(self.schedules.list_runs(plan.plan_id)[0].error_code, "missed")
        once = self.create(schedule=ScheduleRule("once", "UTC", at="1970-01-01T00:03Z"))
        self.assertIsNone(self.schedules.claim_due(once.plan_id, app_id="app", now=241))
        self.assertIsNone(self.schedules.get(once.plan_id).next_due_at)
        self.schedules.update(once.plan_id, expected_revision=1, request_id=self.request(), changes={"enabled": False}, now=241)
        with self.assertRaises(ScheduleError):
            self.schedules.update(once.plan_id, expected_revision=2, request_id=self.request(), changes={"enabled": True}, now=241)

    def test_high_water_survives_pruning_edits_and_clock_rollback(self):
        plan = self.create()
        for index in range(105):
            claim = self.claim(plan.plan_id, 160 + 60 * index)
            self.schedules.release(claim.run.id, error_code="test_not_started")
        self.assertEqual(len(self.schedules.list_runs(plan.plan_id, limit=101)), 100)
        high = 160 + 60 * 104
        self.now = 100
        self.schedules.update(plan.plan_id, expected_revision=1, request_id=self.request(), changes={"name": "clock moved back"})
        changed = self.schedules.get(plan.plan_id)
        self.assertEqual(changed.processed_through, high)
        self.assertEqual(changed.next_due_at, high + 60)
        self.assertIsNone(self.claim(plan.plan_id, 160))
        self.assertIsNotNone(self.claim(plan.plan_id, high + 60))

    def test_exhausted_once_metadata_edit_preserves_exhaustion_and_enable_requires_future(self):
        once_rule = ScheduleRule("once", "UTC", at="1970-01-01T00:03Z")
        plan = self.create(schedule=once_rule)
        self.schedules.claim_due(plan.plan_id, app_id="app", now=241)
        self.now = 241
        self.schedules.update(plan.plan_id, expected_revision=1, request_id=self.request(), changes={"name": "renamed", "instructions": "new description"})
        updated = self.schedules.get(plan.plan_id)
        self.assertEqual((updated.name, updated.instructions), ("renamed", "new description"))
        self.assertTrue(updated.enabled)
        self.assertEqual(updated.processed_through, 180)
        self.assertIsNone(updated.next_due_at)
        self.schedules.update(plan.plan_id, expected_revision=2, request_id=self.request(), changes={
            "enabled": True, "schedule": once_rule, "name": "full form rename",
        })
        self.assertIsNone(self.schedules.get(plan.plan_id).next_due_at)
        self.schedules.update(plan.plan_id, expected_revision=3, request_id=self.request(), changes={"enabled": False})
        self.assertFalse(self.schedules.get(plan.plan_id).enabled)
        with self.assertRaises(ScheduleError):
            self.schedules.update(plan.plan_id, expected_revision=4, request_id=self.request(), changes={"enabled": True})

    def test_pending_record_not_pruned_and_unique_occurrence_ignores_revision(self):
        plan = self.create()
        first = self.claim(plan.plan_id)
        for index in range(105):
            self.claim(plan.plan_id, 220 + 60 * index)
        self.assertEqual(self.schedules.get_run(first.run.id).barrier, "held")
        self.assertEqual(len(self.schedules.list_runs(plan.plan_id, limit=102)), 101)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._transaction():
                self.schedules._insert_run(dataclasses.replace(first.plan, revision=9), 160, self.now, reason="missed")

    def test_publishing_unknown_preserves_external_evidence_after_barrier_release(self):
        plan = self.create()
        first = self.claim(plan.plan_id)
        self.schedules.set_run(first.run.id, phase="publishing_topic", root_message_id="uncertain-root")
        self.schedules.release(first.run.id, error_code="publishing_unknown")
        for index in range(105):
            claim = self.claim(plan.plan_id, 220 + 60 * index)
            self.schedules.release(claim.run.id, error_code="test_no_start")
        self.assertEqual(self.schedules.get_run(first.run.id).root_message_id, "uncertain-root")
        snapshot = self.store.preview_project_delete("p")
        self.assertEqual([run.id for run in snapshot.scheduled_runs], [first.run.id])
        reserved = self.store.begin_project_delete(alias="p", expected_revision=snapshot.project.revision, expected_inventory_fingerprint=snapshot.fingerprint)
        with self.assertRaises(ProjectInventoryConflict):
            self.store.finish_project_delete(alias="p", expected_revision=reserved.project.revision, expected_inventory_fingerprint=reserved.fingerprint)

    def test_busy_history_does_not_prune_first_turn_before_final_delivery_receipt(self):
        plan = self.create()
        first = self.claim(plan.plan_id)
        binding = self.binding(first)
        self.store.begin_scheduled_initial(first.run.id, binding.id)
        self.store.mark_scheduled_turn_started(first.run.id, binding.id, "initial")
        for index in range(105):
            self.claim(plan.plan_id, 220 + 60 * index)
        self.store.release_scheduled_initial_turn(binding.id, "initial")
        self.assertEqual(self.schedules.get_run(first.run.id).barrier, "released")
        receipt = self.schedules.set_run(first.run.id, delivery_state="sent")
        self.assertEqual(receipt.delivery_state, "sent")
        with self.assertRaises(ScheduleNotFound):
            self.schedules.get_run(first.run.id)
        self.assertEqual(len(self.schedules.list_runs(plan.plan_id, limit=101)), 100)

    def test_restart_marks_unfinished_receipt_unknown_without_recreating_delivery(self):
        plan = self.create()
        first = self.claim(plan.plan_id)
        binding = self.binding(first)
        self.store.begin_scheduled_initial(first.run.id, binding.id)
        self.store.mark_scheduled_turn_started(first.run.id, binding.id, "initial")
        self.store.release_scheduled_initial_turn(binding.id, "initial")
        self.schedules.abandon_pending_deliveries()
        run = self.schedules.get_run(first.run.id)
        self.assertEqual((run.barrier, run.delivery_state), ("released", "unknown"))

    def test_atomic_binding_does_not_change_parent_pointer_and_rejects_topic_race(self):
        parent_scope = FeishuScope("app", "chat", ScopeKind.GROUP)
        parent = self.store.create_channel_binding(scope=parent_scope, project_alias="p", creator_id="user")
        plan = self.create()
        claim = self.claim(plan.plan_id)
        binding = self.binding(claim)
        self.assertEqual(self.store.active_binding(parent_scope.key).id, parent.id)
        self.assertEqual(self.schedules.get_run(claim.run.id).binding_id, binding.id)
        self.assertFalse(binding.task_feedback.reaction_pulse_enabled)
        self.assertIsNone(binding.turn_settings)
        self.assertEqual(binding.creator_id, "scheduled_plan")
        self.schedules.release(claim.run.id)
        second = self.claim(plan.plan_id, 220)
        self.schedules.set_run(second.run.id, phase="publishing_topic", root_message_id="other-root", topic_id="occupied-topic", origin_message_id="other-seed")
        occupied = self.store.create_channel_binding(scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "occupied-topic"), project_alias="p", creator_id="user")
        with self.assertRaises(ScopeConflict):
            self.store.create_scheduled_binding(run_id=second.run.id, scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "occupied-topic"))
        self.assertIsNone(self.schedules.get_run(second.run.id).binding_id)
        self.assertEqual(self.store.active_binding(binding.scope_key).id, binding.id)
        self.assertEqual(self.store.active_binding(occupied.scope_key).id, occupied.id)

    def test_initial_reservation_only_starts_once_and_terminal_cas_is_monotonic(self):
        plan = self.create()
        claim = self.claim(plan.plan_id)
        binding = self.binding(claim)
        self.assertEqual(self.store.scheduled_initial_reservation(binding.id), claim.run.id)
        self.store.begin_scheduled_initial(claim.run.id, binding.id)
        with self.assertRaises(ScheduleConflict):
            self.store.begin_scheduled_initial(claim.run.id, binding.id)
        self.store.assign_native_thread_id(binding.id, "native-thread")
        self.assertEqual(self.store.find_by_native_thread_id("native-thread").id, binding.id)
        self.store.release_scheduled_initial_turn(binding.id, "first-turn")
        self.store.mark_scheduled_turn_started(claim.run.id, binding.id, "first-turn")
        run = self.schedules.get_run(claim.run.id)
        self.assertEqual((run.phase, run.barrier), ("released", "released"))
        self.assertEqual(run.initial_turn_id, "first-turn")
        self.assertIsNone(self.store.scheduled_initial_reservation(binding.id))
        with self.assertRaises(ScheduleConflict):
            self.store.mark_scheduled_turn_started(claim.run.id, binding.id, "different-turn")

    def test_only_exact_initial_turn_releases_and_later_conversation_is_irrelevant(self):
        plan = self.create()
        claim = self.claim(plan.plan_id)
        binding = self.binding(claim)
        self.store.begin_scheduled_initial(claim.run.id, binding.id)
        self.store.mark_scheduled_turn_started(claim.run.id, binding.id, "initial")
        self.store.release_scheduled_initial_turn(binding.id, "unrelated")
        self.assertEqual(self.schedules.get_run(claim.run.id).barrier, "held")
        self.store.release_scheduled_initial_turn(binding.id, "initial")
        self.assertIsNotNone(self.claim(plan.plan_id, 220))
        self.store.release_scheduled_initial_turn(binding.id, "later-manual-turn")
        self.assertEqual(self.schedules.get_run(claim.run.id).initial_turn_id, "initial")

    def test_deactivate_is_not_archive_and_delete_atomically_marks_removed(self):
        plan = self.create()
        claim = self.claim(plan.plan_id)
        binding = self.binding(claim)
        self.store.deactivate(scope_key=binding.scope_key, binding_id=binding.id)
        self.assertEqual(self.schedules.get_run(claim.run.id).barrier, "held")
        self.store.archive_binding(binding.id)
        self.assertEqual(self.schedules.get_run(claim.run.id).barrier, "released")
        self.assertFalse(self.schedules.get_run(claim.run.id).binding_removed)
        self.store.delete_binding(binding.id)
        run = self.schedules.get_run(claim.run.id)
        self.assertTrue(run.binding_removed)
        self.assertEqual(run.binding_id, binding.id)

    def test_root_refs_are_write_once_and_pending_routes_end_after_handoff(self):
        plan = self.create()
        claim = self.claim(plan.plan_id)
        root_uuid, seed_uuid = claim.run.root_uuid, claim.run.seed_uuid
        self.assertNotEqual(root_uuid, seed_uuid)
        binding = self.binding(claim)
        self.assertEqual(self.schedules.get_run(claim.run.id).root_uuid, root_uuid)
        self.assertEqual(self.schedules.pending_route(app_id="app", chat_id="chat", root_message_id="root-topic").id, claim.run.id)
        with self.assertRaises(ScheduleConflict):
            self.schedules.set_run(claim.run.id, root_message_id="replacement")
        another = self.create()
        another_claim = self.claim(another.plan_id, 220)
        with self.assertRaises(ScheduleConflict):
            self.schedules.set_run(another_claim.run.id, phase="publishing_topic", root_message_id="root-topic")
        self.assertIsNone(self.schedules.get_run(another_claim.run.id).root_message_id)
        self.store.begin_scheduled_initial(claim.run.id, binding.id)
        self.store.mark_scheduled_turn_started(claim.run.id, binding.id, "initial")
        self.assertIsNone(self.schedules.pending_route(app_id="app", chat_id="chat", topic_id="topic"))

    def test_publication_reservation_rejects_duplicate_dispatch_without_releasing_owner(self):
        plan = self.create()
        claim = self.claim(plan.plan_id)
        self.schedules.begin_publication(claim.run.id)
        with self.assertRaises(ScheduleConflict):
            self.schedules.begin_publication(claim.run.id)
        run = self.schedules.get_run(claim.run.id)
        self.assertEqual((run.phase, run.barrier), ("publishing_topic", "held"))
        self.assertEqual(run.root_uuid, claim.run.root_uuid)

    def test_project_disable_preserves_intent_and_allows_pause(self):
        plan = self.create()
        project = self.store.get_project("p")
        self.store.set_project_enabled(alias="p", enabled=False, expected_revision=project.revision)
        self.assertIsNone(self.claim(plan.plan_id))
        self.assertEqual(self.schedules.list_runs(plan.plan_id)[0].error_code, "project_disabled")
        self.assertTrue(self.schedules.get(plan.plan_id).enabled)
        self.schedules.update(plan.plan_id, expected_revision=1, request_id=self.request(), changes={"enabled": False})
        with self.assertRaises(ProjectDisabled):
            self.schedules.update(plan.plan_id, expected_revision=2, request_id=self.request(), changes={"enabled": True})

    def test_project_delete_checks_plan_revision_and_keeps_pending_inventory(self):
        plan = self.create()
        stale = self.store.preview_project_delete("p")
        self.schedules.update(plan.plan_id, expected_revision=1, request_id=self.request(), changes={"name": "edited"})
        with self.assertRaises(ProjectInventoryConflict):
            self.store.begin_project_delete(alias="p", expected_revision=stale.project.revision, expected_inventory_fingerprint=stale.fingerprint)
        claim = self.claim(plan.plan_id)
        snapshot = self.store.preview_project_delete("p")
        self.assertEqual(snapshot.scheduled_plans, ((plan.plan_id, 2),))
        self.assertEqual(snapshot.scheduled_runs[0].id, claim.run.id)
        reserved = self.store.begin_project_delete(alias="p", expected_revision=snapshot.project.revision, expected_inventory_fingerprint=snapshot.fingerprint)
        self.assertTrue(self.schedules.get(plan.plan_id, include_deleted=True).deleted)
        with self.assertRaises(ProjectInventoryConflict):
            self.store.finish_project_delete(alias="p", expected_revision=reserved.project.revision, expected_inventory_fingerprint=reserved.fingerprint)
        self.assertEqual(self.schedules.pending_runs(project_alias="p")[0].id, claim.run.id)
        with self.assertRaises(ProjectDeleting):
            self.binding(claim)
        self.schedules.release(claim.run.id, error_code="project_deleted_before_start")
        self.store.finish_project_delete(alias="p", expected_revision=reserved.project.revision, expected_inventory_fingerprint=reserved.fingerprint)
        self.store.register_project(alias="p", cwd="/tmp/new-project")
        self.assertTrue(self.schedules.get(plan.plan_id, include_deleted=True).deleted)
        with self.assertRaises(ScheduleNotFound):
            self.schedules.update(plan.plan_id, expected_revision=3, request_id=self.request(), changes={"enabled": True})

    def test_app_namespace_filters_and_keyset_pagination(self):
        first = self.create(name="100% literal _ report")
        second = self.create(name="other")
        self.create(app_id="other-app")
        records = self.schedules.list(app_id="app", limit=1)
        more = self.schedules.list(app_id="app", after=records[0].id)
        self.assertEqual({records[0].id, more[0].id}, {first.plan_id, second.plan_id})
        self.assertEqual(self.schedules.list(app_id="app", name="% literal _")[0].id, first.plan_id)
        self.assertIsNone(self.schedules.claim_due(first.plan_id, app_id="other-app", now=160))


class ScheduleMigrationTest(unittest.TestCase):
    def test_cutoff_survives_history_pruning_and_file_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel.db"
            store = BindingStore(path, wall_clock=lambda: 100)
            store.register_project(alias="p", cwd="/tmp/project")
            rule = ScheduleRule("interval", "UTC", every_minutes=1, anchor=100, end_at="1970-01-01T01:48Z")
            plan_id = store.schedules.create(name="limited", instructions="run", project_alias="p",
                app_id="app", chat_id="chat", schedule=rule, request_id="create").plan_id
            for index in range(105):
                claim = store.schedules.claim_due(plan_id, app_id="app", now=160 + index * 60)
                store.schedules.release(claim.run.id, error_code="publication_failed")
            self.assertEqual(len(store.schedules.list_runs(plan_id, limit=1000)), 100)
            store.close()
            restarted = BindingStore(path, wall_clock=lambda: 6460)
            try:
                saved = restarted.schedules.get(plan_id)
                self.assertEqual(saved.next_due_at, 6460)
                self.assertEqual(saved.schedule.preview(6400), (6460,))
                last = restarted.schedules.claim_due(plan_id, app_id="app")
                self.assertIsNone(restarted.schedules.get(plan_id).next_due_at)
                restarted.schedules.release(last.run.id)
                self.assertIsNone(restarted.schedules.claim_due(plan_id, app_id="app", now=6520))
            finally:
                restarted.close()

    def test_current_schema_missing_columns_indexes_or_routes_rejects_without_repair(self):
        mutations = (
            ("ALTER TABLE schedule_plans DROP COLUMN chat_id",),
            ("DROP INDEX schedule_plans_due", "ALTER TABLE schedule_plans DROP COLUMN next_due_at"),
            ("ALTER TABLE schedule_runs DROP COLUMN origin_message_id",),
            ("ALTER TABLE schedule_requests DROP COLUMN operation",),
            ("DROP INDEX schedule_runs_barrier",),
            ("DROP INDEX schedule_runs_root",),
            ("DROP TABLE side_topics",),
            ("CREATE TABLE requests_without_key AS SELECT * FROM schedule_requests", "DROP TABLE schedule_requests", "ALTER TABLE requests_without_key RENAME TO schedule_requests"),
            ("DROP INDEX schedule_runs_barrier", "CREATE UNIQUE INDEX schedule_runs_barrier ON schedule_runs(plan_id) WHERE barrier='held'"),
            ("ALTER TABLE schedule_plans DROP COLUMN session_settings_json",),
        )
        for statements in mutations:
            with self.subTest(statements=statements), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "channel.db"
                BindingStore(path).close()
                with sqlite3.connect(path) as db:
                    for statement in statements:
                        db.execute(statement)
                before = path.read_bytes()
                with self.assertRaises(RuntimeError):
                    validate_channel_database(path)
                self.assertEqual(path.read_bytes(), before)
                with self.assertRaises(RuntimeError):
                    BindingStore(path)
                self.assertEqual(path.read_bytes(), before)

    def test_file_store_restart_preserves_plan_cursor_and_exact_dispatch_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel.db"
            store = BindingStore(path, wall_clock=lambda: 100)
            store.register_project(alias="p", cwd="/tmp/project")
            settings = SessionSettings(BindingTurnSettings("m", "high", "default"), BindingTaskFeedback(True, False), MentionContextMode.CATCH_UP)
            plan = store.schedules.create(name="report", instructions="task definition", project_alias="p", app_id="app", chat_id="chat", schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100), request_id="create", session_settings=settings)
            claim = store.schedules.claim_due(plan.plan_id, app_id="app", now=160)
            store.schedules.begin_publication(claim.run.id)
            store.schedules.set_run(claim.run.id, root_message_id="known-root")
            store.close()
            restarted = BindingStore(path, wall_clock=lambda: 220)
            try:
                saved = restarted.schedules.get(plan.plan_id)
                self.assertEqual((saved.instructions, saved.next_due_at, saved.processed_through), ("task definition", 220, 160))
                self.assertEqual(saved.session_settings, settings)
                run = restarted.schedules.get_run(claim.run.id)
                self.assertEqual((run.phase, run.root_message_id, run.root_uuid), ("publishing_topic", "known-root", claim.run.root_uuid))
                self.assertIsNone(restarted.schedules.claim_due(plan.plan_id, app_id="app", recover=True))
                self.assertEqual(restarted.schedules.get(plan.plan_id).processed_through, 220)
                self.assertEqual(restarted.schedules.get_run(claim.run.id).barrier, "held")
            finally:
                restarted.close()


if __name__ == "__main__":
    unittest.main()
