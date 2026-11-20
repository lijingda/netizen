import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_native_claude import fixture_environment, validate_worker_fixture


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

    def test_worker_resolves_home_and_removes_only_darwin_launcher_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root).resolve()
            alias = home / "home-alias"
            alias.symlink_to(home, target_is_directory=True)
            cwd = home / "project"
            cwd.mkdir()
            extras = {"__CF_USER_TEXT_ENCODING": "fixture", "__PYVENV_LAUNCHER__": "fixture"}
            env = fixture_environment(alias) | extras
            validate_worker_fixture(cwd, env, "darwin")
            self.assertEqual(env, fixture_environment(alias))
            with self.assertRaisesRegex(RuntimeError, "unexpected keys="):
                validate_worker_fixture(cwd, env | extras, "linux")
            with self.assertRaisesRegex(RuntimeError, "unexpected keys=EXTRA_ENV"):
                validate_worker_fixture(cwd, env | {"EXTRA_ENV": "fixture"}, "darwin")

    def test_required_paths_values_and_keys_fail_with_names_only(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root).resolve()
            cfg = fixture_environment(home)
            cfg["ANTHROPIC_API_KEY"] = "secret-test-value"
            del cfg["HTTPS_PROXY"]
            with self.assertRaises(RuntimeError) as caught:
                validate_worker_fixture(home, cfg, "darwin")
            message = str(caught.exception)
            for text in ("working directory", "missing keys=HTTPS_PROXY", "mismatched keys=ANTHROPIC_API_KEY",
                         "resolved cwd.parent == resolved HOME: False"):
                self.assertIn(text, message)
            self.assertNotIn("secret-test-value", message)
            self.assertNotIn(str(home), message)
