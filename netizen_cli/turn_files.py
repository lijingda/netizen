"""Derive stateless current files from one native Turn."""

from __future__ import annotations

import ast
import logging
import re
import stat
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Literal

from openai_codex.generated.v2_all import FileChangeThreadItem
from openai_codex.types import ThreadItem

from .file_evidence import DiffPath, FileEvidence, OutputPath, PatchCount
from .pagination import ItemPage, PageError, paginate_items
from .turn_patch_children import TaskPatchChildren


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
    deleted: bool = False

    @property
    def available(self) -> bool:
        return self.size is not None and self.media_kind is not None


@dataclass(frozen=True, slots=True)
class TurnFilePage(ItemPage[TurnFile]):
    pass


@dataclass(frozen=True, slots=True)
class TurnDiffFileStats:
    """Optional line counts associated with one reported file path."""

    path: str
    additions: int | None = None
    deletions: int | None = None
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class TurnDiffSummary:
    """Display-safe paths and optional paired line counts."""

    additions: int | None = None
    deletions: int | None = None
    files: tuple[TurnDiffFileStats, ...] = ()


@dataclass(frozen=True, slots=True)
class _ParsedDiffBlock:
    path: str | None
    deleted: bool
    binary: bool
    counts: tuple[int, int] | None


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
    evidence = _patch_evidence(items, children, turn_diff)
    return summarize_file_evidence(evidence, project_cwd)


def project_file_evidence(
    items: Sequence[object], *, children: TaskPatchChildren = TaskPatchChildren(), turn_diff: str | None = None,
) -> FileEvidence:
    """Translate exact native facts without resolving a path or inspecting disk."""
    outputs = tuple(_item_paths(items))
    try:
        return _patch_evidence(items, children, turn_diff, outputs=outputs)
    except Exception:
        logging.getLogger(__name__).exception("failed to prepare completed patch statistics")
        return FileEvidence(outputs=outputs, patch_counts_complete=False)


def completed_file_evidence(
    result: object | None = None, *, children: TaskPatchChildren, turn_diff: str | None = None,
    final_items: tuple[object, ...] | None = None,
) -> FileEvidence:
    """Files are best effort and can never prevent delivery of the conclusion."""
    try:
        items = final_items if final_items is not None else tuple(getattr(result, "items", ()))
        return project_file_evidence(items, children=children, turn_diff=turn_diff)
    except Exception:
        logging.getLogger(__name__).exception("failed to project completed Turn file evidence")
        return FileEvidence()


def _patch_evidence(
    items: Sequence[object], children: TaskPatchChildren, turn_diff: str | None,
    *, outputs: tuple[OutputPath, ...] = (),
) -> FileEvidence:
    patches, complete = _patch_counts(items, children)
    return FileEvidence(patches, complete, tuple(_aggregate_paths(turn_diff or "")), outputs)


def _patch_counts(
    items: Sequence[object], children: TaskPatchChildren,
) -> tuple[tuple[PatchCount, ...], bool]:
    changes: list[PatchCount] = []
    complete = children.complete
    seen: set[tuple[str, str, str]] = set()
    batches = chain(
        (("", "", None, items),),
        ((child.thread_id, child.turn_id, child.cwd, child.items) for child in children.batches),
    )
    for thread_id, turn_id, cwd, batch_items in batches:
        # Final item views replace earlier lifecycle views of the same item.
        patches: dict[str, FileChangeThreadItem] = {}
        conflicting: set[str] = set()
        for wrapped in batch_items:
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
            identity = (thread_id, turn_id, item.id)
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
                value: tuple[int, int] | None | Exception = None
                if item.id not in conflicting:
                    try:
                        value = _file_change_counts(
                            change.diff, kind.type, getattr(kind, "move_path", None),
                        )
                    except Exception as error:
                        value = error.with_traceback(None)
                changes.append(PatchCount(
                    raw_path, cwd, value, kind.type == "delete" and item.id not in conflicting,
                ))
    return tuple(changes), complete


def summarize_file_evidence(evidence: FileEvidence, project_cwd: Path) -> TurnDiffSummary:
    """Resolve and combine patch paths at the time the reply is prepared."""
    counts: dict[str, tuple[int, int] | None] = {}
    deleted_paths: set[str] = set()
    additions = deletions = 0
    complete = evidence.patch_counts_complete
    saw_counts = False
    root_paths: set[str] = set()
    for patch in evidence.patches:
        try:
            path = _patch_path(patch.path, patch.cwd or project_cwd)
        except (OSError, RuntimeError, ValueError):
            complete = False
            continue
        if patch.cwd is None:
            root_paths.add(path)
        if patch.deleted:
            deleted_paths.add(path)
        value = patch.counts
        if isinstance(value, Exception):
            raise value
        if value is None:
            complete = False
        else:
            additions += value[0]
            deletions += value[1]
            saw_counts = True
        if path not in counts:
            counts[path] = value
        else:
            previous = counts[path]
            counts[path] = (previous[0] + value[0], previous[1] + value[1]) if previous and value else None

    # Aggregate diffs supplement discovery only, never the patch operation totals.
    for block in evidence.aggregate_paths:
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
            TurnDiffFileStats(
                path,
                additions=value[0] if value else None,
                deletions=value[1] if value else None,
                deleted=path in deleted_paths,
            )
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
        line_count = diff.count("\n") + int(bool(diff) and not diff.endswith("\n"))
        return (line_count, 0) if kind == "add" else (0, line_count)
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
    """Return current files and missing files with successful deletion evidence.

    The public ``ThreadItem.root`` discriminator is the only SDK shape used.
    Project is the base for relative paths, not an authorization boundary.
    Other missing and special files are omitted; this function never scans any
    directory to infer extra outputs.
    """

    summary = _reference_summary(diff_summary, turn_diff)
    return resolve_evidenced_files(
        project_file_evidence(items), project_cwd, summary, home_directory=home_directory,
    )


def resolve_evidenced_files(
    evidence: FileEvidence, project_cwd: Path, summary: TurnDiffSummary, *, home_directory: Path | None = None,
) -> tuple[TurnFile, ...]:
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
    for reported in chain(_summary_paths(summary), evidence.outputs):
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

    summary = _reference_summary(diff_summary, turn_diff)
    return has_file_evidence(project_file_evidence(items), summary)


def has_file_evidence(evidence: FileEvidence, summary: TurnDiffSummary) -> bool:
    return bool(summary.files or evidence.outputs)


def turn_diff_paths(diff: str | None) -> tuple[str, ...]:
    """Return current-side paths from a Git-style aggregate unified diff.

    Aggregate snapshots supplement file discovery, never patch line counts.
    Deleted targets are excluded; rename destinations and binary-file headers
    remain supported. Repeated paths retain their first reported position.
    """

    if not isinstance(diff, str) or not diff:
        return ()
    return tuple(dict.fromkeys(
        block.path for block in _aggregate_paths(diff)
        if block.path is not None and not block.deleted
    ))


def _aggregate_paths(diff: str) -> Iterator[DiffPath]:
    for lines in _diff_blocks(diff):
        block = _parse_diff_block(lines)
        yield DiffPath(block.path, block.deleted)


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
    return _ParsedDiffBlock(path, deleted, binary, counts)


def paginate_turn_files(
    files: Sequence[TurnFile],
    page: int,
    *,
    page_size: int = TURN_FILE_PAGE_SIZE,
) -> TurnFilePage:
    try:
        visible = paginate_items(files, page, page_size=page_size)
    except PageError as error:
        raise TurnFileError(f"本轮文件{error}请重新打开原卡片。") from error
    return TurnFilePage(
        items=visible.items,
        page=visible.page,
        total_pages=visible.total_pages,
        total_items=visible.total_items,
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


def _reference_summary(summary: TurnDiffSummary | None, turn_diff: str | None) -> TurnDiffSummary:
    # Without supplied statistics these callers report aggregate paths first,
    # then item outputs in native order, with no inferred patch counts.
    if summary is not None:
        return summary
    return TurnDiffSummary(files=tuple(TurnDiffFileStats(path) for path in turn_diff_paths(turn_diff)))


def _summary_paths(summary: TurnDiffSummary) -> Iterator[OutputPath]:
    for stats in summary.files:
        yield OutputPath(stats.path, deleted=stats.deleted, additions=stats.additions, deletions=stats.deletions)


@dataclass(frozen=True, slots=True)
class SavedImage:
    id: str
    saved_path: str
    type: str = "imageGeneration"
    status: str = "completed"


def direct_file_item(item: object) -> object | None:
    root = getattr(item, "root", item)
    if _enum_value(getattr(root, "status", None)) == "completed":
        if getattr(root, "type", None) == "fileChange":
            return root
        if getattr(root, "type", None) == "imageGeneration":
            if path := _path_value(getattr(root, "saved_path", None)):
                return SavedImage(getattr(root, "id", ""), path)
    return None


def _item_paths(items: Sequence[object]) -> Iterator[OutputPath]:
    for item in items:
        root = direct_file_item(item)
        if isinstance(root, SavedImage):
            yield OutputPath(root.saved_path, generated_image=True)
        elif root is not None:
            for change in getattr(root, "changes", ()):
                kind = getattr(getattr(change, "kind", None), "root", None)
                change_type = getattr(kind, "type", None)
                if change_type not in {"add", "update", "delete"}:
                    continue
                raw_path = getattr(change, "path", None)
                if change_type == "update":
                    move_path = getattr(kind, "move_path", None)
                    if isinstance(move_path, str) and move_path:
                        raw_path = move_path
                if isinstance(raw_path, str) and raw_path:
                    yield OutputPath(
                        raw_path, deleted=change_type == "delete",
                    )



def _resolve_turn_file(
    reported: OutputPath,
    root: Path,
    home: Path | None,
) -> TurnFile | None:
    try:
        candidate = Path(reported.path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except FileNotFoundError:
        if not reported.deleted:
            return None
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        return TurnFile(
            display_path=_display_path(reported, resolved, project_root=root, home=home),
            resolved_path=resolved,
            size=None,
            media_kind=None,
            additions=reported.additions,
            deletions=reported.deletions,
            deleted=True,
        )
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
    reported: OutputPath,
    resolved: Path,
    *,
    project_root: Path,
    home: Path | None,
) -> str:
    if resolved.is_relative_to(project_root):
        return resolved.relative_to(project_root).as_posix()
    if reported.generated_image:
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
    return is_supported_image_header(header)


def is_supported_image_header(header: bytes) -> bool:
    """Use the same PNG/JPEG/GIF/WebP signature for file sending and previews."""
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
