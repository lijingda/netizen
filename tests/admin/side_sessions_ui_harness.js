window.location = { search: initialSearch, assign() {} };
window.confirm = () => true;
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, expire() {} };
const sessionStorage = { getItem() {}, setItem() {}, removeItem() {} };
let now = 0;
let nextTimer = 0;
const timers = new Map();
function setTimeout(callback, delay = 0) { const id = ++nextTimer; timers.set(id, { callback, at: now + delay }); return id; }
function clearTimeout(id) { timers.delete(id); }
function setInterval(callback, delay) { const id = setTimeout(callback, delay); timers.get(id).interval = delay; return id; }
function clearInterval(id) { timers.delete(id); }
const performance = { now: () => now };
Date.now = () => Date.UTC(2026, 9, 12, 8) + now;
const requests = [];
const confirmations = [];
window.confirm = (message) => { confirmations.push(message); return true; };
const ordinary = {
  bindingId: "binding-parent", shortId: "parent", chatId: "oc_parent", chatLabel: "研发群",
  chatLabelResolved: true, chatMode: "group", chatOpenUrl: "https://applink.feishu.cn/chat",
  nativeTitle: "普通会话标题", sessionType: "message", catalogState: "active", pointerState: "current",
  createdAt: "2026-10-10T08:00:00+00:00", updatedAt: Date.UTC(2026, 9, 12, 7) / 1000,
  projectAlias: "netizen", sessionSettings: { message_context_mode: "current-only", turn_settings: null },
  runtime: { bindingId: "binding-parent", primaryStatus: "idle", primaryStatusResolution: "local" }, actions: {},
};
if (initialSearch.includes("inventoryState=all")) ordinary.catalogState = "archived";
const grant = (action) => ({ actionToken: action, csrfToken: "csrf", target: { resource: "side", sideId: "side-a" } });
const side = {
  sideId: "side-a", parentBindingId: "binding-parent", parentSession: { bindingId: "binding-parent", title: "普通会话标题", available: true },
  chatId: "oc_side", chatLabel: "研发群", chatLabelResolved: true, chatMode: "group", topicId: "omt_side",
  rootMessageId: "om_root", topicOpenUrl: "https://applink.feishu.cn/topic", chatOpenUrl: "https://applink.feishu.cn/chat",
  projectAlias: "netizen", state: "open", sessionState: "open", createdAt: "2026-10-12T08:00:00+00:00",
  updatedAt: "2026-10-12T09:00:00+00:00",
  runtime: { sideId: "side-a", revision: 1, state: "open", sessionState: "open", turnId: "turn-a", turnState: "running" },
  actions: { stop: grant("stop"), close: grant("close") },
};
let listItems = [side];
let sideAnswer;
let ordinaryAnswer;
let failSide = false;
let actionFailure = false;
async function fetch(path, options = {}) {
  const url = new URL(path, "https://admin");
  requests.push({ path: url.pathname, query: url.searchParams, options });
  let data;
  if (url.pathname === "/api/v1/projects/options") data = { items: [{ alias: "netizen", enabled: true }, { alias: "docs", enabled: false }], nextCursor: null };
  else if (url.pathname === "/api/v1/chats") data = { items: [{ chatId: "oc_selected", name: "已选群" }], nextCursor: null };
  else if (url.pathname === "/api/v1/projects") data = { items: [], nextCursor: null };
  else if (url.pathname === "/api/v1/sessions") data = ordinaryAnswer ? await ordinaryAnswer() : { items: [structuredClone(ordinary)], nextCursor: "ordinary-next" };
  else if (url.pathname === "/api/v1/side-topics") {
    if (failSide) throw new Error("side list unavailable");
    data = sideAnswer ? await sideAnswer() : { items: structuredClone(listItems), nextCursor: url.searchParams.has("cursor") ? null : "side-next" };
  } else if (url.pathname.endsWith("/stop") || url.pathname.endsWith("/close")) {
    if (actionFailure) return { ok: false, status: 409, json: async () => ({ message: "会话状态已变化，请重试" }) };
    data = { message: "操作完成" };
  }
  else throw new Error(`Unexpected request ${path}`);
  return { ok: true, status: 200, json: async () => data };
}
for (const id of ["sessions-body", "sides-body"]) {
  Object.defineProperty(document.querySelector(`#${id}`), "rows", { get() { return this.children; } });
}
async function flush() { for (let i = 0; i < 80; i += 1) await Promise.resolve(); }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
async function advanceTime(duration) {
  const end = now + duration;
  let ticks = 0;
  while (true) {
    const next = [...timers].filter(([, timer]) => timer.at <= end).sort((left, right) => left[1].at - right[1].at)[0];
    if (!next) break;
    assert(++ticks < 1000, "timer loop did not settle");
    const [id, timer] = next;
    now = timer.at;
    if (timer.interval) timer.at += timer.interval;
    else timers.delete(id);
    await timer.callback();
    await flush();
  }
  now = end;
  await flush();
}
// SHIPPED_ADMIN_PAGE
(async () => {
  await flush();
  if (initialSearch) {
    assert.equal(state.tab, "sessions");
    assert.equal(state.sessionTab, "ordinary");
    const query = requests.findLast((request) => request.path === "/api/v1/sessions").query;
    assert.equal(query.get("identity"), "binding-parent");
    assert.deepEqual(query.getAll("inventoryState"), initialSearch.includes("inventoryState=all") ? ["all"] : ["active", "lazy", "unknown"]);
    assert.equal(document.querySelector("#sessions-ordinary").hidden, false);
    assert(document.querySelector("#sessions-body").textContent.includes("普通会话标题"));
    if (initialSearch.includes("inventoryState=all")) {
      assert(document.querySelector("#sessions-body").textContent.includes("已归档"));
      assert.deepEqual(formQuery(document.querySelector("#session-filter")).getAll("inventoryState"), ["all"]);
    }
    return;
  }
  assert.equal(state.tab, "projects");
  assert.equal(state.sessionTab, "ordinary");
  assert(!document.querySelectorAll(".tab").some((tab) => tab.dataset.tab === "side-topics"));
  selectTab("sessions");
  await flush();
  const ordinaryQuery = requests.findLast((request) => request.path === "/api/v1/sessions").query;
  assert.deepEqual(ordinaryQuery.getAll("inventoryState"), ["active", "lazy", "unknown"]);
  assert.equal(ordinaryQuery.get("pageSize"), "20");
  const ordinaryText = document.querySelector("#sessions-body").textContent;
  const ordinaryPage = state.sessionPage;
  await selectSessionTab("temporary");
  assert.equal(document.querySelector("#sessions-ordinary").hidden, true);
  assert.equal(document.querySelector("#sessions-temporary").hidden, false);
  const sideQuery = () => requests.findLast((request) => request.path === "/api/v1/side-topics").query;
  assert.deepEqual(sideQuery().getAll("state"), ["creating", "open"]);
  assert.equal(sideQuery().get("pageSize"), "20");
  for (const key of ["inventoryState", "scopeKind", "current", "parentBindingId", "topicId", "rootMessageId"]) assert(!sideQuery().has(key));
  const row = () => document.querySelector("#sides-body").children[0];
  assert.equal(row().children.length, 8);
  assert.equal(row().children[0].querySelector("a").href, side.topicOpenUrl);
  assert(!row().children[0].textContent.includes(side.chatId));
  assert.equal(row().children[1].children[0].textContent, "普通会话标题");
  assert(row().querySelector("details").textContent.includes("Side side-a"));
  assert.equal(row().querySelector(".runtime-state").textContent, "运行中");
  const parentLink = row().querySelector(".actions").querySelector("a");
  const parentUrl = new URL(parentLink.href, "http://localhost");
  assert.equal(parentUrl.pathname, "/sessions/ordinary");
  assert.equal(parentUrl.searchParams.get("identity"), "binding-parent");
  assert.equal(parentUrl.searchParams.get("inventoryState"), "all");
  assert.equal(parentLink.target, "_blank");
  const buttons = () => row().querySelectorAll("button");
  assert.deepEqual(buttons().map((button) => button.textContent), ["停止", "结束临时会话"]);
  buttons()[0].click();
  await flush();
  assert.equal(JSON.parse(requests.findLast((request) => request.path.endsWith("/stop")).options.body).actionToken, "stop");
  buttons()[1].click();
  await flush();
  assert(confirmationIncludes("之后仍可继续交流"));
  assert(confirmationIncludes("结束后无法恢复"));

  const form = document.querySelector("#side-filter");
  const sidePickerRoot = document.querySelector("#side-chat-picker");
  assert.equal(sidePickerRoot.querySelector(".chat-picker-summary").textContent, "全部");
  const projectRoot = form.querySelectorAll("[data-side-multi]").find((root) => root.dataset.sideMulti === "project");
  projectRoot.querySelectorAll("input").find((input) => input.value === "docs").click();
  sideChatPicker.setSelection({ chatId: "oc_selected", name: "已选群" });
  document.querySelector("#side-chat-filter").value = "oc_selected";
  document.querySelector("#side-chat-picker .chat-picker-trigger").click();
  await flush();
  assert(sidePickerRoot.querySelector(".chat-picker-empty-option"));
  const sideSearch = document.querySelector("#side-group-search");
  sideSearch.value = "其他群";
  sideSearch.dispatch("input");
  sideSearch.dispatch("keydown", { key: "Escape" });
  assert.equal(document.querySelector("#side-chat-filter").value, "oc_selected");
  const range = form.querySelector("[data-time-range]");
  range.querySelector("[data-time-range-trigger]").click();
  range.querySelectorAll("[data-time-range-preset]").find((button) => button.dataset.timeRangePreset === "today").click();
  range.querySelector("[data-time-range-done]").click();
  form.dispatch("submit"); await flush();
  assert.deepEqual(sideQuery().getAll("project"), ["docs"]);
  assert.equal(sideQuery().get("chatId"), "oc_selected");
  assert(sideQuery().get("createdFrom"));
  const applied = state.sidePage.query;
  projectRoot.querySelectorAll("input").find((input) => input.value === "netizen").click();
  await moveSidePage("next");
  assert.equal(state.sidePage.number, 2);
  assert.equal(sideQuery().get("cursor"), "side-next");
  assert.deepEqual(sideQuery().getAll("project"), ["docs"], "paging retains applied filters");
  await selectSessionTab("ordinary");
  assert.deepEqual(state.sessionPage, ordinaryPage);
  assert.equal(document.querySelector("#sessions-body").textContent, ordinaryText);
  await selectSessionTab("temporary");
  assert.equal(state.sidePage.query, applied);
  assert.equal(state.sidePage.number, 2);
  await moveSidePage("previous");
  assert.equal(state.sidePage.number, 1);
  failSide = true;
  await moveSidePage("next");
  assert.equal(state.sidePage.number, 1, "failed page requests retain the displayed cursor stack");
  failSide = false;
  await resetSideFilters();
  assert.equal(sidePickerRoot.querySelector(".chat-picker-summary").textContent, "全部");
  assert.equal(sideChatPicker.getSelection(), null);
  assert.equal(sideQuery().get("pageSize"), "20");
  assert.deepEqual(sideQuery().getAll("state"), ["creating", "open"]);
  for (const key of ["project", "chatId", "createdFrom", "createdBefore", "cursor"]) assert(!sideQuery().has(key));
  document.querySelector("#side-page-size").value = "100";
  document.querySelector("#side-page-size").dispatch("change"); await flush();
  assert.equal(sideQuery().get("pageSize"), "100");

  // A pending inactive-tab response cannot overwrite its saved page or current status.
  const slow = deferred();
  sideAnswer = () => slow.promise;
  const savedSides = state.sides;
  const oldRefresh = refresh("sessions"); await flush();
  await selectSessionTab("ordinary");
  const statusBefore = statusNode.textContent;
  slow.resolve({ items: [], nextCursor: null }); await oldRefresh;
  assert.equal(state.sides, savedSides);
  assert.equal(statusNode.textContent, statusBefore);
  sideAnswer = null;

  await selectSessionTab("temporary");
  listItems = [{ ...side, parentSession: { bindingId: side.parentBindingId, available: false, title: null },
    projectAlias: null, runtime: null, sessionState: "closed", state: "closed", actions: {}, chatLabelResolved: false }];
  await refresh("sessions");
  assert.equal(row().children[1].children[0].textContent, "原会话已删除");
  assert.equal(row().querySelector(".runtime-state").textContent, "—");
  assert.equal(row().children[4].textContent, "原项目已不可用");
  assert.equal(buttons().length, 0);
  assert(!row().querySelector(".actions").querySelector("a"));
  const beforeIdle = requests.length;
  await advanceTime(60000);
  assert.equal(requests.length, beforeIdle);

  // Explicit schedule selection forces ordinary even after temporary was selected.
  selectTab("sessions", "ordinary"); await flush();
  assert.equal(state.sessionTab, "ordinary");
  assert.equal(document.querySelector("#sessions-ordinary").hidden, false);
  await checkAtomicSessionLists();
  await checkSessionSorting();
  function confirmationIncludes(text) { return confirmations.some((message) => message.includes(text)); }
})().catch((error) => { console.error(error); process.exitCode = 1; });

async function checkAtomicSessionLists() {
  for (const [kind, view, dataKey] of [["session", "ordinary", "sessions"], ["side", "temporary", "sides"]]) {
    const pageKey = `${kind}Page`;
    const form = document.querySelector(`#${kind}-filter`);
    const size = document.querySelector(`#${kind}-page-size`);
    const chat = document.querySelector(`#${kind}-chat-filter`);
    const body = document.querySelector(`#${dataKey}-body`);
    const pagination = document.querySelector(`#${dataKey}-page`);
    const reply = (name, nextCursor = "page-two") => ({
      items: [kind === "side" ? { ...structuredClone(side), parentSession: { ...side.parentSession, title: name } }
        : { ...structuredClone(ordinary), nativeTitle: name }], nextCursor,
    });
    const answerWith = (answer) => { if (kind === "side") sideAnswer = answer; else ordinaryAnswer = answer; };
    const snapshot = () => ({ page: structuredClone(state[pageKey]), data: structuredClone(state[dataKey]),
      body: body.textContent, pagination: pagination.textContent,
      refreshedAt: state[`${kind}RefreshedAt`],
      freshness: document.querySelector("#sessions-refreshed-at").textContent });
    answerWith(async () => reply("original"));
    await selectSessionTab(view);
    await resetSessionFilters(kind);
    await moveSessionPage("next", kind);
    const saved = snapshot();

    // Failed filter/page-size requests retain data + applied query + pagination,
    // including edits made to the form after the request was submitted.
    chat.value = "oc_submitted";
    const filter = deferred(); answerWith(() => filter.promise);
    const applying = refresh("sessions", sessionFilterPage(kind)); await flush();
    assert.deepEqual(snapshot(), saved);
    chat.value = "oc_edited_during_request";
    filter.reject(new Error("filter unavailable")); await applying;
    assert.deepEqual(snapshot(), saved);
    assert.equal(chat.value, "oc_edited_during_request");
    assert.match(statusNode.textContent, /上次成功/);
    size.value = "100";
    answerWith(async () => { throw new Error("page size unavailable"); });
    size.dispatch("change"); await flush();
    assert.deepEqual(snapshot(), saved);
    assert.equal(size.value, "100");

    // A failed reset also keeps the result, while resetting the draft controls.
    await resetSessionFilters(kind);
    assert.deepEqual(snapshot(), saved);
    assert.equal(size.value, "20");
    assert.equal(chat.value, "");

    // Successful submission applies its captured query, never later draft edits.
    const success = deferred(); answerWith(() => success.promise);
    chat.value = "oc_submitted";
    const submitting = refresh("sessions", sessionFilterPage(kind)); await flush();
    chat.value = "oc_later_draft";
    success.resolve(reply("new query")); await submitting;
    assert.equal(state[pageKey].number, 1);
    assert.deepEqual(state[pageKey].previousCursors, []);
    assert.equal(new URLSearchParams(state[pageKey].query).get("chatId"), "oc_submitted");
    assert.equal(chat.value, "oc_later_draft");

    // An older paging failure cannot roll back a newer successful paging result.
    const old = deferred(); answerWith(() => old.promise);
    const oldMove = moveSessionPage("next", kind); await flush();
    assert.equal(state[pageKey].number, 1);
    answerWith(async () => reply("new page", null));
    await moveSessionPage("next", kind);
    const newPage = snapshot();
    const newStatus = statusNode.textContent;
    assert.equal(state[pageKey].number, 2);
    old.reject(new Error("old page failed")); await oldMove;
    assert.deepEqual(snapshot(), newPage);
    assert.equal(statusNode.textContent, newStatus);

    // Once a newer request fails, an older success is still obsolete.
    const older = deferred(); answerWith(() => older.promise);
    const olderRefresh = refresh("sessions"); await flush();
    answerWith(async () => { throw new Error("latest failed"); });
    assert.equal(await refresh("sessions"), false);
    const failedStatus = statusNode.textContent;
    older.resolve(reply("obsolete success")); await olderRefresh;
    assert.deepEqual(snapshot(), newPage);
    assert.equal(statusNode.textContent, failedStatus);
    answerWith(null);
  }
}

async function checkSessionSorting() {
  const freshness = document.querySelector("#sessions-refreshed-at");
  for (const [kind, view, dataKey] of [["session", "ordinary", "sessions"], ["side", "temporary", "sides"]]) {
    const pageKey = `${kind}Page`;
    const form = document.querySelector(`#${kind}-filter`);
    const chat = document.querySelector(`#${kind}-chat-filter`);
    const buttons = document.querySelectorAll("[data-session-sort]").filter((button) => button.dataset.sessionSort === kind);
    const created = buttons.find((button) => button.dataset.sortBy === "createdAt");
    const updated = buttons.find((button) => button.dataset.sortBy === "updatedAt");
    const path = kind === "side" ? "/api/v1/side-topics" : "/api/v1/sessions";
    const query = () => requests.findLast((request) => request.path === path).query;
    const answerWith = (answer) => { if (kind === "side") sideAnswer = answer; else ordinaryAnswer = answer; };
    const row = () => document.querySelector(`#${dataKey}-body`).children[0];
    listItems = [structuredClone(side)];
    await selectSessionTab(view);
    await resetSessionFilters(kind);
    assert.equal(query().get("sortBy"), "updatedAt");
    assert.equal(query().get("sortDirection"), "desc");
    assert.equal(updated.parentElement.getAttribute("aria-sort"), "descending");
    assert.match(updated.getAttribute("aria-label"), /最近更新由远及近/);
    assert.equal(created.parentElement.getAttribute("aria-sort"), "none");
    assert.equal(freshness.hidden, false);
    assert.match(freshness.textContent, /^更新于 \d{2}:\d{2}:\d{2}$/);

    const createdCell = row().children[kind === "side" ? 5 : 7];
    const updatedCell = row().children[kind === "side" ? 6 : 8];
    assert.equal(createdCell.textContent, sessionDateLabel(kind === "side" ? side.createdAt : ordinary.createdAt));
    assert.equal(updatedCell.textContent, kind === "side" ? sessionDateLabel(side.updatedAt) : sessionUpdatedAtLabel(ordinary));

    chat.value = "oc_applied";
    form.dispatch("submit"); await flush();
    await moveSessionPage("next", kind);
    assert.equal(state[pageKey].number, 2);
    chat.value = "oc_unsubmitted";
    await advanceTime(1000);
    updated.click(); await flush();
    assert.equal(query().get("sortBy"), "updatedAt");
    assert.equal(query().get("sortDirection"), "asc");
    assert.equal(query().get("chatId"), "oc_applied", "sort must preserve applied filters, not submit drafts");
    assert.equal(chat.value, "oc_unsubmitted");
    assert(!query().has("cursor"));
    assert.equal(state[pageKey].number, 1);
    assert.deepEqual(state[pageKey].previousCursors, []);
    assert.equal(updated.parentElement.getAttribute("aria-sort"), "ascending");
    assert.equal(updated.querySelector(".sort-indicator").textContent, "↑");

    created.click(); await flush();
    assert.equal(query().get("sortBy"), "createdAt");
    assert.equal(query().get("sortDirection"), "desc", "a newly selected column starts newest first");
    assert.equal(created.parentElement.getAttribute("aria-sort"), "descending");
    assert.equal(updated.parentElement.getAttribute("aria-sort"), "none");
    created.click(); await flush();
    assert.equal(query().get("sortDirection"), "asc");
    assert.equal(created.querySelector(".sort-indicator").textContent, "↑");
    form.dispatch("submit"); await flush();
    assert.equal(query().get("chatId"), "oc_unsubmitted");
    assert.equal(query().get("sortBy"), "createdAt");
    assert.equal(query().get("sortDirection"), "asc", "filtering retains the selected sort");
    await moveSessionPage("next", kind);
    assert.equal(query().get("sortDirection"), "asc", "paging retains the selected sort");
    const savedPage = structuredClone(state[pageKey]);
    const savedTime = freshness.textContent;
    const savedRefreshedAt = state[`${kind}RefreshedAt`];
    const savedRows = document.querySelector(`#${dataKey}-body`).textContent;
    await advanceTime(1000);
    answerWith(async () => { throw new Error("sorting unavailable"); });
    updated.click(); await flush();
    assert.deepEqual(state[pageKey], savedPage);
    assert.equal(freshness.textContent, savedTime);
    assert.equal(state[`${kind}RefreshedAt`], savedRefreshedAt);
    assert.equal(document.querySelector(`#${dataKey}-body`).textContent, savedRows);
    assert.equal(created.parentElement.getAttribute("aria-sort"), "ascending", "failed sorting must retain the displayed sort");
    assert.equal(updated.parentElement.getAttribute("aria-sort"), "none");
    answerWith(null);

    const other = kind === "side" ? "ordinary" : "temporary";
    await selectSessionTab(other);
    await selectSessionTab(view);
    assert.deepEqual(state[pageKey], savedPage, "each Tab retains its applied sort and page");
    const unchangedFreshness = freshness.textContent;
    setStatus("unrelated operation message");
    assert.equal(freshness.textContent, unchangedFreshness);
    await resetSessionFilters(kind);
    assert.equal(query().get("sortBy"), "updatedAt");
    assert.equal(query().get("sortDirection"), "desc");
    assert.equal(updated.parentElement.getAttribute("aria-sort"), "descending");
    assert.equal(created.parentElement.getAttribute("aria-sort"), "none");
  }
}
