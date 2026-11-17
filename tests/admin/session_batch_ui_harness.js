let completed = false;
// Model the browser rules that matter to modal cleanup and focus restoration.
Element.prototype.getClientRects = function () {
  if (!document.body.contains(this)) return [];
  for (let node = this; node; node = node.parentElement) {
    if (node.hidden || (node.tagName === "dialog" && !node.open)) return [];
  }
  return [this.getBoundingClientRect()];
};
const originalFocus = Element.prototype.focus;
Element.prototype.focus = function (...args) {
  if (!this.disabled && this.getClientRects().length) originalFocus.apply(this, args);
};
Element.prototype.close = function () {
  if (!this.open) return;
  this.open = false;
  if (this.contains(document.activeElement)) document.activeElement = document.body;
  this.returnFocus?.focus();
  queueMicrotask(() => this.dispatch("close"));
};
const bodyClasses = new Set();
document.body.classList.toggle = (name, active) => active ? bodyClasses.add(name) : bodyClasses.delete(name);
const windowEvents = new Map();
window.addEventListener = (name, callback) => {
  const listeners = windowEvents.get(name) || []; listeners.push(callback); windowEvents.set(name, listeners);
};
let statusError = false;
document.querySelector("#status").classList.toggle = (name, value) => {
  if (name === "error") statusError = value;
};
process.once("beforeExit", () => assert(completed, "batch scenario must finish all assertions"));
const response = (status = 200, message = "完成") => ({ ok: status >= 200 && status < 300, status,
  json: async () => ({ message }) });
let mutationAnswer = async () => response();
let labelsAnswer = async () => ({ items: [] });
const priorFetch = fetch;
fetch = async (path, options = {}) => {
  const url = new URL(path, "https://admin");
  if (["/api/v1/sessions/archive", "/api/v1/sessions/delete-materialized", "/api/v1/sessions/delete-lazy"].includes(url.pathname)) {
    requests.push({ path: url.pathname, query: url.searchParams, options });
    return mutationAnswer(path, options);
  }
  if (url.pathname === "/api/v1/chat-labels") {
    requests.push({ path: url.pathname, query: url.searchParams, options });
    return { ...response(), json: labelsAnswer };
  }
  return priorFetch(path, options);
};
sessionStorage.setItem = () => assert.fail("selection must not persist");
const localStorage = { setItem: () => assert.fail("selection must not persist") };
// SHIPPED_ADMIN_PAGE
const node = (name) => document.querySelector(`#session-batch-${name}`);
const rows = () => document.querySelector("#sessions-body").children.filter((row) => row.dataset.bindingId);
const checks = () => rows().map((row) => row.querySelector(".session-select"));
const all = () => document.querySelector("#session-select-page");
const posts = () => requests.filter((request) => request.options.method === "POST");
const reads = () => requests.filter((request) => request.path === "/api/v1/sessions");
const summary = () => node("progress").textContent;
const details = () => node("results").children.map((item) => item.textContent);
const selected = () => rows().filter((row) => row.querySelector(".session-select").checked).map((row) => row.dataset.bindingId);
const exact = (id, action, revision) => fixtureAction(
  `sessions.${{ delete: "delete-materialized", lazy: "delete-lazy" }[action] || action}`,
  { resource: "binding", targetId: id, scopeKey: `scope-${id}`,
    actionToken: `${revision}-${action}-${id}`, csrfToken: `csrf-${revision}-${id}` });
function sample(id, properties = {}, revision = "old") {
  return { ...structuredClone(ordinary), bindingId: id, shortId: `short-${id}`, nativeTitle: `会话 ${id}`,
    runtime: { primaryStatus: "idle", primaryStatusResolution: "resolved" },
    pointerState: "inactive", actions: { archive: exact(id, "archive", revision),
      deleteMaterialized: exact(id, "delete", revision), deleteLazy: exact(id, "lazy", revision) }, ...properties };
}
function answer(items, cursor = "next") {
  ordinaryAnswer = async () => ({ items: structuredClone(items), nextCursor: cursor });
}
async function show(items, inventory = "active") {
  answer(items);
  const query = `inventoryState=${inventory}&pageSize=100&sortBy=updatedAt&sortDirection=desc`;
  sessionLists.session.restore(new URLSearchParams(query));
  await selectSessionTab("ordinary"); await flush();
}
function layout(value) {
  const input = document.querySelector("#session-layout"); input.value = value; input.dispatch("change");
}
function start(operation = "archive") {
  const trigger = node(operation); trigger.focus(); trigger.click();
}
async function confirm(operation = "archive") {
  confirmResponder = null; start(operation); await flush();
  assert(document.querySelector("#admin-confirm").open);
  assert.equal(posts().length, 0, "no mutation before consent");
  document.querySelector("#admin-confirm-submit").click(); await flush();
}
function finish() {
  assert(node("dialog").open);
  assert.equal(node("close").hidden, false);
  node("close").click();
  assert.equal(node("dialog").open, false);
}

async function selectionScenario() {
  const items = [sample("a"), sample("b", { projectAlias: "other" }), sample("disabled", { actions: {} })];
  await show(items);
  const headers = document.querySelector("#sessions-body").parentElement.querySelector("thead").querySelectorAll("th");
  assert.equal(headers.length, 9); assert.equal(headers[0].textContent, "选择");
  for (const row of rows()) {
    assert.equal(row.children.length, headers.length);
    row.children.forEach((cell, index) => assert.equal(cell.getAttribute("data-label"), headers[index].textContent.replace(/[↕↑↓]/g, "").trim()));
  }
  assert(checks().every((input) => input.type === "checkbox"));
  assert.equal(checks()[0].getAttribute("aria-label"), "选择会话：会话 a · short-a");
  assert(checks()[2].disabled); assert.equal(checks()[2].title, "此会话不支持批量操作");
  assert.equal(node("archive").getAttribute("aria-disabled"), "true");
  start(); await flush(); assert.equal(reads().length, 1, "disabled actions do not even refresh");
  const url = window.location.pathname + window.location.search;
  checks()[0].click(); assert(all().indeterminate); assert(!all().checked);
  all().click(); assert(all().checked); assert(!all().indeterminate); assert.deepEqual(selected(), ["a", "b"]);
  const saved = checks(); const count = requests.length;
  layout("project");
  document.querySelector("#session-groups-collapse").click();
  assert.match(node("count").textContent, /已选 2 项（其中 2 项在折叠分组中）/);
  assert.equal(document.querySelector("#sessions-body").querySelector("th").getAttribute("colspan"), "9");
  all().click(); assert.deepEqual(selected(), []);
  all().click(); assert.equal(selected().length, 2, "select all includes collapsed rows");
  layout("compact"); assert.deepEqual(checks(), saved); assert(!node("count").textContent.includes("折叠"));
  assert.equal(requests.length, count, "layout and selection do not query");
  assert.equal(window.location.pathname + window.location.search, url);
  await refresh("sessions"); assert.deepEqual(selected(), ["a", "b"]);
  // The normal single-action mutation path refreshes once without losing selection.
  await mutate("/api/v1/sessions/stop", exact("a", "stop", "single"));
  assert.deepEqual(selected(), ["a", "b"]);
  answer([items[0], items[2]]); await refresh("sessions"); assert.deepEqual(selected(), ["a"]);
  node("clear").click(); checks()[0].click(); await refresh("sessions"); assert.deepEqual(selected(), ["a"]);
  const label = deferred(); labelsAnswer = () => label.promise;
  answer([sample("a", { chatLabelResolved: false, chatLabel: null })]);
  await refresh("sessions");
  label.resolve({ items: [{ chatId: ordinary.chatId, chatLabel: "补读的名称", chatLabelResolved: true }] });
  await flush(); assert.deepEqual(selected(), ["a"]);
  assert(rows()[0].textContent.includes("补读的名称"));
  for (const change of [
    () => sessionLists.session.move("next"),
    () => sessionLists.session.sort("createdAt"),
    () => { const input = document.querySelector("#session-search"); input.value = "搜索"; document.querySelector("#session-filter").dispatch("submit"); },
    () => { const input = document.querySelector("#session-page-size"); input.value = "50"; input.dispatch("change"); },
    () => { const input = document.querySelector("#session-inventory-state"); input.value = "archived"; input.dispatch("change"); document.querySelector("#session-filter").dispatch("submit"); },
    () => selectSessionTab("temporary"),
    () => selectTab("projects"),
  ]) {
    if (state.tab !== "sessions" || state.sessionTab !== "ordinary") await selectSessionTab("ordinary");
    if (!checks()[0].checked) checks()[0].click();
    await change(); await flush();
    assert.equal(sessionSelection.ids.size, 0, "a successful new page or navigation clears selection");
  }
  await selectSessionTab("temporary"); layout("compact");
  const sides = document.querySelector("#sides-body");
  assert.equal(sides.querySelector(".session-select"), null); assert.equal(sides.children[0].children.length, 7);
  const sideLayout = document.querySelector("#side-layout"); sideLayout.value = "project"; sideLayout.dispatch("change");
  assert.equal(sides.querySelector("th").getAttribute("colspan"), "7");
  sideAnswer = async () => ({ items: [], nextCursor: null }); await refresh("sessions");
  assert.equal(sides.querySelector("td").colSpan, 7);
  await show([sample("a")]); all().click(); expireAdminSession();
  assert.equal(sessionSelection.ids.size, 0); assert(!checks()[0].checked); assert(checks()[0].disabled);
}

async function confirmationScenario() {
  await show([sample("dropped"), sample("unsupported"), sample("running"), sample("archived")]);
  await sessionLists.session.move("next"); all().click();
  const fresh = [sample("running", { nativeTitle: "<b>新标题</b>", pointerState: "current",
    runtime: { primaryStatus: "running", primaryStatusResolution: "deferred" } }, "fresh"),
    sample("unsupported", { actions: {} }), sample("archived", {
      runtime: { primaryStatus: "running", primaryStatusResolution: "archived" } }, "fresh")];
  answer([fresh[2], fresh[1], fresh[0]]); layout("project"); document.querySelector("#session-groups-collapse").click();
  const applied = reads().at(-1).query.toString();
  document.querySelector("#session-search").value = "未应用草稿";
  confirmResponder = null; start(); await flush();
  assert.equal(reads().at(-1).query.toString(), applied, "same query and cursor, not draft controls");
  assert.equal(posts().length, 0);
  const dialog = document.querySelector("#admin-confirm");
  assert.match(dialog.textContent, /批量归档会话/); assert.match(dialog.textContent, /以下 2 个会话/);
  assert.match(dialog.textContent, /1 个已移出本页、1 个不支持该操作/);
  assert.match(dialog.textContent, /其中 1 个是所在聊天的当前会话/);
  assert.match(dialog.textContent, /其中 1 个正在运行或状态未确认/);
  const lines = dialog.querySelector("ul").children.map((item) => item.textContent);
  assert.deepEqual(lines, ["会话 archived · short-archived · netizen · 研发群",
    "<b>新标题</b> · short-running · netizen · 研发群（当前会话）（运行中（待确认））"]);
  assert.equal(dialog.querySelector("b"), null);
  assert(!document.querySelector("#admin-confirm-submit").className.includes("danger"));
  const n = reads().length;
  document.querySelector("#admin-confirm-submit").click(); await flush();
  assert.deepEqual(posts().map((item) => JSON.parse(item.options.body)), [fresh[2].actions.archive, fresh[0].actions.archive].map(fixtureActionPayload));
  assert(posts().every((item) => item.path === "/api/v1/sessions/archive"));
  assert.equal(reads().length, n + 1); assert.equal(selected().length, 0);
  assert.equal(summary(), "已完成 2 项。"); assert.equal(statusNode.textContent, summary());
  assert.equal(statusError, false);
  finish(); assert.equal(document.activeElement, node("archive"));
}

async function preparationScenario() {
  await show([sample("a")]); all().click();
  ordinaryAnswer = async () => { throw new Error("服务端目录不可用"); };
  start(); await flush();
  assert.equal(statusNode.textContent, "刷新当前页失败，未执行任何操作。");
  assert(document.querySelector("#sessions-error").textContent.includes("服务端目录不可用"));
  assert.equal(posts().length, 0); assert.deepEqual(selected(), ["a"]);
  answer([sample("a")]); confirmResponder = () => false;
  start(); await flush(); assert.deepEqual(selected(), ["a"]); assert.equal(posts().length, 0);
  confirmResponder = null;
  window.NetizenConfirm.ask({ body: "另一个操作" });
  const count = reads().length; start(); await flush(); assert.equal(reads().length, count);
  assert.equal(statusNode.textContent, "另一个确认窗口已打开，批量操作未执行。");
  window.NetizenConfirm.cancel();
  answer([sample("a", { actions: {} })]); start(); await flush();
  assert.equal(statusNode.textContent, "所选会话已不在本页或不支持该操作，未执行。");
  assert.equal(posts().length, 0); assert(!document.querySelector("#admin-confirm").open);
  answer([]); await refresh("sessions");
  assert(document.querySelector("#session-batch").hidden);
  assert.equal(document.querySelector("#sessions-body").querySelector("td").colSpan, 9);
}

async function sequentialScenario() {
  await show([sample("a"), sample("b"), sample("c"), sample("d")]); all().click();
  const waits = [deferred(), deferred(), deferred(), deferred()];
  mutationAnswer = () => waits[posts().length - 1].promise;
  await confirm();
  assert.equal(posts().length, 1); assert.equal(summary(), "正在归档第 1 / 4 项");
  const before = reads().length;
  start("delete"); await flush(); assert.equal(posts().length, 1); assert.equal(reads().length, before);
  node("dialog").dispatch("cancel"); node("dialog").dispatch("click", { clientX: -1, clientY: -1 });
  assert(node("dialog").open, "Escape and backdrop cannot dismiss running batch");
  for (const [index, status] of [200, 409, 404, 204].entries()) {
    assert.equal(posts().length, index + 1);
    waits[index].resolve(response(status, `服务端原因 ${status}`)); await flush();
  }
  assert.deepEqual(posts().map((item) => JSON.parse(item.options.body).target.targetId), ["a", "b", "c", "d"]);
  assert.equal(reads().length, before + 1);
  assert.equal(summary(), "已完成 2 项，未完成 2 项。");
  assert.match(details()[1], /未完成（服务端原因 409）/); assert.match(details()[2], /未完成（服务端原因 404）/);
  assert.equal(statusNode.textContent, summary()); assert.equal(statusError, true); finish();
}

async function unknownScenario(outcome) {
  await show([sample("a"), sample("b"), sample("c")]); all().click();
  mutationAnswer = async () => {
    if (posts().length === 1) return response();
    if (outcome === "network") throw new TypeError("网络断开");
    if (outcome === "parse") return { ...response(), json: async () => { throw new SyntaxError("无法解析"); } };
    return response(Number(outcome), `服务端原因 ${outcome}`);
  };
  await confirm();
  assert.equal(posts().length, 2); assert.equal(reads().length, 3, "initial, before confirm, once after execution");
  assert.equal(summary(), "已完成 1 项，结果未确认 1 项，未执行 1 项。");
  assert.equal(statusError, true);
  assert.match(details()[1], /结果未确认/); assert.match(details()[2], /未执行/);
  assert(details()[1].includes(outcome === "network" ? "网络断开" : outcome === "parse" ? "无法解析" : `服务端原因 ${outcome}`));
  await advanceTime(60000); assert.equal(posts().length, 2, "no automatic retries"); finish();
}

async function expiryScenario() {
  await show([sample("a"), sample("b"), sample("c")]); all().click();
  mutationAnswer = async () => response(posts().length === 1 ? 200 : 401);
  await confirm();
  assert(state.authExpired); assert.equal(posts().length, 2); assert.equal(reads().length, 2);
  assert(!node("dialog").open); assert.equal(document.querySelector("#auth-expired").hidden, false);
  assert.equal(sessionSelection.ids.size, 0);
  assert.equal(statusNode.textContent, "登录已过期，批量操作已停止：已完成 1 项，请重新登录后刷新核对。");
  assert.equal(statusError, true);
  setStatus("另一个操作的晚到提示");
  assert.equal(statusNode.textContent, "登录已过期，批量操作已停止：已完成 1 项，请重新登录后刷新核对。");
  statusNode.querySelector("button").click(); assert(statusNode.hidden);
}

async function stopScenario(outcome) {
  await show([sample("a"), sample("b"), sample("c")]); all().click();
  const wait = deferred();
  mutationAnswer = async () => posts().length === 1 ? response() : outcome === "parsing"
    ? { ...response(), json: () => wait.promise } : wait.promise;
  await confirm();
  assert.equal(posts().length, 2);
  node("stop").click(); await flush();
  assert.equal(posts().length, 2); assert.equal(reads().length, 3);
  assert.equal(summary(), "已完成 1 项，结果未确认 1 项，未执行 1 项。");
  assert.match(details()[1], /结果未确认（已停止等待，服务端可能仍在完成）/);
  const saved = { summary: summary(), details: details(), status: statusNode.textContent, data: sessionLists.session.snapshot().items, requests: requests.length };
  if (outcome !== "401") { finish(); checks()[0].click(); }
  if (outcome === "network") wait.reject(new TypeError("late offline"));
  else wait.resolve(outcome === "parsing" ? { message: "late parsed" } : response(Number(outcome)));
  await flush();
  assert.deepEqual({ summary: summary(), details: details(), data: sessionLists.session.snapshot().items, requests: requests.length },
    { summary: saved.summary, details: saved.details, data: saved.data, requests: saved.requests });
  if (outcome === "401") {
    assert(state.authExpired, "late 401 still expires the global login");
    assert.equal(statusNode.textContent, `登录已过期。批量结果：${saved.summary}`);
    assert.equal(document.querySelector("#auth-expired").hidden, false);
    assert.deepEqual(selected(), []);
  } else {
    assert(!state.authExpired); assert.equal(statusNode.textContent, saved.status);
    assert.deepEqual(selected(), ["a"]);
  }
  assert(!node("dialog").open);
}

async function statesScenario() {
  for (const inventory of ["active", "archived", "lazy"]) {
    requests.length = 0;
    await show([sample("a", { catalogState: inventory, nativeTitle: null })], inventory);
    assert.equal(node("archive").hidden, inventory !== "active"); assert(!node("delete").hidden);
    assert.equal(checks()[0].getAttribute("aria-label"), `选择会话：${inventory === "lazy" ? "未开始的会话" : "未命名会话"} · short-a`);
    all().click(); confirmResponder = null; start("delete"); await flush();
    const dialog = document.querySelector("#admin-confirm");
    assert(document.querySelector("#admin-confirm-submit").className.includes("danger"));
    assert.match(dialog.textContent, inventory === "lazy" ? /批量删除未开始的会话/ : /批量永久删除会话/);
    if (inventory !== "lazy") assert(dialog.textContent.includes(materializedDeleteWarning));
    else assert(dialog.textContent.includes("将永久删除以下 1 个尚未开始的会话。"));
    document.querySelector("#admin-confirm-submit").click(); await flush();
    assert.equal(posts()[0].path, `/api/v1/sessions/delete-${inventory === "lazy" ? "lazy" : "materialized"}`);
    assert.equal(JSON.parse(posts()[0].options.body).actionToken, `old-${inventory === "lazy" ? "lazy" : "delete"}-a`);
    finish(); assert.equal(document.activeElement, node("delete"));
    answer([sample("a", { actions: inventory === "active" ? { deleteLazy: exact("a", "lazy", "x") }
      : { archive: exact("a", "archive", "x") } })]);
    await refresh("sessions"); assert(node("archive").hidden); assert(node("delete").hidden); assert(checks()[0].disabled);
  }
}

async function refreshFailureScenario() {
  await show([sample("a")]); all().click();
  mutationAnswer = async () => { ordinaryAnswer = async () => { throw new Error("刷新断网"); }; return response(); };
  await confirm();
  assert.equal(reads().length, 3); assert.equal(posts().length, 1);
  assert.equal(summary(), "已完成 1 项。"); assert.equal(node("note").textContent, "列表刷新失败，请手动刷新核对。");
  assert.equal(statusNode.textContent, "已完成 1 项。 列表刷新失败，请手动刷新核对。");
  assert.equal(statusError, true);
  assert.equal(selected().length, 0); finish();
}

async function emptiedPageScenario() {
  await show([sample("a")]); all().click();
  mutationAnswer = async () => { answer([]); return response(); };
  await confirm("delete");
  assert(document.querySelector("#session-batch").hidden);
  assert.equal(rows().length, 0); assert.equal(summary(), "已完成 1 项。");
  finish();
  assert.equal(document.activeElement, document.querySelectorAll("[data-refresh]").find((button) => button.dataset.refresh === "sessions"));
}

async function pageCloseScenario() {
  await show([sample("a"), sample("b")]); all().click();
  const wait = deferred(); mutationAnswer = () => wait.promise;
  await confirm(); for (const callback of windowEvents.get("pagehide")) callback(); await flush();
  wait.resolve(response()); await flush();
  assert.equal(posts().length, 1); assert.equal(reads().length, 2, "a closed document does not refresh or resume");
  assert(!node("dialog").open, "pagehide must not trigger native-close reopening");
  assert(!bodyClasses.has("session-batch-open"));
}

async function nativeCloseScenario() {
  await show([sample("a")]); all().click();
  const wait = deferred(); mutationAnswer = () => wait.promise;
  await confirm();
  node("dialog").close(); await flush();
  assert(node("dialog").open, "a browser-forced close reopens a running modal");
  assert(bodyClasses.has("session-batch-open")); assert.equal(posts().length, 1);
  wait.resolve(response()); await flush();
  assert.equal(summary(), "已完成 1 项。");
  node("dialog").close(); await flush();
  assert(!node("dialog").open); assert(!bodyClasses.has("session-batch-open"));
  assert.equal(sessionBatch, null); assert.equal(document.activeElement, node("archive"));
  // The previous close event must not leave the next batch blocked or reopen it.
  all().click(); confirmResponder = null; start(); await flush();
  assert(document.querySelector("#admin-confirm").open);
  document.querySelector("#admin-confirm-cancel").click(); await flush();
  assert.equal(sessionBatch, null);
}

async function navigationScenario(temporary) {
  await show([sample("a"), sample("b")]); all().click();
  const pendingPost = deferred(); mutationAnswer = () => pendingPost.promise;
  await confirm();
  const before = reads().length;
  const sideRead = deferred(); sideAnswer = () => sideRead.promise;
  const navigation = temporary ? selectSessionTab("temporary") : selectTab("projects");
  await flush();
  assert(node("dialog").open); assert(node("dialog").getClientRects().length, "modal remains visible outside the old panel");
  assert.equal(reads().length, before, "leaving ordinary sessions never sends an ending ordinary read");
  assert.equal(summary(), "结果未确认 1 项，未执行 1 项。"); assert(node("note").hidden);
  if (temporary) {
    sideRead.resolve({ items: [{ ...structuredClone(side), chatLabel: "切换后临时会话" }], nextCursor: null });
    await navigation; await flush();
    assert(document.querySelector("#sides-body").textContent.includes("切换后临时会话"), "batch completion does not invalidate the Side read");
  } else await navigation;
  const saved = details(); pendingPost.resolve(response()); await flush();
  assert.deepEqual(details(), saved); assert.equal(reads().length, before);
  const focused = [];
  const focus = Element.prototype.focus;
  Element.prototype.focus = function (...args) { focused.push(this); return focus.apply(this, args); };
  finish(); await flush(); Element.prototype.focus = focus;
  assert(!bodyClasses.has("session-batch-open")); assert.equal(sessionBatch, null);
  assert(!focused.includes(node("archive")), "never force focus into a hidden panel");
  if (temporary) assert.equal(document.activeElement, document.querySelectorAll("[data-refresh]").find((button) => button.dataset.refresh === "sessions"));
  else assert(!focused.some((button) => button.dataset.refresh === "sessions"), "hidden refresh control is not focused either");
}

async function lostGrantScenario() {
  await show([sample("a"), sample("b"), sample("gone")]); all().click();
  answer([sample("a", { actions: {} }), sample("b", { actions: { deleteMaterialized: exact("b", "delete", "new") } })]);
  await refresh("sessions");
  assert.deepEqual(selected(), ["b"]); assert.equal(node("count").textContent, "已选 1 项");
  assert(checks()[0].disabled); assert(!checks()[0].checked);
  assert.equal(checks()[0].title, "此会话不支持批量操作"); assert(all().checked);
  checks()[1].click(); assert.deepEqual(selected(), []); assert(!all().checked); assert(!all().indeterminate);
  answer([sample("a"), sample("b")]); await refresh("sessions");
  assert.deepEqual(selected(), [], "restoring grants does not restore discarded selections");
}

async function stagedExpiryScenario(stage) {
  await show([sample("a")]); all().click();
  const wait = deferred();
  if (stage === "preparing") {
    ordinaryAnswer = () => wait.promise; start(); await flush();
  } else if (stage === "confirming") {
    confirmResponder = null; start(); await flush();
  } else await confirm();
  const saved = details(), result = summary();
  assert.equal(sessionBatch.phase, stage);
  expireAdminSession(); await flush();
  assert(!node("dialog").open); assert(!document.querySelector("#admin-confirm").open);
  assert(!bodyClasses.has("session-batch-open")); assert.equal(sessionSelection.ids.size, 0);
  assert.equal(document.querySelector("#auth-expired").hidden, false);
  assert.equal(statusNode.textContent, stage === "finished" ? `登录已过期。批量结果：${result}` : "登录已过期，批量操作未执行。");
  assert.equal(posts().length, stage === "finished" ? 1 : 0);
  if (stage === "preparing") wait.resolve({ items: [sample("a")], nextCursor: null });
  await flush(); assert(!node("dialog").open); assert.deepEqual(details(), saved);
}

async function supersededPreparationScenario() {
  for (const change of [() => sessionLists.session.sort("createdAt"), () => refresh("sessions")]) {
    await show([sample("a")]); all().click();
    const wait = deferred(); ordinaryAnswer = () => wait.promise;
    start(); await flush();
    answer([sample("a", { nativeTitle: "新读取" })]); await change();
    wait.resolve({ items: [sample("a", { nativeTitle: "旧读取" })], nextCursor: null }); await flush();
    assert.equal(statusNode.textContent, "列表已变化，批量操作未执行。");
    assert.equal(posts().length, 0); assert(!document.querySelector("#admin-confirm").open);
    assert(rows()[0].textContent.includes("新读取"));
  }
}

async function otherConfirmationScenario() {
  await show([sample("a")]); all().click();
  const wait = deferred(); ordinaryAnswer = () => wait.promise;
  confirmResponder = null; start(); await flush();
  const other = window.NetizenConfirm.ask({ body: "另一个确认", trigger: document.querySelector("#session-search") });
  wait.resolve({ items: [sample("a")], nextCursor: null }); await flush();
  assert.equal(statusNode.textContent, "另一个确认窗口已打开，批量操作未执行。");
  assert(document.querySelector("#admin-confirm").open); assert.equal(posts().length, 0);
  assert(document.querySelector("#admin-confirm").textContent.includes("另一个确认"));
  document.querySelector("#admin-confirm-cancel").click(); await other;
}

async function supersededEndingScenario() {
  await show([sample("a")]); all().click();
  const wait = deferred();
  mutationAnswer = async () => { ordinaryAnswer = () => wait.promise; return response(); };
  await confirm(); assert.equal(sessionBatch.phase, "refreshing");
  answer([sample("a", { nativeTitle: "新读取结果" })]); await refresh("sessions");
  wait.resolve({ items: [sample("a", { nativeTitle: "过期结果" })], nextCursor: null }); await flush();
  assert.equal(summary(), "已完成 1 项。"); assert(node("note").hidden);
  assert.equal(statusNode.textContent, summary()); assert.equal(statusError, false);
  assert(rows()[0].textContent.includes("新读取结果"));
  assert.equal(reads().length, 4); assert.equal(posts().length, 1); finish();
}

async function cancelFocusScenario() {
  await show([sample("a")]); all().click(); confirmResponder = null;
  start(); await flush(); document.querySelector("#admin-confirm-cancel").click();
  assert.equal(document.activeElement, document.body, "browser cannot focus the still-disabled trigger");
  await flush(); assert.equal(document.activeElement, node("archive")); assert(!node("archive").disabled);
  assert.deepEqual(selected(), ["a"]); assert.equal(posts().length, 0);
  start(); await flush(); document.querySelector("#admin-confirm-cancel").click();
  document.querySelector("#session-search").focus(); await flush();
  assert.equal(document.activeElement, document.querySelector("#session-search"), "do not steal an intentional new focus");
  const dialog = document.querySelector("#admin-confirm"), close = dialog.close;
  dialog.close = function () {
    const focused = document.activeElement; close.call(this); document.activeElement = focused;
  };
  start(); await flush(); document.querySelector("#admin-confirm-cancel").click();
  assert.equal(document.activeElement, document.querySelector("#admin-confirm-cancel"));
  await flush(); assert.equal(document.activeElement, node("archive"), "Chrome's stale focus inside the closed dialog is also restored");
  dialog.close = close;
}

async function warningScenario() {
  const quiet = ["goal-paused", "goal-blocked", "goal-usageLimited", "goal-budgetLimited", "goal-complete", "idle"];
  const items = quiet.map((status) => sample(status, { runtime: { primaryStatus: status, primaryStatusResolution: "resolved" } }));
  items.push(sample("active", { runtime: { primaryStatus: "goal-active", primaryStatusResolution: "resolved" } }),
    sample("deferred", { runtime: { primaryStatus: null, primaryStatusResolution: "deferred" } }),
    sample("unavailable", { runtime: { primaryStatus: null, primaryStatusResolution: "unavailable" } }),
    sample("running", { runtime: { primaryStatus: "running", primaryStatusResolution: "local" } }),
    sample("archived", { runtime: { primaryStatus: null, primaryStatusResolution: "archived" } }));
  await show(items); all().click(); confirmResponder = null; start(); await flush();
  const dialog = document.querySelector("#admin-confirm");
  assert.match(dialog.textContent, /其中 4 个正在运行或状态未确认，Codex 会关闭正在运行的任务。/);
  const lines = dialog.querySelector("ul").children.map((item) => item.textContent);
  for (const [index] of quiet.entries()) assert(lines[index].endsWith("研发群"), "persisted inactive Goals and idle have no running suffix");
  assert(lines[6].endsWith("（Goal 进行中）")); assert(lines[7].endsWith("（状态待确认）"));
  assert(lines[8].endsWith("（状态暂不可用）")); assert(lines[9].endsWith("（运行中）"));
  assert(lines[10].endsWith("研发群")); document.querySelector("#admin-confirm-cancel").click(); await flush();
}

async function chatFallbackScenario() {
  const chatId = "oc_unresolved_01234567890123456789";
  await show([sample("unresolved", { chatId, chatLabel: chatId, chatLabelResolved: false }),
    sample("missing", { chatId: null, chatLabel: null, chatLabelResolved: false })]);
  all().click(); confirmResponder = null; start(); await flush();
  const lines = document.querySelector("#admin-confirm").querySelector("ul").children.map((item) => item.textContent);
  assert(lines[0].endsWith(`群聊 · ${chatId.slice(0, 12)}…`)); assert(!lines[0].includes(chatId));
  assert.equal(rows()[0].querySelector(".chat-link").textContent, `群聊 · ${chatId.slice(0, 12)}…`);
  assert(lines[1].endsWith("聊天不可用"));
  document.querySelector("#admin-confirm-cancel").click(); await flush();
}

async function groupedOrderScenario(mode) {
  await show([sample("a", { projectAlias: "one", chatId: "chat-one" }),
    sample("b", { projectAlias: "two", chatId: "chat-two" }), sample("c", { projectAlias: "one", chatId: "chat-one" })]);
  all().click(); layout(mode); document.querySelector("#session-groups-collapse").click();
  confirmResponder = null; start(); await flush();
  const expected = ["a", "c", "b"];
  assert.deepEqual(rows().map((row) => row.dataset.bindingId), expected); assert(rows().every((row) => row.hidden));
  const lines = document.querySelector("#admin-confirm").querySelector("ul").children;
  assert.deepEqual(lines.map((item) => item.textContent.split(" · ")[0]), expected.map((id) => `会话 ${id}`));
  document.querySelector("#admin-confirm-submit").click(); await flush();
  assert.deepEqual(posts().map((item) => JSON.parse(item.options.body).target.targetId), expected);
  assert.deepEqual(details().map((item) => item.split(" · ")[0]), expected.map((id) => `会话 ${id}`)); finish();
}

async function resultPresentationScenario() {
  await show([sample("a"), sample("b")]); all().click();
  const first = deferred(), second = deferred(); mutationAnswer = () => posts().length === 1 ? first.promise : second.promise;
  await confirm(); assert(node("results").hidden); assert(node("note").hidden);
  first.resolve(response(409, "授权已失效")); await flush();
  assert(!node("results").hidden); assert.equal(node("results").children[0].className, "error");
  assert(node("note").hidden);
  ordinaryAnswer = async () => { throw new Error("刷新失败"); };
  second.resolve(response()); await flush();
  assert(!node("note").hidden); assert.equal(node("note").className, "error");
  assert.equal(node("results").children[1].className, ""); finish();
}

async function queryBudgetScenario() {
  await show(Array.from({ length: 100 }, (_, i) => sample(String(i))));
  const original = sessionBatchActions;
  let actionCalculations = 0;
  sessionBatchActions = (...args) => { actionCalculations += 1; return original(...args); };
  try {
    await loadSessions(); assert.equal(actionCalculations, 1, "100 rows and their selection controls share one action calculation");
    actionCalculations = 0; all().click(); assert.equal(actionCalculations, 1, "select-all shares its action calculation with the toolbar render");
    assert.equal(selected().length, 100);
  } finally { sessionBatchActions = original; }
}

(async () => {
  await flush();
  const scenarios = { selection: selectionScenario, confirmation: confirmationScenario, preparation: preparationScenario,
    sequential: sequentialScenario, expiry: expiryScenario, states: statesScenario,
    "refresh-failure": refreshFailureScenario, "empty-page": emptiedPageScenario, "page-close": pageCloseScenario,
    "native-close": nativeCloseScenario, "lost-grants": lostGrantScenario, "superseded-preparation": supersededPreparationScenario,
    "other-confirmation": otherConfirmationScenario, "superseded-ending": supersededEndingScenario, "cancel-focus": cancelFocusScenario,
    warnings: warningScenario, "chat-fallback": chatFallbackScenario, presentation: resultPresentationScenario, "query-budget": queryBudgetScenario };
  if (scenario.startsWith("stop-")) await stopScenario(scenario.slice(5));
  else if (scenario.startsWith("expiry-")) await stagedExpiryScenario(scenario.slice(7));
  else if (scenario.startsWith("navigate-")) await navigationScenario(scenario === "navigate-temporary");
  else if (scenario.startsWith("order-")) await groupedOrderScenario(scenario.slice(6));
  else if (scenarios[scenario]) await scenarios[scenario]();
  else await unknownScenario(scenario);
  completed = true;
})().catch((error) => { console.error(error); process.exitCode = 1; });
