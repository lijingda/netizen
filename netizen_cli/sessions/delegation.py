"""The native CLI delegation envelope used for previews and history display."""
from __future__ import annotations


_PREFIX = "<codex_delegation>\n  <source_thread_id>"
_SEPARATOR = "</source_thread_id>\n  <input>"
_SUFFIX = "</input>\n</codex_delegation>"


def delegated_prompt(source_thread_id: str, prompt: str) -> str:
    # Match the CLI's wire text exactly: Codex recognizes this envelope when
    # deriving a native preview from an ExternalMessage tool output.
    def escape(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return _PREFIX + escape(source_thread_id) + _SEPARATOR + escape(prompt) + _SUFFIX


def parse_delegated_prompt(value: str) -> tuple[str, str] | None:
    if not value.startswith(_PREFIX) or not value.endswith(_SUFFIX):
        return None
    source, separator, prompt = value[len(_PREFIX):-len(_SUFFIX)].partition(_SEPARATOR)
    if not separator:
        return None

    def unescape(text: str) -> str:
        return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

    return unescape(source), unescape(prompt)
