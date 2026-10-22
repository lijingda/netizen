"""Bounded native history projections for model tools, never Feishu cards."""
from __future__ import annotations

import copy
import json
from enum import Enum
from typing import Any

from .delegation import parse_delegated_prompt

MAX_RESULT_BYTES = 8000


def native_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: native_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [native_json(item) for item in value]
    return value


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(0, limit - 1)] + ("…" if limit else "")


def output_summary(text: str, limit: int) -> dict[str, Any]:
    return {"text": text[:limit], "truncated": len(text) > limit,
            **({"originalChars": len(text)} if len(text) > limit else {})}


def _delegation_summary(item: dict[str, Any]) -> dict[str, str] | None:
    # Like the native task tools, expose recognized delegated requests even
    # when raw outputs are hidden. This projection never grants input authority.
    if item.get("name") not in {
        "create_thread", "send_message_to_thread",
    } or not isinstance(item.get("output"), str):
        return None
    if item.get("namespace") in {"codex_tui", "codex_app"}:
        delegation = parse_delegated_prompt(item["output"])
        if delegation is None:
            return None
        source, prompt = delegation
        return {"sourceThreadId": source, "input": truncate(prompt, 2000)}
    # Retain the existing projection for already recorded Netizen inputs.
    if item.get("namespace") != "netizen_sessions":
        return None
    try:
        envelope = json.loads(item["output"])
    except ValueError:
        return None
    if not isinstance(envelope, dict) or set(envelope) != {"sourceThreadId", "sourceBindingId", "message"}:
        return None
    if any(not isinstance(envelope[key], str) or not envelope[key].strip()
           for key in ("sourceThreadId", "sourceBindingId", "message")):
        return None
    return {"sourceThreadId": envelope["sourceThreadId"], "input": truncate(envelope["message"], 2000)}


def item_summary(item: dict[str, Any], include_outputs: bool, output_chars: int) -> dict[str, Any]:
    kind = item.get("type")
    result = {"type": kind, "id": item.get("id")}
    if kind == "agentMessage":
        result.update(text=item.get("text", ""), phase=item.get("phase"))
    elif kind == "userMessage":
        result["content"] = [
            {**entry, "text": truncate(entry["text"], 2000)} if entry.get("type") == "text" else entry
            for entry in item.get("content", [])
        ]
    elif kind == "functionCallOutput":
        result.update(name=item.get("name"), namespace=item.get("namespace"))
        delegation = _delegation_summary(item)
        if delegation is not None:
            result["codexDelegation"] = delegation
        if include_outputs:
            raw = item.get("output", "")
            text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            result["output"] = output_summary(text, output_chars)
    elif kind == "reasoning":
        result["summary"] = [truncate(text, 2000) for text in item.get("summary", [])]
        if include_outputs:
            result["content"] = [output_summary(text, output_chars) for text in item.get("content", [])]
    elif kind == "hookPrompt":
        result["fragmentCount"] = len(item.get("fragments", []))
    else:
        # Match the native tool projection, not every protocol field. In
        # particular commandActions and opaque future fields are not history
        # output contracts and must not bypass the result budget.
        fields = {
            "plan": {"text"},
            "commandExecution": {"command", "cwd", "exitCode", "status", "durationMs", "aggregatedOutput"},
            "fileChange": {"status", "changes"},
            "mcpToolCall": {"server", "tool", "arguments", "status", "durationMs"},
            "dynamicToolCall": {"namespace", "tool", "arguments", "status", "success", "durationMs"},
            "collabAgentToolCall": {"tool", "status", "senderThreadId", "receiverThreadIds", "prompt", "model", "reasoningEffort"},
            "subAgentActivity": {"kind", "agentThreadId", "agentPath"},
            "webSearch": {"query", "action"},
            "imageView": {"path"},
            "sleep": {"durationMs"},
            "imageGeneration": {"status", "revisedPrompt", "savedPath", "result"},
            "enteredReviewMode": {"review"},
            "exitedReviewMode": {"review"},
            "contextCompaction": set(),
        }.get(kind, set())
        prose = {
            "plan": {"text"}, "commandExecution": {"command"},
            "collabAgentToolCall": {"prompt"}, "webSearch": {"query"},
            "imageGeneration": {"revisedPrompt"},
            "enteredReviewMode": {"review"}, "exitedReviewMode": {"review"},
        }.get(kind, set())
        outputs = {"aggregatedOutput", "output", "result", "diff"}
        for key, value in item.items():
            if key not in fields:
                continue
            if key in {"id", "type", "processId"}:
                continue
            if key in outputs:
                if include_outputs and value is not None:
                    result["output" if key == "aggregatedOutput" else key] = output_summary(
                        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), output_chars,
                    )
            elif key == "changes" and isinstance(value, list):
                result[key] = [
                    {**{field: content for field, content in change.items() if field != "diff"},
                     **({"diff": output_summary(change["diff"], output_chars)} if include_outputs and "diff" in change else {})}
                    for change in value
                ]
            else:
                result[key] = truncate(value, 2000) if key in prose and isinstance(value, str) else value
    return result


def turn_summary(turn: dict[str, Any], include_outputs: bool, output_chars: int) -> dict[str, Any]:
    result = {key: turn.get(key) for key in ("id", "status", "startedAt", "completedAt", "durationMs")}
    error = turn.get("error")
    result["error"] = ({key: error.get(key) for key in ("message", "additionalDetails")} if error else None)
    result["items"] = [item_summary(item, include_outputs, output_chars) for item in turn.get("items", [])[-20:]]
    if len(turn.get("items", [])) > 20:
        result["itemsTruncated"] = True
    return result


def result_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _shorten_text(node: dict[str, Any] | None, key: str, limit: int, *, mark: bool = False) -> None:
    """Shorten one prose field selected by its result projection."""
    if node is None:
        return
    text = node.get(key)
    if isinstance(text, str) and len(text) > limit:
        node[key] = text[:limit] if mark else truncate(text, limit)
        if mark:
            node["truncated"] = True


def _shorten_result_text(value: dict[str, Any], limit: int) -> None:
    # These are the published list/wait/error projections. Unknown fields,
    # including nested tool arguments with names like "text", stay untouched.
    _shorten_text(value.get("error"), "message", limit)
    for thread in value.get("threads", []):
        _shorten_text(thread, "title", limit)
        _shorten_text(thread, "summary", limit)
    for poll in value.get("polls", []):
        _shorten_text(poll.get("latestAssistantMessage"), "text", limit)
        turn = poll.get("latestTurn")
        if turn is not None:
            _shorten_text(turn.get("error"), "message", limit)
            _shorten_text(turn.get("error"), "additionalDetails", limit)
    for error in value.get("errors", []):
        _shorten_text(error, "message", limit)


def fit_result(value: dict[str, Any]) -> dict[str, Any]:
    """Trim prose/items while retaining native page membership and cursors."""
    if result_bytes(value) <= MAX_RESULT_BYTES:
        return value
    value = copy.deepcopy(value)
    value["truncated"] = True
    if isinstance(value.get("turns"), list):
        # CLI 0.163.0's read strategy. Keep every Turn and the native page
        # cursor, plus Netizen metadata, while prioritizing each latest answer.
        value["thread"] = {key: value["thread"][key] for key in ("id", "status")}
        for index, turn in enumerate(value["turns"]):
            answer = next((item for item in reversed(turn["items"]) if item.get("type") == "agentMessage"), None)
            error = turn.get("error")
            value["turns"][index] = {
                "id": turn["id"], "status": turn["status"],
                "error": {"message": error.get("message")} if isinstance(error, dict) else None,
                "items": [answer] if answer is not None else [], "truncated": True,
            }
        if result_bytes(value) <= MAX_RESULT_BYTES:
            return value

        low, high = 0, MAX_RESULT_BYTES
        best = None
        while low <= high:
            limit = (low + high) // 2
            # Retry from the same compact result, without losing text to a
            # previous trial that chose a smaller per-field limit.
            candidate = copy.deepcopy(value)
            for turn in candidate["turns"]:
                _shorten_text(turn["error"], "message", limit, mark=True)
                for answer in turn["items"]:
                    _shorten_text(answer, "text", limit, mark=True)
            if result_bytes(candidate) <= MAX_RESULT_BYTES:
                best = candidate
                low = limit + 1
            else:
                high = limit - 1
        return best if best is not None else _oversized_result(value)

    for limit in (2000, 1000, 500, 250, 100, 20, 0):
        if result_bytes(value) <= MAX_RESULT_BYTES:
            return value
        _shorten_result_text(value, limit)
    polls = value.get("polls", [])
    while len(polls) > 1 and result_bytes(value) > MAX_RESULT_BYTES:
        wake_id = (value.get("wake") or {}).get("threadId")
        index = next((i for i in range(len(polls) - 1, -1, -1) if polls[i]["thread"]["id"] != wake_id), len(polls) - 1)
        polls.pop(index)
        value["pollsTruncated"] = True
    if result_bytes(value) <= MAX_RESULT_BYTES:
        return value
    return _oversized_result(value)


def _oversized_result(value: dict[str, Any]) -> dict[str, Any]:
    # A malformed or unusually large structured response cannot yield an
    # unbounded tool result. Preserve operation acceptance and exact receipt
    # identities even when optional payloads cannot fit.
    minimal = {"truncated": True, "error": {"code": "response_too_large", "message": "The result exceeded the output budget. Delivery acceptance, when present, is unchanged; do not resend automatically."}}
    for key in ("threadId", "turnId"):
        if key in value:
            minimal[key] = value[key]
    if "thread" in value:
        minimal["thread"] = {"id": value["thread"]["id"]}
    if "page" in value:
        minimal["page"] = value["page"]
    if "netizen" in value:
        minimal["netizen"] = {key: item for key, item in value["netizen"].items()
                              if key in {"accepted", "bindingId", "chatId", "topicId", "isCurrent", "disposition"}}
    if result_bytes(minimal) > MAX_RESULT_BYTES:
        # Protocol-invalid oversized identities are omitted, never presented as
        # shortened usable identifiers. Real native/Binding IDs are bounded.
        return {"truncated": True, "error": {"code": "response_too_large", "message": "Native response contained oversized identity fields; inspect the target before retrying."},
                "netizen": {"accepted": value.get("netizen", {}).get("accepted")}}
    return minimal
