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
let selectorConfigGate = null;
let selectorConfigs = 0;
let selectorRenders = 0;
let selectorUnmounts = 0;
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
    "/api/v1/admin-users", "/api/v1/admin-users/selector-config"].includes(url.pathname)) return baseFetch(path, options);
  requests.push({ path: url.pathname, query: url.searchParams, options });
  if (failPage) throw new Error("本页读取失败");
  const next = url.searchParams.has("cursor");
  let data;
  if (url.pathname === "/api/v1/projects") data = {
    items: [{ alias: next ? "第二页" : "首页", cwd: "/workspace/project", actions: {} }],
    nextCursor: next ? null : "projects-next",
  };
  else if (url.pathname === "/api/v1/schedules") data = {
    plans: [{ ...scheduledPlan(), name: next ? "计划第二页" : "计划首页" }],
    next_cursor: next ? null : "plans-next", default_timezone: "Asia/Shanghai", actions: {},
  };
  else if (url.pathname === "/api/v1/defaults") data = { items: [], has_more: false, actions: {} };
  else if (url.pathname === "/api/v1/identity") data = { openId: "fixture-admin", name: "管理员" };
  else if (url.pathname === "/api/v1/admin-users") data = {
    users: [], revision: 1, addAction: adminGrant, selectorAction: adminGrant,
  };
  else if (url.pathname === "/api/v1/admin-users/selector-config") {
    if (selectorConfigGate) await selectorConfigGate.promise;
    data = { openId: "fixture-admin", appId: "fixture-app", signature: "fixture-signature" };
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
      assert.deepEqual(before.getAll("inventoryState"), ["all"]);
    } else assert.deepEqual(before.getAll("state"), ["closed"]);
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
  window.history.go(1); await flush();
  assert.equal(lastQuery("/api/v1/sessions").get("sortBy"), "createdAt");
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
})().catch((error) => { console.error(error); process.exitCode = 1; });
