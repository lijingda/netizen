const state = { sessionPage: {} };
const requests = [];
let answer;
async function api(path) { requests.push(path); return answer(path); }
let refreshed;
let loadOnRefresh = false;
async function refresh(tab) {
  refreshed = tab;
  if (loadOnRefresh) {
    try { await loadSessions(); return true; } catch { return false; }
  }
}
// SHIPPED_FILTER_CONTROLLERS
(async () => {
  const form = document.querySelector("#session-filter");
  const roots = new Map(form.querySelectorAll("[data-session-multi]")
    .map((root) => [root.dataset.sessionMulti, root]));
  const control = (name, className) => roots.get(name).querySelector(`.${className}`);
  const checkbox = (name, value) => roots.get(name).querySelectorAll("input")
    .find((input) => input.type === "checkbox" && input.value === value);
  const query = () => formQuery(form, "20");
  assert.deepEqual(query().getAll("inventoryState"), ["active", "lazy"]);
  assert.equal(query().get("pageSize"), "20");
  assert(!query().has("project"));
  assert(!query().has("scopeKind"));
  assert(!query().has("current"));
  assert.equal(form.querySelectorAll("input").filter((input) =>
    ["chatId", "topicId", "identity"].includes(input.name)).length, 0);

  // All option pages are fetched without native metadata/action-grant endpoints.
  answer = (path) => new URL(path, "https://admin").searchParams.has("cursor")
    ? { items: [{ alias: "disabled-last-page", enabled: false },
      { alias: "<img src=x onerror=alert(1)>", enabled: true }], nextCursor: null }
    : { items: [{ alias: "netizen", enabled: true }], nextCursor: "second" };
  await loadSessionProjectOptions();
  assert.equal(requests.length, 2);
  assert(requests.every((path) => path.startsWith("/api/v1/projects/options?pageSize=50")));
  assert(checkbox("project", "disabled-last-page"));
  assert.equal(control("project", "multi-filter-options").querySelectorAll("img").length, 0);

  control("project", "multi-filter-trigger").click();
  const search = control("project", "multi-filter-search");
  assert.equal(document.activeElement, search);
  search.value = "LAST-PAGE";
  search.dispatch("input");
  assert.equal(checkbox("project", "netizen").parentElement.hidden, true);
  assert.equal(checkbox("project", "disabled-last-page").parentElement.hidden, false);
  checkbox("project", "disabled-last-page").click();
  search.value = "netizen";
  search.dispatch("input");
  checkbox("project", "netizen").click();
  assert.deepEqual(query().getAll("project"), ["disabled-last-page", "netizen"]);
  assert.match(control("project", "multi-filter-summary").textContent, /已停用/);
  search.value = "no such project";
  search.dispatch("input");
  assert.equal(control("project", "multi-filter-empty").hidden, false);
  assert.deepEqual(query().getAll("project"), ["disabled-last-page", "netizen"]);
  const escaped = search.dispatch("keydown", { key: "Escape" });
  assert(escaped.prevented);
  assert.equal(document.activeElement, control("project", "multi-filter-trigger"));
  assert.equal(control("project", "multi-filter-popover").hidden, true);
  assert.equal(control("project", "multi-filter-trigger").getAttribute("aria-expanded"), "false");

  // Selection is OR within each dimension and kept as separate repeated parameters.
  checkbox("scopeKind", "direct").click();
  checkbox("scopeKind", "topic").click();
  checkbox("inventoryState", "archived").click();
  checkbox("current", "false").click();
  assert.deepEqual(query().getAll("scopeKind"), ["direct", "topic"]);
  assert.deepEqual(query().getAll("inventoryState"), ["active", "lazy", "archived"]);
  assert.deepEqual(query().getAll("current"), ["false"]);
  control("inventoryState", "multi-filter-clear").click();
  assert.deepEqual(query().getAll("inventoryState"), ["all"]);
  assert.equal(control("inventoryState", "multi-filter-summary").textContent, "全部");
  control("project", "multi-filter-clear").click();
  assert(!query().has("project"));

  const scopeTrigger = control("scopeKind", "multi-filter-trigger");
  scopeTrigger.dispatch("keydown", { key: "ArrowDown" });
  assert.equal(document.activeElement, checkbox("scopeKind", "direct"));
  checkbox("scopeKind", "direct").dispatch("keydown", { key: "ArrowDown" });
  assert.equal(document.activeElement, checkbox("scopeKind", "group"));
  control("current", "multi-filter-trigger").click();
  assert.equal(control("scopeKind", "multi-filter-popover").hidden, true);
  document.body.dispatch("pointerdown");
  assert.equal(control("current", "multi-filter-popover").hidden, true);
  scopeTrigger.click();
  document.querySelector("#session-filter-reset").focus();
  assert.equal(control("scopeKind", "multi-filter-popover").hidden, true);

  // Use the actual shared time controller, including its reset of applied UTC fields.
  const rangeRoot = form.querySelector("[data-time-range]");
  rangeRoot.querySelector("[data-time-range-trigger]").click();
  rangeRoot.querySelectorAll("[data-time-range-preset]")
    .find((button) => button.dataset.timeRangePreset === "today").click();
  rangeRoot.querySelector("[data-time-range-done]").click();
  assert(query().get("createdFrom"));
  assert(query().get("createdBefore"));
  document.querySelector("#session-page-size").value = "100";
  state.sessionPage = { cursor: "later", nextCursor: "next", previousCursors: [null], number: 2 };
  await resetSessionFilters();
  assert.equal(refreshed, "sessions");
  assert.deepEqual(query().getAll("inventoryState"), ["active", "lazy"]);
  assert.equal(query().get("pageSize"), "20");
  for (const key of ["project", "scopeKind", "current", "createdFrom", "createdBefore"]) {
    assert(!query().has(key), `${key} should be reset`);
  }
  assert.deepEqual(state.sessionPage, { cursor: null, nextCursor: null, previousCursors: [], number: 1, query: null });
  assert.equal(rangeRoot.querySelector("[data-time-range-summary]").textContent, "全部时间");

  // A deleted option stays removable, and options refresh never silently changes a selection.
  checkbox("project", "netizen").click();
  answer = () => ({ items: [], nextCursor: null });
  await loadSessionProjectOptions();
  assert.deepEqual(query().getAll("project"), ["netizen"]);
  assert.match(control("project", "multi-filter-summary").textContent, /已不可用/);
  const side = document.querySelector("#side-filter");
  side.querySelectorAll("input").find((input) => input.name === "project").value = "side-project";
  assert.equal(formQuery(side).get("project"), "side-project");
  assert(!formQuery(side).has("inventoryState"));

  // Pagination reuses the applied repeated values and absolute UTC bounds.
  // Checkbox/time changes remain drafts until explicit 筛选/reset/page-size submission.
  await resetSessionFilters();
  const sessionQueries = [];
  let failNextPage = false;
  answer = (path) => {
    if (path.startsWith("/api/v1/projects/options?")) {
      return { items: [{ alias: "netizen", enabled: true }], nextCursor: null };
    }
    const params = new URL(path, "https://admin").searchParams;
    sessionQueries.push(params);
    if (failNextPage && params.has("cursor")) throw new Error("catalog unavailable");
    return { items: [], nextCursor: params.has("cursor") ? null : "page-two" };
  };
  const applyTimePreset = (preset) => {
    rangeRoot.querySelector("[data-time-range-trigger]").click();
    rangeRoot.querySelectorAll("[data-time-range-preset]")
      .find((button) => button.dataset.timeRangePreset === preset).click();
    rangeRoot.querySelector("[data-time-range-done]").click();
  };
  applyTimePreset("today");
  await loadSessions();
  const applied = state.sessionPage.query;
  const bounds = sessionQueries.at(-1).get("createdFrom");
  checkbox("inventoryState", "active").click();
  checkbox("inventoryState", "archived").click();
  checkbox("project", "netizen").click();
  checkbox("scopeKind", "topic").click();
  applyTimePreset("yesterday");
  assert.deepEqual(query().getAll("inventoryState"), ["lazy", "archived"]);
  assert.notEqual(query().get("createdFrom"), bounds);
  loadOnRefresh = true;
  await moveSessionPage("next");
  assert.equal(sessionQueries.at(-1).get("cursor"), "page-two");
  assert.deepEqual(sessionQueries.at(-1).getAll("inventoryState"), ["active", "lazy"]);
  assert.equal(sessionQueries.at(-1).get("createdFrom"), bounds);
  assert(!sessionQueries.at(-1).has("project"));
  assert(!sessionQueries.at(-1).has("scopeKind"));
  assert.equal(state.sessionPage.query, applied);
  await moveSessionPage("previous");
  assert(!sessionQueries.at(-1).has("cursor"));
  assert.equal(sessionQueries.at(-1).toString(), applied);
  await refresh("sessions");
  assert.equal(sessionQueries.at(-1).toString(), applied);
  failNextPage = true;
  await moveSessionPage("next");
  assert.equal(state.sessionPage.cursor, null);
  assert.equal(state.sessionPage.query, applied);
  assert.equal(state.sessionPage.number, 1);
  failNextPage = false;
  resetSessionPagination();
  await loadSessions();
  assert.deepEqual(sessionQueries.at(-1).getAll("inventoryState"), ["lazy", "archived"]);
  assert.deepEqual(sessionQueries.at(-1).getAll("project"), ["netizen"]);
  assert.deepEqual(sessionQueries.at(-1).getAll("scopeKind"), ["topic"]);
  assert.notEqual(sessionQueries.at(-1).get("createdFrom"), bounds);
})().catch((error) => { console.error(error); process.exitCode = 1; });
