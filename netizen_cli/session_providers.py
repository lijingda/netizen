"""The exact session naming capabilities consumed by management."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class ManualTitleWriter(Protocol):
    """Validate materialization and lifecycle at the exact Binding's native write boundary."""

    async def rename_exact(self, binding_id: str, name: str) -> str: ...


@dataclass(frozen=True, slots=True)
class _ExactTitleWriter:
    write: Callable[[str, str], Awaitable[str]]

    async def rename_exact(self, binding_id: str, name: str) -> str:
        return await self.write(binding_id, name)


@runtime_checkable
class _CandidateSource(Protocol):
    async def generate_name_exact(self, binding_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class SessionNames:
    writer: ManualTitleWriter | None = None
    generate: Callable[[str], Awaitable[str]] | None = None

    @classmethod
    def from_runtime(cls, runtime: object) -> "SessionNames":
        # Keep the native methods responsible for their exact identity and locking.
        writer = None
        if isinstance(runtime, ManualTitleWriter):
            native_writer = runtime
            writer = _ExactTitleWriter(
                lambda binding_id, name: native_writer.rename_exact(binding_id, name)
            )
        if isinstance(runtime, _CandidateSource):
            return cls(
                writer, lambda binding_id: runtime.generate_name_exact(binding_id)
            )
        return cls(writer)
