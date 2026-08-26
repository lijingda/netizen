#!/usr/bin/env python3
"""Create or update the Bot-only Feishu/Lark app used by Netizen.

This helper runs inside a verified candidate release so the public installer
does not need either a system Python package or Lark CLI.  Its stdout is a
private machine-readable credential channel consumed by the parent installer;
all user-facing progress goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, IO


REQUIRED_TENANT_SCOPES = (
    "im:message",
    "im:message.group_msg",
    "im:message.p2p_msg:readonly",
    "im:chat:read",
    "im:chat.members:read",
    "im:message.reactions:write_only",
    "im:resource",
    "im:message:send_as_bot",
)
TENANT_SCOPE_ALTERNATIVES = {
    "im:chat:read": frozenset(("im:chat", "im:chat:read", "im:chat:readonly")),
}
TENANT_EVENTS = ("im.message.receive_v1",)
CALLBACKS = ("card.action.trigger",)


class OnboardingError(RuntimeError):
    """The registration flow did not return a safe credential pair."""


RegisterApp = Callable[..., Mapping[str, Any]]
QrRenderer = Callable[[str, IO[str]], None]


def _registration_options(app_id: str | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "source": "netizen-installer",
        "app_preset": {
            "name": "{user}'s Netizen",
            "desc": "Use native Codex threads from Feishu or Lark.",
        },
        "addons": {
            "preset": False,
            "scopes": {"tenant": list(REQUIRED_TENANT_SCOPES)},
            "events": {"items": {"tenant": list(TENANT_EVENTS)}},
            "callbacks": {"items": list(CALLBACKS)},
        },
    }
    if app_id is not None:
        options["app_id"] = app_id
    return options


def _default_qr_renderer(url: str, output: IO[str]) -> None:
    import qrcode

    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        border=2,
    )
    code.add_data(url)
    code.make(fit=True)
    code.print_ascii(out=output, tty=output.isatty(), invert=True)


def _credential_payload(result: Mapping[str, Any]) -> dict[str, str | int]:
    app_id = result.get("client_id")
    app_secret = result.get("client_secret")
    if (
        not isinstance(app_id, str)
        or not app_id.startswith("cli_")
        or app_id.strip() != app_id
        or any(ord(character) < 0x20 for character in app_id)
    ):
        raise OnboardingError("registration returned an invalid App ID")
    if (
        not isinstance(app_secret, str)
        or not app_secret
        or app_secret.strip() != app_secret
        or any(ord(character) < 0x20 for character in app_secret)
    ):
        raise OnboardingError("registration returned an invalid App Secret")
    return {"version": 1, "appId": app_id, "appSecret": app_secret}


def run_registration(
    app_id: str | None,
    *,
    register_app: RegisterApp,
    render_qr: QrRenderer = _default_qr_renderer,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> None:
    """Run one official device flow and emit credentials only on success."""

    credential_output = sys.stdout if stdout is None else stdout
    progress_output = sys.stderr if stderr is None else stderr
    if app_id is not None and not app_id.startswith("cli_"):
        raise OnboardingError("existing App ID must start with cli_")

    def on_qr_code(info: Mapping[str, Any]) -> None:
        url = info.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise OnboardingError("registration returned an invalid verification URL")
        print("", file=progress_output)
        print(
            "Open this Feishu/Lark URL and confirm the requested Bot access:",
            file=progress_output,
        )
        print(url, file=progress_output)
        print("Or scan this QR code:", file=progress_output)
        try:
            render_qr(url, progress_output)
        except Exception:
            print(
                "(QR rendering is unavailable; use the URL above.)",
                file=progress_output,
            )

    def on_status_change(info: Mapping[str, Any]) -> None:
        status = info.get("status")
        if status == "domain_switched":
            print("Continuing with the Lark account domain...", file=progress_output)
        elif status == "slow_down":
            print("Waiting for confirmation...", file=progress_output)

    result = register_app(
        on_qr_code=on_qr_code,
        on_status_change=on_status_change,
        **_registration_options(app_id),
    )
    payload = _credential_payload(result)
    if app_id is not None and payload["appId"] != app_id:
        raise OnboardingError("registration returned a different App ID")
    json.dump(payload, credential_output, ensure_ascii=True, separators=(",", ":"))
    credential_output.write("\n")
    credential_output.flush()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Netizen Feishu/Lark app onboarding")
    parser.add_argument("--app-id", help="update this existing cli_ application")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import lark_oapi as lark

        run_registration(args.app_id, register_app=lark.register_app)
        return 0
    except KeyboardInterrupt:
        print("netizen: Feishu/Lark app setup interrupted", file=sys.stderr)
        return 130
    except Exception:
        # Credential material is only emitted after complete validation.  Keep
        # failures generic so an unexpected SDK exception can never echo a
        # sensitive response into the terminal or installer logs.
        print(
            "netizen: Feishu/Lark app setup did not complete; "
            "rerun the installer or choose manual credentials",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
