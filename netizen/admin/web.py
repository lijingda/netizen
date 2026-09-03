"""Authenticated in-process Admin Web application and lifecycle runner."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import importlib.resources
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .auth import (
    ActionCsrfRejected,
    AdmissionClosed,
    AdminAuth,
    AdminAuthError,
    AuthCapacityExceeded,
    ConsumedActionGrant,
    ExpectedValue,
    LoginRejected,
    MalformedActionGrant,
    SessionRejected,
    StaleActionGrant,
)
from .transport import AdminHttpTransport, Request, Response
from ..bindings import (
    AmbiguousBinding,
    BindingCursor,
    BindingNotFound,
    BindingQuery,
    BindingQueryBusy,
    BindingQueryClosed,
    BindingQueryTimeout,
    BindingSettingsRevisionConflict,
    BindingTurnSettings,
    ProjectConflict as StoredProjectConflict,
    ScopeNotFound,
    SideTopicCursor,
    SideTopicNotFound,
    SideTopicQuery,
    SideTopicState,
    ThreadBinding,
)
from ..management.blocking_io import (
    BlockingIODrainTimeout,
    BlockingIOExecutorClosed,
    BlockingIOExecutorSaturated,
    BlockingIOResultUnknown,
    BlockingIOShutdownTimeout,
)
from ..codex_runtime import (
    NativeThreadCatalogState,
    ReleaseDisposition,
    RuntimeClosed,
    SideCloseFailed,
    SideSessionConflict,
    SideSessionNotFound,
    ThreadArchived,
    ThreadBackgroundTerminalsActive,
    ThreadCatalogError,
    ThreadCatalogDeadlineExceeded,
    ThreadCompacting,
    ThreadDeleteUnavailable,
    ThreadGoalActive,
    ThreadLifecycleError,
    ThreadLifecycleStateUnknown,
    ThreadNotArchived,
    ThreadNotMaterialized,
    ThreadReleaseError,
    ThreadReleaseStateUnknown,
    ThreadRunningConfiguration,
    StopDisposition,
)
from ..domain import MentionContextMode, ScopeKind
from ..management import (
    ActivePointerChanged,
    BindingStatusProjection,
    BindingScopeMismatch,
    ChatLabel,
    CurrentSideTarget,
    ExactBindingTarget,
    InstanceManagementService,
    NativeCatalogInconsistent,
    NativeThreadMissing,
    RuntimePrecondition,
    RuntimeStateChanged,
    SessionInventoryItem,
    SessionInventoryState,
    SessionQuery,
    SideIdentityMismatch,
)
from ..projects import (
    ProjectAlreadyExists,
    ProjectDisabled,
    ProjectError,
    StaleProject,
    UnknownProject,
)


logger = logging.getLogger(__name__)


_SESSION_COOKIE = "netizen_admin_session"
_PREAUTH_COOKIE = "netizen_admin_preauth"
_JSON_TYPE = "application/json"
_FORM_TYPE = "application/x-www-form-urlencoded"
_QUERY_DEADLINE_SECONDS = 5.0
_SESSION_QUERY_DEADLINE_SECONDS = 10.0
_MUTATION_DEADLINE_SECONDS = 15.0
_MAX_QUERY_FIELDS = 24
_MAX_JSON_FIELDS = 20
_MAX_TEXT_BYTES = 4_096
_SESSION_PAGE_SIZES = frozenset((10, 20, 50, 100))
_RUNTIME_SNAPSHOT_BATCH_SIZE = 50
_ISO_INSTANT_PATTERN = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)

_SECURITY_HEADERS = (
    (b"Cache-Control", b"no-store"),
    (
        b"Content-Security-Policy",
        b"default-src 'self'; script-src 'self'; style-src 'self'; "
        b"object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        b"form-action 'self'",
    ),
    (b"X-Content-Type-Options", b"nosniff"),
    # Basic HTML form submissions serialize Origin as "null" under
    # no-referrer.  same-origin keeps cross-origin referrers suppressed while
    # preserving the exact Origin required by the login CSRF boundary.
    (b"Referrer-Policy", b"same-origin"),
    (b"X-Frame-Options", b"DENY"),
)


class AdminWebError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class AdminMutationDrainTimeout(TimeoutError):
    pass


@dataclass(frozen=True, slots=True)
class AdminActionTarget:
    resource: str
    target_id: str
    scope_key: str | None = None


@dataclass(frozen=True, slots=True)
class AdminActionPreconditions:
    active_binding_id: ExpectedValue[str]
    project_revision: ExpectedValue[int]
    settings_revision: ExpectedValue[int]
    native_thread_id: ExpectedValue[str]
    activity_revision: ExpectedValue[int]
    physical_turn_id: ExpectedValue[str]
    side_app_id: ExpectedValue[str]
    side_chat_id: ExpectedValue[str]
    side_topic_id: ExpectedValue[str]
    side_root_message_id: ExpectedValue[str]


@dataclass(frozen=True, slots=True)
class _RequestContext:
    request: Request
    method: str
    path: str
    query: Mapping[str, list[str]]
    authority: str
    source_ip: str
    session_token: str | None
    session_log_handle: str | None


T = TypeVar("T")


class AdminWebApplication:
    """HTTP application with no direct access to CodexRuntime or SQLite."""

    def __init__(
        self,
        *,
        auth: AdminAuth,
        management: InstanceManagementService,
    ) -> None:
        self._auth = auth
        self._management = management
        self._ready = False
        self._authorities: frozenset[str] = frozenset()
        self._origins: frozenset[str] = frozenset()
        self._mutation_tasks: set[asyncio.Task[object]] = set()
        self._assets = _load_assets()

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def mutation_task_count(self) -> int:
        return len(self._mutation_tasks)

    def configure_authorities(self, authorities: Sequence[str]) -> None:
        if self._ready:
            raise RuntimeError("cannot change Admin authorities after admission")
        normalized = frozenset(authority.lower() for authority in authorities)
        if not normalized or any(not authority for authority in normalized):
            raise ValueError("Admin authority allowlist must not be empty")
        self._authorities = normalized
        self._origins = frozenset(f"http://{authority}" for authority in normalized)

    def open_admission(self) -> None:
        if not self._authorities:
            raise RuntimeError("Admin authorities are not configured")
        if not self._auth.admission_open:
            raise RuntimeError("Admin authentication admission is closed")
        self._ready = True

    def close_admission(self) -> None:
        self._ready = False

    def close_auth(self) -> None:
        self._ready = False
        self._auth.close()

    async def drain(self, deadline: float) -> None:
        while self._mutation_tasks:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AdminMutationDrainTimeout(
                    "Admin mutations did not drain before shutdown deadline"
                )
            done, _pending = await asyncio.wait(
                tuple(self._mutation_tasks),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise AdminMutationDrainTimeout(
                    "Admin mutations did not drain before shutdown deadline"
                )

    async def handle(self, request: Request) -> Response:
        try:
            context = self._context(request)
            response = await self._dispatch(context)
        except AdminWebError as error:
            response = _json_error(
                request.request_id,
                error.status,
                error.code,
                error.message,
            )
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            mapped = _map_error(error)
            if mapped is None:
                # Exception text can originate in filesystem/native adapters
                # and may contain cwd, title, preview, or request-derived data.
                # Keep the server-side signal useful without serializing it.
                logger.error(
                    "admin request failed request_id=%s error_type=%s",
                    request.request_id,
                    type(error).__name__,
                )
                mapped = AdminWebError(500, "internal_error", "服务内部错误。")
            response = _json_error(
                request.request_id,
                mapped.status,
                mapped.code,
                mapped.message,
            )
        return _secure(response)

    def _context(self, request: Request) -> _RequestContext:
        authority = _single_header(request, b"host").decode("ascii").lower()
        if authority not in self._authorities:
            raise AdminWebError(400, "invalid_host", "请求 Host 不受信任。")
        try:
            target = request.target.decode("ascii")
        except UnicodeDecodeError:
            raise AdminWebError(400, "invalid_target", "请求 URL 无效。") from None
        split = urlsplit(target)
        if not split.path.startswith("/") or split.scheme or split.netloc or split.fragment:
            raise AdminWebError(400, "invalid_target", "请求 URL 无效。")
        try:
            query = parse_qs(
                split.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=_MAX_QUERY_FIELDS,
            )
        except ValueError:
            raise AdminWebError(400, "invalid_query", "查询参数无效。") from None
        cookies = _cookies(request)
        peer = request.peer
        if not isinstance(peer, tuple) or not peer or not isinstance(peer[0], str):
            raise AdminWebError(400, "invalid_source", "请求来源无效。")
        return _RequestContext(
            request=request,
            method=request.method.decode("ascii"),
            path=split.path,
            query=query,
            authority=authority,
            source_ip=peer[0],
            session_token=cookies.get(_SESSION_COOKIE),
            session_log_handle=None,
        )

    async def _dispatch(self, context: _RequestContext) -> Response:
        if context.path == "/health/ready" and context.method == "GET":
            return Response(204 if self._ready else 503)
        if not self._ready:
            raise AdminWebError(503, "not_ready", "管理服务尚未就绪。")
        if context.path == "/static/admin.css" and context.method == "GET":
            # The login page reuses the same inert stylesheet.  Keep HTML,
            # JavaScript, API routes, and all instance state authenticated.
            return Response(
                200,
                ((b"Content-Type", b"text/css; charset=utf-8"),),
                self._assets["admin.css"],
            )
        if context.path == "/login" and context.method == "GET":
            return self._login_page(context)
        if context.path == "/login" and context.method == "POST":
            self._require_origin(context)
            return self._login(context)

        if context.path == "/" and context.method == "GET":
            # The browser entry point redirects anonymous visitors to the
            # login page.  Every other route (API, static assets other than
            # the login stylesheet, and unknown paths) still fails closed
            # with a JSON 401 so the anonymous content surface stays minimal.
            session = self._authenticate_or_none(context)
            if session is None:
                return Response(303, ((b"Location", b"/login"),))
        else:
            session = self._authenticate(context)
        context = _RequestContext(
            request=context.request,
            method=context.method,
            path=context.path,
            query=context.query,
            authority=context.authority,
            source_ip=context.source_ip,
            session_token=context.session_token,
            session_log_handle=session.log_handle,
        )
        if context.method == "POST":
            self._require_origin(context)
        return await self._dispatch_authenticated(context)

    def _authenticate(self, context: _RequestContext):
        try:
            return self._auth.authenticate(context.session_token)
        except (SessionRejected, AdmissionClosed):
            raise AdminWebError(401, "not_authenticated", "请先登录。") from None

    def _authenticate_or_none(self, context: _RequestContext):
        try:
            return self._auth.authenticate(context.session_token)
        except (SessionRejected, AdmissionClosed):
            return None

    def _require_origin(self, context: _RequestContext) -> None:
        try:
            origin = _single_header(context.request, b"origin").decode("ascii")
        except UnicodeDecodeError:
            raise AdminWebError(403, "invalid_origin", "请求 Origin 不受信任。") from None
        if origin not in self._origins:
            raise AdminWebError(403, "invalid_origin", "请求 Origin 不受信任。")

    def _login_page(self, context: _RequestContext) -> Response:
        try:
            challenge = self._auth.issue_preauth(context.source_ip)
        except LoginRejected:
            raise AdminWebError(401, "login_rejected", "登录暂不可用，请稍后重试。") from None
        page = _login_html(challenge.form_nonce)
        return Response(
            200,
            headers=(
                (b"Content-Type", b"text/html; charset=utf-8"),
                (b"Set-Cookie", _cookie(_PREAUTH_COOKIE, challenge.cookie_token)),
            ),
            body=page,
        )

    def _login(self, context: _RequestContext) -> Response:
        form = _parse_form(context.request)
        cookies = _cookies(context.request)
        try:
            issued = self._auth.login(
                source_ip=context.source_ip,
                cookie_token=cookies.get(_PREAUTH_COOKIE),
                form_nonce=_one(form, "nonce"),
                credential=_one(form, "credential"),
            )
        except LoginRejected:
            raise AdminWebError(401, "login_rejected", "登录凭据无效。") from None
        return Response(
            303,
            headers=(
                (b"Location", b"/"),
                (b"Set-Cookie", _cookie(_SESSION_COOKIE, issued.token)),
                (b"Set-Cookie", _expired_cookie(_PREAUTH_COOKIE)),
            ),
        )

    async def _dispatch_authenticated(self, context: _RequestContext) -> Response:
        route = (context.method, context.path)
        if route == ("GET", "/"):
            return Response(
                200,
                ((b"Content-Type", b"text/html; charset=utf-8"),),
                self._assets["index.html"],
            )
        if route == ("GET", "/static/admin.js"):
            return Response(
                200,
                ((b"Content-Type", b"text/javascript; charset=utf-8"),),
                self._assets["admin.js"],
            )
        if route == ("POST", "/logout"):
            self._auth.logout(context.session_token)
            return Response(
                204,
                ((b"Set-Cookie", _expired_cookie(_SESSION_COOKIE)),),
            )
        if route == ("GET", "/api/v1/projects"):
            return await self._projects(context)
        if route == ("GET", "/api/v1/sessions"):
            return await self._sessions(context)
        if route == ("GET", "/api/v1/runtime-snapshots"):
            return await self._runtime_snapshots(context)
        if route == ("GET", "/api/v1/side-topics"):
            return await self._side_topics(context)

        mutations: dict[str, Callable[[_RequestContext], Awaitable[Response]]] = {
            "/api/v1/projects/register": self._project_register,
            "/api/v1/projects/create-directory": self._project_create_directory,
            "/api/v1/projects/set-enabled": self._project_set_enabled,
            "/api/v1/sessions/create-lazy": self._session_create_lazy,
            "/api/v1/sessions/activate": self._session_activate,
            "/api/v1/sessions/configure": self._session_configure,
            "/api/v1/sessions/rename": self._session_rename,
            "/api/v1/sessions/archive": self._session_archive,
            "/api/v1/sessions/unarchive": self._session_unarchive,
            "/api/v1/sessions/delete-lazy": self._session_delete_lazy,
            "/api/v1/sessions/delete-materialized": (
                self._session_delete_materialized
            ),
            "/api/v1/sessions/stop": self._session_stop,
            "/api/v1/sessions/release": self._session_release,
            "/api/v1/side-topics/close": self._side_close,
        }
        handler = mutations.get(context.path) if context.method == "POST" else None
        if handler is not None:
            return await handler(context)
        raise AdminWebError(404, "not_found", "管理接口不存在。")

    async def _projects(self, context: _RequestContext) -> Response:
        allowed = {"cursor", "pageSize"}
        _require_query_keys(context.query, allowed)
        page_size = _page_size(context.query)
        fingerprint = _fingerprint("projects", {"pageSize": page_size})
        cursor = _decode_project_cursor(_optional_one(context.query, "cursor"), fingerprint)
        deadline = asyncio.get_running_loop().time() + _QUERY_DEADLINE_SECONDS
        page = await self._management.query_projects(
            cursor=cursor,
            limit=page_size,
            deadline=deadline,
        )
        registry_target = AdminActionTarget("project-registry", "new")
        empty = _empty_preconditions()
        actions = {
            "register": self._grant(
                context,
                "projects.register",
                registry_target,
                empty,
            ),
            "createDirectory": self._grant(
                context,
                "projects.create-directory",
                registry_target,
                empty,
            ),
        }
        items = []
        for item in page.items:
            project = item.aggregate.project
            project_target = AdminActionTarget("project", project.alias)
            preconditions = _empty_preconditions(
                project_revision=ExpectedValue.expect(project.revision)
            )
            project_actions: dict[str, object] = {}
            if project.alias != "none" or not project.enabled:
                project_actions["setEnabled"] = self._grant(
                    context,
                    "projects.set-enabled",
                    project_target,
                    preconditions,
                )
            items.append(
                {
                    "alias": project.alias,
                    "cwd": project.cwd,
                    "enabled": project.enabled,
                    "revision": project.revision,
                    "createdAt": project.created_at,
                    "updatedAt": project.updated_at,
                    "bindingCount": item.aggregate.binding_count,
                    "lazyBindingCount": item.aggregate.lazy_binding_count,
                    "materializedBindingCount": item.aggregate.materialized_binding_count,
                    "archivedBindingCount": item.archived_binding_count,
                    "lastActivatedAt": item.aggregate.last_activated_at,
                    "actions": project_actions,
                }
            )
        return _json_response(
            200,
            {
                "requestId": context.request.request_id,
                "items": items,
                "nextCursor": _encode_project_cursor(page.next_cursor, fingerprint),
                "actions": actions,
            },
        )

    async def _sessions(self, context: _RequestContext) -> Response:
        allowed = {
            "cursor",
            "pageSize",
            "project",
            "scopeKind",
            "chatId",
            "topicId",
            "identity",
            "inventoryState",
            "current",
            "createdFrom",
            "createdBefore",
        }
        _require_query_keys(context.query, allowed)
        page_size = _session_page_size(context.query)
        created_from, created_before = _created_range_query(context.query)
        local = BindingQuery(
            project_alias=_optional_text_query(context.query, "project"),
            scope_kind=_optional_scope_kind(context.query),
            chat_id=_optional_text_query(context.query, "chatId"),
            topic_id=_optional_text_query(context.query, "topicId"),
            identity=_optional_text_query(context.query, "identity"),
            current=_optional_bool_query(context.query, "current"),
            created_from=created_from,
            created_before=created_before,
        )
        inventory_state = _session_inventory_state(context.query)
        filter_values = {
            "pageSize": page_size,
            "local": _jsonable(local),
            "inventoryState": (
                inventory_state.value if inventory_state is not None else "all"
            ),
        }
        fingerprint = _fingerprint("sessions", filter_values)
        cursor = _decode_binding_cursor(_optional_one(context.query, "cursor"), fingerprint)
        deadline = (
            asyncio.get_running_loop().time() + _SESSION_QUERY_DEADLINE_SECONDS
        )
        page = await self._management.query_sessions(
            query=SessionQuery(
                local=local,
                inventory_state=inventory_state,
            ),
            cursor=cursor,
            limit=page_size,
            deadline=deadline,
        )
        binding_ids = tuple(item.record.binding.id for item in page.items)
        catalog_states = {
            item.record.binding.id: (
                item.native.state if item.native is not None else None
            )
            for item in page.items
        }
        snapshots_by_id = {}
        for batch in _batches(binding_ids, _RUNTIME_SNAPSHOT_BATCH_SIZE):
            snapshots = await self._management.binding_statuses_exact(
                binding_ids=batch,
                catalog_states={item: catalog_states[item] for item in batch},
                deadline=deadline,
            )
            snapshots_by_id.update(
                (snapshot.binding_id, snapshot) for snapshot in snapshots
            )
        items = [
            self._session_item(context, item, snapshots_by_id[item.record.binding.id])
            for item in page.items
        ]
        return _json_response(
            200,
            {
                "requestId": context.request.request_id,
                "items": items,
                "nextCursor": _encode_binding_cursor(page.next_cursor, fingerprint),
                "pageSize": page_size,
            },
        )

    def _session_item(
        self,
        context: _RequestContext,
        item: SessionInventoryItem,
        status: BindingStatusProjection,
    ) -> dict[str, object]:
        binding = item.record.binding
        scope = item.record.scope
        runtime = status.snapshot
        target = AdminActionTarget("binding", binding.id, binding.scope_key)
        active_expected = (
            ExpectedValue.expect_none()
            if scope.active_binding_id is None
            else ExpectedValue.expect(scope.active_binding_id)
        )
        native_expected = (
            ExpectedValue.expect_none()
            if binding.native_thread_id is None
            else ExpectedValue.expect(binding.native_thread_id)
        )
        physical_turn_id = runtime.turn.turn_id if runtime.turn is not None else None
        preconditions = _empty_preconditions(
            active_binding_id=active_expected,
            settings_revision=ExpectedValue.expect(binding.settings_revision),
            native_thread_id=native_expected,
            activity_revision=ExpectedValue.expect(runtime.activity_revision),
            physical_turn_id=(
                ExpectedValue.expect_none()
                if physical_turn_id is None
                else ExpectedValue.expect(physical_turn_id)
            ),
        )
        lifecycle_preconditions = _empty_preconditions()
        delete_preconditions = _empty_preconditions(
            native_thread_id=native_expected,
        )
        actions: dict[str, object] = {
            "configure": self._grant(
                context, "sessions.configure", target, preconditions
            )
        }
        native_state = item.native.state if item.native is not None else None
        if (
            binding.message_context_mode is MentionContextMode.CURRENT_ONLY
            and status.pointer_state != "current"
            and (
                binding.native_thread_id is None
                or native_state is NativeThreadCatalogState.ACTIVE
            )
        ):
            actions["activate"] = self._grant(
                context, "sessions.activate", target, preconditions
            )
        if binding.native_thread_id is None:
            actions["deleteLazy"] = self._grant(
                context, "sessions.delete-lazy", target, preconditions
            )
        elif native_state is NativeThreadCatalogState.ACTIVE:
            actions["rename"] = self._grant(
                context, "sessions.rename", target, preconditions
            )
            actions["archive"] = self._grant(
                context,
                "sessions.archive",
                target,
                lifecycle_preconditions,
            )
            if status.can_release:
                actions["release"] = self._grant(
                    context, "sessions.release", target, preconditions
                )
        elif native_state is NativeThreadCatalogState.ARCHIVED:
            actions["unarchive"] = self._grant(
                context, "sessions.unarchive", target, preconditions
            )
            if binding.message_context_mode is MentionContextMode.CURRENT_ONLY:
                actions["unarchiveCurrent"] = self._grant(
                    context, "sessions.unarchive-current", target, preconditions
                )
        if (
            self._management.native_delete_available
            and native_state
            in {
                NativeThreadCatalogState.ACTIVE,
                NativeThreadCatalogState.ARCHIVED,
            }
        ):
            actions["deleteMaterialized"] = self._grant(
                context,
                "sessions.delete-materialized",
                target,
                delete_preconditions,
            )
        if status.can_stop:
            actions["stop"] = self._grant(
                context, "sessions.stop", target, preconditions
            )
        actions["createLazy"] = self._grant(
            context,
            "sessions.create-lazy",
            AdminActionTarget("scope", binding.scope_key, binding.scope_key),
            preconditions,
        )
        metadata = item.native.metadata if item.native is not None else None
        return {
            "bindingId": binding.id,
            "shortId": binding.short_id,
            "scopeKey": binding.scope_key,
            "scopeKind": scope.kind.value,
            "appId": scope.app_id,
            "chatId": scope.chat_id,
            "chatLabel": item.chat.display_name,
            "chatLabelResolved": item.chat.resolved,
            "chatMode": item.chat.chat_mode,
            "chatType": item.chat.chat_type,
            "chatOpenUrl": _chat_open_url(item.chat),
            "topicId": scope.topic_id,
            "sessionType": (
                "topic" if scope.kind is ScopeKind.TOPIC else "message"
            ),
            "pointerState": status.pointer_state,
            "catalogState": (
                status.catalog_state.value
                if status.catalog_state is not None
                else "lazy"
            ),
            "projectAlias": binding.project_alias,
            "nativeThreadId": binding.native_thread_id,
            "nativeTitle": metadata.name if metadata is not None else None,
            "nativePreview": metadata.preview if metadata is not None else None,
            "creator": binding.creator_id,
            "createdAt": binding.created_at,
            "activatedAt": binding.activated_at,
            "settingsRevision": binding.settings_revision,
            "turnSettings": _settings_json(binding.turn_settings),
            "messageContextMode": binding.message_context_mode.value,
            "contextRevision": binding.context_revision,
            "runtime": _runtime_binding_json(status),
            "actions": actions,
        }

    async def _runtime_snapshots(self, context: _RequestContext) -> Response:
        _require_query_keys(
            context.query,
            {"bindingIds", "sideIds", "resolvePrimary"},
        )
        binding_ids = _id_query(context.query, "bindingIds")
        side_ids = _id_query(context.query, "sideIds")
        if len(binding_ids) + len(side_ids) > _RUNTIME_SNAPSHOT_BATCH_SIZE:
            raise ValueError("runtime snapshot request accepts at most 50 IDs")
        resolve_primary = (
            _optional_bool_query(context.query, "resolvePrimary") or False
        )
        if resolve_primary:
            bindings = await self._management.binding_statuses_exact(
                binding_ids=binding_ids,
                deadline=(
                    asyncio.get_running_loop().time() + _QUERY_DEADLINE_SECONDS
                ),
            )
            side_snapshots = self._management.runtime_snapshots(side_ids=side_ids)
        else:
            snapshots = self._management.runtime_snapshots(
                binding_ids=binding_ids,
                side_ids=side_ids,
            )
            bindings = snapshots.bindings
            side_snapshots = snapshots
        return _json_response(
            200,
            {
                "requestId": context.request.request_id,
                "bindings": [
                    _runtime_binding_json(snapshot) for snapshot in bindings
                ],
                "sides": [
                    _runtime_side_json(snapshot) for snapshot in side_snapshots.sides
                ],
                "missingSideIds": list(side_snapshots.missing_side_ids),
            },
        )

    async def _side_topics(self, context: _RequestContext) -> Response:
        allowed = {
            "cursor",
            "pageSize",
            "project",
            "parentBindingId",
            "appId",
            "chatId",
            "topicId",
            "rootMessageId",
            "state",
            "createdFrom",
            "createdBefore",
        }
        _require_query_keys(context.query, allowed)
        page_size = _page_size(context.query)
        state_value = _optional_text_query(context.query, "state")
        try:
            state = SideTopicState(state_value) if state_value is not None else None
        except ValueError:
            raise AdminWebError(400, "invalid_state", "Side 状态筛选无效。") from None
        created_from, created_before = _created_range_query(context.query)
        query = SideTopicQuery(
            project_alias=_optional_text_query(context.query, "project"),
            parent_binding_id=_optional_text_query(context.query, "parentBindingId"),
            app_id=_optional_text_query(context.query, "appId"),
            chat_id=_optional_text_query(context.query, "chatId"),
            topic_id=_optional_text_query(context.query, "topicId"),
            root_message_id=_optional_text_query(context.query, "rootMessageId"),
            state=state,
            created_from=created_from,
            created_before=created_before,
        )
        fingerprint = _fingerprint(
            "side-topics",
            {"pageSize": page_size, "query": _jsonable(query)},
        )
        cursor = _decode_side_cursor(_optional_one(context.query, "cursor"), fingerprint)
        page = await self._management.query_side_topics(
            query=query,
            cursor=cursor,
            limit=page_size,
            deadline=asyncio.get_running_loop().time() + _QUERY_DEADLINE_SECONDS,
        )
        items = []
        for item in page.items:
            side = item.record.side_topic
            actions: dict[str, object] = {}
            if side.state is SideTopicState.OPEN and item.runtime is not None:
                target = AdminActionTarget("side", side.id, None)
                preconditions = _empty_preconditions(
                    side_app_id=ExpectedValue.expect(side.app_id),
                    side_chat_id=ExpectedValue.expect(side.chat_id),
                    side_topic_id=(
                        ExpectedValue.expect_none()
                        if side.topic_id is None
                        else ExpectedValue.expect(side.topic_id)
                    ),
                    side_root_message_id=(
                        ExpectedValue.expect_none()
                        if side.root_message_id is None
                        else ExpectedValue.expect(side.root_message_id)
                    ),
                )
                actions["close"] = self._grant(
                    context,
                    "side-topics.close",
                    target,
                    preconditions,
                )
            items.append(
                {
                    "sideId": side.id,
                    "parentBindingId": side.parent_binding_id,
                    "projectAlias": item.record.project_alias,
                    "appId": side.app_id,
                    "chatId": side.chat_id,
                    "chatLabel": item.chat.display_name,
                    "chatLabelResolved": item.chat.resolved,
                    "chatMode": item.chat.chat_mode,
                    "chatType": item.chat.chat_type,
                    "topicId": side.topic_id,
                    "rootMessageId": side.root_message_id,
                    "sourceMessageId": side.source_message_id,
                    "state": side.state.value,
                    "createdAt": side.created_at,
                    "updatedAt": side.updated_at,
                    "runtime": (
                        _runtime_side_json(item.runtime)
                        if item.runtime is not None
                        else None
                    ),
                    "actions": actions,
                }
            )
        return _json_response(
            200,
            {
                "requestId": context.request.request_id,
                "items": items,
                "nextCursor": _encode_side_cursor(page.next_cursor, fingerprint),
            },
        )

    async def _project_register(self, context: _RequestContext) -> Response:
        payload, _grant = self._redeem(
            context,
            "projects.register",
            expected_resource="project-registry",
            allowed_extra={"alias", "path"},
        )
        alias = _required_text(payload, "alias", maximum=64)
        path = _required_text(payload, "path", maximum=4_096)
        project = await self._mutation(
            context,
            "projects.register",
            "project-registry",
            self._management.register_project(
                alias=alias,
                path=path,
                create_directory=False,
                deadline=asyncio.get_running_loop().time()
                + _MUTATION_DEADLINE_SECONDS,
            ),
        )
        return _json_response(200, _project_result(context, project))

    async def _project_create_directory(
        self,
        context: _RequestContext,
    ) -> Response:
        payload, _grant = self._redeem(
            context,
            "projects.create-directory",
            expected_resource="project-registry",
            allowed_extra={"alias", "path"},
        )
        alias = _required_text(payload, "alias", maximum=64)
        path = _optional_text_body(payload, "path", maximum=4_096)
        project = await self._mutation(
            context,
            "projects.create-directory",
            "project-registry",
            self._management.register_project(
                alias=alias,
                path=path,
                create_directory=True,
                deadline=asyncio.get_running_loop().time()
                + _MUTATION_DEADLINE_SECONDS,
            ),
        )
        return _json_response(200, _project_result(context, project))

    async def _project_set_enabled(self, context: _RequestContext) -> Response:
        payload, grant = self._redeem(
            context,
            "projects.set-enabled",
            expected_resource="project",
            allowed_extra={"enabled"},
        )
        enabled = _required_bool(payload, "enabled")
        preconditions = _grant_preconditions(grant)
        revision = _expected_value(preconditions.project_revision, "Project revision")
        project = await self._mutation(
            context,
            "projects.set-enabled",
            grant.target.target_id,
            self._management.set_project_enabled(
                alias=grant.target.target_id,
                enabled=enabled,
                expected_revision=revision,
            ),
        )
        return _json_response(200, _project_result(context, project))

    async def _session_create_lazy(self, context: _RequestContext) -> Response:
        payload, grant = self._redeem(
            context,
            "sessions.create-lazy",
            expected_resource="scope",
            allowed_extra={
                "projectAlias",
                "projectRevision",
                "activate",
                "turnSettings",
            },
        )
        project_alias = _required_text(payload, "projectAlias", maximum=64)
        project_revision = _required_int(payload, "projectRevision", minimum=1)
        activate = _required_bool(payload, "activate")
        settings = await self._validated_settings(payload.get("turnSettings"))
        preconditions = _grant_preconditions(grant)
        created = await self._mutation(
            context,
            "sessions.create-lazy",
            grant.target.target_id,
            self._management.create_exact_lazy_binding(
                scope_key=grant.target.target_id,
                project_alias=project_alias,
                expected_project_revision=project_revision,
                expected_active_binding_id=_expected_optional(
                    preconditions.active_binding_id,
                    "active Binding",
                ),
                activate=activate,
                turn_settings=settings,
                deadline=asyncio.get_running_loop().time()
                + _MUTATION_DEADLINE_SECONDS,
            ),
        )
        return _json_response(
            200,
            {
                "requestId": context.request.request_id,
                "bindingId": created.binding.id,
                "scopeKey": created.binding.scope_key,
                "projectAlias": created.project.alias,
                "current": created.binding.active,
            },
        )

    async def _session_activate(self, context: _RequestContext) -> Response:
        _payload, grant = self._redeem(
            context,
            "sessions.activate",
            expected_resource="binding",
        )
        binding = await self._mutation(
            context,
            "sessions.activate",
            grant.target.target_id,
            self._management.activate_exact_binding(
                target=_binding_target(grant)
            ),
        )
        return _json_response(200, _binding_result(context, binding))

    async def _session_configure(self, context: _RequestContext) -> Response:
        payload, grant = self._redeem(
            context,
            "sessions.configure",
            expected_resource="binding",
            allowed_extra={"turnSettings"},
        )
        settings = await self._validated_settings(payload.get("turnSettings"))
        preconditions = _grant_preconditions(grant)
        revision = _expected_value(
            preconditions.settings_revision,
            "Binding settings revision",
        )
        binding = await self._mutation(
            context,
            "sessions.configure",
            grant.target.target_id,
            self._management.configure_exact_binding(
                target=_binding_target(grant),
                expected_settings_revision=revision,
                settings=settings,
            ),
        )
        return _json_response(200, _binding_result(context, binding))

    async def _session_rename(self, context: _RequestContext) -> Response:
        payload, grant = self._redeem(
            context,
            "sessions.rename",
            expected_resource="binding",
            allowed_extra={"name"},
        )
        name = _required_text(payload, "name", maximum=120)
        renamed = await self._mutation(
            context,
            "sessions.rename",
            grant.target.target_id,
            self._management.rename_exact_binding(
                target=_binding_target(grant),
                name=name,
            ),
        )
        return _json_response(
            200,
            {
                "requestId": context.request.request_id,
                "bindingId": renamed.binding.id,
                "name": renamed.name,
            },
        )

    async def _session_archive(self, context: _RequestContext) -> Response:
        _payload, grant = self._redeem(
            context,
            "sessions.archive",
            expected_resource="binding",
        )
        binding = await self._mutation(
            context,
            "sessions.archive",
            grant.target.target_id,
            self._management.archive_exact_binding(
                target=_lifecycle_binding_target(grant),
            ),
        )
        return _json_response(200, _binding_result(context, binding))

    async def _session_unarchive(self, context: _RequestContext) -> Response:
        payload = _parse_json(context.request)
        target = _action_target(payload)
        action_kind = _required_text(payload, "actionKind", maximum=128)
        if action_kind not in {"sessions.unarchive", "sessions.unarchive-current"}:
            raise AdminWebError(400, "invalid_action", "恢复动作无效。")
        _require_body_keys(
            payload,
            _ACTION_KEYS | {"actionKind"},
        )
        grant = self._redeem_parsed(
            context,
            payload,
            action_kind,
            target,
            expected_resource="binding",
        )
        operation = (
            self._management.restore_exact_binding_as_current(
                target=_binding_target(grant)
            )
            if action_kind == "sessions.unarchive-current"
            else self._management.restore_exact_binding(target=_binding_target(grant))
        )
        binding = await self._mutation(
            context,
            action_kind,
            grant.target.target_id,
            operation,
        )
        return _json_response(200, _binding_result(context, binding))

    async def _session_delete_lazy(self, context: _RequestContext) -> Response:
        _payload, grant = self._redeem(
            context,
            "sessions.delete-lazy",
            expected_resource="binding",
        )
        binding = await self._mutation(
            context,
            "sessions.delete-lazy",
            grant.target.target_id,
            self._management.delete_exact_lazy_binding(
                target=_binding_target(grant)
            ),
        )
        return _json_response(200, _binding_result(context, binding))

    async def _session_delete_materialized(
        self,
        context: _RequestContext,
    ) -> Response:
        _payload, grant = self._redeem(
            context,
            "sessions.delete-materialized",
            expected_resource="binding",
        )
        target = _lifecycle_binding_target(grant)
        expected_native_thread_id = _expected_value(
            _grant_preconditions(grant).native_thread_id,
            "native Thread",
        )
        binding = await self._mutation(
            context,
            "sessions.delete-materialized",
            target.binding_id,
            self._management.delete_exact_binding(
                target=target,
                expected_native_thread_id=expected_native_thread_id,
            ),
        )
        return _json_response(200, _binding_result(context, binding))

    async def _session_stop(self, context: _RequestContext) -> Response:
        _payload, grant = self._redeem(
            context,
            "sessions.stop",
            expected_resource="binding",
        )
        stopped = await self._mutation(
            context,
            "sessions.stop",
            grant.target.target_id,
            self._management.stop_exact_binding(
                target=_binding_target(grant),
                runtime_precondition=_runtime_precondition(grant),
            ),
        )
        return _json_response(
            200,
            {
                "requestId": context.request.request_id,
                "bindingId": stopped.binding.id,
                "disposition": stopped.disposition.value,
                "message": _stop_disposition_message(stopped.disposition),
            },
        )

    async def _session_release(self, context: _RequestContext) -> Response:
        _payload, grant = self._redeem(
            context,
            "sessions.release",
            expected_resource="binding",
        )
        released = await self._mutation(
            context,
            "sessions.release",
            grant.target.target_id,
            self._management.release_exact_binding(
                target=_binding_target(grant),
                runtime_precondition=_runtime_precondition(grant),
            ),
        )
        return _json_response(
            200,
            {
                "requestId": context.request.request_id,
                "bindingId": released.binding.id,
                "disposition": released.disposition.value,
                "message": _release_disposition_message(released.disposition),
            },
        )

    async def _side_close(self, context: _RequestContext) -> Response:
        _payload, grant = self._redeem(
            context,
            "side-topics.close",
            expected_resource="side",
        )
        preconditions = _grant_preconditions(grant)
        closed = await self._mutation(
            context,
            "side-topics.close",
            grant.target.target_id,
            self._management.close_side(
                target=CurrentSideTarget(
                    side_id=grant.target.target_id,
                    app_id=_expected_value(preconditions.side_app_id, "Side app"),
                    chat_id=_expected_value(preconditions.side_chat_id, "Side chat"),
                    topic_id=_expected_optional(
                        preconditions.side_topic_id,
                        "Side topic",
                    ),
                    root_message_id=_expected_optional(
                        preconditions.side_root_message_id,
                        "Side root message",
                    ),
                )
            ),
        )
        return _json_response(
            200,
            {
                "requestId": context.request.request_id,
                "sideId": closed.record.id,
                "state": closed.record.state.value,
                "alreadyTerminal": closed.outcome is None,
                "missingRuntimeSession": closed.missing_runtime_session,
            },
        )

    async def _validated_settings(self, value: object) -> BindingTurnSettings | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise AdminWebError(400, "invalid_settings", "会话配置必须是对象或 null。")
        _require_body_keys(value, {"modelId", "effortId", "serviceTierId"})
        return await self._management.resolve_turn_settings(
            model_id=_required_text(value, "modelId", maximum=256),
            effort_id=_required_text(value, "effortId", maximum=128),
            service_tier_id=_required_text(value, "serviceTierId", maximum=128),
        )

    def _grant(
        self,
        context: _RequestContext,
        action_kind: str,
        target: AdminActionTarget,
        preconditions: AdminActionPreconditions,
    ) -> dict[str, object]:
        try:
            issued = self._auth.issue_action(
                context.session_token,
                action_kind=action_kind,
                target=target,
                preconditions=preconditions,
            )
        except AuthCapacityExceeded:
            raise AdminWebError(
                503,
                "action_capacity",
                "操作凭据容量已满，请稍后刷新页面。",
            ) from None
        return {
            "actionKind": action_kind,
            "csrfToken": issued.csrf_token,
            "actionToken": issued.action_token,
            "target": _action_target_json(target),
        }

    def _redeem(
        self,
        context: _RequestContext,
        action_kind: str,
        *,
        expected_resource: str,
        allowed_extra: set[str] | None = None,
    ):
        payload = _parse_json(context.request)
        _require_body_keys(payload, _ACTION_KEYS | (allowed_extra or set()))
        target = _action_target(payload)
        grant = self._redeem_parsed(
            context,
            payload,
            action_kind,
            target,
            expected_resource=expected_resource,
        )
        return payload, grant

    def _redeem_parsed(
        self,
        context: _RequestContext,
        payload: Mapping[str, object],
        action_kind: str,
        target: AdminActionTarget,
        *,
        expected_resource: str,
    ):
        if target.resource != expected_resource:
            raise AdminWebError(400, "invalid_target", "操作目标类型无效。")
        try:
            return self._auth.redeem_action(
                context.session_token,
                csrf_token=payload.get("csrfToken"),
                action_token=payload.get("actionToken"),
                action_kind=action_kind,
                target=target,
            )
        except ActionCsrfRejected:
            raise AdminWebError(403, "csrf_rejected", "操作校验失败。") from None
        except MalformedActionGrant:
            raise AdminWebError(400, "invalid_action", "操作凭据无效。") from None
        except (StaleActionGrant, ConsumedActionGrant):
            raise AdminWebError(
                409,
                "stale_or_consumed",
                "操作已过期或已执行，请刷新页面后重试。",
            ) from None
        except AdminAuthError:
            raise AdminWebError(401, "not_authenticated", "请重新登录。") from None

    async def _mutation(
        self,
        context: _RequestContext,
        action_kind: str,
        target_id: str,
        operation: Awaitable[T],
    ) -> T:
        async def execute() -> T:
            started = time.monotonic()
            terminal = "success"
            error_name: str | None = None
            try:
                return await operation
            except BaseException as error:
                terminal = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
                error_name = type(error).__name__
                raise
            finally:
                event = {
                    "event": "admin_mutation",
                    "requestId": context.request.request_id,
                    "session": context.session_log_handle,
                    "action": action_kind,
                    "targetId": target_id,
                    "terminal": terminal,
                    "elapsedMs": round((time.monotonic() - started) * 1000),
                    "errorType": error_name,
                }
                logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))

        task: asyncio.Task[T] = asyncio.create_task(
            execute(),
            name=f"netizen-admin:{action_kind}",
        )
        self._mutation_tasks.add(task)  # type: ignore[arg-type]
        task.add_done_callback(self._mutation_finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The exact operation remains strongly tracked and is drained by
            # the Admin runner; a disconnected HTTP response is not rollback.
            raise

    def _mutation_finished(self, task: asyncio.Task[object]) -> None:
        self._mutation_tasks.discard(task)
        if not task.cancelled():
            task.exception()


class AdminWebRunner:
    """Bind, admit, and drain the Admin application on one event loop."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        credential_path: Path,
        management: InstanceManagementService | None = None,
        auth: AdminAuth | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._auth = auth or AdminAuth(credential_path)
        self._application: AdminWebApplication | None = None
        self._authorities: tuple[str, ...] | None = None
        if management is not None:
            self.attach_management(management)
        self._transport = AdminHttpTransport(host, port, self._handle)

    @property
    def application(self) -> AdminWebApplication:
        application = self._application
        if application is None:
            raise RuntimeError("Admin management application is not attached")
        return application

    @property
    def transport(self) -> AdminHttpTransport:
        return self._transport

    @property
    def addresses(self):
        return self._transport.addresses

    async def bind(self) -> None:
        try:
            await self._transport.bind()
            self._authorities = await asyncio.to_thread(
                accepted_authorities,
                self._host,
                self._port,
                self._transport.addresses,
            )
            if self._application is not None:
                self._application.configure_authorities(self._authorities)
        except BaseException:
            try:
                await self._transport.close()
            finally:
                self._auth.close()
            raise

    def attach_management(self, management: InstanceManagementService) -> None:
        """Attach the sole application service after the closed listener binds."""

        if self._application is not None:
            raise RuntimeError("Admin management application is already attached")
        application = AdminWebApplication(auth=self._auth, management=management)
        if self._authorities is not None:
            application.configure_authorities(self._authorities)
        self._application = application

    async def _handle(self, request: Request) -> Response:
        application = self._application
        if application is None:
            return _secure(
                _json_error(
                    request.request_id,
                    503,
                    "not_ready",
                    "服务尚未就绪。",
                )
            )
        return await application.handle(request)

    def open_admission(self) -> None:
        self.application.open_admission()
        self._transport.open_admission()

    def close_admission(self) -> None:
        self._transport.close_admission()
        if self._application is not None:
            self._application.close_admission()

    async def close_listener(self) -> None:
        self.close_admission()
        await self._transport.close()

    async def drain(self, deadline: float) -> None:
        await self._transport.drain(deadline)
        if self._application is not None:
            await self._application.drain(deadline)

    def close_auth(self) -> None:
        if self._application is not None:
            self._application.close_auth()
        else:
            self._auth.close()


def accepted_authorities(
    bind_host: str,
    configured_port: int,
    addresses: Sequence[object],
) -> tuple[str, ...]:
    """Build the exact Host allowlist from local names and bound sockets."""

    ports: set[int] = set()
    hosts: set[str] = {"localhost", "127.0.0.1", "::1"}
    for address in addresses:
        if isinstance(address, tuple) and len(address) >= 2:
            if isinstance(address[0], str):
                hosts.add(address[0].split("%", 1)[0])
            if isinstance(address[1], int):
                ports.add(address[1])
    if not ports:
        ports.add(configured_port)
    if bind_host not in {"0.0.0.0", "::", ""}:
        hosts.add(bind_host.split("%", 1)[0])
    for name in (socket.gethostname(), socket.getfqdn()):
        if name:
            hosts.add(name.rstrip(".").lower())
    try:
        infos = socket.getaddrinfo(
            socket.gethostname(),
            None,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        infos = []
    for _family, _kind, _protocol, _canonical, sockaddr in infos:
        if isinstance(sockaddr, tuple) and sockaddr and isinstance(sockaddr[0], str):
            hosts.add(sockaddr[0].split("%", 1)[0])
    authorities = {
        _format_authority(host, port)
        for host in hosts
        for port in ports
        if host
    }
    return tuple(sorted(authorities))


def _format_authority(host: str, port: int) -> str:
    normalized = host.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        rendered = normalized
    else:
        rendered = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{rendered}:{port}"


def _load_assets() -> dict[str, bytes]:
    root = importlib.resources.files("netizen.admin").joinpath("static")
    assets: dict[str, bytes] = {}
    for name in ("index.html", "admin.css", "admin.js"):
        assets[name] = root.joinpath(name).read_bytes()
    return assets


def _login_html(nonce: str) -> bytes:
    escaped = html.escape(nonce, quote=True)
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Netizen Admin 登录</title>"
        "<link rel='stylesheet' href='/static/admin.css'></head>"
        "<body class='login-body'><main class='login-card'>"
        "<p class='eyebrow'>INSTANCE CONTROL PLANE</p>"
        "<h1>Netizen Admin</h1><p>输入实例管理员密钥。</p>"
        "<form method='post' action='/login'>"
        f"<input type='hidden' name='nonce' value='{escaped}'>"
        "<label for='credential'>管理员密钥</label>"
        "<input id='credential' name='credential' type='password' "
        "autocomplete='current-password' required autofocus>"
        "<button type='submit'>登录</button></form></main></body></html>"
    ).encode("utf-8")


def _secure(response: Response) -> Response:
    existing = {name.lower() for name, _value in response.headers}
    headers = list(response.headers)
    headers.extend(
        (name, value)
        for name, value in _SECURITY_HEADERS
        if name.lower() not in existing
    )
    return Response(response.status, tuple(headers), response.body)


def _json_response(status: int, payload: object) -> Response:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return Response(
        status,
        ((b"Content-Type", b"application/json; charset=utf-8"),),
        body,
    )


def _json_error(
    request_id: str,
    status: int,
    code: str,
    message: str,
) -> Response:
    return _json_response(
        status,
        {"requestId": request_id, "code": code, "message": message},
    )


def _single_header(request: Request, name: bytes) -> bytes:
    values = request.header_values(name)
    if len(values) != 1 or not values[0]:
        raise AdminWebError(400, "invalid_headers", "请求头无效。")
    return values[0]


def _optional_single_header(request: Request, name: bytes) -> bytes | None:
    values = request.header_values(name)
    if not values:
        return None
    if len(values) != 1:
        raise AdminWebError(400, "invalid_headers", "请求头无效。")
    return values[0]


def _cookies(request: Request) -> dict[str, str]:
    raw = _optional_single_header(request, b"cookie")
    if raw is None:
        return {}
    try:
        text = raw.decode("ascii")
        parsed = SimpleCookie()
        parsed.load(text)
    except (UnicodeDecodeError, ValueError):
        raise AdminWebError(400, "invalid_cookie", "Cookie 无效。") from None
    return {name: morsel.value for name, morsel in parsed.items()}


def _cookie(name: str, value: str) -> bytes:
    return (
        f"{name}={value}; Path=/; HttpOnly; SameSite=Strict"
    ).encode("ascii")


def _expired_cookie(name: str) -> bytes:
    return (
        f"{name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    ).encode("ascii")


def _content_type(request: Request) -> str:
    raw = _single_header(request, b"content-type")
    try:
        return raw.decode("ascii").split(";", 1)[0].strip().lower()
    except UnicodeDecodeError:
        raise AdminWebError(415, "invalid_content_type", "请求格式不支持。") from None


def _parse_form(request: Request) -> Mapping[str, list[str]]:
    if _content_type(request) != _FORM_TYPE:
        raise AdminWebError(415, "invalid_content_type", "登录表单格式无效。")
    try:
        body = request.body.decode("utf-8")
        values = parse_qs(
            body,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        )
    except (UnicodeDecodeError, ValueError):
        raise AdminWebError(400, "invalid_form", "登录表单无效。") from None
    if set(values) != {"nonce", "credential"}:
        raise AdminWebError(400, "invalid_form", "登录表单无效。")
    return values


def _parse_json(request: Request) -> dict[str, object]:
    if _content_type(request) != _JSON_TYPE:
        raise AdminWebError(415, "invalid_content_type", "请求必须使用 JSON。")
    try:
        text = request.body.decode("utf-8")

        def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
            if len(values) > _MAX_JSON_FIELDS:
                raise ValueError("too many JSON fields")
            result: dict[str, object] = {}
            for key, value in values:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result

        value = json.loads(text, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise AdminWebError(400, "invalid_json", "请求 JSON 无效。") from None
    if not isinstance(value, dict):
        raise AdminWebError(400, "invalid_json", "请求 JSON 必须是对象。")
    return value


def _one(values: Mapping[str, list[str]], name: str) -> str:
    items = values.get(name)
    if items is None or len(items) != 1 or not items[0]:
        raise AdminWebError(400, "invalid_input", f"缺少字段 {name}。")
    return items[0]


def _optional_one(values: Mapping[str, list[str]], name: str) -> str | None:
    items = values.get(name)
    if items is None:
        return None
    if len(items) != 1 or not items[0]:
        raise AdminWebError(400, "invalid_query", f"查询参数 {name} 无效。")
    return items[0]


def _require_query_keys(
    values: Mapping[str, list[str]],
    allowed: set[str],
) -> None:
    unknown = values.keys() - allowed
    if unknown:
        raise AdminWebError(400, "invalid_query", "包含未知查询参数。")


def _page_size(values: Mapping[str, list[str]]) -> int:
    raw = _optional_one(values, "pageSize")
    if raw is None:
        return 25
    try:
        value = int(raw, 10)
    except ValueError:
        raise AdminWebError(400, "invalid_page_size", "分页大小无效。") from None
    if not 1 <= value <= 50:
        raise AdminWebError(400, "invalid_page_size", "分页大小必须为 1 到 50。")
    return value


def _session_page_size(values: Mapping[str, list[str]]) -> int:
    raw = _optional_one(values, "pageSize")
    if raw is None:
        return 20
    try:
        value = int(raw, 10)
    except ValueError:
        raise AdminWebError(400, "invalid_page_size", "分页大小无效。") from None
    if value not in _SESSION_PAGE_SIZES:
        raise AdminWebError(
            400,
            "invalid_page_size",
            "Sessions 分页大小必须为 10、20、50 或 100。",
        )
    return value


def _batches(values: Sequence[str], size: int) -> tuple[tuple[str, ...], ...]:
    if size < 1:
        raise ValueError("batch size must be positive")
    return tuple(
        tuple(values[index : index + size])
        for index in range(0, len(values), size)
    )


def _chat_open_url(chat: ChatLabel) -> str:
    if chat.chat_mode == "p2p" and chat.p2p_target_open_id is not None:
        query = urlencode({"openId": chat.p2p_target_open_id})
    else:
        query = urlencode({"openChatId": chat.chat_id})
    return f"https://applink.feishu.cn/client/chat/open?{query}"


def _optional_text_query(
    values: Mapping[str, list[str]],
    name: str,
    *,
    maximum: int = _MAX_TEXT_BYTES,
) -> str | None:
    value = _optional_one(values, name)
    if value is None:
        return None
    if len(value.encode("utf-8")) > maximum or value.strip() != value:
        raise AdminWebError(400, "invalid_query", f"查询参数 {name} 无效。")
    return value


def _created_range_query(
    values: Mapping[str, list[str]],
) -> tuple[str | None, str | None]:
    created_from = _optional_created_time_query(values, "createdFrom")
    created_before = _optional_created_time_query(values, "createdBefore")
    if (
        created_from is not None
        and created_before is not None
        and created_from >= created_before
    ):
        raise AdminWebError(
            400,
            "invalid_time_range",
            "创建时间的开始时间必须早于结束时间。",
        )
    return created_from, created_before


def _optional_created_time_query(
    values: Mapping[str, list[str]],
    name: str,
) -> str | None:
    raw = _optional_text_query(values, name, maximum=64)
    if raw is None:
        return None
    if _ISO_INSTANT_PATTERN.fullmatch(raw) is None:
        raise AdminWebError(
            400,
            "invalid_time",
            f"查询参数 {name} 必须是带时区的 ISO-8601 时间。",
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (OverflowError, ValueError):
        raise AdminWebError(
            400,
            "invalid_time",
            f"查询参数 {name} 必须是带时区的 ISO-8601 时间。",
        ) from None


def _optional_bool_query(
    values: Mapping[str, list[str]],
    name: str,
) -> bool | None:
    value = _optional_one(values, name)
    if value is None:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise AdminWebError(400, "invalid_query", f"查询参数 {name} 必须是布尔值。")


def _optional_scope_kind(values: Mapping[str, list[str]]) -> ScopeKind | None:
    raw = _optional_one(values, "scopeKind")
    if raw is None:
        return None
    try:
        return ScopeKind(raw)
    except ValueError:
        raise AdminWebError(400, "invalid_scope_kind", "Scope 类型无效。") from None


def _session_inventory_state(
    values: Mapping[str, list[str]],
) -> SessionInventoryState | None:
    raw = _optional_one(values, "inventoryState")
    if raw is None:
        return SessionInventoryState.ACTIVE
    if raw == "all":
        return None
    try:
        return SessionInventoryState(raw)
    except ValueError:
        raise AdminWebError(
            400,
            "invalid_inventory_state",
            "会话状态无效。",
        ) from None


def _id_query(values: Mapping[str, list[str]], name: str) -> tuple[str, ...]:
    raw_values = values.get(name, [])
    result: list[str] = []
    for raw in raw_values:
        for value in raw.split(","):
            if not value or value.strip() != value or len(value.encode("utf-8")) > 256:
                raise AdminWebError(400, "invalid_ids", f"{name} 包含无效 ID。")
            result.append(value)
    return tuple(result)


def _required_text(
    payload: Mapping[str, object],
    name: str,
    *,
    maximum: int,
) -> str:
    value = payload.get(name)
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 0x20 for character in value)
    ):
        raise AdminWebError(400, "invalid_input", f"字段 {name} 无效。")
    return value


def _optional_text_body(
    payload: Mapping[str, object],
    name: str,
    *,
    maximum: int,
) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    return _required_text(payload, name, maximum=maximum)


def _required_bool(payload: Mapping[str, object], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise AdminWebError(400, "invalid_input", f"字段 {name} 必须是布尔值。")
    return value


def _required_int(
    payload: Mapping[str, object],
    name: str,
    *,
    minimum: int,
) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AdminWebError(400, "invalid_input", f"字段 {name} 必须是整数。")
    return value


def _require_body_keys(payload: Mapping[str, object], allowed: set[str]) -> None:
    if payload.keys() != allowed:
        missing = allowed - payload.keys()
        unknown = payload.keys() - allowed
        if missing or unknown:
            raise AdminWebError(400, "invalid_input", "请求字段不完整或包含未知字段。")


_ACTION_KEYS = {"csrfToken", "actionToken", "target"}


def _action_target(payload: Mapping[str, object]) -> AdminActionTarget:
    raw = payload.get("target")
    if not isinstance(raw, dict):
        raise AdminWebError(400, "invalid_target", "操作目标无效。")
    allowed = {"resource", "targetId", "scopeKey"}
    if not set(raw).issubset(allowed) or {"resource", "targetId"} - set(raw):
        raise AdminWebError(400, "invalid_target", "操作目标无效。")
    resource = _required_text(raw, "resource", maximum=64)
    target_id = _required_text(raw, "targetId", maximum=512)
    scope_key = _optional_text_body(raw, "scopeKey", maximum=1_024)
    return AdminActionTarget(resource, target_id, scope_key)


def _action_target_json(target: AdminActionTarget) -> dict[str, object]:
    return {
        "resource": target.resource,
        "targetId": target.target_id,
        "scopeKey": target.scope_key,
    }


def _empty_preconditions(
    *,
    active_binding_id: ExpectedValue[str] | None = None,
    project_revision: ExpectedValue[int] | None = None,
    settings_revision: ExpectedValue[int] | None = None,
    native_thread_id: ExpectedValue[str] | None = None,
    activity_revision: ExpectedValue[int] | None = None,
    physical_turn_id: ExpectedValue[str] | None = None,
    side_app_id: ExpectedValue[str] | None = None,
    side_chat_id: ExpectedValue[str] | None = None,
    side_topic_id: ExpectedValue[str] | None = None,
    side_root_message_id: ExpectedValue[str] | None = None,
) -> AdminActionPreconditions:
    return AdminActionPreconditions(
        active_binding_id or ExpectedValue.dont_check(),
        project_revision or ExpectedValue.dont_check(),
        settings_revision or ExpectedValue.dont_check(),
        native_thread_id or ExpectedValue.dont_check(),
        activity_revision or ExpectedValue.dont_check(),
        physical_turn_id or ExpectedValue.dont_check(),
        side_app_id or ExpectedValue.dont_check(),
        side_chat_id or ExpectedValue.dont_check(),
        side_topic_id or ExpectedValue.dont_check(),
        side_root_message_id or ExpectedValue.dont_check(),
    )


def _grant_preconditions(grant: object) -> AdminActionPreconditions:
    value = getattr(grant, "preconditions", None)
    if not isinstance(value, AdminActionPreconditions):
        raise RuntimeError("Admin action precondition type mismatch")
    return value


def _expected_value(expected: ExpectedValue[T], label: str) -> T:
    if expected.mode.value != "expect_value" or expected.value is None:
        raise RuntimeError(f"{label} action precondition is unavailable")
    return expected.value


def _expected_optional(expected: ExpectedValue[T], label: str) -> T | None:
    if expected.mode.value == "expect_none":
        return None
    return _expected_value(expected, label)


def _binding_target(grant: object) -> ExactBindingTarget:
    target = getattr(grant, "target", None)
    if not isinstance(target, AdminActionTarget) or target.scope_key is None:
        raise RuntimeError("Binding action target type mismatch")
    preconditions = _grant_preconditions(grant)
    expected = preconditions.active_binding_id
    if expected.mode.value == "expect_none":
        active = None
    else:
        active = _expected_value(expected, "active Binding")
    return ExactBindingTarget(
        scope_key=target.scope_key,
        binding_id=target.target_id,
        expected_active_binding_id=active,
    )


def _lifecycle_binding_target(grant: object) -> ExactBindingTarget:
    """Resolve an exact Binding without coupling lifecycle to Scope activity."""

    target = getattr(grant, "target", None)
    if not isinstance(target, AdminActionTarget) or target.scope_key is None:
        raise RuntimeError("Binding action target type mismatch")
    return ExactBindingTarget(
        scope_key=target.scope_key,
        binding_id=target.target_id,
        expected_active_binding_id=None,
    )


def _runtime_precondition(grant: object) -> RuntimePrecondition:
    preconditions = _grant_preconditions(grant)
    activity_revision = _expected_value(
        preconditions.activity_revision,
        "runtime activity revision",
    )
    physical_turn_id = _expected_optional(
        preconditions.physical_turn_id,
        "physical Turn",
    )
    return RuntimePrecondition(activity_revision, physical_turn_id)


def _fingerprint(route: str, filters: object) -> str:
    canonical = json.dumps(
        {"route": route, "filters": filters},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:32]


def _encode_cursor(kind: str, values: Sequence[str], fingerprint: str) -> str:
    payload = json.dumps(
        {"v": 1, "t": kind, "k": list(values), "f": fingerprint},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(
    encoded: str | None,
    *,
    kind: str,
    length: int,
    fingerprint: str,
) -> tuple[str, ...] | None:
    if encoded is None:
        return None
    if len(encoded) > 2_048:
        raise AdminWebError(400, "invalid_cursor", "分页游标无效。")
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        raise AdminWebError(400, "invalid_cursor", "分页游标无效。") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "t", "k", "f"}
        or payload["v"] != 1
        or payload["t"] != kind
        or payload["f"] != fingerprint
        or not isinstance(payload["k"], list)
        or len(payload["k"]) != length
        or any(not isinstance(value, str) or not value for value in payload["k"])
    ):
        raise AdminWebError(400, "invalid_cursor", "分页游标与当前筛选不匹配。")
    canonical = _encode_cursor(kind, payload["k"], fingerprint)
    if canonical != encoded:
        raise AdminWebError(400, "invalid_cursor", "分页游标无效。")
    return tuple(payload["k"])


def _encode_binding_cursor(
    cursor: BindingCursor | None,
    fingerprint: str,
) -> str | None:
    if cursor is None:
        return None
    return _encode_cursor(
        "binding",
        (cursor.created_at, cursor.binding_id),
        fingerprint,
    )


def _decode_binding_cursor(
    encoded: str | None,
    fingerprint: str,
) -> BindingCursor | None:
    values = _decode_cursor(
        encoded,
        kind="binding",
        length=2,
        fingerprint=fingerprint,
    )
    return BindingCursor(*values) if values is not None else None


def _encode_side_cursor(
    cursor: SideTopicCursor | None,
    fingerprint: str,
) -> str | None:
    if cursor is None:
        return None
    return _encode_cursor("side", (cursor.created_at, cursor.side_id), fingerprint)


def _decode_side_cursor(
    encoded: str | None,
    fingerprint: str,
) -> SideTopicCursor | None:
    values = _decode_cursor(
        encoded,
        kind="side",
        length=2,
        fingerprint=fingerprint,
    )
    return SideTopicCursor(*values) if values is not None else None


def _encode_project_cursor(cursor: str | None, fingerprint: str) -> str | None:
    if cursor is None:
        return None
    return _encode_cursor("project", (cursor,), fingerprint)


def _decode_project_cursor(encoded: str | None, fingerprint: str) -> str | None:
    values = _decode_cursor(
        encoded,
        kind="project",
        length=1,
        fingerprint=fingerprint,
    )
    return values[0] if values is not None else None


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            key: _jsonable(item)
            for key, item in asdict(value).items()
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"unsupported JSON projection: {type(value).__name__}")


def _settings_json(settings: BindingTurnSettings | None) -> dict[str, str] | None:
    if settings is None:
        return None
    return {
        "modelId": settings.model_id,
        "effortId": settings.effort_id,
        "serviceTierId": settings.service_tier_id,
    }


def _runtime_binding_json(status: BindingStatusProjection) -> dict[str, object]:
    snapshot = status.snapshot
    turn = snapshot.turn
    goal = snapshot.goal
    lifecycle = snapshot.lifecycle
    subscription = snapshot.subscription
    usage = snapshot.context_window_usage
    return {
        "bindingId": snapshot.binding_id,
        "activityRevision": snapshot.activity_revision,
        "primaryStatus": status.primary_status,
        "primaryStatusResolution": status.primary_status_resolution.value,
        "subscriptionState": (
            status.subscription_state.value
            if status.subscription_state is not None
            else None
        ),
        "turn": (
            {
                "threadId": turn.thread_id,
                "turnId": turn.turn_id,
                "state": turn.state.value,
            }
            if turn is not None
            else None
        ),
        "goal": (
            {
                "threadId": goal.thread_id,
                "logicalTurnId": goal.logical_turn_id,
                "state": goal.state.value,
            }
            if goal is not None
            else None
        ),
        "compacting": snapshot.compacting,
        "lifecycle": (
            {
                "threadId": lifecycle.thread_id,
                "state": lifecycle.state.value,
            }
            if lifecycle is not None
            else None
        ),
        "subscription": (
            {
                "threadId": subscription.thread_id,
                "state": subscription.state.value,
                "releaseInSeconds": subscription.release_in_seconds,
            }
            if subscription is not None
            else None
        ),
        "contextWindow": (
            {
                "usedTokens": usage.used_tokens,
                "contextWindowTokens": usage.context_window_tokens,
            }
            if usage is not None
            else None
        ),
    }


def _stop_disposition_message(disposition: StopDisposition) -> str:
    return {
        StopDisposition.NOT_RUNNING: "该会话当前没有运行任务。",
        StopDisposition.REQUESTED: (
            "已请求中断 exact Codex Turn；确认终态前仍会显示为停止中。"
        ),
        StopDisposition.STOPPING: (
            "该会话正在停止；已再次尝试完成中断与终端清理。"
        ),
        StopDisposition.COMPACTING: (
            "该会话正在压缩，当前没有已验证的安全取消能力。"
        ),
        StopDisposition.GOAL_REQUESTED: (
            "已请求暂停 Goal 并中断当前物理 Turn。"
        ),
        StopDisposition.GOAL_STOPPING: "该 Goal 正在暂停。",
        StopDisposition.EXTERNAL_GOAL: (
            "这是外部 active Goal，当前无法安全重挂并暂停。"
        ),
    }[disposition]


def _release_disposition_message(disposition: ReleaseDisposition) -> str:
    return {
        ReleaseDisposition.NOT_MATERIALIZED: (
            "该会话尚未物化，没有原生 Thread 订阅可释放。"
        ),
        ReleaseDisposition.NOT_SUBSCRIBED: (
            "本进程当前没有该 Thread 的订阅；Binding 与原生历史均保留。"
        ),
        ReleaseDisposition.RELEASED: (
            "已取消本进程对该 Thread 的订阅；Binding 与原生历史均保留，"
            "下次消息仍会 resume 同一 Thread。"
        ),
    }[disposition]


def _runtime_side_json(snapshot: Any) -> dict[str, object]:
    return {
        "sideId": snapshot.side_id,
        "parentBindingId": snapshot.parent_binding_id,
        "threadId": snapshot.thread_id,
        "state": snapshot.state.value,
        "turnId": snapshot.turn_id,
        "turnState": (
            snapshot.turn_state.value if snapshot.turn_state is not None else None
        ),
    }


def _project_result(context: _RequestContext, project: Any) -> dict[str, object]:
    return {
        "requestId": context.request.request_id,
        "alias": project.alias,
        "cwd": str(project.cwd),
        "enabled": project.enabled,
        "revision": project.revision,
    }


def _binding_result(context: _RequestContext, binding: Any) -> dict[str, object]:
    return {
        "requestId": context.request.request_id,
        "bindingId": binding.id,
        "scopeKey": binding.scope_key,
        "current": binding.active,
        "nativeThreadId": binding.native_thread_id,
        "settingsRevision": binding.settings_revision,
        "messageContextMode": binding.message_context_mode.value,
        "contextRevision": binding.context_revision,
    }


def _map_error(error: BaseException) -> AdminWebError | None:
    if isinstance(error, AdminWebError):
        return error
    if isinstance(
        error,
        (
            ValueError,
            AmbiguousBinding,
        ),
    ):
        return AdminWebError(400, "invalid_input", "请求参数无效。")
    if isinstance(
        error,
        (BindingNotFound, ScopeNotFound, SideTopicNotFound, UnknownProject),
    ):
        return AdminWebError(404, "not_found", "目标不存在。")
    if isinstance(
        error,
        (
            ActivePointerChanged,
            BindingScopeMismatch,
            BindingSettingsRevisionConflict,
            RuntimeStateChanged,
            StaleProject,
            StoredProjectConflict,
            ProjectAlreadyExists,
            ProjectDisabled,
            SideIdentityMismatch,
            SideSessionConflict,
            ThreadArchived,
            ThreadNotArchived,
            ThreadNotMaterialized,
            ThreadDeleteUnavailable,
            ThreadCompacting,
            ThreadGoalActive,
            ThreadRunningConfiguration,
            ThreadBackgroundTerminalsActive,
            NativeThreadMissing,
        ),
    ):
        return AdminWebError(409, "stale_or_conflict", "状态已变化，请刷新后重试。")
    if isinstance(error, (BindingQueryBusy, BlockingIOExecutorSaturated)):
        return AdminWebError(429, "busy", "管理服务繁忙，请稍后重试。")
    if isinstance(error, BindingQueryTimeout):
        return AdminWebError(504, "query_timeout", "查询超时，请缩小筛选范围。")
    if isinstance(error, ThreadCatalogDeadlineExceeded):
        return AdminWebError(504, "query_timeout", "原生会话查询超时。")
    if isinstance(error, TimeoutError):
        return AdminWebError(504, "deadline_exceeded", "操作等待超时，请刷新对账。")
    if isinstance(
        error,
        (
            AdmissionClosed,
            AuthCapacityExceeded,
            BindingQueryClosed,
            BlockingIOExecutorClosed,
            BlockingIOResultUnknown,
            BlockingIODrainTimeout,
            BlockingIOShutdownTimeout,
            NativeCatalogInconsistent,
            RuntimeClosed,
            ThreadCatalogError,
            ThreadLifecycleStateUnknown,
            ThreadReleaseStateUnknown,
        ),
    ):
        return AdminWebError(503, "state_unknown", "结果未确认，请刷新对账。")
    if isinstance(error, MalformedActionGrant):
        return AdminWebError(400, "invalid_action", "操作凭据无效。")
    if isinstance(
        error,
        (
            ProjectError,
            SideCloseFailed,
            SideSessionNotFound,
            ThreadLifecycleError,
            ThreadReleaseError,
        ),
    ):
        return AdminWebError(409, "operation_rejected", str(error)[:500])
    return None
