"""Ordered facts about one execution's outputs, before filesystem inspection."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PatchCount:
    path: str
    # Child-specific base. Root patches use the execution's captured cwd.
    cwd: Path | None
    counts: tuple[int, int] | None
    deleted: bool


@dataclass(frozen=True, slots=True)
class DiffPath:
    path: str | None
    deleted: bool


@dataclass(frozen=True, slots=True)
class OutputPath:
    path: str
    generated_image: bool = False
    deleted: bool = False
    additions: int | None = None
    deletions: int | None = None


@dataclass(frozen=True, slots=True)
class FileEvidence:
    patches: tuple[PatchCount, ...] = ()
    patch_counts_complete: bool = True
    aggregate_paths: tuple[DiffPath, ...] = ()
    outputs: tuple[OutputPath, ...] = ()
