"use strict";

const state = {
  tab: "projects",
  projects: null,
  sessions: null,
  sides: null,
  projectCursor: null,
  sideCursor: null,
  sessionPage: {
    cursor: null,
    nextCursor: null,
    previousCursors: [],
    number: 1,
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
    throw new Error("登录已失效");
  }
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || `请求失败 (${response.status})`);
  return data;
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
    setStatus("操作完成，已按服务端事实刷新。");
    await refresh(state.tab);
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

function badge(value, active = false, warning = false) {
  const node = document.createElement("span");
  node.className = `badge${active ? " on" : ""}${warning ? " warn" : ""}`;
  node.textContent = value;
  return node;
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
    cell(row, project.alias);
    cell(row, project.cwd, "id");
    const status = document.createElement("td");
    status.append(badge(project.enabled ? "Enabled" : "Disabled", project.enabled));
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
    body.append(row);
  }
  document.querySelector("#projects-next").hidden = !data.nextCursor;
}

function formQuery(form, defaultPageSize = "25") {
  const query = new URLSearchParams();
  for (const [key, value] of new FormData(form)) {
    if (String(value)) query.set(key, String(value));
  }
  if (!query.has("pageSize")) query.set("pageSize", defaultPageSize);
  return query;
}

function runtimeLabel(runtime) {
  if (runtime.turn) return `Turn ${runtime.turn.state}`;
  if (runtime.goal) return `Goal ${runtime.goal.state}`;
  if (runtime.compacting) return "Compacting";
  if (runtime.lifecycle) return runtime.lifecycle.state;
  if (runtime.subscription) return runtime.subscription.state;
  return "Idle";
}

function chatModeLabel(mode, scopeKind = null) {
  const known = { p2p: "单聊", group: "群聊", topic: "话题群" }[mode];
  if (known) return known;
  if (scopeKind === "direct") return "单聊";
  if (scopeKind === "group") return "群聊";
  return "飞书会话";
}

function sessionStateLabel(value) {
  return {
    current: "当前",
    "non-current": "非当前",
    archived: "已归档",
    "lazy-current": "Lazy · 当前",
    "lazy-non-current": "Lazy · 非当前",
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
    || (session.nativeState === "lazy" ? "Lazy Session" : "未命名 Session");
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
  };
}

async function loadSessions(cursor = state.sessionPage.cursor) {
  const query = formQuery(document.querySelector("#session-filter"), "20");
  if (cursor) query.set("cursor", cursor);
  const data = await api(`/api/v1/sessions?${query}`);
  state.sessions = data;
  state.sessionPage.cursor = cursor;
  state.sessionPage.nextCursor = data.nextCursor;
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
      sessionStateLabel(session.sessionState),
      session.sessionState === "current" || session.sessionState === "lazy-current",
      session.sessionState === "missing",
    ));
    row.append(sessionState);
    const runtime = cell(row, runtimeLabel(session.runtime));
    runtime.className = "runtime-state";
    cell(row, session.projectAlias);
    const settings = session.turnSettings;
    cell(row, settings ? `${settings.modelId} / ${settings.effortId} / ${settings.serviceTierId}` : "默认");
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
  if (a.stop) actions.append(actionButton("停止", () => window.confirm("停止这个 exact 会话的当前运行？") && mutate("/api/v1/sessions/stop", a.stop), true));
  if (a.release) actions.append(actionButton("释放订阅", () => window.confirm("释放本进程订阅？历史不会删除。") && mutate("/api/v1/sessions/release", a.release)));
}

async function loadSides(cursor = null) {
  const query = formQuery(document.querySelector("#side-filter"));
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

function applyRuntimeSnapshots(payload) {
  const bindings = new Map(payload.bindings.map((item) => [item.bindingId, item]));
  for (const session of state.sessions?.items || []) {
    const runtime = bindings.get(session.bindingId);
    if (!runtime) continue;
    session.runtime = runtime;
    const row = rowByIdentity("#sessions-body", "bindingId", session.bindingId);
    const node = row?.querySelector(".runtime-state span");
    if (node) node.textContent = runtimeLabel(runtime);
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
    setStatus("已更新。");
    return true;
  } catch (error) {
    setStatus(error.message, true);
    return false;
  }
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    for (const item of document.querySelectorAll(".tab")) {
      const active = item === tab;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    }
    for (const panel of document.querySelectorAll(".panel")) {
      const active = panel.id === state.tab;
      panel.hidden = !active;
      panel.classList.toggle("active", active);
    }
    refresh(state.tab);
  });
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
document.querySelector("#session-page-size").addEventListener("change", () => {
  resetSessionPagination();
  refresh("sessions");
});
document.querySelector("#side-filter").addEventListener("submit", (event) => { event.preventDefault(); refresh("side-topics"); });
document.querySelector("#projects-next").addEventListener("click", () => refresh("projects", state.projectCursor));
document.querySelector("#sessions-previous").addEventListener("click", () => moveSessionPage("previous"));
document.querySelector("#sessions-next").addEventListener("click", () => moveSessionPage("next"));
document.querySelector("#sides-next").addEventListener("click", () => refresh("side-topics", state.sideCursor));
document.querySelector("#logout").addEventListener("click", async () => { await api("/logout", { method: "POST" }); window.location.assign("/login"); });

function chunkValues(values, size = 50) {
  const chunks = [];
  for (let index = 0; index < values.length; index += size) {
    chunks.push(values.slice(index, index + size));
  }
  return chunks;
}

async function fetchRuntimeSnapshots(bindingIds, sideIds) {
  const requests = [];
  for (const ids of chunkValues(bindingIds)) {
    const query = new URLSearchParams({ bindingIds: ids.join(",") });
    requests.push(api(`/api/v1/runtime-snapshots?${query}`));
  }
  for (const ids of chunkValues(sideIds)) {
    const query = new URLSearchParams({ sideIds: ids.join(",") });
    requests.push(api(`/api/v1/runtime-snapshots?${query}`));
  }
  const payloads = await Promise.all(requests);
  return {
    bindings: payloads.flatMap((payload) => payload.bindings),
    sides: payloads.flatMap((payload) => payload.sides),
    missingSideIds: payloads.flatMap((payload) => payload.missingSideIds),
  };
}

setInterval(async () => {
  if (document.hidden || (state.tab !== "sessions" && state.tab !== "side-topics")) return;
  const bindingIds = state.tab === "sessions"
    ? state.sessions?.items?.map((item) => item.bindingId) || []
    : [];
  const sideIds = state.tab === "side-topics"
    ? state.sides?.items?.map((item) => item.sideId) || []
    : [];
  if (!bindingIds.length && !sideIds.length) return;
  try {
    const snapshots = await fetchRuntimeSnapshots(bindingIds, sideIds);
    applyRuntimeSnapshots(snapshots);
  } catch (error) {
    setStatus(error.message, true);
  }
}, 5000);

refresh("projects");
