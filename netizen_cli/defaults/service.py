"""Shared default-rule management and first-match resolution."""

from __future__ import annotations

import asyncio
from typing import Any

from ..bindings import BindingQueryBusy, BindingQueryClosed, BindingQueryTimeout, BindingStore
from ..channel.messages import public_chat_kind
from ..chat_targets import ChatTargetError, ChatTargetValidator
from ..domain import MentionContextMode
from ..model_settings import ModelCatalogError, STANDARD_SERVICE_TIER_ID
from ..projects import Project, ProjectError, ProjectRegistry
from ..session_settings import SessionSettings, SessionSettingsError
from .models import DefaultConfigurationError, DefaultRule
from .store import MAX_GROUP_RULES


_FIELDS = {
    "view": {"chat_id"},
    "list": {"kind", "offset", "limit"},
    "save": {"kind", "chat_id", "keyword", "id", "expected_revision", "project", "expected_project_revision", "session_settings"},
    "delete": {"id", "expected_revision", "chat_id", "kind"},
    "reorder": {"rule_ids", "order_revision"},
    "options": {"chat_id"},
}


class SessionDefaultsService:
    def __init__(
        self, *, bindings: BindingStore, projects: ProjectRegistry,
        runtime: Any, app_id: str, chat_info: Any = None, blocking_io: Any = None,
        chat_target_validator: ChatTargetValidator | None = None,
    ) -> None:
        self.app_id = app_id
        self._store = bindings.defaults
        self._projects = projects
        self._runtime = runtime
        self._chat_info = chat_info
        self._chat_target_validator = chat_target_validator
        self._blocking_io = blocking_io

    async def _chat(self, chat_id: str) -> tuple[str, Any]:
        if self._chat_info is None:
            raise DefaultConfigurationError("聊天信息服务暂不可用。", code="chat_unavailable")
        try:
            async with asyncio.timeout(5):
                info = await self._chat_info.get_chat_info(chat_id)
        except Exception as error:
            raise DefaultConfigurationError("无法读取聊天信息，请检查 chat_id 和机器人是否可访问该聊天。", code="chat_unavailable") from error
        kind = public_chat_kind(info)
        if kind is None:
            raise DefaultConfigurationError("无法确定聊天类型，请稍后重试。", code="chat_kind_unknown")
        return kind, info

    @staticmethod
    def _match(rules: tuple[DefaultRule, ...], info: Any) -> DefaultRule | None:
        name = getattr(info, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise DefaultConfigurationError("无法读取群名称，不能判断默认配置匹配结果。", code="chat_name_unavailable")
        folded = name.casefold()
        return next((rule for rule in rules if rule.keyword is not None and rule.keyword.casefold() in folded), None)

    async def resolve(self, chat_id: str, chat_kind: str | None = None) -> DefaultRule | None:
        """Select one saved rule, without checking its Project or native model."""
        chat_id = _text(chat_id, "chat_id")
        exact = self._store.exact(self.app_id, chat_id)
        if exact is not None:
            return exact
        if chat_kind == "p2p":
            return None
        rules = self._store.group_rules(self.app_id)
        if not rules:
            return None
        kind, info = await self._chat(chat_id)
        return self._match(rules, info) if kind == "group" else None

    async def validate(self, rule: DefaultRule, *, expected_project_revision: int | None = None) -> Project:
        """Validate at use time; no Project revision or effective config is saved."""
        if rule.app_id != self.app_id:
            raise DefaultConfigurationError("当前应用下没有这个默认配置。", code="not_found")
        try:
            if self._blocking_io is None:
                project = self._projects.resolve_for_new(rule.project, expected_revision=expected_project_revision)
            else:
                project = await self._blocking_io.submit(
                    self._projects.resolve_for_new, rule.project,
                    expected_revision=expected_project_revision,
                    deadline=asyncio.get_running_loop().time() + 5,
                )
        except ProjectError as error:
            raise DefaultConfigurationError(f"默认配置的 Project {rule.project} 不可用：{error}", code="project_unavailable") from error
        except Exception as error:
            raise DefaultConfigurationError("暂时无法校验默认配置的 Project，请稍后重试。", code="project_unavailable") from error
        if rule.session_settings.turn_settings is None:
            return project
        try:
            async with asyncio.timeout(5):
                catalog = await self._runtime.model_catalog()
                rule.session_settings.validate_catalog(catalog)
        except ModelCatalogError as error:
            raise DefaultConfigurationError(f"默认配置的模型、思考强度或速度不可用：{error}", code="invalid_model_settings") from error
        except Exception as error:
            raise DefaultConfigurationError("暂时无法校验默认配置的模型目录，请稍后重试。", code="model_catalog_unavailable") from error
        return project

    async def manage(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise DefaultConfigurationError("默认配置管理参数必须是对象。")
        mode = request.get("mode")
        if not isinstance(mode, str) or mode not in _FIELDS or set(request) - _FIELDS[mode] - {"mode"}:
            raise DefaultConfigurationError("默认配置管理操作或参数无效。")
        if mode == "view":
            return await self._view(_text(request.get("chat_id"), "chat_id"))
        if mode == "options":
            chat = request.get("chat_id")
            return await self._options(_text(chat, "chat_id") if chat is not None else None)
        if mode == "list":
            kind = _kind(request.get("kind"))
            offset = _integer(request.get("offset", 0), "offset", minimum=0, maximum=2**31 - 1)
            limit = _integer(request.get("limit", 50), "limit", maximum=MAX_GROUP_RULES if kind == "group_name" else 50)
            try:
                async with asyncio.timeout(3):
                    return await self._store.list(self.app_id, kind, offset=offset, limit=limit)
            except (BindingQueryBusy, BindingQueryClosed, BindingQueryTimeout, TimeoutError) as error:
                raise DefaultConfigurationError("默认配置列表暂不可用，请稍后刷新。", code="unavailable") from error
        if mode == "save":
            return {"rule": (await self._save(request)).to_dict()}
        if mode == "delete":
            rule_id = _text(request.get("id"), "id")
            if "chat_id" in request or "kind" in request:
                rule = self._store.get(self.app_id, rule_id)
                if "kind" in request and rule.kind != _kind(request["kind"]):
                    raise DefaultConfigurationError("默认配置类型不匹配。", code="not_found")
                if "chat_id" in request and (rule.kind != "chat" or rule.chat_id != _text(request["chat_id"], "chat_id")):
                    raise DefaultConfigurationError("这份默认配置不属于当前聊天。", code="not_found")
            self._store.delete(self.app_id, rule_id, _integer(request.get("expected_revision"), "expected_revision"))
            return {"deleted": rule_id}
        ids = request.get("rule_ids")
        if not isinstance(ids, list) or len(ids) > MAX_GROUP_RULES or not all(isinstance(item, str) and item for item in ids):
            raise DefaultConfigurationError("rule_ids 必须为全部群名规则 ID 的有序数组。")
        revision = self._store.reorder(self.app_id, ids, _integer(request.get("order_revision"), "order_revision"))
        return {"order_revision": revision}

    async def _view(self, chat_id: str) -> dict[str, Any]:
        exact = self._store.exact(self.app_id, chat_id)
        kind, info = await self._chat(chat_id)
        effective = exact
        error = None
        if effective is None and kind == "group":
            rules = self._store.group_rules(self.app_id)
            if rules:
                try:
                    effective = self._match(rules, info)
                except DefaultConfigurationError as failure:
                    error = str(failure)
        return {"exact": exact.to_dict() if exact else None,
                "effective": effective.to_dict() if effective else None,
                "chat_kind": kind, "match_error": error}

    async def _save(self, request: dict[str, Any]) -> DefaultRule:
        kind = _kind(request.get("kind"))
        chat_id = _text(request.get("chat_id"), "chat_id") if kind == "chat" else None
        keyword = _text(request.get("keyword"), "keyword", maximum=200) if kind == "group_name" else None
        if (kind == "chat" and request.get("keyword") is not None) or (kind == "group_name" and request.get("chat_id") is not None):
            raise DefaultConfigurationError("精确聊天配置和群名规则的条件不能混用。")
        if "expected_revision" not in request:
            raise DefaultConfigurationError("新增配置需明确 expected_revision 为 null；修改需当前 revision。")
        expected = request["expected_revision"]
        if expected is not None:
            expected = _integer(expected, "expected_revision")
        rule_id = _text(request["id"], "id") if request.get("id") is not None else None
        try:
            settings = SessionSettings.from_dict(request.get("session_settings"))
        except SessionSettingsError as error:
            raise DefaultConfigurationError(str(error)) from error
        project = _text(request.get("project"), "project", maximum=64)
        if chat_id is not None:
            if self._chat_target_validator is None:
                raise DefaultConfigurationError("飞书聊天校验暂不可用，请稍后重试。", code="chat_query_unavailable")
            try:
                chat_kind = (await self._chat_target_validator(chat_id)).chat_kind
            except ChatTargetError as error:
                raise DefaultConfigurationError(str(error), code=error.code) from error
            if chat_kind == "p2p" and settings.message_context_mode is MentionContextMode.CATCH_UP:
                raise DefaultConfigurationError("单聊默认配置只支持 current-only。")
        candidate = DefaultRule(rule_id or "", self.app_id, kind, chat_id, keyword, project, settings, expected or 1, None)
        project_revision = request.get("expected_project_revision")
        if project_revision is not None:
            project_revision = _integer(project_revision, "expected_project_revision")
        checked_project = await self.validate(candidate, expected_project_revision=project_revision)
        return self._store.save(
            app_id=self.app_id, kind=kind, chat_id=chat_id, keyword=keyword,
            project=project, session_settings=settings, rule_id=rule_id, expected_revision=expected,
            checked_project_revision=checked_project.revision,
        )

    async def _options(self, chat_id: str | None) -> dict[str, Any]:
        kind = (await self._chat(chat_id))[0] if chat_id is not None else "group"
        catalog = None
        error = None
        try:
            async with asyncio.timeout(5):
                catalog = await self._runtime.model_catalog()
        except Exception:
            error = "Codex 模型目录暂不可用；可以选择继承 Codex，稍后再选择模型。"
        return {"models": _models(catalog), "session_settings": SessionSettings.new_defaults(catalog).to_dict(),
                "context_mode_available": kind != "p2p", "model_catalog_error": error}


def _kind(value: Any) -> str:
    if not isinstance(value, str) or value not in {"chat", "group_name"}:
        raise DefaultConfigurationError("kind 必须为 chat 或 group_name。")
    return value


def _text(value: Any, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise DefaultConfigurationError(f"{field} 必须是最多 {maximum} 字符的非空文本。")
    return value.strip()


def _integer(value: Any, field: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise DefaultConfigurationError(f"{field} 超出允许范围。")
    return value


def _models(catalog: Any) -> list[dict[str, Any]]:
    if catalog is None:
        return []
    return [{
        "id": model.id, "model": model.model, "display_name": model.display_name,
        "description": model.description, "is_default": model.is_default,
        "default_effort_id": model.default_effort_id, "default_service_tier_id": model.default_service_tier_id,
        "efforts": [{"id": item.id, "description": item.description} for item in model.efforts],
        "service_tiers": [{"id": STANDARD_SERVICE_TIER_ID, "name": "Standard", "description": "Codex 标准服务层"},
                          *[{"id": item.id, "name": item.name, "description": item.description}
                            for item in model.service_tiers if item.id != STANDARD_SERVICE_TIER_ID]],
    } for model in catalog.models]
