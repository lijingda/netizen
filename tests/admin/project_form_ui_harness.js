window.location = { pathname: "/projects", search: "", assign() {} };
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, expire() {} };
const sessionStorage = { getItem() {}, setItem() {}, removeItem() {} };
const flush = () => new Promise((resolve) => setImmediate(resolve));
const grant = (mode) => ({ csrfToken: "csrf", actionToken: mode, target: { resource: "project-registry" } });
const actions = () => ({ register: grant("register"), createDirectory: grant("create") });
const requests = [];
let completeMutation;
let failMutation;
let listRead;
let listReads = 0;
let listFailure = false;
const status = () => document.querySelector("#status").textContent;
async function fixtureResponse(path, options = {}) {
  if (path.startsWith("/api/v1/projects?")) {
    listReads += 1;
    if (listRead) await listRead;
    if (listFailure) throw new Error("list unavailable");
    return { items: [], nextCursor: null, actions: actions() };
  }
  assert.equal(options.method, "POST");
  requests.push({ path, payload: JSON.parse(options.body) });
  return new Promise((resolve, reject) => { completeMutation = resolve; failMutation = reject; });
}
async function fetch(path, options = {}) {
  try {
    const data = await fixtureResponse(path, options);
    return { ok: true, status: 200, json: async () => data };
  } catch (error) {
    if (!error.status) throw error;
    return { ok: false, status: error.status,
      json: async () => ({ message: error.message, code: error.code }) };
  }
}
let discardConfirmed = true;
let discardPrompts = 0;
window.confirm = () => { discardPrompts += 1; return discardConfirmed; };
let valid = true;
document.querySelector("#project-editor").reportValidity = () => valid;
const input = (name) => document.querySelector(`#project-${name}`);
function open() {
  input("new").focus();
  input("new").click();
}
function submit() { return input("editor").listeners.get("submit")[0]({ preventDefault() {} }); }
// SHIPPED_PROJECT_FORM_HANDLERS
(async () => {
  await flush();
  renderProjectEditorControls();
  assert.equal(input("drawer").open, undefined);
  assert.equal(input("editor").hidden, true);
  open();
  assert.equal(input("drawer").open, true);
  assert.equal(input("editor").hidden, false);
  assert.equal(document.activeElement, input("alias"));
  assert.equal(projectEditorHasChanges(), false);
  assert.equal(input("path").required, true);
  assert.equal(input("mode-register").getAttribute("aria-pressed"), "true");

  input("alias").value = "example";
  input("path").value = "/workspace/existing";
  input("mode-create").click();
  assert.equal(input("path").required, false);
  assert.equal(input("path").value, "");
  assert.equal(input("alias").value, "example");
  assert.equal(input("submit").textContent, "创建并登记");
  input("path").value = "/workspace/new";
  input("mode-register").click();
  assert.equal(input("path").value, "/workspace/existing");
  input("mode-create").click();
  assert.equal(input("path").value, "/workspace/new");

  discardConfirmed = false;
  for (const kind of ["cancel", "close"]) {
    input(kind).click();
    assert.equal(input("drawer").open, true);
    assert.equal(input("alias").value, "example");
  }
  assert.equal(input("drawer").dispatch("cancel").prevented, true);
  assert.equal(input("drawer").open, true);
  discardConfirmed = true;
  input("cancel").click();
  assert.equal(input("drawer").open, false);
  assert.equal(document.activeElement, input("new"));

  open();
  const prompts = discardPrompts;
  input("alias").value = "temporary";
  input("alias").value = "";
  input("mode-create").click();
  input("mode-register").click();
  assert.equal(projectEditorHasChanges(), false);
  input("close").click();
  assert.equal(discardPrompts, prompts, "restoring initial values needs no discard prompt");

  for (const mode of ["register", "createDirectory"]) {
    await loadProjects();
    open();
    if (mode === "createDirectory") input("mode-create").click();
    input("alias").value = "example";
    input("path").value = mode === "register" ? "/workspace/existing" : "";
    const before = requests.length;
    valid = false;
    await submit();
    assert.equal(requests.length, before);
    valid = true;
    const pending = submit();
    await submit();
    input("cancel").click();
    assert.equal(input("drawer").dispatch("cancel").prevented, true);
    assert.equal(input("drawer").open, true);
    assert.equal(input("close").disabled, true);
    assert.equal(input("mode-create").disabled, true);
    assert.equal(input("submit").disabled, true);
    assert.equal(requests.length, before + 1);
    assert.deepEqual(requests.at(-1), {
      path: mode === "register" ? "/api/v1/projects/register" : "/api/v1/projects/create-directory",
      payload: { ...actions()[mode], alias: "example", path: mode === "register" ? "/workspace/existing" : null },
    });
    completeMutation({ alias: "example", cwd: "/workspace/result" });
    await pending;
    assert.equal(input("drawer").open, false);
    assert.equal(projectEditorHasChanges(), false);
    assert.match(status(), /example.*目录：\/workspace\/result/);
    assert.equal(document.activeElement, input("new"));
  }

  for (const failure of [Object.assign(new Error("目录不存在"), { status: 400 }), new TypeError("network unavailable")]) {
    await loadProjects();
    open();
    input("alias").value = "keep";
    input("path").value = "/workspace/keep";
    const pending = submit();
    failMutation(failure);
    await pending;
    assert.equal(input("drawer").open, true);
    assert.equal(input("alias").value, "keep");
    assert.equal(input("path").value, "/workspace/keep");
    assert.equal(input("check").hidden, false);
    assert.equal(input("submit").disabled, true);
    assert.match(input("note").textContent, failure.status ? /目录不存在/ : /结果未确认/);
    const before = requests.length;
    const reads = listReads;
    await checkProjectResult();
    assert.equal(listReads, reads + 1);
    assert.equal(input("path").value, "/workspace/keep");
    assert.equal(requests.length, before, "reading never resubmits the mutation");
    if (failure.status) {
      assert.equal(input("submit").disabled, false, "a definite refusal can use a fresh grant without losing the draft");
      input("path").value = "/workspace/corrected";
      const retry = submit();
      assert.equal(requests.at(-1).payload.path, "/workspace/corrected");
      completeMutation({ alias: "keep", cwd: "/workspace/corrected" });
      await retry;
      assert.equal(input("drawer").open, false);
    } else {
      assert.equal(input("submit").disabled, true, "uncertain results remain blocked after read-only reconciliation");
      await submit();
      assert.equal(requests.length, before);
      input("cancel").click();
    }
  }

  // A confirmed save survives a failed list refresh without encouraging another POST.
  await loadProjects();
  open();
  input("alias").value = "success";
  input("path").value = "/workspace/success";
  const pending = submit();
  listFailure = true;
  completeMutation({ alias: "success", cwd: "/workspace/success" });
  await pending;
  assert.equal(input("drawer").open, false);
  assert.match(status(), /已登记.*列表刷新失败/);
  listFailure = false;

  // A late read can update the list, but cannot overwrite a newer editor.
  await loadProjects();
  open();
  let releaseRead;
  listRead = new Promise((resolve) => { releaseRead = resolve; });
  const checking = checkProjectResult();
  input("cancel").click();
  open();
  input("alias").value = "new draft";
  releaseRead();
  await checking;
  listRead = null;
  assert.equal(input("alias").value, "new draft");
  assert.equal(input("note").textContent, "");
  state.authExpired = true;
  renderProjectEditorControls();
  assert.equal(input("submit").disabled, true);
  const before = requests.length;
  await submit();
  assert.equal(requests.length, before);
})().catch((error) => { console.error(error); process.exitCode = 1; });
