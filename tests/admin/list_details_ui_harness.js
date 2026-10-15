const flush = async () => { for (let index = 0; index < 15; index += 1) await Promise.resolve(); };
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
const state = { projects: { items: [] } };
const mutations = [];
const confirmations = [];
let accepted = false;
window.confirm = (message) => { confirmations.push(message); return accepted; };
window.prompt = () => "new-name";
async function mutate(...args) { mutations.push(args); }
let editorCall = null;
function openSessionSettingsEditor(...args) { editorCall = args; }
function openScheduleForSession(...args) { editorCall = args; }
const session = {
  bindingId: "binding-full-12345678901234567890", nativeThreadId: "thread-full-12345678901234567890",
  shortId: "b-short", chatId: "oc_full-12345678901234567890", topicId: "om_topic-12345678901234567890",
  nativeTitle: "<b>会话名称</b>", nativePreview: "<img src=x onerror=alert(1)>", scopeKind: "group",
  chatLabelResolved: true, chatLabel: "研发群", chatMode: "group", topicOpenUrl: "https://applink.feishu.cn/topic",
  scopeKey: "group:exact", catalogState: "active", actions: {},
};
const requests = [];
const plan = (id) => ({
  id, name: `<b>${id}</b>`, revision: 7, enabled: true, chat_id: "oc_target", target_kind: "binding",
  target_binding_id: "binding-target", target_label: "原会话", instructions: "<img src=x onerror=alert(1)>\n计划指令",
  lifecycle: { ended: false }, execution: { kind: "none", status: "not_started" },
  schedule: { kind: "daily", at: "09:00", timezone: "Asia/Shanghai" },
});
const view = (id) => ({ plan: plan(id), preview: [{ local: "2030-01-01 09:00", utc: "2030-01-01 01:00Z" }] });
const runPage = (label, next = null) => ({ runs: [{
  due_at: label, trigger_source: "manual", status: "input_steered", target_kind: "binding",
  binding_id: "binding-target", feishu_url: "https://applink.feishu.cn/message",
}], next_cursor: next });
let respond = async (query) => query.get("mode") === "view" ? view(query.get("plan_id"))
  : runPage(query.get("cursor") || "first-page", query.has("cursor") ? null : "second-cursor");
async function api(path, options = {}) {
  assert(!options.method || options.method === "GET", "details must remain read-only");
  const url = new URL(path, "http://localhost");
  assert.equal(url.pathname, "/api/v1/schedules");
  requests.push(url.searchParams);
  return respond(url.searchParams);
}
// SHIPPED_LIST_DETAILS
(async () => {
  const location = document.createElement("tr");
  sessionLocationCell(location, session);
  assert.equal(location.querySelector("a").textContent, "研发群");
  assert.equal(location.querySelector("a").href, session.topicOpenUrl);
  assert(!location.textContent.includes(session.chatId));
  assert(!location.textContent.includes(session.topicId));
  const identityRow = document.createElement("tr");
  sessionIdentityCell(identityRow, session);
  const disclosure = identityRow.querySelector("details");
  assert(!disclosure.open);
  assert.equal(disclosure.querySelector("summary").textContent, "会话标识");
  assert.deepEqual(disclosure.querySelectorAll("input").map((input) => input.value),
    [session.chatId, session.topicId, session.bindingId, session.nativeThreadId]);
  assert(disclosure.querySelectorAll("input").every((input) => input.readOnly));
  const identityInput = disclosure.querySelector("input");
  let selected = 0;
  identityInput.select = () => { selected += 1; };
  identityInput.dispatch("focus"); identityInput.click();
  assert.equal(selected, 2);
  assert.equal(identityRow.querySelector("strong").textContent, session.nativeTitle);
  assert.equal(identityRow.querySelector("img"), null);
  assert(identityRow.textContent.includes("Binding b-short"));
  const lazyRow = document.createElement("tr");
  sessionIdentityCell(lazyRow, { ...session, nativeThreadId: null, topicId: null, nativeTitle: null, catalogState: "lazy" });
  assert.equal(lazyRow.querySelector("strong").textContent, "Lazy Session");
  assert.equal(lazyRow.querySelectorAll("input").length, 2);

  for (const action of ["activate", "configure", "rename", "archive", "unarchive", "unarchiveCurrent", "deleteLazy", "deleteMaterialized", "stop", "release"]) {
    session.actions[action] = { actionToken: action };
  }
  const actions = document.createElement("div");
  wireSessionActions(actions, session);
  assert.deepEqual(actions.children.filter((child) => child.tagName === "button").map((button) => button.textContent),
    ["配置", "停止", "设为当前", "恢复", "恢复并设为当前"]);
  const more = actions.querySelector("details");
  assert(!more.open);
  assert.deepEqual(more.querySelectorAll("button").map((button) => button.textContent),
    ["创建定时任务", "重命名", "归档", "删除 Lazy", "删除", "释放订阅"]);
  const button = (label) => actions.querySelectorAll("button").find((item) => item.textContent === label);
  button("配置").click();
  assert.deepEqual(editorCall, [session, button("配置")]);
  button("创建定时任务").click();
  assert.deepEqual(editorCall, [session, button("创建定时任务")]);
  button("删除").click(); await flush();
  assert.equal(mutations.length, 0);
  assert.match(confirmations.at(-1), /不可恢复|无法恢复/);
  accepted = true;
  button("删除").click(); await flush();
  assert.equal(mutations.at(-1)[0], "/api/v1/sessions/delete-materialized");
  assert.equal(mutations.at(-1)[1], session.actions.deleteMaterialized);
  button("停止").click(); await flush();
  assert.equal(mutations.at(-1)[1], session.actions.stop);
  more.open = true;
  more.dispatch("keydown", { key: "Escape" });
  assert.equal(more.open, false);
  assert.equal(document.activeElement, more.querySelector("summary"));
  const unprivileged = document.createElement("div");
  wireSessionActions(unprivileged, { ...session, actions: {} });
  assert.deepEqual(unprivileged.querySelectorAll("button").map((item) => item.textContent), ["创建定时任务"]);

  const opener = document.createElement("button");
  opener.isConnected = true;
  document.body.append(opener);
  opener.focus();
  const waiting = deferred();
  respond = () => waiting.promise;
  const opening = showSchedule("first", opener);
  assert.equal(scheduleInput("detail").open, true);
  assert.equal(scheduleInput("detail-content").hidden, true);
  assert.match(scheduleInput("detail-note").textContent, /正在读取/);
  scheduleInput("detail-close").click();
  assert.equal(scheduleInput("detail").open, false);
  assert.equal(document.activeElement, opener);
  waiting.resolve(view("first")); await opening;
  assert.equal(scheduleInput("detail-content").hidden, true, "closed drawer rejects late response");
  assert.equal(requests.length, 1, "closed view does not start runs request");

  respond = async (query) => query.get("mode") === "view" ? view(query.get("plan_id"))
    : runPage(query.get("cursor") || "first-page", query.has("cursor") ? null : "second-cursor");
  await showSchedule("second", opener);
  assert.equal(scheduleInput("detail-title").textContent, "<b>second</b>");
  assert.equal(scheduleInput("detail-title").querySelector("b"), null);
  assert.equal(scheduleInput("detail-instructions").querySelector("img"), null);
  assert.equal(scheduleInput("detail-content").hidden, false);
  assert.equal(scheduleInput("runs-page").textContent, "第 1 页");
  assert.equal(scheduleInput("runs-prev").disabled, true);
  assert.equal(scheduleInput("runs-next").disabled, false);
  const runLink = scheduleInput("runs-body").querySelectorAll("a")[1];
  const linked = new URL(runLink.href, "http://localhost");
  assert.equal(linked.pathname, "/sessions/ordinary");
  assert.equal(linked.searchParams.get("identity"), "binding-target");
  assert.equal(linked.searchParams.get("inventoryState"), "all");
  scheduleInput("runs-next").click(); await flush();
  assert.equal(scheduleRunsPage.number, 2);
  assert.deepEqual(scheduleRunsPage.previousCursors, [null]);
  assert.match(scheduleInput("runs-body").textContent, /second-cursor/);
  assert.equal(scheduleInput("runs-next").disabled, true);
  const oldPage = scheduleRunsPage;
  const oldRows = scheduleInput("runs-body").children;
  respond = async () => { throw new Error("temporary failure"); };
  scheduleInput("runs-prev").click(); await flush();
  assert.equal(scheduleRunsPage, oldPage);
  assert.equal(scheduleInput("runs-body").children, oldRows);
  assert.equal(scheduleInput("runs-page").textContent, "第 2 页");
  assert.match(scheduleInput("runs-note").textContent, /temporary failure/);
  assert.equal(scheduleInput("runs-retry").hidden, false);
  respond = async () => runPage("retry-page-one", "new-next");
  scheduleInput("runs-retry").click(); await flush();
  assert.equal(requests.at(-1).get("cursor"), null);
  assert.equal(scheduleRunsPage.number, 1);
  assert.match(scheduleInput("runs-body").textContent, /retry-page-one/);

  const oldRuns = deferred();
  respond = () => oldRuns.promise;
  const pending = moveScheduleRunsPage("next");
  assert.equal(scheduleInput("runs-next").disabled, true);
  const requestCount = requests.length;
  await moveScheduleRunsPage("next");
  assert.equal(requests.length, requestCount, "busy paging does not create duplicate requests");
  scheduleInput("detail").dispatch("cancel");
  respond = async (query) => query.get("mode") === "view" ? view("second") : { runs: [], next_cursor: null };
  await showSchedule("second", opener);
  oldRuns.resolve(runPage("STALE")); await pending;
  assert.equal(scheduleInput("runs-body").children.length, 0, "reopening exact same plan still rejects old page response");
  assert.equal(scheduleInput("runs-page").textContent, "第 1 页");
  assert.equal(scheduleInput("runs-note").textContent, "暂无触发记录。");

  const staleView = deferred();
  respond = () => staleView.promise;
  const firstView = showSchedule("stale-plan");
  respond = async (query) => query.get("mode") === "view" ? view("new-plan") : { runs: [], next_cursor: null };
  await showSchedule("new-plan");
  staleView.reject(new Error("STALE ERROR")); await firstView;
  assert.equal(scheduleInput("detail-title").textContent, "<b>new-plan</b>");
  assert.equal(scheduleInput("detail-note").textContent, "");
  respond = async () => { throw new Error("view failed"); };
  await showSchedule("error-plan");
  assert.equal(scheduleInput("detail-content").hidden, true);
  assert.equal(scheduleInput("detail-note").textContent, "view failed");
  assert.equal(scheduleInput("detail-retry").hidden, false);
  respond = async (query) => query.get("mode") === "view" ? view("error-plan") : { runs: [], next_cursor: null };
  scheduleInput("detail-retry").click(); await flush();
  assert.equal(scheduleInput("detail-content").hidden, false);
  scheduleInput("detail-done").click();
  assert.equal(scheduleDetailId, null);
  assert.equal(scheduleInput("detail").open, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
