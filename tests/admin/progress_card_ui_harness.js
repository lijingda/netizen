let confirm = true;
window.location = { pathname: "/projects", search: "", assign() {} };
window.confirm = () => confirm;
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, expire() {} };
let nextTimer = 0;
const timers = new Map();
function setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; }
function clearTimeout(id) { timers.delete(id); }
const initialSettings = { show_commentary: true, show_operations: true, show_plan: true,
  commentary_limit: 7, operation_limit: 15, commentary_max_chars: 280, operation_max_chars: 210,
  organization: "time" };
const grant = (revision) => ({ actionToken: `save-${revision}`, csrfToken: "csrf", target: {
  resource: "progress-card-settings", targetId: "instance", revision } });
let revision = 1;
let stored = structuredClone(initialSettings);
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
  assert.equal(path, "/api/v1/progress-card/settings");
  requests.push({ path, options });
  const readSettings = structuredClone(stored);
  if (gate) await gate.promise;
  if (failure) return response({ message: "配置已变化，请刷新" }, 409);
  if (options.method === "POST") {
    stored = structuredClone(JSON.parse(options.body).settings);
    revision += 1;
  }
  return response({ settings: options.method === "POST" ? structuredClone(stored) : readSettings,
    revision, editable, actions: { save: editable ? grant(revision) : null } });
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
// SHIPPED_PROGRESS_CARD_CONTROLLER
const field = (name) => document.querySelector(`#global-card-${name}`);
const controller = window.NetizenProgressCard;
const posts = () => requests.filter((request) => request.options.method === "POST");
function change(name, value) {
  if (name.startsWith("show-")) field(`progress-${name}`).checked = value;
  else field(`progress-${name}`).value = value;
  field(`progress-${name}`).dispatch("change");
}
async function submit() { field("form").dispatch("submit"); await flush(); }
const cases = {
  async save_discard() {
    await controller.load();
    assert.equal(field("progress"), null, "global settings do not add a total progress-card switch");
    assert.equal(field("progress-commentary-limit").value, 7);
    change("commentary-limit", "9");
    assert.equal(field("save").disabled, true);
    await submit();
    assert.equal(posts().length, 0);
    change("commentary-limit", "8");
    change("show-operations", false);
    assert.equal(field("progress-operation-capacity").hidden, true);
    assert.equal(field("progress-operation-limit").disabled, true);
    assert.equal(controller.dirty(), true);
    assert.equal(field("unsaved").hidden, false);
    await controller.read();
    assert.equal(field("progress-commentary-limit").value, 8, "previews must not overwrite global drafts");
    field("cancel").click();
    assert.equal(field("progress-commentary-limit").value, 7);
    assert.equal(controller.dirty(), false);
    change("organization", "content");
    gate = deferred();
    await submit(); await submit();
    assert.equal(posts().length, 1);
    assert.equal(controller.busy(), true);
    gate.resolve(); gate = null;
    await flush();
    const body = JSON.parse(posts()[0].options.body);
    assert.equal(body.actionToken, "save-1");
    assert.equal(body.csrfToken, "csrf");
    assert.deepEqual(body.settings, { ...initialSettings, organization: "content" });
    assert.equal(controller.dirty(), false);
    assert.equal(controller.current().organization, "content");
    assert.match(document.querySelector("#status").textContent, /已保存/);
  },
  async readonly() {
    editable = false;
    await controller.load();
    assert.equal(field("fields").disabled, true);
    change("organization", "content");
    assert.equal(controller.dirty(), false);
    await submit();
    assert.equal(posts().length, 0);
  },
  async conflict() {
    await controller.load();
    change("commentary-limit", "8");
    failure = true;
    await submit();
    assert.equal(posts().length, 1);
    assert.equal(controller.dirty(), true);
    assert.equal(field("save").disabled, true);
    assert.match(document.querySelector("#status").textContent, /配置已变化/);
    assert.match(field("note").textContent, /未自动重试/);
    await submit();
    assert.equal(posts().length, 1);
    failure = false;
    confirm = false;
    assert.equal(await controller.load(), false);
    assert.equal(field("progress-commentary-limit").value, 8);
    confirm = true;
    await controller.load();
    assert.equal(controller.dirty(), false);
    assert.equal(field("progress-commentary-limit").value, 7);
    change("commentary-limit", "6");
    await submit();
    assert.equal(posts().length, 2);
  },
  async expiry() {
    await controller.load();
    change("organization", "content");
    gate = deferred();
    await submit();
    assert.equal(controller.busy(), true);
    controller.expire();
    gate.resolve(); gate = null;
    await flush();
    assert.equal(field("fields").disabled, true);
    assert.equal(field("save").disabled, true);
    assert.equal(controller.dirty(), true, "late responses cannot clear an expired draft");
    await submit();
    assert.equal(posts().length, 1);
    assert.equal(await controller.load(), false);
  },
  async read_failure() {
    failure = true;
    await assert.rejects(controller.load(), /配置已变化/);
    assert.equal(field("fields").disabled, true);
    assert.equal(field("save").disabled, true);
    failure = false;
    await controller.load();
    assert.equal(field("fields").disabled, false);
  },
  async late_refresh() {
    await controller.load();
    gate = deferred();
    const loading = controller.load();
    controller.discard();
    change("organization", "content");
    gate.resolve(); gate = null;
    assert.equal(await loading, false);
    assert.equal(field("progress-organization").value, "content");
    assert.equal(controller.dirty(), true);
  },
  async preview_order() {
    gate = deferred();
    const oldGate = gate;
    const oldRead = controller.read();
    gate = null;
    stored.commentary_limit = 3;
    await controller.read();
    assert.equal(controller.current().commentary_limit, 3);
    oldGate.resolve();
    await oldRead;
    assert.equal(controller.current().commentary_limit, 3, "late reads cannot replace a newer global preview");

    await controller.load();
    gate = deferred();
    const beforeSaveGate = gate;
    const beforeSaveRead = controller.read();
    gate = null;
    change("commentary-limit", "6");
    await submit();
    beforeSaveGate.resolve();
    await beforeSaveRead;
    assert.equal(controller.current().commentary_limit, 6, "late reads cannot replace a successful global save");
  },
};

cases[process.argv[2]]().catch((error) => { console.error(error); process.exitCode = 1; });
