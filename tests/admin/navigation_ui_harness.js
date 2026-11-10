const windowEvents = new Map();
window.addEventListener = (name, callback) => {
  const listeners = windowEvents.get(name) || [];
  listeners.push(callback); windowEvents.set(name, listeners);
};
let historyPosition = 0;
const historyEntries = [{ href: initialUrl, state: null }];
function locate(href) {
  const url = new URL(href, "https://admin");
  window.location.pathname = url.pathname;
  window.location.search = url.search;
}
locate(initialUrl);
window.history = {
  get state() { return historyEntries[historyPosition].state; },
  replaceState(state, _unused, href) {
    historyEntries[historyPosition] = { href, state }; locate(href);
  },
  pushState(state, _unused, href) {
    historyEntries.splice(historyPosition + 1, Infinity, { href, state });
    historyPosition += 1; locate(href);
  },
  go(delta) {
    const next = historyPosition + delta;
    if (next < 0 || next >= historyEntries.length) return;
    historyPosition = next;
    locate(historyEntries[next].href);
    for (const callback of windowEvents.get("popstate") || []) callback({ state: this.state });
  },
};
const baseFetch = fetch;
let failPage = false;
let expireResponse = false;
let projectDeletePreviewGate = null;
let selectorConfigGate = null;
let selectorConfigs = 0;
let selectorRenders = 0;
let selectorUnmounts = 0;
let selectorOnSelect = null;
let accessSaveGate = null;
let scheduleListGate = null;
let accessRules = { revision: "access-first", discovery: { mode: "all", chatIds: [] },
  readSend: { mode: "all", chatIds: [] } };
document.head = document.querySelector("head");
const appendScript = document.head.append.bind(document.head);
document.head.append = (script) => { appendScript(script); queueMicrotask(() => script.onload()); };
window.webComponent = {
  async config() { selectorConfigs += 1; },
  render(_name, options) { selectorRenders += 1; selectorOnSelect = options.onSelect; return { unmount() { selectorUnmounts += 1; } }; },
};
const adminGrant = { actionToken: "fixture-selector", csrfToken: "fixture-csrf", target: {} };
if (typeof navigationRace !== "undefined") {
  ordinary.actions.configure = adminGrant;
  Object.assign(ordinary.sessionSettings, { reaction_pulse_enabled: false, progress_card_enabled: false,
    completion_mention_enabled: false, progress_card_settings: null });
}
const scheduledPlan = () => ({
  id: "plan-one", name: "计划首页", project_alias: "netizen", enabled: true,
  chat: { chatId: "oc_selected", chatLabel: "研发群", chatLabelResolved: true },
  schedule: { kind: "daily", at: "09:00", timezone: "Asia/Shanghai" },
  lifecycle: { ended: false, has_future: true, has_trigger: true },
  execution: { kind: "none", status: "not_started", is_last: false }, actions: {},
});
fetch = async (path, options = {}) => {
  const url = new URL(path, "https://admin");
  if (url.pathname === "/api/v1/sessions/options" || (url.pathname === "/api/v1/schedules" && url.searchParams.get("mode") === "options")) {
    requests.push({ path: url.pathname, query: url.searchParams, options });
    return { ok: true, status: 200, json: async () => ({ models: [], context_mode_available: true,
      session_settings: structuredClone(ordinary.sessionSettings) }) };
  }
  if (url.pathname === "/api/v1/projects/delete-preview") {
    requests.push({ path: url.pathname, query: url.searchParams, options });
    const data = await projectDeletePreviewGate.promise;
    return { ok: true, status: 200, json: async () => data };
  }
  if (!["/api/v1/projects", "/api/v1/schedules", "/api/v1/defaults", "/logout", "/api/v1/identity",
    "/api/v1/admin-users", "/api/v1/admin-users/selector-config", "/api/v1/worktrees", "/api/v1/worktrees/settings",
    "/api/v1/question-notifications/settings", "/api/v1/group-access", "/api/v1/session-tool-access", "/api/v1/session-tool-access/save", "/api/v1/progress-card/settings"].includes(url.pathname)) return baseFetch(path, options);
  requests.push({ path: url.pathname, query: url.searchParams, options });
  if (expireResponse) return { ok: false, status: 401, json: async () => ({}) };
  if (failPage) throw new Error("本页读取失败");
  const next = url.searchParams.has("cursor");
  let data;
  if (url.pathname === "/api/v1/projects") data = {
    items: [{ alias: next ? "第二页" : "首页", cwd: "/workspace/project", actions: {} }],
    nextCursor: next ? null : "projects-next", actions: { register: adminGrant, createDirectory: adminGrant },
  };
  else if (url.pathname === "/api/v1/schedules") data = scheduleListGate ? await scheduleListGate.promise : {
    plans: [{ ...scheduledPlan(), name: next ? "计划第二页" : "计划首页" }],
    next_cursor: next ? null : "plans-next", default_timezone: "Asia/Shanghai", actions: { create: adminGrant },
  };
  else if (url.pathname === "/api/v1/defaults") data = { items: [], has_more: false, actions: {} };
  else if (url.pathname === "/api/v1/worktrees") data = { items: [], nextCursor: null };
  else if (url.pathname === "/api/v1/worktrees/settings") data = { root: "", effectiveRoot: "/instance/worktrees", editable: true, fetchBeforeCreate: false, actions: { save: adminGrant } };
  else if (url.pathname === "/api/v1/progress-card/settings") data = { editable: true, revision: "global-first",
    settings: { show_commentary: true, show_operations: true, show_plan: true, commentary_limit: 7,
      operation_limit: 12, commentary_max_chars: 280, operation_max_chars: 210, organization: "time" },
    actions: { save: adminGrant } };
  else if (url.pathname === "/api/v1/identity") data = { openId: "fixture-admin", name: "管理员" };
  else if (url.pathname === "/api/v1/admin-users") data = {
    users: [], revision: 1, addAction: adminGrant, selectorAction: adminGrant,
  };
  else if (url.pathname === "/api/v1/admin-users/selector-config") {
    if (selectorConfigGate) await selectorConfigGate.promise;
    data = { openId: "fixture-admin", appId: "fixture-app", signature: "fixture-signature" };
  }
  else if (url.pathname === "/api/v1/question-notifications/settings") data = { settings: { mentionInitiator: false }, editable: true, revision: "notify-first", actions: { save: adminGrant } };
  else if (url.pathname === "/api/v1/group-access") data = {mode: "all", chatIds: [], chats: [], revision: "groups-first", saveAction: adminGrant};
  else if (url.pathname.startsWith("/api/v1/session-tool-access")) {
    if (options.method === "POST") {
      if (accessSaveGate) await accessSaveGate.promise;
      const body = JSON.parse(options.body);
      accessRules = { revision: "access-next", discovery: body.discovery, readSend: body.readSend };
    }
    data = { ...structuredClone(accessRules), saveAction: adminGrant };
  }
  else data = {};
  return { ok: true, status: 200, json: async () => data };
};
const href = () => window.location.pathname + window.location.search;
const lastQuery = (path) => requests.findLast((request) => request.path === path).query;
// SHIPPED_ADMIN_PAGE
(async () => {
  await flush();
  assert.equal(historyEntries.length, 1, "initial canonicalization replaces, not pushes");
  assert(!window.location.search.includes("cursor="));
  assert(!window.location.search.includes("actionToken="));
  if (typeof navigationRace !== "undefined") {
    const { page, navigation, accept } = navigationRace;
    const waitFor = async (condition, message) => {
      for (let attempt = 0; attempt < 2000; attempt += 1) {
        if (condition()) return;
        await Promise.resolve();
      }
      assert.fail(message);
    };
    const navigate = (tab) => document.querySelectorAll(".tab").find((link) => link.dataset.tab === tab
      && (tab !== "sessions" || link.dataset.sessionTab === "ordinary")).click();
    const tab = page === "ordinary" ? "sessions" : "schedules";
    const path = page === "ordinary" ? "/api/v1/sessions" : "/api/v1/schedules";
    const body = document.querySelector(page === "ordinary" ? "#sessions-body" : "#schedules-body");
    const drawer = document.querySelector(page === "ordinary" ? "#session-settings-drawer" : "#schedule-drawer");
    const field = document.querySelector(page === "ordinary" ? "#session-settings-reactions" : "#schedule-name");
    const confirmation = document.querySelector("#admin-confirm");
    // Keep a real predecessor even when exercising Forward and its cancellation.
    for (let index = 0; index < (navigation === "forward" ? 2 : 1); index += 1) {
      navigate("projects"); await flush();
      navigate(tab); await flush();
    }
    if (navigation === "forward") { window.history.go(-2); await flush(); }
    const source = { href: href(), position: historyPosition, entries: structuredClone(historyEntries), rows: body.textContent };
    const gate = deferred();
    if (page === "ordinary") {
      ordinaryAnswer = () => gate.promise;
      sessionMultiFilters.get("project").setValues(["docs"]);
    } else {
      scheduleListGate = gate;
      scheduleFilters.get("project").setValues(["docs"]);
    }
    document.querySelector(page === "ordinary" ? "#session-filter" : "#schedule-filter").dispatch("submit");
    await waitFor(() => requests.some((request) => request.path === path && request.query.get("project") === "docs"), "filtered read did not start");
    if (page === "ordinary") body.querySelectorAll("button").find((button) => button.textContent === "配置").click();
    else document.querySelector("#schedule-new").click();
    await waitFor(() => drawer.open && !document.querySelector(page === "ordinary" ? "#session-settings-fields" : "#schedule-fields").disabled, "editor did not load");
    await flush();
    if (page === "ordinary") { field.checked = true; field.dispatch("change"); }
    else { field.value = "未保存的计划"; field.dispatch("input"); }
    confirmResponder = null;
    if (navigation === "link") navigate("projects");
    else window.history.go(navigation === "forward" ? 1 : -1);
    await waitFor(() => confirmation.open, "navigation must ask before discarding the draft");
    const during = { href: href(), position: historyPosition, entries: structuredClone(historyEntries) };
    gate.resolve(page === "ordinary" ? { items: [{ ...structuredClone(ordinary), nativeTitle: "晚到会话" }], nextCursor: null }
      : { plans: [{ ...scheduledPlan(), name: "晚到计划" }], next_cursor: null, actions: { create: adminGrant } });
    await flush();
    const afterResponse = { href: href(), position: historyPosition, entries: structuredClone(historyEntries) };
    ordinaryAnswer = null; scheduleListGate = null;
    document.querySelector(accept ? "#admin-confirm-submit" : "#admin-confirm-cancel").click();
    await flush();
    assert.equal(href(), accept ? "/projects" : source.href, "navigation must use the original target or source");
    assert.equal(historyPosition, accept ? (navigation === "link" ? source.position + 1 : during.position) : source.position);
    assert.equal(drawer.open, !accept, "cancellation retains the draft drawer; acceptance closes it");
    assert.deepEqual(afterResponse, during, "a late list must not write history while confirmation is pending");
    assert.equal(new Set(historyEntries.map((entry) => entry.state.netizenIndex)).size, historyEntries.length, "history indexes must remain unique");
    const expectedEntries = structuredClone(source.entries);
    if (accept && navigation === "link") expectedEntries.push({ href: "/projects", state: { netizenIndex: source.position + 1 } });
    assert.deepEqual(historyEntries, expectedEntries, "navigation must preserve the existing history entries");
    if (!accept) {
      assert.equal(page === "ordinary" ? field.checked : field.value, page === "ordinary" ? true : "未保存的计划");
      assert.equal(body.textContent, source.rows, "cancellation keeps the last applied list");
      window.history.go(-1);
      await waitFor(() => confirmation.open, "the next real Back must still be guarded");
      document.querySelector("#admin-confirm-submit").click();
      await flush();
      assert.equal(href(), "/projects");
      assert.equal(historyPosition, source.position - 1);
      assert.equal(drawer.open, false);
    }
    assert(!requests.some((request) => request.options.method === "POST"), "navigation never submits a draft");
    return;
  }
  if (authDrawer) {
    if (authDrawer === "project") {
      document.querySelector("#project-new").click();
      document.querySelector("#project-alias").value = "unsaved";
    } else {
      await selectTab("worktrees");
      document.querySelector(authDrawer === "worktree-create" ? "#worktree-new" : "#worktree-configure").click();
      await flush();
    }
    const drawer = document.querySelector(`#${authDrawer}-drawer`);
    assert.equal(drawer.open, true);
    const posts = requests.filter((request) => request.options.method === "POST").length;
    expireResponse = true;
    await refresh("projects");
    assert.equal(state.authExpired, true);
    assert.equal(drawer.open, false, "expired modal must release the page's login link");
    assert.equal(document.querySelector("#auth-expired").hidden, false);
    assert.notEqual(document.querySelector("#auth-expired").inert, true);
    assert.equal(document.querySelector("#status").hidden, true);
    assert.equal(document.querySelector("#auth-expired").hidden, false);
    assert.equal(requests.filter((request) => request.options.method === "POST").length, posts);
    return;
  }
  if (initialUrl.startsWith("/worktrees")) {
    assert.equal(state.tab, "worktrees");
    assert.equal(href(), "/worktrees");
    assert.equal(document.querySelector("#worktrees").hidden, false);
    assert.match(document.querySelector("#worktrees-body").textContent, /本页没有受管 worktree/);
    assert.equal(document.querySelector("#worktree-create-submit").disabled, true);
    assert(!lastQuery("/api/v1/worktrees").has("cursor"));
    return;
  }
  const standalone = ["/admins", "/group-access", "/session-tools", "/progress-card", "/question-notifications", "/attachments"];
  if (initialUrl === "/system" || standalone.includes(initialUrl)) {
    const expected = initialUrl === "/system" ? "admins" : initialUrl.slice(1);
    assert.equal(state.tab, expected);
    assert.equal(href(), `/${expected}`);
    assert.equal(document.querySelector(`#${expected}`).hidden, false);
    assert.equal(document.querySelector(`#nav-${expected}`).getAttribute("aria-current"), "page");
    const readCount = (path) => requests.filter((request) => request.path === path && request.options.method !== "POST").length;
    // Direct paths read only their own configuration. Attachments remain on demand.
    const pageReads = { admins: "/api/v1/admin-users", "group-access": "/api/v1/group-access",
      "session-tools": "/api/v1/session-tool-access", "progress-card": "/api/v1/progress-card/settings",
      "question-notifications": "/api/v1/question-notifications/settings" };
    for (const [page, path] of Object.entries(pageReads)) assert.equal(readCount(path), page === expected ? 1 : 0);
    if (initialUrl !== "/system") return;
    const unload = () => {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      for (const callback of windowEvents.get("beforeunload") || []) callback(event);
      return event.prevented;
    };
    const mode = document.querySelector("#session-access-discovery-mode");
    const chats = document.querySelector("#session-access-discovery-chats");
    const marker = document.querySelector("#session-access-unsaved");
    await selectTab("session-tools");
    mode.value = "only"; mode.dispatch("change"); chats.value = "oc_draft"; chats.dispatch("input");
    assert.equal(marker.hidden, false);
    assert.equal(marker.parentElement.id, "nav-session-tools");
    assert.equal(unload(), true);
    const reads = readCount(pageReads["session-tools"]);
    confirmResponder = () => { throw new Error("page navigation must retain inline drafts without a prompt"); };
    document.querySelector("#session-access-discovery-picker .chat-picker-trigger").click(); await flush();
    const picker = document.querySelector("#session-access-discovery-picker .chat-picker-popup");
    assert.equal(picker.hidden, false);
    await selectTab("projects");
    assert.equal(picker.hidden, true);
    assert.equal(state.tab, "projects");
    assert.equal(marker.hidden, false);
    window.history.go(-1); await flush();
    assert.equal(href(), "/session-tools");
    assert.equal(chats.value, "oc_draft");
    assert.equal(readCount(pageReads["session-tools"]), reads);
    window.history.go(1); await flush();
    assert.equal(href(), "/projects");
    await selectTab("session-tools");
    confirmResponder = () => false;
    assert.equal(await refresh("session-tools"), false);
    assert.equal(readCount(pageReads["session-tools"]), reads);
    document.querySelector("#logout").click(); await flush();
    assert(!requests.some((request) => request.path === "/logout"));
    assert.equal(chats.value, "oc_draft");
    // Saving after a round trip consumes the original grant, without a new read.
    accessSaveGate = deferred();
    document.querySelector("#session-access-form").dispatch("submit"); await flush();
    await selectTab("projects"); await selectTab("session-tools");
    assert.equal(document.querySelector("#session-access-save").disabled, true);
    assert.equal(readCount(pageReads["session-tools"]), reads);
    accessSaveGate.resolve(); accessSaveGate = null; await flush();
    assert.equal(marker.hidden, true);
    const saved = requests.find((request) => request.path.endsWith("/session-tool-access/save"));
    assert.deepEqual(JSON.parse(saved.options.body).discovery, { mode: "only", chatIds: ["oc_draft"] });
    assert.equal(JSON.parse(saved.options.body).actionToken, adminGrant.actionToken);

    // Every other independent configuration has the same preservation contract.
    const edits = [
      ["group-access", "group-access-mode", "only", "group-access-unsaved", "group-access-cancel"],
      ["progress-card", "global-card-progress-organization", "content", "global-card-unsaved", "global-card-cancel"],
      ["question-notifications", "question-notifications-enabled", true, "question-notifications-unsaved", "question-notifications-cancel"],
    ];
    for (const [page, fieldId, value, markerId, cancelId] of edits) {
      await selectTab(page);
      const input = document.querySelector(`#${fieldId}`);
      if (typeof value === "boolean") input.checked = value; else input.value = value;
      input.dispatch("change");
      const count = readCount(pageReads[page]);
      assert.equal(document.querySelector(`#${markerId}`).hidden, false);
      await selectTab("projects"); await selectTab(page);
      assert.equal(readCount(pageReads[page]), count);
      assert.equal(typeof value === "boolean" ? input.checked : input.value, value);
      assert.equal(unload(), true);
      await selectTab("projects");
      document.querySelector("#logout").click(); await flush();
      assert(!requests.some((request) => request.path === "/logout"));
      document.querySelector(`#${cancelId}`).click();
      assert.equal(unload(), false);
      await selectTab(page);
      assert.equal(readCount(pageReads[page]), count + 1, "clean pages load on entry");
    }
    // Explicit refresh is the only read that can replace an unsaved inline form.
    await selectTab("session-tools"); mode.value = "disabled"; mode.dispatch("change");
    confirmResponder = () => true;
    assert.equal(await refresh("session-tools"), true);
    assert.equal(mode.value, "only");
    assert.equal(marker.hidden, true);

    await selectTab("admins");
    document.querySelector("#admin-user-add").click(); await flush();
    selectorOnSelect({ type: "user", id: "ou_selected", entity: { id: "ou_selected", name: "保留的人员" } });
    const late = selectorOnSelect;
    const adminReads = readCount(pageReads.admins);
    await selectTab("attachments");
    assert.equal(selectorUnmounts, 1);
    late({ type: "user", id: "ou_late", entity: { id: "ou_late", name: "晚到人员" } });
    await selectTab("admins");
    assert.equal(readCount(pageReads.admins), adminReads);
    assert.equal(document.querySelector("#admin-user-panel").hidden, false);
    assert.equal(document.querySelector("#admin-user-confirm").disabled, false);
    assert.match(document.querySelector("#admin-user-selection").textContent, /保留的人员/);
    assert.equal(selectorRenders, 1, "selector reload stays explicit");
    const nav = document.querySelector("#admin-navigation");
    const menu = document.querySelector("#navigation-drawer");
    const trigger = document.querySelector("#navigation-open");
    trigger.focus(); trigger.click();
    assert.equal(menu.open, true);
    assert.equal(nav.parentElement.id, "navigation-drawer");
    assert.equal(document.activeElement.id, "nav-admins");
    assert.equal(trigger.getAttribute("aria-expanded"), "true");
    const escape = menu.dispatch("cancel");
    assert.equal(escape.prevented, true);
    assert.equal(menu.open, false);
    assert.equal(document.activeElement.id, "navigation-open");
    assert.equal(nav.parentElement.id, "navigation-home");
    trigger.click(); document.querySelector("#nav-attachments").click(); await flush();
    assert.equal(menu.open, false);
    assert.equal(state.tab, "attachments");
    assert.equal(document.querySelector("#current-page-name").textContent, "文件附件");
    assert.equal(nav.querySelectorAll(".tab").filter((link) => link.getAttribute("aria-current") === "page").length, 1);
    return;
  }
  if (initialUrl.startsWith("/sessions/ordinary") && !initialUrl.includes("project=")) {
    const root = document.querySelector("#session-filter").querySelectorAll("[data-time-range]")
      .find((candidate) => candidate.dataset.timeRangeField === "updated");
    const before = lastQuery("/api/v1/sessions").get("updatedFrom");
    assert(before);
    assert(!lastQuery("/api/v1/sessions").has("updatedBefore"));
    assert(!lastQuery("/api/v1/sessions").has("updatedRange"), "preset names never enter the API query");
    const presetKind = new URLSearchParams(window.location.search).get("updatedRange");
    if (!initialUrl.includes("updatedFrom=") || initialUrl.includes("updatedRange=last-3-months")) {
      assert.equal(presetKind, "last-3-months");
      assert.match(root.querySelector("[data-time-range-trigger]").title.split("\n").slice(0, 2).join("\n"),
        /^最近 3 个月（至今） · UTC[+-]\d{2}:\d{2}(?: → UTC[+-]\d{2}:\d{2})?\n\d{4}-\d{2}-\d{2} \d{2}:\d{2} ~ \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    } else {
      assert.equal(presetKind, null, "custom bounds must not be guessed to be a preset");
    }
    root.querySelector("[data-time-range-trigger]").click();
    if (presetKind) assert.equal(root.querySelectorAll("[data-time-range-preset]")
      .find((button) => button.dataset.timeRangePreset === presetKind).getAttribute("aria-pressed"), "true");
    assert.equal(root.querySelector("[data-time-range-done]").disabled, false);
    root.querySelector("[data-time-range-done]").click();
    assert.equal(root.querySelector("[data-time-range-popover]").hidden, true);
    assert.equal(root.querySelector("[data-time-range-error]").textContent, "");
    root.querySelector("[data-time-range-trigger]").click();
    const start = root.querySelector("[data-time-range-start]");
    start.value = "2026-07-02T00:00"; start.dispatch("input");
    assert.equal(root.querySelector("[data-time-range-done]").disabled, true, "edited custom dates still require a complete range");
    root.querySelector("[data-time-range-cancel]").click();
    document.querySelector("#session-filter").dispatch("submit"); await flush();
    assert.equal(lastQuery("/api/v1/sessions").get("updatedFrom"), before);
    assert(!lastQuery("/api/v1/sessions").has("updatedBefore"));
    return;
  }
  if (initialUrl.startsWith("/sessions/")) {
    const temporary = initialUrl.includes("temporary");
    assert.equal(state.tab, "sessions");
    assert.equal(state.sessionTab, temporary ? "temporary" : "ordinary");
    const query = lastQuery(temporary ? "/api/v1/side-topics" : "/api/v1/sessions");
    assert.deepEqual(query.getAll("project"), temporary ? ["docs"] : ["docs", "netizen"]);
    assert.equal(query.get("pageSize"), "50");
    assert.equal(query.get("sortDirection"), "asc");
    assert(!query.has("cursor"));
    const kind = temporary ? "side" : "session";
    const before = formQuery(document.querySelector(`#${kind}-filter`));
    if (!temporary) {
      assert.equal(query.get("sortBy"), "createdAt");
      assert.equal(before.get("chatId"), "oc_selected");
      assert.equal(before.get("createdFrom"), "2026-10-01T00:00:00Z");
      assert.equal(before.get("createdBefore"), "2026-10-02T00:00:00Z");
      if (initialUrl.includes("createdRange=today")) {
        assert.equal(before.get("createdRange"), "today");
        assert(!query.has("createdRange"));
        const created = document.querySelector("#session-filter").querySelector("[data-time-range]");
        assert.match(created.querySelector("[data-time-range-trigger]").title.split("\n").slice(0, 2).join("\n"), /^今天 · UTC[+-]\d{2}:\d{2}(?: → UTC[+-]\d{2}:\d{2})?\n.+ ~ .+$/);
      }
      assert.deepEqual(before.getAll("inventoryState"), ["archived"]);
      if (initialUrl.includes("updatedRange=all")) {
        assert(!query.has("updatedFrom"));
        assert(!query.has("updatedBefore"));
        assert(!query.has("updatedRange"));
        assert.equal(new URLSearchParams(window.location.search).get("updatedRange"), "all");
      } else {
        assert(query.get("updatedFrom"));
        assert(!query.has("updatedBefore"));
      }
    } else {
      assert.deepEqual(before.getAll("state"), ["closed"]);
      assert.equal(before.get("createdRange"), "last-7-days");
      assert.equal(query.get("createdFrom"), "2026-10-01T00:00:00Z");
      assert.equal(query.get("createdBefore"), "2026-10-08T00:00:00Z");
      assert(!query.has("createdRange"));
      assert.match(document.querySelector("#side-filter").querySelector("[data-time-range-trigger]").title.split("\n").slice(0, 2).join("\n"),
        /^最近 7 天 · UTC[+-]\d{2}:\d{2}(?: → UTC[+-]\d{2}:\d{2})?\n.+ ~ .+$/);
    }
    return;
  }
  if (initialUrl.startsWith("/schedules")) {
    assert.equal(state.tab, "schedules");
    assert.equal(lastQuery("/api/v1/schedules").get("ended"), null, "all sentinel stays outside API");
    assert.equal(lastQuery("/api/v1/schedules").get("enabled"), "false");
    assert.equal(scheduleFilters.get("project").values()[0], "netizen");
    assert.equal(new URLSearchParams(window.location.search).get("ended"), "all");
    return;
  }
  if (initialUrl.startsWith("/defaults")) {
    assert.equal(defaultsTab, "group_name");
    selectDefaultTab("chat");
    assert.equal(href(), "/defaults");
    window.history.go(-1); await flush();
    assert.equal(defaultsTab, "group_name");
    defaultsOffset = 50;
    await selectTab("projects");
    failPage = true;
    window.history.go(-1); await flush();
    assert.equal(defaultsOffset, 0);
    assert.equal(defaultsInput("page").textContent, "第 1 页");
    assert.equal(defaultsInput("previous").disabled, true);
    assert.equal(defaultsInput("new").disabled, true);
    return;
  }

  // Back cancels an open deletion confirmation, including an in-flight preview;
  // Forward must not restore the modal or submit its former delete grant.
  if (initialUrl === "/projects") {
    await selectTab("sessions", "ordinary");
    await selectTab("projects");
    const preview = { project: { alias: "首页", cwd: "/workspace/project" },
      sessionCount: 0, lazySessionCount: 0, materializedSessionCount: 0, sideCount: 0,
      worktreeCount: 0, deleteWorktrees: false,
      actions: { delete: adminGrant, previewDelete: adminGrant } };
    const dialog = document.querySelector("#project-delete-dialog");
    for (const pending of [false, true]) {
      projectDeletePreviewGate = deferred();
      const deletion = deleteProject({ alias: "首页", actions: { previewDelete: adminGrant } });
      await flush();
      assert.equal(dialog.open, true);
      if (!pending) { projectDeletePreviewGate.resolve(preview); await flush(); }
      window.history.go(-1); await flush();
      assert.equal(state.tab, "sessions");
      assert.equal(dialog.open, false, "Back must dismiss deletion confirmation");
      await deletion;
      if (pending) { projectDeletePreviewGate.resolve(preview); await flush(); }
      assert.equal(dialog.open, false, "late preview must not reopen the dialog");
      window.history.go(1); await flush();
      assert.equal(state.tab, "projects");
      assert.equal(dialog.open, false, "Forward must not restore confirmation");
      assert(!requests.some((request) => request.path === "/api/v1/projects/delete"));
    }
  }

  // Failed next-page requests retain the current rows and cursor stack.
  for (const tab of ["projects", "schedules"]) {
    await selectTab(tab);
    const page = () => tab === "projects" ? state.projectPage : schedulePage;
    const body = document.querySelector(`#${tab}-body`);
    const first = body.textContent;
    assert.equal(page().number, 1);
    failPage = true;
    await moveListPage(tab, "next");
    assert.equal(page().number, 1);
    assert.equal(body.textContent, first);
    failPage = false;
    await moveListPage(tab, "next");
    assert.equal(page().number, 2);
    assert.match(body.textContent, /第二页/, `${tab}: ${statusNode.textContent}`);
    assert(!window.location.search.includes("cursor"));
    await refresh(tab);
    assert.equal(page().number, 2, "manual refresh preserves current cursor");
    await moveListPage(tab, "previous");
    assert.equal(page().number, 1);
    assert.equal(body.textContent, first);
    assert.equal(document.querySelector(`#${tab}-previous`).disabled, true);
  }
  scheduleFilters.get("ended").clear();
  scheduleInput("filter").dispatch("submit"); await flush();
  assert.equal(href(), "/schedules?ended=all");
  scheduleFilters.get("project").setValues(["docs"]);
  await moveListPage("schedules", "next");
  assert(!lastQuery("/api/v1/schedules").has("project"), "draft is not applied by paging");
  assert.equal(href(), "/schedules?ended=all");
  await selectTab("projects");
  window.history.go(-1); await flush();
  assert.equal(href(), "/schedules?ended=all");
  assert.equal(schedulePage.number, 1, "history restores intent, not live cursor state");
  assert(!lastQuery("/api/v1/schedules").has("ended"));
  assert.deepEqual(scheduleFilters.get("project").values(), []);

  await selectTab("sessions", "ordinary");
  const emptyUrl = href();
  sessionMultiFilters.get("project").setValues(["docs"]);
  assert.equal(href(), emptyUrl, "filter drafts never enter URL");
  ordinaryAnswer = async () => { throw new Error("offline"); };
  document.querySelector("#session-filter").dispatch("submit"); await flush();
  assert.equal(href(), emptyUrl, "failed query doesn't claim successful filters");
  ordinaryAnswer = null;
  document.querySelector("#session-filter").dispatch("submit"); await flush();
  const applied = href();
  assert.equal(new URLSearchParams(window.location.search).get("project"), "docs");
  sessionMultiFilters.get("project").setValues(["netizen"]);
  await sortSessionPage("session", "createdAt");
  assert.equal(lastQuery("/api/v1/sessions").get("project"), "docs");
  assert.equal(new URLSearchParams(window.location.search).get("sortBy"), "createdAt");
  window.history.go(-1); await flush();
  assert.equal(href(), applied);
  assert.deepEqual(sessionMultiFilters.get("project").values(), ["docs"]);
  assert.equal(lastQuery("/api/v1/sessions").get("sortBy"), "updatedAt");
  assert.match(document.querySelectorAll("[data-time-range]")
    .find((root) => root.dataset.timeRangeId === "session-updated")
    .querySelector("[data-time-range-trigger]").title.split("\n").slice(0, 2).join("\n"), /^最近 3 个月（至今） · UTC[+-]\d{2}:\d{2}(?: → UTC[+-]\d{2}:\d{2})?\n.+ ~ .+$/);
  window.history.go(1); await flush();
  assert.equal(lastQuery("/api/v1/sessions").get("sortBy"), "createdAt");
  // Unlimited update bounds survive URL navigation; the marker never reaches the API.
  const updatedRange = document.querySelector("#session-filter").querySelectorAll("[data-time-range]").find((root) => root.dataset.timeRangeField === "updated");
  updatedRange.querySelector("[data-time-range-quick-clear]").click();
  document.querySelector("#session-filter").dispatch("submit"); await flush();
  const unlimitedUrl = href();
  assert.equal(new URLSearchParams(window.location.search).get("updatedRange"), "all");
  for (const key of ["updatedRange", "updatedFrom", "updatedBefore"]) assert(!lastQuery("/api/v1/sessions").has(key));
  await selectTab("projects");
  window.history.go(-1); await flush();
  assert.equal(href(), unlimitedUrl);
  assert.equal(updatedRange.querySelector("[data-time-range-summary]").textContent, "全部时间");
  assert(!lastQuery("/api/v1/sessions").has("updatedFrom"));
  await resetSessionFilters();
  assert(lastQuery("/api/v1/sessions").get("updatedFrom"));
  assert.match(updatedRange.querySelector("[data-time-range-summary]").textContent, /最近 3 个月/);

  const slow = deferred();
  ordinaryAnswer = () => slow.promise;
  const oldRefresh = refresh("sessions"); await flush();
  await selectTab("projects");
  const projectUrl = href();
  slow.resolve({ items: [], nextCursor: null }); await oldRefresh;
  ordinaryAnswer = null;
  assert.equal(href(), projectUrl, "inactive late response can't change route");

  // Back navigation and logout retain dirty data on cancellation.
  await openProjectEditor();
  projectInput("alias").value = "dirty-project";
  confirmResponder = () => false;
  const position = historyPosition;
  window.history.go(-1); await flush();
  assert.equal(historyPosition, position);
  assert.equal(href(), projectUrl);
  assert.equal(state.tab, "projects");
  assert(projectEditorHasChanges());
  document.querySelector("#logout").click(); await flush();
  assert(!requests.some((request) => request.path === "/logout"));
  confirmResponder = () => true;
  window.history.go(-1); await flush();
  assert.equal(state.tab, "sessions");
  assert.equal(projectEditor, null);
  const projectsLink = document.querySelectorAll(".tab").find((node) => node.dataset.tab === "projects");
  const beforeModifiedClick = href();
  assert.equal(projectsLink.dispatch("click", { ctrlKey: true }).prevented, false);
  assert.equal(href(), beforeModifiedClick);
  assert.equal(new URL(document.querySelector("#auth-expired a").href, "https://admin").searchParams.get("next"), href());

  await selectTab("projects");
  await moveListPage("projects", "next");
  await selectTab("sessions");
  failPage = true;
  window.history.go(-1); await flush();
  assert.equal(state.projectPage.number, 1);
  assert.equal(document.querySelector("#projects-body").textContent, "");
  assert.equal(document.querySelector("#projects-previous").disabled, true);
  failPage = false;
  await refresh("projects");
  assert.match(document.querySelector("#projects-body").textContent, /首页/);

  // Both normal navigation and History use the real Identity lifecycle hook.
  await selectTab("admins");
  document.querySelector("#admin-user-add").click(); await flush();
  const panel = document.querySelector("#admin-user-panel");
  assert.equal(panel.hidden, false);
  assert.equal(selectorRenders, 1);
  projectsLink.dispatch("click", { ctrlKey: true });
  assert.equal(panel.hidden, false, "opening another tab doesn't leave this document");
  assert.equal(selectorUnmounts, 0);
  projectsLink.click(); await flush();
  assert.equal(panel.hidden, false, "navigation preserves the selected administrator draft");
  assert.equal(selectorUnmounts, 1);

  window.history.go(-1); await flush();
  assert.equal(state.tab, "admins");
  document.querySelector("#admin-selector-retry").click(); await flush();
  window.history.go(-1); await flush();
  assert.equal(state.tab, "projects");
  assert.equal(panel.hidden, false, "navigation preserves the selected administrator draft");
  assert.equal(selectorUnmounts, 2);

  window.history.go(1); await flush();
  selectorConfigGate = deferred();
  document.querySelector("#admin-selector-retry").click(); await flush();
  assert.equal(requests.at(-1).path, "/api/v1/admin-users/selector-config");
  const beforeLateConfig = { configs: selectorConfigs, renders: selectorRenders };
  window.history.go(-1); await flush();
  assert.equal(panel.hidden, false, "navigation preserves the selected administrator draft");
  selectorConfigGate.resolve(); selectorConfigGate = null;
  await flush();
  assert.equal(state.tab, "projects");
  assert.equal(selectorConfigs, beforeLateConfig.configs, "late config must not initialize the hidden module");
  assert.equal(selectorRenders, beforeLateConfig.renders);
  assert.equal(document.querySelector("#admin-user-confirm").disabled, true);

  // Workspace drawers participate in the same navigation, logout and unload guard.
  const unload = () => {
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    for (const callback of windowEvents.get("beforeunload") || []) callback(event);
    return event.prevented;
  };
  document.querySelector("#project-new").click();
  document.querySelector("#project-alias").value = "draft";
  assert.equal(unload(), true);
  confirmResponder = () => false;
  await selectTab("worktrees");
  assert.equal(state.tab, "projects");
  assert.equal(document.querySelector("#project-drawer").open, true);
  document.querySelector("#logout").click(); await flush();
  assert(!requests.some((request) => request.path === "/logout"));
  confirmResponder = () => true;
  await selectTab("worktrees");
  assert.equal(document.querySelector("#project-drawer").open, false);
  assert.equal(unload(), false);
  document.querySelector("#worktree-configure").click(); await flush();
  document.querySelector("#worktree-root").value = "/draft/worktrees";
  assert.equal(unload(), true);
  confirmResponder = () => false;
  const worktreePosition = historyPosition;
  window.history.go(-1); await flush();
  assert.equal(historyPosition, worktreePosition);
  assert.equal(state.tab, "worktrees");
  assert.equal(document.querySelector("#worktree-settings-drawer").open, true);
  document.querySelector("#logout").click(); await flush();
  assert(!requests.some((request) => request.path === "/logout"));
  document.querySelector("#worktree-root").value = "";
  assert.equal(unload(), false, "restoring saved Worktree config clears the unload guard");
  window.history.go(-1); await flush();
  assert.equal(state.tab, "projects");
  assert.equal(document.querySelector("#worktree-settings-drawer").open, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
