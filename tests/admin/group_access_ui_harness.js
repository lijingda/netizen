const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
const element = (id) => document.querySelector(`#group-access-${id}`);
const calls = [];
let acceptDiscard = true;
let acceptSwitch = true;
let failed = false;
let gate = null;
let selected = null;
const confirmations = [];
const statuses = [];
let remote = { mode: "all", chatIds: ["oc_retained"], revision: "first",
  chats: [{chatId: "oc_retained", name: "保留群"}],
  saveAction: { actionToken: "first" } };
confirmResponder = (message) => { confirmations.push(message); return acceptSwitch; };
window.createChatPicker = (_root, options) => {
  selected = options.onChange;
  return { setDisabled() {}, close() {}, reset() {} };
};
async function fetchChatPage() { throw new Error("not requested by editor"); }
function confirmDiscardChanges(dirty) { return !dirty || acceptDiscard; }
function actionPayload(action, fields) { return { ...action, ...fields }; }
function setStatus(message, error) { statuses.push({message, error}); }
async function api(path, options = {}) {
  calls.push({path, options});
  if (gate) await gate.promise;
  if (failed) throw new Error("save outcome unknown");
  if (options.method === "POST") {
    const body = JSON.parse(options.body);
    remote = {...remote, mode: body.mode, chatIds: body.chatIds, revision: "next", saveAction: {actionToken: "next"}};
  }
  return structuredClone(remote);
}
function change(id, value, event = "change") { element(id).value = value; element(id).dispatch(event); }
const visibleIds = () => element("chats").value.split(/\s+/).filter(Boolean).sort();
// SHIPPED_GROUP_ACCESS
(async () => {
  const editor = window.NetizenGroupAccess;
  assert.equal(await editor.load(), true);
  assert.equal(editor.dirty(), false);
  assert.equal(element("risk").hidden, false);
  assert.match(element("selected").textContent, /保留群.*oc_retained/);
  change("mode", "only");
  assert.deepEqual(visibleIds(), ["oc_retained"], "changing mode leaves the list alone");
  selected({chatId: "oc_selected", name: "新群"});
  assert.match(element("selected").textContent, /新群.*oc_selected/);
  change("mode", "all");
  assert.deepEqual(visibleIds(), ["oc_retained", "oc_selected"], "switching back keeps the edited list");
  change("mode", "only");
  assert.deepEqual(visibleIds(), ["oc_retained", "oc_selected"]);
  selected({chatId: "oc_manual", name: "手动添加群"});
  assert.deepEqual(visibleIds(), ["oc_manual", "oc_retained", "oc_selected"]);
  acceptSwitch = false;
  const before = calls.length;
  const save = element("save");
  const focusSave = save.focus.bind(save);
  save.focus = () => { if (!save.disabled) focusSave(); };
  save.focus();
  element("form").dispatch("submit");
  await flush();
  assert.equal(calls.length, before, "canceling confirmation must not consume a grant");
  assert.equal(document.activeElement, save, "cancel restores focus after the form is enabled");
  acceptDiscard = false;
  assert.equal(await editor.load(), false);
  acceptSwitch = true;
  const expectedSaved = visibleIds();
  element("form").dispatch("submit");
  assert.equal(editor.busy(), true);
  await flush();
  assert.deepEqual(JSON.parse(calls.at(-1).options.body).chatIds, expectedSaved, "save uses exactly the visible list");
  assert.match(confirmations.at(-1), /3/);
  assert.match(confirmations.at(-1), /\/enable/);
  assert.equal(editor.dirty(), false);
  assert.equal(element("risk").hidden, true);
  change("mode", "all");
  change("mode", "only");
  assert.deepEqual(visibleIds(), expectedSaved, "mode edits preserve the saved list");
  change("mode", "all");
  element("form").dispatch("submit");
  await flush();
  assert.deepEqual(remote.chatIds, expectedSaved, "all mode retains the saved list");
  change("mode", "only");
  editor.discard();
  assert.equal(element("mode").value, "all");
  assert.deepEqual(visibleIds(), expectedSaved, "discard restores the saved list");
  change("mode", "only");
  acceptDiscard = true;
  assert.equal(await editor.load(), true);
  assert.equal(element("mode").value, "all");
  assert.deepEqual(visibleIds(), expectedSaved, "reload restores the saved list");
  change("mode", "only");
  change("chats", "oc_retained", "input");
  element("form").dispatch("submit");
  await flush();
  assert.deepEqual(remote.chatIds, ["oc_retained"], "deleted groups are not included");
  const realIds = Array.from({length: 1000}, (_, index) => `oc_${index.toString(16).padStart(32, "0")}`);
  change("chats", [...realIds, realIds[0]].join("\n"), "input");
  element("form").dispatch("submit");
  await flush();
  assert.deepEqual(remote.chatIds, realIds, "1000 real IDs remain saveable after deduplication");
  const boundedCalls = calls.length;
  change("chats", [...realIds, "oc_ffffffffffffffffffffffffffffffff"].join("\n"), "input");
  element("form").dispatch("submit");
  await flush();
  assert.equal(calls.length, boundedCalls, "over-limit lists do not consume a grant or send a request");
  assert.match(statuses.at(-1).message, /1000/);
  assert.equal(statuses.at(-1).error, true);
  assert.equal(element("save").disabled, false, "validation leaves the grant available for correction");
  change("chats", "oc_new", "input");
  assert.equal(element("selected").textContent, "oc_new");
  failed = true;
  element("form").dispatch("submit");
  await flush();
  const failedCount = calls.length;
  assert.equal(editor.dirty(), true);
  assert.equal(element("save").disabled, true);
  element("form").dispatch("submit");
  await flush();
  assert.equal(calls.length, failedCount, "unknown save cannot be replayed");
  failed = false; acceptDiscard = true;
  gate = deferred();
  const loading = editor.load();
  change("chats", "oc_draft", "input");
  gate.resolve();
  assert.equal(await loading, false, "late reads preserve the edited draft");
  assert.equal(element("chats").value, "oc_draft");
  gate = null;
  editor.discard();
  assert.equal(editor.dirty(), false);
  editor.expire();
  assert.equal(element("fields").disabled, true);
  assert.equal(await editor.load(), false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
