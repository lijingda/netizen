from __future__ import annotations

import json
import unittest

from netizen_cli.domain import ActiveState, GoalOperationState, GoalStatus, SESSION_IDLE_STATE
from netizen_cli.runtime.contracts import ThreadLifecycleState, ThreadSubscriptionState

from tests.admin.ui_harness import CHAT_CONFIG_SCRIPTS, harness_source, read_fixture, run_harness


class SessionSettingsUiTest(unittest.TestCase):
    def test_session_configuration_drawer_and_request_boundaries(self) -> None:
        before, after = read_fixture("session_settings_ui_harness.js", "// SHIPPED_SESSION_SETTINGS_CONTROLLER\n")
        statuses = [state.value for enum in (ActiveState, GoalOperationState, ThreadLifecycleState) for state in enum]
        statuses += [f"goal-{state.value}" for state in GoalStatus]
        statuses += ["compacting", SESSION_IDLE_STATE]
        before += "\nconst knownRuntimeStates = " + json.dumps(statuses) + ";\n"
        before += "const knownSubscriptionStates = " + json.dumps([state.value for state in ThreadSubscriptionState]) + ";\n"
        script = harness_source(
            "index.html", (*CHAT_CONFIG_SCRIPTS, "progress-card.js", "admin.js"), before, after,
        )
        for case in (
            "localized_presentation",
            "entry_and_complete_save",
            "progress_card_configuration",
            "global_progress_fallback",
            "restore_global_progress",
            "unavailable_global_preview",
            "global_preview_without_models",
            "inheritance_and_explicit_defaults",
            "context_and_private_chat",
            "unavailable_catalog",
            "duplicate_save_and_failure",
            "cancel_and_stale_options",
            "saved_refresh_failure",
            "discard_confirmation",
        ):
            with self.subTest(case=case):
                result = run_harness(
                    self, script, timeout=10, args=("-", case),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
