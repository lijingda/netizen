"""Ephemeral HEAD candidates for the Project viewer, using the isolated Git runner."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from ..project_files import MAX_TEXT_BYTES, FileBrowseError, _path_parts, preview_text
from .git import GitError, WorktreeGit, is_not_repository


def unavailable(message: str = "Git 暂不可用，请稍后重试。") -> dict:
    return {"git": "unavailable", "message": message}


@dataclass
class Candidate:
    path: str
    head: str | None = None
    old_mode: str = "000000"
    new_mode: str = "000000"
    worktree: bool = False
    tracked: bool = False
    untracked: bool = False
    ignored: bool = False
    conflict: bool = False
    directory: bool = False

    @property
    def group(self) -> str:
        return "tracked" if self.tracked else "untracked" if self.untracked else "ignored"

    @property
    def kind(self) -> str | None:
        if self.conflict:
            return "conflict"
        if self.head:
            return "modified" if self.worktree else "deleted"
        if self.worktree:
            return "added" if self.tracked else "untracked" if self.untracked else "ignored"
        return None


class Status:
    def __init__(self, prefix: str, scope: str, cap: int) -> None:
        if cap < 1:
            raise ValueError("entry cap must be positive")
        self.prefix, self.scope, self.cap = prefix, scope, cap
        self.head: str | None = None
        self.saw_head = False
        self.entries: dict[str, Candidate] = {}
        self.count = 0
        self.truncated = False
        self.group = "tracked"
        self.groups_complete = dict.fromkeys(("tracked", "untracked", "ignored"), False)
        self.invalid: set[str] = set()
        self.rename: list[str] | None = None

    def entry(self, path: str) -> Candidate | None:
        directory = path.endswith("/")
        if not path.startswith(self.prefix):
            return None
        path = path[len(self.prefix):].removesuffix("/")
        try:
            _path_parts(path, allow_empty=False)
        except FileBrowseError:
            self.invalid.add(self.group)
            return None
        if self.scope and path != self.scope and not path.startswith(self.scope + "/"):
            return None
        entry = self.entries.setdefault(path, Candidate(path))
        entry.directory |= directory
        return entry

    def old(self, entry: Candidate | None, mode: str, oid: str) -> None:
        if entry is None:
            return
        entry.tracked = True
        if set(oid) != {"0"}:
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
                raise GitError("status", "Invalid HEAD object")
            entry.head, entry.old_mode = oid, mode

    def record(self, record: str) -> bool:
        if self.rename is not None:
            fields, self.rename = self.rename, None
            self.old(self.entry(record), fields[3], fields[6])
        elif record.startswith("# "):
            if record.startswith("# branch.oid "):
                oid = record.removeprefix("# branch.oid ")
                if oid != "(initial)" and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
                    raise GitError("status", "Invalid HEAD")
                self.head = None if oid == "(initial)" else oid
                self.saw_head = True
            return False
        else:
            kind = record[:1]
            group = "untracked" if kind == "?" else "ignored" if kind == "!" else "tracked"
            order = ["tracked", "untracked", "ignored"]
            for previous in order[:order.index(group)]:
                self.groups_complete[previous] = previous not in self.invalid
            self.group = group
            if kind in {"?", "!"}:
                entry = self.entry(record[2:])
                if entry:
                    entry.worktree = True
                    entry.untracked |= kind == "?"
                    entry.ignored |= kind == "!"
            elif kind in {"1", "2", "u"}:
                count = {"1": 8, "2": 9, "u": 10}[kind]
                fields = record.split(" ", count)
                if len(fields) != count + 1:
                    raise GitError("status", "Invalid status record")
                entry = self.entry(fields[-1])
                if entry:
                    entry.tracked = True
                    entry.conflict |= kind == "u"
                    entry.new_mode = fields[5] if kind != "u" else fields[6]
                    entry.worktree |= ("D" not in fields[1] if kind == "1" else fields[1][1] != "D")
                if kind == "1":
                    self.old(entry, fields[3], fields[6])
                elif kind == "2":
                    self.rename = fields
                    return False  # A rename and its original path are one record.
            else:
                raise GitError("status", "Unknown status record")
        self.count += 1
        self.truncated = self.count >= self.cap
        return self.truncated

    def finish(self) -> None:
        if not self.saw_head or self.rename is not None:
            raise GitError("status", "Incomplete status header or rename")
        if not self.truncated:
            self.groups_complete = {key: key not in self.invalid for key in self.groups_complete}

    def response(self) -> dict:
        return {"git": "available", "head": self.head,
            "complete": not self.truncated and not self.invalid,
            "groupsComplete": self.groups_complete,
            "entries": [{"path": entry.path, "kind": entry.kind, "group": entry.group,
                         "directory": entry.directory}
                        for entry in self.entries.values() if entry.kind is not None]}


class ViewerGit(WorktreeGit):
    def __init__(self, *, timeout: float = 10, entry_cap: int = 10000) -> None:
        super().__init__(timeout=timeout, environment={"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""})
        self.entry_cap = entry_cap

    async def status(self, root: Path, path: str) -> Status:
        _path_parts(path, allow_empty=True)
        filters = await self._filters(root)
        await self._run(root, "rev-parse", "--show-toplevel", options=filters)
        _, prefix = await self._run(root, "rev-parse", "--show-prefix", options=filters)
        status = Status(prefix.removesuffix("\n"), path, self.entry_cap)
        await self._run(root, "status", "--porcelain=v2", "-z", "--branch", "--no-ahead-behind",
                        "-uall", "--ignored=matching", "--ignore-submodules=all", "--", path or ".",
                        options=filters, on_record=status.record)
        status.finish()
        return status

    async def old_text(self, root: Path, oid: str) -> str:
        filters = await self._filters(root)
        try:
            _, size = await self._run(root, "cat-file", "-s", oid, options=filters)
            if int(size) > MAX_TEXT_BYTES:
                raise FileBrowseError("preview_too_large", "HEAD 文本超过 1 MiB，只显示变化类型。")
            _, text = await self._run(root, "cat-file", "blob", oid, options=filters)
        except GitError as error:
            missing = (
                "could not get object info" in error.detail
                or f"could not fetch {oid} from promisor remote" in error.detail
                or f"{oid}: bad file" in error.detail
            )
            if error.outcome != "failed" or not missing:
                raise
            raise FileBrowseError("missing_object", "HEAD 对象不可读取，只显示变化类型。") from error
        return preview_text(text.encode("utf-8", "surrogateescape"))


def git_failure(error: GitError) -> dict:
    if is_not_repository(error):
        return {"git": "not_repository"}
    return unavailable("Git 状态读取超时，请重试。" if "timed out" in error.detail else "Git 暂不可用，请检查 Git 安装、仓库权限和状态后重试。")
