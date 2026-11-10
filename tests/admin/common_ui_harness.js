window.location = { pathname: "/projects", search: "", assign() {} };
window.NetizenIdentity = { loadProfile: async () => {}, expire() {} };
confirmResponder = null;
const windowEvents = new Map();
window.addEventListener = (event, listener) => {
  if (!windowEvents.has(event)) windowEvents.set(event, []);
  windowEvents.get(event).push(listener);
};
const timers = new Map(); let timerSequence = 0;
const setTimeout = (callback, delay) => { const id = ++timerSequence; timers.set(id, { callback, delay }); return id; };
const clearTimeout = (id) => timers.delete(id);
let failed = false;
let posts = 0;
let finishPost;
let projects = [{ alias: "demo", cwd: "/demo", enabled: true, actions: {}, bindingCount: 0 }];
async function fetch(path, options = {}) {
  if (options.method === "POST") { posts += 1; await new Promise((resolve) => { finishPost = resolve; }); }
  else if (failed) throw new Error("读取失败");
  return { ok: true, status: 200, json: async () => path.startsWith("/api/v1/projects?")
    ? { items: projects, nextCursor: null, actions: {} } : { message: "操作成功。" } };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
// SHIPPED_COMMON_UI
(async () => {
  await flush();
  // A failed identity assertion is deliberately bounded even on attached,
  // cyclic DOM nodes; it must not serialize the entire page into an error diff.
  assert.throws(() => assert.equal(document.body, document.querySelector("#projects")),
    (error) => error.message.startsWith("DOM identity mismatch") && error.message.length < 180);
  const ask = window.NetizenConfirm;
  const dialog = document.querySelector("#admin-confirm");
  const cancel = document.querySelector("#admin-confirm-cancel");
  const submit = document.querySelector("#admin-confirm-submit");
  const trigger = actionButton("删除", async () => {
    if (await ask.ask({ title: "删除", body: "永久删除，无法恢复。", details: ["<b>原样显示</b>"], danger: true })) {
      await mutate("/api/v1/example/delete", { actionToken: "exact-once" });
    }
  }, true);
  document.body.append(trigger); trigger.focus();
  for (const dismiss of [() => cancel.click(), () => dialog.dispatch("cancel"),
    () => dialog.dispatch("click", { clientX: 0, clientY: 0 }), () => dialog.querySelector(".drawer-close").click()]) {
    trigger.click();
    assert.equal(dialog.open, true);
    assert.equal(document.activeElement, cancel, "confirmation defaults to cancel");
    assert.equal(trigger.disabled, true);
    assert.equal(dialog.querySelector("b"), null, "confirmation content is inert text");
    trigger.click();
    assert.equal(await ask.ask({ body: "different action" }), false, "another action cannot share consent");
    dismiss(); await flush();
    assert.equal(dialog.open, false);
    assert.equal(posts, 0, "all cancellation paths make no mutation");
    assert.equal(document.activeElement, trigger);
    assert.equal(trigger.disabled, false);
  }
  trigger.click(); submit.click(); await flush();
  assert.equal(posts, 1);
  assert.equal(trigger.disabled, true, "trigger remains disabled until POST returns");
  trigger.click(); await flush(); assert.equal(posts, 1);
  finishPost(); await flush(); assert.equal(trigger.disabled, false);
  const successTimer = timers.get(statusTimer); assert(successTimer && successTimer.delay >= 3000);
  successTimer.callback(); assert.equal(statusNode.hidden, true);
  setStatus("结果未确认，请刷新核对。", true);
  assert.equal(statusTimer, null);
  await refresh("projects"); assert.equal(statusNode.hidden, false, "reads do not dismiss an operation result");
  statusNode.querySelector("button").click(); assert.equal(statusNode.hidden, true);
  setStatus("正在执行…"); assert.equal(statusTimer, null);

  const row = document.createElement("div"); document.body.append(row);
  let invoked = 0;
  const first = actionButton("查看", () => { invoked += 1; });
  const blocked = actionButton("不可用", () => { throw new Error("disabled action"); }); blocked.disabled = true;
  const last = actionButton("删除", () => { invoked += 1; }, true);
  row.append(window.NetizenMenu.create([last, first, blocked]));
  const more = row.querySelector("button"); more.focus(); more.click();
  const menu = document.querySelector(".row-menu-popover");
  assert.equal(more.getAttribute("aria-expanded"), "true");
  assert.equal(menu.parentElement, document.body, "menu is outside the table row");
  assert.equal(document.activeElement, first);
  assert.equal(menu.querySelectorAll("button").at(-1), last, "danger is last");
  assert(menu.querySelectorAll("div").some((node) => node.getAttribute("role") === "separator"));
  first.dispatch("keydown", { key: "ArrowDown" }); assert.equal(document.activeElement, last);
  last.dispatch("keydown", { key: "ArrowDown" }); assert.equal(document.activeElement, first);
  first.dispatch("keydown", { key: "End" }); assert.equal(document.activeElement, last);
  last.dispatch("keydown", { key: "Escape" }); assert(menu.hidden); assert.equal(document.activeElement, more);
  more.dispatch("keydown", { key: "ArrowUp" }); assert.equal(document.activeElement, last);
  last.dispatch("keydown", { key: "Enter" }); await flush(); assert.equal(invoked, 1); assert(menu.hidden);
  more.click(); first.dispatch("keydown", { key: " " }); await flush(); assert.equal(invoked, 2);
  more.click(); document.body.click(); assert(menu.hidden);
  for (const event of ["resize", "scroll"]) {
    more.click(); for (const listener of windowEvents.get(event)) listener({ target: document }); assert(menu.hidden);
  }

  const body = document.querySelector("#projects-body");
  const previousRow = body.children[0];
  const freshness = document.querySelector("#projects-refreshed-at").dateTime;
  failed = true; assert.equal(await refresh("projects"), false);
  const error = document.querySelector("#projects-error"); assert.equal(error.hidden, false);
  assert.equal(body.children[0], previousRow); assert.equal(document.querySelector("#projects-refreshed-at").dateTime, freshness);
  failed = false; projects = []; error.querySelector("button").click(); await flush();
  assert.equal(error.hidden, true); assert(body.querySelector(".list-empty"));
  failed = true; await refresh("projects"); assert(body.querySelector(".list-empty").hidden, "errors never claim an empty result");
  expireAdminSession(); assert.equal(document.querySelector("#auth-expired").hidden, false); assert(statusNode.hidden);
})().catch((error) => { console.error(error); process.exitCode = 1; });
