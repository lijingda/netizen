"use strict";

const state = {
  tab: "projects",
  projects: null,
  sessions: null,
  sides: null,
  updates: null,
  schedules: null,
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
  recovered: "已通过安装器恢复",
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
  manual_recovery: "安装器已恢复部署，请以当前版本为准；原操作不标记为成功。",
};
const updatePollLimitMs = 5 * 60 * 1000;
let updatePollTimer = null;
let updatePollStarted = null;
let updatePollDelay = 2000;
let updatePollExpired = false;
let updateSubmitting = false;
let updateExpectedTarget = null;
let updateExpectedKind = null;
let updatePriorOperationId = null;
let updateDisconnected = false;
let updateReading = false;

function sameUpdateTarget(left, right, kind) {
  if (kind === "restart") {
    return left?.resource === "instance-restart" && typeof left.targetId === "string"
      && left.targetId === right?.releaseDigest;
  }
  return left != null && right != null
    && ["version", "releaseId", "installerSha256", "archiveSha256"]
      .every((key) => left[key] === right[key]);
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
    published: "官方发布版本", source: "源码安装", unmanaged: "非受管安装",
  }[data.current.source] || "安装来源未知";
  document.querySelector("#update-latest").textContent = data.latest?.version || "尚未检查";
  const versionMessage = !data.latest ? "点击检查更新，获取最新官方版本。"
    : data.current.version === data.latest.version ? "当前已是最新版本。"
      : data.available ? "发现新版本。" : "当前暂不可升级，请查看升级状态或安装说明。";
  document.querySelector("#update-message").textContent = data.checkingError
    || data.message || versionMessage;
  document.querySelector("#update-notes").textContent = data.latest?.notes || "暂无发布说明。";
  const releaseLink = document.querySelector("#update-release-link");
  releaseLink.hidden = !data.latest?.url;
  if (data.latest?.url) releaseLink.href = data.latest.url;
  const operation = data.operation;
  const restarting = (updateExpectedKind || operation?.kind) === "restart";
  const operationName = restarting ? "重启" : "升级";
  let phase = operation
    ? (updatePhaseLabels[operation.phase] || "维护结果未确认").replace("升级", operationName)
    : "尚未执行维护操作";
  let detail = operation
    ? `${restarting ? "保持版本" : "目标版本"} ${operation.target.version}。${
      updateCodeMessages[operation.code] || ""}`
    : "升级或重启开始后，关闭页面不会取消操作。";
  if (operation?.phase === "succeeded") {
    detail += restarting ? " 服务管理器已完成重启，服务就绪已确认。" : " 安装器已确认升级完成。";
  }
  if (updateExpectedTarget) {
    phase = updateSubmitting ? `正在提交${operationName}` : `${operationName}提交结果尚未确认`;
    detail = "正在查询服务端记录，请勿重复提交升级或重启。";
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
  document.querySelector("#update-check").disabled = updateSubmitting || updateReading
    || !data.actions?.check || updateNeedsPolling();
  document.querySelector("#update-install").disabled = updateSubmitting || updateReading
    || !data.supported || !data.available || !data.actions?.install
    || updateNeedsPolling() || updatePollExpired || updateDisconnected;
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
  if ((data.operation?.kind || "upgrade") === updateExpectedKind
      && sameUpdateTarget(updateExpectedTarget, data.operation?.target, updateExpectedKind)
      && data.operation.operationId !== updatePriorOperationId) {
    updateExpectedTarget = null;
    updateExpectedKind = null;
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

async function checkUpdate() {
  if (updateSubmitting || updateReading || updateNeedsPolling()) return;
  const envelope = state.updates?.actions?.check;
  if (!envelope) return;
  updateSubmitting = true;
  renderUpdates();
  setStatus("正在检查官方发布版本…");
  try {
    acceptUpdateStatus(await updateApi("/api/v1/updates/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(envelope)),
    }));
    setStatus(state.updates.checkingError || "检查完成。", Boolean(state.updates.checkingError));
  } catch (error) {
    // The check grant is one-shot even when its HTTP response is lost.
    if (state.updates?.actions) state.updates.actions.check = null;
    setStatus(`${error.message} 请刷新维护状态后重试。`, true);
  } finally {
    updateSubmitting = false;
    renderUpdates();
    scheduleUpdatePoll();
  }
}

async function installUpdate() {
  return submitMaintenance("upgrade");
}

async function restartService() {
  return submitMaintenance("restart");
}

async function submitMaintenance(kind) {
  if (updateSubmitting || updateReading || updateNeedsPolling()
      || updatePollExpired || updateDisconnected) return;
  const restarting = kind === "restart";
  const action = restarting ? "restart" : "install";
  const envelope = state.updates?.actions?.[action];
  if (!envelope || (restarting
    ? !state.updates.restartSupported || !state.updates.restartAvailable
    : !state.updates.supported || !state.updates.available)) return;
  if (restarting && !window.confirm(
    "确认重启服务？将保持当前版本，重新启动 Netizen 及其 Codex 运行环境。\n\n"
      + "重启会中断正在执行的任务、暂停 Goal，并结束临时 Side 会话。"
      + "重启后不会自动续跑，管理页需要重新登录。",
  )) return;
  updateSubmitting = true;
  updateExpectedTarget = envelope.target;
  updateExpectedKind = kind;
  updatePriorOperationId = state.updates.operation?.operationId || null;
  updatePollStarted = Date.now();
  state.updates.actions.install = null;
  state.updates.actions.restart = null;
  renderUpdates();
  let sessionExpired = false;
  try {
    const result = await updateApi(`/api/v1/updates/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(envelope)),
    });
    acceptUpdateStatus({ ...state.updates, operation: result.operation });
    setStatus(restarting ? "重启已受理，正在查询服务重启结果。重启后需要重新登录。"
      : "升级已受理，正在查询安装器结果。重启后可能需要重新登录。");
  } catch (error) {
    if (error.status === 401) {
      sessionExpired = true;
      return;
    }
    if (error.status >= 400 && error.status < 500) {
      updateExpectedTarget = null;
      updateExpectedKind = null;
      setStatus(`${error.message} 请刷新维护状态。`, true);
    } else {
      setStatus(`${restarting ? "重启" : "升级"}提交结果未确认，正在查询服务端记录；请勿重复提交。`, true);
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
    cell(row, project.archivedBindingCount);
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
  inventoryState: [["active", "Active"], ["lazy", "Lazy"], ["archived", "Archived"], ["missing", "Missing"]],
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
  let selected = new Set(name === "inventoryState" ? ["active", "lazy"] : []);

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
      selected = new Set(name === "inventoryState" ? ["active", "lazy"] : []);
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
  root.addEventListener("focusout", (event) => {
    if (!root.contains(event.relatedTarget)) close();
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
  const primary = document.createElement("span");
  primary.className = "runtime-primary";
  primary.textContent = runtimeLabel(runtime);
  target.replaceChildren(primary);
  if (runtime.subscriptionState) {
    const subscription = document.createElement("small");
    subscription.className = "runtime-subscription";
    subscription.textContent = `订阅：${runtime.subscriptionState}`;
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
  link.href = session.chatOpenUrl;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = session.chatLabelResolved
    ? session.chatLabel
    : locationFallback(session);
  link.title = session.sessionType === "topic"
    ? "打开所在飞书会话（暂不定位具体话题）"
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
  for (const controller of sessionMultiFilters.values()) controller.reset();
  for (const root of form.querySelectorAll("[data-time-range]")) {
    timeRangeControllers.get(root)?.reset();
  }
  resetSessionPagination();
  return refresh("sessions");
}

async function loadSessions(cursor = state.sessionPage.cursor) {
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
    const settings = session.turnSettings;
    const model = settings ? `${settings.modelId} / ${settings.effortId} / ${settings.serviceTierId}` : "继承 Codex";
    cell(row, `${session.messageContextMode} · ${model}`);
    const actions = actionsCell(row);
    wireSessionActions(actions, session);
    body.append(row);
  }
  renderSessionPagination();
}

function wireSessionActions(actions, session) {
  const a = session.actions;
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
  if (a.configure) actions.append(actionButton("配置", async () => {
    const clear = window.confirm("确定使用默认配置？取消后可输入精确 Model / Effort / Speed ID。");
    let turnSettings = null;
    if (!clear) {
      const modelId = window.prompt("Model ID", session.turnSettings?.modelId || "");
      const effortId = window.prompt("Effort ID", session.turnSettings?.effortId || "");
      const serviceTierId = window.prompt("Speed / Service Tier ID", session.turnSettings?.serviceTierId || "");
      if (!modelId || !effortId || !serviceTierId) return;
      turnSettings = { modelId, effortId, serviceTierId };
    }
    await mutate("/api/v1/sessions/configure", a.configure, { turnSettings });
  }));
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
    scope_conflict: "话题已有会话，未启动", dispatch_rejected: "未启动" }[status] || "结果暂不可用";
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
    project_unavailable: "Project 不可用" }[plan.blocked_reason] || "";
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
const pendingScheduleMutations = new Set();

function scheduleInput(id) { return document.querySelector(`#schedule-${id}`); }

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
  scheduleEditorSerial += 1;
  scheduleEditor = null;
  invalidateSchedulePreview();
  scheduleInput("editor").hidden = true;
  scheduleInput("drawer").close();
  document.body.classList.toggle("schedule-drawer-open", false);
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
  return { turn_settings: null, reaction_pulse_enabled: false, progress_card_enabled: false,
    message_context_mode: "current-only" };
}

function scheduleSessionSummary(settings) {
  const model = settings.turn_settings;
  const labels = [model ? `${model.model_id} / ${model.effort_id} / ${model.service_tier_id}` : "继承 Codex",
    settings.message_context_mode === "catch-up" ? "补齐未读上下文" : "当前消息"];
  if (settings.reaction_pulse_enabled) labels.push("表情反馈");
  if (settings.progress_card_enabled) labels.push("进度卡片");
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

function renderScheduleSessionSettings() {
  const editor = scheduleEditor;
  if (!editor) return;
  const settings = editor.sessionDraft;
  const current = settings.turn_settings;
  const model = editor.models.find((item) => item.id === current?.model_id);
  scheduleSelectOptions(scheduleInput("model"), [["", "继承 Codex"],
    ...editor.models.map((item) => [item.id, item.display_name])], current?.model_id);
  scheduleSelectOptions(scheduleInput("effort"), (model?.efforts || []).map((item) => [item.id, item.id]), current?.effort_id);
  scheduleSelectOptions(scheduleInput("tier"), (model?.service_tiers || []).map((item) => [item.id, item.name || item.id]), current?.service_tier_id);
  scheduleInput("effort").disabled = !model;
  scheduleInput("tier").disabled = !model;
  scheduleInput("context").value = settings.message_context_mode;
  for (const option of scheduleInput("context").querySelectorAll("option")) {
    option.disabled = option.value === "catch-up" && editor.contextAvailable === false;
  }
  scheduleInput("reactions").checked = settings.reaction_pulse_enabled;
  scheduleInput("progress").checked = settings.progress_card_enabled;
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
  if (chatId) query.set("chat_id", chatId);
  try {
    const data = await api(`/api/v1/schedules?${query}`);
    if (scheduleEditor !== editor || serial !== editor.optionsSerial) return;
    editor.models = data.models;
    editor.contextAvailable = data.context_mode_available;
    editor.catalogMessage = data.model_catalog_error
      ? `${data.model_catalog_error.message} 已有配置保持不变，仍可调整其他项目。` : "";
    if (!editor.plan && !editor.settingsTouched) {
      if (JSON.stringify(editor.sessionDraft) !== JSON.stringify(data.session_settings)) invalidateSchedulePreview();
      editor.sessionBase = structuredClone(data.session_settings);
      editor.sessionDraft = structuredClone(data.session_settings);
    }
    renderScheduleSessionSettings();
  } catch (error) {
    if (scheduleEditor !== editor || serial !== editor.optionsSerial) return;
    editor.models = [];
    editor.contextAvailable = null;
    editor.catalogMessage = `${error.message} 可选配置暂未刷新，已有选择保持不变。`;
    renderScheduleSessionSettings();
  }
}

function changeScheduleSessionSettings(field) {
  const editor = scheduleEditor;
  if (!editor) return;
  const settings = editor.sessionDraft;
  editor.settingsTouched = true;
  if (field === "model") {
    const id = scheduleInput("model").value;
    const selected = editor.models.find((item) => item.id === id);
    if (!id) settings.turn_settings = null;
    else if (selected) settings.turn_settings = { model_id: selected.id,
      effort_id: selected.default_effort_id, service_tier_id: selected.default_service_tier_id };
    else if (id === editor.sessionBase.turn_settings?.model_id) settings.turn_settings = structuredClone(editor.sessionBase.turn_settings);
  } else if (field === "effort" && settings.turn_settings) {
    settings.turn_settings.effort_id = scheduleInput("effort").value;
  } else if (field === "tier" && settings.turn_settings) {
    settings.turn_settings.service_tier_id = scheduleInput("tier").value;
  } else if (field === "context") settings.message_context_mode = scheduleInput("context").value;
  else if (field === "reactions") settings.reaction_pulse_enabled = scheduleInput("reactions").checked;
  else if (field === "progress") settings.progress_card_enabled = scheduleInput("progress").checked;
  invalidateSchedulePreview();
  renderScheduleSessionSettings();
}

function scheduleSessionPatch() {
  const patch = {};
  if (!scheduleEditor) return patch;
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
    const chatId = scheduleInput("chat").value.trim();
    if (chatId) query.set("chat_id", chatId);
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

function openScheduleEditor(plan = null) {
  if (scheduleInput("fields").disabled) return;
  scheduleEditorSerial += 1;
  const action = plan ? plan.actions.update : state.schedules?.actions.create;
  if (!action) return;
  const settings = plan?.session_settings || defaultScheduleSessionSettings();
  scheduleEditor = { plan, action, sessionBase: structuredClone(settings), sessionDraft: structuredClone(settings),
    models: [], contextAvailable: null, optionsSerial: 0, settingsTouched: false, catalogMessage: "正在读取模型目录…" };
  const rule = plan?.schedule || { kind: "daily", at: "09:00", timezone: state.schedules.default_timezone || "" };
  scheduleInput("editor").hidden = false;
  scheduleInput("editor-title").textContent = plan ? `编辑计划 · ${plan.name}` : "创建计划";
  scheduleInput("name").value = plan?.name || "";
  scheduleInput("project").value = plan?.project_alias || "";
  renderScheduleProjects();
  scheduleInput("chat").value = plan?.chat_id || "";
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
  invalidateSchedulePreview();
  scheduleInput("session-settings").open = true;
  renderScheduleSessionSettings();
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
  if (!scheduleEditor?.action || !schedulePrepared
      || schedulePrepared.fingerprint !== scheduleRuleFingerprint()) return;
  const editor = scheduleEditor;
  scheduleEditorSerial += 1;
  const action = editor.action;
  const hasFuture = schedulePrepared.hasFuture;
  const definition = {
    name: scheduleInput("name").value.trim(),
    project: scheduleInput("project").value.trim(),
    chat_id: scheduleInput("chat").value.trim(),
    instructions: scheduleInput("instructions").value.trim(),
    schedule: schedulePrepared.schedule,
    enabled: scheduleInput("enabled").checked,
  };
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
    try {
      await loadSchedules(scheduleListCursor, scheduleListQuery);
      const trigger = Array.from(document.querySelectorAll("[data-schedule-edit]"))
        .find((button) => button.dataset.scheduleEdit === editor.plan?.id);
      (trigger || scheduleInput("new")).focus();
      setStatus(hasFuture ? "已保存计划。执行会话将在计划触发时创建。"
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
    if (plan.execution.due_local) scheduleNote(execution, scheduleLocalTime(plan.execution.due_local));
    row.append(execution);
    const actions = actionsCell(row);
    actions.append(actionButton("详情 / 最近记录", () => showSchedule(plan.id)));
    const edit = actionButton("编辑", () => editSchedule(plan));
    edit.setAttribute("data-schedule-edit", plan.id);
    actions.append(edit);
    if (plan.lifecycle.has_trigger) {
      actions.append(actionButton(plan.enabled ? "暂停" : "启用", () => changeSchedule(plan, "update")));
    }
    actions.append(actionButton("删除", () => changeSchedule(plan, "delete"), true));
    for (const button of actions.querySelectorAll("button")) button.disabled = pendingScheduleMutations.has(plan.id);
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
      + (scheduleBlockedLabel(data.plan) ? ` · ${scheduleBlockedLabel(data.plan)}` : "")
      + ` · 目标会话 ID：${data.plan.chat_id}`
      + (data.inflight ? "。编辑、暂停或删除不会取消本次执行。" : "");
    scheduleInput("detail-instructions").textContent = data.plan.instructions;
    scheduleInput("detail-settings").textContent = scheduleSessionSummary(data.plan.session_settings || defaultScheduleSessionSettings());
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
      cell(row, scheduleRunStatus(run.status || run.stage));
      const links = actionsCell(row);
      if (run.feishu_url) {
        const link = document.createElement("a");
        link.href = run.feishu_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "打开飞书话题";
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
      primaryStatusResolution: "deferred",
    },
    needsResolution: true,
  };
}

function applyRuntimeSnapshots(payload, resolveChanges = true) {
  const bindings = new Map(payload.bindings.map((item) => [item.bindingId, item]));
  const changedBindings = [];
  for (const session of state.sessions?.items || []) {
    const incoming = bindings.get(session.bindingId);
    if (!incoming) continue;
    const previous = session.runtime;
    let runtime = incoming;
    if (incoming.primaryStatusResolution === "deferred") {
      const merged = mergeDeferredBindingRuntime(incoming, previous);
      runtime = merged.runtime;
      if (resolveChanges && merged.needsResolution) {
        changedBindings.push(session.bindingId);
      }
    } else if (
      incoming.primaryStatusResolution === "unavailable"
      && previous?.primaryStatus != null
    ) {
      runtime = {
        ...incoming,
        activityRevision: previous.activityRevision,
        primaryStatus: previous.primaryStatus,
      };
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
    setStatus("已更新。");
    return true;
  } catch (error) {
    setStatus(error.message, true);
    return false;
  }
}

function selectTab(name) {
  state.tab = name;
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
  const form = new FormData(event.currentTarget);
  await mutate("/api/v1/projects/register", state.projects.actions.register, { alias: form.get("alias"), path: form.get("path") });
  event.currentTarget.reset();
});

document.querySelector("#create-project").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await mutate("/api/v1/projects/create-directory", state.projects.actions.createDirectory, { alias: form.get("alias"), path: form.get("path") || null });
  event.currentTarget.reset();
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
scheduleInput("filter").addEventListener("submit", (event) => { event.preventDefault(); refresh("schedules"); });
scheduleInput("new").addEventListener("click", () => openScheduleEditor());
scheduleInput("preview").addEventListener("click", previewSchedule);
scheduleInput("editor").addEventListener("input", invalidateSchedulePreview);
scheduleInput("kind").addEventListener("change", invalidateSchedulePreview);
scheduleInput("editor").addEventListener("submit", saveSchedule);
scheduleInput("chat").addEventListener("change", loadScheduleSessionOptions);
for (const field of ["model", "effort", "tier", "context", "reactions", "progress"]) {
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
document.querySelector("#update-check").addEventListener("click", checkUpdate);
document.querySelector("#update-install").addEventListener("click", installUpdate);
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
    (runtimePollInFlight && state.tab === "sessions")
    || document.hidden
    || (state.tab !== "sessions" && state.tab !== "side-topics")
  ) return;
  const sessionPage = state.sessions;
  const bindingIds = state.tab === "sessions"
    ? state.sessions?.items?.map((item) => item.bindingId) || []
    : [];
  const sideIds = state.tab === "side-topics"
    ? state.sides?.items?.map((item) => item.sideId) || []
    : [];
  if (!bindingIds.length && !sideIds.length) return;
  if (bindingIds.length) runtimePollInFlight = true;
  try {
    const snapshots = await fetchRuntimeSnapshots(bindingIds, sideIds);
    if (bindingIds.length && state.sessions !== sessionPage) return;
    const changedBindings = applyRuntimeSnapshots(snapshots);
    if (changedBindings.length) {
      const resolved = await fetchRuntimeSnapshots(changedBindings, [], true);
      if (state.sessions !== sessionPage) return;
      applyRuntimeSnapshots(resolved, false);
    }
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    if (bindingIds.length) runtimePollInFlight = false;
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
