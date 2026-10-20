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
let selectorConfigGate = null;
let selectorConfigs = 0;
let selectorRenders = 0;
let selectorUnmounts = 0;
let accessSaveGate = null;
let accessRules = { revision: "access-first", discovery: { mode: "all", chatIds: [] },
  readSend: { mode: "all", chatIds: [] } };
document.head = document.querySelector("head");
const appendScript = document.head.append.bind(document.head);
document.head.append = (script) => { appendScript(script); queueMicrotask(() => script.onload()); };
window.webComponent = {
  async config() { selectorConfigs += 1; },
  render() { selectorRenders += 1; return { unmount() { selectorUnmounts += 1; } }; },
};
const adminGrant = { actionToken: "fixture-selector", csrfToken: "fixture-csrf", target: {} };
const scheduledPlan = () => ({
  id: "plan-one", name: "计划首页", project_alias: "netizen", enabled: true,
  chat: { chatId: "oc_selected", chatLabel: "研发群", chatLabelResolved: true },
  schedule: { kind: "daily", at: "09:00", timezone: "Asia/Shanghai" },
  lifecycle: { ended: false, has_future: true, has_trigger: true },
  execution: { kind: "none", status: "not_started", is_last: false }, actions: {},
});
fetch = async (path, options = {}) => {
  const url = new URL(path, "https://admin");
  if (!["/api/v1/projects", "/api/v1/schedules", "/api/v1/defaults", "/logout", "/api/v1/identity",
    "/api/v1/admin-users", "/api/v1/admin-users/selector-config", "/api/v1/worktrees", "/api/v1/worktrees/settings",
    "/api/v1/session-tool-access", "/api/v1/session-tool-access/save"].includes(url.pathname)) return baseFetch(path, options);
  requests.push({ path: url.pathname, query: url.searchParams, options });
  if (expireResponse) return { ok: false, status: 401, json: async () => ({}) };
  if (failPage) throw new Error("本页读取失败");
  const next = url.searchParams.has("cursor");
  let data;
  if (url.pathname === "/api/v1/projects") data = {
    items: [{ alias: next ? "第二页" : "首页", cwd: "/workspace/project", actions: {} }],
    nextCursor: next ? null : "projects-next", actions: { register: adminGrant, createDirectory: adminGrant },
  };
  else if (url.pathname === "/api/v1/schedules") data = {
    plans: [{ ...scheduledPlan(), name: next ? "计划第二页" : "计划首页" }],
    next_cursor: next ? null : "plans-next", default_timezone: "Asia/Shanghai", actions: {},
  };
  else if (url.pathname === "/api/v1/defaults") data = { items: [], has_more: false, actions: {} };
  else if (url.pathname === "/api/v1/worktrees") data = { items: [], nextCursor: null };
  else if (url.pathname === "/api/v1/worktrees/settings") data = { root: "", effectiveRoot: "/instance/worktrees", editable: true, fetchBeforeCreate: false, actions: { save: adminGrant } };
  else if (url.pathname === "/api/v1/identity") data = { openId: "fixture-admin", name: "管理员" };
  else if (url.pathname === "/api/v1/admin-users") data = {
    users: [], revision: 1, addAction: adminGrant, selectorAction: adminGrant,
  };
  else if (url.pathname === "/api/v1/admin-users/selector-config") {
    if (selectorConfigGate) await selectorConfigGate.promise;
    data = { openId: "fixture-admin", appId: "fixture-app", signature: "fixture-signature" };
  }
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
    assert.match(document.querySelector("#status").textContent, /登录已过期/);
    assert.equal(requests.filter((request) => request.options.method === "POST").length, posts);
    return;
  }
  if (initialUrl.startsWith("/worktrees")) {
    assert.equal(state.tab, "worktrees");
    assert.equal(href(), "/worktrees");
    assert.equal(document.querySelector("#worktrees").hidden, false);
    assert.match(document.querySelector("#worktrees-body").textContent, /本页没有受管 Worktree/);
    assert.equal(document.querySelector("#worktree-create-submit").disabled, true);
    assert(!lastQuery("/api/v1/worktrees").has("cursor"));
    return;
  }
  if (initialUrl === "/system") {
    const admins = document.querySelector("#system-tab-admins");
    const access = document.querySelector("#system-tab-access");
    const badge = document.querySelector("#session-access-unsaved");
    const mode = document.querySelector("#session-access-discovery-mode");
    const chats = document.querySelector("#session-access-discovery-chats");
    const unload = () => {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      for (const callback of windowEvents.get("beforeunload") || []) callback(event);
      return event.prevented;
    };
    assert.equal(state.tab, "system");
    assert.equal(document.querySelector("#system-admins").hidden, false);
    assert.equal(document.querySelector("#system-access").hidden, true);
    assert.equal(admins.getAttribute("aria-selected"), "true");
    assert.equal(badge.hidden, true);
    const beforeSwitch = requests.length;
    admins.dispatch("keydown", { key: "End" });
    assert.equal(document.activeElement, access);
    assert.equal(access.getAttribute("tabindex"), "0");
    assert.equal(admins.getAttribute("tabindex"), "-1");
    assert.equal(document.querySelector("#system-admins").hidden, true);
    assert.equal(document.querySelector("#system-access").hidden, false);
    mode.value = "only"; mode.dispatch("change");
    chats.value = "oc_draft"; chats.dispatch("input");
    assert.equal(badge.hidden, false);
    assert.equal(unload(), true);
    window.confirm = () => { throw new Error("sub-tabs must preserve drafts without a prompt"); };
    access.dispatch("keydown", { key: "Home" });
    assert.equal(document.activeElement, admins);
    assert.equal(document.querySelector("#system-access").hidden, true);
    assert.equal(badge.hidden, false, "hidden permission edits remain discoverable");
    admins.dispatch("keydown", { key: "ArrowRight" });
    assert.equal(document.activeElement, access);
    access.dispatch("keydown", { key: "ArrowLeft" });
    access.click();
    assert.equal(chats.value, "oc_draft");
    assert.equal(requests.length, beforeSwitch, "switches never load or save");
    assert.equal(href(), "/system");

    // A picker belonging to the hidden panel must close without discarding input.
    document.querySelector("#session-access-discovery-picker .chat-picker-trigger").click();
    await flush();
    const pickerPanel = document.querySelector("#session-access-discovery-picker .chat-picker-popup");
    assert.equal(pickerPanel.hidden, false);
    admins.click();
    assert.equal(pickerPanel.hidden, true);
    assert.equal(chats.value, "oc_draft");
    window.confirm = () => false;
    const permissionReads = () => requests.filter((request) => request.path === "/api/v1/session-tool-access").length;
    const reads = permissionReads();
    assert.equal(await refresh("system"), false);
    assert.equal(permissionReads(), reads);
    await selectTab("projects");
    assert.equal(state.tab, "system", "hidden edits still guard navigation");
    document.querySelector("#logout").click(); await flush();
    assert(!requests.some((request) => request.path === "/logout"));
    assert.equal(chats.value, "oc_draft");
    access.click();
    document.querySelector("#session-access-cancel").click();
    assert.equal(badge.hidden, true);
    assert.equal(unload(), false);

    mode.value = "disabled"; mode.dispatch("change");
    accessSaveGate = deferred();
    document.querySelector("#session-access-form").dispatch("submit");
    await flush();
    const beforePendingSwitch = requests.length;
    admins.click(); access.click();
    assert.equal(document.querySelector("#session-access-save").disabled, true);
    assert.equal(requests.length, beforePendingSwitch);
    accessSaveGate.resolve(); accessSaveGate = null;
    await flush();
    assert.equal(badge.hidden, true);
    assert.equal(unload(), false);
    const saved = requests.find((request) => request.path.endsWith("/session-tool-access/save"));
    assert.deepEqual(JSON.parse(saved.options.body).discovery, { mode: "disabled", chatIds: [] });
    assert.equal(JSON.parse(saved.options.body).actionToken, adminGrant.actionToken);

    admins.click();
    document.querySelector("#admin-user-add").click(); await flush();
    const beforeSelectorSwitch = requests.length;
    access.click(); admins.click();
    assert.equal(selectorUnmounts, 1, "the body-mounted SDK selector must unmount");
    assert.equal(selectorRenders, 1);
    assert.equal(document.querySelector("#admin-user-panel").hidden, false);
    assert.equal(document.querySelector("#admin-selector-retry").hidden, false);
    assert.equal(requests.length, beforeSelectorSwitch, "selector reload stays explicit");

    access.click();
    mode.value = "all"; mode.dispatch("change");
    admins.click();
    window.confirm = () => true;
    await selectTab("projects");
    assert.equal(state.tab, "projects");
    assert.equal(badge.hidden, true);
    assert.equal(unload(), false);
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
      assert.match(root.querySelector("[data-time-range-summary]").textContent,
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
        assert.match(created.querySelector("[data-time-range-summary]").textContent, /^今天 · UTC[+-]\d{2}:\d{2}(?: → UTC[+-]\d{2}:\d{2})?\n.+ ~ .+$/);
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
      assert.match(document.querySelector("#side-filter").querySelector("[data-time-range-summary]").textContent,
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
    .querySelector("[data-time-range-summary]").textContent, /^最近 3 个月（至今） · UTC[+-]\d{2}:\d{2}(?: → UTC[+-]\d{2}:\d{2})?\n.+ ~ .+$/);
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
  pendingScheduleDraft = { dirty: true };
  window.confirm = () => false;
  const position = historyPosition;
  window.history.go(-1); await flush();
  assert.equal(historyPosition, position);
  assert.equal(href(), projectUrl);
  assert.equal(state.tab, "projects");
  assert(pendingScheduleDraft.dirty);
  document.querySelector("#logout").click(); await flush();
  assert(!requests.some((request) => request.path === "/logout"));
  window.confirm = () => true;
  window.history.go(-1); await flush();
  assert.equal(state.tab, "sessions");
  assert.equal(pendingScheduleDraft, null);
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
  await selectTab("system");
  document.querySelector("#admin-user-add").click(); await flush();
  const panel = document.querySelector("#admin-user-panel");
  assert.equal(panel.hidden, false);
  assert.equal(selectorRenders, 1);
  projectsLink.dispatch("click", { ctrlKey: true });
  assert.equal(panel.hidden, false, "opening another tab doesn't leave this document");
  assert.equal(selectorUnmounts, 0);
  projectsLink.click(); await flush();
  assert.equal(panel.hidden, true);
  assert.equal(selectorUnmounts, 1);

  window.history.go(-1); await flush();
  assert.equal(state.tab, "system");
  document.querySelector("#admin-user-add").click(); await flush();
  window.history.go(-1); await flush();
  assert.equal(state.tab, "projects");
  assert.equal(panel.hidden, true);
  assert.equal(selectorUnmounts, 2);

  window.history.go(1); await flush();
  selectorConfigGate = deferred();
  document.querySelector("#admin-user-add").click(); await flush();
  assert.equal(requests.at(-1).path, "/api/v1/admin-users/selector-config");
  const beforeLateConfig = { configs: selectorConfigs, renders: selectorRenders };
  window.history.go(-1); await flush();
  assert.equal(panel.hidden, true);
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
  window.confirm = () => false;
  await selectTab("worktrees");
  assert.equal(state.tab, "projects");
  assert.equal(document.querySelector("#project-drawer").open, true);
  document.querySelector("#logout").click(); await flush();
  assert(!requests.some((request) => request.path === "/logout"));
  window.confirm = () => true;
  await selectTab("worktrees");
  assert.equal(document.querySelector("#project-drawer").open, false);
  assert.equal(unload(), false);
  document.querySelector("#worktree-configure").click(); await flush();
  document.querySelector("#worktree-root").value = "/draft/worktrees";
  assert.equal(unload(), true);
  window.confirm = () => false;
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
