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

const timeRangeControllers = new WeakMap();

function formQuery(form, defaultPageSize = "25", refreshRelativeTime = true) {
  for (const root of form.querySelectorAll("[data-time-range]")) {
    if (refreshRelativeTime) timeRangeControllers.get(root)?.prepareQuery();
  }
  const query = new URLSearchParams();
  for (const [key, value] of new FormData(form)) {
    if (String(value)) query.set(key, String(value));
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

  const controller = { cancel: cancelDraft, prepareQuery };

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
  };
}

async function loadSessions(cursor = state.sessionPage.cursor) {
  const query = formQuery(
    document.querySelector("#session-filter"),
    "20",
    cursor === null,
  );
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

refresh("projects");
