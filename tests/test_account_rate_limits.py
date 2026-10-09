from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex import AsyncCodex, CodexConfig
from openai_codex.errors import CodexError, InvalidRequestError
from openai_codex.generated import v2_all as generated

from netizen_cli.account_rate_limits import (
    AccountCredits,
    AccountMonthlyCreditLimit,
    AccountRateLimitsUnavailable,
    AppServerAccountRateLimits,
    facade_migration_requirements,
)
from netizen_cli.sdk_gap_adapter import (
    SdkFacadeMigrationRequired,
    SdkGapCapabilityUnavailable,
)


def _bucket(used: int = 37, **kwargs: object) -> dict[str, object]:
    return {"primary": {"usedPercent": used}, **kwargs}


def _response(**kwargs: object) -> dict[str, object]:
    return {"rateLimits": _bucket(99), **kwargs}


def _adapter(payload: dict[str, object]) -> AppServerAccountRateLimits:
    def request(_method, _params, *, response_model):
        return response_model.model_validate(payload)

    codex = SimpleNamespace(
        _initialized=True,
        _client=SimpleNamespace(request=AsyncMock(side_effect=request)),
    )
    return AppServerAccountRateLimits(codex)


_FAKE_SERVER = r'''
import json
import sys

log_path, payload_path, mode = sys.argv[1:]
payload = json.load(open(payload_path, encoding="utf-8"))
pending = None

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps(message) + "\n")
    request_id = message.get("id")
    if request_id is None:
        continue
    if message["method"] == "initialize":
        send({"id": request_id, "result": {
            "userAgent": "netizen-quota-test/1",
            "serverInfo": {"name": "fake", "version": "1"},
        }})
    elif message["method"] == "account/rateLimits/read":
        if mode == "error":
            send({"id": request_id, "error": {
                "code": -32600, "message": "account unavailable",
            }})
        elif mode == "hold-first" and pending is None:
            pending = request_id
        else:
            if pending is not None:
                send({"id": pending, "result": payload})
            send({"id": request_id, "result": payload})
    else:
        send({"id": request_id, "error": {
            "code": -32601, "message": "unexpected method",
        }})
'''


def _requests(log_path: Path) -> list[dict[str, object]]:
    if not log_path.exists():
        return []
    return [
        value for line in log_path.read_text(encoding="utf-8").splitlines()
        if "id" in (value := json.loads(line))
    ]


@asynccontextmanager
async def _server(payload: object, mode: str = "success"):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        payload_path = root / "response.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        log_path = root / "requests.jsonl"
        config = CodexConfig(
            launch_args_override=(
                sys.executable, "-u", "-c", _FAKE_SERVER,
                str(log_path), str(payload_path), mode,
            ),
            experimental_api=False,
        )
        codex = AsyncCodex(config)
        await codex.__aenter__()
        process = codex._client._sync._proc
        try:
            yield codex, log_path
        finally:
            await codex.close()
            if process is not None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=1)
                for name in ("stdout", "stderr"):
                    stream = getattr(process, name, None)
                    if stream is not None:
                        stream.close()


class AccountRateLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def test_multibucket_projection_preserves_unknowns_and_hides_account_data(self):
        adapter = _adapter(_response(
            accountId="private-account",
            rateLimitUpsell={"private": "backend"},
            rateLimitsByLimitId={
                "codex": _bucket(
                    limitId="codex", limitName="Codex", planType="plus",
                    credits={"hasCredits": True, "unlimited": False, "balance": "private"},
                    secondary={"usedPercent": 103, "windowDurationMins": 10080, "resetsAt": 1791600000},
                ),
                "extra": {"limitName": "Extra", "primary": None},
            },
        ))
        snapshot = await adapter.read()
        self.assertEqual(len(snapshot.buckets), 2)
        first, second = snapshot.buckets
        self.assertEqual(first.limit_id, "codex")
        self.assertEqual(first.limit_name, "Codex")
        self.assertEqual(first.primary.used_percent, 37)
        self.assertIsNone(first.primary.window_duration_mins)
        self.assertIsNone(first.primary.resets_at)
        self.assertEqual(first.secondary.used_percent, 103)
        self.assertEqual(first.secondary.window_duration_mins, 10080)
        self.assertEqual(first.secondary.resets_at, 1791600000)
        self.assertEqual(second.limit_id, "extra")
        self.assertIsNone(second.primary)
        self.assertIsNone(second.secondary)
        self.assertNotIn("private", str(asdict(snapshot)))
        self.assertEqual(first.credits, AccountCredits(True, False, None))
        self.assertIsNone(first.monthly_limit)
        with self.assertRaises(FrozenInstanceError):
            first.primary.used_percent = 0
        with self.assertRaises(FrozenInstanceError):
            snapshot.buckets = ()

    async def test_legacy_fallback_only_for_absent_or_null_map(self):
        for kwargs in ({}, {"rateLimitsByLimitId": None}):
            with self.subTest(kwargs=kwargs):
                snapshot = await _adapter(_response(**kwargs)).read()
                self.assertEqual(len(snapshot.buckets), 1)
                self.assertIsNone(snapshot.buckets[0].limit_id)
                self.assertEqual(snapshot.buckets[0].primary.used_percent, 99)

    async def test_optional_credit_and_monthly_values_come_from_the_selected_bucket(self):
        for multiple in (False, True):
            with self.subTest(multiple=multiple):
                bucket = {
                    "credits": {"hasCredits": True, "unlimited": False, "balance": "12.5"},
                    "individualLimit": {
                        "limit": "12000", "used": "456.7", "remainingPercent": 43,
                        "resetsAt": 1791600000,
                    },
                }
                payload = (
                    _response(rateLimitsByLimitId={"codex": bucket})
                    if multiple else {"rateLimits": bucket}
                )
                adapter = _adapter(payload)
                snapshot = await adapter.read()
                projected, = snapshot.buckets
                self.assertIsNone(projected.primary)
                self.assertIsNone(projected.secondary)
                self.assertEqual(projected.credits, AccountCredits(True, False, 13))
                self.assertEqual(projected.monthly_limit, AccountMonthlyCreditLimit(
                    12000, 457, 43, 1791600000,
                ))
                adapter._client.request.assert_awaited_once()

    async def test_credits_only_distinguishes_unlimited_available_and_missing(self):
        for has_credits, unlimited, raw, expected in (
            (False, True, None, AccountCredits(False, True, None)),
            (True, False, None, AccountCredits(True, False, None)),
            (True, False, " 123.4 ", AccountCredits(True, False, 123)),
            (True, False, "0.4", AccountCredits(True, False, 0)),
            (True, False, "0", AccountCredits(True, False, None)),
            (False, False, "123", None),
        ):
            with self.subTest(has_credits=has_credits, unlimited=unlimited, raw=raw):
                payload = {"rateLimits": {"credits": {
                    "hasCredits": has_credits, "unlimited": unlimited, "balance": raw,
                }}}
                if expected is None:
                    with self.assertRaises(AccountRateLimitsUnavailable):
                        await _adapter(payload).read()
                else:
                    snapshot = await _adapter(payload).read()
                    self.assertEqual(snapshot.buckets[0].credits, expected)

    async def test_invalid_credit_amounts_never_leak_or_become_zero(self):
        for raw in ("private@example.com", "", "nan", "inf", "-1", "1_000", "１２", "1e10000"):
            with self.subTest(raw=raw):
                snapshot = await _adapter({"rateLimits": {
                    "credits": {"hasCredits": True, "unlimited": False, "balance": raw},
                    "individualLimit": {
                        "limit": raw, "used": "0", "remainingPercent": 100, "resetsAt": 0,
                    },
                }}).read()
                bucket = snapshot.buckets[0]
                self.assertIsNone(bucket.credits.balance)
                self.assertIsNone(bucket.monthly_limit)
                if raw:
                    self.assertNotIn(raw, str(asdict(snapshot)))

    async def test_monthly_only_keeps_reported_remaining_independent_of_amounts(self):
        for remaining in (0, -10, 110):
            with self.subTest(remaining=remaining):
                snapshot = await _adapter({"rateLimits": {"individualLimit": {
                    "limit": "0", "used": "0", "remainingPercent": remaining, "resetsAt": 0,
                }}}).read()
                self.assertEqual(snapshot.buckets[0].monthly_limit, AccountMonthlyCreditLimit(
                    0, 0, remaining, 0,
                ))

    async def test_optional_typed_fields_are_strict_before_legacy_or_map_coercion(self):
        invalid_buckets = (
            {"credits": {"hasCredits": "false", "unlimited": False}},
            {"credits": {"hasCredits": True, "unlimited": 0}},
            {"credits": {"hasCredits": True, "unlimited": False, "balance": 10}},
            *({"individualLimit": {
                "limit": "100", "used": "1", "remainingPercent": 99, "resetsAt": 0,
                field: value,
            }} for field, value in (
                ("remainingPercent", False), ("remainingPercent", "99"),
                ("remainingPercent", 99.0), ("resetsAt", False), ("resetsAt", "0"),
                ("resetsAt", -1), ("limit", 100), ("used", None),
            )),
        )
        for bucket in invalid_buckets:
            for multiple in (False, True):
                with self.subTest(bucket=bucket, multiple=multiple):
                    payload = (
                        _response(rateLimitsByLimitId={"codex": bucket})
                        if multiple else {"rateLimits": bucket}
                    )
                    with self.assertRaises(AccountRateLimitsUnavailable):
                        await _adapter(payload).read()

    async def test_missing_monthly_details_are_unavailable_not_zero(self):
        for field in ("limit", "used", "remainingPercent", "resetsAt"):
            with self.subTest(field=field):
                monthly = {"limit": "100", "used": "1", "remainingPercent": 99, "resetsAt": 0}
                del monthly[field]
                with self.assertRaises(AccountRateLimitsUnavailable):
                    await _adapter({"rateLimits": {"individualLimit": monthly}}).read()

    async def test_installed_sdk_projects_credits_and_monthly_without_another_request(self):
        payload = _response(rateLimitsByLimitId={"codex": {
            "credits": {"hasCredits": True, "unlimited": True, "balance": None},
            "individualLimit": {
                "limit": "1000", "used": "123.5", "remainingPercent": 80, "resetsAt": 0,
            },
        }})
        async with _server(payload) as (codex, log_path):
            snapshot = await AppServerAccountRateLimits(codex).read()
            bucket, = snapshot.buckets
            self.assertEqual(bucket.credits, AccountCredits(True, True, None))
            self.assertEqual(bucket.monthly_limit, AccountMonthlyCreditLimit(1000, 124, 80, 0))
            self.assertEqual([entry["method"] for entry in _requests(log_path)], [
                "initialize", "account/rateLimits/read",
            ])

    async def test_empty_unknown_or_invalid_multibucket_never_falls_back(self):
        for value in ({}, {"codex": {}}, {"codex": None}, {"codex": []}):
            with self.subTest(value=value):
                with self.assertRaises(AccountRateLimitsUnavailable):
                    await _adapter(_response(rateLimitsByLimitId=value)).read()
        with self.assertRaises(AccountRateLimitsUnavailable):
            await _adapter({"rateLimits": {}}).read()

    async def test_invalid_multibucket_windows_are_not_coerced_to_known_usage(self):
        for field, values in (
            ("usedPercent", [None, True, "0", 1.5, -1, float("inf")]),
            ("windowDurationMins", [False, "60", 0, -1]),
            ("resetsAt", [True, "1791600000", -1]),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    bucket = {"primary": {"usedPercent": 1, field: value}}
                    with self.assertRaises(AccountRateLimitsUnavailable):
                        await _adapter(_response(rateLimitsByLimitId={"codex": bucket})).read()

    async def test_invalid_identity_and_unbounded_bucket_data_are_rejected(self):
        values = (
            {"": _bucket()},
            {"codex": _bucket(limitId="other")},
            {"codex": _bucket(limitName="unsafe\nlabel")},
            {"codex": _bucket(limitName="x" * 257)},
            {str(index): _bucket() for index in range(65)},
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(AccountRateLimitsUnavailable):
                    await _adapter(_response(rateLimitsByLimitId=value)).read()

    async def test_negative_legacy_values_and_wrong_response_type_are_unavailable(self):
        with self.assertRaises(AccountRateLimitsUnavailable):
            await _adapter({"rateLimits": _bucket(-1)}).read()
        adapter = _adapter(_response())
        adapter._client.request.side_effect = None
        adapter._client.request.return_value = None
        with self.assertRaises(AccountRateLimitsUnavailable):
            await adapter.read()

    async def test_sdk_errors_propagate_once(self):
        adapter = _adapter(_response())
        error = InvalidRequestError(-32600, "account unavailable")
        adapter._client.request.side_effect = error
        with self.assertRaises(InvalidRequestError) as captured:
            await adapter.read()
        self.assertIs(captured.exception, error)
        adapter._client.request.assert_awaited_once()

    async def test_installed_sdk_wire_request_is_fixed_and_reads_fresh_each_time(self):
        async with _server(_response(rateLimitsByLimitId={"codex": _bucket()})) as (codex, log_path):
            adapter = AppServerAccountRateLimits(codex)
            self.assertIs(adapter._client, codex._client)
            self.assertFalse(hasattr(adapter, "request"))
            first = await adapter.read()
            second = await adapter.read()
            self.assertEqual(first, second)
            self.assertIsNot(first, second)
            requests = _requests(log_path)
        self.assertEqual([entry["method"] for entry in requests], [
            "initialize", "account/rateLimits/read", "account/rateLimits/read",
        ])
        for request in requests[1:]:
            self.assertEqual(request["params"], {
                "excludeResetCreditDetails": True, "supportsLunaReserve": False,
            })

    async def test_installed_sdk_rejects_empty_null_and_error_responses(self):
        cases = (
            ({}, "success", AccountRateLimitsUnavailable),
            ({"rateLimits": None}, "success", AccountRateLimitsUnavailable),
            (None, "success", CodexError),
            (_response(), "error", InvalidRequestError),
        )
        for payload, mode, expected in cases:
            with self.subTest(payload=payload, mode=mode):
                async with _server(payload, mode) as (codex, log_path):
                    with self.assertRaises(expected):
                        await AppServerAccountRateLimits(codex).read()
                    self.assertEqual(len(_requests(log_path)), 2)

    async def test_installed_sdk_legacy_validation_precedes_numeric_coercion(self):
        for used in (False, "0", 0.0, 0):
            with self.subTest(used=used, kind=type(used).__name__):
                payload = {"rateLimits": _bucket(used, planType="plus")}
                async with _server(payload) as (codex, log_path):
                    adapter = AppServerAccountRateLimits(codex)
                    if type(used) is int:
                        snapshot = await adapter.read()
                        self.assertEqual(snapshot.buckets[0].primary.used_percent, 0)
                    else:
                        with self.assertRaises(AccountRateLimitsUnavailable):
                            await adapter.read()
                    self.assertEqual(len(_requests(log_path)), 2)

    async def test_timeout_and_cancel_do_not_close_shared_client_or_retry(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                async with _server(_response(), "hold-first") as (codex, log_path):
                    adapter = AppServerAccountRateLimits(codex)
                    if cancel:
                        task = asyncio.create_task(adapter.read())
                        async with asyncio.timeout(2):
                            while len(_requests(log_path)) < 2:
                                await asyncio.sleep(0.005)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        with patch("netizen_cli.account_rate_limits._READ_TIMEOUT_SECONDS", 0.05):
                            with self.assertRaises(TimeoutError):
                                await adapter.read()
                    # The second request releases the first late response too.
                    # A closed client, retry or native cancellation changes this trace.
                    snapshot = await adapter.read()
                    self.assertEqual(snapshot.buckets[0].primary.used_percent, 99)
                    methods = [entry["method"] for entry in _requests(log_path)]
                    self.assertEqual(methods, [
                        "initialize", "account/rateLimits/read", "account/rateLimits/read",
                    ])


class AccountRateLimitsShapeTest(unittest.TestCase):
    def test_initialized_owner_and_models_are_required(self):
        for codex in (SimpleNamespace(), SimpleNamespace(_initialized=True, _client=None)):
            with self.subTest(codex=codex), self.assertRaises(SdkGapCapabilityUnavailable):
                AppServerAccountRateLimits(codex)
        for name in ("RateLimitWindow", "CreditsSnapshot", "SpendControlLimitSnapshot"):
            with self.subTest(model=name):
                with patch.object(generated, name, None):
                    with self.assertRaises(SdkGapCapabilityUnavailable):
                        _adapter(_response())
                with patch.object(getattr(generated, name), "model_fields", {}):
                    with self.assertRaises(SdkGapCapabilityUnavailable):
                        _adapter(_response())

    def test_public_facade_candidate_blocks_adapter(self):
        self.assertEqual(facade_migration_requirements(), ())
        with patch.object(AsyncCodex, "account_rate_limits", object(), create=True):
            self.assertEqual(facade_migration_requirements(), (
                "migration-required:account-rate-limits:AsyncCodex.account_rate_limits",
            ))
            with self.assertRaises(SdkFacadeMigrationRequired):
                _adapter(_response())


if __name__ == "__main__":
    unittest.main()
