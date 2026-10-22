"use strict";

const state = {
  tab: "projects",
  sessionTab: "ordinary",
  systemTab: "admins",
  projects: null,
  sessions: null,
  sides: null,
  updates: null,
  schedules: null,
  defaults: null,
  projectPage: firstSessionPage(),
  sidePage: firstSessionPage(),
  sessionPage: firstSessionPage(),
};

const statusNode = document.querySelector("#status");

function setStatus(message, isError = false) {
  statusNode.textContent = message || "";
  statusNode.classList.toggle("error", isError);
}

async function api(path, options = {}) {
  if (state.authExpired) {
    const error = new Error("登录已过期，请重新登录。");
    error.status = 401;
    throw error;
  }
  const response = await fetch(path, { cache: "no-store", ...options });
  if (response.status === 401 || state.authExpired) {
    expireAdminSession();
    const error = new Error("登录已过期，请重新登录。");
    error.status = 401;
    throw error;
  }
  if (response.status === 204) return null;
  const data = await response.json();
  if (state.authExpired) {
    const error = new Error("登录已过期，请重新登录。");
    error.status = 401;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(data.message || `请求失败 (${response.status})`);
    error.status = response.status;
    error.code = data.code;
    error.choices = data.choices;
    throw error;
  }
  return data;
}

function expireAdminSession() {
  if (state.authExpired) return;
  state.authExpired = true;
  invalidateSessionListRequest();
  stopUpdatePolling();
  window.NetizenSelect?.close();
  // Release these modal drawers so the page's login link remains reachable.
  // Inputs stay in memory, but expired actions can never be submitted again.
  for (const dialog of document.querySelectorAll("#project-drawer, #worktree-create-drawer, #worktree-settings-drawer")) {
    if (dialog.open) dialog.close();
  }
  document.body.classList.toggle("workspace-drawer-open", false);
  for (const element of document.querySelectorAll?.(".panel, dialog, .tabs") || []) element.inert = true;
  for (const button of document.querySelectorAll?.("button") || []) button.disabled = true;
  const notice = document.querySelector("#auth-expired");
  if (notice) notice.hidden = false;
  window.NetizenIdentity?.expire();
  window.NetizenSessionAccess?.expire();
  window.NetizenProgressCard?.expire();
  window.NetizenWorktrees?.expire();
  setStatus("登录已过期，请重新登录。", true);
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
let diagnosticsRequest = null;

function renderInstanceDiagnostics(data, error = null) {
  for (const [key, yes, no] of [
    ["bound", "已注册", "未注册"], ["loaded", "已加载", "未加载"],
    ["running", "运行中", "未运行"], ["ready", "已就绪", "未就绪"],
    ["enabled", "已启用", "未启用"],
  ]) {
    const value = data?.[key];
    const node = document.querySelector(`#diagnostics-${key}`);
    node.textContent = value === true ? yes : value === false ? no : "无法确认";
    node.className = `badge ${value === true ? "on" : value === false ? "warn" : ""}`;
  }
  document.querySelector("#diagnostics-root").textContent = data?.root || "无法确认";
  for (const key of ["python", "prefix"]) {
    document.querySelector(`#diagnostics-${key}`).textContent = data
      ? data.binding?.[key] || "未绑定" : "无法确认";
  }
  document.querySelector("#diagnostics-codex-home").textContent = !data ? "无法确认"
    : !data.binding ? "未绑定" : data.binding.codexHome || "未显式指定，随服务环境确定";
  if (data) document.querySelector("#diagnostics-checked-at").textContent =
    `上次成功检查：${new Date(data.checkedAt * 1000).toLocaleString("zh-CN", { hour12: false })}`;
  const message = document.querySelector("#diagnostics-message");
  message.textContent = error || "只读检查已完成；按需点击“刷新维护状态”重新检查。";
  message.className = error ? "error" : "";
}

function loadInstanceDiagnostics() {
  if (diagnosticsRequest) return diagnosticsRequest;
  document.querySelector("#diagnostics-message").textContent = "正在检查当前实例…";
  diagnosticsRequest = updateApi("/api/v1/instance-diagnostics")
    .then((data) => { if (!state.authExpired) renderInstanceDiagnostics(data); })
    .catch(() => {
      renderInstanceDiagnostics(null, state.authExpired ? "登录已失效，请重新登录后检查。"
        : "本次检查失败，无法确认当前状态；请稍后手动刷新，或使用 doctor --root 排查。");
    })
    .finally(() => { diagnosticsRequest = null; });
  return diagnosticsRequest;
}

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
  const operationName = restarting ? "重启" : "维护";
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
  const legacyUnresolved = operation && operation.kind !== "restart"
    && (!updateTerminalPhases.has(operation.phase) || operation.phase === "recovery_required");
  if (legacyUnresolved && !updateExpectedTarget) {
    phase = "历史维护状态待核查";
    detail = "旧安装机制的维护结果尚未确认，请检查实例状态和日志。此记录不代表当前版本的升级结果；不要删除维护记录或重复提交操作。";
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
  document.querySelector("#update-operation").hidden = !(operation?.kind === "restart"
    || legacyUnresolved || updateExpectedTarget || updateDisconnected || updatePollExpired);
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
  if (state.authExpired || state.tab !== "updates" || updatePollExpired || !updateNeedsPolling()) return;
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
  if (state.authExpired || state.tab !== "updates") return;
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
  await Promise.all([loadMaintenanceStatus(), loadInstanceDiagnostics()]);
}

async function loadMaintenanceStatus() {
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

let projectEditor = null;
const projectInput = (name) => document.querySelector(`#project-${name}`);

function projectEditorValues() {
  if (!projectEditor) return null;
  return { mode: projectEditor.mode, alias: projectInput("alias").value,
    paths: { ...projectEditor.paths, [projectEditor.mode]: projectInput("path").value } };
}

function projectEditorHasChanges() {
  return Boolean(projectEditor && JSON.stringify(projectEditorValues()) !== projectEditor.discardBase);
}

function renderProjectEditorControls() {
  const busy = state.authExpired || projectEditor?.saving;
  projectInput("editor").setAttribute("aria-busy", String(Boolean(projectEditor?.saving || projectEditor?.checking)));
  projectInput("new").disabled = Boolean(busy
    || !(state.projects?.actions?.register || state.projects?.actions?.createDirectory));
  for (const button of document.querySelectorAll("[data-project-add]")) button.disabled = projectInput("new").disabled;
  for (const field of projectInput("editor").querySelectorAll("input, button")) field.disabled = Boolean(busy);
  if (!projectEditor) {
    projectInput("submit").disabled = true;
    return;
  }
  const registering = projectEditor.mode === "register";
  projectInput("path").required = registering;
  projectInput("path-label").textContent = registering ? "绝对路径" : "绝对路径（可留空）";
  projectInput("path-help").textContent = registering
    ? "登记一个已存在的目录作为 Project。"
    : "留空时在实例项目根目录下按 Alias 创建空目录；自定义路径也须位于该根目录内。";
  for (const button of document.querySelectorAll("[data-project-mode]")) {
    button.setAttribute("aria-pressed", String(button.dataset.projectMode === projectEditor.mode));
    button.disabled = Boolean(busy || projectEditor.attempted);
  }
  projectInput("submit").textContent = projectEditor.saving
    ? registering ? "正在登记…" : "正在创建…" : registering ? "登记 Project" : "创建并登记";
  projectInput("submit").disabled = Boolean(busy || projectEditor.attempted || !projectEditor.actions[projectEditor.mode]);
  projectInput("check").disabled = Boolean(busy || projectEditor.checking);
}

function openProjectEditor() {
  if (state.authExpired || projectEditor?.saving
      || !(state.projects?.actions?.register || state.projects?.actions?.createDirectory)) return;
  if (projectEditor && !closeProjectEditor()) return;
  projectEditor = { mode: "register", paths: { register: "", createDirectory: "" },
    actions: { ...state.projects.actions }, returnFocus: document.activeElement, saving: false, attempted: false };
  projectInput("alias").value = "";
  projectInput("path").value = "";
  projectInput("note").textContent = "";
  projectInput("note").classList.toggle("error", false);
  projectInput("check").hidden = true;
  projectInput("check").textContent = "刷新列表核对";
  projectEditor.discardBase = JSON.stringify(projectEditorValues());
  projectInput("editor").hidden = false;
  renderProjectEditorControls();
  projectInput("drawer").showModal();
  document.body.classList.toggle("workspace-drawer-open", true);
  projectInput("drawer").querySelector(".drawer-body").scrollTop = 0;
  projectInput("alias").focus();
}

function closeProjectEditor({ saved = false, confirmed = false } = {}) {
  if (!projectEditor) return true;
  if (projectEditor.saving) return false;
  if (!saved && !confirmed && !confirmDiscardChanges(projectEditorHasChanges())) return false;
  const returnFocus = projectEditor.returnFocus;
  projectEditor = null;
  projectInput("editor").hidden = true;
  projectInput("drawer").close();
  document.body.classList.toggle("workspace-drawer-open", false);
  renderProjectEditorControls();
  (returnFocus?.isConnected === false ? projectInput("new") : returnFocus)?.focus();
  return true;
}

function selectProjectMode(mode) {
  if (!projectEditor || projectEditor.saving || projectEditor.attempted || state.authExpired) return;
  projectEditor.paths[projectEditor.mode] = projectInput("path").value;
  projectEditor.mode = mode;
  projectInput("path").value = projectEditor.paths[mode];
  renderProjectEditorControls();
}

async function checkProjectResult() {
  const editor = projectEditor;
  if (!editor || editor.saving || editor.checking || state.authExpired) return;
  editor.checking = true;
  renderProjectEditorControls();
  try {
    const refreshed = await loadProjects();
    if (projectEditor !== editor || state.authExpired) return;
    if (refreshed !== false && editor.attempted && !editor.uncertain) {
      editor.actions = { ...state.projects.actions };
      editor.attempted = false;
      projectInput("check").hidden = true;
      projectInput("note").classList.toggle("error", false);
      projectInput("note").textContent = "已刷新，当前输入已保留。可修改后重新提交。";
    } else {
      projectInput("note").textContent = refreshed === false
        ? "列表未更新，请再次刷新核对。"
        : "已刷新 Projects 列表；请关闭表单核对 Alias 与路径。未重复提交操作。";
    }
  } catch (error) {
    if (projectEditor === editor && !state.authExpired) projectInput("note").textContent = `列表读取失败：${error.message}`;
  } finally {
    editor.checking = false;
    if (projectEditor === editor) renderProjectEditorControls();
  }
}

async function saveProject(event) {
  event.preventDefault();
  const editor = projectEditor;
  if (!editor || editor.saving || editor.attempted || state.authExpired || !editor.actions[editor.mode]) return;
  if (!projectInput("editor").reportValidity()) return;
  const envelope = editor.actions[editor.mode];
  editor.actions[editor.mode] = null;
  if (state.projects?.actions) state.projects.actions[editor.mode] = null;
  editor.saving = true;
  editor.attempted = true;
  projectInput("note").textContent = editor.mode === "register" ? "正在登记 Project…" : "正在创建 Project…";
  renderProjectEditorControls();
  let result;
  try {
    result = await api(editor.mode === "register" ? "/api/v1/projects/register" : "/api/v1/projects/create-directory", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(envelope, { alias: projectInput("alias").value,
        path: projectInput("path").value || null })),
    });
  } catch (error) {
    if (state.authExpired) return;
    editor.uncertain = !error.status || error.status >= 500;
    projectInput("note").textContent = !editor.uncertain
      ? `${error.message} 输入已保留，请刷新后修改并重新提交。`
      : "操作结果未确认，请刷新 Projects 列表核对；不要重复提交。";
    projectInput("note").classList.toggle("error", true);
    projectInput("check").hidden = false;
    projectInput("check").textContent = editor.uncertain ? "刷新列表核对" : "刷新后继续填写";
    setStatus(projectInput("note").textContent, true);
    return;
  } finally {
    editor.saving = false;
    renderProjectEditorControls();
  }
  const alias = projectInput("alias").value;
  closeProjectEditor({ saved: true });
  const message = `Project「${result?.alias || alias}」已${editor.mode === "register" ? "登记" : "创建并登记"}。`
    + (result?.cwd ? `目录：${result.cwd}` : "");
  try {
    const refreshed = await loadProjects();
    if (!state.authExpired && state.tab === "projects") setStatus(
      refreshed === false ? `${message} 列表未更新，请手动刷新。` : message);
  } catch {
    if (!state.authExpired && state.tab === "projects") setStatus(`${message} 列表刷新失败，请手动刷新查看。`, true);
  }
}

function confirmMaterializedDelete(session) {
  const name = session.nativeTitle || "未命名 Session";
  return window.confirm(
    `确认永久删除这个会话？\n\n会话：${name}\nScope：${session.scopeKey}\nNetizen 会话编号：${session.shortId}`
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
    + `\n关联受管 worktree：${preview.worktreeCount || 0}，${preview.deleteWorktrees ? "同时安全删除；失败项保留，不强制丢弃成果" : "保留目录，可在 worktree 页面处理；不会自动关联到同名新 Project"}。`
    + (preview.deleteWorktrees && preview.worktrees?.length ? `\n将删除的 checkout 根目录：\n${preview.worktrees.map((item) => `${item.id} · ${item.root}`).join("\n")}` : "")
    + `\n\n磁盘代码目录保留：${preview.project.cwd}`,
  );
}

async function deleteProject(project, deleteWorktrees = false) {
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
      body: JSON.stringify(actionPayload(envelope, deleteWorktrees ? { deleteWorktrees: true } : {})),
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
    if (result.deletedWorktreeCount) message += `\n已删除 ${result.deletedWorktreeCount} 个受管 worktree。`;
    if (result.remainingWorktreeCount) {
      if (result.deleteWorktrees) details.push(`仍有 ${result.remainingWorktreeCount} 个受管 worktree 未安全删除，请在 worktree 页面检查。`);
      else message += `\n已按选择保留 ${result.remainingWorktreeCount} 个受管 worktree，可在 worktree 页面处理。`;
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

async function loadProjects(cursor = undefined) {
  const request = state.projectListRequest = (state.projectListRequest || 0) + 1;
  const page = resolveCursorPage(state.projectPage || firstSessionPage(), cursor);
  const query = new URLSearchParams({ pageSize: "25" });
  if (page.cursor) query.set("cursor", page.cursor);
  const data = await api(`/api/v1/projects?${query}`);
  if (request !== state.projectListRequest) return false;
  state.projects = data;
  state.projectPage = { ...page, nextCursor: data.nextCursor };
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
      const cascadeLabel = document.createElement("label");
      cascadeLabel.className = "cascade-choice";
      const cascade = document.createElement("input");
      cascade.type = "checkbox";
      cascadeLabel.append(cascade, document.createTextNode("同时删除关联受管 worktree"));
      actions.append(cascadeLabel);
      actions.append(actionButton(
        "删除 Project 及关联 Sessions", () => deleteProject(project, cascade.checked), true,
      ));
    }
    if (project.deleting || pendingProjectDeletes.has(project.alias)) {
      for (const button of actions.querySelectorAll("button")) button.disabled = true;
    }
    body.append(row);
  }
  if (!data.items.length) {
    const row = document.createElement("tr");
    const empty = document.createElement("td");
    empty.colSpan = 6;
    const message = document.createElement("p");
    message.textContent = "本页没有 Project。可登记已有目录，或创建空目录。";
    const add = actionButton("添加 Project", openProjectEditor);
    add.setAttribute("data-project-add", "");
    empty.append(message, add);
    row.append(empty);
    body.append(row);
  }
  renderProjectEditorControls();
  renderCursorPagination("projects", state.projectPage, data.items.length);
  return true;
}

const timeRangeControllers = new WeakMap();
const sessionMultiFilters = new Map();
const sideMultiFilters = new Map();
const scheduleFilters = new Map();
let openFilter = null;

const filterOptions = {
  project: [],
  scopeKind: [["direct", "单聊"], ["group", "群聊"], ["topic", "话题"]],
  current: [["true", "当前"], ["false", "非当前"]],
  state: [["creating", "创建中"], ["open", "存活"], ["closed", "已结束"], ["expired", "已失效"], ["failed", "失败"]],
  enabled: [["true", "已启用"], ["false", "已暂停"]],
  ended: [["false", "未结束"], ["true", "已结束"]],
};

// Keep native selects as the field/value/validation owner. Only their popup is
// replaced, so async option updates and existing form handlers need no mirror.
function initializeSelectControls() {
  let current = null;

  function close({ restoreFocus = false } = {}) {
    if (!current) return;
    const { select, popup, observer } = current;
    current = null;
    observer.disconnect();
    popup.remove();
    select.setAttribute("aria-expanded", "false");
    select.removeAttribute("aria-controls");
    select.removeAttribute("aria-activedescendant");
    if (restoreFocus && !select.matches(":disabled")) select.focus();
  }

  function open(select) {
    if (select.matches(":disabled")) return;
    close();
    const popup = document.createElement("div");
    popup.className = "multi-filter-popover admin-select-popover";
    const search = document.createElement("input");
    search.type = "search";
    search.className = "multi-filter-search";
    search.placeholder = "搜索选项";
    search.setAttribute("aria-label", "搜索选项");
    search.setAttribute("role", "combobox");
    search.setAttribute("aria-autocomplete", "list");
    search.setAttribute("aria-expanded", "true");
    const list = document.createElement("div");
    list.className = "multi-filter-options";
    list.id = `${select.id}-choices`;
    list.setAttribute("role", "listbox");
    const label = select.labels?.[0];
    if (label) {
      label.id ||= `${select.id}-label`;
      list.setAttribute("aria-labelledby", label.id);
    } else list.setAttribute("aria-label", select.getAttribute("aria-label") || "选项");
    search.setAttribute("aria-controls", list.id);
    const empty = document.createElement("p");
    empty.className = "multi-filter-empty";
    empty.textContent = "没有匹配选项";
    const options = [...select.querySelectorAll("option")]
      .filter((option) => !option.hidden && !option.closest("optgroup")?.hidden);
    const rows = options.map((option, index) => {
      const row = document.createElement("div");
      row.className = "admin-select-option";
      row.id = `${list.id}-${index}`;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(option.value === select.value));
      row.setAttribute("aria-disabled", String(option.disabled || Boolean(option.closest("optgroup")?.disabled)));
      row.textContent = option.label || option.textContent;
      row.setAttribute("aria-label", row.textContent);
      row.addEventListener("pointerdown", (event) => event.preventDefault());
      row.addEventListener("click", () => choose(index));
      list.append(row);
      return row;
    });
    search.hidden = options.length < 8;
    popup.append(search, list, empty);
    (select.closest("dialog") || document.body).append(popup);
    // Catalog or field locks can change while open. Discard this menu rather
    // than let a stale option bypass the current native field's constraints.
    const observer = new MutationObserver(() => close());
    observer.observe(select, { childList: true, subtree: true, attributes: true,
      attributeFilter: ["disabled", "hidden", "selected", "value"] });
    for (let parent = select.parentElement; parent; parent = parent.parentElement) {
      observer.observe(parent, { attributes: true, attributeFilter: ["disabled", "hidden", "open"] });
    }
    let active = options.findIndex((option) => option.value === select.value);
    let typed = "", typedAt = 0;
    current = { select, popup, observer, keydown };
    select.setAttribute("aria-expanded", "true");
    select.setAttribute("aria-controls", list.id);

    function available() {
      return rows.map((row, index) => index)
        .filter((index) => !rows[index].hidden && rows[index].getAttribute("aria-disabled") !== "true");
    }
    function highlight(index, scroll = true) {
      active = index;
      rows.forEach((row, offset) => { row.dataset.active = String(offset === active); });
      for (const control of [select, search]) {
        if (active >= 0) control.setAttribute("aria-activedescendant", rows[active].id);
        else control.removeAttribute("aria-activedescendant");
      }
      if (scroll) rows[active]?.scrollIntoView({ block: "nearest" });
    }
    function filter() {
      const query = search.value.trim().toLocaleLowerCase();
      rows.forEach((row) => { row.hidden = !row.textContent.toLocaleLowerCase().includes(query); });
      const visible = available();
      empty.hidden = rows.some((row) => !row.hidden);
      highlight(visible.includes(active) ? active : visible[0] ?? -1, false);
    }
    function choose(index) {
      const option = options[index];
      if (!option || select.matches(":disabled") || !select.contains(option)
          || option.hidden || option.disabled || option.closest("optgroup")?.disabled || option.closest("optgroup")?.hidden) return;
      const changed = select.value !== option.value;
      select.value = option.value;
      close({ restoreFocus: true });
      if (changed) {
        select.dispatchEvent(new Event("input", { bubbles: true }));
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
    function keydown(event) {
      if (event.isComposing) return;
      if (event.key === "Escape" || event.key === "F4" || (event.altKey && event.key === "ArrowUp")) {
        event.preventDefault();
        event.stopPropagation();
        close({ restoreFocus: true });
      } else if (event.key === "Tab") {
        // Resume the original field's tab order, not the appended popup's.
        close({ restoreFocus: true });
      } else if (event.key === "Enter" || (event.key === " " && event.target === select)) {
        event.preventDefault();
        choose(active);
      } else if (["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
          && (event.target === select || ["ArrowDown", "ArrowUp"].includes(event.key))) {
        event.preventDefault();
        const visible = available();
        const offset = visible.indexOf(active);
        highlight(event.key === "Home" ? visible[0] ?? -1 : event.key === "End" ? visible.at(-1) ?? -1
          : visible[(offset + (["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1) + visible.length) % visible.length] ?? -1);
      } else if (event.target === select && event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        if (!search.hidden) {
          search.focus();
          search.value = event.key;
          filter();
        } else {
          typed = Date.now() - typedAt > 700 ? event.key : typed + event.key;
          typedAt = Date.now();
          const match = available().find((index) => rows[index].textContent.toLocaleLowerCase().startsWith(typed.toLocaleLowerCase()));
          if (match !== undefined) highlight(match);
        }
      }
    }
    search.addEventListener("input", filter);
    popup.addEventListener("keydown", keydown);
    filter();
    // Fixed positioning avoids clipping by the drawer's scrolling body. The
    // popup remains inside the dialog's top layer and within the viewport.
    const bounds = select.getBoundingClientRect();
    popup.style.width = `${Math.min(Math.max(bounds.width, 180), window.innerWidth - 24)}px`;
    popup.style.left = `${Math.max(12, Math.min(bounds.left, window.innerWidth - 12 - popup.getBoundingClientRect().width))}px`;
    const below = window.innerHeight - bounds.bottom - 12;
    const above = bounds.top - 12;
    const upward = below < Math.min(popup.getBoundingClientRect().height, 240) && above > below;
    popup.style.maxHeight = `${Math.max(80, upward ? above - 6 : below - 6)}px`;
    if (upward) {
      popup.style.top = "auto";
      popup.style.bottom = `${window.innerHeight - bounds.top + 6}px`;
    }
    else popup.style.top = `${bounds.bottom + 6}px`;
    highlight(active);
    if (!search.hidden) search.focus();
    else select.focus();
  }

  for (const select of document.querySelectorAll("select")) {
    if (select.multiple || select.size > 1) continue;
    select.classList.toggle("admin-select", true);
    select.setAttribute("aria-expanded", "false");
    // Desktop opens on mousedown, touch/AT may activate via click. Cancel the
    // native action at both stages, but toggle exactly once per gesture.
    for (const eventName of ["pointerdown", "mousedown"]) {
      select.addEventListener(eventName, (event) => {
        if (event.button !== 0 || select.matches(":disabled")) return;
        event.preventDefault();
        select.focus();
      });
    }
    select.addEventListener("click", (event) => {
      event.preventDefault();
      if (current?.select === select) close(); else open(select);
    });
    select.addEventListener("keydown", (event) => {
      if (current?.select === select) return current.keydown(event);
      if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey && !["ArrowDown", "ArrowUp"].includes(event.key)) return;
      if (["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End", " ", "Enter", "F4"].includes(event.key) || event.key.length === 1) {
        event.preventDefault();
        open(select);
        if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", " ", "Enter", "F4"].includes(event.key)) current?.keydown(event);
      }
    });
  }
  for (const eventName of ["pointerdown", "focusin"]) {
    document.addEventListener(eventName, (event) => {
      if (current && event.target !== current.select && !current.popup.contains(event.target)) close();
    });
  }
  document.addEventListener("close", () => close(), true);
  window.addEventListener("resize", () => close());
  window.addEventListener("blur", () => close());
  window.addEventListener("scroll", (event) => {
    if (current && !current.popup.contains(event.target)) close();
  }, true);
  return { close };
}

window.NetizenSelect = initializeSelectControls();

function initializeFilter(root) {
  const name = root.dataset.sessionMulti || root.dataset.sideMulti || root.dataset.scheduleFilter;
  const single = Boolean(root.dataset.scheduleFilter);
  const prefix = single ? "schedule" : root.dataset.sideMulti ? "side" : "session";
  const valueField = single ? document.querySelector(`#schedule-filter-${name}`) : null;
  const defaults = name === "state" ? ["creating", "open"] : name === "ended" ? ["false"] : [];
  const legend = root.querySelector("legend");
  legend.id = `${prefix}-${name}-label`;
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
  popover.id = `${prefix}-${name}-options`;
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
  if (single) {
    choices.setAttribute("role", "radiogroup");
    choices.setAttribute("aria-labelledby", legend.id);
  }
  const empty = document.createElement("p");
  empty.className = "multi-filter-empty";
  empty.textContent = "没有匹配的 Project";
  empty.hidden = true;
  popover.append(clear, choices, empty);
  root.append(trigger, popover);
  let options = filterOptions[name];
  let selected = new Set(defaults);

  function renderSummary() {
    const labels = options.filter(([value]) => selected.has(value)).map(([, label]) => label);
    summary.textContent = selected.size === 0 ? "全部" : labels.join("、");
    trigger.title = summary.textContent;
    trigger.setAttribute("aria-label", `${legend.textContent}：${summary.textContent}`);
    if (valueField) valueField.value = [...selected][0] || "";
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
      input.type = single ? "radio" : "checkbox";
      input.value = value;
      input.checked = selected.has(value);
      input.addEventListener(single ? "click" : "change", () => {
        if (single) {
          selected = new Set([value]);
          close({ restoreFocus: true });
          renderChoices();
          return;
        }
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
    if (openFilter === controller) openFilter = null;
    if (restoreFocus) trigger.focus();
  }

  function visibleInputs() {
    return [...choices.children].filter((label) => !label.hidden)
      .map((label) => label.querySelector("input"));
  }

  function open() {
    openFilter?.close();
    openTimeRangeFilter?.cancel({ restoreFocus: false });
    openFilter = controller;
    search.value = "";
    filterChoices();
    popover.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    // Keep the dropdown inside the viewport even in the last grid column.
    const bounds = root.getBoundingClientRect();
    const width = popover.getBoundingClientRect().width;
    popover.style.left = `${Math.min(0, window.innerWidth - 20 - bounds.left - width)}px`;
    const inputs = visibleInputs();
    const current = single ? inputs.find((input) => input.checked) : null;
    (name === "project" ? search : current || inputs[0] || clear).focus();
  }

  const controller = {
    close,
    values: () => [...selected],
    setValues(values) {
      selected = new Set(values);
      const known = new Set(options.map(([value]) => value));
      options = [...options, ...values.filter((value) => !known.has(value)).map((value) => [value, value])];
      renderChoices();
    },
    clear() { selected.clear(); renderChoices(); },
    reset() {
      selected = new Set(defaults);
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
    if (single) close({ restoreFocus: true });
    renderChoices();
  });
  root.addEventListener("keydown", (event) => {
    if (popover.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close({ restoreFocus: true });
    } else if (event.key === "Enter" && event.target === search) {
      event.preventDefault();
    } else if (single && event.key === "Enter" && visibleInputs().includes(event.target)) {
      event.preventDefault();
      event.target.click();
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
  sessionMultiFilters.set(root.dataset.sessionMulti, initializeFilter(root));
}
for (const root of document.querySelectorAll("[data-side-multi]")) {
  sideMultiFilters.set(root.dataset.sideMulti, initializeFilter(root));
}
for (const root of document.querySelectorAll("[data-schedule-filter]")) {
  scheduleFilters.set(root.dataset.scheduleFilter, initializeFilter(root));
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

async function loadSessionProjectOptions(filters = sessionMultiFilters, isCurrent = () => true) {
  const projects = await queryProjectOptions();
  if (!isCurrent()) return;
  filters.get("project").setOptions(projects.map((project) => [
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
  for (const root of form.querySelectorAll("[data-time-range]")) {
    const kind = timeRangeControllers.get(root)?.presetKind();
    if (kind) query.set(`${root.dataset.timeRangeField || "created"}Range`, kind);
  }
  for (const root of form.querySelectorAll("[data-session-multi], [data-side-multi]")) {
    const name = root.dataset.sessionMulti || root.dataset.sideMulti;
    const filters = root.dataset.sideMulti ? sideMultiFilters : sessionMultiFilters;
    const values = filters.get(name).values();
    for (const value of values) query.append(name, value);
    if (name === "state" && values.length === 0) query.set(name, "all");
  }
  if (form.id === "session-filter") {
    // Exact links remain targeted while the user changes state or date filters.
    const identity = new URLSearchParams(state.sessionPage?.query || "").get("identity");
    if (identity) query.set("identity", identity);
    if (query.get("inventoryState") === "lazy") {
      for (const key of ["createdFrom", "createdBefore", "createdRange", "updatedFrom", "updatedBefore", "updatedRange"]) query.delete(key);
    } else if (!query.has("updatedFrom") && !query.has("updatedBefore")) {
      query.set("updatedRange", "all"); // URL intent only; never sent to the API.
    }
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
  "last-3-months": "最近 3 个月",
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

function timeRangeSummary(from, before, kind = "custom", now = new Date()) {
  if (!from && !before) return "全部时间";
  const rolling = kind.startsWith("last-");
  const start = new Date(from);
  // Show the last included minute, including a partially included rolling minute.
  const end = before ? new Date(new Date(before).getTime() - 1) : now;
  const startLabel = Number.isNaN(start.getTime()) ? from || "不限起点" : formatLocalMinute(start);
  const endLabel = !before && !rolling ? "不限终点"
    : Number.isNaN(end.getTime()) ? before : formatLocalMinute(end);
  const label = timeRangePresetLabels[kind];
  const name = label && kind !== "all" ? `${label}${!before && rolling ? "（至今）" : ""}` : "";
  const dates = [from ? start : null, before || rolling ? end : null]
    .filter((value) => value && !Number.isNaN(value.getTime()));
  const offsets = [...new Set(dates.map(utcOffsetLabel))].join(" → ");
  const heading = [name, offsets].filter(Boolean).join(" · ");
  return `${heading ? `${heading}\n` : ""}${startLabel} ~ ${endLabel}`;
}

function presetTimeRange(kind, now = new Date(), openEnded = false) {
  if (kind === "all") return allTimeRange();
  let from;
  let before;
  if (kind === "today") {
    from = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    before = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  } else if (kind === "yesterday") {
    from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    before = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  } else if (kind === "last-3-months") {
    before = new Date(now.getTime());
    from = new Date(now.getTime());
    const day = from.getDate();
    from.setDate(1);
    from.setMonth(from.getMonth() - 3);
    const lastDay = new Date(from.getFullYear(), from.getMonth() + 1, 0).getDate();
    from.setDate(Math.min(day, lastDay));
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
  const rolling = kind.startsWith("last-");
  const fromUtc = canonicalUtc(from);
  const beforeUtc = openEnded && rolling ? "" : canonicalUtc(before);
  return {
    kind,
    from: fromUtc,
    before: beforeUtc,
    startValue: localMinuteValue(from),
    endValue: localMinuteValue(new Date(before.getTime() - 1)),
    summary: timeRangeSummary(fromUtc, beforeUtc, kind, now),
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
      summary: timeRangeSummary(canonicalUtc(parsedStart.date), canonicalUtc(before)),
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
  const field = root.dataset.timeRangeField || "created";
  const label = root.querySelector("legend").textContent;
  const defaultKind = root.dataset.timeRangeDefault || "all";
  // Rolling update filters include later updates when this applied page is refreshed.
  const preset = (kind) => presetTimeRange(kind, new Date(), field === "updated");
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

  fromNode.name = `${field}From`;
  beforeNode.name = `${field}Before`;
  title.textContent = label;
  quickClear.setAttribute("aria-label", `清除${label}筛选`);
  quickClear.title = `清除${label}筛选`;
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

  let applied = preset(defaultKind);
  let draft = { ...applied };

  function setInputValidity(invalid) {
    for (const [node, name] of [[start, "start"], [end, "end"]]) {
      if (invalid === name) node.setAttribute("aria-invalid", "true");
      else node.removeAttribute("aria-invalid");
    }
  }

  function renderApplied() {
    fromNode.value = applied.from;
    beforeNode.value = applied.before;
    applied.summary = timeRangeSummary(applied.from, applied.before, applied.kind);
    const [heading, bounds] = applied.summary.split("\n");
    if (bounds) {
      const name = document.createElement("span");
      name.textContent = `${heading}\n`;
      const dates = document.createElement("span");
      dates.className = "time-range-dates";
      dates.textContent = bounds;
      summary.replaceChildren(name, dates);
    } else summary.textContent = applied.summary;
    const dates = [applied.from, applied.before || new Date()].filter(Boolean)
      .map((value) => new Date(value)).filter((value) => !Number.isNaN(value.getTime()));
    trigger.title = `${applied.summary}\n${zoneDescription(dates)}`;
    trigger.classList.toggle("has-value", applied.kind !== "all");
    trigger.setAttribute("aria-label", `${label}：${applied.summary}`);
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
    if (applied.kind === "all" || applied.kind === "custom" || applied.restored) return;
    applied = preset(applied.kind);
    renderApplied();
  }

  function openPopover() {
    openFilter?.close();
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
    refreshSummary: renderApplied,
    presetKind() { return applied.kind !== "all" && applied.kind !== "custom" ? applied.kind : null; },
    setBounds(from, before, kind = "custom") {
      if (!from && !before) { this.reset("all"); return; }
      const startDate = new Date(from);
      const beforeDate = new Date(before);
      const endDate = new Date(beforeDate.getTime() - 1);
      // URL bounds stay absolute; the preset name is presentation metadata only.
      applied = { kind: Object.hasOwn(timeRangePresetLabels, kind) && kind !== "all" ? kind : "custom", from, before, restored: true,
        startValue: Number.isNaN(startDate.getTime()) ? "" : localMinuteValue(startDate),
        endValue: Number.isNaN(endDate.getTime()) ? "" : localMinuteValue(endDate) };
      draft = { ...applied };
      closePopover({ restoreFocus: false });
      renderApplied();
    },
    reset(kind = defaultKind) {
      applied = preset(kind);
      draft = { ...applied };
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
      draft = preset(button.dataset.timeRangePreset);
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
    // A restored URL may have only one bound. Confirming it unchanged must
    // preserve that valid range; edited custom input still requires both dates.
    const unchanged = start.value === applied.startValue && end.value === applied.endValue;
    if (draft.kind === "custom" && !unchanged && !validateDraft()) {
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

function sessionLocationCell(row, session, { showIdentity = true } = {}) {
  const td = document.createElement("td");
  td.className = "session-location";
  const link = document.createElement("a");
  link.className = "chat-link";
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  const mode = document.createElement("div");
  mode.className = "meta-line";
  mode.append(badge(chatModeLabel(session.chatMode, session.scopeKind)));
  td.append(link, mode);
  updateSessionLocationCell(td, session, { showIdentity });
  row.append(td);
}

function updateSessionLocationCell(td, session, { showIdentity = true, loading = false } = {}) {
  const link = td.querySelector(".chat-link");
  link.href = session.topicOpenUrl || session.chatOpenUrl;
  link.textContent = session.chatLabelResolved
    ? session.chatLabel
    : loading ? `${chatModeLabel(session.chatMode, session.scopeKind)}（名称加载中）`
      : showIdentity ? locationFallback(session) : `${chatModeLabel(session.chatMode, session.scopeKind)}（名称暂不可用）`;
  link.title = session.topicOpenUrl
    ? "打开飞书话题"
    : "打开飞书会话";
  td.querySelector(".badge").textContent = chatModeLabel(session.chatMode, session.scopeKind);
  td.setAttribute("aria-busy", String(loading));
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
  bindingId.textContent = `Netizen 会话编号：${session.shortId}`;
  td.append(bindingId);
  const details = document.createElement("details");
  details.className = "session-identities";
  const summary = document.createElement("summary");
  summary.textContent = "会话标识";
  details.append(summary);
  for (const [label, value] of [["飞书聊天 ID（位置）", session.chatId], ["飞书话题 ID（位置）", session.topicId],
    ["Netizen 会话编号（Binding ID，管理与切换）", session.bindingId],
    ["Codex 会话 ID（Thread ID，跨会话工具）", session.nativeThreadId]]) {
    if (!value) continue;
    const field = document.createElement("label");
    field.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.readOnly = true;
    input.value = value;
    input.setAttribute("aria-label", `${label}，完整值，选中后可复制`);
    input.addEventListener("focus", () => input.select());
    input.addEventListener("click", () => input.select());
    field.append(input);
    details.append(field);
  }
  const hint = document.createElement("small");
  hint.textContent = "点击标识全选，再按 Ctrl/Cmd+C 复制。跨会话工具使用完整 Codex 会话 ID；现有管理与 /resume 使用 Netizen 编号。";
  details.append(hint);
  if (!session.nativeThreadId) {
    const pending = document.createElement("p");
    pending.className = "meta-line";
    pending.textContent = "Codex 会话 ID 尚未生成，首条消息后创建。";
    details.append(pending);
  }
  td.append(details);
  row.append(td);
}

function nativeSessionDateLabel(value) {
  if (!Number.isSafeInteger(value) || value < 0) return "—";
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function sessionUpdatedAtLabel(session) {
  return session.catalogState === "lazy" ? "尚未开始" : nativeSessionDateLabel(session.updatedAt);
}

function sessionDateLabel(value) {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function firstSessionPage(query = null) {
  return { cursor: null, nextCursor: null, previousCursors: [], number: 1, query };
}

function stepCursorPage(page, direction) {
  const next = { ...page, previousCursors: [...page.previousCursors] };
  if (direction === "next") {
    if (!page.nextCursor) return null;
    next.previousCursors.push(page.cursor);
    next.cursor = page.nextCursor;
    next.number += 1;
  } else {
    if (!next.previousCursors.length) return null;
    next.cursor = next.previousCursors.pop();
    next.number -= 1;
  }
  return next;
}

function resolveCursorPage(current, cursor, query = current.query) {
  if (cursor && typeof cursor === "object") return { ...cursor, previousCursors: [...cursor.previousCursors] };
  if (query === current.query) {
    if (cursor === undefined || cursor === current.cursor) return { ...current, previousCursors: [...current.previousCursors] };
    if (cursor && cursor === current.nextCursor) return stepCursorPage(current, "next");
  }
  return { ...firstSessionPage(query), cursor: cursor || null };
}

function renderCursorPagination(prefix, page, count) {
  document.querySelector(`#${prefix}-previous`).disabled = !page.previousCursors.length;
  document.querySelector(`#${prefix}-next`).disabled = !page.nextCursor;
  document.querySelector(`#${prefix}-page`).textContent = `第 ${page.number} 页 · ${count} 条`;
}

function moveListPage(tab, direction) {
  const page = tab === "projects" ? state.projectPage : schedulePage;
  const next = stepCursorPage(page, direction);
  if (next) return refresh(tab, next);
}

function sessionSort(kind) {
  const query = new URLSearchParams(state[`${kind}Page`]?.query || "");
  return { by: query.get("sortBy") || "updatedAt", direction: query.get("sortDirection") || "desc" };
}

function sessionFilterPage(kind = "session", { resetSort = false } = {}) {
  const query = formQuery(document.querySelector(`#${kind}-filter`), "20");
  const sort = resetSort ? { by: "updatedAt", direction: "desc" } : sessionSort(kind);
  query.set("sortBy", kind === "session" && query.get("inventoryState") === "lazy" ? "createdAt" : sort.by);
  query.set("sortDirection", sort.direction);
  return firstSessionPage(query.toString());
}

function renderSessionSort(kind) {
  const sort = sessionSort(kind);
  const applied = new URLSearchParams(state[`${kind}Page`]?.query || "");
  const lazy = kind === "session" && applied.get("inventoryState") === "lazy";
  for (const button of document.querySelectorAll("[data-session-sort]")) {
    if (button.dataset.sessionSort !== kind) continue;
    const selected = button.dataset.sortBy === sort.by;
    const descending = selected && sort.direction === "desc";
    const label = button.dataset.sortBy === "createdAt" ? lazy ? "登记时间" : "创建时间" : "最近更新";
    button.disabled = lazy && button.dataset.sortBy === "updatedAt";
    const labelNode = button.querySelector(".sort-label");
    if (labelNode) {
      labelNode.textContent = label;
      button.title = lazy ? "Netizen 登记时间；此时尚无 Codex 创建时间" : "Codex 原生会话创建时间，与创建时间筛选口径一致；按浏览器本地时区显示";
    }
    button.parentElement.setAttribute("aria-sort", selected ? descending ? "descending" : "ascending" : "none");
    button.setAttribute("aria-label", `按${label}${descending ? "由远及近" : "由近及远"}排序`
      + (selected ? `（当前${descending ? "由近及远" : "由远及近"}）` : ""));
    button.querySelector(".sort-indicator").textContent = selected ? descending ? "↓" : "↑" : "↕";
  }
}

function updateSessionDateFilters() {
  const lazy = document.querySelector("#session-inventory-state").value === "lazy";
  for (const root of document.querySelector("#session-filter").querySelectorAll("[data-time-range]")) {
    if (lazy) timeRangeControllers.get(root)?.cancel({ restoreFocus: false });
    root.disabled = lazy;
  }
}

function renderSessionFreshness() {
  const kind = state.sessionTab === "temporary" ? "side" : "session";
  const updatedAt = state[`${kind}RefreshedAt`];
  const node = document.querySelector("#sessions-refreshed-at");
  node.hidden = updatedAt == null;
  if (updatedAt == null) return;
  const date = new Date(updatedAt);
  node.dateTime = date.toISOString();
  node.textContent = `更新于 ${date.toLocaleTimeString("zh-CN", { hour12: false })}`;
  node.title = `${date.toLocaleString("zh-CN", { hour12: false })} 读取；列表不会自动刷新`;
}

async function sortSessionPage(kind, by) {
  const page = state[`${kind}Page`];
  // Sorting operates on the displayed result, not unsubmitted filter drafts.
  const query = new URLSearchParams(page.query ?? sessionFilterPage(kind).query);
  if (kind === "session" && query.get("inventoryState") === "lazy" && by === "updatedAt") return false;
  const previous = sessionSort(kind);
  query.set("sortBy", by);
  query.set("sortDirection", previous.by === by && previous.direction === "desc" ? "asc" : "desc");
  return refresh("sessions", firstSessionPage(query.toString()));
}

function renderSessionPagination(kind = "session") {
  const name = kind === "side" ? "sides" : "sessions";
  const page = state[`${kind}Page`];
  document.querySelector(`#${name}-previous`).disabled = page.previousCursors.length === 0;
  document.querySelector(`#${name}-next`).disabled = !page.nextCursor;
  document.querySelector(`#${name}-page`).textContent = `第 ${page.number} 页 · ${state[name]?.items?.length || 0} 条`;
}

function resetSessionFilters(kind = "session") {
  const form = document.querySelector(`#${kind}-filter`);
  form.reset();
  if (kind === "session") updateSessionDateFilters();
  (kind === "side" ? sideChatPicker : sessionChatPicker)?.reset();
  const filters = kind === "side" ? sideMultiFilters : sessionMultiFilters;
  for (const controller of filters.values()) controller.reset();
  for (const root of form.querySelectorAll("[data-time-range]")) {
    timeRangeControllers.get(root)?.reset();
  }
  const page = sessionFilterPage(kind, { resetSort: true });
  const query = new URLSearchParams(page.query);
  query.delete("identity");
  page.query = query.toString();
  return refresh("sessions", page);
}

let pendingSessionChatLabels = null;

function invalidateSessionListRequest() {
  state.sessionListRequest = (state.sessionListRequest || 0) + 1;
  // A failed replacement read can leave these same rows on screen. Their old
  // enrichment must stop showing a loading placeholder when it is invalidated.
  pendingSessionChatLabels?.();
  pendingSessionChatLabels = null;
}

async function loadSessionChatLabels(kind, data, page, request) {
  const name = kind === "side" ? "sides" : "sessions";
  const isCurrent = () => !state.authExpired && request === state.sessionListRequest
    && state.tab === "sessions" && state.sessionTab === (kind === "side" ? "temporary" : "ordinary")
    && state[name] === data && state[`${kind}Page`] === page;
  if (!isCurrent()) return;
  const body = document.querySelector(`#${name}-body`);
  const targets = data.items.flatMap((item, index) => item.chatLabelResolved || !item.chatId
    ? [] : [{ item, cell: body.children[index].querySelector(".session-location") }]);
  if (!targets.length) return;
  const showIdentity = kind !== "side";
  const finish = () => {
    for (const { item, cell } of targets) updateSessionLocationCell(cell, item, { showIdentity });
  };
  pendingSessionChatLabels = finish;
  for (const { item, cell } of targets) updateSessionLocationCell(cell, item, { showIdentity, loading: true });
  try {
    const query = new URLSearchParams({ chatIds: [...new Set(targets.map(({ item }) => item.chatId))].join(",") });
    const labels = await api(`/api/v1/chat-labels?${query}`);
    if (!isCurrent()) return;
    const byChat = new Map(labels.items.map((label) => [label.chatId, label]));
    for (const { item } of targets) {
      const label = byChat.get(item.chatId);
      if (!label) continue;
      // Enrichment changes display facts only; retain exact identities, topic
      // links, runtime and action grants from the successful list response.
      for (const field of ["chatLabel", "chatLabelResolved", "chatMode", "chatType", "chatOpenUrl"]) {
        if (Object.hasOwn(label, field)) item[field] = label[field];
      }
    }
  } catch {
    // Name failures are local to the location cells. Authentication expiry is
    // already handled by api(), and list status/freshness stay unchanged.
  } finally {
    if (isCurrent()) finish();
    if (pendingSessionChatLabels === finish) pendingSessionChatLabels = null;
  }
}

async function loadSessionPage(kind, page, render) {
  invalidateSessionListRequest();
  const request = state.sessionListRequest;
  const isCurrent = () => request === state.sessionListRequest;
  const name = kind === "side" ? "sides" : "sessions";
  setStatus("正在读取服务端事实…");
  try {
    const query = page.query == null
      ? formQuery(document.querySelector(`#${kind}-filter`), "20")
      : new URLSearchParams(page.query);
    if (!query.has("sortBy")) query.set("sortBy", kind === "session" && query.get("inventoryState") === "lazy" ? "createdAt" : "updatedAt");
    if (!query.has("sortDirection")) query.set("sortDirection", "desc");
    const candidate = { ...page, query: query.toString(), previousCursors: [...page.previousCursors] };
    await loadSessionProjectOptions(kind === "side" ? sideMultiFilters : sessionMultiFilters, isCurrent);
    if (!isCurrent()) return false;
    for (const field of ["created", "updated"]) query.delete(`${field}Range`);
    if (page.cursor) query.set("cursor", page.cursor);
    const data = await api(`/api/v1/${kind === "side" ? "side-topics" : "sessions"}?${query}`);
    if (!isCurrent()) return false;
    // Rows and their applied query/cursor stack are one successful result.
    // Draft edits made while awaiting the response remain in the form.
    state[name] = data;
    state[`${kind}Page`] = { ...candidate, nextCursor: data.nextCursor };
    state[`${kind}RefreshedAt`] = Date.now();
    render(data);
    renderSessionPagination(kind);
    renderSessionSort(kind);
    renderSessionFreshness();
    for (const root of document.querySelector(`#${kind}-filter`).querySelectorAll("[data-time-range]")) {
      timeRangeControllers.get(root)?.refreshSummary();
    }
    syncPageUrl();
    setStatus("已更新。");
    void loadSessionChatLabels(kind, data, state[`${kind}Page`], request);
    return true;
  } catch (error) {
    if (isCurrent()) setStatus(error.message + (state[name] ? " 当前仍显示上次成功读取的结果。" : ""), true);
    return false;
  }
}

async function loadSessions(page = state.sessionPage) {
  ensureSessionChatFilter();
  return loadSessionPage("session", page, renderSessions);
}

function renderSessions(data) {
  const query = new URLSearchParams(state.sessionPage.query || "");
  document.querySelector("#sessions-lazy-notice").hidden = query.get("inventoryState") !== "lazy";
  document.querySelector("#sessions-identity-notice").hidden = !query.has("identity");
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
    cell(row, session.catalogState === "lazy" ? sessionDateLabel(session.registeredAt) : nativeSessionDateLabel(session.createdAt), "session-time");
    cell(row, sessionUpdatedAtLabel(session), "session-time");
    const actions = actionsCell(row);
    wireSessionActions(actions, session);
    body.append(row);
  }
}

function wireSessionActions(actions, session) {
  const a = session.actions;
  const more = document.createElement("details");
  more.className = "session-more";
  const summary = document.createElement("summary");
  summary.textContent = "更多";
  summary.setAttribute("aria-label", `更多操作 · ${session.shortId}`);
  const secondary = document.createElement("div");
  secondary.className = "session-more-items";
  more.append(summary, secondary);
  more.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !more.open) return;
    event.preventDefault();
    more.open = false;
    summary.focus();
  });
  const schedule = actionButton("创建定时任务", () => openScheduleForSession(session, schedule));
  secondary.append(schedule);
  if (a.configure) {
    const button = actionButton("配置", () => openSessionSettingsEditor(session, button));
    actions.append(button);
  }
  if (a.stop) actions.append(actionButton("停止", () => window.confirm("停止这个 exact 会话的当前运行？") && mutate("/api/v1/sessions/stop", a.stop), true));
  if (a.activate) actions.append(actionButton("设为当前", () => mutate("/api/v1/sessions/activate", a.activate)));
  if (a.rename) secondary.append(actionButton("重命名", async () => {
    const name = window.prompt("新的原生会话名称");
    if (name) await mutate("/api/v1/sessions/rename", a.rename, { name });
  }));
  if (a.archive) secondary.append(actionButton("归档", () => window.confirm("确认归档？") && mutate("/api/v1/sessions/archive", a.archive), true));
  if (a.unarchive) actions.append(actionButton("恢复", () => mutate("/api/v1/sessions/unarchive", a.unarchive, { actionKind: a.unarchive.actionKind })));
  if (a.unarchiveCurrent) actions.append(actionButton("恢复并设为当前", () => mutate("/api/v1/sessions/unarchive", a.unarchiveCurrent, { actionKind: a.unarchiveCurrent.actionKind })));
  if (a.deleteLazy) secondary.append(actionButton("删除 Lazy", () => window.confirm("永久删除这个 Lazy Binding？") && mutate("/api/v1/sessions/delete-lazy", a.deleteLazy), true));
  if (a.deleteMaterialized) secondary.append(actionButton("删除", () => confirmMaterializedDelete(session) && mutate("/api/v1/sessions/delete-materialized", a.deleteMaterialized), true));
  if (a.release) secondary.append(actionButton("释放订阅", () => window.confirm("释放本进程订阅？历史不会删除。") && mutate("/api/v1/sessions/release", a.release)));
  actions.append(more);
}

async function loadSides(page = state.sidePage) {
  ensureSideChatFilter();
  return loadSessionPage("side", page, renderSides);
}

function renderSides(data) {
  const body = document.querySelector("#sides-body");
  body.replaceChildren();
  for (const side of data.items) {
    const row = document.createElement("tr");
    row.dataset.sideId = side.sideId;
    sessionLocationCell(row, side, { showIdentity: false });
    const parent = side.parentSession;
    const source = cell(row, parent?.available ? parent.title || "未命名会话" : "原会话已删除");
    const details = document.createElement("details");
    details.className = "side-identities";
    const summary = document.createElement("summary");
    summary.textContent = "标识详情";
    details.append(summary);
    for (const [label, value] of [["Side", side.sideId], ["来源 Binding", side.parentBindingId],
      ["Chat", side.chatId], ["Topic", side.topicId], ["Root Message", side.rootMessageId]]) {
      if (!value) continue;
      const line = document.createElement("div");
      line.className = "id";
      line.textContent = `${label} ${value}`;
      details.append(line);
    }
    source.append(details);
    const stateCell = document.createElement("td");
    stateCell.className = "side-session-state";
    updateSideStateCell(stateCell, side);
    row.append(stateCell);
    const runtime = cell(row, sideRuntimeLabel(side.runtime, side.sessionState));
    runtime.className = "runtime-state";
    cell(row, side.projectAlias || "原项目已不可用");
    cell(row, sessionDateLabel(side.createdAt), "session-time");
    cell(row, sessionDateLabel(side.updatedAt), "session-time");
    const actions = actionsCell(row);
    if (parent?.available) {
      const link = document.createElement("a");
      link.className = "action";
      link.textContent = "查看来源会话";
      link.href = `/sessions/ordinary?identity=${encodeURIComponent(parent.bindingId)}&updatedRange=all`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      actions.append(link);
    }
    if (side.actions.stop) actions.append(actionButton("停止", () => window.confirm("停止此临时会话的当前任务？之后仍可继续交流。") && mutate("/api/v1/side-topics/stop", side.actions.stop), true));
    if (side.actions.close) actions.append(actionButton("结束临时会话", () => window.confirm("结束此临时会话？结束后无法恢复。") && mutate("/api/v1/side-topics/close", side.actions.close), true));
    body.append(row);
  }
}

function resetSideFilters() {
  return resetSessionFilters("side");
}

async function moveSidePage(direction) {
  return moveSessionPage(direction, "side");
}

async function moveSessionPage(direction, kind = "session") {
  const candidate = stepCursorPage(state[`${kind}Page`], direction);
  if (candidate) return refresh("sessions", candidate);
}

function sideStateLabel(value) {
  return { creating: "创建中", open: "可用", closing: "结束中", closed: "已结束",
    expired: "已失效", failed: "失败", unavailable: "状态待确认" }[value] || "状态待确认";
}

function updateSideStateCell(target, side) {
  const value = side.sessionState;
  target.replaceChildren(badge(sideStateLabel(value), value === "open", ["failed", "unavailable"].includes(value)));
}

function sideRuntimeLabel(runtime, sessionState = runtime?.sessionState) {
  if (sessionState === "creating") return "创建中";
  if (!runtime) return ["closed", "expired", "failed"].includes(sessionState) ? "—" : "状态待确认";
  if (runtime.state === "closing") return "结束中";
  const value = runtime.turnState || runtime.state;
  return { idle: "空闲", open: "空闲", running: "运行中", stopping: "停止中",
    unknown: "状态待确认", creating: "创建中" }[value] || value;
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
  sessionSettingsInput("save").disabled = editor.loading || editor.saving || !editor.action
    || Boolean(progressCardSettingsError(editor.settings));
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
  const progressRead = editor.settings.progress_card_settings === null ? window.NetizenProgressCard.read() : null;
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
    await progressRead;
    if (sessionSettingsEditor === editor && serial === editor.optionsSerial) {
      editor.loading = false;
      renderSessionSettingsEditor();
    }
  }
}

function openSessionSettingsEditor(session, returnFocus) {
  if (sessionSettingsEditor?.saving || !session.actions.configure) return;
  if (sessionSettingsEditor && !closeSessionSettingsEditor()) return;
  const base = structuredClone(session.sessionSettings);
  const privateChat = session.scopeKind === "direct" || session.chatMode === "p2p";
  sessionSettingsEditor = { session, returnFocus, settings: structuredClone(base), base,
    action: session.actions.configure, models: [], catalogError: "", error: "",
    privateChat, contextAvailable: !privateChat && base.message_context_mode === "catch-up",
    loading: true, saving: false, optionsSerial: 0 };
  sessionSettingsInput("editor").hidden = false;
  sessionSettingsInput("identity").textContent = `${session.nativeTitle || "未命名 Session"} · Netizen 会话编号：${session.shortId} · ${session.projectAlias}`;
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

function closeSessionSettingsEditor({ saved = false, confirmed = false } = {}) {
  const editor = sessionSettingsEditor;
  if (!editor) return true;
  if (editor.saving) return false;
  if (!saved && !confirmed && !confirmDiscardChanges(sessionSettingsHasChanges())) return false;
  sessionSettingsEditor = null;
  sessionSettingsInput("editor").hidden = true;
  sessionSettingsInput("drawer").close();
  document.body.classList.toggle("session-settings-drawer-open", false);
  const returnFocus = editor.returnFocus?.disabled
    ? document.querySelector("#sessions [data-refresh]") : editor.returnFocus;
  returnFocus?.focus();
  return true;
}

function sessionSettingsHasChanges() {
  return Boolean(sessionSettingsEditor && sessionSettingsFingerprint(sessionSettingsEditor.settings)
    !== sessionSettingsFingerprint(sessionSettingsEditor.base));
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
  if (!editor || editor.loading || editor.saving || !editor.action || progressCardSettingsError(editor.settings)) return;
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
  closeSessionSettingsEditor({ saved: true });
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
let sideChatPicker = null;

function createSessionChatFilter(kind) {
  return window.createChatPicker(document.querySelector(`#${kind}-chat-picker`), {
    id: `${kind}-group`, label: "群聊", placeholder: "全部", emptyOption: "全部",
    fetchPage: fetchChatPage,
    onChange: (chat) => { document.querySelector(`#${kind}-chat-filter`).value = chat?.chatId || ""; },
  });
}

function ensureSessionChatFilter() {
  sessionChatPicker ||= createSessionChatFilter("session");
}

function ensureSideChatFilter() {
  sideChatPicker ||= createSessionChatFilter("side");
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

function workspaceErrorMessage(error) {
  return typeof error === "string" ? error : error?.message || "";
}

function workspacePolicyAvailable(workspace, options, project) {
  if (workspace.mode === "project") return true;
  if (!options?.isGit || options.projectAlias !== project) return false;
  return workspace.mode === "new"
    || options.items.some((item) => item.available === true && item.id === workspace.worktree_id);
}

function workspacePolicySummary(workspace, { scheduled = false } = {}) {
  if (!workspace) return "沿用原会话的实际工作目录";
  if (workspace.mode === "existing") return `使用已有 worktree：${workspace.worktree_id}`;
  if (workspace.mode === "new") return `${scheduled ? "每次执行" : "每个新会话"}新建 worktree · ${workspace.ref || "创建时 Project HEAD"}`;
  return "项目目录（不使用 worktree）";
}

function renderWorkspacePolicy(input, editor, { project, loading = false, scheduled = false } = {}) {
  const workspace = editor.workspace;
  const options = editor.workspaceOptions;
  const available = options?.isGit && options.projectAlias === project;
  const items = available ? options.items.filter((item) => item.available === true) : [];
  input("workspace").value = workspace.mode;
  for (const option of input("workspace").querySelectorAll("option")) {
    option.disabled = option.value !== "project" && (!available || option.value === "existing" && !items.length);
  }
  input("worktree-field").hidden = workspace.mode !== "existing";
  input("workspace-ref-field").hidden = workspace.mode !== "new";
  scheduleSelectOptions(input("worktree"), [["", "选择受管 worktree"],
    ...items.map((item) => [item.id, `已有 worktree · ${item.branch || "detached"} · ${item.cwd}`])],
  workspace.worktree_id || "");
  input("worktree").required = workspace.mode === "existing";
  input("workspace-ref").value = workspace.ref || "";
  for (const field of ["workspace", "worktree", "workspace-ref"]) input(field).disabled = loading;
  input("workspace-refs").replaceChildren();
  for (const branch of options?.branches || []) {
    const option = document.createElement("option");
    option.value = branch.ref;
    option.textContent = branch.name;
    input("workspace-refs").append(option);
  }
  const notes = [];
  if (loading) notes.push("正在读取 worktree 选项…");
  if (editor.workspaceError) notes.push(workspaceErrorMessage(editor.workspaceError));
  else if (options?.isGit === false) notes.push("该 Project 不是 Git 仓库，只支持项目目录。");
  else if (!project) notes.push("先选择 Project，再选择 worktree 使用方式。");
  if (workspace.mode === "existing") {
    notes.push(scheduled ? "各次执行共享所选 worktree 的文件和分支；资源不可用时保留计划并跳过执行。"
      : "新会话共享所选 worktree 的文件和分支；删除或失效后保留此配置，自动创建会话时明确报错。");
    if (!loading && !workspacePolicyAvailable(workspace, options, project)) {
      notes.push("所选 worktree 当前不可用，请重新选择；不会切回项目目录。");
    }
  } else if (workspace.mode === "new") {
    notes.push(scheduled
      ? "每次执行新建 detached worktree；该次会话的后续消息复用。留空使用执行时 Project 的 HEAD，预览和保存不创建目录。"
      : "每个自动创建的新会话新建 detached worktree；后续消息复用。留空使用创建时 Project 的 HEAD，保存配置不创建目录。");
    if (!loading && !available) notes.push("当前无法使用新建 worktree 方式，请检查 Project。");
  } else notes.push(scheduled ? "每次执行创建的会话使用 Project 的项目目录。" : "新会话使用 Project 的项目目录。");
  input("workspace-help").textContent = notes.join(" ");
  input("workspace-help").classList.toggle("error", !loading && !workspacePolicyAvailable(workspace, options, project));
}

function changeWorkspacePolicy(input, editor, field) {
  if (field === "workspace") {
    const mode = input("workspace").value;
    editor.workspace = mode === "existing" ? { mode, worktree_id: "" } : { mode };
  } else if (field === "worktree") {
    editor.workspace = { mode: "existing", worktree_id: input(field).value };
  } else {
    const ref = input(field).value.trim();
    editor.workspace = { mode: "new", ...(ref ? { ref } : {}) };
  }
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
  syncPageUrl();
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
  renderDefaultWorkspace();
  const notes = [editor.loading ? "正在读取配置…" : "", editor.sourceNote, editor.error, editor.catalogError];
  if (turn && !model) notes.push("已保存的模型当前不可用，选择保持不变；可显式选择其他模型。");
  if (editor.contextAvailable === false) notes.push("单聊不支持补齐未读上下文，请使用当前消息。");
  defaultsInput("note").textContent = notes.filter(Boolean).join(" ");
  defaultsInput("note").classList.toggle("error", Boolean(editor.error));
  defaultsInput("save").disabled = defaultsBusy || editor.loading || !editor.action || !defaultWorkspaceAvailable()
    || Boolean(progressCardSettingsError(editor.settings));
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

function defaultWorkspaceAvailable() {
  const editor = defaultsEditor;
  return !editor || workspacePolicyAvailable(editor.workspace, editor.workspaceOptions, defaultsInput("project").value);
}

function renderDefaultWorkspace() {
  renderWorkspacePolicy(defaultsInput, defaultsEditor, {
    project: defaultsInput("project").value, loading: defaultsEditor.loading,
  });
}

async function changeDefaultProject() {
  const editor = defaultsEditor;
  if (!editor || defaultsBusy) return;
  editor.workspace = { mode: "project" };
  editor.workspaceOptions = null;
  editor.workspaceError = "";
  await loadDefaultOptions();
}

function changeDefaultWorkspace(field) {
  const editor = defaultsEditor;
  if (!editor || editor.loading || defaultsBusy) return;
  changeWorkspacePolicy(defaultsInput, editor, field);
  renderDefaultSettings();
}

async function loadDefaultOptions(resolveChat = false) {
  const editor = defaultsEditor;
  if (!editor) return;
  const serial = ++editor.serial;
  const initialOptions = serial === 1 && !resolveChat;
  const chatId = editor.kind === "chat" ? defaultsInput("chat").value.trim() : "";
  editor.loading = true;
  editor.error = "";
  editor.workspaceOptions = null;
  editor.workspaceError = "";
  renderDefaultSettings();
  let progressRead = editor.settings.progress_card_settings === null ? window.NetizenProgressCard.read() : null;
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
        editor.workspace = structuredClone(effective.workspace);
        renderDefaultProject(effective.project);
        editor.touched = true;
      } else {
        editor.settings = defaultScheduleSessionSettings();
        editor.workspace = { mode: "project" };
        renderDefaultProject("");
        editor.touched = false;
      }
      editor.sourceNote = view.exact ? "修改本聊天的精确配置。"
        : view.effective ? "当前使用群名规则；保存后成为本聊天的精确配置。" : "本聊天尚未配置默认会话。";
      if (view.match_error) editor.sourceNote += ` ${view.match_error}`;
    }
    const query = new URLSearchParams({ mode: "options" });
    if (chatId) query.set("chat_id", chatId);
    const project = defaultsInput("project").value;
    if (project) query.set("project", project);
    const data = await api(`/api/v1/defaults?${query}`);
    if (defaultsEditor !== editor || serial !== editor.serial) return;
    editor.models = data.models;
    editor.contextAvailable = data.context_mode_available;
    editor.workspaceOptions = data.workspace_options || null;
    editor.workspaceError = data.workspace_options_error || "";
    if (editor.kind === "chat") chatTarget("defaults").resolveKind(editor.contextAvailable);
    editor.catalogError = typeof data.model_catalog_error === "string"
      ? data.model_catalog_error : data.model_catalog_error?.message || "";
    if (!editor.record && !editor.touched && !view?.effective) editor.settings = structuredClone(data.session_settings);
  } catch (error) {
    if (defaultsEditor !== editor || serial !== editor.serial) return;
    editor.error = `${error.message} 请重新打开表单后操作。`;
    if (resolveChat) editor.action = null;
  } finally {
    if (defaultsEditor === editor && serial === editor.serial
        && editor.settings.progress_card_settings === null && !progressRead) {
      progressRead = window.NetizenProgressCard.read();
    }
    await progressRead;
    if (defaultsEditor === editor && serial === editor.serial) {
      // Initial catalog/defaults hydration is not a user edit. Later target
      // resolution must not erase the original values used for discard checks.
      if (initialOptions) editor.discardBase = defaultEditorValues();
      editor.loading = false;
      renderDefaultSettings();
    }
  }
}

function openDefaultEditor(kind, record = null) {
  if (defaultsBusy || defaultsNeedsRefresh || !state.defaults) return;
  if (defaultsEditor && !closeDefaultEditor()) return;
  defaultsEditor = { kind, record, action: record ? record.actions.save : state.defaults[kind].actions.create,
    returnFocus: document.activeElement,
    settings: structuredClone(record?.session_settings || defaultScheduleSessionSettings()),
    models: [], serial: 0, loading: true, touched: false, contextAvailable: null,
    workspace: structuredClone(record ? record.workspace : { mode: "project" }), workspaceOptions: null, workspaceError: "",
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
  defaultsEditor.discardBase = defaultEditorValues();
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

function closeDefaultEditor({ saved = false, confirmed = false } = {}) {
  if (defaultsBusy) return false;
  if (!saved && !confirmed && !confirmDiscardChanges(defaultEditorHasChanges())) return false;
  chatTargetControllers.get("defaults")?.close();
  defaultsEditor = null;
  defaultsInput("editor").hidden = true;
  defaultsInput("drawer").close();
  document.body.classList.toggle("defaults-drawer-open", false);
  return true;
}

function defaultEditorValues() {
  const editor = defaultsEditor;
  if (!editor) return null;
  return { kind: editor.kind, project: defaultsInput("project").value,
    target: defaultsInput(editor.kind === "chat" ? "chat" : "keyword").value,
    settings: sessionSettingsFingerprint(editor.settings), workspace: structuredClone(editor.workspace) };
}

function defaultEditorHasChanges() {
  return Boolean(defaultsEditor && JSON.stringify(defaultEditorValues())
    !== JSON.stringify(defaultsEditor.discardBase));
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
  closeDefaultEditor({ saved: true });
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
  if (!editor || editor.loading || !editor.action || defaultsBusy || !defaultWorkspaceAvailable()
      || progressCardSettingsError(editor.settings)) return;
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
    session_settings: structuredClone(editor.settings), workspace: structuredClone(editor.workspace) };
  if (editor.workspaceOptions?.projectAlias === definition.project) {
    definition.expected_project_revision = editor.workspaceOptions.projectRevision;
  }
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

function defaultSettingsCell(row, settings, workspace) {
  const td = document.createElement("td");
  const model = settings.turn_settings;
  const title = document.createElement("div");
  title.textContent = model ? `${model.model_id} / ${model.effort_id} / ${model.service_tier_id}` : "继承 Codex";
  const detail = document.createElement("div");
  detail.className = "default-settings-detail";
  detail.textContent = scheduleSessionSummary(settings).split(" · ").slice(1).join(" · ");
  const location = document.createElement("div");
  location.className = "default-settings-detail";
  location.textContent = workspacePolicySummary(workspace);
  td.append(title, detail, location);
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
      defaultSettingsCell(row, rule.session_settings, rule.workspace);
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

function sessionSettingsFingerprint(settings) {
  const turn = settings.turn_settings;
  return JSON.stringify([turn ? [turn.model_id, turn.effort_id, turn.service_tier_id] : null,
    settings.message_context_mode, settings.reaction_pulse_enabled,
    settings.progress_card_enabled, settings.completion_mention_enabled,
    settings.progress_card_settings === null ? null
      : progressCardFields.map((field) => settings.progress_card_settings[field.replaceAll("-", "_")])]);
}

function confirmDiscardChanges(changed) {
  return !changed || window.confirm("有未保存的修改，确定放弃吗？");
}

function hasUnsavedEditors() {
  return projectEditorHasChanges() || sessionSettingsHasChanges() || defaultEditorHasChanges() || scheduleEditorHasChanges()
    || Boolean(pendingScheduleDraft?.dirty) || Boolean(window.NetizenSessionAccess?.dirty())
    || Boolean(window.NetizenProgressCard?.dirty())
    || Boolean(window.NetizenWorktrees?.dirty());
}

// Navigation asks once before closing any editor. Busy submissions retain the
// existing fail-closed boundary; this helper never cancels or repeats a POST.
function confirmDiscardEditors() {
  if (projectEditor?.saving || sessionSettingsEditor?.saving || defaultsBusy || (scheduleEditor && scheduleInput("fields").disabled)
      || window.NetizenSessionAccess?.busy() || window.NetizenProgressCard?.busy()
      || window.NetizenWorktrees?.busy()) return false;
  if (!confirmDiscardChanges(hasUnsavedEditors())) return false;
  closeProjectEditor({ confirmed: true });
  closeSessionSettingsEditor({ confirmed: true });
  closeDefaultEditor({ confirmed: true });
  if (scheduleEditor) closeScheduleEditor({ confirmed: true });
  cancelScheduleSelection({ confirmed: true });
  window.NetizenSessionAccess?.discard();
  window.NetizenProgressCard?.discard();
  window.NetizenWorktrees?.discard();
  return true;
}

window.addEventListener("beforeunload", (event) => {
  if (!hasUnsavedEditors()) return;
  event.preventDefault();
  event.returnValue = "";
});

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
    workspace_unavailable: "worktree 不可用，本次未执行",
    workspace_creation_failed: "worktree 创建失败，本次未执行",
    workspace_creation_unknown: "worktree 创建结果待确认，本次未执行",
    workspace_handoff_failed: "worktree 已保留，会话交接失败",
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

function scheduleWorktreeReceipt(run, { details = true } = {}) {
  if (!run?.worktree_id) return "";
  const labels = [`worktree：${run.worktree_id}`];
  const receipt = run.worktree;
  if (details && receipt) {
    if (receipt.cwd) labels.push(`工作目录：${receipt.cwd}`);
    if (receipt.root && receipt.root !== receipt.cwd) labels.push(`根目录：${receipt.root}`);
    if (!receipt.cwd && !receipt.root) labels.push("资源登记已不可用");
    if (receipt.creation_state === "unknown") labels.push("创建结果待确认");
    else if (receipt.creation_state === "creating") labels.push("创建尚未确认完成");
  }
  return labels.join(" · ");
}

function scheduleExecutionLabel(execution) {
  const label = scheduleRunStatus(execution.status);
  if (execution.kind === "none") return label;
  if (execution.status === "deleted") return label;
  if (execution.kind === "current") return `${execution.is_last ? "最后一次" : "本次"}${label}`;
  if (["skipped_busy", "blocked_unknown", "project_disabled", "project_unavailable", "workspace_unavailable"].includes(execution.status)) return label;
  return `上次${label}`;
}

function scheduleBlockedLabel(plan) {
  const primary = { blocked_unknown: "执行结果待确认", project_disabled: "Project 已停用",
    project_unavailable: "Project 不可用", target_inactive: "原会话已切走或归档，自动暂停",
    target_missing: "原会话不可用", target_mismatch: "原会话位置不匹配" }[plan.blocked_reason] || "";
  const workspace = workspaceErrorMessage(plan.workspace_error)
    || ([plan.blocked_reason, plan.suspended_reason].includes("workspace_unavailable") ? "worktree 不可用" : "");
  return [primary, workspace].filter(Boolean).join(" · ");
}

function scheduleTargetLabel(plan) {
  return plan.target_kind === "binding" ? `原会话 · ${plan.target_label || plan.target_binding_id}` : "每次新建独立话题";
}

function scheduleInheritedWorkspace(plan) {
  const workspace = plan.effective_workspace;
  if (!workspace) return "沿用原会话的实际工作目录；目录信息暂未取得。";
  const labels = [workspace.working_cwd
    ? `${workspace.native_cwd_unresolved ? "已登记" : "已选"}工作目录：${workspace.working_cwd}`
    : "工作目录尚未登记"];
  if (workspace.worktree_id) labels.push(`worktree：${workspace.worktree_id}`);
  labels.push(workspace.native_cwd_unresolved
    ? "实际目录由原会话确定，本次未查询原生目录。" : "执行时沿用原会话的工作目录。");
  return labels.join(" · ");
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
let scheduleDetailReturnFocus = null;
let scheduleRunsSerial = 0;
let scheduleRunsLoading = false;
let scheduleRunsPage = { cursor: null, nextCursor: null, previousCursors: [], number: 1 };
let scheduleRunsRetryPage = null;
let scheduleListCursor = null;
let scheduleListQuery = null;
let schedulePage = firstSessionPage();
let scheduleListSerial = 0;
let scheduleProjects = [];
let pendingScheduleDraft = null;
const pendingScheduleMutations = new Set();

function scheduleInput(id) { return document.querySelector(`#schedule-${id}`); }

const scheduleFieldErrors = new Map();

function clearScheduleFieldError(field) {
  const error = scheduleFieldErrors.get(field);
  if (!error) return;
  error.note.hidden = true;
  error.control.removeAttribute("aria-invalid");
  const describedBy = (error.control.getAttribute("aria-describedby") || "").split(" ")
    .filter((id) => id && id !== error.note.id).join(" ");
  if (describedBy) error.control.setAttribute("aria-describedby", describedBy);
  else error.control.removeAttribute("aria-describedby");
  scheduleFieldErrors.delete(field);
}

function setScheduleFieldError(field, message) {
  clearScheduleFieldError(field);
  const control = field === "chat" && !scheduleInput("group-picker").hidden
    ? scheduleInput("group-picker").querySelector(".chat-picker-trigger")
    : scheduleInput(field === "binding" ? "select-session" : field);
  let note = scheduleInput(`${field}-error`);
  if (!note) {
    note = document.createElement("span");
    note.id = `schedule-${field}-error`;
    note.className = "schedule-field-error";
  }
  // Chat mode can change between a picker and an ID input. Keep its error next
  // to the currently visible control, not inside the hidden previous mode.
  const container = field === "chat" && !scheduleInput("group-picker").hidden ? scheduleInput("group-picker")
    : field === "weekdays" ? control : control.parentElement;
  if (note.parentElement !== container) container.append(note);
  note.textContent = message;
  note.hidden = false;
  control.setAttribute("aria-invalid", "true");
  control.setAttribute("aria-describedby", [control.getAttribute("aria-describedby"), note.id].filter(Boolean).join(" "));
  scheduleFieldErrors.set(field, { control, note });
}

function focusScheduleError() {
  const control = scheduleInput("editor").querySelector("[aria-invalid]");
  if (!control) return;
  for (let parent = control.parentElement; parent; parent = parent.parentElement) {
    if (parent.matches("details")) parent.open = true;
  }
  const focus = control.querySelector("input, select, button") || control;
  focus.focus({ preventScroll: true });
  control.scrollIntoView?.({ block: "center" });
}

function showScheduleFormError(message = "") {
  scheduleInput("form-error").textContent = message;
  scheduleInput("form-error").hidden = !message;
}

function validateScheduleFields({ saving = false } = {}) {
  for (const field of [...scheduleFieldErrors.keys()]) clearScheduleFieldError(field);
  const required = saving ? [["name", "请填写任务名称。"], ["instructions", "请填写任务指令。"]] : [];
  if (scheduleTargetKind() === "binding") required.push(["binding", "请选择具体会话。"]);
  else {
    if (saving) required.push(["project", "请选择 Project。"]);
    required.push(["chat", "请选择群聊或填写聊天 ID。"]);
  }
  const kind = scheduleInput("kind").value;
  required.push(["timezone", "请填写时区，例如 Asia/Shanghai。"]);
  if (kind === "once") required.push(["at", "请填写触发日期与时间。"]);
  if (["daily", "weekly"].includes(kind)) required.push(["time", "请填写当地时间。"]);
  const offset = kind === "once" ? "offset" : "end-offset";
  if (!scheduleInput(`${offset}-field`).hidden) required.push([offset, "请选择这一天的哪一次时间。"]);
  for (const [field, message] of required) {
    if (!scheduleInput(field).value.trim()) setScheduleFieldError(field, message);
  }
  const fields = [...required.map(([field]) => field), ...(kind === "once" ? [] : ["end-at"])];
  for (const field of fields) {
    const input = scheduleInput(field);
    if (input.validity && !input.validity.valid && !scheduleFieldErrors.has(field)) {
      setScheduleFieldError(field, ["time", "at", "end-at"].includes(field)
        ? "请填写有效时间，精确到分钟。" : input.validationMessage);
    }
  }
  if (kind === "interval") {
    const minutes = Number(scheduleInput("every").value);
    if (!Number.isSafeInteger(minutes) || minutes < 1) setScheduleFieldError("every", "请输入至少 1 的整数分钟。");
  }
  if (kind === "weekly" && !Array.from(scheduleInput("weekdays").querySelectorAll("input")).some((input) => input.checked)) {
    setScheduleFieldError("weekdays", "请至少选择一天。");
  }
  if (saving && !scheduleWorkspaceCanSave()) {
    setScheduleFieldError("workspace", scheduleEditor.workspaceLoading ? "worktree 选项仍在读取，请稍后保存。"
      : workspaceErrorMessage(scheduleEditor.workspaceError) || "请选择可用的 worktree 使用方式。");
  }
  if (scheduleFieldErrors.size) {
    if (scheduleEditor?.action) showScheduleFormError("请检查标红的表单项。");
    focusScheduleError();
    return false;
  }
  // A spent action still requires explicit reconciliation, even after editing
  // fields or previewing again. Never erase that instruction with validation.
  if (scheduleEditor?.action) showScheduleFormError();
  return true;
}

function showScheduleRequestError(error, { submitted = false } = {}) {
  const field = ({ ambiguous_local_time: "offset", ambiguous_end_time: "end-offset",
    invalid_chat_id: "chat", not_group_chat: "chat",
    project_disabled: "project", project_unavailable: "project", project_deleting: "project", project_changed: "project",
    workspace_unavailable: "workspace" })[error.code];
  if (field && (!["chat", "project"].includes(field) || scheduleTargetKind() !== "binding")) {
    setScheduleFieldError(field, error.message);
    focusScheduleError();
  }
  showScheduleFormError(error.message + (submitted || !scheduleEditor?.action ? " 请刷新列表并重新打开编辑，核查后再操作。" : ""));
}

function scheduleTargetKind() {
  return Array.from(scheduleInput("target-kind").querySelectorAll("input"))
    .find((input) => input.checked).value;
}

function renderScheduleTarget() {
  const editor = scheduleEditor;
  if (!editor) return;
  const binding = scheduleTargetKind() === "binding";
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
  scheduleInput("auto-archive-field").hidden = binding;
  scheduleInput("auto-archive").disabled = binding;
  renderScheduleWorkspace();
  const target = editor.targetSession;
  const selected = editor.plan?.target_binding_id || target?.bindingId || "";
  scheduleInput("binding").value = selected;
  scheduleInput("select-session").hidden = Boolean(selected);
  scheduleInput("binding-summary").textContent = target
    ? `${target.nativeTitle || target.shortId || "未命名会话"}\n${target.chatLabel || target.chatId} · ${target.projectAlias}\nNetizen 会话编号：${target.shortId || target.bindingId.slice(0, 8)}${target.topicId ? ` · 话题 ${target.topicId}` : ""}`
      + (target.pointerState !== "current" || target.catalogState === "archived" ? "\n目标当前不是可执行的当前会话，计划将自动暂停；不会自动切换或恢复会话。" : "")
    : selected ? `${editor.plan.target_label || selected}\n${editor.plan.chat?.chatLabel || editor.plan.chat_id} · ${editor.plan.project_alias}`
      : "请前往 Sessions 找到具体会话，再点击该行的“创建定时任务”。已填写内容会在当前页面保留。";
  if (binding && editor.plan) scheduleInput("binding-summary").textContent += `\n${scheduleInheritedWorkspace(editor.plan)}`;
  scheduleInput("preview").disabled = binding && !selected;
}

function renderScheduleProjects() {
  scheduleFilters.get("project").setOptions(scheduleProjects.map((project) => [project.alias,
    project.enabled ? project.alias : `${project.alias}（已停用）`]));
  if (!scheduleEditor) return;
  const selected = scheduleInput("project").value || scheduleEditor.plan?.project_alias || "";
  const available = scheduleProjects.filter((project) => project.enabled || project.alias === selected);
  scheduleSelectOptions(scheduleInput("project"), [["", "选择 Project"],
    ...available.map((project) => [project.alias,
      project.enabled ? project.alias : `${project.alias}（已停用）`])], selected);
}

function closeScheduleEditor({ saved = false, confirmed = false, preserveDraft = false } = {}) {
  if (scheduleInput("fields").disabled && !saved) return false;
  if (!saved && !confirmed && !preserveDraft && !confirmDiscardChanges(scheduleEditorHasChanges())) return false;
  const returnFocus = scheduleEditor?.returnFocus;
  scheduleEditorSerial += 1;
  chatTargetControllers.get("schedule")?.close();
  scheduleEditor = null;
  invalidateSchedulePreview();
  scheduleInput("editor").hidden = true;
  scheduleInput("drawer").close();
  document.body.classList.toggle("schedule-drawer-open", false);
  returnFocus?.focus();
  return true;
}

function scheduleEditorValues() {
  if (!scheduleEditor) return null;
  const binding = scheduleTargetKind() === "binding";
  const kind = scheduleInput("kind").value;
  const rule = { kind, timezone: scheduleInput("timezone").value };
  for (const field of kind === "once" ? ["at", "offset"]
    : kind === "interval" ? ["every", "end-at", "end-offset"] : ["time", "end-at", "end-offset"]) {
    rule[field] = scheduleInput(field).value;
  }
  if (kind === "weekly") rule.weekdays = Array.from(scheduleInput("weekdays").querySelectorAll("input"))
    .filter((input) => input.checked).map((input) => input.value);
  return { name: scheduleInput("name").value, instructions: scheduleInput("instructions").value,
    enabled: scheduleInput("enabled").checked, targetKind: scheduleTargetKind(),
    autoArchive: binding ? null : scheduleInput("auto-archive").checked,
    binding: binding ? scheduleInput("binding").value : "",
    project: binding ? "" : scheduleInput("project").value,
    chat: binding ? "" : scheduleInput("chat").value, rule,
    settings: binding ? null : sessionSettingsFingerprint(scheduleEditor.sessionDraft),
    workspace: binding ? null : structuredClone(scheduleEditor.workspace) };
}

function scheduleEditorHasChanges() {
  return Boolean(scheduleEditor && JSON.stringify(scheduleEditorValues())
    !== JSON.stringify(scheduleEditor.discardBase));
}

function captureScheduleDraft() {
  const fields = ["name", "instructions", "enabled", "auto-archive", "kind", "timezone", "time", "at", "offset", "end-at", "end-offset", "every"];
  return {
    discardBase: structuredClone(scheduleEditor.discardBase), dirty: scheduleEditorHasChanges(),
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
  if (!scheduleEditor || scheduleEditor.plan || scheduleInput("fields").disabled) return;
  pendingScheduleDraft = captureScheduleDraft();
  closeScheduleEditor({ preserveDraft: true });
  document.querySelector("#session-schedule-selection").hidden = false;
  document.querySelector("#session-schedule-saved").hidden = true;
  selectTab("sessions", "ordinary", { preserveEditors: true });
}

function cancelScheduleSelection({ confirmed = false } = {}) {
  if (!confirmed && !confirmDiscardChanges(Boolean(pendingScheduleDraft?.dirty))) return false;
  pendingScheduleDraft = null;
  scheduleEditorSerial += 1;
  document.querySelector("#session-schedule-selection").hidden = true;
  return true;
}

async function openScheduleForSession(session, returnFocus) {
  if (scheduleEditor || returnFocus.disabled) return;
  const serial = ++scheduleEditorSerial;
  const draft = pendingScheduleDraft;
  returnFocus.disabled = true;
  try {
    // Obtain a fresh management grant without changing either tab's list/filter state.
    const data = await api("/api/v1/schedules?mode=list");
    if (serial !== scheduleEditorSerial || state.tab !== "sessions" || state.sessionTab === "temporary") return;
    const loading = openScheduleEditor(null, { targetSession: session, returnFocus, draft,
      action: data.actions.create, timezone: data.default_timezone });
    // The editor takes ownership synchronously; catalog loading must not keep
    // a second draft alive after the user saves or discards this editor.
    if (scheduleEditor?.targetSession === session) {
      pendingScheduleDraft = null;
      document.querySelector("#session-schedule-selection").hidden = true;
    }
    await loading;
  } catch (error) { setStatus(error.message, true); }
  finally { returnFocus.disabled = false; }
}

function changeScheduleChat(resolve = true) {
  const editor = scheduleEditor;
  if (!editor) return;
  clearScheduleFieldError("chat");
  editor.optionsSerial += 1;
  editor.workspaceLoading = false;
  editor.chatMessage = "";
  editor.contextAvailable = chatTarget("schedule").kind() === "p2p" ? false : null;
  if (editor.contextAvailable === false) {
    editor.sessionDraft.message_context_mode = "current-only";
    editor.settingsTouched = true;
  }
  invalidateSchedulePreview();
  renderScheduleSessionSettings();
  renderScheduleWorkspace();
  if (resolve && scheduleInput("chat").value.trim()) loadScheduleSessionOptions();
}

function scheduleRuleFingerprint() {
  return JSON.stringify({ rule: readScheduleRule(),
    project: scheduleTargetKind() === "binding" ? null : scheduleInput("project").value,
    workspace: scheduleTargetKind() === "binding" ? null : scheduleEditor?.workspace,
    utc_offset: scheduleInput("kind").value === "once" ? scheduleInput("offset").value : "",
    end_utc_offset: scheduleInput("kind").value !== "once" ? scheduleInput("end-offset").value : "" });
}

function resetScheduleOffset() {
  clearScheduleFieldError("offset");
  scheduleInput("offset-field").hidden = true;
  scheduleInput("offset").replaceChildren();
  scheduleInput("offset").value = "";
}

function resetScheduleEndOffset() {
  clearScheduleFieldError("end-offset");
  scheduleInput("end-offset-field").hidden = true;
  scheduleInput("end-offset").replaceChildren();
  scheduleInput("end-offset").value = "";
}

function defaultScheduleSessionSettings() {
  return { turn_settings: null, reaction_pulse_enabled: false, progress_card_enabled: true, completion_mention_enabled: true,
    message_context_mode: "current-only", progress_card_settings: null };
}

const progressCardFields = ["show-commentary", "show-operations", "show-plan", "commentary-limit",
  "operation-limit", "commentary-max-chars", "operation-max-chars", "organization"];

function progressCardSettings(settings) {
  return settings.progress_card_settings || window.NetizenProgressCard?.current();
}

function progressCardSettingsError(settings) {
  const progress = progressCardSettings(settings);
  if (!progress) return "";
  for (const [key, label, maximum] of [
    ["commentary_limit", "进展条数", 8], ["operation_limit", "操作条数", 16],
    ["commentary_max_chars", "进展摘要长度", 320], ["operation_max_chars", "操作摘要长度", 240],
  ]) {
    if (!Number.isInteger(progress[key]) || progress[key] < 1 || progress[key] > maximum) {
      return `${label}必须是 1 到 ${maximum} 之间的整数。`;
    }
  }
  return "";
}

function renderProgressCardSettings(input, settings) {
  const container = input("progress-settings");
  if (!container) return;
  const progress = progressCardSettings(settings);
  container.hidden = !settings.progress_card_enabled;
  const source = input("progress-source");
  if (source) source.hidden = settings.progress_card_settings !== null;
  for (const node of container.querySelectorAll("input, select")) {
    const field = node.id.split("-progress-").at(-1);
    const key = field.replaceAll("-", "_");
    if (field.startsWith("show-")) node.checked = progress?.[key] || false;
    else node.value = progress?.[key] ?? "";
    node.disabled = !progress || !settings.progress_card_enabled
      || (field.startsWith("commentary-") && !progress.show_commentary)
      || (field.startsWith("operation-") && !progress.show_operations);
  }
  for (const [kind, shown] of [["commentary", progress?.show_commentary], ["operation", progress?.show_operations]]) {
    input(`progress-${kind}-capacity`).hidden = !shown;
  }
  const error = progress ? progressCardSettingsError(settings) : "读取全局配置后可编辑过程卡细项。";
  const note = input("progress-error");
  note.textContent = error;
  note.hidden = !error;
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
  renderProgressCardSettings(input, settings);
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
  else if (field.startsWith("progress-") && progressCardFields.includes(field.slice(9))) {
    const progress = progressCardSettings(settings);
    if (!progress) return;
    const key = field.slice(9).replaceAll("-", "_");
    const node = input(field);
    const value = key.startsWith("show_") ? node.checked
      : key === "organization" || node.value === "" ? node.value : Number(node.value);
    settings.progress_card_settings = { ...progress, [key]: value };
  }
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
  if (editor.chatMessage) notes.push(editor.chatMessage);
  if (current && !model && !editor.catalogMessage) notes.push("已保存的模型当前不可用，原配置保持不变。可调整其他配置，或显式选择新模型。");
  if (editor.contextAvailable === false) notes.push("私聊目标不支持补齐未读上下文；请选择当前消息后保存。");
  scheduleInput("session-note").textContent = notes.join(" ");
}

function scheduleWorkspaceChanged() {
  const editor = scheduleEditor;
  return !editor?.plan || scheduleInput("project").value !== editor.plan.project_alias
    || JSON.stringify(editor.workspace) !== JSON.stringify(editor.plan.workspace);
}

function scheduleWorkspaceCanSave() {
  if (!scheduleEditor || scheduleTargetKind() === "binding" || !scheduleWorkspaceChanged()) return true;
  return workspacePolicyAvailable(scheduleEditor.workspace, scheduleEditor.workspaceOptions, scheduleInput("project").value);
}

function renderScheduleWorkspace() {
  const editor = scheduleEditor;
  if (!editor) return;
  const binding = scheduleTargetKind() === "binding";
  scheduleInput("workspace-fields").hidden = binding;
  if (binding) {
    for (const field of ["workspace", "worktree", "workspace-ref"]) scheduleInput(field).disabled = true;
    scheduleInput("worktree").required = false;
    return;
  }
  const unchanged = !scheduleWorkspaceChanged();
  renderWorkspacePolicy(scheduleInput, { ...editor,
    workspaceError: editor.workspaceError || (unchanged ? editor.plan.workspace_error : null) }, { project: scheduleInput("project").value,
    loading: editor.workspaceLoading, scheduled: true });
  if (unchanged && (!workspacePolicyAvailable(editor.workspace, editor.workspaceOptions, scheduleInput("project").value)
      || editor.plan.workspace_available === false)) {
    scheduleInput("workspace-help").textContent += " 可保留此策略修改计划，之后执行仍会重新校验。";
  }
}

function updateScheduleSaveAvailability() {
  scheduleInput("save").disabled = !schedulePrepared || !scheduleEditor?.action
    || (!schedulePrepared.hasFuture && !scheduleEditor.plan) || !scheduleWorkspaceCanSave();
}

async function changeScheduleProject() {
  if (!scheduleEditor || scheduleInput("fields").disabled) return;
  scheduleEditor.workspace = { mode: "project" };
  clearScheduleFieldError("workspace");
  invalidateSchedulePreview();
  await loadScheduleSessionOptions();
}

function changeScheduleWorkspace(field) {
  if (!scheduleEditor || scheduleEditor.workspaceLoading || scheduleInput("fields").disabled
      || scheduleTargetKind() === "binding") return;
  changeWorkspacePolicy(scheduleInput, scheduleEditor, field);
  clearScheduleFieldError("workspace");
  invalidateSchedulePreview();
  renderScheduleWorkspace();
}

async function loadScheduleSessionOptions() {
  const editor = scheduleEditor;
  if (!editor) return;
  const chatId = scheduleInput("chat").value.trim();
  const serial = ++editor.optionsSerial;
  const query = new URLSearchParams({ mode: "options" });
  if (chatId && scheduleTargetKind() !== "binding") query.set("chat_id", chatId);
  if (scheduleTargetKind() !== "binding" && scheduleInput("project").value) {
    query.set("project", scheduleInput("project").value);
  }
  editor.workspaceOptions = null;
  editor.workspaceError = null;
  editor.workspaceLoading = query.has("project");
  renderScheduleWorkspace();
  updateScheduleSaveAvailability();
  try {
    const data = await api(`/api/v1/schedules?${query}`);
    if (scheduleEditor !== editor || serial !== editor.optionsSerial) return;
    editor.models = data.models;
    editor.contextAvailable = data.chat_info_error ? null : data.context_mode_available;
    editor.chatMessage = data.chat_info_error?.message || "";
    editor.workspaceOptions = data.workspace_options || null;
    editor.workspaceError = data.workspace_options_error || null;
    if (scheduleTargetKind() !== "binding") chatTarget("schedule").resolveKind(editor.contextAvailable);
    editor.catalogMessage = data.model_catalog_error
      ? `${data.model_catalog_error.message} 已有配置保持不变，仍可调整其他项目。` : "";
    if (!editor.plan && !editor.settingsTouched) {
      if (JSON.stringify(editor.sessionDraft) !== JSON.stringify(data.session_settings)) invalidateSchedulePreview();
      editor.sessionBase = structuredClone(data.session_settings);
      editor.sessionDraft = structuredClone(data.session_settings);
      if (editor.discardBase && scheduleTargetKind() !== "binding") {
        editor.discardBase.settings = sessionSettingsFingerprint(data.session_settings);
      }
    }
    renderScheduleSessionSettings();
    renderScheduleTarget();
  } catch (error) {
    if (scheduleEditor !== editor || serial !== editor.optionsSerial) return;
    editor.models = [];
    editor.contextAvailable = null;
    editor.chatMessage = "";
    editor.catalogMessage = `${error.message} 可选配置暂未刷新，已有选择保持不变。`;
    editor.workspaceError = error.message;
    renderScheduleSessionSettings();
    renderScheduleTarget();
  } finally {
    if (scheduleEditor === editor && serial === editor.optionsSerial) {
      editor.workspaceLoading = false;
      renderScheduleWorkspace();
      updateScheduleSaveAvailability();
    }
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
  if (scheduleTargetKind() === "binding") return patch;
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
  scheduleInput("preview-message").classList.toggle("error", false);
  for (const node of document.querySelectorAll("[data-schedule-rule]")) {
    node.hidden = !node.dataset.scheduleRule.split(" ").includes(scheduleInput("kind").value);
  }
  scheduleInput("end-condition").disabled = scheduleInput("kind").value === "once";
  for (const [field, error] of scheduleFieldErrors) {
    for (let node = error.control; node && node !== scheduleInput("editor"); node = node.parentElement) {
      if (node.hidden) { clearScheduleFieldError(field); break; }
    }
  }
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
  if (scheduleInput("fields").disabled || !scheduleEditor) return;
  const serial = ++schedulePreviewSerial;
  schedulePrepared = null;
  scheduleInput("save").disabled = true;
  if (!validateScheduleFields()) return;
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
    if (scheduleTargetKind() === "binding") {
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
    updateScheduleSaveAvailability();
  } catch (error) {
    if (serial !== schedulePreviewSerial) return;
    if (["ambiguous_local_time", "ambiguous_end_time"].includes(error.code) && Array.isArray(error.choices)) {
      const field = error.code === "ambiguous_end_time" ? "end-offset" : "offset";
      scheduleSelectOptions(scheduleInput(field), [["", field === "end-offset" ? "选择截止时刻" : "选择触发时刻"],
        ...error.choices.map((choice, index) => [choice.utc_offset,
          `第 ${index + 1} 次 · UTC${choice.utc_offset}`])], "");
      scheduleInput(`${field}-field`).hidden = false;
      if (field === "end-offset") scheduleInput("end-details").open = true;
    }
    scheduleInput("preview-message").textContent = error.message;
    scheduleInput("preview-message").classList.toggle("error", true);
    showScheduleRequestError(error);
  }
}

function openScheduleEditor(plan = null, source = {}) {
  if (scheduleInput("fields").disabled) return;
  if (scheduleEditor && !closeScheduleEditor()) return;
  for (const field of [...scheduleFieldErrors.keys()]) clearScheduleFieldError(field);
  showScheduleFormError();
  scheduleEditorSerial += 1;
  const action = source.action || (plan ? plan.actions.update : state.schedules?.actions.create);
  if (!action) return;
  const settings = plan?.session_settings || defaultScheduleSessionSettings();
  scheduleEditor = { plan, action, sessionBase: structuredClone(settings), sessionDraft: structuredClone(settings),
    targetSession: source.targetSession || null, returnFocus: source.returnFocus || document.activeElement,
    fromSessions: Boolean(source.targetSession), models: [], contextAvailable: null,
    optionsSerial: 0, settingsTouched: false, catalogMessage: "正在读取模型目录…", chatMessage: "",
    workspace: structuredClone(plan?.target_kind === "binding" || source.targetSession ? null
      : plan ? plan.workspace : { mode: "project" }),
    workspaceOptions: null, workspaceError: null, workspaceLoading: false };
  const rule = plan?.schedule || { kind: "daily", at: "09:00", timezone: source.timezone || state.schedules?.default_timezone || "" };
  scheduleInput("editor").hidden = false;
  scheduleInput("editor-title").textContent = plan ? `编辑计划 · ${plan.name}` : "创建计划";
  scheduleInput("name").value = plan?.name || "";
  for (const input of scheduleInput("target-kind").querySelectorAll("input")) {
    input.checked = input.value === (source.targetSession ? "binding" : plan?.target_kind || "new_topic");
  }
  scheduleInput("binding").value = plan?.target_binding_id || source.targetSession?.bindingId || "";
  scheduleInput("project").value = plan?.project_alias || "";
  renderScheduleProjects();
  chatTarget("schedule", changeScheduleChat).set({ chatId: plan?.chat_id, chat: plan?.chat });
  scheduleInput("instructions").value = plan?.instructions || "";
  scheduleInput("enabled").checked = plan?.enabled ?? true;
  scheduleInput("auto-archive").checked = plan?.auto_archive ?? false;
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
  scheduleEditor.discardBase = source.draft?.discardBase || scheduleEditorValues();
  if (source.draft) restoreScheduleDraft(source.draft);
  scheduleInput("end-details").open = Boolean(scheduleInput("end-at").value);
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
  if (!validateScheduleFields({ saving: true })) return;
  const editor = scheduleEditor;
  const prepared = schedulePrepared;
  if (scheduleTargetKind() !== "binding") {
    let validationError = null;
    scheduleInput("fields").disabled = true;
    scheduleInput("close").disabled = true;
    try { await chatTarget("schedule").validate(); }
    catch (error) {
      invalidateSchedulePreview();
      validationError = error;
    } finally {
      scheduleInput("fields").disabled = false;
      scheduleInput("close").disabled = false;
    }
    if (scheduleEditor !== editor) return;
    if (validationError) { showScheduleRequestError(validationError); return; }
  }
  // A late options response may replace defaults and invalidate the preview
  // while group validation is in flight. Never submit that stale confirmation.
  if (schedulePrepared !== prepared || prepared.fingerprint !== scheduleRuleFingerprint()) {
    invalidateSchedulePreview();
    scheduleInput("preview-message").textContent = "配置已更新，请重新预览后保存。";
    scheduleInput("preview-message").classList.toggle("error", true);
    showScheduleFormError("配置已更新，请重新预览后保存。");
    return;
  }
  if (!scheduleWorkspaceCanSave()) {
    validateScheduleFields({ saving: true });
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
  if (scheduleTargetKind() === "binding") {
    if (!editor.plan) {
      definition.target_kind = "binding";
      definition.target_binding_id = scheduleInput("binding").value;
    }
  } else {
    definition.project = scheduleInput("project").value.trim();
    definition.chat_id = scheduleInput("chat").value.trim();
    definition.auto_archive = scheduleInput("auto-archive").checked;
    if (scheduleWorkspaceChanged()) {
      definition.workspace = structuredClone(editor.workspace);
      if (editor.workspaceOptions?.projectAlias === definition.project) {
        definition.expected_project_revision = editor.workspaceOptions.projectRevision;
      }
    }
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
    showScheduleRequestError(error, { submitted: true });
    setStatus(error.message, true);
    await refresh("schedules");
    setStatus(error.message, true);
  } finally {
    scheduleInput("fields").disabled = false;
    scheduleInput("close").disabled = false;
    scheduleInput("save").disabled = true;
    if (!saved) focusScheduleError();
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
    if (scheduleDetailId === plan.id) closeScheduleDetail();
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

async function loadSchedules(cursor = undefined, appliedQuery = scheduleListQuery) {
  const serial = ++scheduleListSerial;
  const page = resolveCursorPage(schedulePage, cursor, appliedQuery);
  const query = new URLSearchParams(page.query || "");
  if (page.query === null) {
    for (const [name, value] of new FormData(scheduleInput("filter"))) {
      if (String(value).trim()) query.set(name, String(value).trim());
    }
  }
  const queryIdentity = query.toString();
  if (page.cursor) query.set("cursor", page.cursor);
  const [data, projects] = await Promise.all([
    api(`/api/v1/schedules?${query}`), queryProjectOptions(),
  ]);
  if (serial !== scheduleListSerial) return false;
  state.schedules = data;
  scheduleProjects = projects;
  scheduleListCursor = page.cursor;
  scheduleListQuery = queryIdentity;
  schedulePage = { ...page, query: queryIdentity, nextCursor: data.next_cursor };
  renderScheduleProjects();
  const body = document.querySelector("#schedules-body");
  body.replaceChildren();
  for (const plan of data.plans) {
    const row = document.createElement("tr");
    cell(row, plan.name);
    const project = cell(row, plan.project_alias);
    scheduleNote(project, workspacePolicySummary(plan.workspace, { scheduled: true }));
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
    if (plan.execution.worktree_id) scheduleNote(execution, scheduleWorktreeReceipt(plan.execution, { details: false }));
    row.append(execution);
    const actions = actionsCell(row);
    const detail = actionButton("详情 / 最近记录", () => showSchedule(plan.id, detail));
    detail.setAttribute("data-schedule-detail", plan.id);
    actions.append(detail);
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
  renderCursorPagination("schedules", schedulePage, data.plans.length);
  syncPageUrl();
  return true;
}

function closeScheduleDetail() {
  const planId = scheduleDetailId;
  scheduleDetailSerial += 1;
  scheduleRunsSerial += 1;
  scheduleDetailId = null;
  scheduleRunsLoading = false;
  scheduleRunsRetryPage = null;
  const drawer = scheduleInput("detail");
  if (drawer.open) drawer.close();
  drawer.hidden = true;
  document.body.classList.toggle("schedule-detail-open", false);
  const returnFocus = scheduleDetailReturnFocus?.isConnected ? scheduleDetailReturnFocus
    : Array.from(document.querySelectorAll("[data-schedule-detail]")).find((button) => button.dataset.scheduleDetail === planId);
  returnFocus?.focus({ preventScroll: true });
  scheduleDetailReturnFocus = null;
}

function renderScheduleRunsPagination() {
  scheduleInput("runs-page").textContent = `第 ${scheduleRunsPage.number} 页`;
  scheduleInput("runs-prev").disabled = scheduleRunsLoading || !scheduleRunsPage.previousCursors.length;
  scheduleInput("runs-next").disabled = scheduleRunsLoading || !scheduleRunsPage.nextCursor;
}

async function showSchedule(planId, returnFocus = document.activeElement) {
  const serial = ++scheduleDetailSerial;
  scheduleRunsSerial += 1;
  scheduleDetailId = planId;
  const drawer = scheduleInput("detail");
  if (!drawer.open) scheduleDetailReturnFocus = returnFocus;
  drawer.hidden = false;
  if (!drawer.open) drawer.showModal();
  drawer.querySelector(".drawer-body").scrollTop = 0;
  document.body.classList.toggle("schedule-detail-open", true);
  scheduleInput("detail-title").textContent = "计划详情";
  scheduleInput("detail-content").hidden = true;
  scheduleInput("detail-note").textContent = "正在读取计划详情…";
  scheduleInput("detail-note").classList.toggle("error", false);
  scheduleInput("detail-retry").hidden = true;
  scheduleInput("runs-body").replaceChildren();
  scheduleInput("runs-note").textContent = "";
  scheduleRunsPage = { cursor: null, nextCursor: null, previousCursors: [], number: 1 };
  scheduleRunsLoading = false;
  scheduleRunsRetryPage = null;
  renderScheduleRunsPagination();
  try {
    const data = await api(`/api/v1/schedules?${new URLSearchParams({ mode: "view", plan_id: planId })}`);
    if (serial !== scheduleDetailSerial) return;
    scheduleInput("detail-title").textContent = data.plan.name;
    scheduleInput("detail-identity").textContent = `计划 ${data.plan.id} · 修订 ${data.plan.revision}`;
    scheduleInput("detail-state").textContent = `${schedulePlanState(data.plan).replace("\n", " · ")} · ${scheduleExecutionLabel(data.plan.execution)}`
      + (scheduleTriggerSource(data.plan.execution.trigger_source) ? ` · ${scheduleTriggerSource(data.plan.execution.trigger_source)}` : "")
      + (scheduleBlockedLabel(data.plan) ? ` · ${scheduleBlockedLabel(data.plan)}` : "")
      + (data.plan.execution.worktree_id ? ` · ${scheduleWorktreeReceipt(data.plan.execution)}` : "")
      + ` · 目标会话 ID：${data.plan.chat_id}`
      + (data.inflight ? "。编辑、暂停或删除不会取消本次执行。" : "");
    scheduleInput("detail-instructions").textContent = data.plan.instructions;
    scheduleInput("detail-settings").textContent = data.plan.target_kind === "binding"
      ? `${scheduleTargetLabel(data.plan)} · 沿用原会话当前配置；系统输入不新增 @ 通知。`
      : scheduleSessionSummary(data.plan.session_settings || defaultScheduleSessionSettings());
    scheduleInput("detail-settings").textContent += ` · ${data.plan.target_kind === "binding"
      ? scheduleInheritedWorkspace(data.plan) : workspacePolicySummary(data.plan.workspace, { scheduled: true })}`;
    if (data.plan.target_kind !== "binding") {
      scheduleInput("detail-settings").textContent += ` · 成功后自动归档：${data.plan.auto_archive ? "开启" : "关闭"}`;
    }
    scheduleInput("detail-rule").textContent = scheduleRuleLabel(data.plan.schedule);
    renderSchedulePreview("#schedule-detail-preview", data.preview);
    scheduleInput("detail-note").textContent = "";
    scheduleInput("detail-content").hidden = false;
    await loadScheduleRuns();
  } catch (error) {
    if (serial !== scheduleDetailSerial) return;
    scheduleInput("detail-note").textContent = error.message;
    scheduleInput("detail-note").classList.toggle("error", true);
    scheduleInput("detail-retry").hidden = false;
  }
}

async function loadScheduleRuns(page = scheduleRunsPage) {
  const planId = scheduleDetailId;
  if (!planId) return false;
  const serial = ++scheduleRunsSerial;
  const detailSerial = scheduleDetailSerial;
  const isCurrent = () => serial === scheduleRunsSerial && detailSerial === scheduleDetailSerial && planId === scheduleDetailId;
  const candidate = { ...page, previousCursors: [...page.previousCursors] };
  const query = new URLSearchParams({ mode: "runs", plan_id: planId });
  if (candidate.cursor) query.set("cursor", candidate.cursor);
  scheduleRunsLoading = true;
  scheduleRunsRetryPage = null;
  renderScheduleRunsPagination();
  scheduleInput("runs-note").textContent = "正在读取触发记录…";
  scheduleInput("runs-note").classList.toggle("error", false);
  scheduleInput("runs-retry").hidden = true;
  try {
    const data = await api(`/api/v1/schedules?${query}`);
    if (!isCurrent()) return false;
    const body = scheduleInput("runs-body");
    body.replaceChildren();
    for (const run of data.runs) {
      const row = document.createElement("tr");
      cell(row, scheduleDate(run.due_at));
      cell(row, scheduleTriggerSource(run.trigger_source));
      const result = cell(row, scheduleRunStatus(run.status || run.stage));
      if (run.worktree_id) scheduleNote(result, scheduleWorktreeReceipt(run));
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
        link.href = `/sessions/ordinary?${new URLSearchParams({ identity: run.binding_id, updatedRange: "all" })}`;
        link.textContent = "打开 Session";
        links.append(link);
      }
      body.append(row);
    }
    scheduleRunsPage = { ...candidate, nextCursor: data.next_cursor };
    scheduleInput("runs-note").textContent = data.runs.length ? "" : "暂无触发记录。";
    return true;
  } catch (error) {
    if (!isCurrent()) return false;
    scheduleInput("runs-note").textContent = `${error.message} 当前页保持不变，请重试。`;
    scheduleInput("runs-note").classList.toggle("error", true);
    scheduleRunsRetryPage = candidate;
    scheduleInput("runs-retry").hidden = false;
    return false;
  } finally {
    if (isCurrent()) {
      scheduleRunsLoading = false;
      renderScheduleRunsPagination();
    }
  }
}

async function moveScheduleRunsPage(direction) {
  if (scheduleRunsLoading) return false;
  const page = scheduleRunsPage;
  if (direction === "next" && page.nextCursor) {
    return loadScheduleRuns({ cursor: page.nextCursor, nextCursor: null,
      previousCursors: [...page.previousCursors, page.cursor], number: page.number + 1 });
  }
  if (direction === "prev" && page.previousCursors.length) {
    return loadScheduleRuns({ cursor: page.previousCursors.at(-1), nextCursor: null,
      previousCursors: page.previousCursors.slice(0, -1), number: page.number - 1 });
  }
  return false;
}

// Only durable query intent belongs in URLs. Grants, editor drafts and live
// keyset cursors stay in this document and are never restored as server facts.
const adminPagePaths = { projects: "/projects", worktrees: "/worktrees", schedules: "/schedules", defaults: "/defaults",
  updates: "/maintenance", system: "/system" };
const sessionUrlKeys = ["project", "scopeKind", "inventoryState", "current", "chatId", "topicId", "identity",
  "createdFrom", "createdBefore", "createdRange", "updatedFrom", "updatedBefore", "updatedRange", "pageSize", "sortBy", "sortDirection"];
const sideUrlKeys = ["project", "state", "chatId", "createdFrom", "createdBefore", "createdRange", "pageSize", "sortBy", "sortDirection"];
let navigationReady = false;
let navigationIndex = 0;
let navigationHref = "";
let restoringHistory = false;

function pageQuery(tab, sessionTab, raw = "") {
  const input = new URLSearchParams(raw);
  const query = new URLSearchParams();
  const allowed = tab === "sessions" ? sessionTab === "temporary" ? sideUrlKeys : sessionUrlKeys
    : tab === "schedules" ? ["project", "enabled", "ended"] : tab === "defaults" ? ["kind"] : [];
  for (const key of allowed) {
    for (const value of [...new Set(input.getAll(key).filter(Boolean))].sort()) query.append(key, value);
  }
  if (tab === "sessions") {
    const field = sessionTab === "temporary" ? "state" : "inventoryState";
    if (!query.has(field)) for (const value of sessionTab === "temporary" ? ["creating", "open"] : ["active"]) query.append(field, value);
    if (sessionTab === "ordinary") {
      if (!["active", "archived", "lazy"].includes(query.get(field))) query.set(field, "active");
      else query.set(field, query.get(field));
      if (query.get(field) === "lazy") {
        for (const key of ["createdFrom", "createdBefore", "createdRange", "updatedFrom", "updatedBefore", "updatedRange"]) query.delete(key);
        query.set("sortBy", "createdAt");
      } else if (!query.has("updatedFrom") && !query.has("updatedBefore") && query.get("updatedRange") !== "all") {
        const range = presetTimeRange("last-3-months", new Date(), true);
        query.set("updatedFrom", range.from);
        query.set("updatedRange", range.kind);
      }
    }
    for (const field of ["created", "updated"]) {
      const kind = query.get(`${field}Range`);
      const hasBounds = query.has(`${field}From`) || query.has(`${field}Before`);
      const valid = Object.hasOwn(timeRangePresetLabels, kind) && (kind === "all" ? !hasBounds : hasBounds);
      if (valid) query.set(`${field}Range`, kind);
      else query.delete(`${field}Range`);
    }
    if (!query.has("pageSize")) query.set("pageSize", "20");
    if (!query.has("sortBy")) query.set("sortBy", "updatedAt");
    if (!query.has("sortDirection")) query.set("sortDirection", "desc");
  }
  if (tab === "schedules" && !query.has("ended")) query.set("ended", "false");
  return query;
}

function pageUrl(tab, sessionTab = state.sessionTab, raw = "") {
  const path = tab === "sessions" ? `/sessions/${sessionTab}` : adminPagePaths[tab];
  const intent = new URLSearchParams(raw);
  if (tab === "sessions" && sessionTab === "ordinary" && raw && !intent.has("updatedFrom") && !intent.has("updatedBefore")) intent.set("updatedRange", "all");
  const query = pageQuery(tab, sessionTab, intent);
  if (tab === "sessions") {
    for (const [key, value] of [["pageSize", "20"], ["sortBy", "updatedAt"], ["sortDirection", "desc"]]) {
      if (query.getAll(key).length === 1 && query.get(key) === value) query.delete(key);
    }
    const key = sessionTab === "temporary" ? "state" : "inventoryState";
    const defaults = sessionTab === "temporary" ? "creating,open" : "active";
    if (query.getAll(key).sort().join(",") === defaults) query.delete(key);
  }
  if (tab === "schedules" && query.get("ended") === "false") query.delete("ended");
  if (tab === "defaults" && query.get("kind") !== "group_name") query.delete("kind");
  query.sort();
  return path + (query.size ? `?${query}` : "");
}

function currentPageUrl(tab = state.tab) {
  let query = "";
  if (tab === "sessions") {
    const kind = state.sessionTab === "temporary" ? "side" : "session";
    query = state[`${kind}Page`].query || "";
  } else if (tab === "schedules") {
    query = new URLSearchParams(scheduleListQuery || "");
    if (scheduleListQuery !== null && !query.has("ended")) query.set("ended", "all");
  }
  else if (tab === "defaults") query = `kind=${defaultsTab}`;
  return pageUrl(tab, state.sessionTab, query);
}

function syncPageUrl({ replace = false } = {}) {
  if (!navigationReady || state.authExpired) return;
  const href = currentPageUrl();
  if (replace || href !== navigationHref) {
    if (!replace) navigationIndex += 1;
    window.history[replace ? "replaceState" : "pushState"]({ netizenIndex: navigationIndex }, "", href);
    navigationHref = href;
  }
  for (const link of document.querySelectorAll(".tab")) link.href = currentPageUrl(link.dataset.tab);
  const login = document.querySelector("#auth-expired a");
  if (login) login.href = `/login?${new URLSearchParams({ next: href })}`;
}

function readPageLocation() {
  const path = window.location.pathname || "/";
  const query = new URLSearchParams(window.location.search);
  let tab = Object.keys(adminPagePaths).find((name) => adminPagePaths[name] === path) || "projects";
  const sessionTab = path === "/sessions/temporary" ? "temporary" : "ordinary";
  if (path.startsWith("/sessions/")) tab = "sessions";
  if (path === "/" && query.get("binding_id")) {
    tab = "sessions";
    query.set("identity", query.get("binding_id"));
    if (!query.has("updatedFrom") && !query.has("updatedBefore")) query.set("updatedRange", "all");
  }
  return { tab, sessionTab, query: pageQuery(tab, sessionTab, query) };
}

function restorePageFilters(route) {
  const { tab, sessionTab, query } = route;
  if (tab === "sessions") {
    const kind = sessionTab === "temporary" ? "side" : "session";
    const filters = kind === "side" ? sideMultiFilters : sessionMultiFilters;
    for (const [name, filter] of filters) filter.setValues(query.getAll(name).filter((value) => value !== "all"));
    if (kind === "side") ensureSideChatFilter(); else ensureSessionChatFilter();
    const picker = kind === "side" ? sideChatPicker : sessionChatPicker;
    const chatId = query.get("chatId");
    picker.setSelection(chatId ? { chatId, name: chatId } : null);
    document.querySelector(`#${kind}-chat-filter`).value = chatId || "";
    document.querySelector(`#${kind}-page-size`).value = query.get("pageSize");
    for (const root of document.querySelector(`#${kind}-filter`).querySelectorAll("[data-time-range]")) {
      const field = root.dataset.timeRangeField || "created";
      timeRangeControllers.get(root).setBounds(query.get(`${field}From`) || "", query.get(`${field}Before`) || "", query.get(`${field}Range`) || "custom");
    }
    if (kind === "session") {
      document.querySelector("#session-inventory-state").value = query.get("inventoryState");
      updateSessionDateFilters();
    }
    state[`${kind}Page`] = firstSessionPage(query.toString());
    const name = kind === "side" ? "sides" : "sessions";
    state[name] = null;
    state[`${kind}RefreshedAt`] = null;
    document.querySelector(`#${name}-body`).replaceChildren();
    renderSessionPagination(kind);
    renderSessionSort(kind);
  } else if (tab === "schedules") {
    for (const [name, filter] of scheduleFilters) filter.setValues(query.getAll(name).filter((value) => value !== "all"));
    const apiQuery = new URLSearchParams(query);
    for (const key of ["enabled", "ended"]) if (apiQuery.get(key) === "all") apiQuery.delete(key);
    scheduleListQuery = apiQuery.toString();
    scheduleListCursor = null;
    schedulePage = firstSessionPage(scheduleListQuery);
    state.schedules = null;
    document.querySelector("#schedules-body").replaceChildren();
    renderCursorPagination("schedules", schedulePage, 0);
  } else if (tab === "projects") {
    state.projectPage = firstSessionPage();
    state.projects = null;
    document.querySelector("#projects-body").replaceChildren();
    renderCursorPagination("projects", state.projectPage, 0);
  } else if (tab === "defaults") {
    defaultsOffset = 0;
    state.defaults = null;
    defaultsInput("chat-body").replaceChildren();
    defaultsInput("rules-body").replaceChildren();
    defaultsInput("pagination").hidden = true;
    defaultsInput("page").textContent = "第 1 页";
    // Render the sub-tab without recording a second history entry.
    const ready = navigationReady;
    navigationReady = false;
    selectDefaultTab(query.get("kind") === "group_name" ? "group_name" : "chat");
    navigationReady = ready;
  }
}

function restorePageLocation() {
  const route = readPageLocation();
  restorePageFilters(route);
  return selectTab(route.tab, route.sessionTab, { preserveEditors: true, replace: true });
}

function initializePageNavigation() {
  navigationReady = true;
  navigationIndex = window.history.state?.netizenIndex || 0;
  window.addEventListener("popstate", (event) => {
    if (restoringHistory) { restoringHistory = false; return; }
    const targetIndex = event.state?.netizenIndex;
    if (!confirmDiscardEditors()) {
      if (Number.isInteger(targetIndex) && targetIndex !== navigationIndex) {
        restoringHistory = true;
        window.history.go(navigationIndex - targetIndex);
      } else window.history.replaceState({ netizenIndex: navigationIndex }, "", navigationHref);
      return;
    }
    navigationIndex = Number.isInteger(targetIndex) ? targetIndex : 0;
    restorePageLocation();
  });
  return restorePageLocation();
}

async function refresh(tab, cursor = undefined) {
  if (state.authExpired) return false;
  if (tab === "sessions") {
    return state.sessionTab === "temporary" ? loadSides(cursor) : loadSessions(cursor);
  }
  const request = state.refreshRequest = (state.refreshRequest || 0) + 1;
  const isCurrentView = () => state.tab === tab && request === state.refreshRequest;
  setStatus("正在读取服务端事实…");
  try {
    if (tab === "projects" && await loadProjects(cursor) === false) return false;
    if (tab === "worktrees" && await window.NetizenWorktrees.load() === false) return false;
    if (tab === "updates") await loadUpdates();
    if (tab === "schedules" && await loadSchedules(cursor) === false) return false;
    if (tab === "defaults") await loadDefaults();
    if (tab === "system") {
      const results = await Promise.all([window.NetizenIdentity.loadUsers(), window.NetizenSessionAccess?.load(),
        window.NetizenProgressCard?.load()]);
      if (results.includes(false)) return false;
    }
    if (state.authExpired) return false;
    if (isCurrentView()) setStatus("已更新。");
    return true;
  } catch (error) {
    if (isCurrentView()) setStatus(error.message, true);
    return false;
  }
}

function renderSessionTabs() {
  for (const button of document.querySelectorAll("[data-session-tab]")) {
    const selected = button.dataset.sessionTab === state.sessionTab;
    button.setAttribute("aria-selected", String(selected));
    button.setAttribute("tabindex", selected ? "0" : "-1");
    document.querySelector(`#sessions-${button.dataset.sessionTab}`).hidden = !selected;
  }
  renderSessionFreshness();
}

function selectSessionTab(name) {
  if (state.authExpired || !confirmDiscardEditors()) return;
  state.sessionTab = name;
  invalidateSessionListRequest();
  sessionChatPicker?.close();
  sideChatPicker?.close();
  openFilter?.close();
  openTimeRangeFilter?.cancel({ restoreFocus: false });
  renderSessionTabs();
  syncPageUrl();
  return refresh("sessions");
}

function selectSystemTab(name) {
  if (state.authExpired || state.systemTab === name) return;
  window.NetizenSelect?.close();
  if (state.systemTab === "admins") window.NetizenIdentity?.suspendSelector();
  else if (state.systemTab === "access") window.NetizenSessionAccess?.closePickers();
  state.systemTab = name;
  for (const button of document.querySelectorAll("[data-system-tab]")) {
    const selected = button.dataset.systemTab === name;
    button.setAttribute("aria-selected", String(selected));
    button.setAttribute("tabindex", selected ? "0" : "-1");
    document.querySelector(`#system-${button.dataset.systemTab}`).hidden = !selected;
  }
}

function selectTab(name, sessionTab = state.sessionTab || "ordinary", { preserveEditors = false, replace = false } = {}) {
  if (state.authExpired) return;
  if (!preserveEditors && !confirmDiscardEditors()) return;
  window.NetizenSelect?.close();
  window.NetizenIdentity?.closePanel?.();
  closeScheduleDetail();
  state.tab = name;
  state.sessionTab = sessionTab;
  invalidateSessionListRequest();
  state.projectListRequest = (state.projectListRequest || 0) + 1;
  scheduleListSerial += 1;
  state.refreshRequest = (state.refreshRequest || 0) + 1;
  sessionChatPicker?.close();
  sideChatPicker?.close();
  openFilter?.close();
  openTimeRangeFilter?.cancel({ restoreFocus: false });
  renderSessionTabs();
  stopUpdatePolling();
  for (const item of document.querySelectorAll(".tab")) {
    const active = item.dataset.tab === name;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  for (const panel of document.querySelectorAll(".panel")) {
    const active = panel.id === name;
    panel.hidden = !active;
    panel.classList.toggle("active", active);
  }
  syncPageUrl({ replace });
  return refresh(name);
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button > 0) return;
    event.preventDefault();
    selectTab(tab.dataset.tab);
  });
}
for (const tab of document.querySelectorAll("[data-session-tab]")) {
  tab.addEventListener("click", () => selectSessionTab(tab.dataset.sessionTab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const name = event.key === "Home" ? "ordinary" : event.key === "End" ? "temporary"
      : tab.dataset.sessionTab === "ordinary" ? "temporary" : "ordinary";
    selectSessionTab(name);
    document.querySelector(`#session-tab-${name}`).focus();
  });
}

for (const tab of document.querySelectorAll("[data-system-tab]")) {
  tab.addEventListener("click", () => selectSystemTab(tab.dataset.systemTab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const names = [...document.querySelectorAll("[data-system-tab]")].map((button) => button.dataset.systemTab);
    const index = names.indexOf(tab.dataset.systemTab);
    const name = event.key === "Home" ? names[0] : event.key === "End" ? names.at(-1)
      : names[(index + (event.key === "ArrowRight" ? 1 : names.length - 1)) % names.length];
    selectSystemTab(name);
    document.querySelector(`#system-tab-${name}`).focus();
  });
}

for (const button of document.querySelectorAll("[data-refresh]")) {
  button.addEventListener("click", () => refresh(button.dataset.refresh));
}

for (const button of document.querySelectorAll("[data-session-sort]")) {
  button.addEventListener("click", () => sortSessionPage(button.dataset.sessionSort, button.dataset.sortBy));
}

projectInput("new").addEventListener("click", openProjectEditor);
projectInput("cancel").addEventListener("click", closeProjectEditor);
projectInput("close").addEventListener("click", closeProjectEditor);
projectInput("drawer").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeProjectEditor();
});
projectInput("editor").addEventListener("submit", saveProject);
projectInput("check").addEventListener("click", checkProjectResult);
for (const button of document.querySelectorAll("[data-project-mode]")) {
  button.addEventListener("click", () => selectProjectMode(button.dataset.projectMode));
}

document.querySelector("#session-inventory-state").addEventListener("change", updateSessionDateFilters);
document.querySelector("#session-filter").addEventListener("submit", (event) => {
  event.preventDefault();
  refresh("sessions", sessionFilterPage());
});
document.querySelector("#session-filter-reset").addEventListener("click", () => resetSessionFilters());
document.querySelector("#session-page-size").addEventListener("change", () => {
  refresh("sessions", sessionFilterPage());
});
document.querySelector("#side-filter").addEventListener("submit", (event) => {
  event.preventDefault(); refresh("sessions", sessionFilterPage("side"));
});
document.querySelector("#side-filter-reset").addEventListener("click", resetSideFilters);
document.querySelector("#side-page-size").addEventListener("change", () => {
  refresh("sessions", sessionFilterPage("side"));
});
document.querySelector("#projects-previous").addEventListener("click", () => moveListPage("projects", "previous"));
document.querySelector("#projects-next").addEventListener("click", () => moveListPage("projects", "next"));
window.NetizenWorktrees?.init({ api, actionPayload, setStatus, queryProjectOptions, isExpired: () => state.authExpired });
document.querySelector("#sessions-previous").addEventListener("click", () => moveSessionPage("previous"));
document.querySelector("#sessions-next").addEventListener("click", () => moveSessionPage("next"));
document.querySelector("#sides-previous").addEventListener("click", () => moveSidePage("previous"));
document.querySelector("#sides-next").addEventListener("click", () => moveSidePage("next"));
sessionSettingsInput("cancel").addEventListener("click", closeSessionSettingsEditor);
sessionSettingsInput("close").addEventListener("click", closeSessionSettingsEditor);
sessionSettingsInput("drawer").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeSessionSettingsEditor();
});
sessionSettingsInput("editor").addEventListener("submit", saveSessionSettings);
for (const field of ["model", "effort", "tier", "context", "reactions", "progress", "completion-mention",
  ...progressCardFields.map((name) => `progress-${name}`)]) {
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
defaultsInput("project").addEventListener("change", changeDefaultProject);
for (const field of ["workspace", "worktree"]) {
  defaultsInput(field).addEventListener("change", () => changeDefaultWorkspace(field));
}
defaultsInput("workspace-ref").addEventListener("input", () => changeDefaultWorkspace("workspace-ref"));
for (const field of ["model", "effort", "tier", "context", "reactions", "progress", "completion-mention",
  ...progressCardFields.map((name) => `progress-${name}`)]) {
  defaultsInput(field).addEventListener("change", () => changeDefaultSettings(field));
}
for (const [name, delta] of [["previous", -50], ["next", 50]]) {
  defaultsInput(name).addEventListener("click", async () => {
    try { await loadDefaults(Math.max(0, defaultsOffset + delta)); } catch (error) { setStatus(error.message, true); }
  });
}
scheduleInput("filter").addEventListener("submit", (event) => {
  event.preventDefault();
  const query = new URLSearchParams();
  for (const [key, value] of new FormData(scheduleInput("filter"))) if (value) query.set(key, value);
  refresh("schedules", firstSessionPage(query.toString()));
});
scheduleInput("new").addEventListener("click", () => openScheduleEditor());
scheduleInput("preview").addEventListener("click", previewSchedule);
scheduleInput("editor").addEventListener("input", invalidateSchedulePreview);
for (const eventName of ["input", "change"]) {
  scheduleInput("editor").addEventListener(eventName, (event) => {
    for (const [field, error] of scheduleFieldErrors) {
      if (error.control === event.target || error.control.contains(event.target)) clearScheduleFieldError(field);
    }
    if (scheduleEditor?.action && !scheduleFieldErrors.size) showScheduleFormError();
  });
}
scheduleInput("kind").addEventListener("change", invalidateSchedulePreview);
scheduleInput("editor").addEventListener("submit", saveSchedule);
scheduleInput("chat").addEventListener("change", loadScheduleSessionOptions);
scheduleInput("project").addEventListener("change", changeScheduleProject);
for (const field of ["workspace", "worktree"]) {
  scheduleInput(field).addEventListener("change", () => changeScheduleWorkspace(field));
}
scheduleInput("workspace-ref").addEventListener("input", () => changeScheduleWorkspace("workspace-ref"));
scheduleInput("target-kind").addEventListener("change", () => {
  for (const field of ["binding", "project", "chat", "workspace"]) clearScheduleFieldError(field);
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
document.querySelector("#schedules-previous").addEventListener("click", () => moveListPage("schedules", "previous"));
document.querySelector("#schedules-next").addEventListener("click", () => moveListPage("schedules", "next"));
scheduleInput("runs-prev").addEventListener("click", () => moveScheduleRunsPage("prev"));
scheduleInput("runs-next").addEventListener("click", () => moveScheduleRunsPage("next"));
scheduleInput("runs-retry").addEventListener("click", () => loadScheduleRuns(scheduleRunsRetryPage || scheduleRunsPage));
scheduleInput("detail-retry").addEventListener("click", () => showSchedule(scheduleDetailId));
scheduleInput("detail-close").addEventListener("click", closeScheduleDetail);
scheduleInput("detail-done").addEventListener("click", closeScheduleDetail);
scheduleInput("detail").addEventListener("cancel", (event) => { event.preventDefault(); closeScheduleDetail(); });
document.querySelector("#service-restart").addEventListener("click", restartService);
document.querySelector("#logout").addEventListener("click", async () => {
  if (!confirmDiscardEditors()) return;
  await api("/logout", { method: "POST" });
  window.location.assign("/login");
});

initializePageNavigation();
window.NetizenIdentity.loadProfile().catch((error) => setStatus(error.message, true));
