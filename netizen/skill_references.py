"""Parse the explicit, current-message Skill reference prefix."""

from __future__ import annotations

import re


_SKILL_REFERENCE = re.compile(r"\$([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})(?=\s|$)")


class InvalidSkillReference(ValueError):
    pass


def parse_skill_references(text: str) -> tuple[str, ...]:
    """Return the leading ``$name`` block, preserving user order.

    Restricting activation to a leading reference block avoids interpreting
    shell variables, prose, or code later in a prompt as executable Skills.
    The caller must parse only the current user message, never quoted/history
    material.
    """

    position = 0
    length = len(text)
    while position < length and text[position].isspace():
        position += 1
    names: list[str] = []
    while position < length and text[position] == "$":
        match = _SKILL_REFERENCE.match(text, position)
        if match is None:
            break
        name = match.group(1)
        if name in names:
            raise InvalidSkillReference(f"Skill ${name} 在同一条消息中重复引用。")
        names.append(name)
        position = match.end()
        while position < length and text[position].isspace():
            position += 1
    return tuple(names)
