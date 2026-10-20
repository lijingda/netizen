const flush = async () => { for (let i = 0; i < 15; i += 1) await Promise.resolve(); };
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
const element = (id) => document.querySelector(`#session-access-${id}`);
const calls = [];
let acceptDiscard = true;
let failed = false;
let gate = null;
let status = "";
let remote = { revision: "first", discovery: { mode: "all", chatIds: [] },
  readSend: { mode: "all", chatIds: [] }, saveAction: { actionToken: "first" } };
const selectors = new Map();
window.createChatPicker = (_root, options) => {
  const picker = { setDisabled() {}, close() {}, reset() {}, select: options.onChange };
  selectors.set(options.id, picker);
  return picker;
};
async function fetchChatPage() { throw new Error("not requested by the editor"); }
function confirmDiscardChanges(dirty) { return !dirty || acceptDiscard; }
function actionPayload(action, fields) { return { ...action, ...fields }; }
function setStatus(message) { status = message; }
async function api(path, options = {}) {
  calls.push({ path, options });
  if (gate) await gate.promise;
  if (failed) throw new Error("保存结果未确认");
  if (options.method === "POST") {
    const body = JSON.parse(options.body);
    remote = { revision: "next", discovery: body.discovery, readSend: body.readSend, saveAction: { actionToken: "next" } };
  }
  return structuredClone(remote);
}
function change(id, value, event = "change") { element(id).value = value; element(id).dispatch(event); }
// SHIPPED_SESSION_ACCESS
(async () => {
  const editor = window.NetizenSessionAccess;
  assert.equal(await editor.load(), true);
  assert.equal(editor.dirty(), false);
  assert.equal(element("unsaved").hidden, true);
  assert.equal(element("save").disabled, true);
  assert.equal(element("discovery-targets").hidden, true);
  change("discovery-mode", "only");
  assert.equal(element("discovery-targets").hidden, false);
  assert.equal(element("readSend-targets").hidden, true);
  selectors.get("session-access-discovery-group").select({ chatId: "oc_selected", name: "群名" });
  change("discovery-chats", element("discovery-chats").value + "\noc_manual\noc_selected", "input");
  change("readSend-mode", "exclude");
  change("readSend-chats", "oc_private", "input");
  assert.equal(editor.dirty(), true);
  assert.equal(element("unsaved").hidden, false);
  assert.equal(element("save").disabled, false);
  acceptDiscard = false;
  const before = calls.length;
  assert.equal(await editor.load(), false);
  assert.equal(calls.length, before, "cancelled refresh makes no request");
  element("form").dispatch("submit");
  assert.equal(editor.busy(), true);
  assert.equal(element("save").disabled, true);
  await flush();
  const payload = JSON.parse(calls.at(-1).options.body);
  assert.deepEqual(payload.discovery, { mode: "only", chatIds: ["oc_manual", "oc_selected"] });
  assert.deepEqual(payload.readSend, { mode: "exclude", chatIds: ["oc_private"] });
  assert.equal(payload.actionToken, "first");
  assert.equal(editor.dirty(), false);
  assert.equal(element("unsaved").hidden, true);
  assert.match(status, /已保存/);

  change("discovery-mode", "disabled");
  assert.equal(element("discovery-targets").hidden, true);
  failed = true;
  element("form").dispatch("submit");
  await flush();
  const failedCount = calls.length;
  assert.deepEqual(JSON.parse(calls.at(-1).options.body).discovery, { mode: "disabled", chatIds: [] });
  assert.equal(element("save").disabled, true, "unknown save consumes the local grant");
  assert.equal(editor.dirty(), true, "failed save preserves draft");
  assert.equal(element("unsaved").hidden, false);
  element("form").dispatch("submit");
  await flush();
  assert.equal(calls.length, failedCount, "failed save is never automatically replayed");
  assert.match(element("note").textContent, /刷新核对/);

  failed = false;
  acceptDiscard = true;
  gate = deferred();
  const loading = editor.load();
  change("readSend-chats", "oc_edited_during_read", "input");
  gate.resolve();
  assert.equal(await loading, false, "late refresh cannot overwrite new edits");
  assert.equal(element("readSend-chats").value, "oc_edited_during_read");
  gate = deferred();
  const picking = editor.load();
  selectors.get("session-access-readSend-group").select({ chatId: "oc_added_during_read", name: "新群" });
  gate.resolve();
  assert.equal(await picking, false, "chat selection also invalidates a pending refresh");
  assert.match(element("readSend-chats").value, /oc_added_during_read/);
  gate = null;
  await editor.load();
  assert.equal(editor.dirty(), false);
  change("readSend-mode", "disabled");
  editor.discard();
  assert.equal(editor.dirty(), false);
  assert.equal(element("unsaved").hidden, true);
  assert.equal(element("readSend-mode").value, "exclude");
  editor.expire();
  assert.equal(element("fields").disabled, true);
  assert.equal(element("save").disabled, true);
  assert.equal(await editor.load(), false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
