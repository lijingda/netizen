"""Derive stateless current files from one native Turn."""

from __future__ import annotations

import ast
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openai_codex.types import ThreadItem


TURN_FILE_PAGE_SIZE = 8


class TurnFileError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TurnFile:
    display_path: str
    resolved_path: Path
    size: int | None
    media_kind: Literal["image", "file"] | None

    @property
    def available(self) -> bool:
        return self.size is not None and self.media_kind is not None


@dataclass(frozen=True, slots=True)
class TurnFilePage:
    items: tuple[TurnFile, ...]
    page: int
    total_pages: int
    total_items: int


@dataclass(frozen=True, slots=True)
class _ReportedTurnPath:
    item_type: Literal["turnDiff", "fileChange", "imageGeneration"]
    value: str


def extract_turn_files(
    items: Sequence[ThreadItem],
    project_cwd: Path,
    *,
    turn_diff: str | None = None,
    home_directory: Path | None = None,
) -> tuple[TurnFile, ...]:
    """Return current regular files named by supported completed Turn items.

    The public ``ThreadItem.root`` discriminator is the only SDK shape used.
    Project is the base for relative paths, not an authorization boundary.
    Missing, deleted, and special files are omitted; this function never scans
    any directory to infer extra outputs.
    """

    try:
        root = project_cwd.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise TurnFileError("Project 目录当前不可用。") from error
    if not root.is_dir():
        raise TurnFileError("Project 路径不是目录。")

    try:
        home = (
            Path.home() if home_directory is None else home_directory
        ).resolve(strict=True)
    except (OSError, RuntimeError):
        home = None

    results: list[TurnFile] = []
    seen: set[Path] = set()
    for reported in _reported_paths(items, turn_diff=turn_diff):
        turn_file = _resolve_turn_file(reported, root, home)
        if turn_file is None or turn_file.resolved_path in seen:
            continue
        seen.add(turn_file.resolved_path)
        results.append(turn_file)
    return tuple(results)


def has_turn_file_references(
    items: Sequence[object],
    *,
    turn_diff: str | None = None,
) -> bool:
    """Return whether supported structured items name at least one output path."""

    return next(iter(_reported_paths(items, turn_diff=turn_diff)), None) is not None


def turn_diff_paths(diff: str | None) -> tuple[str, ...]:
    """Return current-side paths from a Git-style aggregate unified diff.

    App Server's ``turn/diff/updated`` payload is an aggregate snapshot.  This
    parser reads only file-level metadata and never interprets hunk contents.
    Deleted targets are excluded; rename destinations and binary-file headers
    are supported.
    """

    if not isinstance(diff, str) or not diff:
        return ()

    results: list[str] = []
    header_target: str | None = None
    target: str | None = None
    rename_target: str | None = None
    binary_target: str | None = None
    target_deleted = False
    in_hunk = False
    have_block = False

    def finish_block() -> None:
        nonlocal header_target, target, rename_target, binary_target
        nonlocal target_deleted, in_hunk, have_block
        if have_block and not target_deleted:
            selected = rename_target or target or binary_target or header_target
            normalized = _normalize_diff_path(selected)
            if normalized is not None:
                results.append(normalized)
        header_target = None
        target = None
        rename_target = None
        binary_target = None
        target_deleted = False
        in_hunk = False
        have_block = False

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            finish_block()
            have_block = True
            header_paths = _git_header_paths(line[len("diff --git ") :])
            if header_paths is not None:
                header_target = header_paths[1]
            continue
        if line.startswith("--- ") and not in_hunk:
            if not have_block:
                have_block = True
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not have_block or in_hunk:
            continue
        if line.startswith("+++ "):
            parsed = _metadata_path(line[4:])
            target_deleted = parsed == "/dev/null"
            if not target_deleted:
                target = parsed
            continue
        if line.startswith("rename to "):
            rename_target = _metadata_path(line[len("rename to ") :])
            continue
        if line.startswith("Binary files ") and line.endswith(" differ"):
            pair = line[len("Binary files ") : -len(" differ")]
            if " and " in pair:
                binary_target = _metadata_path(pair.rsplit(" and ", 1)[1])

    finish_block()
    return tuple(results)


def paginate_turn_files(
    files: Sequence[TurnFile],
    page: int,
    *,
    page_size: int = TURN_FILE_PAGE_SIZE,
) -> TurnFilePage:
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        raise TurnFileError("本轮文件页码无效，请重新打开原卡片。")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or page_size < 1
    ):
        raise ValueError("page_size must be a positive integer")
    total_items = len(files)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    if page >= total_pages:
        raise TurnFileError("本轮文件页码已过期，请重新打开原卡片。")
    start = page * page_size
    return TurnFilePage(
        items=tuple(files[start : start + page_size]),
        page=page,
        total_pages=total_pages,
        total_items=total_items,
    )


def inspect_turn_file_path(path: str, display_path: str) -> TurnFile:
    """Inspect the current file named by a v4 card manifest entry.

    The absolute path is the callback capability selected for this product
    boundary.  It is intentionally not constrained to a registered Project.
    Missing, inaccessible, directory, and special-file targets remain visible
    during pagination but are marked unavailable and cannot be sent.
    """

    candidate = Path(path)
    if not candidate.is_absolute():
        return TurnFile(display_path, candidate, None, None)
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError, ValueError):
        return TurnFile(display_path, candidate, None, None)
    if not stat.S_ISREG(metadata.st_mode):
        return TurnFile(display_path, resolved, None, None)
    return TurnFile(
        display_path=display_path,
        resolved_path=resolved,
        size=metadata.st_size,
        media_kind="image" if _is_supported_image(resolved) else "file",
    )


def require_turn_file_path(path: str, display_path: str = "") -> TurnFile:
    turn_file = inspect_turn_file_path(path, display_path or Path(path).name)
    if not turn_file.available:
        raise TurnFileError(
            "该文件当前已不可用：路径不存在、不可访问或不是普通文件，未发送。"
        )
    return turn_file


def format_file_size(size: int) -> str:
    if size < 0:
        raise ValueError("file size must be non-negative")
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value / 1024:.1f} TB"


def _reported_paths(
    items: Sequence[object],
    *,
    turn_diff: str | None = None,
) -> Iterable[_ReportedTurnPath]:
    for path in turn_diff_paths(turn_diff):
        yield _ReportedTurnPath("turnDiff", path)
    for item in items:
        root = getattr(item, "root", item)
        item_type = getattr(root, "type", None)
        status = _enum_value(getattr(root, "status", None))
        if item_type == "fileChange" and status == "completed":
            for change in getattr(root, "changes", ()):
                kind = getattr(getattr(change, "kind", None), "root", None)
                change_type = getattr(kind, "type", None)
                if change_type not in {"add", "update"}:
                    continue
                raw_path = getattr(change, "path", None)
                if change_type == "update":
                    move_path = getattr(kind, "move_path", None)
                    if isinstance(move_path, str) and move_path:
                        raw_path = move_path
                if isinstance(raw_path, str) and raw_path:
                    yield _ReportedTurnPath("fileChange", raw_path)
        elif item_type == "imageGeneration" and status == "completed":
            saved_path = _path_value(getattr(root, "saved_path", None))
            if saved_path is not None:
                yield _ReportedTurnPath("imageGeneration", saved_path)


def _resolve_turn_file(
    reported: _ReportedTurnPath,
    root: Path,
    home: Path | None,
) -> TurnFile | None:
    try:
        candidate = Path(reported.value)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    display_path = _display_path(
        reported,
        resolved,
        project_root=root,
        home=home,
    )
    return TurnFile(
        display_path=display_path,
        resolved_path=resolved,
        size=metadata.st_size,
        media_kind="image" if _is_supported_image(resolved) else "file",
    )

def _display_path(
    reported: _ReportedTurnPath,
    resolved: Path,
    *,
    project_root: Path,
    home: Path | None,
) -> str:
    if resolved.is_relative_to(project_root):
        return resolved.relative_to(project_root).as_posix()
    if reported.item_type == "imageGeneration":
        return f"生成图片/{resolved.name}"
    if home is not None and resolved.is_relative_to(home):
        return f"~/{resolved.relative_to(home).as_posix()}"
    parts = tuple(part for part in resolved.parts if part != resolved.anchor)
    visible = parts[-3:]
    prefix = "外部文件/…/" if len(parts) > len(visible) else "外部文件/"
    return prefix + "/".join(visible)


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _path_value(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    root = getattr(value, "root", None)
    return root if isinstance(root, str) and root else None


def _is_supported_image(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(12)
    except OSError:
        return False
    return (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or header.startswith((b"GIF87a", b"GIF89a"))
        or (
            len(header) >= 12
            and header[:4] == b"RIFF"
            and header[8:12] == b"WEBP"
        )
    )


def _git_header_paths(value: str) -> tuple[str, str] | None:
    tokens: list[str] = []
    index = 0
    while index < len(value):
        while index < len(value) and value[index].isspace():
            index += 1
        if index >= len(value):
            break
        start = index
        if value[index] == '"':
            index += 1
            escaped = False
            while index < len(value):
                character = value[index]
                index += 1
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    break
            else:
                return None
        else:
            while index < len(value) and not value[index].isspace():
                index += 1
        token = _decode_git_path(value[start:index])
        if token is None:
            return None
        tokens.append(token)
    if len(tokens) != 2:
        return None
    return tokens[0], tokens[1]


def _metadata_path(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith('"'):
        index = 1
        escaped = False
        while index < len(value):
            character = value[index]
            index += 1
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                return _decode_git_path(value[:index])
        return None
    # Traditional unified diffs may append a timestamp after a tab.
    return value.split("\t", 1)[0]


def _decode_git_path(value: str) -> str | None:
    if not value:
        return None
    if not value.startswith('"'):
        return value
    try:
        decoded = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(decoded, str):
        return None
    try:
        return decoded.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return decoded


def _normalize_diff_path(value: str | None) -> str | None:
    if not isinstance(value, str) or not value or value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value or None
