#!/usr/bin/env python3
"""Check the configured app's effective tenant and user permission contract."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, IO


from .feishu_app_onboarding import (
    REQUIRED_TENANT_SCOPES,
    REQUIRED_USER_SCOPES,
    TENANT_SCOPE_ALTERNATIVES,
)
from .lark_app import load_lark_app


class PermissionCheckError(RuntimeError):
    """The platform did not return a trustworthy app permission state."""


ScopeLister = Callable[[], Any]


def _missing_required_scopes(response: Any) -> dict[str, list[str]]:
    success = getattr(response, "success", None)
    if not callable(success) or not success():
        raise PermissionCheckError("the app permission query failed")
    data = getattr(response, "data", None)
    scopes = getattr(data, "scopes", None)
    if not isinstance(scopes, list):
        raise PermissionCheckError("the app permission query returned invalid data")

    granted: dict[str, set[str]] = {"tenant": set(), "user": set()}
    for scope in scopes:
        name = getattr(scope, "scope_name", None)
        status = getattr(scope, "grant_status", None)
        scope_type = getattr(scope, "scope_type", None)
        if (
            not isinstance(name, str)
            or not name
            or isinstance(status, bool)
            or not isinstance(status, int)
            or scope_type not in {"tenant", "user"}
        ):
            raise PermissionCheckError(
                "the app permission query returned an invalid scope"
            )
        if status == 1:
            granted[scope_type].add(name)
    return {
        "tenant": [
            scope for scope in REQUIRED_TENANT_SCOPES
            if not (TENANT_SCOPE_ALTERNATIVES.get(scope, frozenset((scope,))) & granted["tenant"])
        ],
        "user": [scope for scope in REQUIRED_USER_SCOPES if scope not in granted["user"]],
    }


def run_permission_check(
    *,
    list_scopes: ScopeLister,
    stdout: IO[str] | None = None,
) -> None:
    output = sys.stdout if stdout is None else stdout
    missing = _missing_required_scopes(list_scopes())
    print(
        json.dumps(
            {"version": 1, "missingScopes": missing},
            separators=(",", ":"),
        ),
        file=output,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lark-app-config", type=Path, required=True)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str, str], Any] | None = None,
) -> int:
    args = parse_args(argv)
    try:
        credentials = load_lark_app(args.lark_app_config)
        if client_factory is None:
            import lark_oapi as lark
            from lark_oapi.api.application.v6 import ListScopeRequest

            client = (
                lark.Client.builder()
                .app_id(credentials.app_id)
                .app_secret(credentials.app_secret)
                .log_level(lark.LogLevel.ERROR)
                .timeout(15)
                .build()
            )
        else:
            client = client_factory(credentials.app_id, credentials.app_secret)

        def list_scopes() -> Any:
            if client_factory is None:
                request = ListScopeRequest.builder().build()
            else:
                request = None
            return client.application.v6.scope.list(request)

        run_permission_check(list_scopes=list_scopes)
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Feishu/Lark permission verification did not complete.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
