"""Single-process Feishu Channel + one long-lived AsyncCodex service."""

from __future__ import annotations

import os

# The launcher makes this one descriptor inheritable only for its final exec.
# Restore CLOEXEC before importing SDK modules so import-time subprocesses could
# never inherit the service-lifetime lock.
_EARLY_LIFETIME_DESCRIPTOR = os.environ.get("NETIZEN_LIFETIME_LOCK_FD", "")
if _EARLY_LIFETIME_DESCRIPTOR.isdecimal():
    try:
        os.set_inheritable(int(_EARLY_LIFETIME_DESCRIPTOR), False)
    except OSError:
        # _adopt_lifetime_lock() reports the authoritative validation error.
        pass
del _EARLY_LIFETIME_DESCRIPTOR

import time

_IMPORT_STARTED_AT = time.monotonic()

import asyncio
import concurrent.futures
import contextlib
import logging
import signal
import stat
import sys
import uuid
from collections.abc import Awaitable, Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from lark_channel import (
    ChatQueueConfig,
    ChannelConfig,
    Events,
    FeishuChannel,
    InboundConfig,
    LogLevel,
    OutboundConfig,
    PolicyConfig,
    SafetyConfig,
    SecurityConfig,
    TextBatchConfig,
)
import lark_oapi as lark
from openai_codex import AsyncCodex, CodexConfig

from .admin.web import AdminWebRunner
from .admin.auth import CredentialFileError, load_credential_snapshot
from .bindings import BindingStore, validate_channel_database
from .builtin_skills import builtin_skill_root
from .cli_data import (
    InstanceDataError,
    StartupRejected,
    acquire_lifetime_lock,
    prepare_instance,
)
from .channel_app import ChannelApplication
from .codex_runtime import CodexRuntime
from .instance import resolve_instance_root
from .management import (
    InstanceManagementService,
    ManagementRuntimePort,
    ScopeCoordinator,
)
from .management.chat_directory import FeishuChatDirectory
from .message_history import FeishuMessageHistoryReader
from .account_rate_limits import AppServerAccountRateLimits
from .projects import ProjectError, ProjectRegistry
from .sdk_gap_adapter import (
    AppServerGoalControl,
    AppServerSideBoundaryControl,
    AppServerSkillCatalog,
    AppServerSkillRoots,
    AppServerThreadDeleteControl,
    AppServerThreadSubscriptionControl,
    SdkGapCapabilityUnavailable,
)
from .settings import Settings
from .schedules.mcp import ScheduleMcpRunner
from .schedules.scheduler import Scheduler
from .terminal_cleanup import PinnedExperimentalTerminalCleanup
from .turn_plan_observer import (
    PinnedTurnActivityObserver,
    TurnActivityObservationUnavailable,
)

_IMPORT_SECONDS = time.monotonic() - _IMPORT_STARTED_AT


logger = logging.getLogger(__name__)


_CODEX_SERVICE_CONFIG_OVERRIDES = ("allow_login_shell=false",)
_SHUTDOWN_BUDGET_SECONDS = 60.0
_READY_MARKER_CONTENT = b"netizen service ready\n"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 2


def _log_startup_timing(stage: str, started_at: float) -> float:
    now = time.monotonic()
    logger.info("netizen startup: %s completed in %.3fs", stage, now - started_at)
    return now


def _configure_platform_trust() -> None:
    """Use the native macOS trust store for application-owned TLS."""

    if sys.platform != "darwin":
        return
    import truststore

    # Netizen is the application entry point, so application-wide injection is
    # intentional. It lets the Channel WebSocket honor Keychain-managed roots
    # without generating a CA bundle or inventing another environment policy.
    truststore.inject_into_ssl()


def build_channel(settings: Settings, store: BindingStore) -> FeishuChannel:
    return FeishuChannel(
        config=ChannelConfig(resolve_sender_names=True),
        app_id=settings.app_id,
        app_secret=settings.app_secret,
        log_level=LogLevel.WARNING,
        policy=PolicyConfig(
            dm_policy="open",
            group_policy="open",
            require_mention=True,
            respond_to_mention_all=False,
        ),
        safety=SafetyConfig(
            text_batch=TextBatchConfig(delay_ms=0, long_delay_ms=0),
            # Scope/Thread serialization belongs to CodexRuntime. The SDK's
            # queue is keyed only by chat_id and would incorrectly serialize
            # independent Feishu topics in the same group.
            chat_queue=ChatQueueConfig(enabled=False, merge_while_busy=False),
        ),
        # ADR 0011's exact-version first-level quote adapter consumes the
        # public InboundMessage.raw relation fields. Keep that contract
        # explicit instead of relying on the SDK's current default.
        inbound=InboundConfig(include_raw=True),
        outbound=OutboundConfig(
            reply_mode="static",
            text_chunk_limit=3_500,
        ),
        security=SecurityConfig(mode=settings.security_mode),
        dedup_store=store,
    )


class ServiceCore:
    """Objects that must live on FeishuChannel's background event loop."""

    def __init__(
        self,
        *,
        settings: Settings,
        channel: FeishuChannel,
        store: BindingStore,
        projects: ProjectRegistry,
        instance_root: Path,
    ) -> None:
        self._settings = settings
        self._channel = channel
        self._store = store
        self._projects = projects
        self._instance_root = instance_root.resolve()
        self._codex: AsyncCodex | None = None
        self._runtime: CodexRuntime | None = None
        self._management: InstanceManagementService | None = None
        self._feishu_read_client: Any | None = None
        self._admin: AdminWebRunner | None = None
        self._schedule_mcp: ScheduleMcpRunner | None = None
        self._scheduler: Scheduler | None = None
        self.application: ChannelApplication | None = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        stage_started_at = time.monotonic()
        try:
            if self._settings.admin_web.enabled:
                credential_path = self._settings.admin_web.credential_path
                if credential_path is None:
                    raise RuntimeError(
                        "Admin Web is enabled without a credential path"
                    )
                # Credential validation and the closed listener come first so
                # a bad secret or occupied port cannot start the Feishu side.
                self._admin = AdminWebRunner(
                    host=self._settings.admin_web.host,
                    port=self._settings.admin_web.port,
                    credential_path=credential_path,
                    instance_root=self._instance_root,
                    config_path=self._settings.config_path,
                    config_snapshot=self._settings.config_snapshot,
                    access_host=self._settings.admin_web.access_host,
                )
                await self._admin.bind()
                stage_started_at = _log_startup_timing("Admin listener", stage_started_at)

            # Bind the native MCP transport before App Server initializes its
            # catalog. Calls remain closed until the shared application is ready.
            self._schedule_mcp = ScheduleMcpRunner()
            await self._schedule_mcp.bind()
            stage_started_at = _log_startup_timing("Schedule MCP listener", stage_started_at)

            expired_sides = self._store.expire_live_side_topics()
            if expired_sides:
                logger.info(
                    "expired Side Topics from a previous process",
                    extra={"count": len(expired_sides)},
                )
            self._codex = AsyncCodex(
                CodexConfig(
                    config_overrides=(
                        *_CODEX_SERVICE_CONFIG_OVERRIDES,
                        *self._schedule_mcp.config_overrides,
                    ),
                    env={
                        **os.environ,
                        **self._schedule_mcp.app_server_env,
                        "NETIZEN_ROOT": str(self._instance_root),
                    },
                )
            )
            await self._codex.__aenter__()
            # Release-local Skills must be active before catalogs, recovery or
            # any native Thread can be reached. Failure closes this startup.
            await AppServerSkillRoots(self._codex).set_roots((builtin_skill_root(),))
            stage_started_at = _log_startup_timing("Codex connection", stage_started_at)
            terminal_cleanup = PinnedExperimentalTerminalCleanup(self._codex)
            thread_subscription_control = AppServerThreadSubscriptionControl(
                self._codex
            )
            skill_catalog = None
            account_rate_limits = None
            goal_control = None
            side_boundary_control = None
            thread_delete_control = None
            turn_plan_observer = None
            try:
                skill_catalog = AppServerSkillCatalog(self._codex)
            except SdkGapCapabilityUnavailable as error:
                logger.warning("native Skills unavailable: %s", error)
            try:
                account_rate_limits = AppServerAccountRateLimits(self._codex)
            except SdkGapCapabilityUnavailable as error:
                logger.warning("native account rate limits unavailable: %s", error)
            try:
                goal_control = AppServerGoalControl(self._codex)
            except SdkGapCapabilityUnavailable as error:
                logger.warning("native Goal unavailable: %s", error)
            try:
                side_boundary_control = AppServerSideBoundaryControl(self._codex)
            except SdkGapCapabilityUnavailable as error:
                logger.warning("native Side unavailable: %s", error)
            try:
                thread_delete_control = AppServerThreadDeleteControl(self._codex)
            except SdkGapCapabilityUnavailable as error:
                logger.warning("native Thread Delete unavailable: %s", error)
            try:
                turn_plan_observer = PinnedTurnActivityObserver(self._codex)
            except TurnActivityObservationUnavailable as error:
                logger.warning("native Turn activity observation unavailable: %s", error)
            self._runtime = CodexRuntime(
                codex=self._codex,
                bindings=self._store,
                terminal_cleanup=terminal_cleanup,
                skill_catalog=skill_catalog,
                account_rate_limits=account_rate_limits,
                goal_control=goal_control,
                side_boundary_control=side_boundary_control,
                thread_subscription_control=thread_subscription_control,
                background_terminal_inspector=terminal_cleanup,
                thread_delete_control=thread_delete_control,
                turn_plan_observer=turn_plan_observer,
            )
            scope_coordinator = ScopeCoordinator()
            self._feishu_read_client = (
                lark.Client.builder()
                .app_id(self._settings.app_id)
                .app_secret(self._settings.app_secret)
                .timeout(10)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
            self._management = InstanceManagementService(
                bindings=self._store,
                projects=self._projects,
                runtime=ManagementRuntimePort(self._runtime),
                scope_coordinator=scope_coordinator,
                chat_labels=self._channel,
                chat_directory=FeishuChatDirectory(self._feishu_read_client),
                root=self._instance_root,
            )
            schedules = self._management.enable_schedules(
                app_id=self._settings.app_id, chat_info=self._channel,
            )
            self._management.enable_defaults(
                app_id=self._settings.app_id, chat_info=self._channel,
            )

            async def manage_schedule(request: dict[str, Any], thread_id: str | None) -> dict[str, Any]:
                return await schedules.manage(request, native_thread_id=thread_id)

            self._schedule_mcp.attach(manage_schedule)
            if self._admin is not None:
                self._admin.attach_management(self._management)
            self.application = ChannelApplication(
                app_id=self._settings.app_id,
                channel=self._channel,
                runtime=self._runtime,
                bindings=self._store,
                projects=self._projects,
                message_history=FeishuMessageHistoryReader(
                    self._feishu_read_client
                ),
                management=self._management,
                admin_urls=self._admin.urls if self._admin is not None else (),
                admin_loopback_only=self._admin.loopback_only if self._admin is not None else None,
                instance_root=self._instance_root,
            )
            self._scheduler = Scheduler(
                bindings=self._store, runtime=self._runtime,
                app_id=self._settings.app_id,
                dispatch=self.application.dispatch_scheduled_run,
            )
            schedules.set_wake_handler(self._scheduler.wake)
            schedules.set_refresh_handler(self._scheduler.refresh)
            schedules.set_run_now_handler(self._scheduler.run_now)
            self._management.set_schedule_creation_drain(self._scheduler.drain_project_creation)
            await self._scheduler.recover()
            if expired_sides:
                await self.application.refresh_expired_side_cards(expired_sides)
            self._started = True
            _log_startup_timing("runtime recovery", stage_started_at)
        except BaseException:
            await self._close_partial_start()
            raise

    def open_admission(self) -> None:
        if not self._started or self.application is None:
            raise RuntimeError("service core is not ready for admission")
        if self._admin is not None:
            self._admin.open_admission()
        if self._schedule_mcp is not None:
            self._schedule_mcp.open_admission()
        if self._scheduler is not None:
            self._scheduler.start()

    async def _close_partial_start(self) -> None:
        deadline = asyncio.get_running_loop().time() + _SHUTDOWN_BUDGET_SECONDS
        if self._scheduler is not None:
            self._scheduler.close_admission()
            await _cleanup_with_budget(
                "partial Scheduler close", self._scheduler.close,
                deadline=deadline, cap=5,
            )
            await _cleanup_with_budget(
                "partial scheduled dispatch drain", lambda: self._scheduler.drain(deadline),
                deadline=deadline, cap=10,
            )
        if self._schedule_mcp is not None:
            self._schedule_mcp.close_admission()
            await _cleanup_with_budget(
                "partial schedule MCP close", self._schedule_mcp.close,
                deadline=deadline, cap=5,
            )
        if self._admin is not None:
            await _cleanup_with_budget(
                "partial Admin listener close",
                self._admin.close_listener,
                deadline=deadline,
                cap=5,
            )
            await _cleanup_with_budget(
                "partial Admin handler drain",
                lambda: self._admin.drain(deadline),
                deadline=deadline,
                cap=10,
            )
            self._admin.close_auth()
        if self._management is not None:
            await _cleanup_with_budget(
                "partial management I/O close",
                lambda: self._management.close(deadline=deadline),
                deadline=deadline,
                cap=10,
            )
        if self._runtime is not None:
            self._runtime.close_admission()
            await _cleanup_with_budget(
                "partial Runtime task cleanup",
                self._runtime.cancel_tasks,
                deadline=deadline,
                cap=5,
            )
        if self._codex is not None:
            await _cleanup_with_budget(
                "partial Codex transport close",
                self._codex.close,
                deadline=deadline,
                cap=10,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._management is not None:
            self._management.set_service_ready(False)
        deadline = asyncio.get_running_loop().time() + _SHUTDOWN_BUDGET_SECONDS
        management_closed = self._management is None
        try:
            if self._scheduler is not None:
                self._scheduler.close_admission()
            if self._schedule_mcp is not None:
                self._schedule_mcp.close_admission()
            if self._admin is not None:
                self._admin.close_admission()
            try:
                self._channel.update_policy(
                    dm_policy="disabled",
                    group_policy="disabled",
                )
            except Exception:
                logger.exception("failed to disable Feishu admission")
            if self._runtime is not None:
                self._runtime.close_admission()
            if self._scheduler is not None:
                await _cleanup_with_budget(
                    "Scheduler timer close", self._scheduler.close,
                    deadline=deadline, cap=3,
                )
            if self._admin is not None:
                await _cleanup_with_budget(
                    "Admin listener close",
                    self._admin.close_listener,
                    deadline=deadline,
                    cap=5,
                )
            safety = getattr(self._channel, "safety", None)
            if safety is not None:
                await _cleanup_with_budget(
                    "Feishu handler drain",
                    safety.dispose,
                    deadline=deadline,
                    cap=10,
                )
            if self._admin is not None:
                await _cleanup_with_budget(
                    "Admin handler drain",
                    lambda: self._admin.drain(deadline),
                    deadline=deadline,
                    cap=_SHUTDOWN_BUDGET_SECONDS,
                )
            if self._schedule_mcp is not None:
                await _cleanup_with_budget(
                    "schedule MCP handler drain", lambda: self._schedule_mcp.drain(deadline),
                    deadline=deadline, cap=10,
                )
            if self._scheduler is not None:
                await _cleanup_with_budget(
                    "scheduled dispatch drain", lambda: self._scheduler.drain(deadline),
                    deadline=deadline, cap=10,
                )
            if self._management is not None:
                management_closed = await _cleanup_with_budget(
                    "management I/O drain",
                    lambda: self._management.close(deadline=deadline),
                    deadline=deadline,
                    cap=15,
                )
            if self._runtime is not None:
                interrupted = await _cleanup_with_budget(
                    "native Turn interrupt",
                    self._runtime.interrupt_all,
                    deadline=deadline,
                    cap=15,
                )
                if interrupted:
                    remaining = max(
                        0.0,
                        min(15.0, deadline - asyncio.get_running_loop().time()),
                    )
                    idle = remaining > 0 and await self._runtime.wait_idle(
                        timeout=remaining
                    )
                    if not idle:
                        logger.warning("native Turns did not drain before SDK close")
                else:
                    logger.warning(
                        "native Turn cleanup did not complete; skipping completion drain"
                    )
        finally:
            try:
                try:
                    if self.application is not None:
                        await _cleanup_with_budget(
                            "Feishu presentation cleanup",
                            self.application.close,
                            deadline=deadline,
                            cap=4,
                        )
                finally:
                    # A cancelled or timed-out drain leaves submitted I/O running.
                    # Give it one final bounded join within the same shutdown budget.
                    if not management_closed and self._management is not None:
                        await _cleanup_with_budget(
                            "management I/O final cleanup",
                            lambda: self._management.close(deadline=deadline),
                            deadline=deadline,
                            cap=4,
                        )
            finally:
                try:
                    if self._codex is not None:
                        await _cleanup_with_budget(
                            "Codex transport close",
                            self._codex.close,
                            deadline=deadline,
                            cap=10,
                        )
                finally:
                    if self._runtime is not None:
                        await _cleanup_with_budget(
                            "Runtime task cleanup",
                            self._runtime.cancel_tasks,
                            deadline=deadline,
                            cap=5,
                        )
                    if self._admin is not None:
                        self._admin.close_auth()
                    if self._schedule_mcp is not None:
                        await _cleanup_with_budget(
                            "schedule MCP transport close", self._schedule_mcp.close,
                            deadline=deadline, cap=5,
                        )
                    await _cleanup_with_budget(
                        "Binding Store close",
                        self._store.aclose,
                        deadline=deadline,
                        cap=5,
                    )


async def run(
    settings: Settings,
    *,
    instance_root: Path,
    ready_file: Path | None = None,
) -> None:
    stage_started_at = time.monotonic()
    # Initialization belongs to explicit setup. A lost database must never turn
    # into a fresh empty instance merely because Runtime was restarted.
    validate_channel_database(settings.data_dir / "channel.sqlite3")
    store = BindingStore(settings.data_dir / "channel.sqlite3")
    stage_started_at = _log_startup_timing("database", stage_started_at)
    try:
        try:
            projects = ProjectRegistry(
                store=store,
                project_root=settings.project_root,
                projects=settings.projects,
            )
        except ProjectError as error:
            raise StartupRejected(str(error)) from error
        channel = build_channel(settings, store)
    except BaseException:
        store.close()
        raise
    core = ServiceCore(
        settings=settings,
        channel=channel,
        store=store,
        projects=projects,
        instance_root=instance_root,
    )
    _log_startup_timing("Projects and Channel construction", stage_started_at)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    core_started = False
    try:
        await _await_channel_future(channel.schedule(core.start()))
        core_started = True
        assert core.application is not None
        stage_started_at = time.monotonic()
        await _start_channel_input(channel, core.application, ready_file=ready_file)
        _log_startup_timing("Feishu connection", stage_started_at)
        await _await_channel_future(channel.schedule(_open_core_admission(core, ready_file=ready_file)))
        logger.info(
            "netizen service ready",
            extra={"projects": len(projects.list())},
        )
        await stop_event.wait()
    finally:
        try:
            if core_started:
                await _await_channel_future(channel.schedule(core.close()))
        finally:
            try:
                await asyncio.to_thread(channel.stop)
                if not core._closed:
                    store.close()
            finally:
                if ready_file is not None:
                    _clear_ready_marker(ready_file)


async def _start_channel_input(
    channel: FeishuChannel,
    application: ChannelApplication,
    *,
    ready_file: Path | None,
) -> None:
    _register_channel_handlers(channel, application)
    await channel.start_background()


async def _open_core_admission(core: ServiceCore, *, ready_file: Path | None = None) -> None:
    core.open_admission()
    if ready_file is not None:
        _publish_ready_marker(ready_file)
        assert core._management is not None
        core._management.set_service_ready(True)


async def _await_channel_future(
    future: concurrent.futures.Future[Any],
) -> Any:
    return await asyncio.wrap_future(future)


async def _cleanup_step(
    label: str,
    operation: Awaitable[object],
    *,
    timeout: float,
) -> bool:
    try:
        await asyncio.wait_for(operation, timeout=timeout)
    except TimeoutError:
        logger.warning("%s timed out after %.1fs", label, timeout)
        return False
    except Exception:
        logger.exception("%s failed", label)
        return False
    return True


async def _cleanup_with_budget(
    label: str,
    operation: Callable[[], Awaitable[object]],
    *,
    deadline: float,
    cap: float,
) -> bool:
    remaining = min(cap, deadline - asyncio.get_running_loop().time())
    if remaining <= 0:
        logger.warning("%s skipped because the shutdown budget was exhausted", label)
        return False
    return await _cleanup_step(label, operation(), timeout=remaining)


def _log_channel_error(error: object) -> None:
    logger.error("Feishu Channel error: %s", type(error).__name__)


def _register_channel_handlers(
    channel: FeishuChannel,
    application: ChannelApplication,
) -> None:
    channel.on(Events.MESSAGE, application.handle_message)
    channel.on(Events.CARD_ACTION, application.handle_card_action)
    channel.on(Events.ERROR, _log_channel_error)


def _scrub_channel_environment() -> None:
    # Hygiene only: with the accepted same-user/full-access Pilot boundary,
    # Codex can still read the protected secret file if explicitly instructed.
    # NETIZEN_ROOT stays as the non-secret, exact instance context for tools.
    os.environ.pop("FEISHU_APP_SECRET", None)
    os.environ.pop("FEISHU_APP_SECRET_FILE", None)
    os.environ.pop("NETIZEN_LARK_APP_CONFIG", None)
    os.environ.pop("NETIZEN_ADMIN_SECRET", None)
    os.environ.pop("NETIZEN_ADMIN_SECRET_FILE", None)
    os.environ.pop("NETIZEN_CONFIG_PATH", None)
    os.environ.pop("NETIZEN_LOG_FILE", None)
    os.environ.pop("NETIZEN_MANAGED_LAUNCH_AGENT", None)
    os.environ.pop("NETIZEN_READY_FILE", None)


def _managed_absolute_path(raw: str, *, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or path == Path(path.anchor):
        raise StartupRejected(f"{label} must be an absolute non-root path: {path}")
    return path


def _validate_instance_path(path: Path, expected: Path, *, label: str) -> None:
    if not path.is_absolute() or path.resolve() != expected:
        raise StartupRejected(f"{label} must belong to NETIZEN_ROOT: expected {expected}, got {path}")


def _validate_instance_environment(
    root: Path, config_path: Path, environment: dict[str, str],
) -> None:
    """Reject mixed instance paths before opening logs, credentials or state."""
    _validate_instance_path(config_path, root / "config.yaml", label="NETIZEN_CONFIG_PATH")
    expected_paths = {
        "NETIZEN_LARK_APP_CONFIG": root / "lark-app" / "config.json",
        "NETIZEN_ADMIN_SECRET_FILE": root / "credentials" / "admin-web-secret",
        "NETIZEN_LOG_FILE": root / "state" / "netizen.log",
        "NETIZEN_READY_FILE": root / "state" / "service.ready",
        "NETIZEN_LIFETIME_LOCK_FILE": root / "state" / "service.lifetime.lock",
    }
    for name, expected in expected_paths.items():
        if name in environment:
            _validate_instance_path(Path(environment[name]), expected, label=name)


def _adopt_lifetime_lock() -> int | None:
    raw_descriptor = os.environ.pop("NETIZEN_LIFETIME_LOCK_FD", None)
    raw_path = os.environ.pop("NETIZEN_LIFETIME_LOCK_FILE", None)
    if raw_descriptor is None and raw_path is None:
        return None
    if raw_descriptor is None or raw_path is None:
        raise StartupRejected("managed service lifetime lock environment is incomplete")
    if not raw_descriptor.isdecimal():
        raise StartupRejected("NETIZEN_LIFETIME_LOCK_FD is not a file descriptor")
    descriptor = int(raw_descriptor)
    lock_path = _managed_absolute_path(
        raw_path,
        label="NETIZEN_LIFETIME_LOCK_FILE",
    )
    if lock_path.is_symlink():
        raise StartupRejected(f"service lifetime lock must not be a symlink: {lock_path}")
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = lock_path.stat()
    except OSError as error:
        raise StartupRejected(f"could not adopt service lifetime lock: {error}") from error
    if (
        not stat.S_ISREG(descriptor_metadata.st_mode)
        or descriptor_metadata.st_uid != os.geteuid()
        or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
    ):
        raise StartupRejected("service lifetime lock FD does not match its managed path")
    os.set_inheritable(descriptor, False)
    return descriptor


def _publish_ready_marker(path: Path) -> None:
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        view = memoryview(_READY_MARKER_CONTENT)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise RuntimeError(f"could not publish service ready marker {path}: {error}") from error
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()


def _clear_ready_marker(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        logger.error("service ready marker became a directory: %s", path)
        return
    try:
        path.unlink()
    except OSError:
        logger.exception("failed to clear service ready marker")


def _configure_logging() -> None:
    raw_log_file = os.environ.get("NETIZEN_LOG_FILE", "")
    if not raw_log_file.strip():
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            force=True,
        )
        return
    log_file = _managed_absolute_path(raw_log_file, label="NETIZEN_LOG_FILE")
    if log_file.is_symlink() or (log_file.exists() and not log_file.is_file()):
        raise RuntimeError(f"managed service log is not a regular file: {log_file}")
    handler = RotatingFileHandler(
        log_file,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    os.chmod(log_file, 0o600)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[handler],
        force=True,
    )


def main() -> None:
    try:
        _run_service()
    except StartupRejected as error:
        if os.environ.get("NETIZEN_CLI_SERVICE") != "1":
            raise
        # systemd Restart=on-failure and launchd SuccessfulExit=false must not
        # loop over unchanged invalid data. No readiness marker is published;
        # `netizen start` still reports failure when readiness is not reached.
        logger.error("Netizen startup rejected: %s; repair the instance and start it again", error)


def _run_service() -> None:
    startup_environment = dict(os.environ)
    lifetime_descriptor = _adopt_lifetime_lock()
    try:
        stage_started_at = time.monotonic()
        if lifetime_descriptor is not None and "NETIZEN_ROOT" not in startup_environment:
            raise StartupRejected("managed service environment is missing NETIZEN_ROOT")
        try:
            instance_root = resolve_instance_root(environ=startup_environment)
        except ValueError as error:
            raise StartupRejected(str(error)) from error
        config_path = Path(startup_environment.get(
            "NETIZEN_CONFIG_PATH",
            str(instance_root / "config.yaml"),
        ))
        _validate_instance_environment(instance_root, config_path, startup_environment)
        # The entry point owns the instance context for both managed and manual
        # starts. Export it before creating any Codex/tool subprocesses.
        os.environ["NETIZEN_ROOT"] = str(instance_root)
        os.environ["NETIZEN_LARK_APP_CONFIG"] = str(instance_root / "lark-app" / "config.json")
        os.environ["NETIZEN_ADMIN_SECRET_FILE"] = str(instance_root / "credentials" / "admin-web-secret")
        _configure_platform_trust()
        raw_ready_file = startup_environment.get("NETIZEN_READY_FILE", "")
        if lifetime_descriptor is not None and not raw_ready_file:
            raise StartupRejected("managed service environment is missing NETIZEN_READY_FILE")
        ready_file = (
            _managed_absolute_path(raw_ready_file, label="NETIZEN_READY_FILE")
            if lifetime_descriptor is not None
            else None
        )
        try:
            settings = Settings.from_file(config_path)
        except (ValueError, FileNotFoundError, PermissionError) as error:
            raise StartupRejected(str(error)) from error
        _validate_instance_path(settings.data_dir, instance_root / "state", label="instance.dataDir")
        if settings.admin_web.credential_path is not None:
            _validate_instance_path(
                settings.admin_web.credential_path,
                instance_root / "credentials" / "admin-web-secret",
                label="Admin credential path",
            )
            if settings.admin_web.enabled:
                try:
                    load_credential_snapshot(settings.admin_web.credential_path)
                except CredentialFileError as error:
                    raise StartupRejected(str(error)) from error
        if lifetime_descriptor is None:
            try:
                lifetime_descriptor = acquire_lifetime_lock(instance_root)
            except (InstanceDataError, FileNotFoundError, PermissionError) as error:
                raise StartupRejected(str(error)) from error
        prepare_instance(instance_root, lifetime_descriptor=lifetime_descriptor)
        _configure_logging()
        logger.info("netizen startup: module imports completed in %.3fs", _IMPORT_SECONDS)
        _scrub_channel_environment()
        _log_startup_timing("configuration", stage_started_at)
        asyncio.run(run(settings, instance_root=instance_root, ready_file=ready_file))
    finally:
        if lifetime_descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(lifetime_descriptor)


if __name__ == "__main__":
    main()
