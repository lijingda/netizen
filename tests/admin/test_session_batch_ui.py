from __future__ import annotations

import json
import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class SessionBatchUiTest(unittest.TestCase):
    def run_scenario(self, scenario: str) -> None:
        before, after = read_fixture("session_batch_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        common, _ = read_fixture("side_sessions_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        script = harness_source(
            "index.html", None, common + before, after,
        )
        script = f"const initialSearch = ''; const scenario = {json.dumps(scenario)};\n" + script
        result = run_harness(self, script, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_selection_identity_grouping_and_temporary_sessions(self) -> None:
        self.run_scenario("selection")

    def test_refresh_before_confirmation_uses_fresh_exact_grants(self) -> None:
        self.run_scenario("confirmation")

    def test_failed_refresh_cancel_and_missing_targets_never_post(self) -> None:
        self.run_scenario("preparation")

    def test_sequential_requests_and_known_failures_continue(self) -> None:
        self.run_scenario("sequential")

    def test_unknown_results_stop_without_retry(self) -> None:
        for outcome in ["503", "400", "403", "429", "network", "parse"]:
            with self.subTest(outcome=outcome):
                self.run_scenario(outcome)

    def test_expiry_closes_modal_and_counts_completed_items(self) -> None:
        self.run_scenario("expiry")

    def test_stop_does_not_wait_or_observe_late_responses(self) -> None:
        for outcome in ["200", "401", "network", "parsing"]:
            with self.subTest(outcome=outcome):
                self.run_scenario(f"stop-{outcome}")

    def test_state_specific_buttons_endpoints_and_danger_confirmation(self) -> None:
        self.run_scenario("states")

    def test_final_refresh_failure_retains_results(self) -> None:
        self.run_scenario("refresh-failure")

    def test_empty_page_hides_toolbar_and_restores_reachable_focus(self) -> None:
        self.run_scenario("empty-page")

    def test_page_close_stops_without_followup_or_resume(self) -> None:
        self.run_scenario("page-close")

    def test_native_close_reopens_running_dialog_and_cleans_finished_dialog(self) -> None:
        self.run_scenario("native-close")

    def test_navigation_keeps_modal_visible_without_interrupting_other_lists(self) -> None:
        for tab in ["projects", "temporary"]:
            with self.subTest(tab=tab):
                self.run_scenario(f"navigate-{tab}")

    def test_refresh_discards_selection_that_lost_its_grants(self) -> None:
        self.run_scenario("lost-grants")

    def test_expiry_messages_distinguish_unexecuted_and_finished_batches(self) -> None:
        for stage in ["preparing", "confirming", "finished"]:
            with self.subTest(stage=stage):
                self.run_scenario(f"expiry-{stage}")

    def test_superseded_preparation_does_not_claim_refresh_failure(self) -> None:
        self.run_scenario("superseded-preparation")

    def test_confirmation_opened_during_preparation_is_reported(self) -> None:
        self.run_scenario("other-confirmation")

    def test_superseded_ending_refresh_preserves_new_list_and_result(self) -> None:
        self.run_scenario("superseded-ending")

    def test_cancel_restores_focus_without_stealing_another_control(self) -> None:
        self.run_scenario("cancel-focus")

    def test_running_or_unconfirmed_warning_excludes_persisted_inactive_goals(self) -> None:
        self.run_scenario("warnings")

    def test_confirmation_uses_short_chat_identity_when_name_unresolved(self) -> None:
        self.run_scenario("chat-fallback")

    def test_grouped_confirmation_progress_and_execution_follow_display_order(self) -> None:
        for layout in ["project", "chat"]:
            with self.subTest(layout=layout):
                self.run_scenario(f"order-{layout}")

    def test_result_list_hides_empty_details_and_marks_failures(self) -> None:
        self.run_scenario("presentation")

    def test_inventory_parsing_does_not_grow_with_page_size(self) -> None:
        self.run_scenario("query-budget")
