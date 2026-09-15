from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


_SCRIPT = Path(__file__).resolve().parents[1] / "skills/netizen-feishu/scripts/credentials.py"
_SPEC = importlib.util.spec_from_file_location("netizen_feishu_credentials", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
helper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(helper)


class FeishuCredentialsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / ".netizen"
        (self.root / "credentials").mkdir(parents=True)
        self.app_id, self.secret, self.token = "cli_test123", "private-app-secret", "temporary-bot-token"
        self.config = self.root / "config.yaml"
        self.config.write_text(f"instance:\n  appId: {self.app_id}\n", encoding="utf-8")
        self.secret_file = self.root / "credentials/feishu-app-secret"
        self.secret_file.write_text(self.secret + "\n", encoding="utf-8")
        self.secret_file.chmod(0o600)
        self.enterContext(patch.object(helper.sys, "platform", "linux"))
        self.account = self.enterContext(patch.object(
            helper.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home)),
        ))
        self.response = SimpleNamespace(success=Mock(return_value=True), raw=SimpleNamespace(
            content=json.dumps({"code": 0, "tenant_access_token": self.token, "expire": 7200}).encode(),
        ))
        self.client = Mock()
        self.request_token = self.client.auth.v3.tenant_access_token.internal
        self.request_token.return_value = self.response
        builder = Mock()
        for name in ("app_id", "app_secret", "timeout", "log_level"):
            getattr(builder, name).return_value = builder
        builder.build.return_value = self.client
        self.enterContext(patch("lark_oapi.Client.builder", return_value=builder))

    def invoke(self) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = helper.main()
        return status, stdout.getvalue(), stderr.getvalue()

    def assert_safe_failure(self) -> None:
        status, stdout, stderr = self.invoke()
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr)
        self.assertNotIn(self.secret, stderr)
        self.assertNotIn(self.token, stderr)

    def test_uses_effective_account_installation_and_ignores_environment_overrides(self) -> None:
        with patch.dict(os.environ, {
            "HOME": "/other-account", "NETIZEN_HOME": "/other-installation",
            "FEISHU_APP_SECRET": "wrong-secret", "FEISHU_APP_SECRET_FILE": "/other-secret",
        }):
            result = helper.credentials()
        self.account.assert_called_once_with(os.geteuid())
        self.assertEqual(result, {"app_id": self.app_id, "tenant_access_token": self.token})
        self.assertEqual(self.request_token.call_args.args[0].request_body.app_secret, self.secret)

    def test_success_emits_only_app_id_and_token_using_public_sdk_request(self) -> None:
        status, stdout, stderr = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {"app_id": self.app_id, "tenant_access_token": self.token})
        self.assertNotIn(self.secret, stdout)
        self.request_token.assert_called_once()
        request = self.request_token.call_args.args[0]
        self.assertEqual(request.request_body.app_id, self.app_id)
        self.assertEqual(request.request_body.app_secret, self.secret)

    def test_missing_or_malformed_config_fails_before_authentication(self) -> None:
        for contents in (None, "", "[]", "instance: {}", "instance: {appId: null}",
                         f"instance:\n  appId: {self.secret}", "[" + self.secret):
            with self.subTest(contents=contents):
                if contents is None:
                    self.config.unlink(missing_ok=True)
                else:
                    self.config.write_text(contents, encoding="utf-8")
                self.assert_safe_failure()
                self.request_token.assert_not_called()

    def test_unreadable_or_unprotected_secret_fails_before_authentication(self) -> None:
        for content in (b"", b" \n", b"x" * 4097, b"\xff"):
            with self.subTest(content_length=len(content)):
                self.secret_file.write_bytes(content)
                self.assert_safe_failure()
        self.secret_file.write_text(self.secret, encoding="utf-8")
        self.secret_file.chmod(0o644)
        self.assert_safe_failure()
        self.secret_file.chmod(0o600)
        metadata = self.secret_file.stat()
        with patch.object(helper.os, "fstat", return_value=SimpleNamespace(
            st_mode=metadata.st_mode, st_uid=os.geteuid() + 1,
        )):
            self.assert_safe_failure()
        self.secret_file.unlink()
        self.assert_safe_failure()
        self.secret_file.symlink_to(self.config)
        self.assert_safe_failure()
        self.request_token.assert_not_called()

    def test_authentication_and_response_errors_emit_no_credentials(self) -> None:
        self.response.success.return_value = False
        self.response.msg = self.secret
        self.assert_safe_failure()
        self.response.success.return_value = True
        for content in (self.secret.encode(), b"{}", b"[]",
                        b'{"tenant_access_token":""}', b'{"tenant_access_token":123}'):
            with self.subTest(content=content):
                self.response.raw.content = content
                self.assert_safe_failure()
        self.request_token.side_effect = RuntimeError(self.secret + self.token)
        self.assert_safe_failure()


if __name__ == "__main__":
    unittest.main()
