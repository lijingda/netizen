"use strict";

const state = {
  tab: "projects",
  projects: null,
  sessions: null,
  sides: null,
  updates: null,
  schedules: null,
  defaults: null,
  projectCursor: null,
  sideCursor: null,
  sessionPage: {
    cursor: null,
    nextCursor: null,
    previousCursors: [],
    number: 1,
    query: null,
  },
};

const statusNode = document.querySelector("#status");

function setStatus(message, isError = false) {
  statusNode.textContent = message || "";
  statusNode.classList.toggle("error", isError);
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (response.status === 401) {
    window.location.assign("/login");
    const error = new Error("登录已失效，请重新登录。");
    error.status = 401;
    throw error;
  }
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.message || `请求失败 (${response.status})`);
    error.status = response.status;
    error.code = data.code;
    error.choices = data.choices;
    throw error;
  }
  return data;
}

const updateTerminalPhases = new Set([
  "succeeded", "failed", "rolled_back", "requires_action", "recovery_required", "recovered",
]);
const updatePhaseLabels = {
  accepted: "升级已受理",
  downloading: "正在下载更新",
  preparing: "正在准备并验证新版本",
  installing: "正在安装",
  restarting: "正在重启",
  succeeded: "升级成功",
  failed: "升级失败",
  rolled_back: "升级失败，已回滚",
  requires_action: "需要处理后重试",
  recovery_required: "升级结果未确认，需要修复",
  recovered: "已通过显式维护恢复",
};
const updateCodeMessages = {
  restart_failed: "服务未能确认重启完成，请检查服务日志并按部署说明恢复。",
  download_failed: "下载失败，请检查网络后重新检查更新。",
  installer_invalid: "安装器校验失败，请重新检查更新。",
  preparation_failed: "新版本准备失败，旧版本未切换。请通过官方安装器重试以查看具体原因。",
  configuration_required: "升级需要补全配置或凭据，请按部署文档完成配置后重试。",
  permissions_required: "升级需要处理飞书应用权限，请按部署文档完成授权后重试。",
  activation_failed: "新版本未能完成启动，请查看回滚结果，检查服务日志并用官方安装器恢复。",
  rollback_incomplete: "回滚未完成，请使用官方安装器修复，当前结果不能确认为成功。",
  installer_failed: "安装器未成功完成，请通过官方安装器重试以查看具体原因。",
  worker_interrupted: "维护进程已中断，请按部署说明检查并恢复。",
  lock_busy: "已有维护操作正在执行，请稍后刷新维护状态。",
  operation_invalid: "维护记录无法验证，请按部署说明检查并修复。",
  dispatch_failed: "未能启动维护进程，请检查服务管理器后刷新维护状态。",
  dispatch_unknown: "维护进程的启动结果未确认，请按部署说明检查并修复。",
  worker_lost: "维护进程未能完成，请按部署说明检查并恢复。",
  previous_release_changed: "当前安装版本已变化，请刷新维护状态。",
  profile_failed: "无法读取账户运行环境，请检查登录 Shell 配置后重试。",
  manual_recovery: "显式维护已恢复服务，请以当前版本为准；原操作不标记为成功。",
  service_ready: "当前服务已就绪，可继续维护；原重启操作不标记为成功。",
};
const updatePollLimitMs = 5 * 60 * 1000;
let updatePollTimer = null;
let updatePollStarted = null;
let updatePollDelay = 2000;
let updatePollExpired = false;
let updateSubmitting = false;
let updateExpectedTarget = null;
let updatePriorOperationId = null;
let updateDisconnected = false;
let updateReading = false;

function sameRestartTarget(left, right) {
  return left?.resource === "instance-restart" && typeof left.targetId === "string"
    && left.targetId === right?.installationId;
}

function updateNeedsPolling() {
  const operation = state.updates?.operation;
  return updateExpectedTarget != null
    || (operation != null && !updateTerminalPhases.has(operation.phase));
}

function renderUpdates() {
  const data = state.updates;
  if (!data) return;
  document.querySelector("#update-current").textContent = data.current.version;
  document.querySelector("#update-source").textContent = {
    python: "Python 环境安装",
  }[data.current.source] || "安装来源未知";
  document.querySelector("#update-message").textContent = data.message
    || "程序更新请在对应 Python 环境运行 netizen update。";
  const operation = data.operation;
  const restarting = updateExpectedTarget != null || operation?.kind === "restart";
  const operationName = restarting ? "重启" : "升级";
  let phase = operation
    ? (updatePhaseLabels[operation.phase] || "维护结果未确认").replace("升级", operationName)
    : "尚未执行维护操作";
  let detail = operation
    ? `${restarting ? "保持版本" : "目标版本"} ${operation.target.version}。${
      updateCodeMessages[operation.code] || ""}`
    : "重启开始后，关闭页面不会取消操作。";
  if (operation?.kind === "restart" && operation.phase === "recovered"
      && operation.code === "service_ready") {
    phase = "服务已恢复";
  }
  if (operation?.phase === "succeeded") {
    detail += restarting ? " 服务管理器已完成重启，服务就绪已确认。" : " 安装器已确认升级完成。";
  }
  if (updateExpectedTarget) {
    phase = updateSubmitting ? `正在提交${operationName}` : `${operationName}提交结果尚未确认`;
    detail = "正在查询服务端记录，请勿重复提交重启。";
  }
  if (updateDisconnected) {
    phase = `连接暂时中断，正在查询${operationName}结果`;
    detail = "服务可能正在重启；重新连接后将读取维护记录。";
  }
  if (updatePollExpired) {
    phase = `${operationName}结果尚未确认`;
    detail = "自动查询已停止；请点击“刷新维护状态”手动检查。请勿据此认定操作失败或重复提交。";
  }
  document.querySelector("#update-phase").textContent = phase;
  document.querySelector("#update-detail").textContent = detail;
  document.querySelector("#service-restart").disabled = updateSubmitting || updateReading
    || !data.restartSupported || !data.restartAvailable || !data.actions?.restart
    || updateNeedsPolling() || updatePollExpired || updateDisconnected;
  document.querySelector("#restart-message").textContent = !data.restartSupported
    ? "当前安装不支持从管理页重启，请按部署说明管理服务。"
    : !data.restartAvailable && !updateNeedsPolling()
      ? "当前暂不可重启，请查看维护状态或部署说明。" : "";
}

async function updateApi(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    return await api(path, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

function acceptUpdateStatus(data) {
  state.updates = data;
  if (data.operation?.kind === "restart"
      && sameRestartTarget(updateExpectedTarget, data.operation?.target)
      && data.operation.operationId !== updatePriorOperationId) {
    updateExpectedTarget = null;
  }
  updateDisconnected = false;
  updatePollDelay = 2000;
  renderUpdates();
}

function stopUpdatePolling() {
  clearTimeout(updatePollTimer);
  updatePollTimer = null;
}

function scheduleUpdatePoll() {
  stopUpdatePolling();
  if (state.tab !== "updates" || updatePollExpired || !updateNeedsPolling()) return;
  if (updatePollStarted == null) updatePollStarted = Date.now();
  if (Date.now() - updatePollStarted >= updatePollLimitMs) {
    updatePollExpired = true;
    renderUpdates();
    return;
  }
  updatePollTimer = setTimeout(pollUpdateStatus, updatePollDelay);
}

async function pollUpdateStatus() {
  updatePollTimer = null;
  if (state.tab !== "updates") return;
  if (updateReading) {
    scheduleUpdatePoll();
    return;
  }
  updateReading = true;
  try {
    acceptUpdateStatus(await updateApi("/api/v1/updates"));
  } catch (error) {
    if (error.status === 401) return;
    updateDisconnected = true;
    updatePollDelay = Math.min(updatePollDelay * 2, 15000);
  } finally {
    updateReading = false;
    renderUpdates();
  }
  scheduleUpdatePoll();
}

async function loadUpdates() {
  if (updateReading) return;
  stopUpdatePolling();
  updatePollStarted = Date.now();
  updatePollExpired = false;
  updateReading = true;
  renderUpdates();
  try {
    acceptUpdateStatus(await updateApi("/api/v1/updates"));
  } finally {
    updateReading = false;
    renderUpdates();
    scheduleUpdatePoll();
  }
}

async function restartService() {
  if (updateSubmitting || updateReading || updateNeedsPolling()
      || updatePollExpired || updateDisconnected) return;
  const envelope = state.updates?.actions?.restart;
  if (!envelope || !state.updates.restartSupported || !state.updates.restartAvailable) return;
  if (!window.confirm(
    "确认重启服务？将使用绑定环境中当前安装的程序，不更新软件包。\n\n"
      + `实例：${document.querySelector("#instance-root")?.textContent || "当前实例"}\n\n`
      + "重启会中断正在执行的任务、暂停 Goal，并结束临时 Side 会话。"
      + "重启后不会自动续跑，管理页需要重新登录。",
  )) return;
  updateSubmitting = true;
  updateExpectedTarget = envelope.target;
  updatePriorOperationId = state.updates.operation?.operationId || null;
  updatePollStarted = Date.now();
  state.updates.actions.restart = null;
  renderUpdates();
  let sessionExpired = false;
  try {
    const result = await updateApi("/api/v1/updates/restart", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(envelope)),
    });
    acceptUpdateStatus({ ...state.updates, operation: result.operation });
    const instanceLabel = document.querySelector("#instance-root")?.textContent || "当前实例";
    setStatus(`重启已受理，正在查询服务重启结果。重启后需要重新登录。 实例：${instanceLabel}`);
  } catch (error) {
    if (error.status === 401) {
      sessionExpired = true;
      return;
    }
    if (error.status >= 400 && error.status < 500) {
      updateExpectedTarget = null;
      setStatus(`${error.message} 请刷新维护状态。`, true);
    } else {
      setStatus("重启提交结果未确认，正在查询服务端记录；请勿重复提交。", true);
    }
  } finally {
    updateSubmitting = false;
    renderUpdates();
    if (!sessionExpired) scheduleUpdatePoll();
  }
}

function actionPayload(envelope, extra = {}) {
  return {
    csrfToken: envelope.csrfToken,
    actionToken: envelope.actionToken,
    target: envelope.target,
    ...extra,
  };
}

async function mutate(path, envelope, extra = {}) {
  setStatus("正在执行…");
  try {
    const result = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(envelope, extra)),
    });
    const refreshed = await refresh(state.tab);
    if (refreshed) {
      setStatus(result?.message || "操作完成，已按服务端事实刷新。");
    }
    return result;
  } catch (error) {
    const message = error.message;
    await refresh(state.tab);
    setStatus(message, true);
    throw error;
  }
}

function cell(row, text, className = "") {
  const td = document.createElement("td");
  const node = document.createElement("span");
  node.textContent = text == null || text === "" ? "—" : String(text);
  if (className) node.className = className;
  td.append(node);
  row.append(td);
  return td;
}

function actionButton(label, handler, danger = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = danger ? "action danger" : "action";
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function actionsCell(row) {
  const td = document.createElement("td");
  const wrap = document.createElement("div");
  wrap.className = "actions";
  td.append(wrap);
  row.append(td);
  return wrap;
}

function confirmMaterializedDelete(session) {
  const name = session.nativeTitle || "未命名 Session";
  return window.confirm(
    `确认永久删除这个会话？\n\n会话：${name}\nScope：${session.scopeKey}\nShort ID：${session.shortId}`
    + "\n\n原生 Thread、spawned descendants、Codex App/CLI 历史和本地 Binding 都会永久消失，无法恢复。",
  );
}

function badge(value, active = false, warning = false) {
  const node = document.createElement("span");
  node.className = `badge${active ? " on" : ""}${warning ? " warn" : ""}`;
  node.textContent = value;
  return node;
}

const pendingProjectDeletes = new Set();

function showProjectDeleteResult(message, details = [], isError = false) {
  const panel = document.querySelector("#project-delete-result");
  panel.hidden = false;
  panel.classList.toggle("error", isError);
  document.querySelector("#project-delete-message").textContent = message;
  const remaining = document.querySelector("#project-delete-remaining");
  remaining.replaceChildren();
  for (const detail of details) {
    const item = document.createElement("li");
    item.textContent = detail;
    remaining.append(item);
  }
  remaining.hidden = details.length === 0;
}

function confirmProjectDelete(preview) {
  return window.confirm(
    `删除 Project「${preview.project.alias}」及关联 Sessions？`
    + `\n\n关联 Sessions：${preview.sessionCount}（Lazy ${preview.lazySessionCount}，已创建 Thread ${preview.materializedSessionCount}）`
    + "\n范围包含所有归档会话，不受 Sessions 页面筛选影响。"
    + `\n关联 Side：${preview.sideCount}，将结束并移除。`
    + `\n关联定时计划：${preview.scheduledPlanCount || 0}；正在交接或未决的定时执行：${preview.scheduledRunCount || 0}。`
    + "\n定时计划将被删除；后续会话清理失败也不会恢复计划。"
    + "\n\n原生会话、派生子会话、Codex App/CLI 历史和本地会话登记将永久删除，无法恢复。"
    + "\n全部会话删除确认成功后，才会删除 Project 登记；部分失败时会保留 Project 和剩余会话。"
    + `\n\n磁盘代码目录保留：${preview.project.cwd}`,
  );
}

async function deleteProject(project) {
  const envelope = project.actions.previewDelete;
  if (!envelope || pendingProjectDeletes.has(project.alias)) return;
  pendingProjectDeletes.add(project.alias);
  project.actions.previewDelete = null;
  for (const row of document.querySelectorAll("#projects-body tr")) {
    if (row.dataset.projectAlias !== project.alias) continue;
    for (const button of row.querySelectorAll("button")) button.disabled = true;
  }
  let submitted = false;
  let message = `正在读取 Project「${project.alias}」的删除范围…`;
  let details = [];
  let isError = false;
  showProjectDeleteResult(message);
  setStatus(message);
  try {
    const preview = await api("/api/v1/projects/delete-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(envelope)),
    });
    if (!confirmProjectDelete(preview)) {
      message = `已取消删除 Project「${project.alias}」，未提交删除。`;
      return;
    }
    submitted = true;
    showProjectDeleteResult(`正在删除 Project「${project.alias}」及关联 Sessions，请等待服务端确认…`);
    const result = await api("/api/v1/projects/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(preview.actions.delete)),
    });
    if (result.deleted === true) {
      message = `Project「${result.projectAlias}」及关联 Sessions 已删除，已删除 ${result.deletedSessionCount} 个 Session。磁盘代码目录保留。`;
    } else {
      isError = true;
      message = `Project「${result.projectAlias}」仍保留，删除尚未全部完成。`
        + `\n已删除 ${result.deletedSessionCount} 个 Session；剩余 ${result.remainingSessionCount} 个 Session、${result.remainingSideCount} 个 Side。`
        + (result.message ? `\n${result.message}` : "");
      details = (result.remainingSessions || []).map((session) =>
        `Session ${session.shortId} · Scope ${session.scopeKey} · Binding ${session.bindingId}`);
      if (result.failedBindingId) details.push(`未确认删除的 Binding：${result.failedBindingId}`);
      if (result.failedSideId) details.push(`未确认收尾的 Side：${result.failedSideId}`);
    }
    if (result.deletedPlanCount) {
      message += `\n已删除 ${result.deletedPlanCount} 个定时计划；后续会话清理失败也不会恢复计划。`;
    }
    if (result.remainingScheduledRunCount) {
      details.push(`仍有 ${result.remainingScheduledRunCount} 条定时执行的交接或清理未确认。`);
    }
    return result;
  } catch (error) {
    isError = true;
    if (submitted && (error.status == null || error.status >= 500)) {
      message = `Project「${project.alias}」的删除结果未确认，服务端可能仍在处理。请手动刷新 Projects 查看事实；不要重复提交删除。`;
    } else {
      message = submitted
        ? `Project「${project.alias}」的删除请求未完成。${error.message}`
        : `无法读取 Project「${project.alias}」的删除范围，未提交删除。${error.status ? error.message : "请检查连接后刷新 Projects。"}`;
    }
  } finally {
    pendingProjectDeletes.delete(project.alias);
    const refreshed = await refresh("projects");
    if (!refreshed) message += "\nProjects 刷新失败，请手动刷新查看最新状态。";
    showProjectDeleteResult(message, details, isError);
    setStatus(message, isError);
  }
}

async function loadProjects(cursor = null) {
  const query = new URLSearchParams({ pageSize: "25" });
  if (cursor) query.set("cursor", cursor);
  const data = await api(`/api/v1/projects?${query}`);
  state.projects = data;
  state.projectCursor = data.nextCursor;
  const body = document.querySelector("#projects-body");
  body.replaceChildren();
  for (const project of data.items) {
    const row = document.createElement("tr");
    row.dataset.projectAlias = project.alias;
    cell(row, project.alias);
    cell(row, project.cwd, "id");
    const status = document.createElement("td");
    status.append(badge(
      project.deleting ? "正在删除" : project.enabled ? "Enabled" : "Disabled",
      !project.deleting && project.enabled,
      project.deleting,
    ));
    row.append(status);
    cell(row, `${project.bindingCount}（Lazy ${project.lazyBindingCount}）`);
    cell(row, project.archivedBindingCount == null
      ? "未确认"
      : project.unconfirmedBindingCount > 0
        ? `已确认 ${project.archivedBindingCount}；另有 ${project.unconfirmedBindingCount} 个会话状态未确认`
        : project.archivedBindingCount);
    cell(row, project.lastActivatedAt);
    const actions = actionsCell(row);
    if (project.actions.setEnabled) {
      actions.append(actionButton(
        project.enabled ? "停用" : "启用",
        () => mutate("/api/v1/projects/set-enabled", project.actions.setEnabled, { enabled: !project.enabled }),
        project.enabled,
      ));
    }
    if (project.actions.previewDelete) {
      actions.append(actionButton(
        "删除 Project 及关联 Sessions", () => deleteProject(project), true,
      ));
    }
    if (project.deleting || pendingProjectDeletes.has(project.alias)) {
      for (const button of actions.querySelectorAll("button")) button.disabled = true;
    }
    body.append(row);
  }
  document.querySelector("#projects-next").hidden = !data.nextCursor;
}

const timeRangeControllers = new WeakMap();
const sessionMultiFilters = new Map();
let openSessionMultiFilter = null;

const sessionFilterOptions = {
  project: [],
  scopeKind: [["direct", "单聊"], ["group", "群聊"], ["topic", "话题"]],
  inventoryState: [["active", "Active"], ["lazy", "Lazy"], ["unknown", "状态未确认"], ["archived", "Archived"], ["missing", "Missing"]],
  current: [["true", "当前"], ["false", "非当前"]],
};

function initializeSessionMultiFilter(root) {
  const name = root.dataset.sessionMulti;
  const legend = root.querySelector("legend");
  legend.id = `session-${name}-label`;
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "multi-filter-trigger";
  trigger.setAttribute("aria-expanded", "false");
  const summary = document.createElement("span");
  summary.className = "multi-filter-summary";
  const chevron = document.createElement("span");
  chevron.textContent = "⌄";
  chevron.setAttribute("aria-hidden", "true");
  trigger.append(summary, chevron);
  const popover = document.createElement("div");
  popover.className = "multi-filter-popover";
  popover.id = `session-${name}-options`;
  popover.hidden = true;
  popover.setAttribute("role", "group");
  popover.setAttribute("aria-labelledby", legend.id);
  trigger.setAttribute("aria-controls", popover.id);
  const search = document.createElement("input");
  search.type = "search";
  search.className = "multi-filter-search";
  search.placeholder = "搜索 Project";
  search.setAttribute("aria-label", "搜索 Project");
  if (name === "project") popover.append(search);
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "multi-filter-clear";
  clear.textContent = "清除筛选（全部）";
  const choices = document.createElement("div");
  choices.className = "multi-filter-options";
  const empty = document.createElement("p");
  empty.className = "multi-filter-empty";
  empty.textContent = "没有匹配的 Project";
  empty.hidden = true;
  popover.append(clear, choices, empty);
  root.append(trigger, popover);
  let options = sessionFilterOptions[name];
  let selected = new Set(name === "inventoryState" ? ["active", "lazy", "unknown"] : []);

  function renderSummary() {
    const labels = options.filter(([value]) => selected.has(value)).map(([, label]) => label);
    summary.textContent = selected.size === 0 ? "全部" : labels.join("、");
    trigger.title = summary.textContent;
    trigger.setAttribute("aria-label", `${legend.textContent}：${summary.textContent}`);
  }

  function filterChoices() {
    const term = search.value.trim().toLocaleLowerCase();
    let visible = 0;
    for (const label of choices.children) {
      label.hidden = !label.textContent.toLocaleLowerCase().includes(term);
      if (!label.hidden) visible += 1;
    }
    empty.hidden = visible !== 0;
  }

  function renderChoices() {
    choices.replaceChildren();
    for (const [value, labelText] of options) {
      const label = document.createElement("label");
      label.className = "multi-filter-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = value;
      input.checked = selected.has(value);
      input.addEventListener("change", () => {
        if (input.checked) selected.add(value);
        else selected.delete(value);
        renderSummary();
      });
      label.append(input, document.createTextNode(labelText));
      choices.append(label);
    }
    filterChoices();
    renderSummary();
  }

  function close({ restoreFocus = false } = {}) {
    popover.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (openSessionMultiFilter === controller) openSessionMultiFilter = null;
    if (restoreFocus) trigger.focus();
  }

  function visibleInputs() {
    return [...choices.children].filter((label) => !label.hidden)
      .map((label) => label.querySelector("input"));
  }

  function open() {
    openSessionMultiFilter?.close();
    openTimeRangeFilter?.cancel({ restoreFocus: false });
    openSessionMultiFilter = controller;
    search.value = "";
    filterChoices();
    popover.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    // Keep the dropdown inside the viewport even in the last grid column.
    const bounds = root.getBoundingClientRect();
    const width = popover.getBoundingClientRect().width;
    popover.style.left = `${Math.min(0, window.innerWidth - 20 - bounds.left - width)}px`;
    (name === "project" ? search : visibleInputs()[0] || clear).focus();
  }

  const controller = {
    close,
    values: () => [...selected],
    reset() {
      selected = new Set(name === "inventoryState" ? ["active", "lazy", "unknown"] : []);
      search.value = "";
      close();
      renderChoices();
    },
    setOptions(nextOptions) {
      // A selection remains visible if its Project was removed in another view.
      const known = new Set(nextOptions.map(([value]) => value));
      options = [...nextOptions, ...[...selected].filter((value) => !known.has(value))
        .map((value) => [value, `${value}（已不可用）`])];
      renderChoices();
    },
  };
  trigger.addEventListener("click", () => {
    if (popover.hidden) open();
    else close({ restoreFocus: true });
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      open();
    }
  });
  search.addEventListener("input", filterChoices);
  clear.addEventListener("click", () => {
    selected.clear();
    renderChoices();
  });
  root.addEventListener("keydown", (event) => {
    if (popover.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close({ restoreFocus: true });
    } else if (event.key === "Enter" && event.target === search) {
      event.preventDefault();
    } else if (event.target !== trigger && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      const inputs = visibleInputs();
      if (!inputs.length) return;
      event.preventDefault();
      const index = inputs.indexOf(document.activeElement);
      const next = index < 0 ? 0 : (index + (event.key === "ArrowDown" ? 1 : -1) + inputs.length) % inputs.length;
      inputs[next].focus();
    }
  });
  // Label clicks briefly blur to no target before focusing their checkbox.
  // Close only when focus actually enters another part of the page.
  document.addEventListener("focusin", (event) => {
    if (!root.contains(event.target)) close();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!root.contains(event.target)) close();
  });
  renderChoices();
  return controller;
}

for (const root of document.querySelectorAll("[data-session-multi]")) {
  sessionMultiFilters.set(root.dataset.sessionMulti, initializeSessionMultiFilter(root));
}

async function queryProjectOptions() {
  const options = [];
  const seenCursors = new Set();
  let cursor = null;
  do {
    const query = new URLSearchParams({ pageSize: "50" });
    if (cursor) query.set("cursor", cursor);
    const data = await api(`/api/v1/projects/options?${query}`);
    options.push(...data.items);
    cursor = data.nextCursor;
    if (cursor && seenCursors.has(cursor)) throw new Error("Project 列表分页异常，请刷新重试。");
    if (cursor) seenCursors.add(cursor);
  } while (cursor);
  return options;
}

async function loadSessionProjectOptions() {
  const projects = await queryProjectOptions();
  sessionMultiFilters.get("project").setOptions(projects.map((project) => [
    project.alias, project.enabled ? project.alias : `${project.alias}（已停用）`,
  ]));
}

function formQuery(form, defaultPageSize = "25", refreshRelativeTime = true) {
  for (const root of form.querySelectorAll("[data-time-range]")) {
    if (refreshRelativeTime) timeRangeControllers.get(root)?.prepareQuery();
  }
  const query = new URLSearchParams();
  for (const [key, value] of new FormData(form)) {
    if (String(value)) query.append(key, String(value));
  }
  for (const root of form.querySelectorAll("[data-session-multi]")) {
    const name = root.dataset.sessionMulti;
    const values = sessionMultiFilters.get(name).values();
    for (const value of values) query.append(name, value);
    if (name === "inventoryState" && values.length === 0) query.set(name, "all");
  }
  if (!query.has("pageSize")) query.set("pageSize", defaultPageSize);
  return query;
}

const timeRangeTemplate = document.querySelector("#time-range-template");
const timeRangeMobile = window.matchMedia("(max-width: 650px)");
const timeRangeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "浏览器本地时区";
let openTimeRangeFilter = null;
let timeRangeModalOwner = null;
let timeRangeModalIsolation = [];

const timeRangePresetLabels = {
  all: "不限时间",
  today: "今天",
  yesterday: "昨天",
  "last-24-hours": "最近 24 小时",
  "last-7-days": "最近 7 天",
  "last-30-days": "最近 30 天",
};

function twoDigits(value) {
  return String(value).padStart(2, "0");
}

function localMinuteValue(value) {
  return [
    String(value.getFullYear()).padStart(4, "0"),
    "-",
    twoDigits(value.getMonth() + 1),
    "-",
    twoDigits(value.getDate()),
    "T",
    twoDigits(value.getHours()),
    ":",
    twoDigits(value.getMinutes()),
  ].join("");
}

function formatLocalMinute(value) {
  return localMinuteValue(value).replace("T", " ");
}

function compactLocalMinute(value) {
  return `${twoDigits(value.getMonth() + 1)}-${twoDigits(value.getDate())} ${twoDigits(value.getHours())}:${twoDigits(value.getMinutes())}`;
}

function utcOffsetLabel(value) {
  const total = -value.getTimezoneOffset();
  const sign = total >= 0 ? "+" : "-";
  const absolute = Math.abs(total);
  return `UTC${sign}${twoDigits(Math.floor(absolute / 60))}:${twoDigits(absolute % 60)}`;
}

function canonicalUtc(value) {
  return `${value.toISOString().slice(0, -1)}000+00:00`;
}

function sameLocalMinute(value, parts) {
  return value.getFullYear() === parts.year
    && value.getMonth() === parts.month - 1
    && value.getDate() === parts.day
    && value.getHours() === parts.hour
    && value.getMinutes() === parts.minute;
}

function parseLocalMinute(raw) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(raw);
  if (!match) return { date: null, error: "请选择有效的本地日期和时间。" };
  const parts = {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
  };
  const value = new Date(0);
  value.setFullYear(parts.year, parts.month - 1, parts.day);
  value.setHours(parts.hour, parts.minute, 0, 0);
  if (!sameLocalMinute(value, parts)) {
    return { date: null, error: "该本地时间不存在，可能处于夏令时切换。" };
  }

  const currentOffset = value.getTimezoneOffset();
  const nearbyOffsets = new Set([
    new Date(value.getTime() - 24 * 60 * 60 * 1000).getTimezoneOffset(),
    new Date(value.getTime() + 24 * 60 * 60 * 1000).getTimezoneOffset(),
  ]);
  for (const candidateOffset of nearbyOffsets) {
    if (candidateOffset === currentOffset) continue;
    const alternative = new Date(
      value.getTime() + (candidateOffset - currentOffset) * 60 * 1000,
    );
    if (sameLocalMinute(alternative, parts)) {
      return { date: null, error: "该本地时间因夏令时切换而重复，请选择其他时间。" };
    }
  }
  return { date: value, error: "" };
}

function zoneDescription(values = []) {
  const dates = values.length ? values : [new Date()];
  const offsets = [...new Set(dates.map(utcOffsetLabel))];
  return `时区：${timeRangeZone} (${offsets.join(" → ")})`;
}

function setTimeRangeModalIsolation(controller, popover, active) {
  if (!active && timeRangeModalOwner !== controller) return;
  for (const node of timeRangeModalIsolation) node.removeAttribute("inert");
  timeRangeModalIsolation = [];
  timeRangeModalOwner = active ? controller : null;
  if (active) {
    let branch = popover;
    for (let parent = popover.parentElement; parent; parent = parent.parentElement) {
      for (const sibling of parent.children) {
        if (sibling === branch || sibling.matches("[data-time-range-backdrop]")) {
          continue;
        }
        if (!sibling.hasAttribute("inert")) {
          sibling.setAttribute("inert", "");
          timeRangeModalIsolation.push(sibling);
        }
      }
      branch = parent;
    }
  }
  document.body.classList.toggle(
    "time-range-modal-open",
    timeRangeModalOwner !== null,
  );
}

function allTimeRange() {
  return {
    kind: "all",
    from: "",
    before: "",
    startValue: "",
    endValue: "",
    summary: "全部时间",
  };
}

function presetTimeRange(kind, now = new Date()) {
  if (kind === "all") return allTimeRange();
  let from;
  let before;
  if (kind === "today") {
    from = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    before = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  } else if (kind === "yesterday") {
    from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    before = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  } else {
    const durationDays = {
      "last-24-hours": 1,
      "last-7-days": 7,
      "last-30-days": 30,
    }[kind];
    if (!durationDays) throw new Error("未知时间范围预设");
    before = new Date(now.getTime());
    from = new Date(now.getTime() - durationDays * 24 * 60 * 60 * 1000);
  }
  const label = timeRangePresetLabels[kind];
  const summary = kind.startsWith("last-")
    ? `${label} · 至 ${compactLocalMinute(before)} · ${utcOffsetLabel(before)}`
    : `${label} · ${utcOffsetLabel(from)}`;
  return {
    kind,
    from: canonicalUtc(from),
    before: canonicalUtc(before),
    startValue: localMinuteValue(from),
    endValue: localMinuteValue(new Date(before.getTime() - 60 * 1000)),
    summary,
  };
}

function customTimeRange(startValue, endValue) {
  if (!startValue || !endValue) {
    return {
      range: null,
      error: "请选择开始时间和结束时间。",
      invalid: startValue ? "end" : "start",
    };
  }
  const parsedStart = parseLocalMinute(startValue);
  if (!parsedStart.date) {
    return { range: null, error: `开始时间：${parsedStart.error}`, invalid: "start" };
  }
  const parsedEnd = parseLocalMinute(endValue);
  if (!parsedEnd.date) {
    return { range: null, error: `结束时间：${parsedEnd.error}`, invalid: "end" };
  }
  if (parsedEnd.date < parsedStart.date) {
    return { range: null, error: "结束时间不能早于开始时间。", invalid: "end" };
  }
  const before = new Date(parsedEnd.date.getTime() + 60 * 1000);
  return {
    range: {
      kind: "custom",
      from: canonicalUtc(parsedStart.date),
      before: canonicalUtc(before),
      startValue,
      endValue,
      summary: `${formatLocalMinute(parsedStart.date)} — ${formatLocalMinute(parsedEnd.date)} · ${utcOffsetLabel(parsedStart.date)}`,
    },
    error: "",
    invalid: null,
  };
}

function initializeTimeRangeFilter(root) {
  if (!(timeRangeTemplate instanceof HTMLTemplateElement)) {
    throw new Error("时间范围模板不可用");
  }
  root.append(timeRangeTemplate.content.cloneNode(true));
  const identity = root.dataset.timeRangeId;
  const fromNode = root.querySelector("[data-time-range-from]");
  const beforeNode = root.querySelector("[data-time-range-before]");
  const trigger = root.querySelector("[data-time-range-trigger]");
  const summary = root.querySelector("[data-time-range-summary]");
  const quickClear = root.querySelector("[data-time-range-quick-clear]");
  const backdrop = root.querySelector("[data-time-range-backdrop]");
  const popover = root.querySelector("[data-time-range-popover]");
  const title = root.querySelector("[data-time-range-dialog-title]");
  const start = root.querySelector("[data-time-range-start]");
  const end = root.querySelector("[data-time-range-end]");
  const help = root.querySelector("[data-time-range-help]");
  const zone = root.querySelector("[data-time-range-zone]");
  const error = root.querySelector("[data-time-range-error]");
  const done = root.querySelector("[data-time-range-done]");
  const cancel = root.querySelector("[data-time-range-cancel]");
  const clear = root.querySelector("[data-time-range-clear]");
  const presetButtons = [...root.querySelectorAll("[data-time-range-preset]")];

  popover.id = `${identity}-popover`;
  title.id = `${identity}-title`;
  help.id = `${identity}-help`;
  zone.id = `${identity}-zone`;
  error.id = `${identity}-error`;
  start.id = `${identity}-start`;
  end.id = `${identity}-end`;
  trigger.setAttribute("aria-controls", popover.id);
  popover.setAttribute("aria-labelledby", title.id);
  start.setAttribute("aria-describedby", `${help.id} ${zone.id} ${error.id}`);
  end.setAttribute("aria-describedby", `${help.id} ${zone.id} ${error.id}`);

  let applied = allTimeRange();
  let draft = allTimeRange();

  function setInputValidity(invalid) {
    for (const [node, name] of [[start, "start"], [end, "end"]]) {
      if (invalid === name) node.setAttribute("aria-invalid", "true");
      else node.removeAttribute("aria-invalid");
    }
  }

  function renderApplied() {
    fromNode.value = applied.from;
    beforeNode.value = applied.before;
    summary.textContent = applied.summary;
    trigger.classList.toggle("has-value", applied.kind !== "all");
    trigger.setAttribute("aria-label", `创建时间：${applied.summary}`);
    quickClear.hidden = applied.kind === "all";
  }

  function renderDraft() {
    start.value = draft.startValue;
    end.value = draft.endValue;
    error.textContent = "";
    setInputValidity(null);
    done.disabled = false;
    for (const button of presetButtons) {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.timeRangePreset === draft.kind),
      );
    }
    const parsedDates = [start.value, end.value]
      .map(parseLocalMinute)
      .filter((item) => item.date)
      .map((item) => item.date);
    zone.textContent = zoneDescription(parsedDates);
  }

  function validateDraft() {
    const result = customTimeRange(start.value, end.value);
    draft = result.range || {
      kind: "custom",
      from: "",
      before: "",
      startValue: start.value,
      endValue: end.value,
      summary: "自定义范围",
    };
    error.textContent = result.error;
    setInputValidity(result.invalid);
    done.disabled = !result.range;
    for (const button of presetButtons) button.setAttribute("aria-pressed", "false");
    const parsedDates = [start.value, end.value]
      .map(parseLocalMinute)
      .filter((item) => item.date)
      .map((item) => item.date);
    zone.textContent = zoneDescription(parsedDates);
    return Boolean(result.range);
  }

  function syncPresentationMode() {
    const mobile = timeRangeMobile.matches && !popover.hidden;
    backdrop.hidden = !mobile;
    if (mobile) {
      popover.setAttribute("aria-modal", "true");
      popover.style.removeProperty("left");
      popover.style.removeProperty("right");
    } else {
      popover.removeAttribute("aria-modal");
      if (!popover.hidden) {
        const margin = 24;
        const width = Math.min(620, window.innerWidth - margin * 2);
        const rootBounds = root.getBoundingClientRect();
        const viewportLeft = Math.min(
          Math.max(rootBounds.left, margin),
          window.innerWidth - margin - width,
        );
        popover.style.left = `${viewportLeft - rootBounds.left}px`;
        popover.style.right = "auto";
      }
    }
    setTimeRangeModalIsolation(controller, popover, mobile);
  }

  function closePopover({ restoreFocus = true } = {}) {
    popover.hidden = true;
    backdrop.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    popover.removeAttribute("aria-modal");
    setTimeRangeModalIsolation(controller, popover, false);
    if (openTimeRangeFilter === controller) openTimeRangeFilter = null;
    if (restoreFocus) trigger.focus();
  }

  function cancelDraft(options) {
    draft = { ...applied };
    closePopover(options);
  }

  function prepareQuery() {
    if (applied.kind === "all" || applied.kind === "custom") return;
    applied = presetTimeRange(applied.kind);
    renderApplied();
  }

  function openPopover() {
    openSessionMultiFilter?.close();
    if (openTimeRangeFilter && openTimeRangeFilter !== controller) {
      openTimeRangeFilter.cancel({ restoreFocus: false });
    }
    openTimeRangeFilter = controller;
    draft = { ...applied };
    renderDraft();
    popover.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    syncPresentationMode();
    const selected = presetButtons.find(
      (button) => button.dataset.timeRangePreset === draft.kind,
    );
    (selected || start).focus();
  }

  const controller = {
    cancel: cancelDraft,
    prepareQuery,
    reset() {
      applied = allTimeRange();
      draft = allTimeRange();
      closePopover({ restoreFocus: false });
      renderApplied();
    },
  };

  trigger.addEventListener("click", () => {
    if (popover.hidden) openPopover();
    else cancelDraft();
  });
  quickClear.addEventListener("click", () => {
    applied = allTimeRange();
    draft = allTimeRange();
    renderApplied();
    if (!popover.hidden) renderDraft();
    trigger.focus();
  });
  for (const button of presetButtons) {
    button.addEventListener("click", () => {
      draft = presetTimeRange(button.dataset.timeRangePreset);
      renderDraft();
    });
  }
  start.addEventListener("input", validateDraft);
  end.addEventListener("input", validateDraft);
  clear.addEventListener("click", () => {
    draft = allTimeRange();
    renderDraft();
  });
  cancel.addEventListener("click", () => cancelDraft());
  done.addEventListener("click", () => {
    if (draft.kind === "custom" && !validateDraft()) {
      (start.getAttribute("aria-invalid") === "true" ? start : end).focus();
      return;
    }
    applied = { ...draft };
    renderApplied();
    closePopover();
  });
  backdrop.addEventListener("click", () => cancelDraft());
  popover.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      cancelDraft();
      return;
    }
    if (event.key === "Enter" && (event.target === start || event.target === end)) {
      event.preventDefault();
      if (!done.disabled) done.click();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...popover.querySelectorAll("button:not(:disabled), input:not(:disabled)")]
      .filter((node) => !node.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!popover.hidden && !root.contains(event.target)) {
      cancelDraft({ restoreFocus: false });
    }
  });
  timeRangeMobile.addEventListener("change", syncPresentationMode);
  window.addEventListener("resize", syncPresentationMode);
  renderApplied();
  zone.textContent = zoneDescription();
  return controller;
}

for (const root of document.querySelectorAll("[data-time-range]")) {
  timeRangeControllers.set(root, initializeTimeRangeFilter(root));
}

function runtimeLabel(runtime) {
  if (runtime.primaryStatusResolution === "archived") return "—";
  if (runtime.primaryStatus != null) {
    return runtime.primaryStatusResolution === "unavailable"
      || runtime.primaryStatusResolution === "deferred"
      ? `${runtime.primaryStatus}（待确认）`
      : runtime.primaryStatus;
  }
  return runtime.primaryStatusResolution === "deferred"
    ? "状态待确认"
    : "状态暂不可用";
}

function updateRuntimeCell(target, runtime) {
  const archived = runtime.primaryStatusResolution === "archived";
  const label = runtimeLabel(runtime);
  const title = archived ? "已归档，不查询运行态" : "";
  const subscriptionLabel = !archived && runtime.subscriptionState
    ? `订阅：${runtime.subscriptionState}` : "";
  const previous = target.querySelector(".runtime-primary");
  if (
    previous?.textContent === label
    && previous.title === title
    && (target.querySelector(".runtime-subscription")?.textContent || "") === subscriptionLabel
  ) return;
  const primary = document.createElement("span");
  primary.className = "runtime-primary";
  primary.textContent = label;
  primary.title = title;
  target.replaceChildren(primary);
  if (subscriptionLabel) {
    const subscription = document.createElement("small");
    subscription.className = "runtime-subscription";
    subscription.textContent = subscriptionLabel;
    target.append(document.createElement("br"), subscription);
  }
}

function chatModeLabel(mode, scopeKind = null) {
  const known = { p2p: "单聊", group: "群聊", topic: "话题群" }[mode];
  if (known) return known;
  if (scopeKind === "direct") return "单聊";
  if (scopeKind === "group") return "群聊";
  return "飞书会话";
}

function pointerStateLabel(value) {
  return {
    current: "当前",
    inactive: "非当前",
  }[value] || value;
}

function catalogStateLabel(value) {
  return {
    active: "Active",
    archived: "已归档",
    lazy: "Lazy",
    unknown: "状态未确认",
    missing: "原生会话缺失",
  }[value] || value;
}

function shortIdentity(value) {
  const text = String(value || "");
  return text.length > 18 ? `${text.slice(0, 12)}…` : text;
}

function locationFallback(session) {
  return `${chatModeLabel(session.chatMode, session.scopeKind)} · ${shortIdentity(session.chatId)}`;
}

function sessionLocationCell(row, session) {
  const td = document.createElement("td");
  const link = document.createElement("a");
  link.className = "chat-link";
  link.href = session.topicOpenUrl || session.chatOpenUrl;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = session.chatLabelResolved
    ? session.chatLabel
    : locationFallback(session);
  link.title = session.topicOpenUrl
    ? "打开飞书话题"
    : "打开飞书会话";
  const mode = document.createElement("div");
  mode.className = "meta-line";
  mode.append(badge(chatModeLabel(session.chatMode, session.scopeKind)));
  const chatId = document.createElement("span");
  chatId.className = "id";
  chatId.textContent = session.chatId;
  mode.append(chatId);
  td.append(link, mode);
  if (session.topicId) {
    const topicId = document.createElement("div");
    topicId.className = "id";
    topicId.textContent = `Topic ${session.topicId}`;
    td.append(topicId);
  }
  row.append(td);
}

function sessionIdentityCell(row, session) {
  const td = document.createElement("td");
  const title = document.createElement("strong");
  title.textContent = session.nativeTitle
    || (session.catalogState === "lazy" ? "Lazy Session" : "未命名 Session");
  td.append(title);
  if (session.nativePreview) {
    const preview = document.createElement("div");
    preview.className = "preview";
    preview.textContent = session.nativePreview;
    td.append(preview);
  }
  const bindingId = document.createElement("div");
  bindingId.className = "id";
  bindingId.textContent = `Binding ${session.shortId} · ${session.bindingId}`;
  td.append(bindingId);
  if (session.nativeThreadId) {
    const threadId = document.createElement("div");
    threadId.className = "id";
    threadId.textContent = `Thread ${session.nativeThreadId}`;
    td.append(threadId);
  }
  row.append(td);
}

function sessionUpdatedAtLabel(session) {
  if (session.catalogState === "lazy") return "尚未开始";
  if (!Number.isSafeInteger(session.updatedAt) || session.updatedAt < 0) return "—";
  const date = new Date(session.updatedAt * 1000);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function renderSessionPagination() {
  const page = state.sessionPage;
  document.querySelector("#sessions-previous").disabled = page.previousCursors.length === 0;
  document.querySelector("#sessions-next").disabled = !page.nextCursor;
  const count = state.sessions?.items?.length || 0;
  document.querySelector("#sessions-page").textContent = `第 ${page.number} 页 · ${count} 条`;
}

function resetSessionPagination() {
  state.sessionPage = {
    cursor: null,
    nextCursor: null,
    previousCursors: [],
    number: 1,
    query: null,
  };
}

function resetSessionFilters() {
  const form = document.querySelector("#session-filter");
  form.reset();
  sessionChatPicker?.reset();
  for (const controller of sessionMultiFilters.values()) controller.reset();
  for (const root of form.querySelectorAll("[data-time-range]")) {
    timeRangeControllers.get(root)?.reset();
  }
  resetSessionPagination();
  return refresh("sessions");
}

async function loadSessions(cursor = state.sessionPage.cursor) {
  ensureSessionChatFilter();
  const query = state.sessionPage.query == null
    ? formQuery(document.querySelector("#session-filter"), "20")
    : new URLSearchParams(state.sessionPage.query);
  const appliedQuery = query.toString();
  await loadSessionProjectOptions();
  if (cursor) query.set("cursor", cursor);
  const data = await api(`/api/v1/sessions?${query}`);
  state.sessions = data;
  state.sessionPage.cursor = cursor;
  state.sessionPage.nextCursor = data.nextCursor;
  state.sessionPage.query = appliedQuery;
  document.querySelector("#sessions-catalog-notice").hidden = data.catalogAvailable !== false
    && !data.items.some((session) => session.catalogState === "unknown");
  const body = document.querySelector("#sessions-body");
  body.replaceChildren();
  for (const session of data.items) {
    const row = document.createElement("tr");
    row.dataset.bindingId = session.bindingId;
    sessionLocationCell(row, session);
    const type = document.createElement("td");
    type.append(badge(session.sessionType === "topic" ? "话题" : "消息"));
    row.append(type);
    sessionIdentityCell(row, session);
    const sessionState = document.createElement("td");
    sessionState.append(badge(
      pointerStateLabel(session.pointerState),
      session.pointerState === "current",
    ));
    sessionState.append(document.createTextNode(" "), badge(
      catalogStateLabel(session.catalogState),
      session.catalogState === "active",
      session.catalogState === "missing",
    ));
    row.append(sessionState);
    const runtime = document.createElement("td");
    runtime.className = "runtime-state";
    updateRuntimeCell(runtime, session.runtime);
    row.append(runtime);
    cell(row, session.projectAlias);
    const settings = session.sessionSettings;
    const turnSettings = settings.turn_settings;
    const model = turnSettings ? `${turnSettings.model_id} / ${turnSettings.effort_id} / ${turnSettings.service_tier_id}` : "继承 Codex";
    cell(row, `${settings.message_context_mode} · ${model}`);
    cell(row, sessionUpdatedAtLabel(session));
    const actions = actionsCell(row);
    wireSessionActions(actions, session);
    body.append(row);
  }
  renderSessionPagination();
}

function wireSessionActions(actions, session) {
  const a = session.actions;
  const schedule = actionButton("创建定时任务", () => openScheduleForSession(session, schedule));
  actions.append(schedule);
  if (a.createLazy) actions.append(actionButton("新建 Lazy", async () => {
    if (!state.projects) await loadProjects();
    const alias = window.prompt("Project alias");
    if (!alias) return;
    const project = state.projects.items.find((item) => item.alias === alias && item.enabled);
    if (!project) return setStatus("未找到当前页中已启用的 Project，请先刷新 Projects。", true);
    const activate = window.confirm("是否立即设为当前会话？");
    await mutate("/api/v1/sessions/create-lazy", a.createLazy, {
      projectAlias: alias,
      projectRevision: project.revision,
      activate,
      turnSettings: null,
    });
  }));
  if (a.activate) actions.append(actionButton("设为当前", () => mutate("/api/v1/sessions/activate", a.activate)));
  if (a.configure) {
    const button = actionButton("配置", () => openSessionSettingsEditor(session, button));
    actions.append(button);
  }
  if (a.rename) actions.append(actionButton("重命名", async () => {
    const name = window.prompt("新的原生会话名称");
    if (name) await mutate("/api/v1/sessions/rename", a.rename, { name });
  }));
  if (a.archive) actions.append(actionButton("归档", () => window.confirm("确认归档？") && mutate("/api/v1/sessions/archive", a.archive), true));
  if (a.unarchive) actions.append(actionButton("恢复", () => mutate("/api/v1/sessions/unarchive", a.unarchive, { actionKind: a.unarchive.actionKind })));
  if (a.unarchiveCurrent) actions.append(actionButton("恢复并设为当前", () => mutate("/api/v1/sessions/unarchive", a.unarchiveCurrent, { actionKind: a.unarchiveCurrent.actionKind })));
  if (a.deleteLazy) actions.append(actionButton("删除 Lazy", () => window.confirm("永久删除这个 Lazy Binding？") && mutate("/api/v1/sessions/delete-lazy", a.deleteLazy), true));
  if (a.deleteMaterialized) actions.append(actionButton("删除", () => confirmMaterializedDelete(session) && mutate("/api/v1/sessions/delete-materialized", a.deleteMaterialized), true));
  if (a.stop) actions.append(actionButton("停止", () => window.confirm("停止这个 exact 会话的当前运行？") && mutate("/api/v1/sessions/stop", a.stop), true));
  if (a.release) actions.append(actionButton("释放订阅", () => window.confirm("释放本进程订阅？历史不会删除。") && mutate("/api/v1/sessions/release", a.release)));
}

async function loadSides(cursor = null) {
  const query = formQuery(
    document.querySelector("#side-filter"),
    "25",
    cursor === null,
  );
  if (cursor) query.set("cursor", cursor);
  const data = await api(`/api/v1/side-topics?${query}`);
  state.sides = data;
  state.sideCursor = data.nextCursor;
  const body = document.querySelector("#sides-body");
  body.replaceChildren();
  for (const side of data.items) {
    const row = document.createElement("tr");
    row.dataset.sideId = side.sideId;
    cell(row, side.sideId, "id");
    cell(row, `${side.parentBindingId} · ${side.projectAlias || "unknown"}`, "id");
    cell(row, `${side.chatLabel}${side.topicId ? ` · ${side.topicId}` : ""}`);
    const stateCell = document.createElement("td");
    stateCell.append(badge(side.state, side.state === "open", side.state === "failed"));
    row.append(stateCell);
    const runtime = cell(row, sideRuntimeLabel(side.runtime));
    runtime.className = "runtime-state";
    const actions = actionsCell(row);
    if (side.actions.close) actions.append(actionButton("结束 Side", () => window.confirm("确认结束这个 exact Side？") && mutate("/api/v1/side-topics/close", side.actions.close), true));
    body.append(row);
  }
  document.querySelector("#sides-next").hidden = !data.nextCursor;
}

async function moveSessionPage(direction) {
  const page = state.sessionPage;
  const previousState = {
    cursor: page.cursor,
    nextCursor: page.nextCursor,
    previousCursors: [...page.previousCursors],
    number: page.number,
    query: page.query,
  };
  if (direction === "next") {
    if (!page.nextCursor) return;
    page.previousCursors.push(page.cursor);
    page.cursor = page.nextCursor;
    page.number += 1;
  } else {
    if (!page.previousCursors.length) return;
    page.cursor = page.previousCursors.pop();
    page.number -= 1;
  }
  if (!await refresh("sessions")) {
    state.sessionPage = previousState;
    renderSessionPagination();
  }
}

function sideRuntimeLabel(runtime) {
  return runtime
    ? `${runtime.state}${runtime.turnState ? ` · ${runtime.turnState}` : ""}`
    : "无进程内 Session";
}

function rowByIdentity(selector, key, value) {
  for (const row of document.querySelector(selector).rows) {
    if (row.dataset[key] === value) return row;
  }
  return null;
}

let sessionSettingsEditor = null;
const sessionSettingsInput = (name) => document.querySelector(`#session-settings-${name}`);

function renderSessionSettingsEditor() {
  const editor = sessionSettingsEditor;
  if (!editor) return;
  renderSessionSettingsFields(sessionSettingsInput, editor.settings, editor.models, editor.contextAvailable);
  sessionSettingsInput("fields").disabled = editor.loading || editor.saving;
  sessionSettingsInput("context-field").hidden = editor.privateChat;
  sessionSettingsInput("context").disabled = editor.privateChat;
  sessionSettingsInput("context-note").hidden = editor.privateChat;
  sessionSettingsInput("context-note").textContent = editor.contextAvailable
    ? "补齐未读上下文会在下次 @ 时带入同一位置未 @ 机器人的成员消息。关闭后，请到飞书 /config 重新开启。"
    : "开启补齐未读上下文，请到飞书 /config 设置。";
  const notes = [editor.loading ? "正在读取模型目录…" : "", editor.catalogError, editor.error];
  if (editor.settings.turn_settings && !editor.models.some((model) => model.id === editor.settings.turn_settings.model_id)) {
    notes.push("已保存的模型当前不可用，原选择保持不变；可调整其他配置，或显式选择继承 Codex。");
  }
  sessionSettingsInput("note").textContent = notes.filter(Boolean).join(" ");
  sessionSettingsInput("note").classList.toggle("error", Boolean(editor.error));
  sessionSettingsInput("save").disabled = editor.loading || editor.saving || !editor.action;
  sessionSettingsInput("save").textContent = editor.saving ? "保存中…" : "保存";
  sessionSettingsInput("cancel").disabled = editor.saving;
  sessionSettingsInput("close").disabled = editor.saving;
  sessionSettingsInput("editor").setAttribute("aria-busy", String(editor.loading || editor.saving));
}

async function loadSessionSettingsOptions() {
  const editor = sessionSettingsEditor;
  if (!editor) return;
  const serial = ++editor.optionsSerial;
  editor.loading = true;
  renderSessionSettingsEditor();
  try {
    const data = await api("/api/v1/sessions/options");
    if (sessionSettingsEditor !== editor || serial !== editor.optionsSerial) return;
    editor.models = data.models;
    editor.catalogError = data.model_catalog_error || "";
  } catch (error) {
    if (sessionSettingsEditor !== editor || serial !== editor.optionsSerial) return;
    editor.models = [];
    editor.catalogError = `${error.message} 模型目录暂未读取，原选择保持不变。`;
  } finally {
    if (sessionSettingsEditor === editor && serial === editor.optionsSerial) {
      editor.loading = false;
      renderSessionSettingsEditor();
    }
  }
}

function openSessionSettingsEditor(session, returnFocus) {
  if (sessionSettingsEditor?.saving || !session.actions.configure) return;
  const base = structuredClone(session.sessionSettings);
  const privateChat = session.scopeKind === "direct" || session.chatMode === "p2p";
  sessionSettingsEditor = { session, returnFocus, settings: structuredClone(base), base,
    action: session.actions.configure, models: [], catalogError: "", error: "",
    privateChat, contextAvailable: !privateChat && base.message_context_mode === "catch-up",
    loading: true, saving: false, optionsSerial: 0 };
  sessionSettingsInput("editor").hidden = false;
  sessionSettingsInput("identity").textContent = `${session.nativeTitle || "未命名 Session"} · ${session.shortId} · ${session.projectAlias}`;
  renderSessionSettingsEditor();
  if (!sessionSettingsInput("drawer").open) sessionSettingsInput("drawer").showModal();
  document.body.classList.toggle("session-settings-drawer-open", true);
  sessionSettingsInput("close").focus();
  const editor = sessionSettingsEditor;
  loadSessionSettingsOptions().then(() => {
    if (sessionSettingsEditor === editor && document.activeElement === sessionSettingsInput("close")) {
      sessionSettingsInput("model").focus();
    }
  });
}

function closeSessionSettingsEditor() {
  const editor = sessionSettingsEditor;
  if (!editor || editor.saving) return;
  sessionSettingsEditor = null;
  sessionSettingsInput("editor").hidden = true;
  sessionSettingsInput("drawer").close();
  document.body.classList.toggle("session-settings-drawer-open", false);
  const returnFocus = editor.returnFocus?.disabled
    ? document.querySelector("#sessions [data-refresh]") : editor.returnFocus;
  returnFocus?.focus();
}

function changeSessionSettings(field) {
  const editor = sessionSettingsEditor;
  if (!editor || editor.loading || editor.saving) return;
  if (field === "context" && (editor.privateChat
    || (sessionSettingsInput("context").value === "catch-up" && !editor.contextAvailable))) {
    renderSessionSettingsEditor();
    return;
  }
  changeSessionSettingsField(sessionSettingsInput, editor.settings, editor.models, field, editor.base.turn_settings);
  renderSessionSettingsEditor();
}

async function saveSessionSettings(event) {
  event.preventDefault();
  const editor = sessionSettingsEditor;
  if (!editor || editor.loading || editor.saving || !editor.action) return;
  const action = editor.action;
  editor.action = null;
  editor.session.actions.configure = null;
  if (editor.returnFocus) editor.returnFocus.disabled = true;
  editor.saving = true;
  renderSessionSettingsEditor();
  setStatus("正在保存会话配置…");
  let result;
  try {
    result = await api("/api/v1/sessions/configure", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(action, { sessionSettings: structuredClone(editor.settings) })) });
  } catch (error) {
    editor.error = `${error.message} 请关闭表单，刷新 Sessions 后重新打开配置。`;
    setStatus(editor.error, true);
    editor.saving = false;
    renderSessionSettingsEditor();
    return;
  }
  editor.saving = false;
  closeSessionSettingsEditor();
  if (await refresh("sessions")) {
    setStatus(result?.message || "会话配置已保存。");
    if (!sessionSettingsEditor && state.tab === "sessions") {
      const row = rowByIdentity("#sessions-body", "bindingId", editor.session.bindingId);
      Array.from(row?.querySelectorAll("button") || []).find((button) => button.textContent === "配置")?.focus();
    }
  } else {
    setStatus("会话配置已保存，但 Sessions 列表刷新失败，请手动刷新。", true);
  }
}

// Shared target controls: names are display-only; submissions always use exact chat IDs.
async function fetchChatPage({ query, cursor }) {
  const params = new URLSearchParams();
  if (query) params.set("query", query);
  if (cursor) params.set("cursor", cursor);
  return api(`/api/v1/chats?${params}`);
}

let sessionChatPicker = null;
function ensureSessionChatFilter() {
  if (sessionChatPicker) return;
  sessionChatPicker = window.createChatPicker(document.querySelector("#session-chat-picker"), {
    id: "session-group", label: "群聊", placeholder: "不限群聊，输入群名查找",
    fetchPage: fetchChatPage,
    onChange: (chat) => { document.querySelector("#session-chat-filter").value = chat?.chatId || ""; },
  });
}

const chatTargetControllers = new Map();
function chatTarget(prefix, onChange = null) {
  if (chatTargetControllers.has(prefix)) return chatTargetControllers.get(prefix);
  const node = (name) => document.querySelector(`#${prefix}-${name}`);
  let locked = false;
  let avatarRevision = 0;
  const picker = window.createChatPicker(node("group-picker"), {
    id: `${prefix}-group`, label: "群聊", fetchPage: fetchChatPage,
    onChange: (chat) => {
      avatarRevision += 1;
      node("chat").value = chat?.chatId || "";
      onChange?.();
    },
  });
  function render() {
    const group = node("chat-kind").value === "group";
    const manual = !group || node("chat-manual").checked;
    node("group-picker").hidden = manual;
    node("chat-manual-field").hidden = !group || locked;
    node("chat-field").hidden = !manual;
    node("chat").disabled = locked || !manual;
    node("chat").required = !locked && manual;
    node("chat-kind").disabled = locked;
    picker.setDisabled(locked);
    node("chat-id-help").hidden = !manual || locked;
    node("chat-help").textContent = node("chat-kind").value === "p2p"
      ? `也可在目标单聊中发送 ${prefix === "defaults" ? "/defaults，直接配置该聊天" : "/cron，选择“当前聊天”"}，无需查询聊天 ID。`
      : node("chat-kind").value === "unknown"
        ? "暂未确认聊天类型，保留原聊天 ID；读取失败不会更换目标。"
        : manual ? "请填写群聊的聊天 ID。保存时将重新检查目标。" : "";
  }
  node("chat-kind").addEventListener("change", () => {
    avatarRevision += 1;
    picker.reset();
    node("chat").value = "";
    node("chat-manual").checked = false;
    render();
    onChange?.();
  });
  node("chat-manual").addEventListener("change", () => {
    avatarRevision += 1;
    if (!node("chat-manual").checked) {
      picker.reset();
      node("chat").value = "";
    } else picker.close();
    render();
    onChange?.();
  });
  node("chat").addEventListener("input", () => { avatarRevision += 1; picker.reset(); onChange?.(false); });
  async function hydrateAvatar(chatId, current) {
    try {
      const result = await api(`/api/v1/chats/validate?${new URLSearchParams({ chatId })}`);
      if (current !== avatarRevision || node("chat").value !== chatId || result.chat?.chatId !== chatId) return;
      picker.updateSelectionAvatar(chatId, result.chat.avatarUrl);
    } catch {
      // Display metadata must never prevent editing a saved target.
    }
  }
  const controller = {
    picker,
    render,
    set({ chatId = "", chat = null, readOnly = false } = {}) {
      const current = ++avatarRevision;
      locked = readOnly;
      const kind = ["p2p", "group"].includes(chat?.chatType) ? chat.chatType : chat?.chatMode;
      node("chat-kind").value = kind === "p2p" ? "p2p" : ["group", "topic"].includes(kind) ? "group" : chatId ? "unknown" : "group";
      node("chat-manual").checked = Boolean(chatId && !kind);
      node("chat").value = chatId;
      picker.setSelection(chatId ? { chatId, name: chat?.chatLabel || chatId, avatarUrl: chat?.avatarUrl } : null);
      render();
      if (chatId && node("chat-kind").value === "group" && !chat?.avatarUrl) void hydrateAvatar(chatId, current);
    },
    resolveKind(contextAvailable) {
      if (contextAvailable == null || !node("chat").value.trim()) return;
      const kind = contextAvailable ? "group" : "p2p";
      // Existing/manual values stay in ID mode unless the user chooses the picker.
      if (node("chat-kind").value !== kind) node("chat-manual").checked = true;
      node("chat-kind").value = kind;
      render();
    },
    kind: () => node("chat-kind").value,
    focus() { if (node("group-picker").hidden) node("chat").focus(); else picker.focus(); },
    close() { avatarRevision += 1; picker.close(); },
    async validate() {
      const id = node("chat").value.trim();
      if (!id) throw new Error("请选择聊天，或手动填写聊天 ID。");
      if (node("chat-kind").value === "group") {
        await api(`/api/v1/chats/validate?${new URLSearchParams({ chatId: id })}`);
      }
    },
  };
  chatTargetControllers.set(prefix, controller);
  return controller;
}

let defaultsEditor = null;
let defaultsProjects = [];
let defaultsOffset = 0;
let defaultsTab = "chat";
let defaultsListSerial = 0;
let defaultsBusy = false;
let defaultsNeedsRefresh = false;
const defaultsInput = (name) => document.querySelector(`#defaults-${name}`);

function selectDefaultTab(kind) {
  if (defaultsBusy) return;
  defaultsTab = kind;
  for (const candidate of ["chat", "group_name"]) {
    const selected = candidate === kind;
    defaultsInput(`tab-${candidate}`).setAttribute("aria-selected", String(selected));
    defaultsInput(`tab-${candidate}`).tabIndex = selected ? 0 : -1;
    defaultsInput(`panel-${candidate}`).hidden = !selected;
  }
  defaultsInput("new").textContent = kind === "chat" ? "添加聊天配置" : "添加群名规则";
  renderDefaultControls();
}

function renderDefaultControls() {
  defaultsInput("new").disabled = defaultsBusy || defaultsNeedsRefresh || !state.defaults?.[defaultsTab];
  for (const kind of ["chat", "group_name"]) defaultsInput(`tab-${kind}`).disabled = defaultsBusy;
  defaultsInput("previous").disabled = defaultsBusy || defaultsOffset === 0;
  defaultsInput("next").disabled = defaultsBusy || !state.defaults?.chat.has_more;
  defaultsInput("cancel").disabled = defaultsBusy;
  defaultsInput("close").disabled = defaultsBusy;
  for (const button of document.querySelectorAll("[data-default-action]")) {
    button.disabled = defaultsBusy || defaultsNeedsRefresh || button.dataset.unavailable === "true";
  }
}

function renderDefaultSettings() {
  const editor = defaultsEditor;
  renderDefaultControls();
  defaultsInput("fields").disabled = defaultsBusy || Boolean(editor?.loading);
  if (!editor) return;
  const settings = editor.settings;
  const turn = settings.turn_settings;
  const model = renderSessionSettingsFields(defaultsInput, settings, editor.models, editor.contextAvailable);
  defaultsInput("context-field").hidden = editor.kind === "chat"
    && chatTarget("defaults").kind() === "p2p" && settings.message_context_mode === "current-only";
  const notes = [editor.loading ? "正在读取配置…" : "", editor.sourceNote, editor.error, editor.catalogError];
  if (turn && !model) notes.push("已保存的模型当前不可用，选择保持不变；可显式选择其他模型。");
  if (editor.contextAvailable === false) notes.push("单聊不支持补齐未读上下文，请使用当前消息。");
  defaultsInput("note").textContent = notes.filter(Boolean).join(" ");
  defaultsInput("note").classList.toggle("error", Boolean(editor.error));
  defaultsInput("save").disabled = defaultsBusy || editor.loading || !editor.action;
  defaultsInput("save").textContent = defaultsBusy ? "保存中…" : "保存";
  defaultsInput("editor").setAttribute("aria-busy", String(defaultsBusy || editor.loading));
}

function changeDefaultChat(resolve = true) {
  const editor = defaultsEditor;
  if (!editor || editor.lockedTarget) return;
  editor.serial += 1;
  editor.loading = false;
  editor.record = null;
  editor.action = null;
  editor.error = "";
  editor.contextAvailable = chatTarget("defaults").kind() === "p2p" ? false : null;
  if (editor.contextAvailable === false) editor.settings.message_context_mode = "current-only";
  if (resolve && defaultsInput("chat").value.trim()) loadDefaultOptions(true);
  else renderDefaultSettings();
}

function renderDefaultProject(project) {
  scheduleSelectOptions(defaultsInput("project"), [["", "选择 Project"], ...defaultsProjects.map((item) => [
    item.alias, item.enabled ? item.alias : `${item.alias}（已停用）`,
  ])], project);
}

async function loadDefaultOptions(resolveChat = false) {
  const editor = defaultsEditor;
  if (!editor) return;
  const serial = ++editor.serial;
  const chatId = editor.kind === "chat" ? defaultsInput("chat").value.trim() : "";
  editor.loading = true;
  editor.error = "";
  renderDefaultSettings();
  try {
    let view = null;
    if (resolveChat && chatId) {
      view = await api(`/api/v1/defaults?${new URLSearchParams({ mode: "view", chat_id: chatId })}`);
      if (defaultsEditor !== editor || serial !== editor.serial) return;
      editor.record = view.exact;
      editor.action = view.exact?.actions.save || view.actions.create;
      const effective = view.exact || view.effective;
      if (effective) {
        editor.settings = structuredClone(effective.session_settings);
        renderDefaultProject(effective.project);
        editor.touched = true;
      } else {
        editor.settings = defaultScheduleSessionSettings();
        renderDefaultProject("");
        editor.touched = false;
      }
      editor.sourceNote = view.exact ? "修改本聊天的精确配置。"
        : view.effective ? "当前使用群名规则；保存后成为本聊天的精确配置。" : "本聊天尚未配置默认会话。";
      if (view.match_error) editor.sourceNote += ` ${view.match_error}`;
    }
    const query = new URLSearchParams({ mode: "options" });
    if (chatId) query.set("chat_id", chatId);
    const data = await api(`/api/v1/defaults?${query}`);
    if (defaultsEditor !== editor || serial !== editor.serial) return;
    editor.models = data.models;
    editor.contextAvailable = data.context_mode_available;
    if (editor.kind === "chat") chatTarget("defaults").resolveKind(editor.contextAvailable);
    editor.catalogError = typeof data.model_catalog_error === "string"
      ? data.model_catalog_error : data.model_catalog_error?.message || "";
    if (!editor.record && !editor.touched && !view?.effective) editor.settings = structuredClone(data.session_settings);
  } catch (error) {
    if (defaultsEditor !== editor || serial !== editor.serial) return;
    editor.error = `${error.message} 请重新打开表单后操作。`;
    if (resolveChat) editor.action = null;
  } finally {
    if (defaultsEditor === editor && serial === editor.serial) {
      editor.loading = false;
      renderDefaultSettings();
    }
  }
}

function openDefaultEditor(kind, record = null) {
  if (defaultsBusy || defaultsNeedsRefresh || !state.defaults) return;
  defaultsEditor = { kind, record, action: record ? record.actions.save : state.defaults[kind].actions.create,
    returnFocus: document.activeElement,
    settings: structuredClone(record?.session_settings || defaultScheduleSessionSettings()),
    models: [], serial: 0, loading: true, touched: false, contextAvailable: null,
    sourceNote: record ? "修改已保存的配置，只影响之后创建的会话。" : "保存后，在没有当前会话的位置收到消息时生效。",
    error: "", catalogError: "", lockedTarget: Boolean(record) };
  defaultsInput("editor").hidden = false;
  defaultsInput("editor-title").textContent = `${record ? "编辑" : "添加"}${kind === "chat" ? "聊天配置" : "群名规则"}`;
  defaultsInput("target-help").textContent = kind === "chat"
    ? "支持单聊和群聊，优先于群名规则。删除后可能重新命中群名规则。"
    : "群名包含关键词即匹配，英文忽略大小写。新规则追加到末尾，保存后可在列表中调整优先级。";
  chatTarget("defaults", changeDefaultChat).set({ chatId: record?.chat_id, chat: record?.chat, readOnly: Boolean(record) });
  defaultsInput("chat-target").hidden = kind !== "chat";
  defaultsInput("keyword-field").hidden = kind !== "group_name";
  defaultsInput("keyword").value = record?.keyword || "";
  defaultsInput("keyword").disabled = kind !== "group_name";
  defaultsInput("keyword").required = kind === "group_name";
  renderDefaultProject(record?.project || "");
  renderDefaultSettings();
  if (!defaultsInput("drawer").open) defaultsInput("drawer").showModal();
  document.body.classList.toggle("defaults-drawer-open", true);
  defaultsInput("close").focus();
  const editor = defaultsEditor;
  loadDefaultOptions().then(() => {
    if (defaultsEditor === editor && document.activeElement === defaultsInput("close")) {
      if (!record && kind === "chat") chatTarget("defaults").focus();
      else defaultsInput(record ? "project" : "keyword").focus();
    }
  });
}

function closeDefaultEditor() {
  if (defaultsBusy) return;
  chatTargetControllers.get("defaults")?.close();
  defaultsEditor = null;
  defaultsInput("editor").hidden = true;
  defaultsInput("drawer").close();
  document.body.classList.toggle("defaults-drawer-open", false);
}

function changeDefaultSettings(field) {
  const editor = defaultsEditor;
  if (!editor) return;
  editor.touched = true;
  changeSessionSettingsField(defaultsInput, editor.settings, editor.models, field);
  renderDefaultSettings();
}

async function defaultMutation(mode, action, extra = {}) {
  if (defaultsBusy || !action) return false;
  const returnFocus = defaultsEditor?.returnFocus || document.activeElement;
  defaultsListSerial += 1;
  defaultsBusy = true;
  renderDefaultSettings();
  try {
    await api(`/api/v1/defaults/${mode}`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(action, extra)) });
  } catch (error) {
    setStatus(`${error.message} 请刷新列表，核查后重新操作。`, true);
    if (defaultsEditor) {
      defaultsEditor.action = null;
      defaultsEditor.error = `${error.message} 请重新打开表单后操作。`;
    }
    return false;
  } finally {
    defaultsListSerial += 1;
    defaultsNeedsRefresh = true;
    defaultsBusy = false;
    renderDefaultSettings();
  }
  closeDefaultEditor();
  try {
    await loadDefaults(defaultsOffset, returnFocus);
    setStatus(mode === "delete" ? "默认会话配置已删除。" : mode === "reorder" ? "群名规则优先级已更新。" : "默认会话配置已保存。");
  } catch (error) {
    setStatus(`操作已完成，但列表刷新失败：${error.message} 请手动刷新。`, true);
  }
  return true;
}

async function saveDefault(event) {
  event.preventDefault();
  const editor = defaultsEditor;
  if (!editor || editor.loading || !editor.action || defaultsBusy) return;
  if (editor.kind === "chat") {
    editor.loading = true;
    renderDefaultSettings();
    try { await chatTarget("defaults").validate(); }
    catch (error) { editor.error = error.message; return; }
    finally {
      editor.loading = false;
      if (defaultsEditor === editor) renderDefaultSettings();
    }
    if (defaultsEditor !== editor) return;
  }
  const definition = { kind: editor.kind, project: defaultsInput("project").value,
    session_settings: structuredClone(editor.settings) };
  if (editor.kind === "chat") definition.chat_id = defaultsInput("chat").value.trim();
  else definition.keyword = defaultsInput("keyword").value.trim();
  const action = editor.action;
  editor.action = null;
  await defaultMutation("save", action, { definition });
}

async function deleteDefault(record) {
  if (defaultsBusy || defaultsNeedsRefresh || !window.confirm(record.kind === "chat"
    ? `删除 ${record.chat_id} 的精确配置？删除后可能重新命中群名规则，已有会话不受影响。`
    : `删除群名规则“${record.keyword}”？已有会话不受影响。`)) return;
  const action = record.actions.delete;
  record.actions.delete = null;
  await defaultMutation("delete", action);
}

async function moveDefaultRule(index, delta) {
  const page = state.defaults?.group_name;
  if (!page || page.has_more || defaultsBusy || defaultsNeedsRefresh) return;
  const ids = page.items.map((rule) => rule.id);
  const other = index + delta;
  if (other < 0 || other >= ids.length) return;
  [ids[index], ids[other]] = [ids[other], ids[index]];
  const action = page.actions.reorder;
  page.actions.reorder = null;
  await defaultMutation("reorder", action, { rule_ids: ids });
}

function defaultListButton(label, callback, { id = "", disabled = false, primary = false, danger = false } = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `action${primary ? "" : " secondary"}${danger ? " danger" : ""}`;
  button.textContent = label;
  button.setAttribute("data-default-action", label);
  button.dataset.defaultId = id;
  button.dataset.unavailable = String(disabled);
  button.disabled = defaultsBusy || disabled;
  button.addEventListener("click", callback);
  return button;
}

function defaultChatCell(row, rule) {
  const td = document.createElement("td");
  const chat = rule.chat;
  const title = document.createElement(chat?.chatOpenUrl ? "a" : "strong");
  title.textContent = chat?.chatLabelResolved ? chat.chatLabel : rule.chat_id;
  if (chat?.chatOpenUrl) {
    title.className = "chat-link";
    title.href = chat.chatOpenUrl;
    title.target = "_blank";
    title.rel = "noopener noreferrer";
  }
  td.append(title);
  if (chat?.chatLabelResolved) {
    const id = document.createElement("div");
    id.className = "id";
    id.textContent = rule.chat_id;
    td.append(id);
  }
  row.append(td);
}

function defaultSettingsCell(row, settings) {
  const td = document.createElement("td");
  const model = settings.turn_settings;
  const title = document.createElement("div");
  title.textContent = model ? `${model.model_id} / ${model.effort_id} / ${model.service_tier_id}` : "继承 Codex";
  const detail = document.createElement("div");
  detail.className = "default-settings-detail";
  detail.textContent = scheduleSessionSummary(settings).split(" · ").slice(1).join(" · ");
  td.append(title, detail);
  row.append(td);
}

async function loadDefaults(offset = defaultsOffset, returnFocus = null) {
  const serial = ++defaultsListSerial;
  const [chat, group, projects] = await Promise.all([
    api(`/api/v1/defaults?${new URLSearchParams({ kind: "chat", offset: String(offset), limit: "50" })}`),
    api("/api/v1/defaults?kind=group_name&limit=200"), queryProjectOptions(),
  ]);
  if (serial !== defaultsListSerial) return;
  if (offset > 0 && !chat.items.length) return loadDefaults(Math.max(0, offset - 50), returnFocus);
  const focused = returnFocus || document.activeElement;
  const restoreFocus = !document.activeElement || document.activeElement === focused || document.activeElement === document.body;
  defaultsProjects = projects;
  defaultsOffset = offset;
  defaultsNeedsRefresh = false;
  state.defaults = { chat, group_name: group };
  for (const [kind, page, body] of [["chat", chat, defaultsInput("chat-body")], ["group_name", group, defaultsInput("rules-body")]]) {
    body.replaceChildren();
    page.items.forEach((rule, index) => {
      const row = document.createElement("tr");
      if (kind === "group_name") {
        cell(row, String(index + 1));
        cell(row, rule.keyword);
      } else defaultChatCell(row, rule);
      cell(row, rule.project);
      defaultSettingsCell(row, rule.session_settings);
      const actions = document.createElement("td");
      const buttons = document.createElement("div");
      buttons.className = "actions";
      buttons.append(
        defaultListButton("编辑", () => openDefaultEditor(kind, rule), { id: rule.id, disabled: !rule.actions.save }),
        defaultListButton("删除", () => deleteDefault(rule), { id: rule.id, disabled: !rule.actions.delete, danger: true }),
      );
      if (kind === "group_name") {
        buttons.append(
          defaultListButton("上移", () => moveDefaultRule(index, -1), { id: rule.id, disabled: index === 0 || page.has_more || !page.actions.reorder }),
          defaultListButton("下移", () => moveDefaultRule(index, 1), { id: rule.id, disabled: index === page.items.length - 1 || page.has_more || !page.actions.reorder }),
        );
      }
      actions.append(buttons);
      row.append(actions);
      body.append(row);
    });
    if (!page.items.length) {
      const row = document.createElement("tr");
      const empty = cell(row, "", "defaults-empty");
      empty.colSpan = kind === "chat" ? 4 : 5;
      const message = document.createElement("p");
      message.textContent = kind === "chat" ? "还没有聊天配置，为一个单聊或群聊设置会话默认值。" : "还没有群名规则，通过关键词为匹配的群聊设置会话默认值。";
      empty.append(message, defaultListButton(kind === "chat" ? "添加聊天配置" : "添加群名规则",
        () => openDefaultEditor(kind), { primary: true }));
      body.append(row);
    }
  }
  defaultsInput("pagination").hidden = offset === 0 && !chat.has_more;
  defaultsInput("page").textContent = `第 ${Math.floor(offset / 50) + 1} 页`;
  renderDefaultControls();
  if (restoreFocus && focused === defaultsInput("new")) {
    focused.focus({ preventScroll: true });
  } else if (restoreFocus && focused?.dataset.defaultAction) {
    const replacement = Array.from(defaultsInput(`panel-${defaultsTab}`).querySelectorAll("[data-default-action]"))
      .find((button) => button.dataset.defaultId === focused.dataset.defaultId
        && button.dataset.defaultAction === focused.dataset.defaultAction && !button.disabled);
    (replacement || defaultsInput("new")).focus({ preventScroll: true });
  }
}

function scheduleDate(value) {
  if (value == null) return "—";
  if (typeof value === "number") return new Date(value * 1000).toISOString();
  return String(value);
}

function scheduleRuleLabel(rule) {
  const kinds = { once: "一次性", daily: "每天", weekly: "每周", interval: "固定间隔" };
  const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const when = rule.kind === "interval" ? `每 ${rule.every_minutes} 分钟`
    : rule.kind === "weekly" ? `${(rule.weekdays || []).map((day) => days[day]).join("、")} ${rule.at}`
      : rule.kind === "once" ? String(rule.at).replace("T", " ") : rule.at;
  const labels = [`${kinds[rule.kind] || rule.kind} · ${when} · ${rule.timezone}`];
  if (rule.end_at) labels.push(`截止 ${scheduleLocalTime(rule.end_at)}（含）`);
  return labels.join(" · ");
}

function scheduleLocalTime(value) {
  return value ? String(value).replace("T", " ").replace(/(\d{2}:\d{2}):00(?=[+-]\d{2}:\d{2}$)/, "$1") : "—";
}

function schedulePlanState(plan) {
  return `启停：${plan.enabled ? "已启用" : "已暂停"}\n结束：${plan.lifecycle.ended ? "已结束" : "未结束"}`;
}

function scheduleRunStatus(status) {
  return { completed: "已完成", failed: "失败", interrupted: "已停止", inProgress: "执行中",
    starting: "启动中", unknown: "结果待确认", unavailable: "结果暂不可用",
    not_started: "尚未执行", expired: "已过期，未执行", missed: "已错过",
    skipped_busy: "上次仍在执行，本次跳过", blocked_unknown: "上次结果待确认，本次跳过",
    project_disabled: "Project 已停用，本次未执行", project_unavailable: "Project 不可用，本次未执行",
    released: "调度已收尾", recovery_no_start: "服务恢复，本次未启动",
    publishing_unknown: "话题投递待确认", deleted: "执行会话已删除",
    initial_start_rejected: "启动被拒绝", publishing_failed: "话题发布失败",
    scope_conflict: "话题已有会话，未启动", dispatch_rejected: "未启动",
    input_started: "已启动新一轮", input_steered: "已追加当前任务", input_unknown: "输入接收情况待确认",
    input_rejected: "输入被拒绝", anchor_failed: "触发消息发送失败", anchor_unknown: "触发消息发送结果待确认",
    target_inactive: "原会话已切走或归档，本次未执行", target_missing: "原会话不可用",
    target_mismatch: "原会话位置不匹配" }[status] || "结果暂不可用";
}

function scheduleTriggerSource(source) {
  return source === "manual" ? "手动触发" : source === "scheduled" ? "定时触发" : "";
}

function scheduleExecutionLabel(execution) {
  const label = scheduleRunStatus(execution.status);
  if (execution.kind === "none") return label;
  if (execution.status === "deleted") return label;
  if (execution.kind === "current") return `${execution.is_last ? "最后一次" : "本次"}${label}`;
  if (["skipped_busy", "blocked_unknown", "project_disabled", "project_unavailable"].includes(execution.status)) return label;
  return `上次${label}`;
}

function scheduleBlockedLabel(plan) {
  return { blocked_unknown: "执行结果待确认", project_disabled: "Project 已停用",
    project_unavailable: "Project 不可用", target_inactive: "原会话已切走或归档，自动暂停",
    target_missing: "原会话不可用", target_mismatch: "原会话位置不匹配" }[plan.blocked_reason] || "";
}

function scheduleTargetLabel(plan) {
  return plan.target_kind === "binding" ? `原会话 · ${plan.target_label || plan.target_binding_id}` : "每次新建独立话题";
}

function scheduleNote(parent, value) {
  const note = document.createElement("span");
  note.className = "schedule-note";
  note.textContent = value;
  parent.append(note);
}

let scheduleEditor = null;
let scheduleEditorSerial = 0;
let schedulePrepared = null;
let schedulePreviewSerial = 0;
let scheduleDetailSerial = 0;
let scheduleDetailId = null;
let scheduleRunsCursor = null;
let scheduleListCursor = null;
let scheduleListQuery = null;
let scheduleProjects = [];
let pendingScheduleDraft = null;
const pendingScheduleMutations = new Set();

function scheduleInput(id) { return document.querySelector(`#schedule-${id}`); }

function renderScheduleTarget() {
  const editor = scheduleEditor;
  if (!editor) return;
  const binding = scheduleInput("target-kind").value === "binding";
  scheduleInput("target-kind").disabled = Boolean(editor.plan || editor.targetSession);
  scheduleInput("binding-field").hidden = !binding;
  scheduleInput("target-note").hidden = !binding;
  scheduleInput("project-field").hidden = binding;
  scheduleInput("project").disabled = binding;
  scheduleInput("project").required = !binding;
  chatTarget("schedule").render();
  scheduleInput("chat-target").hidden = binding;
  if (binding) { scheduleInput("chat").required = false; chatTarget("schedule").close(); }
  scheduleInput("session-settings").hidden = binding;
  const target = editor.targetSession;
  const selected = editor.plan?.target_binding_id || target?.bindingId || "";
  scheduleInput("binding").value = selected;
  scheduleInput("select-session").hidden = Boolean(selected);
  scheduleInput("binding-summary").textContent = target
    ? `${target.nativeTitle || target.shortId || "未命名会话"}\n${target.chatLabel || target.chatId} · ${target.projectAlias}\n会话 ${target.shortId || target.bindingId.slice(0, 8)}${target.topicId ? ` · 话题 ${target.topicId}` : ""}`
      + (target.pointerState !== "current" || target.catalogState === "archived" ? "\n目标当前不是可执行的当前会话，计划将自动暂停；不会自动切换或恢复会话。" : "")
    : selected ? `${editor.plan.target_label || selected}\n${editor.plan.chat?.chatLabel || editor.plan.chat_id} · ${editor.plan.project_alias}`
      : "请前往 Sessions 找到具体会话，再点击该行的“创建定时任务”。已填写内容会在当前页面保留。";
  scheduleInput("preview").disabled = binding && !selected;
}

function renderScheduleProjects() {
  scheduleSelectOptions(scheduleInput("filter-project"), [["", "全部 Project"],
    ...scheduleProjects.map((project) => [project.alias,
      project.enabled ? project.alias : `${project.alias}（已停用）`])], scheduleInput("filter-project").value);
  if (!scheduleEditor) return;
  const selected = scheduleInput("project").value || scheduleEditor.plan?.project_alias || "";
  const available = scheduleProjects.filter((project) => project.enabled || project.alias === selected);
  scheduleSelectOptions(scheduleInput("project"), [["", "选择 Project"],
    ...available.map((project) => [project.alias,
      project.enabled ? project.alias : `${project.alias}（已停用）`])], selected);
}

function closeScheduleEditor({ saved = false } = {}) {
  if (scheduleInput("fields").disabled && !saved) return;
  const returnFocus = scheduleEditor?.returnFocus;
  scheduleEditorSerial += 1;
  chatTargetControllers.get("schedule")?.close();
  scheduleEditor = null;
  invalidateSchedulePreview();
  scheduleInput("editor").hidden = true;
  scheduleInput("drawer").close();
  document.body.classList.toggle("schedule-drawer-open", false);
  returnFocus?.focus();
}

function captureScheduleDraft() {
  const fields = ["name", "instructions", "enabled", "kind", "timezone", "time", "at", "offset", "end-at", "end-offset", "every"];
  return {
    fields: fields.map((id) => ({ id, value: scheduleInput(id).value, checked: scheduleInput(id).checked })),
    offsets: ["offset", "end-offset"].map((id) => ({ id, hidden: scheduleInput(`${id}-field`).hidden,
      options: Array.from(scheduleInput(id).querySelectorAll("option")).map((option) => [option.value, option.textContent]) })),
    weekdays: Array.from(scheduleInput("weekdays").querySelectorAll("input")).map((input) => input.checked),
  };
}

function restoreScheduleDraft(draft) {
  for (const field of draft.offsets) {
    scheduleSelectOptions(scheduleInput(field.id), field.options, "");
    scheduleInput(`${field.id}-field`).hidden = field.hidden;
  }
  for (const field of draft.fields) {
    scheduleInput(field.id).value = field.value;
    scheduleInput(field.id).checked = field.checked;
  }
  Array.from(scheduleInput("weekdays").querySelectorAll("input"))
    .forEach((input, index) => { input.checked = draft.weekdays[index]; });
  invalidateSchedulePreview();
}

function chooseScheduleSession() {
  if (!scheduleEditor || scheduleEditor.plan) return;
  pendingScheduleDraft = captureScheduleDraft();
  closeScheduleEditor();
  document.querySelector("#session-schedule-selection").hidden = false;
  document.querySelector("#session-schedule-saved").hidden = true;
  selectTab("sessions");
}

function cancelScheduleSelection() {
  pendingScheduleDraft = null;
  scheduleEditorSerial += 1;
  document.querySelector("#session-schedule-selection").hidden = true;
}

async function openScheduleForSession(session, returnFocus) {
  if (scheduleEditor || returnFocus.disabled) return;
  const serial = ++scheduleEditorSerial;
  const draft = pendingScheduleDraft;
  returnFocus.disabled = true;
  try {
    // Obtain a fresh management grant without changing either tab's list/filter state.
    const data = await api("/api/v1/schedules?mode=list");
    if (serial !== scheduleEditorSerial || state.tab !== "sessions") return;
    await openScheduleEditor(null, { targetSession: session, returnFocus, draft,
      action: data.actions.create, timezone: data.default_timezone });
    if (scheduleEditor?.targetSession === session) {
      pendingScheduleDraft = null;
      document.querySelector("#session-schedule-selection").hidden = true;
    }
  } catch (error) { setStatus(error.message, true); }
  finally { returnFocus.disabled = false; }
}

function changeScheduleChat(resolve = true) {
  const editor = scheduleEditor;
  if (!editor) return;
  editor.optionsSerial += 1;
  editor.contextAvailable = chatTarget("schedule").kind() === "p2p" ? false : null;
  if (editor.contextAvailable === false) {
    editor.sessionDraft.message_context_mode = "current-only";
    editor.settingsTouched = true;
  }
  invalidateSchedulePreview();
  renderScheduleSessionSettings();
  if (resolve && scheduleInput("chat").value.trim()) loadScheduleSessionOptions();
}

function scheduleRuleFingerprint() {
  return JSON.stringify({ rule: readScheduleRule(),
    utc_offset: scheduleInput("kind").value === "once" ? scheduleInput("offset").value : "",
    end_utc_offset: scheduleInput("kind").value !== "once" ? scheduleInput("end-offset").value : "" });
}

function resetScheduleOffset() {
  scheduleInput("offset-field").hidden = true;
  scheduleInput("offset").replaceChildren();
  scheduleInput("offset").value = "";
}

function resetScheduleEndOffset() {
  scheduleInput("end-offset-field").hidden = true;
  scheduleInput("end-offset").replaceChildren();
  scheduleInput("end-offset").value = "";
}

function defaultScheduleSessionSettings() {
  return { turn_settings: null, reaction_pulse_enabled: false, progress_card_enabled: true, completion_mention_enabled: true,
    message_context_mode: "current-only" };
}

function scheduleSessionSummary(settings) {
  const model = settings.turn_settings;
  const labels = [model ? `${model.model_id} / ${model.effort_id} / ${model.service_tier_id}` : "继承 Codex",
    settings.message_context_mode === "catch-up" ? "补齐未读上下文" : "当前消息"];
  if (settings.reaction_pulse_enabled) labels.push("表情反馈");
  if (settings.progress_card_enabled) labels.push("进度卡片");
  if (settings.completion_mention_enabled) labels.push("结束时 @ 提醒");
  return labels.join(" · ");
}

function scheduleSelectOptions(node, choices, selected) {
  node.replaceChildren();
  for (const [value, label] of choices) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    node.append(option);
  }
  if (selected && !choices.some(([value]) => value === selected)) {
    const option = document.createElement("option");
    option.value = selected;
    option.textContent = `${selected}（保留已存设置，当前目录不可用）`;
    node.append(option);
  }
  node.value = selected || "";
}

function renderSessionSettingsFields(input, settings, models, contextAvailable) {
  const turn = settings.turn_settings;
  const model = models.find((item) => item.id === turn?.model_id);
  scheduleSelectOptions(input("model"), [["", "继承 Codex"],
    ...models.map((item) => [item.id, item.display_name])], turn?.model_id);
  scheduleSelectOptions(input("effort"), (model?.efforts || []).map((item) => [item.id, item.id]), turn?.effort_id);
  scheduleSelectOptions(input("tier"), (model?.service_tiers || []).map((item) => [item.id, item.name || item.id]), turn?.service_tier_id);
  input("effort").disabled = !model;
  input("tier").disabled = !model;
  input("context").value = settings.message_context_mode;
  for (const option of input("context").querySelectorAll("option")) {
    option.disabled = option.value === "catch-up" && contextAvailable === false;
  }
  input("reactions").checked = settings.reaction_pulse_enabled;
  input("progress").checked = settings.progress_card_enabled;
  input("completion-mention").checked = settings.completion_mention_enabled;
  return model;
}

function changeSessionSettingsField(input, settings, models, field, baseTurn = null) {
  if (field === "model") {
    const id = input("model").value;
    const selected = models.find((item) => item.id === id);
    if (!id) settings.turn_settings = null;
    else if (selected) settings.turn_settings = { model_id: selected.id,
      effort_id: selected.default_effort_id, service_tier_id: selected.default_service_tier_id };
    else if (id === baseTurn?.model_id) settings.turn_settings = structuredClone(baseTurn);
  } else if (field === "effort" && settings.turn_settings) settings.turn_settings.effort_id = input("effort").value;
  else if (field === "tier" && settings.turn_settings) settings.turn_settings.service_tier_id = input("tier").value;
  else if (field === "context") settings.message_context_mode = input("context").value;
  else if (field === "reactions") settings.reaction_pulse_enabled = input("reactions").checked;
  else if (field === "progress") settings.progress_card_enabled = input("progress").checked;
  else if (field === "completion-mention") settings.completion_mention_enabled = input("completion-mention").checked;
}

function renderScheduleSessionSettings() {
  const editor = scheduleEditor;
  if (!editor) return;
  const settings = editor.sessionDraft;
  const current = settings.turn_settings;
  const model = renderSessionSettingsFields(scheduleInput, settings, editor.models, editor.contextAvailable);
  scheduleInput("context-field").hidden = chatTarget("schedule").kind() === "p2p"
    && settings.message_context_mode === "current-only";
  scheduleInput("session-summary").textContent = scheduleSessionSummary(settings);
  const notes = [];
  if (editor.catalogMessage) notes.push(editor.catalogMessage);
  if (current && !model && !editor.catalogMessage) notes.push("已保存的模型当前不可用，原配置保持不变。可调整其他配置，或显式选择新模型。");
  if (editor.contextAvailable === false) notes.push("私聊目标不支持补齐未读上下文；请选择当前消息后保存。");
  scheduleInput("session-note").textContent = notes.join(" ");
}

async function loadScheduleSessionOptions() {
  const editor = scheduleEditor;
  if (!editor) return;
  const chatId = scheduleInput("chat").value.trim();
  const serial = ++editor.optionsSerial;
  const query = new URLSearchParams({ mode: "options" });
  if (chatId && scheduleInput("target-kind").value !== "binding") query.set("chat_id", chatId);
  try {
    const data = await api(`/api/v1/schedules?${query}`);
    if (scheduleEditor !== editor || serial !== editor.optionsSerial) return;
    editor.models = data.models;
    editor.contextAvailable = data.context_mode_available;
    if (scheduleInput("target-kind").value !== "binding") chatTarget("schedule").resolveKind(editor.contextAvailable);
    editor.catalogMessage = data.model_catalog_error
      ? `${data.model_catalog_error.message} 已有配置保持不变，仍可调整其他项目。` : "";
    if (!editor.plan && !editor.settingsTouched) {
      if (JSON.stringify(editor.sessionDraft) !== JSON.stringify(data.session_settings)) invalidateSchedulePreview();
      editor.sessionBase = structuredClone(data.session_settings);
      editor.sessionDraft = structuredClone(data.session_settings);
    }
    renderScheduleSessionSettings();
    renderScheduleTarget();
  } catch (error) {
    if (scheduleEditor !== editor || serial !== editor.optionsSerial) return;
    editor.models = [];
    editor.contextAvailable = null;
    editor.catalogMessage = `${error.message} 可选配置暂未刷新，已有选择保持不变。`;
    renderScheduleSessionSettings();
    renderScheduleTarget();
  }
}

function changeScheduleSessionSettings(field) {
  const editor = scheduleEditor;
  if (!editor) return;
  editor.settingsTouched = true;
  changeSessionSettingsField(scheduleInput, editor.sessionDraft, editor.models, field, editor.sessionBase.turn_settings);
  invalidateSchedulePreview();
  renderScheduleSessionSettings();
}

function scheduleSessionPatch() {
  const patch = {};
  if (!scheduleEditor) return patch;
  if (scheduleInput("target-kind").value === "binding") return patch;
  if (!scheduleEditor.plan) return structuredClone(scheduleEditor.sessionDraft);
  for (const [key, value] of Object.entries(scheduleEditor.sessionDraft)) {
    if (JSON.stringify(value) !== JSON.stringify(scheduleEditor.sessionBase[key])) patch[key] = value;
  }
  return patch;
}

function readScheduleRule() {
  const kind = scheduleInput("kind").value;
  const rule = { kind, timezone: scheduleInput("timezone").value.trim() };
  if (kind === "once") rule.at = scheduleInput("at").value.trim();
  if (kind === "daily" || kind === "weekly") rule.at = scheduleInput("time").value;
  if (kind === "weekly") rule.weekdays = Array.from(scheduleInput("weekdays").querySelectorAll("input"))
    .filter((input) => input.checked).map((input) => Number(input.value));
  if (kind === "interval") {
    rule.every_minutes = Number(scheduleInput("every").value);
    const previous = scheduleEditor?.plan?.schedule;
    if (previous?.kind === "interval") {
      rule.anchor = previous.anchor;
    }
  }
  if (kind !== "once") {
    const endAt = scheduleInput("end-at").value.trim();
    if (endAt) rule.end_at = endAt;
  }
  return rule;
}

function invalidateSchedulePreview() {
  schedulePrepared = null;
  schedulePreviewSerial += 1;
  scheduleInput("save").disabled = true;
  scheduleInput("preview-times").replaceChildren();
  scheduleInput("preview-message").textContent = "保存前请预览触发时间。";
  for (const node of document.querySelectorAll("[data-schedule-rule]")) {
    node.hidden = !node.dataset.scheduleRule.split(" ").includes(scheduleInput("kind").value);
  }
  scheduleInput("end-condition").disabled = scheduleInput("kind").value === "once";
}

function renderSchedulePreview(selector, preview) {
  const list = document.querySelector(selector);
  list.replaceChildren();
  for (const item of preview || []) {
    const li = document.createElement("li");
    li.textContent = `${item.local}（UTC ${item.utc}）`;
    list.append(li);
  }
}

async function previewSchedule() {
  const serial = ++schedulePreviewSerial;
  schedulePrepared = null;
  scheduleInput("save").disabled = true;
  scheduleInput("preview-message").textContent = "正在计算触发时间…";
  scheduleInput("preview-message").classList.toggle("error", false);
  try {
    if (scheduleInput("target-kind").value === "binding" && !scheduleInput("binding").value) {
      throw new Error("请先前往 Sessions 选择具体会话。");
    }
    if (scheduleInput("target-kind").value !== "binding" && !scheduleInput("chat").value.trim()) {
      throw new Error("请选择聊天，或手动填写聊天 ID。");
    }
    const rule = readScheduleRule();
    const fingerprint = scheduleRuleFingerprint();
    const query = new URLSearchParams({ mode: "preview" });
    if (rule.kind === "once") {
      query.set("local_at", rule.at);
      delete rule.at;
      if (scheduleInput("offset").value) query.set("utc_offset", scheduleInput("offset").value);
    }
    if (rule.kind !== "once" && rule.end_at) {
      query.set("local_end_at", rule.end_at);
      delete rule.end_at;
      if (scheduleInput("end-offset").value) query.set("end_utc_offset", scheduleInput("end-offset").value);
    }
    query.set("schedule", JSON.stringify(rule));
    if (scheduleEditor?.plan) query.set("plan_id", scheduleEditor.plan.id);
    if (scheduleInput("target-kind").value === "binding") {
      query.set("target_kind", "binding");
      query.set("target_binding_id", scheduleInput("binding").value);
    } else {
      const chatId = scheduleInput("chat").value.trim();
      if (chatId) query.set("chat_id", chatId);
    }
    const settings = scheduleSessionPatch();
    if (Object.keys(settings).length) query.set("session_settings", JSON.stringify(settings));
    const data = await api(`/api/v1/schedules?${query}`);
    if (serial !== schedulePreviewSerial || fingerprint !== scheduleRuleFingerprint()) return;
    schedulePrepared = { fingerprint, schedule: data.schedule, hasFuture: Boolean(data.preview.length) };
    renderSchedulePreview("#schedule-preview-times", data.preview);
    scheduleInput("preview-message").textContent = data.preview.length
      ? `预计触发时间 · ${data.schedule.timezone}`
      : scheduleEditor?.plan ? "没有后续触发时间。仍可保存计划修改；已认领的执行可以继续。" : "没有后续触发时间，请调整规则。";
    scheduleInput("save").disabled = (!data.preview.length && !scheduleEditor?.plan) || !scheduleEditor?.action;
  } catch (error) {
    if (serial !== schedulePreviewSerial) return;
    if (["ambiguous_local_time", "ambiguous_end_time"].includes(error.code) && Array.isArray(error.choices)) {
      const field = error.code === "ambiguous_end_time" ? "end-offset" : "offset";
      scheduleSelectOptions(scheduleInput(field), [["", field === "end-offset" ? "选择截止时刻" : "选择触发时刻"],
        ...error.choices.map((choice, index) => [choice.utc_offset,
          `第 ${index + 1} 次 · UTC${choice.utc_offset}`])], "");
      scheduleInput(`${field}-field`).hidden = false;
    }
    scheduleInput("preview-message").textContent = error.message;
    scheduleInput("preview-message").classList.toggle("error", true);
  }
}

function openScheduleEditor(plan = null, source = {}) {
  if (scheduleInput("fields").disabled) return;
  scheduleEditorSerial += 1;
  const action = source.action || (plan ? plan.actions.update : state.schedules?.actions.create);
  if (!action) return;
  const settings = plan?.session_settings || defaultScheduleSessionSettings();
  scheduleEditor = { plan, action, sessionBase: structuredClone(settings), sessionDraft: structuredClone(settings),
    targetSession: source.targetSession || null, returnFocus: source.returnFocus || document.activeElement,
    fromSessions: Boolean(source.targetSession), models: [], contextAvailable: null,
    optionsSerial: 0, settingsTouched: false, catalogMessage: "正在读取模型目录…" };
  const rule = plan?.schedule || { kind: "daily", at: "09:00", timezone: source.timezone || state.schedules?.default_timezone || "" };
  scheduleInput("editor").hidden = false;
  scheduleInput("editor-title").textContent = plan ? `编辑计划 · ${plan.name}` : "创建计划";
  scheduleInput("name").value = plan?.name || "";
  scheduleInput("target-kind").value = source.targetSession ? "binding" : plan?.target_kind || "new_topic";
  scheduleInput("binding").value = plan?.target_binding_id || source.targetSession?.bindingId || "";
  scheduleInput("project").value = plan?.project_alias || "";
  renderScheduleProjects();
  chatTarget("schedule", changeScheduleChat).set({ chatId: plan?.chat_id, chat: plan?.chat });
  scheduleInput("instructions").value = plan?.instructions || "";
  scheduleInput("enabled").checked = plan?.enabled ?? true;
  scheduleInput("timezone").value = rule.timezone;
  scheduleInput("kind").value = rule.kind;
  scheduleInput("time").value = ["daily", "weekly"].includes(rule.kind) ? rule.at : "09:00";
  scheduleInput("at").value = plan?.once_local_at || "";
  resetScheduleOffset();
  scheduleInput("end-at").value = plan?.end_local_at || "";
  resetScheduleEndOffset();
  scheduleInput("every").value = String(rule.every_minutes || 60);
  for (const input of scheduleInput("weekdays").querySelectorAll("input")) {
    input.checked = (rule.weekdays || []).includes(Number(input.value));
  }
  if (source.draft) restoreScheduleDraft(source.draft);
  invalidateSchedulePreview();
  scheduleInput("session-settings").open = true;
  renderScheduleSessionSettings();
  renderScheduleTarget();
  scheduleInput("preview-message").classList.toggle("error", false);
  scheduleInput("close").disabled = false;
  if (!scheduleInput("drawer").open) scheduleInput("drawer").showModal();
  document.body.classList.toggle("schedule-drawer-open", true);
  scheduleInput("name").focus();
  scheduleInput("editor").querySelector(".schedule-editor-body").scrollTop = 0;
  return loadScheduleSessionOptions();
}

async function editSchedule(plan) {
  const serial = ++scheduleEditorSerial;
  try {
    const data = await api(`/api/v1/schedules?${new URLSearchParams({ mode: "view", plan_id: plan.id })}`);
    if (serial !== scheduleEditorSerial) return;
    await openScheduleEditor(data.plan);
  } catch (error) { setStatus(error.message, true); }
}

async function saveSchedule(event) {
  event.preventDefault();
  if (scheduleInput("fields").disabled || !scheduleEditor?.action || !schedulePrepared
      || schedulePrepared.fingerprint !== scheduleRuleFingerprint()) return;
  const editor = scheduleEditor;
  const prepared = schedulePrepared;
  if (scheduleInput("target-kind").value !== "binding") {
    scheduleInput("fields").disabled = true;
    scheduleInput("close").disabled = true;
    try { await chatTarget("schedule").validate(); }
    catch (error) {
      invalidateSchedulePreview();
      scheduleInput("preview-message").textContent = error.message;
      scheduleInput("preview-message").classList.toggle("error", true);
      return;
    } finally {
      scheduleInput("fields").disabled = false;
      scheduleInput("close").disabled = false;
    }
    if (scheduleEditor !== editor) return;
  }
  // A late options response may replace defaults and invalidate the preview
  // while group validation is in flight. Never submit that stale confirmation.
  if (schedulePrepared !== prepared || prepared.fingerprint !== scheduleRuleFingerprint()) {
    invalidateSchedulePreview();
    scheduleInput("preview-message").textContent = "配置已更新，请重新预览后保存。";
    scheduleInput("preview-message").classList.toggle("error", true);
    return;
  }
  scheduleEditorSerial += 1;
  const action = editor.action;
  const hasFuture = prepared.hasFuture;
  const definition = {
    name: scheduleInput("name").value.trim(),
    instructions: scheduleInput("instructions").value.trim(),
    schedule: prepared.schedule,
    enabled: scheduleInput("enabled").checked,
  };
  if (scheduleInput("target-kind").value === "binding") {
    if (!editor.plan) {
      definition.target_kind = "binding";
      definition.target_binding_id = scheduleInput("binding").value;
    }
  } else {
    definition.project = scheduleInput("project").value.trim();
    definition.chat_id = scheduleInput("chat").value.trim();
  }
  const settings = scheduleSessionPatch();
  if (Object.keys(settings).length) definition.session_settings = settings;
  scheduleEditor.action = null;
  scheduleInput("fields").disabled = true;
  scheduleInput("close").disabled = true;
  let saved = false;
  try {
    await api(`/api/v1/schedules/${editor.plan ? "update" : "create"}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(action, { definition })),
    });
    saved = true;
    closeScheduleEditor({ saved: true });
  } catch (error) {
    scheduleInput("preview-message").textContent = `${error.message} 请刷新列表并重新打开编辑，核查后再操作。`;
    scheduleInput("preview-message").classList.toggle("error", true);
    setStatus(error.message, true);
    await refresh("schedules");
    setStatus(error.message, true);
  } finally {
    scheduleInput("fields").disabled = false;
    scheduleInput("close").disabled = false;
    scheduleInput("save").disabled = true;
  }
  if (saved) {
    if (editor.fromSessions) {
      document.querySelector("#session-schedule-saved").hidden = false;
      editor.returnFocus?.focus();
      setStatus("已为所选会话创建定时任务；执行时仍会检查会话是否当前且可用。");
      return;
    }
    try {
      await loadSchedules(scheduleListCursor, scheduleListQuery);
      const trigger = Array.from(document.querySelectorAll("[data-schedule-edit]"))
        .find((button) => button.dataset.scheduleEdit === editor.plan?.id);
      (trigger || scheduleInput("new")).focus();
      setStatus(hasFuture ? "已保存计划，将按时向所选目标提交任务。"
        : "已保存计划。当前没有后续触发，已认领的执行可以继续。");
    } catch (error) {
      scheduleInput("new").focus();
      setStatus(`计划已保存，但列表刷新失败：${error.message} 请刷新列表查看。`, true);
    }
  }
}

async function changeSchedule(plan, mode) {
  if (pendingScheduleMutations.has(plan.id)) return;
  if (mode === "delete" && !window.confirm(
    `删除计划「${plan.name}」？\n\n后续不再触发。已认领的执行可以继续，历史话题和普通会话保留。`,
  )) return;
  const action = plan.actions[mode];
  if (!action) return;
  pendingScheduleMutations.add(plan.id);
  plan.actions[mode] = null;
  const definition = mode === "update" ? { definition: { enabled: !plan.enabled } } : {};
  try {
    await api(`/api/v1/schedules/${mode}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(action, definition)),
    });
    if (scheduleDetailId === plan.id) {
      scheduleDetailSerial += 1;
      scheduleDetailId = null;
      scheduleInput("detail").hidden = true;
    }
    pendingScheduleMutations.delete(plan.id);
    await loadSchedules();
    setStatus(mode === "delete" ? "计划已删除，历史会话保留。" : plan.enabled ? "计划已暂停。" : "计划已启用。");
  } catch (error) {
    pendingScheduleMutations.delete(plan.id);
    await refresh("schedules");
    setStatus(`${error.message} 请先核查最新状态。`, true);
  } finally { pendingScheduleMutations.delete(plan.id); }
}

async function runSchedule(plan, button) {
  const action = plan.actions.run_now;
  if (!action || pendingScheduleMutations.has(plan.id)) return;
  pendingScheduleMutations.add(plan.id);
  plan.actions.run_now = null;
  if (button) button.disabled = true;
  scheduleInput("run-receipt").hidden = true;
  let receipt;
  try {
    receipt = await api("/api/v1/schedules/run-now", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(action)),
    });
  } catch (error) {
    setStatus(`${error.message} 请先查看最近记录核查触发结果，再决定是否重新运行。`, true);
    return;
  } finally { pendingScheduleMutations.delete(plan.id); }

  const message = `已受理「${plan.name}」的手动运行，原定时安排保持不变。`;
  const container = scheduleInput("run-receipt");
  container.replaceChildren();
  container.textContent = `${message} 执行 ID：${receipt.run_id}。`;
  if (receipt.run?.feishu_url) {
    const link = document.createElement("a");
    link.href = receipt.run.feishu_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "打开执行话题";
    container.append(link);
  }
  container.append(actionButton("查看最近记录", () => showSchedule(plan.id)));
  container.hidden = false;
  setStatus(message);
  try {
    await loadSchedules(scheduleListCursor, scheduleListQuery);
    if (scheduleDetailId === plan.id) await showSchedule(plan.id);
  } catch (error) {
    setStatus(`${message} 列表刷新失败：${error.message} 请手动刷新查看执行进展。`);
  }
}

async function loadSchedules(cursor = null, appliedQuery = cursor ? scheduleListQuery : null) {
  const query = new URLSearchParams(appliedQuery || "");
  if (appliedQuery === null) {
    for (const [name, value] of new FormData(scheduleInput("filter"))) {
      if (String(value).trim()) query.set(name, String(value).trim());
    }
  }
  const queryIdentity = query.toString();
  if (cursor) query.set("cursor", cursor);
  const [data, projects] = await Promise.all([
    api(`/api/v1/schedules?${query}`), queryProjectOptions(),
  ]);
  state.schedules = data;
  scheduleProjects = projects;
  scheduleListCursor = cursor;
  scheduleListQuery = queryIdentity;
  renderScheduleProjects();
  const body = document.querySelector("#schedules-body");
  body.replaceChildren();
  for (const plan of data.plans) {
    const row = document.createElement("tr");
    cell(row, plan.name);
    cell(row, plan.project_alias);
    const target = document.createElement("td");
    target.className = "schedule-target";
    const link = document.createElement("a");
    link.className = "chat-link";
    link.href = plan.chat.chatOpenUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = plan.chat.chatLabelResolved && plan.chat.chatLabel?.trim() && plan.chat.chatLabel !== "?"
      ? plan.chat.chatLabel : "会话名称暂不可用";
    link.title = `打开飞书会话 · ${plan.chat_id}`;
    target.append(link);
    scheduleNote(target, scheduleTargetLabel(plan));
    row.append(target);
    const timing = document.createElement("td");
    timing.className = "schedule-time";
    timing.textContent = scheduleRuleLabel(plan.schedule);
    scheduleNote(timing, !plan.lifecycle.has_trigger ? "无后续触发"
      : plan.next_due_local ? `下次：${scheduleLocalTime(plan.next_due_local)}`
        : plan.enabled ? "下次：等待触发" : "下次：已暂停");
    row.append(timing);
    const planState = document.createElement("td");
    planState.className = "schedule-state";
    planState.textContent = schedulePlanState(plan);
    const blocked = scheduleBlockedLabel(plan);
    if (blocked) scheduleNote(planState, blocked);
    row.append(planState);
    const execution = document.createElement("td");
    execution.className = "schedule-execution";
    execution.textContent = scheduleExecutionLabel(plan.execution);
    const source = scheduleTriggerSource(plan.execution.trigger_source);
    if (source) scheduleNote(execution, source);
    if (plan.execution.due_local) scheduleNote(execution, scheduleLocalTime(plan.execution.due_local));
    row.append(execution);
    const actions = actionsCell(row);
    actions.append(actionButton("详情 / 最近记录", () => showSchedule(plan.id)));
    const edit = actionButton("编辑", () => editSchedule(plan));
    edit.setAttribute("data-schedule-edit", plan.id);
    actions.append(edit);
    const run = actionButton("立即运行", () => runSchedule(plan, run));
    run.disabled = !plan.actions.run_now;
    run.title = scheduleBlockedLabel(plan) || (plan.inflight ? "本次执行尚未结束" : "按已保存的指令和配置运行一次，原定时安排保持不变");
    actions.append(run);
    if (plan.lifecycle.has_trigger) {
      actions.append(actionButton(plan.enabled ? "暂停" : "启用", () => changeSchedule(plan, "update")));
    }
    actions.append(actionButton("删除", () => changeSchedule(plan, "delete"), true));
    for (const button of actions.querySelectorAll("button")) button.disabled ||= pendingScheduleMutations.has(plan.id);
    body.append(row);
  }
  if (!data.plans.length) {
    const row = document.createElement("tr");
    const empty = document.createElement("td");
    empty.colSpan = 7;
    empty.textContent = "没有符合筛选条件的计划。可调整结束状态查看历史计划，或创建新计划。";
    row.append(empty);
    body.append(row);
  }
  document.querySelector("#schedules-next").hidden = !data.next_cursor;
}

async function showSchedule(planId) {
  const serial = ++scheduleDetailSerial;
  scheduleDetailId = planId;
  try {
    const data = await api(`/api/v1/schedules?${new URLSearchParams({ mode: "view", plan_id: planId })}`);
    if (serial !== scheduleDetailSerial) return;
    scheduleInput("detail").hidden = false;
    scheduleInput("detail-title").textContent = `${data.plan.name} · ${data.plan.id} · 修订 ${data.plan.revision}`;
    scheduleInput("detail-state").textContent = `${schedulePlanState(data.plan).replace("\n", " · ")} · ${scheduleExecutionLabel(data.plan.execution)}`
      + (scheduleTriggerSource(data.plan.execution.trigger_source) ? ` · ${scheduleTriggerSource(data.plan.execution.trigger_source)}` : "")
      + (scheduleBlockedLabel(data.plan) ? ` · ${scheduleBlockedLabel(data.plan)}` : "")
      + ` · 目标会话 ID：${data.plan.chat_id}`
      + (data.inflight ? "。编辑、暂停或删除不会取消本次执行。" : "");
    scheduleInput("detail-instructions").textContent = data.plan.instructions;
    scheduleInput("detail-settings").textContent = data.plan.target_kind === "binding"
      ? `${scheduleTargetLabel(data.plan)} · 沿用原会话当前配置；系统输入不新增 @ 通知。`
      : scheduleSessionSummary(data.plan.session_settings || defaultScheduleSessionSettings());
    scheduleInput("detail-rule").textContent = scheduleRuleLabel(data.plan.schedule);
    renderSchedulePreview("#schedule-detail-preview", data.preview);
    await loadScheduleRuns();
  } catch (error) { if (serial === scheduleDetailSerial) setStatus(error.message, true); }
}

async function loadScheduleRuns(cursor = null) {
  const planId = scheduleDetailId;
  const query = new URLSearchParams({ mode: "runs", plan_id: planId });
  if (cursor) query.set("cursor", cursor);
  try {
    const data = await api(`/api/v1/schedules?${query}`);
    if (planId !== scheduleDetailId) return;
    const body = scheduleInput("runs-body");
    body.replaceChildren();
    for (const run of data.runs) {
      const row = document.createElement("tr");
      cell(row, scheduleDate(run.due_at));
      cell(row, scheduleTriggerSource(run.trigger_source));
      cell(row, scheduleRunStatus(run.status || run.stage));
      const links = actionsCell(row);
      if (run.feishu_url) {
        const link = document.createElement("a");
        link.href = run.feishu_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = run.target_kind === "binding" ? "打开触发消息" : "打开飞书话题";
        links.append(link);
      }
      if (run.binding_id && !run.binding_removed) {
        const link = document.createElement("a");
        link.href = `/?${new URLSearchParams({ binding_id: run.binding_id })}`;
        link.textContent = "打开 Session";
        links.append(link);
      }
      body.append(row);
    }
    scheduleRunsCursor = data.next_cursor;
    scheduleInput("runs-next").hidden = !scheduleRunsCursor;
  } catch (error) { setStatus(error.message, true); }
}

// Page-owned Session objects bound retry state to the current inventory. A
// refresh (including unarchive) gets new objects and an immediate fresh read.
const runtimeResolutionRetries = new WeakMap();

function recordRuntimeResolutionFailure(session, activityRevision) {
  const previous = runtimeResolutionRetries.get(session);
  const failures = previous?.activityRevision === activityRevision
    ? Math.min(previous.failures + 1, 4) : 1;
  runtimeResolutionRetries.set(session, {
    activityRevision,
    failures,
    nextAt: performance.now() + Math.min(10000 * 2 ** (failures - 1), 60000),
  });
}

function mergeDeferredBindingRuntime(incoming, previous) {
  const needsResolution = !previous
    || incoming.activityRevision !== previous.activityRevision
    || previous.primaryStatusResolution === "deferred"
    || previous.primaryStatusResolution === "unavailable";
  if (!needsResolution) {
    return {
      runtime: {
        ...incoming,
        primaryStatus: previous.primaryStatus,
        primaryStatusResolution: previous.primaryStatusResolution,
      },
      needsResolution: false,
    };
  }
  return {
    runtime: {
      ...incoming,
      // Commit a new revision only after its exact projection succeeds. This
      // leaves a failed/timeout follow-up eligible for the next bounded poll.
      activityRevision: previous?.activityRevision ?? incoming.activityRevision,
      primaryStatus: previous?.primaryStatus ?? null,
      // A background retry must not turn an unavailable result into a
      // visible loading state on every poll. A changed known value remains
      // explicitly unconfirmed until its exact projection succeeds.
      primaryStatusResolution: previous?.primaryStatusResolution === "unavailable"
        ? "unavailable" : "deferred",
    },
    needsResolution: true,
  };
}

function applyRuntimeSnapshots(payload, resolveChanges = true) {
  const bindings = new Map(payload.bindings.map((item) => [item.bindingId, item]));
  const changedBindings = [];
  for (const session of state.sessions?.items || []) {
    let incoming = bindings.get(session.bindingId);
    if (!incoming) continue;
    // The inventory already confirmed archival. Keep local activity visible
    // until it clears, then stop resolving/polling this row without inventing
    // idle or reading a historical Goal.
    if (session.catalogState === "archived" && incoming.primaryStatusResolution !== "local") {
      incoming = { ...incoming, primaryStatus: null, primaryStatusResolution: "archived" };
    }
    const previous = session.runtime;
    let runtime = incoming;
    if (incoming.primaryStatusResolution === "deferred") {
      const merged = mergeDeferredBindingRuntime(incoming, previous);
      runtime = merged.runtime;
      let retry = runtimeResolutionRetries.get(session);
      if (retry && retry.activityRevision !== incoming.activityRevision) {
        runtimeResolutionRetries.delete(session);
        retry = null;
      }
      if (resolveChanges && merged.needsResolution && (!retry || performance.now() >= retry.nextAt)) {
        changedBindings.push(session.bindingId);
      }
    } else if (incoming.primaryStatusResolution === "unavailable") {
      runtime = {
        ...incoming,
        activityRevision: previous?.activityRevision ?? incoming.activityRevision,
        primaryStatus: previous?.primaryStatus ?? null,
      };
      recordRuntimeResolutionFailure(session, incoming.activityRevision);
    } else {
      runtimeResolutionRetries.delete(session);
    }
    session.runtime = runtime;
    const row = rowByIdentity("#sessions-body", "bindingId", session.bindingId);
    const node = row?.querySelector(".runtime-state");
    if (node) updateRuntimeCell(node, runtime);
  }

  const sides = new Map(payload.sides.map((item) => [item.sideId, item]));
  const missing = new Set(payload.missingSideIds);
  for (const side of state.sides?.items || []) {
    if (!sides.has(side.sideId) && !missing.has(side.sideId)) continue;
    side.runtime = sides.get(side.sideId) || null;
    const row = rowByIdentity("#sides-body", "sideId", side.sideId);
    const node = row?.querySelector(".runtime-state span");
    if (node) node.textContent = sideRuntimeLabel(side.runtime);
  }
  return changedBindings;
}

async function refresh(tab, cursor = undefined) {
  setStatus("正在读取服务端事实…");
  try {
    if (tab === "projects") await loadProjects(cursor || null);
    if (tab === "sessions") {
      if (cursor === undefined) await loadSessions();
      else await loadSessions(cursor);
    }
    if (tab === "side-topics") await loadSides(cursor || null);
    if (tab === "updates") await loadUpdates();
    if (tab === "schedules") await loadSchedules(cursor || null);
    if (tab === "defaults") await loadDefaults();
    setStatus("已更新。");
    return true;
  } catch (error) {
    setStatus(error.message, true);
    return false;
  }
}

function selectTab(name) {
  state.tab = name;
  sessionChatPicker?.close();
  stopUpdatePolling();
  // Only a navigation preference survives re-login; operation facts always
  // come from the server, and no credential or session token is stored here.
  try {
    if (name === "updates") sessionStorage.setItem("netizen-admin-updates-view", "1");
    else sessionStorage.removeItem("netizen-admin-updates-view");
  } catch (_error) { /* Storage may be disabled by browser policy. */ }
  for (const item of document.querySelectorAll(".tab")) {
    const active = item.dataset.tab === name;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
  }
  for (const panel of document.querySelectorAll(".panel")) {
    const active = panel.id === name;
    panel.hidden = !active;
    panel.classList.toggle("active", active);
  }
  refresh(name);
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
}

for (const button of document.querySelectorAll("[data-refresh]")) {
  button.addEventListener("click", () => refresh(button.dataset.refresh));
}

document.querySelector("#register-project").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  await mutate("/api/v1/projects/register", state.projects.actions.register, { alias: form.get("alias"), path: form.get("path") });
  formElement.reset();
});

document.querySelector("#create-project").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  await mutate("/api/v1/projects/create-directory", state.projects.actions.createDirectory, { alias: form.get("alias"), path: form.get("path") || null });
  formElement.reset();
});

document.querySelector("#session-filter").addEventListener("submit", (event) => {
  event.preventDefault();
  resetSessionPagination();
  refresh("sessions");
});
document.querySelector("#session-filter-reset").addEventListener("click", resetSessionFilters);
document.querySelector("#session-page-size").addEventListener("change", () => {
  resetSessionPagination();
  refresh("sessions");
});
document.querySelector("#side-filter").addEventListener("submit", (event) => { event.preventDefault(); refresh("side-topics"); });
document.querySelector("#projects-next").addEventListener("click", () => refresh("projects", state.projectCursor));
document.querySelector("#sessions-previous").addEventListener("click", () => moveSessionPage("previous"));
document.querySelector("#sessions-next").addEventListener("click", () => moveSessionPage("next"));
document.querySelector("#sides-next").addEventListener("click", () => refresh("side-topics", state.sideCursor));
sessionSettingsInput("cancel").addEventListener("click", closeSessionSettingsEditor);
sessionSettingsInput("close").addEventListener("click", closeSessionSettingsEditor);
sessionSettingsInput("drawer").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeSessionSettingsEditor();
});
sessionSettingsInput("editor").addEventListener("submit", saveSessionSettings);
for (const field of ["model", "effort", "tier", "context", "reactions", "progress", "completion-mention"]) {
  sessionSettingsInput(field).addEventListener("change", () => changeSessionSettings(field));
}
defaultsInput("new").addEventListener("click", () => openDefaultEditor(defaultsTab));
for (const kind of ["chat", "group_name"]) {
  defaultsInput(`tab-${kind}`).addEventListener("click", () => selectDefaultTab(kind));
  defaultsInput(`tab-${kind}`).addEventListener("keydown", (event) => {
    if (defaultsBusy || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === "Home" ? "chat" : event.key === "End" ? "group_name" : kind === "chat" ? "group_name" : "chat";
    selectDefaultTab(target);
    defaultsInput(`tab-${target}`).focus();
  });
}
defaultsInput("cancel").addEventListener("click", closeDefaultEditor);
defaultsInput("close").addEventListener("click", closeDefaultEditor);
defaultsInput("drawer").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeDefaultEditor();
});
defaultsInput("editor").addEventListener("submit", saveDefault);
defaultsInput("chat").addEventListener("change", () => changeDefaultChat());
for (const field of ["model", "effort", "tier", "context", "reactions", "progress", "completion-mention"]) {
  defaultsInput(field).addEventListener("change", () => changeDefaultSettings(field));
}
for (const [name, delta] of [["previous", -50], ["next", 50]]) {
  defaultsInput(name).addEventListener("click", async () => {
    try { await loadDefaults(Math.max(0, defaultsOffset + delta)); } catch (error) { setStatus(error.message, true); }
  });
}
scheduleInput("filter").addEventListener("submit", (event) => { event.preventDefault(); refresh("schedules"); });
scheduleInput("new").addEventListener("click", () => openScheduleEditor());
scheduleInput("preview").addEventListener("click", previewSchedule);
scheduleInput("editor").addEventListener("input", invalidateSchedulePreview);
scheduleInput("kind").addEventListener("change", invalidateSchedulePreview);
scheduleInput("editor").addEventListener("submit", saveSchedule);
scheduleInput("chat").addEventListener("change", loadScheduleSessionOptions);
scheduleInput("target-kind").addEventListener("change", () => {
  invalidateSchedulePreview();
  renderScheduleTarget();
  loadScheduleSessionOptions();
});
scheduleInput("select-session").addEventListener("click", chooseScheduleSession);
document.querySelector("#session-schedule-cancel").addEventListener("click", cancelScheduleSelection);
document.querySelector("#session-schedule-view").addEventListener("click", () => selectTab("schedules"));
for (const field of ["model", "effort", "tier", "context", "reactions", "progress", "completion-mention"]) {
  scheduleInput(field).addEventListener("change", () => changeScheduleSessionSettings(field));
}
scheduleInput("cancel").addEventListener("click", () => closeScheduleEditor());
scheduleInput("close").addEventListener("click", () => closeScheduleEditor());
scheduleInput("drawer").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeScheduleEditor();
});
for (const field of ["at", "timezone", "kind"]) {
  scheduleInput(field).addEventListener("input", resetScheduleOffset);
}
for (const field of ["end-at", "timezone", "kind"]) {
  scheduleInput(field).addEventListener("input", resetScheduleEndOffset);
}
document.querySelector("#schedules-next").addEventListener("click", () => refresh("schedules", state.schedules.next_cursor));
scheduleInput("runs-next").addEventListener("click", () => loadScheduleRuns(scheduleRunsCursor));
document.querySelector("#service-restart").addEventListener("click", restartService);
document.querySelector("#logout").addEventListener("click", async () => { await api("/logout", { method: "POST" }); window.location.assign("/login"); });

function chunkValues(values, size = 50) {
  const chunks = [];
  for (let index = 0; index < values.length; index += size) {
    chunks.push(values.slice(index, index + size));
  }
  return chunks;
}

async function fetchRuntimeSnapshots(bindingIds, sideIds, resolvePrimary = false) {
  const paths = [];
  for (const ids of chunkValues(bindingIds)) {
    const query = new URLSearchParams({ bindingIds: ids.join(",") });
    if (resolvePrimary) query.set("resolvePrimary", "true");
    paths.push(`/api/v1/runtime-snapshots?${query}`);
  }
  for (const ids of chunkValues(sideIds)) {
    const query = new URLSearchParams({ sideIds: ids.join(",") });
    paths.push(`/api/v1/runtime-snapshots?${query}`);
  }
  const payloads = [];
  if (resolvePrimary) {
    for (const path of paths) payloads.push(await api(path));
  } else {
    payloads.push(...await Promise.all(paths.map((path) => api(path))));
  }
  return {
    bindings: payloads.flatMap((payload) => payload.bindings),
    sides: payloads.flatMap((payload) => payload.sides),
    missingSideIds: payloads.flatMap((payload) => payload.missingSideIds),
  };
}

let runtimePollInFlight = false;

setInterval(async () => {
  if (
    runtimePollInFlight
    || document.hidden
    || (state.tab !== "sessions" && state.tab !== "side-topics")
  ) return;
  const tab = state.tab;
  const page = tab === "sessions" ? state.sessions : state.sides;
  const isCurrentPage = () => state.tab === tab
    && (tab === "sessions" ? state.sessions : state.sides) === page;
  const bindingIds = state.tab === "sessions"
    ? state.sessions?.items?.filter((item) => item.runtime.primaryStatusResolution !== "archived")
      .map((item) => item.bindingId) || []
    : [];
  const sideIds = state.tab === "side-topics"
    ? state.sides?.items?.map((item) => item.sideId) || []
    : [];
  if (!bindingIds.length && !sideIds.length) return;
  runtimePollInFlight = true;
  let resolving = [];
  try {
    const snapshots = await fetchRuntimeSnapshots(bindingIds, sideIds);
    if (!isCurrentPage() || document.hidden) return;
    const changedBindings = applyRuntimeSnapshots(snapshots);
    if (changedBindings.length) {
      const changed = new Set(changedBindings);
      resolving = snapshots.bindings.filter((item) => changed.has(item.bindingId));
      const resolved = await fetchRuntimeSnapshots(changedBindings, [], true);
      if (!isCurrentPage()) return;
      applyRuntimeSnapshots(resolved, false);
    }
  } catch (error) {
    if (!isCurrentPage()) return;
    if (resolving.length) {
      applyRuntimeSnapshots({
        bindings: resolving.map((item) => ({
          ...item, primaryStatus: null, primaryStatusResolution: "unavailable",
        })),
        sides: [], missingSideIds: [],
      }, false);
    }
    setStatus(error.message, true);
  } finally {
    runtimePollInFlight = false;
  }
}, 5000);

let initialTab = "projects";
try {
  if (sessionStorage.getItem("netizen-admin-updates-view") === "1") initialTab = "updates";
} catch (_error) { /* The update page remains available without storage. */ }
const linkedBindingId = new URLSearchParams(window.location.search).get("binding_id");
if (linkedBindingId) {
  resetSessionPagination();
  state.sessionPage.query = new URLSearchParams({ identity: linkedBindingId, pageSize: "20" }).toString();
  initialTab = "sessions";
}
selectTab(initialTab);
