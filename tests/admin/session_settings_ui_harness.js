const state = { tab: "sessions", sessions: null, sessionPage: {
  cursor: "page-two", nextCursor: "page-three", previousCursors: [null], number: 2,
  query: "project=netizen&scopeKind=group&inventoryState=active&inventoryState=lazy&pageSize=20",
} };
Object.defineProperty(document.querySelector("#sessions-body"), "rows", {
  get() { return this.querySelectorAll("tr"); },
});
let status = "";
let statusError = false;
function setStatus(message, error = false) { status = message; statusError = error; }
window.prompt = window.confirm = () => { throw new Error("Configuration must use the drawer"); };
const baseSettings = {
  turn_settings: { model_id: "model-one", effort_id: "high", service_tier_id: "priority" },
  reaction_pulse_enabled: true, progress_card_enabled: false, completion_mention_enabled: true,
  message_context_mode: "catch-up",
};
const catalog = [
  { id: "model-one", display_name: "<b>Model one</b>", default_effort_id: "medium",
    default_service_tier_id: "default", is_default: true,
    efforts: [{ id: "medium" }, { id: "high" }],
    service_tiers: [{ id: "default", name: "Standard" }, { id: "priority", name: "Fast" }] },
  { id: "model-two", display_name: "Model two", default_effort_id: "low",
    default_service_tier_id: "priority",
    efforts: [{ id: "low" }, { id: "high" }],
    service_tiers: [{ id: "default", name: "Standard" }, { id: "priority", name: "Fast" }] },
];
let models = structuredClone(catalog);
let catalogError = null;
let optionsFailure = false;
let listFailure = false;
let postFailure = false;
let optionsGate = null;
let postGate = null;
let generation = 0;
const gets = [];
const posts = [];
const grant = (id) => ({
  csrfToken: id + "-csrf-" + generation, actionToken: id + "-once-" + generation,
  target: { resource: "binding", targetId: id, scopeKey: "cli_test:group:oc_group" },
});
const fixtureSession = (id, settings = baseSettings, extra = {}) => ({
  bindingId: id, shortId: id.slice(-8), projectAlias: "netizen",
  scopeKey: "cli_test:group:oc_group", scopeKind: "group", chatMode: "group",
  sessionType: "message", chatId: "oc_group", topicId: null,
  chatOpenUrl: "https://applink.feishu.cn/client/chat/open?openChatId=oc_group",
  nativeThreadId: "native-" + id, nativeTitle: "<b>Exact Session</b>",
  pointerState: "current", catalogState: "active", updatedAt: 1900000000,
  runtime: { primaryStatus: "idle", primaryStatusResolution: "resolved" },
  sessionSettings: structuredClone(settings), ...extra,
});
let serverSessions = [fixtureSession("binding-exact")];
function paused(gate, result) {
  return new Promise((resolve, reject) => {
    gate.resolve = () => resolve(result);
    gate.reject = (message) => reject(new Error(message));
  });
}
async function api(path, options = {}) {
  if (options.method === "POST") {
    assert.equal(path, "/api/v1/sessions/configure");
    assert.equal(options.headers["Content-Type"], "application/json");
    const body = JSON.parse(options.body);
    posts.push({ path, body });
    if (postGate) return paused(postGate, { message: "已保存 exact 会话配置" });
    if (postFailure) throw new Error("配置已变化");
    serverSessions.find((session) => session.bindingId === body.target.targetId).sessionSettings =
      structuredClone(body.sessionSettings);
    return { message: "已保存 exact 会话配置" };
  }
  gets.push(path);
  if (path === "/api/v1/sessions/options") {
    if (optionsFailure) throw new Error("目录读取失败");
    const result = { models: catalogError ? [] : structuredClone(models), model_catalog_error: catalogError };
    return optionsGate ? paused(optionsGate, result) : result;
  }
  assert(path.startsWith("/api/v1/sessions?"), "Unexpected endpoint: " + path);
  if (listFailure) throw new Error("Sessions 暂时不可用");
  generation += 1;
  return { items: serverSessions.map((session) => ({
    ...structuredClone(session), actions: { configure: grant(session.bindingId) },
  })), nextCursor: "page-three", catalogAvailable: true };
}
async function loadSessionProjectOptions() {}
const flush = () => new Promise((resolve) => setImmediate(resolve));
// SHIPPED_SESSION_SETTINGS_CONTROLLER
const field = sessionSettingsInput;
function configureButton(id = "binding-exact") {
  return rowByIdentity("#sessions-body", "bindingId", id).querySelectorAll("button")
    .find((button) => button.textContent === "配置");
}
async function openFromList(id = "binding-exact") {
  const button = configureButton(id);
  button.focus();
  button.click();
  await flush();
  return button;
}
function change(name, value) {
  if (["reactions", "progress", "completion-mention"].includes(name)) field(name).checked = value;
  else field(name).value = value;
  field(name).dispatch("change");
}
async function submit() {
  const event = field("editor").dispatch("submit");
  assert(event.prevented);
  await flush();
}
function assertPageRetained(page) {
  assert.deepEqual(state.sessionPage, page);
  const query = new URL(gets.filter((path) => path.startsWith("/api/v1/sessions?")).at(-1), "http://admin").searchParams;
  assert.equal(query.get("cursor"), page.cursor);
  assert.deepEqual(query.getAll("inventoryState"), ["active", "lazy"]);
  assert.equal(query.get("project"), "netizen");
}
const cases = {
  async entry_and_complete_save() {
    await loadSessions();
    assert.match(document.querySelector("#sessions-body").textContent,
      /catch-up · model-one \/ high \/ priority/);
    const page = structuredClone(state.sessionPage);
    const session = state.sessions.items[0];
    const action = structuredClone(session.actions.configure);
    await openFromList();
    assert(field("drawer").open);
    assert.equal(field("drawer").getAttribute("aria-labelledby"), "session-settings-title");
    assert.equal(field("drawer").getAttribute("aria-describedby"), "session-settings-identity");
    assert.equal(field("editor").hidden, false);
    assert.equal(document.activeElement, field("model"));
    assert.match(field("identity").textContent, /<b>Exact Session<\/b>/);
    assert.equal(field("identity").querySelector("b"), null);
    assert.equal(field("model").querySelector("b"), null);
    assert.deepEqual(sessionSettingsEditor.settings, baseSettings);
    assert.equal(field("model").value, "model-one");
    assert.equal(field("effort").value, "high");
    assert.equal(field("tier").value, "priority");
    assert.equal(field("reactions").checked, true);
    assert.equal(field("progress").checked, false);
    assert.equal(field("completion-mention").checked, true);
    assert.equal(field("context").value, "catch-up");
    assert.deepEqual(gets.filter((path) => path.includes("/options")), ["/api/v1/sessions/options"]);
    change("model", "model-two");
    assert.deepEqual(sessionSettingsEditor.settings.turn_settings,
      { model_id: "model-two", effort_id: "low", service_tier_id: "priority" });
    change("effort", "high");
    change("tier", "default");
    change("reactions", false);
    change("progress", true);
    change("completion-mention", false);
    change("context", "current-only");
    assert.deepEqual(session.sessionSettings, baseSettings);
    await submit();
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0].body, actionPayload(action, { sessionSettings: {
      turn_settings: { model_id: "model-two", effort_id: "high", service_tier_id: "default" },
      reaction_pulse_enabled: false, progress_card_enabled: true, completion_mention_enabled: false,
      message_context_mode: "current-only",
    } }));
    assert.equal(field("drawer").open, false);
    assert.equal(field("editor").hidden, true);
    assert.equal(sessionSettingsEditor, null);
    assert.equal(document.activeElement, configureButton());
    assert.match(status, /已保存/);
    assert.equal(statusError, false);
    assert.match(document.querySelector("#sessions-body").textContent,
      /current-only · model-two \/ high \/ default/);
    assertPageRetained(page);
  },
  async inheritance_and_explicit_defaults() {
    serverSessions[0].sessionSettings.turn_settings = null;
    serverSessions[0].sessionSettings.message_context_mode = "current-only";
    await loadSessions();
    assert.match(document.querySelector("#sessions-body").textContent, /current-only · 继承 Codex/);
    await openFromList();
    assert.equal(field("model").value, "");
    assert(field("effort").disabled);
    assert(field("tier").disabled);
    assert.equal(sessionSettingsEditor.settings.turn_settings, null);
    await submit();
    assert.equal(posts[0].body.sessionSettings.turn_settings, null);
    await openFromList();
    change("model", "model-one");
    assert.equal(field("effort").value, "medium");
    assert.equal(field("tier").value, "default");
    await submit();
    assert.deepEqual(posts[1].body.sessionSettings.turn_settings,
      { model_id: "model-one", effort_id: "medium", service_tier_id: "default" });
    await openFromList();
    change("model", "");
    assert.equal(sessionSettingsEditor.settings.turn_settings, null);
    assert(field("effort").disabled);
    assert(field("tier").disabled);
    await submit();
    assert.equal(posts[2].body.sessionSettings.turn_settings, null);
  },
  async context_and_private_chat() {
    serverSessions[0].sessionSettings.message_context_mode = "current-only";
    await loadSessions();
    await openFromList();
    assert.equal(field("context-field").hidden, false);
    assert(field("context").querySelectorAll("option").find((option) => option.value === "catch-up").disabled);
    assert.match(field("context-note").textContent, /飞书 \/config/);
    change("context", "catch-up");
    assert.equal(sessionSettingsEditor.settings.message_context_mode, "current-only");
    assert.equal(field("context").value, "current-only");
    field("cancel").click();
    serverSessions[0].sessionSettings.message_context_mode = "catch-up";
    await loadSessions();
    await openFromList();
    assert.equal(field("context").querySelectorAll("option").find((option) => option.value === "catch-up").disabled, false);
    change("context", "current-only");
    change("context", "catch-up");
    await submit();
    assert.equal(posts.at(-1).body.sessionSettings.message_context_mode, "catch-up");
    await openFromList();
    change("context", "current-only");
    await submit();
    assert.equal(posts.at(-1).body.sessionSettings.message_context_mode, "current-only");
    for (const extra of [{ scopeKind: "direct", chatMode: null }, { scopeKind: "group", chatMode: "p2p" }]) {
      serverSessions = [fixtureSession("binding-exact", { ...baseSettings, message_context_mode: "current-only" }, extra)];
      await loadSessions();
      await openFromList();
      assert(field("context-field").hidden);
      assert(field("context-note").hidden);
      assert(field("context").disabled);
      change("context", "catch-up");
      await submit();
      assert.equal(posts.at(-1).body.sessionSettings.message_context_mode, "current-only");
    }
  },
  async unavailable_catalog() {
    const retired = { model_id: "retired-model", effort_id: "retired-effort", service_tier_id: "retired-tier" };
    serverSessions[0].sessionSettings.turn_settings = retired;
    catalogError = "模型目录暂时不可用";
    await loadSessions();
    await openFromList();
    assert.equal(field("model").value, retired.model_id);
    assert.equal(field("effort").value, retired.effort_id);
    assert.equal(field("tier").value, retired.service_tier_id);
    assert(field("effort").disabled);
    assert.equal(field("save").disabled, false);
    assert.match(field("note").textContent, /模型目录暂时不可用/);
    change("progress", true);
    await submit();
    assert.deepEqual(posts.at(-1).body.sessionSettings.turn_settings, retired);
    assert.equal(posts.at(-1).body.sessionSettings.progress_card_enabled, true);
    await openFromList();
    change("model", "");
    await submit();
    assert.equal(posts.at(-1).body.sessionSettings.turn_settings, null);
    serverSessions[0].sessionSettings.turn_settings = retired;
    catalogError = null;
    optionsFailure = true;
    await loadSessions();
    await openFromList();
    assert.deepEqual(sessionSettingsEditor.settings.turn_settings, retired);
    assert.match(field("note").textContent, /目录读取失败/);
    assert.equal(field("save").disabled, false);
    change("reactions", false);
    await submit();
    assert.deepEqual(posts.at(-1).body.sessionSettings.turn_settings, retired);
    assert.equal(posts.at(-1).body.sessionSettings.reaction_pulse_enabled, false);
    optionsFailure = false;
    await openFromList();
    assert.equal(field("model").value, retired.model_id);
    change("model", "model-two");
    await submit();
    assert.deepEqual(posts.at(-1).body.sessionSettings.turn_settings,
      { model_id: "model-two", effort_id: "low", service_tier_id: "priority" });
  },
  async duplicate_save_and_failure() {
    await loadSessions();
    const page = structuredClone(state.sessionPage);
    const button = await openFromList();
    change("progress", true);
    const draft = structuredClone(sessionSettingsEditor.settings);
    const action = structuredClone(sessionSettingsEditor.action);
    const gate = {};
    postGate = gate;
    await submit();
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0].body, actionPayload(action, { sessionSettings: draft }));
    assert(field("fields").disabled);
    assert(field("close").disabled);
    assert(field("cancel").disabled);
    assert(field("save").disabled);
    const escaped = field("drawer").dispatch("cancel", { key: "Escape" });
    assert(escaped.prevented);
    field("close").click();
    field("cancel").click();
    assert(field("drawer").open);
    change("progress", false);
    assert.deepEqual(sessionSettingsEditor.settings, draft);
    await submit();
    assert.equal(posts.length, 1);
    gate.reject("保存结果未确认");
    postGate = null;
    await flush();
    assert(field("drawer").open);
    assert.deepEqual(sessionSettingsEditor.settings, draft);
    assert.equal(field("progress").checked, true);
    assert.equal(field("fields").disabled, false);
    assert.equal(sessionSettingsEditor.action, null);
    assert.equal(state.sessions.items[0].actions.configure, null);
    assert(field("save").disabled);
    assert(button.disabled);
    assert.match(field("note").textContent, /保存结果未确认.*重新打开/);
    assert.match(status, /保存结果未确认/);
    assert.equal(statusError, true);
    await submit();
    assert.equal(posts.length, 1);
    field("cancel").click();
    assert.equal(document.activeElement, document.querySelector("#sessions [data-refresh]"));
    button.click();
    assert.equal(sessionSettingsEditor, null);
    assertPageRetained(page);
    await loadSessions();
    await openFromList();
    assert.notEqual(sessionSettingsEditor.action.actionToken, action.actionToken);
    postFailure = true;
    await submit();
    assert.match(field("note").textContent, /配置已变化/);
    assert(field("save").disabled);
  },
  async cancel_and_stale_options() {
    serverSessions.push(fixtureSession("binding-second", {
      ...baseSettings, turn_settings: { model_id: "model-two", effort_id: "high", service_tier_id: "default" },
    }));
    await loadSessions();
    const original = structuredClone(state.sessions.items[0].actions.configure);
    const oldGate = {};
    optionsGate = oldGate;
    const button = await openFromList();
    assert(field("fields").disabled);
    assert(field("save").disabled);
    assert.equal(document.activeElement, field("close"));
    await submit();
    assert.equal(posts.length, 0);
    const cancel = field("drawer").dispatch("cancel", { key: "Escape" });
    assert(cancel.prevented);
    assert.equal(field("drawer").open, false);
    assert.equal(document.activeElement, button);
    assert.deepEqual(state.sessions.items[0].actions.configure, original);
    optionsGate = null;
    models = [catalog[1]];
    await openFromList("binding-second");
    const currentEditor = sessionSettingsEditor;
    const draft = structuredClone(currentEditor.settings);
    assert.equal(field("model").value, "model-two");
    oldGate.resolve();
    await flush();
    assert.equal(sessionSettingsEditor, currentEditor);
    assert.deepEqual(currentEditor.settings, draft);
    assert.deepEqual(currentEditor.models, [catalog[1]]);
    assert.equal(field("model").value, "model-two");
    assert.equal(document.activeElement, field("model"));
    field("close").click();
    assert.equal(field("drawer").open, false);
    assert.equal(document.activeElement, configureButton("binding-second"));
    await openFromList();
    change("reactions", false);
    field("cancel").click();
    await openFromList();
    assert.equal(field("reactions").checked, baseSettings.reaction_pulse_enabled);
    assert.equal(posts.length, 0);
    field("cancel").click();
    const closedGate = {};
    optionsGate = closedGate;
    await openFromList();
    field("cancel").click();
    closedGate.reject("过期目录失败");
    await flush();
    assert.equal(sessionSettingsEditor, null);
    assert.equal(field("editor").hidden, true);
    assert.equal(document.activeElement, configureButton());
  },
  async saved_refresh_failure() {
    await loadSessions();
    const page = structuredClone(state.sessionPage);
    const button = await openFromList();
    change("completion-mention", false);
    listFailure = true;
    await submit();
    assert.equal(posts.length, 1);
    assert.equal(posts[0].body.sessionSettings.completion_mention_enabled, false);
    assert.equal(field("drawer").open, false);
    assert.equal(sessionSettingsEditor, null);
    assert.match(status, /已保存.*刷新失败/);
    assert.equal(statusError, true);
    assert(button.disabled);
    assertPageRetained(page);
    await submit();
    assert.equal(posts.length, 1);
    listFailure = false;
    assert(await refresh("sessions"));
    assertPageRetained(page);
    await openFromList();
    assert.equal(field("completion-mention").checked, false);
  },
};
(async () => {
  const name = process.argv[2];
  assert.equal(typeof cases[name], "function", "Unknown case: " + name);
  await cases[name]();
})().catch((error) => { console.error(error); process.exitCode = 1; });
