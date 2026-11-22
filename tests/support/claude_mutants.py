"""Single behavioral reversions of the Claude reader contract, installed only in memory."""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import textwrap
from unittest.mock import patch

from netizen_cli.runtime import claude_client, claude_ordinary
from netizen_cli.runtime.claude_client import BundledClaudeClients, ClaudeBackground as Background, EvidenceLedger
from netizen_cli.runtime.claude_ordinary import ClaudeOrdinarySessions as Sessions


@dataclass(frozen=True)
class Mutant:
    name: str
    owner: type | None
    method: str
    original: str
    replacement: str
    family: str
    action: str
    cut: int
    invariant: str

    def applied(self, explorer):
        owner = self.owner or explorer
        target = vars(owner)[self.method]
        source = inspect.getsource(target.fget if isinstance(target, property) else target)
        if source.count(self.original) != 1:
            raise ValueError(f"stale mutation {self.name}")
        # Recompile this one method with its real module globals. A stale edit, type
        # error or harness exception fails the gate independently of the witness.
        mutated = textwrap.dedent(source.replace(self.original, self.replacement))
        namespace = {}
        exec(compile(mutated, f"<mutant {self.name}>", "exec"), vars(inspect.getmodule(owner)), namespace)
        return patch.object(owner, self.method, namespace[self.method])


MUTANTS = (
    Mutant("unproven interrupt", Sessions, "_stop_target", "elif e.writing and not bg.human_open:",
           "elif False:", "single", "stop", 0, "I3: interrupt outside"),
    Mutant("foreign tool UUID", Background, "_observe", "self.human_context = exact and self.turn_open",
           "self.human_context = self.turn_open", "foreign", "observe", 0, "D: missing or repeated"),
    Mutant("physical turn omitted", Sessions, "_refresh", "bg.turn_number, bg.human_open", "bg.human_open",
           "physical", "observe", 0, "I6: visible control"),
    Mutant("empty list erases prior work", Background, "_observe", "self.members = members",
           "self.members = members\n                self.runs_seen = set(members)\n                self.tasks_seen = set(members.values())",
           "dropped_list", "observe", 0, "I2: wrong close"),
    Mutant("delivery cancellation releases Binding", Sessions, "_deliver", "except (Exception, asyncio.CancelledError):",
           "except asyncio.CancelledError:\n            self._release(execution)\n        except Exception:",
           "single", "cancel_delivery", 0, "I4: delivery released"),
    Mutant("settle before delivery", Sessions, "_wake_terminal", "if event is not None:",
           "if False:", "single", "observe", 0, "I1: attributable disposition lost"),
    Mutant("write deadline includes terminal", Sessions, "_observe", "reconciliation.cancel()\n                    reconciliation = None", "pass",
           "lost_write", "observe", 0, "I1: native turn timed out"),
    Mutant("relative proof deadline", Sessions, "_read", "deadlines.get(gap, now + self._remaining(_RECONCILE_SECONDS))",
           "now + self._remaining(_RECONCILE_SECONDS)", "deadline", "observe", 0, "I2: unrelated traffic"),
    Mutant("quarantine plain cold failure", Background, "closable", "self.first_done and self.idle",
           "self.first_done and self.body_results and self.idle", "failure", "observe", 0, "I2: wrong close"),
    Mutant("background refusal on plain turn", Sessions, "_idle", "if active.background.handoff:", "if active.concluded:",
           "plain", "observe", 0, "I5: plain Result"),
    Mutant("ignore stale abandon generation", Sessions, "abandon_background", "or e.input_id != generation", "",
           "proof-cron_count", "observe", 0, "I3: stale abandon"),
    Mutant("abandon only current Binding", Sessions, "abandon_background", "if binding_id is None:\n            binding_id = next",
           "if binding_id is None:\n            raise OrdinaryInputRejected(stale)\n            binding_id = next",
           "proof-cron_count", "observe", 0, "I3: exact generation unreachable"),
    Mutant("deliver cold native replay", Sessions, "_observe",
           "if msg is None or verdict is not Evidence.NEW or bg.degraded:",
           "if isinstance(msg, ResultMessage) and verdict is Evidence.DUPLICATE and msg.origin:\n"
           "                    return OrdinaryConclusion(execution.binding.id, execution.session, execution.input_id, self._owner, "
           "execution.origin, 'completed', msg.result, None)\n                if msg is None or verdict is not Evidence.NEW or bg.degraded:",
           "restart_replay", "observe", 0, "I1:"),
    Mutant("reuse old prompt Stop", Background, "_snapshot", 'self.stop_prompts.add(data["prompt_id"])',
           'self.stop_prompts.add(data["prompt_id"] if data["prompt_id"] not in self.stop_prompts else str(len(self.stop_prompts)))',
           "repeat_stop", "observe", 0, "I2: wrong close"),
    Mutant("revalidate duplicate start against new turn", Background, "_task", "if start is not Evidence.NEW or prior:",
           "if start is Evidence.CONFLICT or prior:", "duplicates", "observe", 0, "I1: attributable disposition lost"),
    Mutant("same UUID hides init conflict", Background, "frame", "if not identity:",
           'if isinstance(msg, SystemMessage) and msg.subtype == "init" and ("frame", identity) in self.ledger.claims:\n'
           "            return Evidence.DUPLICATE\n        if not identity:",
           "conflict_cwd", "observe", 0, "D: missing or repeated"),
    Mutant("time healthy batched wake", Background, "pending_proofs", "if record.disposition_due:",
           "if record.disposition_due or (self.wake_context and sum(bool(r.terminal and not r.settled) for r in self.runs.values()) > 1):",
           "slow_batch", "observe", 0, "I1: native turn timed out"),
    Mutant("shutdown cancels delivery tail only", Sessions, "_begin_client_cleanup", "(*self._controls, *self._deliveries)",
           "(*self._controls, *(e.delivery for e in self._tasks.values() if e.delivery is not None))",
           "close_timeout", "observe", 0, "I4: Feishu send"),
    Mutant("close quarantine keeps old revision", Sessions, "_close_quarantine", "execution.revision = self._advance(key)",
           "self._advance(key)", "close_timeout", "observe", 0, "I6: close quarantine"),
    Mutant("unqualified adapter after pin change", BundledClaudeClients, "__init__", "if (claude_installation.SDK_VERSION,",
           "if False and (claude_installation.SDK_VERSION,", "restart_replay", "observe", 0, "I2: unsupported pin"),
    Mutant("reapply retired membership frame", Background, "_classify", "if verdict is not Evidence.NEW:",
           'if verdict is not Evidence.NEW and not (isinstance(msg, SystemMessage) and msg.subtype == "background_tasks_changed"):',
           "late_membership", "observe", 0, "I2: wrong close"),
    Mutant("body used as delivery identity", None, "_explore", "identities = [(type(e)",
           'self.assertEqual(len(deliveries), len({getattr(e, "final_response", None) for e in deliveries}), "I1: repeated body")\n'
           "                identities = [(type(e)", "single", "observe", 0, "I1: repeated body"),
    Mutant("foreign wake producer", Background, "wake_run", ' or origin.get("producer") != "session-task"', "",
           "proof-foreign", "observe", 0, "I1: routed foreign producer"),
    Mutant("native failure shown as completed", Sessions, "_observe", "status, answer, detail = _terminal(msg)",
           'status, answer, detail = _terminal(msg)\n                status = "completed"', "failure", "observe", 0, "I1: delivery attribution"),
    Mutant("conflict classified as duplicate", EvidenceLedger, "record",
           "return Evidence.CONFLICT", "return Evidence.DUPLICATE",
           "ledger-assistant", "conflict", 9, "D: no disconnect"),
    Mutant("duplicate classified as conflict", EvidenceLedger, "record", "return Evidence.DUPLICATE",
           "return Evidence.CONFLICT", "ledger-tool", "duplicate", 10,
           "I1: exact replay degraded"),
    Mutant("later Stop proves task disposition", Background, "_snapshot", 'self.stop_tasks.update(row["id"] for row in data["background_tasks"])',
           'self.stop_tasks = {row["id"] for row in data["background_tasks"]}',
           "proof-stop_inventory", "observe", 0, "I2: Stop proved disposition"),
    Mutant("Stop cannot wake reader", Background, "snapshot", "self.changed.set()", "pass",
           "proof-callback_only", "observe", 0, "I7: callback failed to wake reader"),
    Mutant("future Stop count ignored", Background, "closable", "and not self.reader_behind", "",
           "proof-ahead", "observe", 0, "I2: credited bodyless receipt or ignored future Stop"),
    Mutant("inventory recorded after suspension", Background, "stop_hook", "self.snapshot(data, tool)",
           "await asyncio.sleep(0)\n            self.snapshot(data, tool)", "proof-entry", "observe", 0, "I7: callback failed to wake reader"),
    Mutant("bodyless receipt earns close credit", Background, "credit", "self.body_results.add(identity)",
           "self.body_results.add(identity)\n        elif identity and msg.terminal_reason is None:\n            self.body_results.add(identity)",
           "proof-ahead", "observe", 0, "I2: credited bodyless receipt or ignored future Stop"),
    Mutant("missing Stop blocks close", Background, "closable", "self.first_done and self.idle",
           "self.first_done and bool(self.stop_prompts) and self.idle", "interrupted", "observe", 0, "I2: wrong close"),
    Mutant("Cron tool ignored", Background, "_observe", "self.cron_touched = True", "pass",
           "proof-cron_touch", "observe", 0, "I2: Cron tool ignored"),
    Mutant("yield after final close check", Sessions, "_disconnect", "await client.disconnect()",
           "await asyncio.sleep(0)\n            await client.disconnect()", "proof-after_disconnect", "observe", 0, "I2: yielded after"),
    Mutant("reuse stale stop revision", Sessions, "_exact", "(revision, turn_id) != (execution.revision, execution.input_id)",
           "turn_id != execution.input_id", "single", "observe", 0, "I3: stale preview"),
    Mutant("bodyless receipt emits a body", Sessions, "_wake_terminal", "if not empty_receipt or result_id is None:",
           "if empty_receipt:\n            self._queue_delivery(e, OrdinaryConclusion(e.binding.id, e.session, result_id, self._owner, e.origin, 'completed', '', None))\n        if not empty_receipt or result_id is None:",
           "proof-bodyless", "observe", 0, "I1: bodyless receipt produced"),
    Mutant("wake usage projected onto H1", Sessions, "_wake_terminal", "self.observations.result(e.binding.id, e.session, wake_id, msg)",
           "self.observations.result(e.binding.id, e.session, e.input_id, msg)", "proof-wake_usage", "observe", 0, "I6: wrong wake usage"),
    Mutant("Stop inventory keyed by prompt", Background, "_snapshot", 'self.record("callback", callback, payload)',
           'self.record("callback", data["prompt_id"], payload)', "proof-stop_continuation", "observe", 0, "I1: native Stop continuation"),
    Mutant("repeat exact task stop", Sessions, "stop_exact", "if (task.cancel_requested if task else execution.stopping):",
           "if not task and execution.stopping:", "single", "stop", 8, "I3: duplicate stop_task"),
    Mutant("route foreign session wake", Sessions, "_wake_terminal", "if msg.session_id != e.session:",
           "if False:", "proof-foreign_session", "observe", 0, "D: no disconnect"),
    Mutant("skip stop receipt before delivery", Sessions, "_deliver", "await execution.stop_receipt.wait()",
           "pass", "proof-stop_receipt_order", "observe", 0, "I4: completion preceded stop receipt"),

    Mutant("queued body after degradation", Sessions, "_deliver", "if isinstance(event, OrdinaryConclusion) and execution.background.degraded:", "if False:",
           "proof-queued_conflict", "observe", 0, "D: queued conclusion"),
    Mutant("repeat degraded notice", Sessions, "_queue_delivery", "await self._deliver(execution, event, notice)",
           "await self._deliver(execution, event, notice)\n            if isinstance(event, TurnObservationUnavailableOutcome):\n                await self._deliver(execution, event, notice)",
           "shape-result/user_message_uuids/0", "eof", 1, "D: repeated or misrouted"),
    Mutant("admit before disconnect", Sessions, "_disconnect", "await client.disconnect()",
           "if execution.background.degraded:\n                self._release(execution)\n            await client.disconnect()",
           "shape-update/data.patch/0", "exit", 2, "D: readmission before disconnect"),
    Mutant("omit degraded disconnect", Sessions, "_disconnect", "await client.disconnect()",
           "if not execution.background.degraded:\n                await client.disconnect()",
           "shape-stop/prompt_id/0", "exit", 1, "D: no disconnect"),
    Mutant("degradation follows resumed session", Sessions, "_run", "bg = execution.background",
           "bg = execution.background\n            previous = getattr(self, '_previous_generation', None)\n"
           "            if previous and previous.session == bg.session:\n                bg.degraded |= previous.degraded\n"
           "            self._previous_generation = bg",
           "shape-list/data.tasks/0", "eof", 0, "D: state leaked into next generation"),
    Mutant("reader shape exception escapes", Sessions, "_run", "except Exception:\n                    bg.degraded = True",
           "except Exception as exc:\n                    if isinstance(exc, TypeError):\n                        raise\n                    bg.degraded = True",
           "proof-reader_exception", "observe", 0, "D: missing anchored notice"),
    Mutant("historical frame bypasses classifier", Background, "_classify", "verdict = self.frame(msg)",
           "verdict = Evidence.NEW if self.before_resumed_input else self.frame(msg)",
           "replay-result_conflict", "observe", 0, "D: no disconnect"),
    Mutant("unowned inventory ignored for close", Background, "_observe", 'self._membership(row["run_id"], row["task_id"])',
           'if not prior:\n                    self._membership(row["run_id"], row["task_id"])', "replay-unowned", "observe", 0, "I2: unowned unresolved"),
    Mutant("attributed terminal leaves replay open", Background, "_prior_replay", "self.before_resumed_input = False\n                return False",
           "if not isinstance(msg, ResultMessage):\n                    self.before_resumed_input = False\n                return False",
           "replay-terminal", "observe", 0, "I1: current input did not end cold replay"),
    Mutant("callback inventory order is identity", Background, "_snapshot", "sorted(data[key], key=lambda row: json.dumps(row, sort_keys=True))",
           "data[key]", "proof-reordered_inventory", "observe", 0, "I1: reordered inventory"),
    Mutant("task stop while reader lags", Background, "single_task", " and not self.reader_behind", "",
           "proof-reader_behind_stop", "observe", 0, "I3: task stop dispatched"),

    Mutant("discard stream before disconnect", Sessions, "_run", "reader = asyncio.create_task(drain())",
           "reader = asyncio.create_task(asyncio.sleep(0))", "shape-result/user_message_uuids/0", "exit", 2, "D: reader stopped"),
    Mutant("degraded notice loses anchor", Sessions, "_run", "self._owner, execution.origin, TerminalStateUnknown(DEGRADED)",
           "self._owner, execution.session, TerminalStateUnknown(DEGRADED)", "shape-init/data.cwd/0", "eof", 0, "D: repeated or misrouted"),
    Mutant("failed degraded disconnect readmits", Sessions, "_disconnect", "self._close_quarantine(execution, unavailable=True)",
           "self._close_quarantine(execution, unavailable=not execution.background.degraded)",
           "proof-degraded_close_failure", "observe", 0, "D: failed close readmitted"),
    Mutant("nested Stop changes main inventory", Background, "stop_hook", 'elif data.get("agent_id") is None:', "else:",
           "proof-subagent", "observe", 0, "I1: ordinary receipt lost"),
    Mutant("skip callback shape check", claude_client, "_valid_fields", "for path, check in CONSUMED_FIELDS[kind].items()",
           'for path, check in CONSUMED_FIELDS[kind].items() if path != "stop_hook_active?"',
           "shape-stop/stop_hook_active/0", "eof", 1, "D: no disconnect"),
    Mutant("terminal bypasses ordinary completion", Sessions, "_run",
           "self._queue_delivery(execution, TurnObservationUnavailableOutcome(\n                        binding.id, execution.session, execution.input_id, self._owner, execution.origin, TerminalStateUnknown(DEGRADED)))",
           "self._schedule(self._tell(execution, DEGRADED))", "proof-handoff_order", "observe", 0, "D: terminal overtook|D: ordinary terminal chain"),
    Mutant("SDK pull cancellation escapes", claude_ordinary, "_messages", "except (Exception, asyncio.CancelledError) as error:",
           "except Exception as error:", "proof-sdk_cancel", "observe", 0, "D: SDK pull cancellation"),
    Mutant("MCP background interpreted as native", Background, "_observe", "fresh = []",
           'self.requested_tools.update(b.id for b in msg.content if isinstance(b, ToolUseBlock) and b.input.get("run_in_background") is True)\n            fresh = []',
           "proof-mcp_background", "observe", 0, "I1: opaque MCP tool"),
    Mutant("unvalidated permission denials", claude_client, "_valid_fields", "for path, check in CONSUMED_FIELDS[kind].items()",
           'for path, check in CONSUMED_FIELDS[kind].items() if path != "permission_denials"',
           "proof-denials_shape", "observe", 0, "D: invalid Result"),
    Mutant("unvalidated API status", claude_client, "_valid_fields", "for path, check in CONSUMED_FIELDS[kind].items()",
           'for path, check in CONSUMED_FIELDS[kind].items() if path != "api_error_status"',
           "proof-api_status_shape", "observe", 0, "D: invalid Result"),
    Mutant("TaskStop accepts a different task", Background, "_observe", 'result.get("task_id") == target',
           'bool(result.get("task_id"))', "proof-mismatched_stop", "observe", 0, "I1: TaskStop accepted"),
    Mutant("slow old delivery retires successor", Sessions, "_release", "self._active.get(execution.binding.id) is execution",
           "execution.binding.id in self._active", "proof-handoff_order", "observe", 0, "I4: old delivery released"),
    Mutant("stopped contradicts killed", Background, "_task", 'if status == "stopped":', "if False:",
           "proof-killed_then_stopped", "observe", 0, "I1: stopped contradicted"),
    Mutant("foreign init confirms lost write", Sessions, "_observe", "if human_init:\n                        accepted()",
           "if initialized or not confirmed_write or human_init:\n                        accepted()", "proof-unattributed_init", "observe", 0, "I1: unrelated init"),
    Mutant("degraded terminal carries session as turn", Sessions, "_run", "execution.input_id, self._owner, execution.origin, TerminalStateUnknown(DEGRADED)",
           "execution.session, self._owner, execution.origin, TerminalStateUnknown(DEGRADED)", "proof-handoff_order", "observe", 0, "D: ordinary terminal chain"),
    Mutant("D disconnect waits for Feishu", Sessions, "_run", "reader = asyncio.create_task(drain())",
           "await execution.delivery\n                    reader = asyncio.create_task(drain())", "proof-handoff_order", "observe", 0, "D: delivery delayed readmission"),
    Mutant("clean SDK ending always degrades", Sessions, "_run", "if bg.unresolved_work:", "if True:",
           "proof-no_work_reader_end", "observe", 0, "I1: clean reader ending degraded"),
    Mutant("D terminal skips receipt", Sessions, "_deliver", "await execution.receipt.wait()",
           "if not isinstance(event, TurnObservationUnavailableOutcome):\n                await execution.receipt.wait()",
           "proof-terminal_receipt", "observe", 0, "D: terminal bypassed input receipt"),
    Mutant("native Task launcher treated as opaque", Background, "_observe", "block.name in _NATIVE_TOOLS",
           'block.name in _NATIVE_TOOLS and block.name != "Task"', "proof-task_launcher", "observe", 0, "I2: native Task launch ignored"),
)
