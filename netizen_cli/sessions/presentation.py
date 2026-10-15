"""Bounded native history projections for model tools, never Feishu cards."""
from __future__ import annotations

import copy
import json
from enum import Enum
from typing import Any

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
    # Like the native task tools, expose the delegated request even when raw
    # outputs are hidden. Only Netizen's own namespace, tools and envelope match.
    if item.get("namespace") != "netizen_sessions" or item.get("name") not in {
        "create_thread", "send_message_to_thread",
    } or not isinstance(item.get("output"), str):
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
                result[key] = truncate(value, 2000) if isinstance(value, str) and key not in {"senderThreadId", "agentThreadId"} else value
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


_PROTECTED = frozenset({"id", "threadId", "bindingId", "chatId", "topicId", "turnId", "cursor", "nextCursor", "afterCursor", "latestAssistantMessageId", "latestToolMarkerId", "projectId", "sourceThreadId", "senderThreadId", "receiverThreadIds", "agentThreadId", "code", "type", "status", "reason", "order", "disposition"})


def fit_result(value: dict[str, Any]) -> dict[str, Any]:
    """Trim prose and older history while retaining valid IDs and pagination."""
    if result_bytes(value) <= MAX_RESULT_BYTES:
        return value
    value = copy.deepcopy(value)
    value["truncated"] = True
    turns = value.get("turns", [])
    while len(turns) > 1 and result_bytes(value) > MAX_RESULT_BYTES:
        turns.pop()
        value["page"].update(hasMore=True, nextCursor=turns[-1]["id"])
    # Keep the newest answer preferentially; remove older non-answer items.
    for turn in turns:
        items = turn.get("items", [])
        while len(items) > 1 and result_bytes(value) > MAX_RESULT_BYTES:
            last_answer = next((i for i in range(len(items) - 1, -1, -1) if items[i].get("type") == "agentMessage"), None)
            index = next((i for i, item in enumerate(items) if item.get("type") != "agentMessage"),
                         next((i for i in range(len(items)) if i != last_answer), 0))
            items.pop(index)
            turn["itemsTruncated"] = True
    def shorten(node: Any, limit: int) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(item, str) and key not in _PROTECTED:
                    node[key] = truncate(item, limit)
                elif key not in _PROTECTED:
                    shorten(item, limit)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                if isinstance(item, str):
                    node[index] = truncate(item, limit)
                else:
                    shorten(item, limit)
    for limit in (2000, 1000, 500, 250, 100, 20, 0):
        if result_bytes(value) <= MAX_RESULT_BYTES:
            return value
        shorten(value, limit)
    # Large arbitrary tool arguments/structured output can contain many short
    # values. Replace these payloads explicitly, never identifiers/cursors.
    def prune(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in list(node.items()):
                if key in {"arguments", "content", "changes", "output", "result", "action", "summary"} and isinstance(item, (dict, list)):
                    node[key] = {"truncated": True}
                else:
                    prune(item)
        elif isinstance(node, list):
            for item in node:
                prune(item)
    prune(value)
    # Bounded native identity arrays retain each complete identifier. A
    # truncated array is explicit; individual IDs are never shortened.
    def trim_arrays(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in list(node.items()):
                if key == "receiverThreadIds" and isinstance(item, list):
                    if len(item) > 8:
                        node[key] = item[:8]
                        node[key + "Truncated"] = True
                else:
                    trim_arrays(item)
        elif isinstance(node, list):
            for item in node:
                trim_arrays(item)
    trim_arrays(value)
    polls = value.get("polls", [])
    while len(polls) > 1 and result_bytes(value) > MAX_RESULT_BYTES:
        wake_id = (value.get("wake") or {}).get("threadId")
        index = next((i for i in range(len(polls) - 1, -1, -1) if polls[i]["thread"]["id"] != wake_id), len(polls) - 1)
        polls.pop(index)
        value["pollsTruncated"] = True
    if result_bytes(value) <= MAX_RESULT_BYTES:
        return value
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
