let confirm = true;
window.location = { pathname: "/projects", search: "", assign() {} };
window.confirm = () => confirm;
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, suspendSelector() {}, expire() {} };
const timers = new Map();
let nextTimer = 0;
function setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; }
function clearTimeout(id) { timers.delete(id); }
let stored = false;
let revision = 1;
let editable = true;
let failure = false;
let gate = null;
const requests = [];
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
const response = (data, status = 200) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
async function fetch(path, options = {}) {
  if (path === "/api/v1/projects") return response({ items: [], nextCursor: null });
  assert.equal(path, "/api/v1/question-notifications/settings");
  requests.push({ path, options });
  const readValue = stored;
  if (gate) await gate.promise;
  if (failure) return response({ message: "配置已变化，请刷新" }, 409);
  if (options.method === "POST") { stored = JSON.parse(options.body).settings.mentionInitiator; revision += 1; }
  return response({ settings: { mentionInitiator: options.method === "POST" ? stored : readValue },
    revision, editable, actions: { save: editable ? { actionToken: `save-${revision}`, csrfToken: "csrf",
      target: { resource: "question-notifications-settings", targetId: "instance" } } : null } });
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
// SHIPPED_QUESTION_NOTIFICATIONS_CONTROLLER
const field = (name) => document.querySelector(`#question-notifications-${name}`);
const controller = window.NetizenQuestionNotifications;
const posts = () => requests.filter((request) => request.options.method === "POST");
function change(value) { field("enabled").checked = value; field("enabled").dispatch("change"); }
async function submit() { field("form").dispatch("submit"); await flush(); }
const cases = {
  async save_discard() {
    await controller.load();
    assert.equal(field("enabled").checked, false);
    change(true);
    assert.equal(controller.dirty(), true);
    assert.equal(field("unsaved").hidden, false);
    field("cancel").click();
    assert.equal(field("enabled").checked, false);
    change(true);
    gate = deferred();
    await submit(); await submit();
    assert.equal(posts().length, 1);
    assert.equal(controller.busy(), true);
    gate.resolve(); gate = null;
    await flush();
    const body = JSON.parse(posts()[0].options.body);
    assert.deepEqual(body.settings, { mentionInitiator: true });
    assert.equal(body.actionToken, "save-1");
    assert.equal(controller.dirty(), false);
  },
  async readonly() {
    editable = false;
    await controller.load();
    assert.equal(field("fields").disabled, true);
    change(true); await submit();
    assert.equal(controller.dirty(), false);
    assert.equal(posts().length, 0);
  },
  async conflict() {
    await controller.load(); change(true); failure = true;
    await submit(); await submit();
    assert.equal(posts().length, 1);
    assert.equal(controller.dirty(), true);
    assert.equal(field("save").disabled, true);
    failure = false; confirm = false;
    assert.equal(await controller.load(), false);
    confirm = true; await controller.load();
    assert.equal(controller.dirty(), false);
  },
  async expiry() {
    await controller.load(); change(true); gate = deferred();
    await submit(); controller.expire(); gate.resolve(); gate = null;
    await flush();
    assert.equal(field("fields").disabled, true);
    assert.equal(controller.dirty(), true);
    assert.equal(await controller.load(), false);
  },
  async late_refresh() {
    await controller.load(); gate = deferred();
    const loading = controller.load(); controller.discard(); change(true);
    gate.resolve(); gate = null;
    assert.equal(await loading, false);
    assert.equal(field("enabled").checked, true);
    assert.equal(controller.dirty(), true);
  },
  async navigation() {
    await selectTab("system");
    selectSystemTab("notifications");
    assert.equal(document.querySelector("#system-notifications").hidden, false);
    change(true); confirm = false;
    await selectTab("projects");
    assert.equal(state.tab, "system");
    confirm = true; await selectTab("projects");
    assert.equal(state.tab, "projects");
    assert.equal(controller.dirty(), false);
  },
};
cases[process.argv[2]]().catch((error) => { console.error(error); process.exitCode = 1; });
