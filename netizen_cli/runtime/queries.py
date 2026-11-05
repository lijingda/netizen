"""Read-only native Thread, catalog, model and account queries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from openai_codex import (
    CodexError,
    InvalidRequestError,
)

from ..account_rate_limits import (
    AccountRateLimitsSnapshot,
    AccountRateLimitsUnavailable,
)
from ..model_settings import ModelCatalog, ModelCatalogError, TurnModelSettings
from ..thread_history import AppServerThreadHistory
from .contracts import NativeAnswerImage, NativeLatestAnswer
from .contracts import (
    NativeThread,
    RuntimeClosed,
    ThreadNotMaterialized,
    SteerRace,
    ThreadCatalogError,
    ThreadCatalogDeadlineExceeded,
    ThreadCatalogLimitExceeded,
    NativeThreadMetadata,
    NativeThreadCatalogState,
    NativeThreadCatalog,
    NativeThreadCatalogPage,
    ContextWindowUsage,
    ThreadLifecycleSnapshot,
)

from . import limits
from .native_values import (
    _enum_value,
    _native_cwd,
    _thread_status_type,
    _thread_updated_at,
    _thread_created_at,
    _final_agent_response,
)
from .forks import ForkRuntime


class NativeQueryRuntime(ForkRuntime):
    """Read-only native Thread, catalog, model and account queries."""

    async def account_rate_limits(self) -> AccountRateLimitsSnapshot:
        """Read shared native account limits without opening a Thread."""

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能读取 Codex 账号额度。")
        if self._account_rate_limits is None:
            raise AccountRateLimitsUnavailable("当前实例的 Codex 账号额度查询不可用。")
        return await self._account_rate_limits.read()

    async def model_catalog(self) -> ModelCatalog:
        """Read the live native catalog; Netizen never caches model options."""

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能读取 Codex 模型目录。")
        response = await self._codex.models()
        return ModelCatalog.from_response(response)

    async def native_thread_model(self, binding_id: str) -> str:
        """Read the exact source's effective native model without loading it."""

        binding = self._bindings.get(binding_id)
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能读取来源模型。")
        if binding.native_thread_id is None:
            raise ThreadNotMaterialized("来源会话尚未生成 Codex Thread，无法继承模型。")
        response = await self._thread_handle(binding.native_thread_id).read()
        native = getattr(response, "thread", None)
        current = self._bindings.get(binding_id)
        if (
            getattr(native, "id", None) != binding.native_thread_id
            or current.native_thread_id != binding.native_thread_id
        ):
            raise SteerRace("读取来源模型期间会话身份已变化，本次未创建。")
        model = getattr(native, "model", None)
        if not isinstance(model, str) or not model:
            raise ModelCatalogError("来源原生会话未返回模型，无法确认继承配置。")
        return model

    async def resolve_binding_cwd(
        self, binding_id: str, *, default_cwd: Path,
    ) -> Path:
        """Resolve lazy intent or exact native cwd without loading the Thread.

        A missing directory is still a valid native cwd fact. Do not replace it
        with the Project directory, inspect history, or resume as a read side effect.
        """
        binding = self._bindings.get(binding_id)
        if binding.native_thread_id is None:
            return Path(binding.working_cwd) if binding.working_cwd is not None else default_cwd
        return await self.native_thread_cwd(binding_id)

    async def native_thread_cwd(self, binding_id: str) -> Path:
        binding = self._bindings.get(binding_id)
        if binding.native_thread_id is None:
            raise ThreadNotMaterialized("会话尚未生成 Codex Thread，无法读取原生工作目录。")
        response = await self._thread_handle(binding.native_thread_id).read(
            include_turns=False,
        )
        current = self._bindings.get(binding_id)
        if current.native_thread_id != binding.native_thread_id:
            raise SteerRace("读取原生工作目录期间会话身份已变化。")
        cwd = _native_cwd(getattr(response, "thread", None), binding.native_thread_id)
        self._bindings.sync_native_workspace(
            binding_id, expected_native_thread_id=binding.native_thread_id,
            working_cwd=str(cwd),
        )
        return cwd

    def _adopt_native_cwd(
        self, *, binding_id: str, thread_id: str, native_cwd: Path,
    ) -> Path:
        self._bindings.sync_native_workspace(
            binding_id, expected_native_thread_id=thread_id,
            working_cwd=str(native_cwd),
        )
        return native_cwd

    async def _read_submission_cwd(
        self, binding_id: str, thread: NativeThread,
    ) -> Path:
        try:
            async with asyncio.timeout(limits.PREPARED_CWD_READ_TIMEOUT_SECONDS):
                response = await thread.read(include_turns=False)
            cwd = _native_cwd(getattr(response, "thread", None), thread.id)
        except Exception as error:
            raise SteerRace(
                "无法确认恢复后的原生工作目录，本条消息未执行；请稍后重新发送。"
            ) from error
        return self._adopt_native_cwd(
            binding_id=binding_id, thread_id=thread.id, native_cwd=cwd,
        )

    async def worktree_usage(self, binding_id: str) -> dict[str, str | None]:
        """Read deletion evidence after waiting for exact in-flight admission.

        The caller reserves the resource's delete intent before this read. Its
        gate prevents new users after this lock is released; no Git operation
        runs under a Binding lock. Unknown native state never proves idleness.
        """
        async with self._lock(binding_id):
            binding = self._bindings.get(binding_id)
            local_busy = (
                binding_id in self._active or binding_id in self._goals
                or binding_id in self._compacting or binding_id in self._lifecycles
                or self.project_has_fork_creation(binding.project_alias)
            )
            if binding.native_thread_id is None:
                return {"cwd": binding.working_cwd, "status": "active" if local_busy else "idle"}
            cwd = None
            try:
                response = await self._thread_handle(binding.native_thread_id).read(include_turns=False)
                native = getattr(response, "thread", None)
                cwd = str(_native_cwd(native, binding.native_thread_id))
                status = _thread_status_type(native)
                if local_busy or status == "active":
                    return {"cwd": cwd, "status": "active"}
                if status not in {"idle", "notLoaded"} or self._background_terminal_inspector is None:
                    return {"cwd": cwd, "status": "unknown"}
                if await self._background_terminal_inspector.has_running(binding.native_thread_id):
                    return {"cwd": cwd, "status": "active"}
                return {"cwd": cwd, "status": "idle"}
            except Exception:
                return {"cwd": cwd, "status": "active" if local_busy else "unknown"}

    async def latest_thread_answer(self, thread_id: str) -> NativeLatestAnswer:
        """Read only the newest complete Turn, including archived history.

        No resume, subscription, backward answer search or partial-item tail.
        Legacy phase compatibility follows the existing terminal projection;
        running Turn messages are not presented as a final answer.
        """
        try:
            page = await AppServerThreadHistory(self._codex).list_turns(
                thread_id, limit=1, items_view="full",
            )
        except InvalidRequestError as error:
            # Verified fixed-SDK result for a newly created, zero-input Thread.
            # Missing/deleted history and every other read error remain errors.
            if error.message != (
                f"thread {thread_id} is not materialized yet; "
                "thread/turns/list is unavailable before first user message"
            ):
                raise
            return NativeLatestAnswer(thread_id, None, None, None)
        if not page.data:
            return NativeLatestAnswer(thread_id, None, None, None)
        turn = page.data[0]
        status = _enum_value(turn.status)
        items = list(turn.items) if status in {"completed", "failed", "interrupted"} else []
        answer = _final_agent_response(items)
        images = []
        if status == "completed" and answer:
            for item in items:
                root = getattr(item, "root", item)
                if (getattr(root, "type", None) != "imageGeneration"
                        or _enum_value(getattr(root, "status", None)) != "completed"):
                    continue
                path = getattr(root, "saved_path", None)
                path = getattr(path, "root", path)
                data = getattr(root, "result", None)
                if isinstance(path, str) and isinstance(data, str):
                    images.append(NativeAnswerImage(path, data))
        return NativeLatestAnswer(
            thread_id, turn.id, status, answer,
            getattr(turn, "started_at", None), getattr(turn, "completed_at", None),
            tuple(images),
        )

    async def thread_summary(
        self, thread_id: str,
    ) -> NativeThreadMetadata:
        """Read one exact summary without resuming a Thread or requesting history.

        The public response does not establish archive membership. Callers must
        not infer Active or Missing from a successful read or a read exception.
        Bound the caller's wait without cancelling this SDK worker: cancellation
        of its async wrapper does not stop the underlying synchronous request.
        """
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("native Thread ID must be a non-empty string")
        response = await self._thread_handle(thread_id).read(include_turns=False)
        thread = getattr(response, "thread", None)
        name = getattr(thread, "name", None)
        preview = getattr(thread, "preview", None)
        if (
            getattr(thread, "id", None) != thread_id
            or (name is not None and not isinstance(name, str))
            or not isinstance(preview, str)
        ):
            raise ThreadCatalogError("thread/read returned an invalid Thread summary")
        return NativeThreadMetadata(
            thread_id, name, preview, updated_at=_thread_updated_at(thread),
            created_at=_thread_created_at(thread),
        )

    async def thread_metadata(
        self,
        thread_ids: tuple[str, ...],
        *,
        archived: bool = False,
        deadline: float | None = None,
        max_pages: int | None = None,
        max_items: int | None = None,
        use_state_db_only: bool | None = None,
    ) -> dict[str, NativeThreadMetadata]:
        """Read native titles without loading, resuming, or mutating Threads."""

        remaining = set(thread_ids)
        if any(
            not isinstance(thread_id, str) or not thread_id
            for thread_id in remaining
        ):
            raise ValueError("native Thread IDs must be non-empty strings")
        if not remaining:
            return {}
        if max_pages is not None and max_pages < 1:
            raise ValueError("native metadata page limit must be positive")
        if max_items is not None and max_items < 1:
            raise ValueError("native metadata item limit must be positive")
        if use_state_db_only is not None and not isinstance(
            use_state_db_only,
            bool,
        ):
            raise ValueError("use_state_db_only must be boolean or None")
        if deadline is not None and deadline <= asyncio.get_running_loop().time():
            raise ThreadCatalogDeadlineExceeded(
                "native Thread metadata deadline elapsed"
            )

        found: dict[str, NativeThreadMetadata] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_count = 0
        item_count = 0
        try:
            async with asyncio.timeout_at(deadline):
                while remaining:
                    if max_pages is not None and page_count >= max_pages:
                        raise ThreadCatalogLimitExceeded(
                            "native Thread metadata exceeded the page limit"
                        )
                    list_kwargs: dict[str, object] = {
                        "archived": archived,
                        "cursor": cursor,
                        "limit": limits.THREAD_LIST_PAGE_LIMIT,
                        # Bindings remain valid across native model-provider changes.
                        # Use the SDK's explicit all-provider form so older Threads
                        # stay visible in both active and archived catalogs.
                        "model_providers": [],
                    }
                    if use_state_db_only is not None:
                        list_kwargs["use_state_db_only"] = use_state_db_only
                    response = await self._codex.thread_list(**list_kwargs)
                    page_count += 1
                    data = getattr(response, "data", None)
                    if not isinstance(data, list):
                        raise RuntimeError("thread_list returned an invalid data page")

                    for thread in data:
                        if max_items is not None and item_count >= max_items:
                            raise ThreadCatalogLimitExceeded(
                                "native Thread metadata exceeded the item limit"
                            )
                        item_count += 1
                        thread_id = getattr(thread, "id", None)
                        if thread_id not in remaining:
                            continue
                        name = getattr(thread, "name", None)
                        preview = getattr(thread, "preview", None)
                        if name is not None and not isinstance(name, str):
                            raise RuntimeError(
                                "thread_list returned an invalid Thread name"
                            )
                        if not isinstance(preview, str):
                            raise RuntimeError(
                                "thread_list returned an invalid Thread preview"
                            )
                        found[thread_id] = NativeThreadMetadata(
                            thread_id=thread_id,
                            name=name,
                            preview=preview,
                            updated_at=_thread_updated_at(thread),
                            created_at=_thread_created_at(thread),
                        )
                        remaining.remove(thread_id)

                    if not remaining:
                        break
                    next_cursor = getattr(response, "next_cursor", None)
                    if next_cursor is None:
                        break
                    if (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or next_cursor in seen_cursors
                    ):
                        raise RuntimeError(
                            "thread_list returned an invalid pagination cursor"
                        )
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
        except TimeoutError as error:
            raise ThreadCatalogDeadlineExceeded(
                "native Thread metadata deadline elapsed"
            ) from error

        return found

    async def thread_catalog_page(
        self,
        *,
        archived: bool,
        cursor: str | None = None,
        limit: int = 20,
        sort_key: Literal["created_at", "updated_at"] = "updated_at",
        sort_direction: Literal["asc", "desc"] = "desc",
        deadline: float | None = None,
        search_term: str | None = None,
    ) -> NativeThreadCatalogPage:
        """Read one native database page without expanding the whole catalog."""

        if not isinstance(archived, bool):
            raise ValueError("archived must be boolean")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("native catalog cursor must be a non-empty string")
        if type(limit) is not int or not 1 <= limit <= limits.THREAD_LIST_PAGE_LIMIT:
            raise ValueError("native catalog page size must be between 1 and 100")
        if sort_key not in ("created_at", "updated_at"):
            raise ValueError("sort_key must be created_at or updated_at")
        if sort_direction not in ("asc", "desc"):
            raise ValueError("sort_direction must be asc or desc")
        if search_term is not None and (not isinstance(search_term, str) or not search_term.strip()):
            raise ValueError("search_term must be a non-empty string")
        if deadline is not None and deadline <= asyncio.get_running_loop().time():
            raise ThreadCatalogDeadlineExceeded("native Thread catalog deadline elapsed")
        try:
            async with asyncio.timeout_at(deadline):
                response = await self._codex.thread_list(
                    archived=archived,
                    cursor=cursor,
                    limit=limit,
                    model_providers=[],
                    use_state_db_only=True,
                    sort_key=sort_key,
                    sort_direction=sort_direction,
                    **({"search_term": search_term} if search_term is not None else {}),
                )
        except TimeoutError as error:
            raise ThreadCatalogDeadlineExceeded(
                "native Thread catalog deadline elapsed"
            ) from error
        except (CodexError, OSError) as error:
            # SDK protocol failures and closed/broken transport are failures of
            # this read-only directory query, not unknown operation outcomes.
            raise ThreadCatalogError("native Thread catalog read failed") from error

        data = getattr(response, "data", None)
        if not isinstance(data, list) or len(data) > limit:
            raise ThreadCatalogError("thread_list returned an invalid data page")
        seen_ids: set[str] = set()
        threads: list[NativeThreadMetadata] = []
        for thread in data:
            thread_id = getattr(thread, "id", None)
            name = getattr(thread, "name", None)
            preview = getattr(thread, "preview", None)
            if not isinstance(thread_id, str) or not thread_id:
                raise ThreadCatalogError("thread_list returned an invalid Thread ID")
            if thread_id in seen_ids:
                raise ThreadCatalogError("thread_list repeated a native Thread ID")
            if name is not None and not isinstance(name, str):
                raise ThreadCatalogError("thread_list returned an invalid Thread name")
            if not isinstance(preview, str):
                raise ThreadCatalogError("thread_list returned an invalid Thread preview")
            seen_ids.add(thread_id)
            threads.append(NativeThreadMetadata(
                thread_id=thread_id,
                name=name,
                preview=preview,
                updated_at=_thread_updated_at(thread),
                created_at=_thread_created_at(thread),
            ))
        next_cursor = getattr(response, "next_cursor", None)
        if next_cursor is not None and (
            not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor
        ):
            raise ThreadCatalogError("thread_list returned an invalid pagination cursor")
        return NativeThreadCatalogPage(tuple(threads), next_cursor)

    async def thread_catalog(
        self,
        *,
        archived: bool,
        deadline: float,
        max_pages: int = limits.THREAD_CATALOG_MAX_PAGES,
        max_items: int = limits.THREAD_CATALOG_MAX_ITEMS,
        use_state_db_only: bool | None = None,
        sort_key: Literal["created_at", "updated_at"] | None = None,
        sort_direction: Literal["asc", "desc"] | None = None,
    ) -> NativeThreadCatalog:
        """Read one complete bounded native catalog for a request-scoped view."""

        if not isinstance(archived, bool):
            raise ValueError("archived must be boolean")
        if max_pages < 1 or max_items < 1:
            raise ValueError("native catalog limits must be positive")
        if use_state_db_only is not None and not isinstance(use_state_db_only, bool):
            raise ValueError("use_state_db_only must be boolean or None")
        if sort_key is not None and sort_key not in ("created_at", "updated_at"):
            raise ValueError("sort_key must be created_at, updated_at, or None")
        if sort_direction is not None and sort_direction not in ("asc", "desc"):
            raise ValueError("sort_direction must be asc, desc, or None")
        loop = asyncio.get_running_loop()
        if deadline <= loop.time():
            raise ThreadCatalogDeadlineExceeded("native Thread catalog deadline elapsed")

        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        threads: list[NativeThreadMetadata] = []
        page_count = 0
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    if page_count >= max_pages:
                        raise ThreadCatalogLimitExceeded(
                            "native Thread catalog exceeded the page limit"
                        )
                    list_kwargs: dict[str, object] = {
                        "archived": archived,
                        "cursor": cursor,
                        "limit": limits.THREAD_LIST_PAGE_LIMIT,
                        "model_providers": [],
                    }
                    if use_state_db_only is not None:
                        list_kwargs["use_state_db_only"] = use_state_db_only
                    if sort_key is not None:
                        list_kwargs["sort_key"] = sort_key
                    if sort_direction is not None:
                        list_kwargs["sort_direction"] = sort_direction
                    response = await self._codex.thread_list(**list_kwargs)
                    page_count += 1
                    data = getattr(response, "data", None)
                    if not isinstance(data, list):
                        raise ThreadCatalogError(
                            "thread_list returned an invalid data page"
                        )
                    for thread in data:
                        thread_id = getattr(thread, "id", None)
                        name = getattr(thread, "name", None)
                        preview = getattr(thread, "preview", None)
                        if not isinstance(thread_id, str) or not thread_id:
                            raise ThreadCatalogError(
                                "thread_list returned an invalid Thread ID"
                            )
                        if thread_id in seen_ids:
                            raise ThreadCatalogError(
                                "thread_list repeated a native Thread ID"
                            )
                        if name is not None and not isinstance(name, str):
                            raise ThreadCatalogError(
                                "thread_list returned an invalid Thread name"
                            )
                        if not isinstance(preview, str):
                            raise ThreadCatalogError(
                                "thread_list returned an invalid Thread preview"
                            )
                        if len(threads) >= max_items:
                            raise ThreadCatalogLimitExceeded(
                                "native Thread catalog exceeded the item limit"
                            )
                        seen_ids.add(thread_id)
                        threads.append(
                            NativeThreadMetadata(
                                thread_id=thread_id,
                                name=name,
                                preview=preview,
                                updated_at=_thread_updated_at(thread),
                                created_at=_thread_created_at(thread),
                            )
                        )

                    next_cursor = getattr(response, "next_cursor", None)
                    if next_cursor is None:
                        break
                    if (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or next_cursor in seen_cursors
                    ):
                        raise ThreadCatalogError(
                            "thread_list returned an invalid pagination cursor"
                        )
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
        except TimeoutError as error:
            raise ThreadCatalogDeadlineExceeded(
                "native Thread catalog deadline elapsed"
            ) from error
        return NativeThreadCatalog(archived=archived, threads=tuple(threads))

    async def thread_catalog_state(
        self,
        thread_id: str,
    ) -> NativeThreadCatalogState:
        """Classify one exact native ID without treating absence as active."""

        active = await self.thread_metadata((thread_id,), archived=False)
        archived = await self.thread_metadata((thread_id,), archived=True)
        if thread_id in active and thread_id in archived:
            raise ThreadCatalogError(
                "native Thread appeared in both active and archived catalogs"
            )
        if thread_id in active:
            return NativeThreadCatalogState.ACTIVE
        if thread_id in archived:
            return NativeThreadCatalogState.ARCHIVED
        return NativeThreadCatalogState.MISSING

    def context_window_usage(
        self,
        binding_id: str,
    ) -> ContextWindowUsage | None:
        """Return the latest public usage update observed by this process."""

        snapshot = self._context_window_usage.get(binding_id)
        if snapshot is None:
            return None
        binding = self._bindings.get(binding_id)
        if binding.native_thread_id != snapshot.thread_id:
            return None
        return snapshot.usage

    def lifecycle_state(
        self,
        binding_id: str,
    ) -> ThreadLifecycleSnapshot | None:
        active = self._lifecycles.get(binding_id)
        if active is None:
            return None
        return ThreadLifecycleSnapshot(
            binding_id=active.binding_id,
            thread_id=active.thread_id,
            state=active.state,
        )

    async def thread_is_archived(self, thread_id: str) -> bool:
        return thread_id in await self.thread_metadata(
            (thread_id,),
            archived=True,
        )

    async def resolve_model_settings(
        self,
        *,
        model_id: str,
        effort_id: str,
        service_tier_id: str,
    ) -> TurnModelSettings:
        catalog = await self.model_catalog()
        return catalog.resolve(
            model_id=model_id,
            effort_id=effort_id,
            service_tier_id=service_tier_id,
        )
