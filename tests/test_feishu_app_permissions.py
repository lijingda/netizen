from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import feishu_app_permissions as permissions
from scripts.feishu_app_onboarding import REQUIRED_TENANT_SCOPES


def _response(*scope_states: tuple[str, int], success: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        success=lambda: success,
        data=SimpleNamespace(
            scopes=[
                SimpleNamespace(
                    scope_name=name,
                    grant_status=status,
                    scope_type="tenant",
                )
                for name, status in scope_states
            ]
        ),
    )


class FeishuAppPermissionsTest(unittest.TestCase):
    def test_reaction_only_scope_is_not_an_independent_requirement(self) -> None:
        self.assertIn("im:message", REQUIRED_TENANT_SCOPES)
        self.assertNotIn(
            "im:message.reactions:write_only",
            REQUIRED_TENANT_SCOPES,
        )

    def test_complete_contract_has_no_missing_scopes(self) -> None:
        output = io.StringIO()

        permissions.run_permission_check(
            list_scopes=lambda: _response(
                *((scope, 1) for scope in REQUIRED_TENANT_SCOPES)
            ),
            stdout=output,
        )

        self.assertEqual(
            json.loads(output.getvalue()),
            {"version": 1, "missingScopes": []},
        )

    def test_each_official_chat_info_scope_satisfies_the_requirement(self) -> None:
        other_grants = [
            (scope, 1)
            for scope in REQUIRED_TENANT_SCOPES
            if scope != "im:chat:read"
        ]

        for chat_scope in ("im:chat", "im:chat:read", "im:chat:readonly"):
            with self.subTest(chat_scope=chat_scope):
                output = io.StringIO()
                permissions.run_permission_check(
                    list_scopes=lambda chat_scope=chat_scope: _response(
                        *other_grants,
                        (chat_scope, 1),
                    ),
                    stdout=output,
                )
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {"version": 1, "missingScopes": []},
                )

    def test_absent_and_unauthorized_scopes_are_reported_in_contract_order(self) -> None:
        granted = [
            (scope, 1)
            for scope in REQUIRED_TENANT_SCOPES
            if scope not in {"im:message.p2p_msg:readonly", "im:chat:read"}
        ]
        granted.append(("im:chat:read", 2))
        response = _response(*granted)
        response.data.scopes.append(
            SimpleNamespace(
                scope_name="im:message.p2p_msg:readonly",
                grant_status=1,
                scope_type="user",
            )
        )
        response.data.scopes.append(
            SimpleNamespace(
                scope_name="im:chat:readonly",
                grant_status=1,
                scope_type="user",
            )
        )
        output = io.StringIO()

        permissions.run_permission_check(
            list_scopes=lambda: response,
            stdout=output,
        )

        self.assertEqual(
            json.loads(output.getvalue())["missingScopes"],
            ["im:message.p2p_msg:readonly", "im:chat:read"],
        )

    def test_failed_or_malformed_platform_response_fails_closed(self) -> None:
        with self.assertRaises(permissions.PermissionCheckError):
            permissions.run_permission_check(
                list_scopes=lambda: _response(success=False),
                stdout=io.StringIO(),
            )
        malformed = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(
                scopes=[
                    SimpleNamespace(
                        scope_name="im:message",
                        grant_status=None,
                        scope_type="tenant",
                    )
                ]
            ),
        )
        with self.assertRaises(permissions.PermissionCheckError):
            permissions.run_permission_check(
                list_scopes=lambda: malformed,
                stdout=io.StringIO(),
            )

    def test_main_uses_app_secret_without_disclosing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / "secret"
            secret_file.write_text("private-secret", encoding="utf-8")
            secret_file.chmod(0o600)
            observed: list[tuple[str, str]] = []

            def client_factory(app_id: str, secret: str) -> SimpleNamespace:
                observed.append((app_id, secret))
                return SimpleNamespace(
                    application=SimpleNamespace(
                        v6=SimpleNamespace(
                            scope=SimpleNamespace(
                                list=lambda _request: _response(
                                    *((scope, 1) for scope in REQUIRED_TENANT_SCOPES)
                                )
                            )
                        )
                    )
                )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("sys.stdout", new=stdout),
                patch("sys.stderr", new=stderr),
            ):
                code = permissions.main(
                    [
                        "--app-id",
                        "cli_existing",
                        "--secret-file",
                        str(secret_file),
                    ],
                    client_factory=client_factory,
                )

            self.assertEqual(code, 0)
            self.assertEqual(observed, [("cli_existing", "private-secret")])
            self.assertNotIn("private-secret", stdout.getvalue())
            self.assertNotIn("private-secret", stderr.getvalue())

    def test_main_preserves_interrupt_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / "secret"
            secret_file.write_text("private-secret", encoding="utf-8")

            def client_factory(_app_id: str, _secret: str) -> SimpleNamespace:
                return SimpleNamespace(
                    application=SimpleNamespace(
                        v6=SimpleNamespace(
                            scope=SimpleNamespace(
                                list=lambda _request: (_ for _ in ()).throw(
                                    KeyboardInterrupt
                                )
                            )
                        )
                    )
                )

            code = permissions.main(
                [
                    "--app-id",
                    "cli_existing",
                    "--secret-file",
                    str(secret_file),
                ],
                client_factory=client_factory,
            )

            self.assertEqual(code, 130)


if __name__ == "__main__":
    unittest.main()
