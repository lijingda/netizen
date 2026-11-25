"""Run the live-session safety oracle against single changes in disposable module copies."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
ORDINARY = "tests.test_claude_ordinary.ClaudeOrdinaryTests."
CHANNEL = "tests.test_claude_channel.OwnerChannelTests."
EXPLORE = ORDINARY + "test_explore_ordered_task_cuts"
STOP = CHANNEL + "test_stop_all_uses_only_verified_membership_and_status_buttons_recheck_revision"
WIRE = CHANNEL + "test_explore_owned_answers_on_the_channel"
WIRE_SAFETY = CHANNEL + "test_only_allowlisted_activity_reaches_either_reply_transport"
CANCEL = ORDINARY + "test_send_cancelled_during_shutdown_drains_following_body"

# Each change represents one incorrect decision, with an independently observed consequence.
MUTANTS = (
    ("retained client allows a rename", "runtime/claude_ordinary",
     "if execution is not None and not execution.close_finished:\n                if execution.admissible",
     "if execution is not None and not execution.close_finished and not execution.admissible:\n                if execution.admissible",
     ORDINARY + "test_rename_rejects_opening_and_retained_client_without_stopping_it"),
    ("fork accepts a running background task", "runtime/claude_ordinary",
     "if execution.live.tasks:\n                raise OrdinaryInputRejected", "if False:\n                raise OrdinaryInputRejected",
     ORDINARY + "test_fork_requires_idle_without_tasks_and_detects_native_wake_during_copy"),
    ("fork consults settled input and stop history", "runtime/claude_ordinary",
     "or execution.awaiting_task_stop):", "or execution.unwritten or execution.stopping or execution.awaiting_task_stop):",
     ORDINARY + "test_can_fork_after_a_cron_wake_rolls_back_a_reserved_input"),
    ("child cwd verification skipped", "runtime/claude_ordinary", "if verified != cwd:", "if False:",
     ORDINARY + "test_fork_validates_unscoped_source_and_child_and_never_repeats_copy"),
    ("retry a fork with unknown result", "runtime/claude_client",
     "return result.session_id", "except OSError:\n            result = fork_session(session_id, directory=str(cwd), title=title)\n        return result.session_id",
     ORDINARY + "test_fork_validates_unscoped_source_and_child_and_never_repeats_copy"),
    ("final text skips the logical-end wait", "channel/presentation",
     "await reply.mention_decided.wait()", "pass",
     CHANNEL + "test_final_post_mention_at_the_logical_terminal_boundary"),
    ("inline mention also triggers a separate reminder", "channel/presentation",
     "event.mention_user_id and not reply.mention_attempted and display.publishable()",
     "event.mention_user_id and display.publishable()",
     CHANNEL + "test_final_post_mention_at_the_logical_terminal_boundary"),
    ("separate reminder leaves the mention unreserved", "channel/presentation",
     "if anchor:\n                    reply.mention_attempted = True", "if anchor:",
     CHANNEL + "test_attributed_wake_queued_behind_separate_reminder_mentions_once"),
    ("child tools become parent operation rows", "runtime/claude_activity",
     "if parent is not None:", "if False:",
     "tests.test_claude_activity.ClaudeToolsTest.test_child_frames_and_late_children_do_not_change_a_successor"),
    ("tool description becomes public Activity", "runtime/claude_activity",
     "operation_summary(label, *", 'operation_summary(label, args.get("description", ""), *',
     WIRE_SAFETY),
    ("Bash description added to the allowlist", "runtime/claude_activity",
     '("command",)),', '("command", "description")),', WIRE_SAFETY),
    ("Bash completion label taken from stdout", "runtime/claude_activity",
     "replace(row, status=status, event_timestamp_ms=now)",
     'replace(row, status=status, event_timestamp_ms=now, text=payload["stdout"] '
     'if name == "Bash" and isinstance(payload, dict) and "stdout" in payload else row.text)', WIRE_SAFETY),
    ("resumed client discards observed task deltas", "runtime/claude_ordinary",
     "self.live = LiveSession(self.session, self.fail)",
     "self.live = LiveSession(self.session, self.fail)\n        if self.binding.native_thread_id: self.checklist.rows = None",
     ORDINARY + "test_resumed_client_tracks_new_steps_and_late_child_only_invalidates_checklist"),
    ("requested path substitutes for missing effective path", "runtime/claude_activity",
     "elif path := _output(name, payload):",
     'elif (path := _output(name, payload)) or (name == "Write" and row and row.text and " · " in row.text'
     ' and (path := OutputPath(row.text.partition(" · ")[2]))):',
     "tests.test_claude_activity.ClaudeToolsTest.test_top_level_start_end_and_effective_files"),
    ("display churn changes the admission and stop revision", "runtime/claude_ordinary",
     "signature = (execution.input_id, execution.human.input_id,", "signature = (execution.display_revision, execution.input_id, execution.human.input_id,",
     ORDINARY + "test_display_churn_preserves_control_revision_and_stop_target"),
    ("notify only when every item is unconfirmed", "channel/physical_delivery",
     'return any(state != "confirmed" for state in self.items.values())',
     'return bool(self.items) and all(state != "confirmed" for state in self.items.values())',
     CHANNEL + "test_physical_delivery_settles_each_item_once_at_its_target"),
    ("body confirms a failed image", "channel/presentation", "(BODY, *ledger.prepared_images)",
     '(BODY, *(item for item in ledger.items if item[0] == "image"))',
     CHANNEL + "test_physical_delivery_settles_each_item_once_at_its_target"),
    ("contradicted card is revoked for this input only", "channel/topics", "self.binding.unusable.update(ids)",
     "from dataclasses import replace\n        self.binding = replace(self.binding, unusable=self.binding.unusable | ids)",
     CHANNEL + "test_later_humans_share_the_verified_root_and_revoke_a_foreign_card"),
    ("final snapshot loses earlier partials", "runtime/claude_ordinary",
     "snapshot = execution.activity(turn_id, source.feedback, state=state(), final=body)",
     "snapshot = execution.activity(turn_id, source.feedback, state=state(), final=body)\n"
     "            snapshot = replace(snapshot, partial_answers=snapshot.partial_answers[-1:])",
     CHANNEL + "test_explore_bodyless_partial_handoff"),
    ("bodyless anonymous wake drops its partials", "runtime/claude_ordinary", "if text != final)",
     "if text != final and not (self.physical_activity and self.physical_activity.anonymous))", CHANNEL + "test_explore_bodyless_partial_handoff"),
    ("healthy close precedes task stop acknowledgment", "runtime/claude_ordinary", "and not execution.awaiting_task_stop)",
     ")", CHANNEL + "test_explore_stop_acknowledgment_and_final_membership"),
    ("bodyless background pulse keeps running", "channel/presentation", "if event.background and event.target.run:",
     "if event.background and event.target.run and display.reply.attempted:", CHANNEL + "test_bodyless_human_tracks_background_and_best_effort_updates_do_not_gate_terminal"),
    ("topic ID replaces verified root", "channel/base", "root = message.source.topic_root_id",
     "root = scope.topic_id", CHANNEL + "test_queued_cron_answer_resolves_the_topic_root_at_channel_handoff"),
    ("cached idle", "runtime/claude_client", "if removed - self.stopped:", "if False:", ORDINARY +
     "test_stop_confirmation_only_preserves_an_existing_fresh_idle"),
    ("stop acknowledgement removes work", "runtime/claude_ordinary", "                        requested += 1",
     "                        live.tasks.pop(task, None)\n                        requested += 1", STOP),
    ("route wake to latest human", "runtime/claude_ordinary", "execution.routable(owner)",
     "(execution.human if wake else execution.routable(owner))", EXPLORE),
    ("early logical terminal", "runtime/claude_ordinary", " or live.has_work(human.input_id):", ":", EXPLORE),
    ("query again on wake", "runtime/claude_ordinary", "                        if not live.init_human:",
     '                        if not live.init_human:\n                            await client.query(single_message({"uuid": "extra"}))',
     EXPLORE),
    ("admit before disconnect", "runtime/claude_ordinary", "            raise OrdinaryInputRejected(\"上一轮正在收尾，本条消息未发送。\")",
     "            return", EXPLORE),
    ("cancel send stops shutdown drain", "external_calls", "if caller is not None and caller.cancelling():",
     "if caller is not None:", CANCEL),
    ("stop intent lost at wake Result", "runtime/claude_ordinary", "                human.stop_requested = True",
     "                pass", WIRE),
    ("unconfirmed content silently settles", "channel/presentation", "if ledger.settle() and display.publishable():",
     "if False:", CHANNEL + "test_physical_delivery_settles_each_item_once_at_its_target"),
    ("partial IDs absent from Activity revision", "runtime/claude_ordinary", ", tuple(self.live.partials), state.line)", ", state.line)", WIRE),
    ("disabled separate completion mention", "channel/presentation", "if event.mention_user_id and not reply.mention_attempted and display.publishable():",
     "if False:", WIRE),
    ("child text becomes partial", "runtime/claude_client", "if parent is None and frame.message_id and not frame.error:",
     "if frame.message_id and not frame.error:", WIRE),
    ("LIFO delivery", "runtime/claude_delivery", "await queue.popleft()()", "await queue.pop()()", WIRE),
    ("constant running label", "ordinary_sessions", "if not self.idle:", "if True:", WIRE),
)

COMBINED_EXITS = (
    ORDINARY + "test_every_exit_settles_submission_delivery_cleanup_and_cursor_once",
    ORDINARY + "test_settlement_survives_uncertain_write_eof_and_shutdown",
    ORDINARY + "test_unwritten_init_on_either_side_of_rollback_degrades_before_result",
    ORDINARY + "test_presentation_joined_at_service_deadline",
    CHANNEL + "test_queued_cron_answer_resolves_the_topic_root_at_channel_handoff",
    CHANNEL + "test_settlement_of_cancelled_reaction_cleanup_leaves_only_the_uncertain_id_for_close",
)


def run(directory, *cases, failfast=True):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-B", "-m", "unittest", *(["-f"] if failfast else []), *cases], cwd=directory, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)


def main():
    from importlib.util import find_spec
    if find_spec("claude_agent_sdk") is None:
        raise SystemExit("Install the pinned Claude extra before running this gate.")
    tests = dict.fromkeys([*(m[-1] for m in MUTANTS), *COMBINED_EXITS])
    baseline = run(ROOT, *tests)
    if baseline.returncode or "skipped=" in baseline.stdout:
        raise SystemExit(baseline.stdout)
    print(f"Baseline passed ({len(tests)} test methods).", flush=True)
    with tempfile.TemporaryDirectory(prefix="netizen-claude-mutants-") as tmp:
        candidate = Path(tmp)
        package = candidate / "netizen_cli"
        shutil.copytree(ROOT / "netizen_cli", package, ignore=shutil.ignore_patterns("__pycache__"))
        (candidate / "tests").symlink_to(ROOT / "tests", target_is_directory=True)
        (candidate / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
        for name, module, before, after, test in MUTANTS:
            source = package / (module + ".py")
            original = source.read_text()
            if original.count(before) != 1:
                raise SystemExit(f"Update the mutation site for {name}.")
            source.write_text(original.replace(before, after))
            result = run(candidate, test, failfast=test != WIRE_SAFETY)
            source.write_text(original)
            if result.returncode != 1 or "FAIL:" not in result.stdout:
                raise SystemExit(f"Missing assertion failure for {name}:\n{result.stdout}")
            coverage = test.rsplit(".", 1)[-1]
            if test == WIRE_SAFETY:
                failures = [line for line in result.stdout.splitlines() if line.startswith("FAIL:")]
                if not all(any(line.endswith(f"(cards={cards})") for line in failures) for cards in (True, False)):
                    raise SystemExit(f"Missing a transport assertion for {name}: {failures}")
                coverage += ", card and process"
            print(f"Killed {name} ({coverage}).", flush=True)


if __name__ == "__main__":
    main()
