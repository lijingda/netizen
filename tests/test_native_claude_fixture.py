import os
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.check_native_claude import fixture_environment


class ClaudeIsolationTests(unittest.TestCase):
    def test_home_credentials_and_traffic_are_fixture_scoped(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "other", "CLAUDE_CODE_OAUTH_TOKEN": "other", "EXTRA_ENV": "other"}):
            config = fixture_environment(Path("/test-home"))
        self.assertEqual(config["HOME"], "/test-home")
        self.assertEqual(config["CLAUDE_CONFIG_DIR"], "/test-home/.claude")
        self.assertEqual(config["ANTHROPIC_API_KEY"], "synthetic-fixture")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", config)
        self.assertNotIn("EXTRA_ENV", config)
        self.assertEqual(config["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9")
        self.assertEqual(config["HTTP_PROXY"], config["HTTPS_PROXY"])
        self.assertEqual(config["NO_PROXY"], "127.0.0.1,localhost")
        self.assertTrue(all(config[name] == "1" for name in (
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY", "DISABLE_AUTOUPDATER", "DISABLE_ERROR_REPORTING",
        )))
