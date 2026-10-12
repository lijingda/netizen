window.location = { search: initialSearch, assign() {} };
window.confirm = () => true;
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, expire() {} };
const sessionStorage = { getItem() {}, setItem() {}, removeItem() {} };
let poll;
function setInterval(callback) { poll = callback; return 1; }
function clearInterval() {}
let now = 0;
const performance = { now: () => now };
const requests = [];
const confirmations = [];
window.confirm = (message) => { confirmations.push(message); return true; };
const ordinary = {
  bindingId: "binding-parent", shortId: "parent", chatId: "oc_parent", chatLabel: "研发群",
  chatLabelResolved: true, chatMode: "group", chatOpenUrl: "https://applink.feishu.cn/chat",
  nativeTitle: "普通会话标题", sessionType: "message", catalogState: "active", pointerState: "current",
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
  runtime: { sideId: "side-a", revision: 1, state: "open", sessionState: "open", turnId: "turn-a", turnState: "running" },
  actions: { stop: grant("stop"), close: grant("close") },
};
let listItems = [side];
let snapshotItems = [side.runtime];
let sideAnswer;
let ordinaryAnswer;
let snapshotAnswer;
let failSide = false;
async function fetch(path, options = {}) {
  const url = new URL(path, "https://admin");
  requests.push({ path: url.pathname, query: url.searchParams, options });
  let data;
  if (url.pathname === "/api/v1/projects/options") data = { items: [{ alias: "netizen", enabled: true }, { alias: "docs", enabled: false }], nextCursor: null };
  else if (url.pathname === "/api/v1/projects") data = { items: [], nextCursor: null };
  else if (url.pathname === "/api/v1/sessions") data = ordinaryAnswer ? await ordinaryAnswer() : { items: [structuredClone(ordinary)], nextCursor: "ordinary-next" };
  else if (url.pathname === "/api/v1/side-topics") {
    if (failSide) throw new Error("side list unavailable");
    data = sideAnswer ? await sideAnswer() : { items: structuredClone(listItems), nextCursor: url.searchParams.has("cursor") ? null : "side-next" };
  } else if (url.pathname === "/api/v1/runtime-snapshots") {
    data = snapshotAnswer ? await snapshotAnswer() : { bindings: url.searchParams.has("bindingIds") ? [ordinary.runtime] : [],
      sides: url.searchParams.has("sideIds") ? structuredClone(snapshotItems) : [], missingSideIds: [] };
  } else if (url.pathname.endsWith("/stop") || url.pathname.endsWith("/close")) data = { message: "操作完成" };
  else throw new Error(`Unexpected request ${path}`);
  return { ok: true, status: 200, json: async () => data };
}
for (const id of ["sessions-body", "sides-body"]) {
  Object.defineProperty(document.querySelector(`#${id}`), "rows", { get() { return this.children; } });
}
async function flush() { for (let i = 0; i < 80; i += 1) await Promise.resolve(); }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
// SHIPPED_ADMIN_PAGE
(async () => {
  await flush();
  if (initialSearch) {
    assert.equal(state.tab, "sessions");
    assert.equal(state.sessionTab, "ordinary");
    const query = requests.findLast((request) => request.path === "/api/v1/sessions").query;
    assert.equal(query.get("identity"), "binding-parent");
    assert.equal(query.get("inventoryState"), initialSearch.includes("inventoryState=all") ? "all" : null);
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
  assert.equal(row().children.length, 7);
  assert.equal(row().children[0].querySelector("a").href, side.topicOpenUrl);
  assert(!row().children[0].textContent.includes(side.chatId));
  assert.equal(row().children[1].children[0].textContent, "普通会话标题");
  assert(row().querySelector("details").textContent.includes("Side side-a"));
  assert.equal(row().querySelector(".runtime-state").textContent, "运行中");
  const parentLink = row().querySelector(".actions").querySelector("a");
  assert.equal(parentLink.href, "/?binding_id=binding-parent&inventoryState=all");
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
  const projectRoot = form.querySelectorAll("[data-side-multi]").find((root) => root.dataset.sideMulti === "project");
  projectRoot.querySelectorAll("input").find((input) => input.value === "docs").click();
  sideChatPicker.setSelection({ chatId: "oc_selected", name: "已选群" });
  document.querySelector("#side-chat-filter").value = "oc_selected";
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
  const snapshotWait = deferred(); snapshotAnswer = () => snapshotWait.promise;
  const oldPoll = poll(); await flush();
  await selectSessionTab("ordinary");
  snapshotWait.resolve({ bindings: [], sides: [{ ...side.runtime, state: "closing", sessionState: "closing" }], missingSideIds: [] });
  await oldPoll;
  assert.equal(state.sides.items[0].sessionState, "open");
  snapshotAnswer = null;
  await selectSessionTab("temporary");

  // Steering the same Turn does not invalidate Close or reload metadata/options.
  const beforeSteer = requests.length;
  snapshotItems = [{ ...side.runtime, revision: 2 }];
  now = 20000;
  await poll();
  assert.equal(requests.length, beforeSteer + 1);
  assert.equal(requests.at(-1).path, "/api/v1/runtime-snapshots");
  assert(buttons().every((button) => !button.disabled));

  // A Turn change refreshes eligibility; old exact grants still use server checks.
  const freshList = deferred(); sideAnswer = () => freshList.promise;
  snapshotItems = [{ ...side.runtime, revision: 3, turnId: "turn-b" }];
  now = 40000;
  const transition = poll(); await flush();
  assert(buttons().every((button) => !button.disabled));
  freshList.resolve({ items: [{ ...structuredClone(side), runtime: snapshotItems[0], actions: { close: grant("fresh-close") } }], nextCursor: null });
  await transition;
  assert.deepEqual(buttons().map((button) => button.textContent), ["结束临时会话"]);
  assert.equal(buttons()[0].disabled, false);
  sideAnswer = null;

  // A route can fail during creation before any process-local Side exists.
  listItems = [{ ...side, state: "creating", sessionState: "creating", runtime: null, actions: {} }];
  await refresh("sessions");
  const creationList = deferred(); sideAnswer = () => creationList.promise;
  snapshotAnswer = async () => ({ bindings: [], sides: [], missingSideIds: [side.sideId] });
  now = 60000;
  const creationPoll = poll(); await flush();
  assert.equal(row().querySelector(".side-session-state").textContent, "创建中");
  assert.equal(row().querySelector(".runtime-state").textContent, "创建中");
  creationList.resolve({ items: [{ ...listItems[0], state: "failed", sessionState: "failed" }], nextCursor: null });
  await creationPoll;
  assert.equal(row().querySelector(".side-session-state").textContent, "失败");
  assert.equal(row().querySelector(".runtime-state").textContent, "—");
  sideAnswer = null; snapshotAnswer = null;

  // An early registry entry is not proof that topic publication has completed.
  await refresh("sessions");
  snapshotItems = [side.runtime];
  now = 65000;
  await poll();
  assert.equal(row().querySelector(".side-session-state").textContent, "创建中");
  snapshotItems = [{ ...side.runtime, state: "closing", sessionState: "closing" }];
  now = 70000;
  await poll();
  assert.equal(row().querySelector(".side-session-state").textContent, "结束中");
  assert.equal(row().querySelector(".runtime-state").textContent, "结束中");

  // Publication changes only the route for a Side with no initial prompt.
  const idleRuntime = { ...side.runtime, revision: 0, turnId: null, turnState: null };
  listItems = [{ ...side, state: "creating", sessionState: "creating", runtime: idleRuntime, actions: {} }];
  await refresh("sessions");
  snapshotItems = [idleRuntime];
  listItems = [{ ...side, runtime: idleRuntime, actions: { close: grant("close") } }];
  const beforePublication = requests.filter((request) => request.path === "/api/v1/side-topics").length;
  now = 90000;
  await poll();
  assert.equal(requests.filter((request) => request.path === "/api/v1/side-topics").length, beforePublication + 1);
  assert.equal(row().querySelector(".side-session-state").textContent, "可用");
  assert.deepEqual(buttons().map((button) => button.textContent), ["结束临时会话"]);

  listItems = [{ ...side, parentSession: { bindingId: side.parentBindingId, available: false, title: null },
    projectAlias: null, runtime: null, sessionState: "closed", state: "closed", actions: {}, chatLabelResolved: false }];
  await refresh("sessions");
  assert.equal(row().children[1].children[0].textContent, "原会话已删除");
  assert.equal(row().querySelector(".runtime-state").textContent, "—");
  assert.equal(row().children[4].textContent, "原项目已不可用");
  assert.equal(buttons().length, 0);
  assert(!row().querySelector(".actions").querySelector("a"));
  const beforeTerminalPoll = requests.length;
  await poll();
  assert.equal(requests.length, beforeTerminalPoll);

  // Explicit schedule selection forces ordinary even after temporary was selected.
  selectTab("sessions", "ordinary"); await flush();
  assert.equal(state.sessionTab, "ordinary");
  assert.equal(document.querySelector("#sessions-ordinary").hidden, false);
  await checkAtomicSessionLists();
  await checkSidePollDuringNavigation();
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
      body: body.textContent, pagination: pagination.textContent });
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

async function checkSidePollDuringNavigation() {
  listItems = [structuredClone(side)];
  await selectSessionTab("temporary");
  await resetSideFilters();
  const slowSnapshot = deferred(); snapshotAnswer = () => slowSnapshot.promise;
  const polling = poll(); await flush();
  const next = deferred(); sideAnswer = () => next.promise;
  const paging = moveSidePage("next"); await flush();
  const requestsBefore = requests.length;
  const changed = { ...side.runtime, revision: 20, turnId: "new-turn" };
  now = 120000;
  slowSnapshot.resolve({ bindings: [], sides: [changed], missingSideIds: [] });
  await polling;
  assert.equal(requests.length, requestsBefore, "an old poll cannot supersede an in-flight page request");
  assert.equal(state.sides.items[0].runtime.turnId, side.runtime.turnId);
  await poll();
  assert.equal(requests.length, requestsBefore, "polling waits for the user request");
  next.resolve({ items: [{ ...side, sideId: "second-page", runtime: changed }], nextCursor: null });
  assert.equal(await paging, true);
  assert.equal(state.sidePage.number, 2);
  assert.equal(state.sides.items[0].sideId, "second-page");
  sideAnswer = null; snapshotAnswer = null;
}
