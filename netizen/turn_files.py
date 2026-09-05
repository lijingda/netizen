"""Derive stateless current files from one native Turn."""

from __future__ import annotations

import ast
import re
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openai_codex.generated.v2_all import FileChangeThreadItem
from openai_codex.types import ThreadItem

from .turn_patch_children import TaskPatchChildren, TurnPatchBatch


TURN_FILE_PAGE_SIZE = 8


class TurnFileError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TurnFile:
    display_path: str
    resolved_path: Path
    size: int | None
    media_kind: Literal["image", "file"] | None
    additions: int | None = None
    deletions: int | None = None

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
class TurnDiffFileStats:
    """Optional line counts associated with one reported file path."""

    path: str
    additions: int | None = None
    deletions: int | None = None


@dataclass(frozen=True, slots=True)
class TurnDiffSummary:
    """Display-safe paths and optional paired line counts."""

    additions: int | None = None
    deletions: int | None = None
    files: tuple[TurnDiffFileStats, ...] = ()


@dataclass(frozen=True, slots=True)
class _ReportedTurnPath:
    item_type: Literal["turnDiff", "fileChange", "imageGeneration"]
    value: str
    additions: int | None = None
    deletions: int | None = None


@dataclass(frozen=True, slots=True)
class _ParsedDiffBlock:
    path: str | None
    deleted: bool
    binary: bool
    counts: tuple[int, int] | None
    malformed: bool


_HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@(?:.*)$"
)


def turn_patch_summary(
    items: Sequence[object],
    project_cwd: Path,
    *,
    children: TaskPatchChildren = TaskPatchChildren(),
    turn_diff: str | None = None,
) -> TurnDiffSummary:
    """Count successful patch operations, including proven new descendants.

    Repeated edits accumulate; this is not a final net diff. Native aggregate
    diffs supplement paths only, so their overlapping counts are never added.
    Unreadable children or patches omit the total, retaining known file counts.
    """
    counts: dict[str, tuple[int, int] | None] = {}
    additions = deletions = 0
    complete = children.complete
    saw_counts = False
    root_paths: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    batches = (TurnPatchBatch("", "", project_cwd, tuple(items)), *children.batches)
    for batch in batches:
        # Final item views replace earlier lifecycle views of the same item.
        patches: dict[str, FileChangeThreadItem] = {}
        conflicting: set[str] = set()
        for wrapped in batch.items:
            item = getattr(wrapped, "root", wrapped)
            if type(item) is not FileChangeThreadItem:
                continue
            previous_item = patches.get(item.id)
            if (
                previous_item is not None
                and _enum_value(previous_item.status) == "completed"
                and _enum_value(item.status) == "completed"
                and previous_item != item
            ):
                conflicting.add(item.id)
            patches[item.id] = item
        for item in patches.values():
            identity = (batch.thread_id, batch.turn_id, item.id)
            if identity in seen:
                continue
            seen.add(identity)
            if _enum_value(item.status) != "completed":
                if _enum_value(item.status) == "inProgress":
                    complete = False
                continue
            for change in item.changes:
                kind = change.kind.root
                raw_path = getattr(kind, "move_path", None) or change.path
                try:
                    path = _patch_path(raw_path, batch.cwd)
                except (OSError, RuntimeError, ValueError):
                    complete = False
                    continue
                if not batch.thread_id:
                    root_paths.add(path)
                value = (
                    None if item.id in conflicting else _file_change_counts(
                        change.diff, kind.type, getattr(kind, "move_path", None),
                    )
                )
                if value is None:
                    complete = False
                else:
                    additions += value[0]
                    deletions += value[1]
                    saw_counts = True
                if path not in counts:
                    counts[path] = value
                elif counts[path] is None or value is None:
                    counts[path] = None
                else:
                    previous = counts[path]
                    assert previous is not None
                    counts[path] = (previous[0] + value[0], previous[1] + value[1])

    # Preserve aggregate-only file discovery without mixing two count metrics.
    for lines in _diff_blocks(turn_diff or ""):
        block = _parse_diff_block(lines)
        if block.path is None:
            complete = False
            continue
        try:
            path = _patch_path(block.path, project_cwd)
        except (OSError, RuntimeError, ValueError):
            complete = False
            continue
        if path not in root_paths:
            complete = False
            if not block.deleted or path in counts:
                counts[path] = None
    return TurnDiffSummary(
        additions if complete and saw_counts else None,
        deletions if complete and saw_counts else None,
        tuple(
            TurnDiffFileStats(path, *(value or (None, None)))
            for path, value in counts.items()
        ),
    )


def _patch_path(value: str, cwd: Path) -> str:
    if not value or "\0" in value:
        raise ValueError("invalid patch path")
    path = Path(value)
    return str((path if path.is_absolute() else cwd / path).resolve())


def _file_change_counts(
    diff: str, kind: str, move_path: str | None,
) -> tuple[int, int] | None:
    if "\0" in diff:
        return None
    if kind in {"add", "delete"}:
        # SDK add/delete contain whole text, not a unified diff. Count LF only;
        # a final newline terminates the last line rather than adding a line.
        lines = diff.count("\n") + int(bool(diff) and not diff.endswith("\n"))
        return (lines, 0) if kind == "add" else (0, lines)
    if kind != "update":
        return None
    if move_path:
        suffix = f"\n\nMoved to: {move_path}"
        if diff.endswith(suffix):
            diff = diff[:-len(suffix)]
        if not diff:
            return (0, 0)
    # Generated update patches can have file headers or start directly with a
    # hunk. Reuse the validated hunk parser; paths come from the typed change.
    lines = [line.removesuffix("\r") for line in diff.split("\n")]
    if lines and not lines[-1]:
        lines.pop()
    if diff.startswith("@@ "):
        lines = ["--- a/file", "+++ b/file", *lines]
    if not diff.startswith("diff --git "):
        lines.insert(0, "diff --git a/file b/file")
    if sum(line.startswith("diff --git ") for line in lines) != 1:
        return None
    block = _parse_diff_block(lines)
    return block.counts if not block.binary else None


def extract_turn_files(
    items: Sequence[ThreadItem],
    project_cwd: Path,
    *,
    turn_diff: str | None = None,
    diff_summary: TurnDiffSummary | None = None,
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
    for reported in _reported_paths(
        items,
        turn_diff=turn_diff,
        diff_summary=diff_summary,
    ):
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
    diff_summary: TurnDiffSummary | None = None,
) -> bool:
    """Return whether supported structured items name at least one output path."""

    return next(
        _reported_paths(
            items,
            turn_diff=turn_diff,
            diff_summary=diff_summary,
        ),
        None,
    ) is not None


def turn_diff_paths(diff: str | None) -> tuple[str, ...]:
    """Return current-side paths from a Git-style aggregate unified diff.

    App Server's ``turn/diff/updated`` payload is an aggregate snapshot.  This
    is the compatibility wrapper for callers that only need paths. Deleted
    targets are excluded; rename destinations and binary-file headers remain
    supported.
    """

    return tuple(item.path for item in turn_diff_summary(diff).files)


def turn_diff_summary(diff: str | None) -> TurnDiffSummary:
    """Parse paths and line counts from the latest aggregate unified diff.

    A malformed hunk invalidates all numeric statistics while preserving any
    independently parseable current-side paths. Binary blocks keep their path
    but never receive line counts. Aggregate totals include valid deleted-file
    hunks even though deleted targets cannot appear in the current file list.
    """

    if not isinstance(diff, str) or not diff:
        return TurnDiffSummary()

    first_header = re.search(r"(?m)^diff --git ", diff)
    malformed_prelude = (
        first_header is not None
        and bool(diff[: first_header.start()].strip())
    )
    blocks = tuple(_parse_diff_block(lines) for lines in _diff_blocks(diff))
    merged: list[TurnDiffFileStats] = []
    positions: dict[str, int] = {}
    for block in blocks:
        if block.deleted or block.path is None:
            continue
        item = TurnDiffFileStats(
            block.path,
            *(block.counts or (None, None)),
        )
        position = positions.get(item.path)
        if position is None:
            positions[item.path] = len(merged)
            merged.append(item)
            continue
        previous = merged[position]
        if (
            previous.additions is None
            or previous.deletions is None
            or item.additions is None
            or item.deletions is None
        ):
            merged[position] = TurnDiffFileStats(item.path)
        else:
            merged[position] = TurnDiffFileStats(
                item.path,
                previous.additions + item.additions,
                previous.deletions + item.deletions,
            )
    if malformed_prelude or any(block.malformed for block in blocks):
        return TurnDiffSummary(
            files=tuple(TurnDiffFileStats(item.path) for item in merged)
        )
    numeric = tuple(block.counts for block in blocks if block.counts is not None)
    totals_known = bool(numeric) and all(
        block.counts is not None or block.binary for block in blocks
    )
    return TurnDiffSummary(
        additions=sum(counts[0] for counts in numeric) if totals_known else None,
        deletions=sum(counts[1] for counts in numeric) if totals_known else None,
        files=tuple(merged),
    )


def _diff_blocks(diff: str) -> Iterable[tuple[str, ...]]:
    current: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current:
                yield tuple(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        yield tuple(current)


def _parse_diff_block(lines: Sequence[str]) -> _ParsedDiffBlock:
    header = _git_header_paths(lines[0][len("diff --git ") :])
    header_target = header[1] if header is not None else None
    source_header = False
    target_header = False
    target: str | None = None
    rename_source: str | None = None
    rename_target: str | None = None
    copy_target: str | None = None
    deleted = False
    binary = False
    rename_only = True
    similarity_seen = False
    additions = 0
    deletions = 0
    saw_hunk = False
    malformed = False
    old_expected: int | None = None
    new_expected: int | None = None
    old_seen = 0
    new_seen = 0

    for line in lines[1:]:
        if line.startswith("@@"):
            if old_expected is not None and (
                old_seen != old_expected or new_seen != new_expected
            ):
                malformed = True
            saw_hunk = True
            rename_only = False
            match = _HUNK_HEADER.fullmatch(line)
            if match is None:
                malformed = True
                old_expected = new_expected = None
                continue
            old_expected = int(match.group(1) or "1")
            new_expected = int(match.group(2) or "1")
            old_seen = new_seen = 0
            continue
        if old_expected is not None:
            if line.startswith("+"):
                additions += 1
                new_seen += 1
            elif line.startswith("-"):
                deletions += 1
                old_seen += 1
            elif line.startswith(" "):
                old_seen += 1
                new_seen += 1
            elif line != r"\ No newline at end of file":
                malformed = True
            if old_seen > old_expected or new_seen > new_expected:
                malformed = True
            continue
        if line.startswith("--- "):
            parsed = _metadata_path(line[4:])
            malformed = malformed or source_header or parsed is None
            source_header = True
            rename_only = False
        elif line.startswith("+++ "):
            parsed = _metadata_path(line[4:])
            malformed = malformed or target_header or parsed is None
            target_header = True
            rename_only = False
            if parsed == "/dev/null":
                deleted = True
            elif parsed is not None:
                target = parsed
        elif line.startswith("rename from "):
            parsed = _metadata_path(line[len("rename from ") :])
            if rename_source is not None or parsed is None:
                rename_only = False
            else:
                rename_source = parsed
        elif line.startswith("rename to "):
            parsed = _metadata_path(line[len("rename to ") :])
            if rename_target is not None or parsed is None:
                rename_only = False
            else:
                rename_target = parsed
        elif line == "similarity index 100%" and not similarity_seen:
            similarity_seen = True
        elif line.startswith("copy to "):
            copy_target = _metadata_path(line[len("copy to ") :])
            rename_only = False
        elif line.startswith("deleted file mode "):
            deleted = True
            rename_only = False
        elif line.startswith("Binary files ") and line.endswith(" differ"):
            binary = True
            rename_only = False
            deleted = deleted or line.endswith(" and /dev/null differ")
        elif line == "GIT binary patch":
            binary = True
            rename_only = False
        elif (
            line.startswith(("+", "-", " "))
            or line == r"\ No newline at end of file"
        ):
            malformed = True
            rename_only = False
        elif line:
            rename_only = False

    if old_expected is not None and (
        old_seen != old_expected or new_seen != new_expected
    ):
        malformed = True
    if (source_header or target_header) and not saw_hunk:
        malformed = True
    if saw_hunk and (
        binary
        or not source_header
        or not target_header
        or (not deleted and target is None)
    ):
        malformed = True
    if saw_hunk and target is not None:
        path = _normalize_diff_path(target)
    elif rename_target is not None:
        path = None if rename_target == "/dev/null" else rename_target
    elif copy_target is not None:
        path = None if copy_target == "/dev/null" else copy_target
    else:
        path = _normalize_diff_path(header_target)
    pure_rename = (
        rename_only
        and similarity_seen
        and rename_source is not None
        and rename_target is not None
    )
    counts = (
        (additions, deletions)
        if not malformed and (saw_hunk or pure_rename)
        else None
    )
    return _ParsedDiffBlock(path, deleted, binary, counts, malformed)


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


def _reported_paths(
    items: Sequence[object],
    *,
    turn_diff: str | None = None,
    diff_summary: TurnDiffSummary | None = None,
) -> Iterable[_ReportedTurnPath]:
    summary = diff_summary if diff_summary is not None else turn_diff_summary(turn_diff)
    for file_stats in summary.files:
        yield _ReportedTurnPath(
            "turnDiff",
            file_stats.path,
            file_stats.additions,
            file_stats.deletions,
        )
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
    media_kind: Literal["image", "file"] = (
        "image" if _is_supported_image(resolved) else "file"
    )
    return TurnFile(
        display_path=display_path,
        resolved_path=resolved,
        size=metadata.st_size,
        media_kind=media_kind,
        additions=(reported.additions if media_kind == "file" else None),
        deletions=(reported.deletions if media_kind == "file" else None),
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
    if len(tokens) == 2:
        return tokens[0], tokens[1]
    same_path = re.fullmatch(r"a/(.+) b/\1", value)
    if same_path is None:
        return None
    path = same_path.group(1)
    return f"a/{path}", f"b/{path}"


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
