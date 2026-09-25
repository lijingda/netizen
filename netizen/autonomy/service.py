"""Opt-in admission decisions, with no dependency on native Turn execution."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Awaitable, Callable

from .config import load_config, parse_config, public_config, save_config
from .models import AutonomyConflict, AutonomyError, Candidate, Context, Decision, DecisionToken
from .provider import SystemOneProvider, estimate_tokens, request_fits
from .store import AutonomyStore


_PUBLIC_ERRORS = {
    "unconfigured": "决策模型尚未配置，请联系实例管理员在 Admin 中完成配置。",
    "disabled": "自主模式已关闭。",
    "candidate_too_large": "当前消息超过决策模型输入预算，可通过 @ 显式提交。",
    "context_too_large": "决策上下文需要摘要，但未能在输入预算内完成摘要。",
    "decision_failed": "决策服务调用失败，请在 Admin 检查连接。",
    "stale": "自主判断的会话状态已变化，本条未自动提交。",
    "storage_failed": "自主模式记录暂不可用。",
    "admission_failed": "本条自主消息未被现有执行流程接收。",
    "preparation_failed": "本条消息无法准备自主判断。",
    "closed": "自主判断服务已关闭。",
}
MAX_SUMMARY_INPUT_BYTES = 64 * 1024
SUMMARY_RETRY_SECONDS = 30.0


class AutonomyService:
    def __init__(
        self, store: AutonomyStore, config_path: Path | str,
        summarizer: Callable[..., Awaitable[str]] | None = None,
        *, provider: object | None = None, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.config_path = Path(config_path)
        self.summarizer = summarizer
        self.provider = provider or SystemOneProvider()
        self._clock = clock
        self._revision = 0
        self._cleared_revision = 0
        self._config = None
        self._config_error: str | None = None
        self._status: dict[str, str | None] = {}
        self._global_error: str | None = None
        self._closed = False
        self._configuration_lock = asyncio.Lock()
        self._summary_locks: dict[str, asyncio.Lock] = {}
        self._failed_summaries: dict[str, tuple[tuple, float]] = {}
        try:
            self._revision, self._config = load_config(self.config_path)
        except AutonomyError:
            # Optional malformed configuration never prevents ordinary startup.
            self._config_error = "决策模型配置无法安全读取，请在 Admin 重新配置。"
        self.store.invalidate_gaps()

    @property
    def configured(self) -> bool:
        return self._config is not None and not self._closed

    def require_configured(self) -> None:
        if not self.configured:
            raise AutonomyError(_PUBLIC_ERRORS["unconfigured"])

    def get_status(self, binding_id: str | None = None) -> dict:
        error = self._config_error or (self._status.get(binding_id) if binding_id else self._global_error)
        if not self.configured:
            error = error or _PUBLIC_ERRORS["unconfigured"]
        return {
            "revision": self._revision,
            "configured": self.configured,
            "config": public_config(self._config),
            "state": "unconfigured" if not self.configured else ("unavailable" if error else "ready"),
            "error": error,
            **({"enabled": self.store.is_enabled(binding_id)} if binding_id else {}),
        }

    async def configure(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise AutonomyError("invalid decision configuration")
        async with self._configuration_lock:
            self._require_revision(payload.get("expected_revision"))
            config = None if payload.get("clear") is True else parse_config(payload, self._config)
            revision = self._revision + 1
            # A single synchronous atomic replace is intentionally not cancellable
            # between durable save and application of this tiny configuration.
            save_config(self.config_path, revision, config)
            self._config, self._revision = config, revision
            if config is None:
                self._cleared_revision = revision
            self._config_error = None
            self._global_error = None
            self._status.clear()
            self._failed_summaries.clear()
        return self.get_status()

    def _require_revision(self, revision: object) -> None:
        if type(revision) is not int or revision != self._revision:
            raise AutonomyConflict("decision configuration changed; reload Admin and try again")
        if self._closed:
            raise AutonomyError(_PUBLIC_ERRORS["closed"])

    async def test_connection(self, expected_revision: int | None = None) -> dict:
        self._require_revision(expected_revision)
        self.require_configured()
        config, revision = self._config, self._revision
        try:
            async with asyncio.timeout(config.timeout_seconds):
                await self.provider.decide(config, 'Current message: Please reply with hello. No private conversation is included.')
        except asyncio.CancelledError:
            raise
        except Exception:
            error = _PUBLIC_ERRORS["decision_failed"]
            if revision == self._revision:
                self._global_error = error
            return {"ok": False, "revision": revision, "error": error}
        if revision != self._revision:
            raise AutonomyConflict("decision configuration changed while testing; test again")
        self._global_error = None
        return {"ok": True, "revision": revision, "error": None}

    def mark_unavailable(self, binding_id: str, reason: str = "decision_failed") -> None:
        error = _PUBLIC_ERRORS.get(reason, _PUBLIC_ERRORS["decision_failed"])
        self._status[binding_id] = error
        self._global_error = error

    def note_gap_unknown(self, binding_id: str) -> None:
        self.store.invalidate_gap(binding_id)

    def _unavailable(self, binding_id: str, reason: str, token: DecisionToken | None = None) -> Decision:
        self.mark_unavailable(binding_id, reason)
        return Decision("unavailable", token=token, reason=reason)

    def _receive(self, binding_id: str, candidate: Candidate, *, explicit: bool) -> DecisionToken | None:
        settings = self.store.receive(binding_id)
        if not settings.enabled or self._closed:
            return None
        return DecisionToken(binding_id, settings.revision, self._revision, settings.received,
                             settings.last_accepted, candidate.message_id, explicit)

    @staticmethod
    def _gap(token: DecisionToken) -> str | None:
        if token.last_accepted is None:
            return None
        gap = token.sequence - token.last_accepted - 1
        if gap <= 0:
            return None
        return f"上一条已消费消息与当前消息之间，有 {gap} 条用户消息未自动提交给你；如需上下文，可按需查询飞书历史。"

    def prepare_explicit(self, binding_id: str, candidate: Candidate) -> Decision:
        token = self._receive(binding_id, candidate, explicit=True)
        return Decision("consume", token, self._gap(token) if token else None)

    def token_current(self, token: DecisionToken | None) -> bool:
        if token is None or self._closed:
            return False
        settings = self.store.settings(token.binding_id)
        return (
            settings.enabled and settings.revision == token.binding_revision
            and (token.explicit or (self.configured and token.config_revision >= self._cleared_revision))
            and settings.last_accepted == token.last_accepted
        )

    async def decide(self, binding_id: str, candidate: Candidate, cwd: str) -> Decision:
        token = self._receive(binding_id, candidate, explicit=False)
        if token is None:
            return self._unavailable(binding_id, "disabled")
        config = self._config
        if config is None:
            return self._unavailable(binding_id, "unconfigured", token)
        empty = _state(Context(), candidate)
        if not request_fits(config, empty):
            return self._unavailable(binding_id, "candidate_too_large", token)
        context = self.store.context(binding_id)
        state = _state(context, candidate)
        if not request_fits(config, state):
            await self._compress(binding_id, candidate, cwd, token, config)
            if not self.token_current(token):
                return self._unavailable(binding_id, "stale", token)
            context = self.store.context(binding_id)
            state = _state(context, candidate)
        if not self.token_current(token):
            return self._unavailable(binding_id, "stale", token)
        if not request_fits(config, state):
            return self._unavailable(binding_id, "context_too_large", token)
        try:
            async with asyncio.timeout(config.timeout_seconds):
                choice = await self.provider.decide(config, state)
            if choice not in {"consume", "skip"}:
                raise AutonomyError("invalid decision choice")
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._unavailable(binding_id, "decision_failed", token)
        if not self.token_current(token):
            return self._unavailable(binding_id, "stale", token)
        if token.config_revision == self._revision:
            self._status[binding_id] = None
            self._global_error = None
        return Decision(choice, token, self._gap(token) if choice == "consume" else None)

    async def _compress(self, binding_id: str, candidate: Candidate, cwd: str, token: DecisionToken, config) -> None:
        if self.summarizer is None:
            return
        # Only the optional summarization task is serialized per Binding. Native
        # input submission and independent Bindings do not wait on this lock.
        lock = self._summary_locks.setdefault(binding_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            context = self.store.context(binding_id)
            failure_key = (token.binding_revision, token.config_revision, context.summary_revision,
                           context.records[-1].sequence if context.records else 0)
            if request_fits(config, _state(context, candidate)):
                return
            if not context.records and not context.summary:
                return
            # Keep up to two recent raw entries when they leave usable summary
            # space. Smaller models can require summarizing all old entries.
            keep = min(2, max(0, len(context.records) - 1))
            while keep:
                tail = Context(records=context.records[-keep:])
                if request_fits(config, _state(tail, candidate) + "x" * 128):
                    break
                keep -= 1
            prefix = context.records[:-keep] if keep else context.records
            tail_records = context.records[-keep:] if keep else ()
            bounded = []
            for record in prefix:
                proposed = Context(context.summary, context.summary_revision, tuple(bounded) + (record,))
                if estimate_tokens(_history(proposed)) > MAX_SUMMARY_INPUT_BYTES:
                    break
                bounded.append(record)
            partial = len(bounded) < len(prefix)
            if partial:
                # No silent truncation of an oversized accepted record. Leave
                # it intact and keep explicit @ available. Otherwise summarize
                # only a bounded oldest prefix; a later candidate can continue
                # compaction while this candidate remains unavailable.
                if not bounded:
                    return
                prefix = tuple(bounded)
                tail_records = context.records[len(prefix):]
            tail_state = _state(Context(records=tail_records), candidate)
            budget = 0
            # Bounds are small (<=32k); derive remaining conservative byte budget
            # without relying on provider-specific tokenizer libraries.
            low, high = 0, config.input_budget
            while low <= high:
                middle = (low + high) // 2
                if request_fits(config, tail_state + "x" * middle):
                    budget, low = middle, middle + 1
                else:
                    high = middle - 1
            budget = max(0, budget - 32)
            if partial:
                budget = min(1024, config.input_budget // 3)
            if budget < 32:
                return
            failure_key += (prefix[-1].sequence if prefix else 0, budget)
            previous_failure = self._failed_summaries.get(binding_id)
            if previous_failure is not None and previous_failure[0] == failure_key and self._clock() - previous_failure[1] < SUMMARY_RETRY_SECONDS:
                return
            text = _history(Context(context.summary, context.summary_revision, prefix))
            try:
                summary = await self.summarizer(cwd, text, budget, key=binding_id)
                if not isinstance(summary, str) or not summary.strip() or estimate_tokens(summary) > budget:
                    raise AutonomyError("invalid decision summary")
                if not self.token_current(token):
                    return
                if not partial and not request_fits(config, _state(Context(summary, records=tail_records), candidate)):
                    raise AutonomyError("summary still exceeds decision budget")
                self.store.replace_prefix(
                    binding_id, binding_revision=token.binding_revision,
                    previous_revision=context.summary_revision,
                    through=prefix[-1].sequence if prefix else 0, summary=summary,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failed_summaries[binding_id] = (failure_key, self._clock())

    def record_accepted(
        self, binding_id: str, candidate: Candidate, turn_id: str,
        token: DecisionToken | None = None, *, logical_turn_id: str | None = None,
    ) -> bool:
        if self._closed:
            return False
        if token is None:
            token = self._receive(binding_id, candidate, explicit=True)
        if token is None or token.binding_id != binding_id or token.message_id != candidate.message_id:
            return False
        return self.store.accepted(token, candidate, logical_turn_id or turn_id)

    async def record_final(
        self, binding_id: str, turn_id: str, text: str, cwd: str | None = None,
        *, accepted_turn_id: str | None = None,
    ) -> bool:
        if self._closed:
            return False
        return self.store.final(binding_id, turn_id, text, accepted_turn_id=accepted_turn_id)

    def forget_turn(self, binding_id: str, turn_id: str) -> None:
        self.store.forget_turn(binding_id, turn_id)

    def close(self) -> None:
        self._closed = True

    async def aclose(self) -> None:
        self.close()


def _history(context: Context) -> str:
    value = {"summary": context.summary, "exchanges": [{"kind": record.kind, "text": record.text} for record in context.records]}
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _state(context: Context, candidate: Candidate) -> str:
    # Explicit structure keeps the candidate separate from selected history; no
    # skipped message bodies are retrieved, retained or fed back on later calls.
    return _history(context) + "\nCurrent message: " + json.dumps(
        {"sender": candidate.sender, "type": candidate.message_type, "text": candidate.text},
        ensure_ascii=False, separators=(",", ":"),
    )
