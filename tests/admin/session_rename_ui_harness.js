window.location = { pathname: "/sessions/ordinary", search: "", assign() {} };
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, expire() {} };
const sessionStorage = { getItem() {}, setItem() {}, removeItem() {} };
window.prompt = () => { throw new Error("Rename must use a dialog"); };
window.confirm = () => true;
Object.defineProperty(document.querySelector("#sessions-body"), "rows", {
  get() { return this.querySelectorAll("tr"); },
});
const posts = [];
const gets = [];
const expiredTokens = new Set();
let serial = 0;
let generateGate = null;
let saveGate = null;
let generateFailure = false;
let saveFailure = false;
let changedTarget = false;
let generatedName = "<b>Generated candidate</b>";
const grant = (kind) => ({ actionKind: `sessions.${kind}`, actionToken: `${kind}-${serial}`,
  csrfToken: `csrf-${serial}`, target: { resource: "binding", targetId: "binding-exact", scopeKey: "scope-exact" } });
const fixtureSession = () => ({
  bindingId: "binding-exact", shortId: "exact", scopeKey: "scope-exact", scopeKind: "group", chatMode: "group",
  projectAlias: "netizen", chatId: "oc_group", chatName: "Group", sessionType: "message", topicId: null,
  nativeThreadId: changedTarget ? "different-native" : "native-exact", nativeTitle: "Original name",
  pointerState: "current", catalogState: "active", runtime: { primaryStatus: "idle" },
  sessionSettings: { turn_settings: null, message_context_mode: "current-only", reaction_pulse_enabled: true,
    progress_card_enabled: true, completion_mention_enabled: true, progress_card_settings: null },
  actions: { rename: grant("rename"), generateName: grant("generate-name") },
});
const paused = (gate, result) => new Promise((resolve, reject) => {
  gate.resolve = () => resolve(result);
  gate.reject = (message) => reject(new Error(message));
});
async function fetch(path, options = {}) {
  try {
    let data;
    if (options.method === "POST") {
      const body = JSON.parse(options.body);
      posts.push({ path, body });
      if (expiredTokens.has(body.actionToken)) {
        throw Object.assign(new Error("操作已过期或已执行，请刷新页面后重试。"), { status: 409 });
      }
      if (path === "/api/v1/sessions/generate-name") {
        if (generateFailure) throw new Error("名称生成失败，请重试。");
        data = { bindingId: "binding-exact", name: generatedName };
        if (generateGate) data = await paused(generateGate, data);
      } else {
        assert.equal(path, "/api/v1/sessions/rename");
        if (saveFailure) throw new Error("保存结果未确认");
        data = { bindingId: "binding-exact", name: body.name };
        if (saveGate) data = await paused(saveGate, data);
      }
    } else {
      gets.push(path);
      if (path.startsWith("/api/v1/projects/options?")) data = { items: [], nextCursor: null };
      else {
        assert(path.startsWith("/api/v1/sessions?"), "Unexpected GET: " + path);
        serial += 1;
        data = { items: [fixtureSession()], nextCursor: null };
      }
    }
    return { ok: true, status: 200, json: async () => data };
  } catch (error) {
    return { ok: false, status: error.status || 400, json: async () => ({ message: error.message }) };
  }
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
// SHIPPED_RENAME_CONTROLLER
const field = sessionRenameInput;
function open() {
  const button = rowByIdentity("#sessions-body", "bindingId", "binding-exact").querySelectorAll("button")
    .find((node) => node.textContent === "重命名");
  button.focus(); button.click();
  assert(field("dialog").open);
  return button;
}
function change(value) { field("name").value = value; field("name").dispatch("input"); }
function submit() { assert(field("editor").dispatch("submit").prevented); }
const cases = {
  async generate_edit_save() {
    const page = structuredClone(state.sessionPage);
    open();
    assert.equal(document.activeElement, field("name"));
    assert.equal(field("name").value, "Original name");
    change("discarded draft");
    generateGate = {};
    field("generate").click(); field("generate").click(); submit();
    assert.equal(posts.length, 1);
    assert.equal(field("name").value, "");
    assert.match(field("status").textContent, /正在生成中/);
    for (const name of ["name", "generate", "save"]) assert(field(name).disabled);
    generateGate.resolve(); await flush();
    assert.equal(field("name").value, generatedName);
    assert.equal(field("name").querySelector("b"), null);
    assert.equal(posts.filter((post) => post.path.endsWith("/rename")).length, 0);
    for (const name of ["name", "generate", "save"]) assert(!field(name).disabled);
    const firstToken = posts[0].body.actionToken;
    generateGate = null;
    field("generate").click(); await flush();
    assert.equal(posts.length, 2);
    assert.notEqual(posts[1].body.actionToken, firstToken);
    const retryQuery = new URL(gets.at(-1), "http://admin").searchParams;
    assert.equal(retryQuery.get("identity"), "binding-exact");
    assert.deepEqual(state.sessionPage, page);
    change("Edited candidate");
    saveGate = {};
    submit(); submit();
    assert.equal(posts.length, 3);
    assert.equal(posts.at(-1).body.name, "Edited candidate");
    assert.match(posts.at(-1).body.actionToken, /^rename-/);
    field("close").click(); assert(field("dialog").open);
    saveGate.resolve(); await flush();
    assert(!field("dialog").open);
    assert.equal(sessionRenameEditor, null);
    assert.match(statusNode.textContent, /已保存/);
    assert.deepEqual(state.sessionPage, page);
  },
  async failure_and_retry() {
    open(); change("draft"); generateFailure = true;
    field("generate").click(); await flush();
    assert.equal(field("name").value, "");
    assert.match(field("status").textContent, /名称生成失败/);
    assert(!field("generate").disabled);
    assert(field("save").disabled);
    assert.equal(posts.length, 1);
    generateFailure = false;
    field("generate").click(); await flush();
    assert.equal(field("name").value, generatedName);
    assert.equal(posts.length, 2);
    assert.notEqual(posts[0].body.actionToken, posts[1].body.actionToken);
  },
  async expired_grants_refresh_together() {
    open();
    const oldGeneration = grant("generate-name").actionToken;
    const oldRename = grant("rename").actionToken;
    expiredTokens.add(oldGeneration); expiredTokens.add(oldRename);
    field("generate").click(); await flush();
    assert.equal(posts.length, 1);
    assert.equal(posts[0].body.actionToken, oldGeneration);
    assert.match(field("status").textContent, /操作已过期/);
    field("generate").click(); await flush();
    assert.equal(posts.length, 2);
    assert.notEqual(posts[1].body.actionToken, oldGeneration);
    assert.equal(field("name").value, generatedName);
    const refreshedRename = grant("rename").actionToken;
    submit(); await flush();
    assert.equal(posts.length, 3);
    assert.equal(posts[2].body.actionToken, refreshedRename);
    assert.notEqual(posts[2].body.actionToken, oldRename);
    assert(!field("dialog").open);
    assert.match(statusNode.textContent, /已保存/);
  },
  async close_and_late_response() {
    open(); generateGate = {};
    field("generate").click();
    field("cancel").click();
    assert(!field("dialog").open);
    open(); change("new draft");
    generateGate.resolve(); await flush();
    assert.equal(field("name").value, "new draft");
    assert.equal(field("status").textContent, "");
    assert.equal(posts.length, 1);
    field("dialog").dispatch("cancel");
    assert(!field("dialog").open);
  },
  async save_failure() {
    open(); change("manual name"); saveFailure = true;
    submit(); await flush();
    assert(field("dialog").open);
    assert.equal(field("name").value, "manual name");
    assert.match(field("status").textContent, /刷新 Sessions 后核对/);
    field("generate").click();
    submit(); assert.equal(posts.length, 1);
    field("close").click(); assert(!field("dialog").open);
  },
  async expired_and_changed_target() {
    open(); generateFailure = true;
    field("generate").click(); await flush();
    changedTarget = true; generateFailure = false;
    field("generate").click(); await flush();
    assert.equal(posts.length, 1);
    assert.equal(field("name").value, "");
    assert.match(field("status").textContent, /会话状态已变化/);
    changedTarget = false; generateGate = {};
    field("generate").click(); await flush();
    expireAdminSession();
    generateGate.resolve(); await flush();
    assert(!field("dialog").open);
    assert.equal(sessionRenameEditor, null);
    assert.equal(field("name").value, "");
  },
  async navigation_closes_generation() {
    open(); generateGate = {};
    field("generate").click();
    assert(confirmDiscardEditors());
    assert(!field("dialog").open);
    generateGate.reject("late failure"); await flush();
    assert.equal(sessionRenameEditor, null);
    assert.equal(field("name").value, "");
  },
};
(async () => {
  await flush();
  await loadSessions();
  await cases[process.argv[2]]();
})().catch((error) => { console.error(error); process.exitCode = 1; });
