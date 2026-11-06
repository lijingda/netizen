"""Scheduled Run dispatch and the schedule manager card."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from lark_channel import (
    OutboundCard,
    SendOpts,
)

from .base import CHANNEL_LOGGER_NAME
from ..bindings import (
    BindingConflict,
    ProjectConflict,
    ProjectNotFound,
    ScopeConflict,
)
from ..cards import (
    CardActionError,
    fetched_card_topic_id,
    scope_from_fetched_card,
)
from ..group_access import GroupAccessRule, GroupNotEnabled
from .messages import public_chat_kind as _public_chat_kind
from .topics import TopicPublishError, complete_fresh_topic, send_topic_message
from ..cards.scheduled import (
    decode_schedule_action,
    schedule_form_card,
    schedule_draft_card,
    refresh_schedule_workspace,
    schedule_manager_card,
    schedule_navigation,
    schedule_query,
    schedule_retry_card,
    schedule_trigger_instructions,
)
from ..runtime.contracts import (
    GoalNotFound,
    GoalNotMaterialized,
    RuntimeClosed,
    SkillReferenceError,
    SteerRace,
    ThreadCompacting,
    ThreadGoalActive,
    ThreadLifecycleError,
    ThreadStopping,
    ThreadOccupied,
)
from ..domain import (
    FeishuScope,
    MentionContextMode,
    ScheduledBindingConversation,
    ScheduledBindingOrigin,
    ScheduledConversation,
    ScheduledOrigin,
    ScopeKind,
)
from ..worktrees.errors import WorktreeError
from ..worktrees.models import WorktreeConflict
from ..model_settings import ModelCatalogError
from ..prompt_projection import (
    ScheduledInputProjection,
)
from ..projects import ProjectError
from ..message_history import (
    MessageHistoryUnavailable,
)
from ..sdk_gap_adapter import SkillCatalogError
from ..skill_references import parse_skill_references
from ..schedules.models import Claim, ScheduleConflict, ScheduleError

from . import constants
from .app_values import (
    _CardReplyConversation,
    _CardReplyTarget,
)
from .session_tools import SessionToolChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class ScheduleChannel(SessionToolChannel):
    """Scheduled Run dispatch and the schedule manager card."""

    async def dispatch_scheduled_run(self, claim: Claim) -> None:
        """Publish one claimed occurrence and hand it to the ordinary Runtime."""
        group_rule, _ = self._group_access.snapshot()
        if claim.run.target_kind == "binding":
            await self._dispatch_binding_input(claim, group_rule)
            return
        plan, run = claim.plan, claim.run
        store = self._bindings.schedules
        native_submission = False
        publication_owned = False
        publishing_started = False
        workspace_creating = False
        workspace = None
        origin: ScheduledOrigin | None = None
        try:
            if run.app_id != self._app_id or plan.app_id != self._app_id:
                raise ScheduleError("计划所属飞书应用不一致。")
            store.begin_publication(run.id)
            publication_owned = True
            project = await self._management.resolve_new_project(
                run.project_alias,
                deadline=asyncio.get_running_loop().time() + 10.0,
            )
            chat_kind = _public_chat_kind(await self._channel.get_chat_info(run.chat_id))
            if chat_kind not in {"group", "p2p"}:
                raise ScheduleError("无法确认目标是可访问的飞书会话，本次定时任务未启动。")
            if not group_rule.allows(run.chat_id, chat_kind):
                raise GroupNotEnabled()
            if plan.session_settings.message_context_mode is MentionContextMode.CATCH_UP and chat_kind != "group":
                raise ScheduleError("私聊不支持读取群聊增量，请修改计划的消息读取范围。")
            assert plan.workspace is not None
            worktrees = self._management.worktrees
            if plan.workspace.mode != "project":
                if worktrees is None:
                    raise WorktreeError("unavailable", "worktree 服务暂不可用，本次未启动。")
                # An unavailable selected checkout must reject before any Feishu send.
                workspace = await worktrees.validate_policy(
                    plan.workspace, project_alias=project.alias,
                    expected_project_revision=project.revision,
                )
                if workspace is not None:
                    store.set_run(run.id, worktree_id=workspace.id)
            skill_names = parse_skill_references(plan.instructions)
            self._bindings.require_project_not_deleting(run.project_alias)
            if not self._bindings.get_project(run.project_alias).enabled:
                raise ProjectConflict("Project 已停用，本次未启动。")
            local_due = datetime.fromtimestamp(run.due_at, ZoneInfo(plan.schedule.timezone)).isoformat(timespec="minutes")
            trigger_label = "手动触发" if run.trigger_source == "manual" else "自动触发"
            # Plain text prevents instructions from manufacturing mentions in the card.
            description = constants.FEISHU_AT_TAG_START.sub("‹", plan.instructions)
            hint = (
                "成功完成且结果送达后自动归档；运行中可用 /stop 停止。"
                if plan.auto_archive else
                "运行中可用 /stop 停止，结束后可在本话题继续交流。"
            )
            root_content = OutboundCard(card={
                "schema": "2.0", "config": {"width_mode": "default", "update_multi": True},
                "header": {"title": {"tag": "plain_text", "content": "定时任务 · " + plan.name}, "template": "blue"},
                "body": {"elements": [
                    {"tag": "div", "text": {"tag": "plain_text", "content":
                        f"{trigger_label} · {local_due} · {plan.schedule.timezone}"}},
                    *schedule_trigger_instructions(description),
                    {"tag": "div", "text": {"tag": "plain_text", "content": hint}},
                ]},
            })
            publishing_started = True
            root = await send_topic_message(self._channel, run.chat_id, root_content, SendOpts(receive_id_type="chat_id", uuid=run.root_uuid))
            store.set_run(run.id, root_message_id=root.message_id)
            def require_seed_project() -> None:
                self._bindings.require_project_not_deleting(run.project_alias)
                if not self._bindings.get_project(run.project_alias).enabled:
                    raise ProjectConflict("Project 已停用，本次未启动。")
            origin_message = await complete_fresh_topic(
                self._channel, root,
                seed_content=f"定时任务{trigger_label}。执行内容见话题根消息；可在本话题继续交流。",
                seed_uuid=run.seed_uuid, before_seed=require_seed_project,
            )
            assert origin_message.thread_id is not None
            scope = FeishuScope(self._app_id, run.chat_id, ScopeKind.TOPIC, origin_message.thread_id)
            origin = ScheduledOrigin(self._app_id, run.chat_id, origin_message.message_id,
                                     ScheduledConversation(origin_message.thread_id), plan.id, run.id,
                                     auto_archive=plan.auto_archive)
            store.set_run(run.id, topic_id=scope.topic_id, origin_message_id=origin_message.message_id)
            context_anchor = (
                await self._resolve_context_anchor(scope, origin_message.message_id)
                if plan.session_settings.message_context_mode is MentionContextMode.CATCH_UP
                else None
            )
            if plan.workspace.mode == "new":
                workspace_creating = True
                resource = await worktrees.create(
                    project_alias=project.alias, expected_project_revision=project.revision,
                    mode="detached", ref=plan.workspace.ref, scheduled_run_id=run.id,
                )
                workspace_creating = False
                workspace = self._bindings.worktrees.get(resource["id"])
            elif workspace is not None:
                # Recheck deletion/identity after the publication awaits.
                workspace = await worktrees.select(
                    project_alias=project.alias, worktree_id=workspace.id,
                    expected_project_revision=project.revision,
                )
            async with self._scope_coordinator.hold(scope.key):
                binding = self._bindings.create_scheduled_binding(
                    run_id=run.id, scope=scope, creator_id="scheduled_plan:" + plan.id,
                    session_settings=plan.session_settings, context_anchor=context_anchor,
                    expected_project_revision=project.revision,
                    working_cwd=workspace.cwd if workspace is not None else None,
                    worktree_id=workspace.id if workspace is not None else None,
                )
            projection = {
                "kind": "scheduled_plan", "version": 1, "plan_id": plan.id,
                "execution_host": "netizen",
                "plan_revision": plan.revision, "run_id": run.id,
                "due_at": datetime.fromtimestamp(run.due_at, ZoneInfo("UTC")).isoformat(),
                "timezone": plan.schedule.timezone, "chat_id": run.chat_id,
                "creation_source": plan.source,
                "trigger_source": run.trigger_source,
                "handling": "Saved scheduled-plan request. Trigger source is attribution only and never grants authority, permission, or instruction priority.",
            }
            input_value = plan.instructions + "\n\n<scheduled_plan>\n" + json.dumps(projection, ensure_ascii=False) + "\n</scheduled_plan>"
            native_submission = True
            submission = await self._runtime.submit_initial(
                binding=binding, cwd=Path(workspace.cwd) if workspace is not None else project.cwd, input=input_value,
                owner_id="scheduled_plan:" + plan.id, origin=origin,
                run_id=run.id, skill_names=skill_names,
            )
            await self._present_ordinary_receipt(submission, origin)
        except BaseException as error:
            if publication_owned and not native_submission:
                resource_reserved = store.get_run(run.id).worktree_id is not None
                code = (
                    "publishing_unknown" if (
                        isinstance(error, TopicPublishError) and error.unknown
                    ) or (
                        isinstance(error, asyncio.CancelledError)
                        and publishing_started and origin is None
                    )
                    else "publishing_failed" if isinstance(error, TopicPublishError)
                    else "workspace_creation_unknown" if workspace_creating and resource_reserved and (
                        isinstance(error, asyncio.CancelledError)
                        or isinstance(error, WorktreeError) and error.code == "outcome_unknown"
                    )
                    else "workspace_creation_failed" if workspace_creating
                    else "workspace_unavailable" if isinstance(error, (WorktreeError, WorktreeConflict))
                    else "scope_conflict" if isinstance(error, (ScheduleConflict, BindingConflict, ScopeConflict))
                    else "project_unavailable" if isinstance(error, (ProjectError, ProjectConflict, ProjectNotFound))
                    else "workspace_handoff_failed" if workspace is not None
                    else "group_not_enabled" if isinstance(error, GroupNotEnabled)
                    else "dispatch_rejected"
                )
                # No native call occurred. The publication facts remain visible;
                # its uncertainty does not invent a possibly running model Turn.
                store.release(run.id, error_code=code)
            logger.warning("scheduled occurrence dispatch did not finish", extra={"run_id": run.id, "error_type": type(error).__name__})
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            if origin is not None:
                try:
                    async with asyncio.timeout(5):
                        receipt = store.get_run(run.id)
                        resource_note = (f" 关联 worktree：{receipt.worktree_id}，请在 Admin 检查；不会自动删除或重建。"
                                         if receipt.worktree_id else "")
                        await self._reply(origin, f"定时任务未完成启动：{str(error)[:400]}。请通过 /cron 查看本次记录。" + resource_note)
                except Exception:
                    logger.warning("scheduled start notice was not delivered", extra={"run_id": run.id})

    async def _dispatch_binding_input(self, claim: Claim, group_rule: GroupAccessRule) -> None:
        """Give a saved input a real anchor and use ordinary message consumption."""
        plan, run = claim.plan, claim.run
        store = self._bindings.schedules
        origin: ScheduledBindingOrigin | None = None
        try:
            if run.app_id != self._app_id or plan.app_id != self._app_id:
                raise ScheduleError("计划所属飞书应用不一致。")
            store.require_binding_target(plan)
            binding = self._bindings.get(plan.target_binding_id)
            scope_record = self._bindings.get_scope(binding.scope_key)
            scope = FeishuScope(scope_record.app_id, scope_record.chat_id,
                                scope_record.kind, scope_record.topic_id)
            # Capture before publishing/reading: these awaits must not turn an
            # idle input into a later steer or use a replaced context boundary.
            admission = None
            admission_error: Exception | None = None
            try:
                admission = await self._runtime.capture_submission_admission(binding.id)
            except Exception as error:
                # Still publish a real anchor for ordinary visible rejection.
                admission_error = error
            chat_kind = _public_chat_kind(await self._channel.get_chat_info(scope.chat_id))
            if chat_kind not in {"p2p", "group"}:
                raise ScheduleError("无法确认目标飞书会话，本次未执行。")
            if not group_rule.allows(scope.chat_id, chat_kind):
                raise GroupNotEnabled()
            reply_to = None
            if scope.kind is ScopeKind.TOPIC:
                if self._message_history is None:
                    raise MessageHistoryUnavailable("话题消息读取能力不可用，本次未执行。")
                anchor = await self._message_history.resolve_topic_reply_anchor(scope)
                reply_to = anchor.message_id
            store.require_binding_target(plan)
            store.begin_publication(run.id)
            trigger = "手动触发" if run.trigger_source == "manual" else "定时触发"
            local_due = datetime.fromtimestamp(run.due_at, ZoneInfo(plan.schedule.timezone)).isoformat(timespec="minutes")
            # A plain card preserves literal instructions without creating @s.
            anchor_content = OutboundCard(card={
                "schema": "2.0", "config": {"width_mode": "default"},
                "header": {"title": {"tag": "plain_text", "content": "定时任务 · " + plan.name}},
                "body": {"elements": [
                    {"tag": "div", "text": {"tag": "plain_text", "content": f"{trigger} · {local_due}"}},
                    *schedule_trigger_instructions(plan.instructions),
                ]},
            })
            sent = await send_topic_message(self._channel, scope.chat_id, anchor_content, SendOpts(
                receive_id_type="chat_id", reply_to=reply_to,
                reply_in_thread=scope.kind is ScopeKind.TOPIC,
                reply_target_gone="fail", uuid=run.root_uuid,
            ))
            if sent.thread_id != scope.topic_id or (
                reply_to is not None and (not sent.root_id or sent.parent_id != reply_to)
            ):
                raise TopicPublishError("触发消息不属于原会话，本次未执行。", unknown=True)
            origin = ScheduledBindingOrigin(
                self._app_id, scope.chat_id, sent.message_id,
                ScheduledBindingConversation(scope.chat_id, chat_kind, scope.topic_id),
                plan.id, run.id, binding.id,
            )
            store.set_run(run.id, origin_message_id=sent.message_id,
                          **({"topic_id": scope.topic_id} if scope.topic_id else {}))
            if admission_error is not None:
                raise admission_error
            projection = ScheduledInputProjection(
                message_id=sent.message_id, request_text=plan.instructions,
                plan={"plan_id": plan.id, "plan_revision": plan.revision,
                      "run_id": run.id, "trigger_source": run.trigger_source,
                      "creation_source": plan.source, "chat_id": scope.chat_id,
                      "due_at": datetime.fromtimestamp(run.due_at, ZoneInfo("UTC")).isoformat(),
                      "timezone": plan.schedule.timezone},
            )
            await self._consume_prompt(
                binding=binding, scope=scope, message=origin, current=projection,
                owner_id="scheduled_plan:" + plan.id,
                skill_names=parse_skill_references(plan.instructions),
                scheduled_run_id=run.id, admission=admission,
            )
        except BaseException as error:
            current_run = store.get_run(run.id)
            if current_run.barrier != "released":
                rejected = isinstance(error, (
                    RuntimeClosed, SteerRace, ThreadStopping, ThreadCompacting,
                    ThreadOccupied, ThreadGoalActive, ThreadLifecycleError,
                    ModelCatalogError, SkillCatalogError, SkillReferenceError,
                    GoalNotFound, GoalNotMaterialized, WorktreeConflict,
                ))
                store.release(run.id, error_code=(
                    "input_unknown" if current_run.phase == "starting_turn" and not rejected
                    else "publishing_unknown" if (
                        isinstance(error, TopicPublishError) and error.unknown
                    ) or (
                        isinstance(error, asyncio.CancelledError)
                        and current_run.phase == "publishing_topic"
                        and current_run.origin_message_id is None
                    )
                    else "group_not_enabled" if isinstance(error, GroupNotEnabled)
                    else "input_rejected"
                ))
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            if origin is not None:
                await self._report_input_error(origin, error)
            else:
                logger.warning("scheduled input has no confirmed anchor", extra={"run_id": run.id, "error_type": type(error).__name__})

    @staticmethod
    def _require_schedule_result(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("ok") is not True:
            raise ScheduleError(str(result.get("error", {}).get("message", "定时任务管理失败，请刷新后重试。")))
        return result

    @staticmethod
    def _schedule_default_chat(
        scope: FeishuScope, request: dict[str, Any],
    ) -> dict[str, Any]:
        result = dict(request)
        for field in ("chat_id", "project"):
            if field in result and (
                not isinstance(result[field], str) or not result[field].strip()
            ):
                raise ScheduleError(f"{field} 必须是非空字符串。")
        if "all" in result and type(result["all"]) is not bool:
            raise ScheduleError("all 必须是布尔值。")
        if result.get("target_kind") != "binding" and not result.get("all") and not result.get("chat_id"):
            result["chat_id"] = scope.chat_id
        return result

    async def _schedule_manager_card(
        self, scope: FeishuScope, *, navigation: dict[str, Any] | None = None,
        selected: dict[str, Any] | None = None, notice: str | None = None,
        show_runs: bool = False, runs_cursor: str | None = None,
    ) -> OutboundCard:
        """Rebuild the current management view from service facts and card navigation."""
        navigation = schedule_navigation(navigation)
        service = self._management.schedules
        if selected is not None:
            if selected.get("deleted"):
                navigation.pop("plan_id", None)
                selected = None
                notice = "该请求已处理；计划之后已被删除，不会重新创建。"
            else:
                navigation["plan_id"] = selected["plan"]["id"]
        if selected is None and navigation.get("plan_id"):
            result = await service.manage(
                {"mode": "view", "plan_id": navigation["plan_id"]}, scope_key=scope.key, source="card",
            )
            if result.get("error", {}).get("code") == "not_found":
                navigation.pop("plan_id", None)
                notice = (notice + " " if notice else "") + "所选计划已删除，请选择其他任务。"
            else:
                selected = self._require_schedule_result(result)
        filters = self._schedule_default_chat(scope, schedule_query(navigation))
        if selected is not None:
            plan = selected["plan"]
            if ((filters.get("chat_id") is not None and plan["chat_id"] != filters["chat_id"])
                    or ("enabled" in filters and plan["enabled"] != filters["enabled"])
                    or ("ended" in filters and plan["lifecycle"]["ended"] != filters["ended"])):
                navigation.pop("plan_id", None)
                selected = None
                notice = (notice + " " if notice else "") + "所选计划不符合当前筛选，已清除选择。"
        listed = self._require_schedule_result(await service.manage(
            {"mode": "list", "limit": 50, **filters}, scope_key=scope.key, source="card",
        ))
        runs = None
        if selected is not None and show_runs:
            runs = self._require_schedule_result(await service.manage(
                {"mode": "runs", "plan_id": selected["plan"]["id"], "limit": 5,
                 **({"cursor": runs_cursor} if runs_cursor else {})}, scope_key=scope.key, source="card",
            ))
        binding = self._bindings.active_binding(scope.key)
        return schedule_manager_card(scope, listed, navigation=navigation, selected=selected,
                                     runs=runs, notice=notice,
                                     current_binding_id=binding.id if binding else None)

    async def _handle_schedule_card_action(self, event: Any) -> None:
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        action = getattr(event, "action", None)
        scope: FeishuScope | None = None
        try:
            sender_id = getattr(getattr(event, "operator", None), "open_id", None)
            if getattr(action, "tag", None) != "button" or not message_id or not chat_id or not sender_id:
                raise CardActionError("定时任务卡片回调缺少消息或操作者。")
            fetched = await self._channel.fetch_message(message_id)
            topic_id = fetched_card_topic_id(callback_chat_id=chat_id, fetched_message=fetched)
            chat_kind = None if topic_id is not None else _public_chat_kind(await self._channel.get_chat_info(chat_id))
            scope = scope_from_fetched_card(app_id=self._app_id, callback_chat_id=chat_id, fetched_message=fetched, chat_type=chat_kind)
            decoded = decode_schedule_action(scope=scope, value=getattr(action, "value", None), form=getattr(action, "form_value", None))
            payload = decoded.payload
            navigation = decoded.navigation
            service = self._management.schedules
            if decoded.action in {"new", "edit"}:
                plan = None
                if decoded.action == "edit":
                    result = self._require_schedule_result(await service.manage({"mode": "view", "plan_id": payload.get("plan_id")}, scope_key=scope.key, source="card"))
                    plan = result["plan"]
                    if plan["revision"] != payload.get("expected_revision"):
                        raise ScheduleError("计划已修改，请刷新详情后重新编辑。")
                binding = self._bindings.active_binding(scope.key)
                target_binding_id = payload.get("target_binding_id") if decoded.action == "new" else None
                target = (plan or {}).get("chat_id") or scope.chat_id
                fixed_target_id = (plan or {}).get("target_binding_id") or target_binding_id
                fixed_display: dict[str, Any] = {}
                if fixed_target_id is not None:
                    fixed_binding = self._bindings.get(fixed_target_id)
                    fixed_scope = self._bindings.get_scope(fixed_binding.scope_key)
                    if fixed_scope.app_id != self._app_id:
                        raise ScheduleError("目标 Agent 会话不属于当前飞书应用。")
                    target = fixed_scope.chat_id
                    fixed_display = {
                        "target_chat_id": target, "target_topic_id": fixed_scope.topic_id,
                        "target_label": fixed_binding.id,
                        "effective_workspace": {
                            "working_cwd": fixed_binding.working_cwd,
                            "worktree_id": fixed_binding.worktree_id,
                            "native_cwd_unresolved": fixed_binding.native_thread_id is not None,
                        },
                    }
                    try:
                        labels = await self._management.resolve_chat_labels(
                            (target,), deadline=asyncio.get_running_loop().time() + 2,
                        )
                        fixed_display["target_chat_label"] = labels[target].display_name
                    except Exception:
                        # Display names never change the exact readonly target.
                        pass
                projects = self._projects.list(enabled_only=True)
                project_alias = None if fixed_target_id is not None else (
                    (plan or {}).get("project_alias") or (binding.project_alias if binding else None)
                    or (projects[0].alias if projects else None)
                )
                options, catalog = await service.form_options(scope_key=scope.key, chat_id=target, project=project_alias)
                self._require_schedule_result(options)
                chat_info_error = options.get("chat_info_error")
                if fixed_target_id is not None and chat_info_error:
                    raise ScheduleError(chat_info_error["message"])
                catalog_error = options.get("model_catalog_error")
                chat_options = {} if fixed_target_id is not None or target == scope.chat_id else await self._schedule_chat_options(
                    target_chat_id=target if options["context_mode_available"] is True else None,
                )
                if chat_info_error:
                    chat_options["chat_directory_error"] = (
                        "目标飞书聊天暂不可读取，聊天类型尚未确认；已保留原 ID 和配置，提交时将重新校验。"
                    )
                workspace_error = options.get("workspace_options_error")
                form_options = dict(projects=projects,
                    default_timezone=service.default_timezone, plan=plan,
                    target_binding_id=target_binding_id,
                    initial_project=binding.project_alias if binding else None,
                    session_settings=options["session_settings"], catalog=catalog,
                    catalog_error=catalog_error.get("message") if catalog_error else None,
                    allow_context_mode=options["context_mode_available"] is not False,
                    workspace_options=options.get("workspace_options"),
                    workspace_error=workspace_error.get("message") if workspace_error else None,
                    navigation=navigation,
                    **fixed_display)
                card = schedule_form_card(scope, **form_options, **chat_options)
            elif decoded.action in {"search_chats", "page_chats", "load_workspace"}:
                # The draft is display state, not a create/update request.
                # Search and failed saves share its renderer, not retry logic.
                draft = payload["draft"]
                workspace_options: dict[str, Any] | None
                (catalog, catalog_error), (workspace_options, workspace_error) = await asyncio.gather(
                    service.model_catalog_options(),
                    service.workspace_catalog_options(
                        project=draft.meta["project"],
                        expected_project_revision=(None if decoded.action == "load_workspace"
                                                   else draft.meta.get("project_revision")),
                    ),
                )
                projects = self._projects.list(enabled_only=True)
                catalog_notice = catalog_error["message"] if catalog_error else None
                workspace_notice = workspace_error["message"] if workspace_error else None
                if decoded.action == "load_workspace":
                    draft = refresh_schedule_workspace(draft, workspace_options)
                try:
                    snapshot = (await self._card_chat_snapshot(query=payload["query"])
                                if decoded.action == "search_chats" else payload.get("snapshot"))
                    card = schedule_draft_card(draft, projects=projects,
                        catalog=catalog, catalog_error=catalog_notice,
                        workspace_options=workspace_options, workspace_error=workspace_notice,
                        chat_snapshot=snapshot, chat_page=payload.get("page", 0))
                except CardActionError as error:
                    # Incomplete/oversized results are never presented as a
                    # complete snapshot. The editable business draft survives.
                    card = schedule_draft_card(draft, projects=projects,
                        catalog=catalog, catalog_error=catalog_notice,
                        workspace_options=workspace_options, workspace_error=workspace_notice,
                        chat_directory_error=str(error))
            elif decoded.action == "save":
                request = {**payload, "mode": "update" if payload.get("plan_id") else "create", "request_id": decoded.request_id}
                result = self._require_schedule_result(await service.manage(request, scope_key=scope.key, source="card"))
                saved_target = result.get("plan", {}).get("chat_id")
                notice = "计划已保存。" + (f"目标飞书聊天：{saved_target}。" if saved_target else "")
                card = await self._schedule_manager_card(scope, navigation=navigation, selected=result, notice=notice)
            elif decoded.action == "enabled":
                result = self._require_schedule_result(await service.manage({"mode": "update", **payload, "request_id": decoded.request_id}, scope_key=scope.key, source="card"))
                card = await self._schedule_manager_card(scope, navigation=navigation, selected=result,
                    notice="计划已启用。" if payload.get("enabled") else "计划已暂停。")
            elif decoded.action == "run_now":
                receipt = self._require_schedule_result(await service.manage(
                    {"mode": "run_now", **payload, "request_id": decoded.request_id}, scope_key=scope.key, source="card",
                ))
                notice = "立即运行请求已受理。" + ("同一请求未重复触发。" if receipt.get("replayed") else "")
                card = await self._schedule_manager_card(scope,
                    navigation={**navigation, "plan_id": payload["plan_id"]}, show_runs=True,
                    notice=notice + "输入将按计划的执行目标交接；原定时安排不变。")
            elif decoded.action == "delete":
                deleted = self._require_schedule_result(await service.manage({"mode": "delete", **payload, "request_id": decoded.request_id}, scope_key=scope.key, source="card"))
                notice = "计划已删除，已有 Agent 会话保留。" + ("本次已触发的交接仍可能继续。" if deleted.get("inflight") else "")
                navigation = {key: value for key, value in navigation.items() if key != "plan_id"}
                card = await self._schedule_manager_card(scope, navigation=navigation, notice=notice)
            elif decoded.action == "runs":
                card = await self._schedule_manager_card(scope, navigation={**navigation, "plan_id": payload["plan_id"]},
                    show_runs=True, runs_cursor=payload.get("cursor"))
            elif decoded.action == "view":
                card = await self._schedule_manager_card(scope, navigation={**navigation, "plan_id": payload["plan_id"]})
            else:
                card = await self._schedule_manager_card(scope, navigation=navigation)
            if not await self._safe_update_card(message_id, card):
                raise ScheduleError("卡片刷新失败；刚才的操作可能已生效。可原样重试确认，同一请求不会重复创建计划或触发执行。")
        except (CardActionError, ScheduleError, ProjectError) as error:
            await self._recover_schedule_card_action(event, scope=scope, notice=str(error))
        except Exception as error:
            logger.error("scheduled card operation failed type=%s", type(error).__name__)
            await self._recover_schedule_card_action(event, scope=scope,
                notice="定时任务操作未确认。可原样重试确认，同一请求不会重复创建计划或触发执行；修改前请先确认上次保存结果。")

    async def _schedule_chat_options(
        self, *, target_chat_id: str | None = None,
    ) -> dict[str, Any]:
        chats = {}
        if target_chat_id:
            selected = await self._card_display_group(target_chat_id)
            if selected is not None:
                chats[selected.chat_id] = selected
        return dict(chats=tuple(chats.values()),
            chat_avatar_keys=await self._chat_avatars.prepare(tuple(chats.values())))

    async def _recover_schedule_card_action(self, event: Any, *, scope: FeishuScope | None, notice: str) -> None:
        """Renew only the UI nonce; never turn display metadata into authority."""
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        action = getattr(event, "action", None)
        try:
            if not message_id or not chat_id or not getattr(getattr(event, "operator", None), "open_id", None):
                raise CardActionError("卡片回调缺少消息或操作者。")
            catalog = None
            catalog_error = None
            if scope is not None and getattr(action, "form_value", None):
                catalog, catalog_error = await self._management.schedules.model_catalog_options()
            display_scope, card = schedule_retry_card(app_id=self._app_id, chat_id=chat_id,
                value=getattr(action, "value", None), form=getattr(action, "form_value", None),
                notice=notice, projects=self._projects.list(enabled_only=True), scope=scope,
                catalog=catalog, catalog_error=catalog_error["message"] if catalog_error else None,
                chat_directory_error="已保留填写内容与已选群；可查找更多群聊，或填写聊天 ID。")
            if await self._safe_update_card(message_id, card):
                return
            scope = scope or display_scope
        except Exception:
            logger.exception("failed to restore scheduled card for retry")
        # One bounded feedback attempt after the normal bounded card update.
        # Do not replace the original form with a dead error card, allocate a
        # new business request, or loop while transport remains unavailable.
        if scope is not None and message_id:
            target = _CardReplyTarget(id=message_id, message_id=message_id, chat_id=scope.chat_id,
                conversation=_CardReplyConversation(thread_id=scope.topic_id))
            try:
                async with asyncio.timeout(5):
                    await self._reply(target, notice + " 输入仍保留，但重试卡片未能恢复；请稍后通过 /cron 查看最新状态。")
            except Exception:
                logger.exception("failed to send scheduled retry feedback")
