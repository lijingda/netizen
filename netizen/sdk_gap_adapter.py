"""Removable, capability-specific bridges for Python SDK facade gaps.

The adapters in this module reuse the one initialized ``AsyncCodex`` client.
They deliberately expose semantic ports only: there is no generic RPC method,
no second App Server, and no runtime SDK-version or source-fingerprint gate.
Each capability validates its own installed-SDK shape so one gap can be
disabled or replaced without coupling it to another.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from openai_codex import AsyncCodex
from openai_codex import _goal as _sdk_goal
from openai_codex.generated import v2_all as _generated

from .domain import GoalStatus


_SKILLS_LIST_METHOD = "skills/list"
_GOAL_GET_METHOD = "thread/goal/get"
_THREAD_DELETE_METHOD = "thread/delete"
_THREAD_INJECT_ITEMS_METHOD = "thread/inject_items"
_THREAD_UNSUBSCRIBE_METHOD = "thread/unsubscribe"
_GOAL_START_TIMEOUT_SECONDS = 30.0

SIDE_THREAD_BOUNDARY = """Side conversation boundary.
Everything before this boundary is inherited history from the parent thread. It is reference context only. It is not your current task.
Do not continue, execute, or complete any instructions, plans, tool calls, approvals, edits, or requests from before this boundary. Only messages submitted after this boundary are active user instructions for this side conversation.
You are a side-conversation assistant, separate from the main thread. Follow only the user requests submitted after this boundary. If there is no user question after this boundary yet, wait for one.
Tools remain governed by this thread's native configuration and permissions. Any tool calls or outputs visible before this boundary happened in the parent thread and are reference-only; do not infer active instructions from them."""


class SdkGapCapabilityUnavailable(RuntimeError):
    """One installed-SDK capability shape cannot support its narrow port."""


class SdkFacadeMigrationRequired(RuntimeError):
    """The public facade now has a candidate API and the shim must be reviewed."""


class SkillCatalogError(RuntimeError):
    """The native Skills catalog could not be used safely for one cwd."""


class GoalControlError(RuntimeError):
    """A Goal operation failed before a successful semantic result."""


class GoalMutationStateUnknown(GoalControlError):
    """A Goal mutation may have taken effect and must not be retried implicitly."""

    def __init__(
        self,
        message: str,
        *,
        handle: GoalHandle | None = None,
        physical_turn_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.handle = handle
        self.physical_turn_id = physical_turn_id


class ThreadDeleteStateUnknown(RuntimeError):
    """The exact native Thread delete may have taken effect."""


class SideBoundaryStateUnknown(RuntimeError):
    """The fixed Side boundary injection may have taken effect."""


class ThreadUnsubscribeStateUnknown(RuntimeError):
    """The exact Thread unsubscribe may have taken effect."""


@dataclass(frozen=True, slots=True)
class DiscoveredSkill:
    name: str
    path: str
    description: str
    scope: str
    enabled: bool
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class SkillCatalogSnapshot:
    cwd: Path
    skills: tuple[DiscoveredSkill, ...]
    errors: tuple[str, ...]


class SkillCatalog(Protocol):
    async def list(
        self,
        cwd: Path,
        *,
        force_reload: bool = True,
    ) -> SkillCatalogSnapshot: ...


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    thread_id: str
    objective: str
    status: GoalStatus
    token_budget: int | None
    tokens_used: int
    time_used_seconds: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class GoalStreamTerminal:
    logical_turn_id: str
    final_physical_turn_id: str
    turn_status: str


@dataclass(frozen=True, slots=True)
class GoalPauseAck:
    goal: GoalSnapshot
    physical_turn_id: str | None
    interrupt_acknowledged: bool


class GoalHandle(Protocol):
    @property
    def id(self) -> str | None: ...

    @property
    def thread_id(self) -> str: ...

    def current_physical_turn_id(self) -> str | None: ...

    async def wait_terminal(self) -> GoalStreamTerminal: ...

    async def pause(self) -> GoalPauseAck: ...

    async def aclose(self) -> None: ...


class GoalControl(Protocol):
    async def get(self, thread_id: str) -> GoalSnapshot | None: ...

    async def start(self, thread_id: str, objective: str) -> GoalHandle: ...

    async def resume(self, thread_id: str) -> GoalHandle: ...

    async def clear(self, thread_id: str) -> bool: ...


class ThreadDeleteControl(Protocol):
    async def delete(self, thread_id: str) -> None: ...


class ThreadUnsubscribeStatus(str, Enum):
    NOT_LOADED = "notLoaded"
    NOT_SUBSCRIBED = "notSubscribed"
    UNSUBSCRIBED = "unsubscribed"


class ThreadSubscriptionControl(Protocol):
    async def unsubscribe(self, thread_id: str) -> ThreadUnsubscribeStatus: ...


class SideBoundaryControl(Protocol):
    async def inject_boundary(self, thread_id: str) -> None: ...


class AppServerSkillCatalog:
    """Fixed ``skills/list`` bridge for the installed SDK's generated schema."""

    __slots__ = ("_client", "_response_model")

    def __init__(self, codex: AsyncCodex) -> None:
        client = _initialized_client(codex, capability="skills")
        response_model = _generated_type(
            "SkillsListResponse",
            capability="skills",
        )
        params_model = _generated_type("SkillsListParams", capability="skills")
        _require_model_fields(
            params_model,
            capability="skills",
            aliases={"cwds": "cwds", "force_reload": "forceReload"},
        )
        _require_model_fields(
            response_model,
            capability="skills",
            aliases={"data": "data"},
        )
        self._client = client
        self._response_model = response_model

    async def list(
        self,
        cwd: Path,
        *,
        force_reload: bool = True,
    ) -> SkillCatalogSnapshot:
        canonical = cwd.resolve()
        response = await self._client.request(
            _SKILLS_LIST_METHOD,
            {
                "cwds": [str(canonical)],
                "forceReload": force_reload,
            },
            response_model=self._response_model,
        )
        entries = tuple(getattr(response, "data", ()))
        matching = tuple(
            entry
            for entry in entries
            if Path(getattr(entry, "cwd", "")).resolve() == canonical
        )
        if len(entries) != 1 or len(matching) != 1:
            raise SkillCatalogError(
                "Codex Skills 目录没有返回唯一的当前 Project 条目。"
            )
        entry = matching[0]
        errors = tuple(
            f"{getattr(item, 'path', '')}: {getattr(item, 'message', '')}".strip(
                ": "
            )
            for item in getattr(entry, "errors", ())
        )
        skills: list[DiscoveredSkill] = []
        for item in getattr(entry, "skills", ()):
            path_value = getattr(getattr(item, "path", None), "root", None)
            if not isinstance(path_value, str) or not Path(path_value).is_absolute():
                raise SkillCatalogError("Codex 返回了非绝对路径的 Skill，已拒绝使用。")
            scope_value = getattr(getattr(item, "scope", None), "value", None)
            if not isinstance(scope_value, str):
                raise SkillCatalogError("Codex 返回了未知的 Skill scope。")
            interface = getattr(item, "interface", None)
            display_name = getattr(interface, "display_name", None)
            skills.append(
                DiscoveredSkill(
                    name=_trimmed_string(getattr(item, "name", None), "Skill name"),
                    path=path_value,
                    description=_trimmed_string(
                        getattr(item, "description", None),
                        "Skill description",
                    ),
                    scope=scope_value,
                    enabled=getattr(item, "enabled", None) is True,
                    display_name=(
                        display_name
                        if isinstance(display_name, str) and display_name.strip()
                        else None
                    ),
                )
            )
        return SkillCatalogSnapshot(canonical, tuple(skills), errors)


class AppServerThreadDeleteControl:
    """Narrow fixed-method bridge for the missing public delete facade.

    ADR 0037 permits this capability-specific adapter in production.  The
    facade sentinel still forces an explicit migration as soon as the public
    SDK exposes native Thread delete.
    """

    __slots__ = ("_client", "_response_model")

    def __init__(self, codex: AsyncCodex) -> None:
        client = _initialized_client(codex, capability="thread-delete")
        params_model = _generated_type(
            "ThreadDeleteParams",
            capability="thread-delete",
        )
        response_model = _generated_type(
            "ThreadDeleteResponse",
            capability="thread-delete",
        )
        _require_model_fields(
            params_model,
            capability="thread-delete",
            aliases={"thread_id": "threadId"},
        )
        _require_model_fields(
            response_model,
            capability="thread-delete",
            aliases={},
        )
        self._client = client
        self._response_model = response_model

    async def delete(self, thread_id: str) -> None:
        _validate_thread_id(thread_id)
        try:
            await self._client.request(
                _THREAD_DELETE_METHOD,
                {"threadId": thread_id},
                response_model=self._response_model,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise ThreadDeleteStateUnknown(
                "Codex Thread 删除结果未确认；不能自动重试。"
            ) from error


class AppServerSideBoundaryControl:
    """Fixed bridge for the App Server method that installs a Side boundary."""

    __slots__ = ("_client", "_inject_response_model")

    def __init__(self, codex: AsyncCodex) -> None:
        client = _initialized_client(codex, capability="side-boundary")
        inject_params_model = _generated_type(
            "ThreadInjectItemsParams",
            capability="side-boundary",
        )
        inject_response_model = _generated_type(
            "ThreadInjectItemsResponse",
            capability="side-boundary",
        )
        _require_model_fields(
            inject_params_model,
            capability="side-boundary",
            aliases={"items": "items", "thread_id": "threadId"},
        )
        _require_model_fields(
            inject_response_model,
            capability="side-boundary",
            aliases={},
        )
        self._client = client
        self._inject_response_model = inject_response_model

    async def inject_boundary(self, thread_id: str) -> None:
        _validate_thread_id(thread_id)
        try:
            await self._client.request(
                _THREAD_INJECT_ITEMS_METHOD,
                {
                    "threadId": thread_id,
                    "items": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": SIDE_THREAD_BOUNDARY,
                                }
                            ],
                        }
                    ],
                },
                response_model=self._inject_response_model,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise SideBoundaryStateUnknown(
                "Side 边界注入结果未确认；不能自动重试。"
            ) from error


class AppServerThreadSubscriptionControl:
    """Fixed bridge for removing this connection's exact Thread subscription."""

    __slots__ = ("_client", "_unsubscribe_response_model")

    def __init__(self, codex: AsyncCodex) -> None:
        client = _initialized_client(codex, capability="thread-subscription")
        unsubscribe_params_model = _generated_type(
            "ThreadUnsubscribeParams",
            capability="thread-subscription",
        )
        unsubscribe_response_model = _generated_type(
            "ThreadUnsubscribeResponse",
            capability="thread-subscription",
        )
        unsubscribe_status_model = _generated_type(
            "ThreadUnsubscribeStatus",
            capability="thread-subscription",
        )
        _require_model_fields(
            unsubscribe_params_model,
            capability="thread-subscription",
            aliases={"thread_id": "threadId"},
        )
        _require_model_fields(
            unsubscribe_response_model,
            capability="thread-subscription",
            aliases={"status": "status"},
        )
        if not issubclass(unsubscribe_status_model, Enum):
            raise SdkGapCapabilityUnavailable(
                "thread-subscription unsubscribe status enum shape changed"
            )
        try:
            status_values = {member.value for member in unsubscribe_status_model}
        except (AttributeError, TypeError) as error:
            raise SdkGapCapabilityUnavailable(
                "thread-subscription unsubscribe status enum shape changed"
            ) from error
        if status_values != {status.value for status in ThreadUnsubscribeStatus}:
            raise SdkGapCapabilityUnavailable(
                "thread-subscription unsubscribe status enum shape changed"
            )
        self._client = client
        self._unsubscribe_response_model = unsubscribe_response_model

    async def unsubscribe(self, thread_id: str) -> ThreadUnsubscribeStatus:
        _validate_thread_id(thread_id)
        try:
            response = await self._client.request(
                _THREAD_UNSUBSCRIBE_METHOD,
                {"threadId": thread_id},
                response_model=self._unsubscribe_response_model,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise ThreadUnsubscribeStateUnknown(
                "Thread 取消订阅结果未确认；不能自动重试。"
            ) from error
        raw_status = getattr(getattr(response, "status", None), "value", None)
        try:
            return ThreadUnsubscribeStatus(raw_status)
        except (TypeError, ValueError) as error:
            raise ThreadUnsubscribeStateUnknown(
                "Thread 取消订阅响应形状无法确认。"
            ) from error


class AppServerGoalControl:
    """Fixed Goal bridge that delegates continuation routing to the SDK."""

    __slots__ = (
        "_client",
        "_get_response_model",
        "_status_model",
        "_stream_type",
    )

    def __init__(self, codex: AsyncCodex) -> None:
        client = _initialized_client(codex, capability="goal")
        for method_name in (
            "request",
            "start_goal_operation",
            "register_goal_operation",
            "unregister_goal_operation",
            "next_goal_notification",
            "cancel_goal_operation",
            "thread_goal_set",
            "thread_goal_clear",
            "pause_goal",
            "turn_interrupt",
        ):
            if not callable(getattr(client, method_name, None)):
                raise SdkGapCapabilityUnavailable(
                    f"goal SDK shape missing callable {method_name}"
                )
        get_response_model = _generated_type(
            "ThreadGoalGetResponse",
            capability="goal",
        )
        get_params_model = _generated_type(
            "ThreadGoalGetParams",
            capability="goal",
        )
        status_model = _generated_type("ThreadGoalStatus", capability="goal")
        _require_model_fields(
            get_params_model,
            capability="goal",
            aliases={"thread_id": "threadId"},
        )
        _require_model_fields(
            get_response_model,
            capability="goal",
            aliases={"goal": "goal"},
        )
        if {member.value for member in status_model} != {
            status.value for status in GoalStatus
        }:
            raise SdkGapCapabilityUnavailable("goal status enum shape changed")
        stream_type = getattr(_sdk_goal, "_AsyncGoalNotificationStream", None)
        state_type = getattr(_sdk_goal, "_GoalOperationState", None)
        if not isinstance(stream_type, type) or not isinstance(state_type, type):
            raise SdkGapCapabilityUnavailable("goal stream ownership shape changed")
        required_state_parameters = {
            "thread_id",
            "logical_turn_id",
            "current_turn_id",
            "completed_turn",
        }
        if not required_state_parameters.issubset(
            inspect.signature(state_type).parameters
        ):
            raise SdkGapCapabilityUnavailable("goal state data shape changed")
        expected_stream_parameters = {
            "state",
            "next_notification",
            "unregister",
            "cancel_goal",
            "_pending",
            "_closed",
        }
        if set(inspect.signature(stream_type).parameters) != expected_stream_parameters:
            raise SdkGapCapabilityUnavailable("goal stream constructor shape changed")
        for state_method in ("current_turn", "wait_for_start"):
            if not callable(getattr(state_type, state_method, None)):
                raise SdkGapCapabilityUnavailable(
                    f"goal state shape missing {state_method}"
                )
        self._client = client
        self._get_response_model = get_response_model
        self._status_model = status_model
        self._stream_type = stream_type

    async def get(self, thread_id: str) -> GoalSnapshot | None:
        _validate_thread_id(thread_id)
        response = await self._client.request(
            _GOAL_GET_METHOD,
            {"threadId": thread_id},
            response_model=self._get_response_model,
        )
        goal = getattr(response, "goal", None)
        if goal is None:
            return None
        snapshot = _goal_snapshot(goal)
        if snapshot.thread_id != thread_id:
            raise GoalControlError(
                "Codex Goal 响应与请求的原生 Thread 不一致。"
            )
        return snapshot

    async def start(self, thread_id: str, objective: str) -> GoalHandle:
        _validate_thread_id(thread_id)
        objective = _validate_objective(objective)
        try:
            state, logical_turn_id = await self._client.start_goal_operation(
                thread_id,
                objective,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise GoalMutationStateUnknown(
                "Codex Goal 启动结果未确认；不能自动重试。"
            ) from error
        return self._handle(state, logical_turn_id)

    async def resume(self, thread_id: str) -> GoalHandle:
        _validate_thread_id(thread_id)
        try:
            state = self._client.register_goal_operation(thread_id)
        except Exception as error:
            raise GoalControlError("无法为既有 Goal 注册通知路由。") from error
        handle = self._handle(state, None)
        mutation_attempted = False
        try:
            mutation_attempted = True
            response = await self._client.thread_goal_set(
                thread_id,
                status=self._status_model.active,
            )
            snapshot = _goal_snapshot(getattr(response, "goal", None))
            if snapshot.thread_id != thread_id or snapshot.status is not GoalStatus.ACTIVE:
                raise RuntimeError("goal resume acknowledgement did not match request")
            logical_turn_id = await asyncio.to_thread(
                state.wait_for_start,
                _GOAL_START_TIMEOUT_SECONDS,
            )
            if not isinstance(logical_turn_id, str) or not logical_turn_id:
                raise TimeoutError("timed out waiting for resumed goal turn")
            handle._bind_logical_turn(logical_turn_id)
            return handle
        except asyncio.CancelledError as error:
            if mutation_attempted:
                # Cancellation must continue to cancel the caller, but the
                # route was registered before the mutation and is now owned by
                # Runtime until transport teardown.  Attach the opaque handle
                # so Runtime can retain that ownership without converting
                # cancellation into an ordinary exception.
                error.goal_handle = handle
                raise
            await handle.aclose()
            raise
        except Exception as error:
            if mutation_attempted:
                raise GoalMutationStateUnknown(
                    "Codex Goal 恢复结果未确认；不能自动重试。",
                    handle=handle,
                ) from error
            await handle.aclose()
            raise GoalControlError("Codex Goal 恢复前置检查失败。") from error

    async def clear(self, thread_id: str) -> bool:
        _validate_thread_id(thread_id)
        try:
            response = await self._client.thread_goal_clear(thread_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise GoalMutationStateUnknown(
                "Codex Goal 清除结果未确认；不能自动重试。"
            ) from error
        cleared = getattr(response, "cleared", None)
        if not isinstance(cleared, bool):
            raise GoalMutationStateUnknown("Codex Goal 清除响应形状无法确认。")
        return cleared

    def _handle(
        self,
        state: Any,
        logical_turn_id: str | None,
    ) -> _AppServerGoalHandle:
        stream = self._stream_type(
            state=state,
            next_notification=lambda: self._client.next_goal_notification(state),
            unregister=lambda: self._client.unregister_goal_operation(state),
            cancel_goal=lambda: self._client.cancel_goal_operation(state),
        )
        return _AppServerGoalHandle(
            client=self._client,
            status_model=self._status_model,
            state=state,
            stream=stream,
            logical_turn_id=logical_turn_id,
        )


class _AppServerGoalHandle:
    __slots__ = (
        "_client",
        "_status_model",
        "_state",
        "_stream",
        "_logical_turn_id",
    )

    def __init__(
        self,
        *,
        client: Any,
        status_model: Any,
        state: Any,
        stream: AsyncIterator[Any],
        logical_turn_id: str | None,
    ) -> None:
        self._client = client
        self._status_model = status_model
        self._state = state
        self._stream = stream
        self._logical_turn_id = logical_turn_id

    @property
    def id(self) -> str | None:
        return self._logical_turn_id

    @property
    def thread_id(self) -> str:
        return self._state.thread_id

    def _bind_logical_turn(self, logical_turn_id: str) -> None:
        if self._logical_turn_id is not None:
            raise RuntimeError("Goal handle is already bound")
        self._logical_turn_id = logical_turn_id

    def current_physical_turn_id(self) -> str | None:
        value = self._state.current_turn()
        return value if isinstance(value, str) and value else None

    async def wait_terminal(self) -> GoalStreamTerminal:
        logical_turn_id = self._logical_turn_id
        if logical_turn_id is None:
            raise GoalControlError("Goal logical Turn 尚未建立。")
        async for _notification in self._stream:
            pass
        final_turn = getattr(self._state, "completed_turn", None)
        physical_turn_id = getattr(final_turn, "id", None)
        status = getattr(getattr(final_turn, "status", None), "value", None)
        if not isinstance(physical_turn_id, str) or not isinstance(status, str):
            raise GoalControlError("Goal 通知流结束但缺少最终物理 Turn。")
        return GoalStreamTerminal(logical_turn_id, physical_turn_id, status)

    async def pause(self) -> GoalPauseAck:
        try:
            response = await self._client.pause_goal(self.thread_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise GoalMutationStateUnknown(
                "Codex Goal 暂停结果未确认；不能自动重试。",
                handle=self,
            ) from error
        snapshot = _goal_snapshot(getattr(response, "goal", None))
        if snapshot.thread_id != self.thread_id or snapshot.status is not GoalStatus.PAUSED:
            raise GoalMutationStateUnknown(
                "Codex Goal 暂停响应无法确认。",
                handle=self,
            )
        physical_turn_id = self.current_physical_turn_id()
        if physical_turn_id is None:
            return GoalPauseAck(snapshot, None, False)
        try:
            await self._client.turn_interrupt(self.thread_id, physical_turn_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise GoalMutationStateUnknown(
                "Goal 已暂停，但当前物理 Turn 的中断结果未确认。",
                handle=self,
                physical_turn_id=physical_turn_id,
            ) from error
        return GoalPauseAck(snapshot, physical_turn_id, True)

    async def aclose(self) -> None:
        await self._stream.aclose()


def facade_migration_requirements() -> tuple[str, ...]:
    """Return public facade candidates that require deleting a gap shim."""

    from openai_codex import AsyncThread

    requirements: list[str] = []
    candidates = {
        "skills": (
            (AsyncCodex, "skills"),
            (AsyncCodex, "skills_list"),
        ),
        "goal": (
            (AsyncCodex, "goal_get"),
            (AsyncThread, "goal_get"),
            (AsyncThread, "goal_set"),
            (AsyncThread, "goal_clear"),
            (AsyncThread, "goal_start"),
        ),
        "apps": (
            (AsyncCodex, "apps"),
            (AsyncCodex, "apps_list"),
        ),
        "thread-delete": (
            (AsyncCodex, "thread_delete"),
            (AsyncThread, "delete"),
        ),
        "side-boundary": (
            (AsyncCodex, "thread_inject_items"),
            (AsyncThread, "inject_items"),
        ),
        "thread-subscription": (
            (AsyncCodex, "thread_unsubscribe"),
            (AsyncThread, "unsubscribe"),
        ),
    }
    for capability, entries in candidates.items():
        names = tuple(f"{owner.__name__}.{name}" for owner, name in entries if hasattr(owner, name))
        if names:
            requirements.append(f"migration-required:{capability}:{','.join(names)}")
    return tuple(requirements)


def require_no_facade_migration() -> None:
    requirements = facade_migration_requirements()
    if requirements:
        raise SdkFacadeMigrationRequired("; ".join(requirements))


def _initialized_client(codex: AsyncCodex, *, capability: str) -> Any:
    if getattr(codex, "_initialized", False) is not True:
        raise SdkGapCapabilityUnavailable(
            f"{capability} requires an initialized AsyncCodex"
        )
    client = getattr(codex, "_client", None)
    if client is None or not callable(getattr(client, "request", None)):
        raise SdkGapCapabilityUnavailable(
            f"{capability} SDK ownership edge changed"
        )
    return client


def _generated_type(name: str, *, capability: str) -> type[Any]:
    value = getattr(_generated, name, None)
    if not isinstance(value, type):
        raise SdkGapCapabilityUnavailable(
            f"{capability} generated model missing {name}"
        )
    return value


def _require_model_fields(
    model: type[Any],
    *,
    capability: str,
    aliases: dict[str, str],
) -> None:
    fields = getattr(model, "model_fields", None)
    if not isinstance(fields, dict) or set(fields) != set(aliases):
        raise SdkGapCapabilityUnavailable(
            f"{capability} generated model fields changed for {model.__name__}"
        )
    for name, expected_alias in aliases.items():
        field = fields[name]
        actual_alias = getattr(field, "alias", None) or name
        if actual_alias != expected_alias:
            raise SdkGapCapabilityUnavailable(
                f"{capability} generated alias changed for {model.__name__}.{name}"
            )


def _goal_snapshot(goal: Any) -> GoalSnapshot:
    if goal is None:
        raise GoalControlError("Codex Goal 响应缺少 goal。")
    status_value = getattr(getattr(goal, "status", None), "value", None)
    try:
        status = GoalStatus(status_value)
    except (TypeError, ValueError) as error:
        raise GoalControlError("Codex Goal 返回了未知状态。") from error
    return GoalSnapshot(
        thread_id=_trimmed_string(getattr(goal, "thread_id", None), "Goal thread_id"),
        objective=_trimmed_string(getattr(goal, "objective", None), "Goal objective"),
        status=status,
        token_budget=_optional_nonnegative_int(
            getattr(goal, "token_budget", None),
            "Goal token_budget",
        ),
        tokens_used=_nonnegative_int(getattr(goal, "tokens_used", None), "Goal tokens_used"),
        time_used_seconds=_nonnegative_int(
            getattr(goal, "time_used_seconds", None),
            "Goal time_used_seconds",
        ),
        created_at=_nonnegative_int(getattr(goal, "created_at", None), "Goal created_at"),
        updated_at=_nonnegative_int(getattr(goal, "updated_at", None), "Goal updated_at"),
    )


def _validate_thread_id(thread_id: str) -> None:
    _trimmed_string(thread_id, "native Thread ID")


def _validate_objective(objective: str) -> str:
    value = _trimmed_string(objective, "Goal objective")
    if len(value) > 4_000:
        raise ValueError("Goal objective 不能超过 4000 个字符。")
    return value


def _trimmed_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be a non-empty trimmed string")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: Any, label: str) -> int | None:
    return None if value is None else _nonnegative_int(value, label)
