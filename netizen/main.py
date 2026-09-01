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
from .bindings import BindingStore
from .channel_app import ChannelApplication
from .codex_runtime import CodexRuntime
from .management import (
    InstanceManagementService,
    ManagementRuntimePort,
    ScopeCoordinator,
)
from .message_history import FeishuMessageHistoryReader
from .projects import ProjectRegistry
from .sdk_gap_adapter import (
    AppServerGoalControl,
    AppServerSideBoundaryControl,
    AppServerSkillCatalog,
    AppServerThreadDeleteControl,
    AppServerThreadSubscriptionControl,
    SdkGapCapabilityUnavailable,
)
from .settings import Settings
from .terminal_cleanup import PinnedExperimentalTerminalCleanup
from .turn_plan_observer import (
    PinnedTurnActivityObserver,
    TurnActivityObservationUnavailable,
)


logger = logging.getLogger(__name__)


_CODEX_SERVICE_CONFIG_OVERRIDES = ("allow_login_shell=false",)
_SHUTDOWN_BUDGET_SECONDS = 60.0
_READY_MARKER_CONTENT = b"netizen service ready\n"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 2


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
    ) -> None:
        self._settings = settings
        self._channel = channel
        self._store = store
        self._projects = projects
        self._codex: AsyncCodex | None = None
        self._runtime: CodexRuntime | None = None
        self._management: InstanceManagementService | None = None
        self._message_history_client: Any | None = None
        self._admin: AdminWebRunner | None = None
        self.application: ChannelApplication | None = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
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
                )
                await self._admin.bind()

            expired_sides = self._store.expire_live_side_topics()
            if expired_sides:
                logger.info(
                    "expired Side Topics from a previous process",
                    extra={"count": len(expired_sides)},
                )
            self._codex = AsyncCodex(
                CodexConfig(config_overrides=_CODEX_SERVICE_CONFIG_OVERRIDES)
            )
            await self._codex.__aenter__()
            terminal_cleanup = PinnedExperimentalTerminalCleanup(self._codex)
            thread_subscription_control = AppServerThreadSubscriptionControl(
                self._codex
            )
            skill_catalog = None
            goal_control = None
            side_boundary_control = None
            thread_delete_control = None
            turn_plan_observer = None
            try:
                skill_catalog = AppServerSkillCatalog(self._codex)
            except SdkGapCapabilityUnavailable as error:
                logger.warning("native Skills unavailable: %s", error)
            try:
                goal_control = AppServerGoalControl(self._codex)
            except SdkGapCapabilityUnavailable as error:
                logger.warning("native Goal unavailable: %s", error)
            if callable(getattr(self._codex, "thread_fork", None)):
                try:
                    side_boundary_control = AppServerSideBoundaryControl(self._codex)
                except SdkGapCapabilityUnavailable as error:
                    logger.warning("native Side unavailable: %s", error)
            else:
                logger.warning("native Side unavailable: AsyncCodex.thread_fork missing")
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
                goal_control=goal_control,
                side_boundary_control=side_boundary_control,
                thread_subscription_control=thread_subscription_control,
                background_terminal_inspector=terminal_cleanup,
                thread_delete_control=thread_delete_control,
                turn_plan_observer=turn_plan_observer,
            )
            scope_coordinator = ScopeCoordinator()
            self._management = InstanceManagementService(
                bindings=self._store,
                projects=self._projects,
                runtime=ManagementRuntimePort(self._runtime),
                scope_coordinator=scope_coordinator,
                chat_labels=self._channel,
            )
            if self._admin is not None:
                self._admin.attach_management(self._management)
            self._message_history_client = (
                lark.Client.builder()
                .app_id(self._settings.app_id)
                .app_secret(self._settings.app_secret)
                .timeout(10)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
            self.application = ChannelApplication(
                app_id=self._settings.app_id,
                channel=self._channel,
                runtime=self._runtime,
                bindings=self._store,
                projects=self._projects,
                message_history=FeishuMessageHistoryReader(
                    self._message_history_client
                ),
                scope_coordinator=scope_coordinator,
                management=self._management,
            )
            if expired_sides:
                await self.application.refresh_expired_side_cards(expired_sides)
            self._started = True
        except BaseException:
            await self._close_partial_start()
            raise

    def open_admission(self) -> None:
        if not self._started or self.application is None:
            raise RuntimeError("service core is not ready for admission")
        if self._admin is not None:
            self._admin.open_admission()

    async def _close_partial_start(self) -> None:
        deadline = asyncio.get_running_loop().time() + _SHUTDOWN_BUDGET_SECONDS
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
        deadline = asyncio.get_running_loop().time() + _SHUTDOWN_BUDGET_SECONDS
        try:
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
            if self._management is not None:
                await _cleanup_with_budget(
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
                if self.application is not None:
                    await _cleanup_with_budget(
                        "Feishu reaction cleanup",
                        self.application.close,
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
                    await _cleanup_with_budget(
                        "Binding Store close",
                        self._store.aclose,
                        deadline=deadline,
                        cap=5,
                    )


async def run(settings: Settings, *, ready_file: Path | None = None) -> None:
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(settings.data_dir, 0o700)
    store = BindingStore(settings.data_dir / "channel.sqlite3")
    try:
        projects = ProjectRegistry(
            store=store,
            default_cwd=settings.default_cwd,
            projects=settings.projects,
            project_root=settings.project_root,
        )
        channel = build_channel(settings, store)
    except BaseException:
        store.close()
        raise
    core = ServiceCore(
        settings=settings,
        channel=channel,
        store=store,
        projects=projects,
    )
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
        _register_channel_handlers(channel, core.application)
        await channel.start_background()
        await _await_channel_future(channel.schedule(_open_core_admission(core)))
        if ready_file is not None:
            _publish_ready_marker(ready_file)
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


async def _open_core_admission(core: ServiceCore) -> None:
    core.open_admission()


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
    os.environ.pop("FEISHU_APP_SECRET", None)
    os.environ.pop("FEISHU_APP_SECRET_FILE", None)
    os.environ.pop("NETIZEN_ADMIN_SECRET", None)
    os.environ.pop("NETIZEN_ADMIN_SECRET_FILE", None)
    os.environ.pop("NETIZEN_CONFIG_PATH", None)
    os.environ.pop("NETIZEN_LOG_FILE", None)
    os.environ.pop("NETIZEN_MANAGED_LAUNCH_AGENT", None)
    os.environ.pop("NETIZEN_READY_FILE", None)


def _managed_absolute_path(raw: str, *, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or path == Path(path.anchor):
        raise RuntimeError(f"{label} must be an absolute non-root path: {path}")
    return path


def _adopt_lifetime_lock() -> int | None:
    raw_descriptor = os.environ.pop("NETIZEN_LIFETIME_LOCK_FD", None)
    raw_path = os.environ.pop("NETIZEN_LIFETIME_LOCK_FILE", None)
    if raw_descriptor is None and raw_path is None:
        return None
    if raw_descriptor is None or raw_path is None:
        raise RuntimeError("managed service lifetime lock environment is incomplete")
    if not raw_descriptor.isdecimal():
        raise RuntimeError("NETIZEN_LIFETIME_LOCK_FD is not a file descriptor")
    descriptor = int(raw_descriptor)
    lock_path = _managed_absolute_path(
        raw_path,
        label="NETIZEN_LIFETIME_LOCK_FILE",
    )
    if lock_path.is_symlink():
        raise RuntimeError(f"service lifetime lock must not be a symlink: {lock_path}")
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = lock_path.stat()
    except OSError as error:
        raise RuntimeError(f"could not adopt service lifetime lock: {error}") from error
    if (
        not stat.S_ISREG(descriptor_metadata.st_mode)
        or descriptor_metadata.st_uid != os.geteuid()
        or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
    ):
        raise RuntimeError("service lifetime lock FD does not match its managed path")
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
    raw_log_file = os.environ.get("NETIZEN_LOG_FILE", "").strip()
    if not raw_log_file:
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
    lifetime_descriptor = _adopt_lifetime_lock()
    try:
        _configure_platform_trust()
        raw_ready_file = os.environ.get("NETIZEN_READY_FILE", "").strip()
        if lifetime_descriptor is not None and not raw_ready_file:
            raise RuntimeError("managed service environment is missing NETIZEN_READY_FILE")
        ready_file = (
            _managed_absolute_path(raw_ready_file, label="NETIZEN_READY_FILE")
            if lifetime_descriptor is not None
            else None
        )
        _configure_logging()
        config_path = Path(os.environ.get("NETIZEN_CONFIG_PATH", "config.yaml"))
        settings = Settings.from_file(config_path)
        _scrub_channel_environment()
        asyncio.run(run(settings, ready_file=ready_file))
    finally:
        if lifetime_descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(lifetime_descriptor)


if __name__ == "__main__":
    main()
