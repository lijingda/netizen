import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts import probe_scheduled_outcome_mount as probe


class ScheduledOutcomeProbeTest(unittest.TestCase):
    def test_namespace_inspection_preserves_tool_ownership(self):
        observed = probe.advertised_tools([
            {"type": "namespace", "name": "thread_entry", "tools": [{"name": "report_run_outcome"}]},
            {"type": "namespace", "name": "user_entry", "tools": [{"name": "report_run_outcome"}]},
            {"type": "function", "name": "ordinary"},
        ])
        self.assertEqual(observed, {("thread_entry", "report_run_outcome"), ("user_entry", "report_run_outcome"),
                                    ("", "ordinary")})

    def test_selected_case_writes_same_evidence_to_json_and_stdout(self):
        evidence = {"cases": [{"name": "fork", "passed": True}], "config_unchanged": True}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence.json"
            with patch.object(probe, "probe", new_callable=AsyncMock, return_value=evidence) as run, \
                    patch("sys.argv", ["probe", "--case", "fork", "--output", str(output)]), redirect_stdout(StringIO()) as stdout:
                probe.main()
            run.assert_awaited_once_with("fork")
            self.assertEqual(json.loads(output.read_text()), evidence)
            self.assertEqual(stdout.getvalue(), output.read_text())
