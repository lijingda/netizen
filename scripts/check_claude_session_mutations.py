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
CANCEL = ORDINARY + "test_send_cancelled_during_shutdown_drains_following_body"

# Each change represents one incorrect decision, with an independently observed consequence.
MUTANTS = (
    ("notify only when every item is unconfirmed", "channel/physical_delivery",
     'return any(state != "confirmed" for state in self.items.values())',
     'return bool(self.items) and all(state != "confirmed" for state in self.items.values())',
     CHANNEL + "test_physical_delivery_settles_each_item_once_at_its_target"),
    ("body confirms a failed image", "channel/presentation", "(BODY, *ledger.prepared_images)",
     '(BODY, *(item for item in ledger.items if item[0] == "image"))',
     CHANNEL + "test_physical_delivery_settles_each_item_once_at_its_target"),
    ("contradicted card is revoked for this input only", "channel/topics", "self.binding.unusable.update(ids)",
     "from dataclasses import replace\n            self.binding = replace(self.binding, unusable=self.binding.unusable | ids)",
     CHANNEL + "test_later_humans_share_the_verified_root_and_revoke_a_foreign_card"),
    ("final snapshot loses earlier partials", "runtime/claude_ordinary",
     "snapshot = execution.activity(turn_id, source.binding.task_feedback, final=body)",
     "snapshot = execution.activity(turn_id, source.binding.task_feedback, final=body)\n"
     "            snapshot = replace(snapshot, partial_answers=snapshot.partial_answers[-1:])",
     CHANNEL + "test_explore_bodyless_partial_handoff"),
    ("bodyless anonymous wake drops its partials", "runtime/claude_ordinary", "or live.partials or (status",
     "or (live.partials and not wake) or (status", CHANNEL + "test_explore_bodyless_partial_handoff"),
    ("healthy close precedes task stop acknowledgment", "runtime/claude_ordinary", "and not execution.awaiting_task_stop)",
     ")", CHANNEL + "test_explore_stop_acknowledgment_and_final_membership"),
    ("bodyless background pulse keeps running", "channel/presentation", "if outcome.background:",
     "if outcome.background and outcome.reply.attempted:", CHANNEL + "test_bodyless_human_tracks_background_and_best_effort_updates_do_not_gate_terminal"),
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
    ("unconfirmed content silently settles", "channel/presentation", "if ledger.settle() and outcome.publishable():",
     "if False:", CHANNEL + "test_physical_delivery_settles_each_item_once_at_its_target"),
    ("partial IDs absent from Activity revision", "runtime/claude_ordinary", ", tuple(live.partials))", ")", WIRE),
    ("disabled completion mention", "channel/presentation", "if outcome.mention_user_id and outcome.publishable():",
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


def run(directory, *cases):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-B", "-m", "unittest", "-f", *cases], cwd=directory, env=env,
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
            result = run(candidate, test)
            source.write_text(original)
            if result.returncode != 1 or "FAIL:" not in result.stdout:
                raise SystemExit(f"Missing assertion failure for {name}:\n{result.stdout}")
            print(f"Killed {name} ({test.rsplit('.', 1)[-1]}).", flush=True)


if __name__ == "__main__":
    main()
