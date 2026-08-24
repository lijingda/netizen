from __future__ import annotations

import io
import json
import subprocess
import sys
import types
import unittest
from collections.abc import Mapping
from typing import Any
from unittest.mock import patch

from scripts import feishu_app_onboarding as onboarding


class FeishuAppOnboardingTest(unittest.TestCase):
    def test_pinned_dependencies_expose_registration_and_terminal_qr(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import inspect, lark_oapi; "
                "from lark_oapi.api.application.v6 import ListScopeRequest; "
                "print(','.join(inspect.signature(lark_oapi.register_app).parameters)); "
                "print(ListScopeRequest.builder().build().uri)",
            ],
            check=False,
            capture_output=True,
            text=True,
            # A first import from a freshly built macOS release can spend more
            # than 30 seconds loading the generated SDK modules from cold disk.
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        parameters_line, scope_uri = result.stdout.strip().splitlines()
        parameters = parameters_line.split(",")
        self.assertIn("addons", parameters)
        self.assertIn("create_only", parameters)
        self.assertIn("app_id", parameters)
        self.assertEqual(scope_uri, "/open-apis/application/v6/scopes")
        output = io.StringIO()
        onboarding._default_qr_renderer("https://example.com/setup", output)
        self.assertGreater(len(output.getvalue().splitlines()), 10)

    def test_unbound_app_uses_official_create_or_select_page(self) -> None:
        captured: dict[str, Any] = {}
        stdout = io.StringIO()
        stderr = io.StringIO()

        def register_app(**kwargs: Any) -> Mapping[str, Any]:
            captured.update(kwargs)
            kwargs["on_qr_code"](
                {
                    "url": "https://accounts.feishu.cn/device/example",
                    "expire_in": 600,
                }
            )
            kwargs["on_status_change"]({"status": "polling"})
            return {
                "client_id": "cli_created",
                "client_secret": "created-secret",
            }

        onboarding.run_registration(
            None,
            register_app=register_app,
            render_qr=lambda url, output: output.write(f"QR:{url}\n"),
            stdout=stdout,
            stderr=stderr,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload,
            {
                "version": 1,
                "appId": "cli_created",
                "appSecret": "created-secret",
            },
        )
        self.assertNotIn("created-secret", stderr.getvalue())
        self.assertIn("https://accounts.feishu.cn/device/example", stderr.getvalue())
        self.assertIn("QR:https://accounts.feishu.cn/device/example", stderr.getvalue())
        self.assertEqual(captured["source"], "netizen-installer")
        self.assertNotIn("create_only", captured)
        self.assertNotIn("app_id", captured)
        addons = captured["addons"]
        self.assertIs(addons["preset"], False)
        self.assertEqual(
            addons["scopes"],
            {"tenant": list(onboarding.REQUIRED_TENANT_SCOPES)},
        )
        self.assertIn(
            "im:message.p2p_msg:readonly",
            onboarding.REQUIRED_TENANT_SCOPES,
        )
        self.assertIn("im:chat:readonly", onboarding.REQUIRED_TENANT_SCOPES)
        self.assertNotIn("user", addons["scopes"])
        self.assertEqual(
            addons["events"],
            {"items": {"tenant": ["im.message.receive_v1"]}},
        )
        self.assertNotIn("user", addons["events"]["items"])
        self.assertEqual(
            addons["callbacks"],
            {"items": ["card.action.trigger"]},
        )

    def test_existing_app_is_updated_and_must_keep_its_identity(self) -> None:
        captured: dict[str, Any] = {}

        def register_app(**kwargs: Any) -> Mapping[str, Any]:
            captured.update(kwargs)
            return {
                "client_id": "cli_existing",
                "client_secret": "updated-secret",
            }

        stdout = io.StringIO()
        onboarding.run_registration(
            "cli_existing",
            register_app=register_app,
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(captured["app_id"], "cli_existing")
        self.assertNotIn("create_only", captured)
        self.assertEqual(json.loads(stdout.getvalue())["appSecret"], "updated-secret")

        with self.assertRaisesRegex(onboarding.OnboardingError, "different App ID"):
            onboarding.run_registration(
                "cli_existing",
                register_app=lambda **_kwargs: {
                    "client_id": "cli_other",
                    "client_secret": "other-secret",
                },
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

    def test_qr_failure_preserves_url_fallback(self) -> None:
        stderr = io.StringIO()

        def register_app(**kwargs: Any) -> Mapping[str, Any]:
            kwargs["on_qr_code"](
                {"url": "https://accounts.feishu.cn/device/example", "expire_in": 600}
            )
            return {
                "client_id": "cli_created",
                "client_secret": "created-secret",
            }

        def fail_qr(_url: str, _output: io.StringIO) -> None:
            raise RuntimeError("terminal has no QR support")

        onboarding.run_registration(
            None,
            register_app=register_app,
            render_qr=fail_qr,
            stdout=io.StringIO(),
            stderr=stderr,
        )

        self.assertIn("use the URL above", stderr.getvalue())

    def test_main_keeps_sdk_failure_and_credentials_out_of_stdout(self) -> None:
        fake_sdk = types.SimpleNamespace(
            register_app=lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError("unexpected-secret")
            )
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(sys.modules, {"lark_oapi": fake_sdk}),
            patch("sys.stdout", new=stdout),
            patch("sys.stderr", new=stderr),
        ):
            code = onboarding.main([])

        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("unexpected-secret", stderr.getvalue())
        self.assertIn("did not complete", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
