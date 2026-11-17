from __future__ import annotations

import unittest
from unittest.mock import patch

from netizen_cli.admin.web import _ActionGrantPlan
from tests.admin.ui_harness import harness_source, run_harness


class ActionGrantFixtureTest(unittest.TestCase):
    def source(self) -> str:
        # Only exercise the grant fixture contract; page controllers are irrelevant.
        return harness_source("index.html", (), "", """
const binding = fixtureAction("sessions.stop", {
  resource: "binding", targetId: "one", scopeKey: "scope-one",
});
const side = fixtureAction("side-topics.close", { resource: "side", targetId: "two" });
assert.equal(binding.target.scopeKey, "scope-one");
assert.equal(side.target.scopeKey, null);
""")

    def test_fixture_matches_web_envelope_for_binding_and_side(self) -> None:
        result = run_harness(self, self.source(), timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_server_key_changes_fail_the_javascript_contract(self) -> None:
        original = _ActionGrantPlan.add
        for surface, removed in (("grant", None), ("grant", "actionKind"),
                                 ("target", None), ("target", "scopeKey")):
            with self.subTest(surface=surface, removed=removed):
                def changed_add(plan, *args, **kwargs):
                    envelope = original(plan, *args, **kwargs)
                    target = envelope if surface == "grant" else envelope["target"]
                    if removed:
                        target.pop(removed)
                    else:
                        target["newServerField"] = "changed"
                    return envelope

                with patch.object(_ActionGrantPlan, "add", changed_add):
                    source = self.source()
                result = run_harness(self, source, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Web action {surface} keys changed", result.stderr)
