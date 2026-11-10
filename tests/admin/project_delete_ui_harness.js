window.location = { pathname: "/projects", search: "", assign() {} };
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, expire() {} };
const sessionStorage = { getItem() {}, setItem() {}, removeItem() {} };
const flush = () => new Promise((resolve) => setImmediate(resolve));
let completed = false;
process.once("beforeExit", () => assert(completed, "All deletion dialog cases must finish"));
const requests = [];
let statusError = false;
document.querySelector("#status").classList.toggle = (_name, error) => { statusError = error; };
confirmResponder = () => { throw new Error("Project deletion must use its scope dialog"); };
const envelope = (kind) => ({
  csrfToken: `${kind}-csrf`, actionToken: `${kind}-once`,
  target: { resource: "project", targetId: "example", revision: 7 },
});
const previewEnvelope = envelope("preview");
const deleteEnvelope = envelope("delete");
const projectFixture = () => ({
  alias: "example", cwd: "/workspace/example", enabled: true, deleting: false,
  bindingCount: 3, lazyBindingCount: 1, lastActivatedAt: null,
  actions: { setEnabled: envelope("enable"), previewDelete: structuredClone(previewEnvelope) },
});
const previewFixture = (deleteWorktrees = false, serial = "default") => ({
  project: { alias: "example", cwd: "/workspace/example", revision: 7 },
  sessionCount: 3, lazySessionCount: 1, materializedSessionCount: 2, sideCount: 2,
  scheduledPlanCount: 2, scheduledRunCount: 1, worktreeCount: 2, deleteWorktrees,
  worktrees: [{ id: "wt-one", root: "/managed/one" }, { id: "wt-two", root: "/managed/two" }],
  actions: { delete: serial === "default" ? deleteEnvelope : envelope(`delete-${serial}`),
    previewDelete: envelope(`preview-${serial}`) },
});
const preview = previewFixture();
let projects = [projectFixture()];
let respond;
let refreshFailed = false;
async function fixtureResponse(path, options = {}) {
  requests.push({ path, options });
  if (path.startsWith("/api/v1/projects?")) {
    if (refreshFailed) throw new TypeError("offline");
    return { items: structuredClone(projects), nextCursor: null };
  }
  return respond(path, options);
}
async function fetch(path, options = {}) {
  const data = await fixtureResponse(path, options);
  if (data?.unauthorized) return { ok: false, status: 401, json: async () => ({}) };
  return { ok: true, status: 200, json: async () => data };
}
const postCount = (path) => requests.filter((request) => request.path === path).length;
const previewCount = () => postCount("/api/v1/projects/delete-preview");
const deleteCount = () => postCount("/api/v1/projects/delete");
const lastBody = (path) => JSON.parse(requests.filter((request) => request.path === path).at(-1).options.body);
const resultMessage = () => document.querySelector("#project-delete-message").textContent;
const resultDetails = () => document.querySelector("#project-delete-remaining").textContent;
const field = (name) => document.querySelector(`#project-delete-${name}`);
const deletionButton = () => document.querySelector("#projects-body").querySelector(".project-delete-trigger");
function openDeletion() {
  const button = deletionButton();
  button.focus();
  const result = button.listeners.get("click")[0]();
  assert.equal(field("dialog").open, true);
  assert.equal(field("confirm").disabled, true);
  assert.equal(field("worktrees").checked, false);
  return result;
}
async function confirmDeletion(project = state.projects.items[0]) {
  const result = deleteProject(project);
  await flush();
  assert.equal(field("confirm").disabled, false);
  field("confirm").click();
  return result;
}
// SHIPPED_PROJECT_CONTROLLER
(async () => {
  await flush();
  // Projects retain their local counts, with the optional cascade scoped to the dialog.
  await loadProjects();
  const localCells = document.querySelector("#projects-body").querySelectorAll("td");
  assert.equal(localCells[3].textContent, "3（未开始 1）");
  assert.equal(localCells.length, 6);
  assert.equal(deletionButton().textContent, "删除…");
  assert.equal(deletionButton().getAttribute("aria-haspopup"), "dialog");
  assert.equal(document.querySelector("#projects-body").querySelectorAll("input").length, 0);
  assert.equal(field("result").getAttribute("aria-live"), "polite");
  assert(field("dialog").getAttribute("aria-labelledby"));
  assert.equal(field("worktrees").type, "checkbox");
  assert(field("worktree-option").contains(field("worktrees")));
  respond = async (path) => {
    assert.equal(path, "/api/v1/projects/delete-preview");
    return structuredClone(preview);
  };
  let operation = openDeletion();
  assert.equal(document.activeElement, field("cancel"));
  await flush();
  assert.equal(field("scope").hidden, false);
  assert.equal(field("confirm").disabled, false);
  assert.equal(field("worktree-option").hidden, false);
  assert.equal(field("worktrees").disabled, false);
  assert.match(field("dialog").textContent, /example/);
  assert.match(field("dialog").textContent, /归档/);
  assert.match(field("dialog").textContent, /定时/);
  assert.match(field("dialog").textContent, /永久.*无法恢复/);
  assert.equal(field("sessions").textContent, "3（未开始 1，已创建 2）");
  assert.equal(field("sides").textContent, "2");
  assert.equal(field("plans").textContent, "2");
  assert.equal(field("runs").textContent, "1");
  assert.equal(field("cwd").textContent, "/workspace/example");
  assert.equal(field("paths").hidden, true);
  field("cancel").click();
  await operation;
  assert.equal(previewCount(), 1);
  assert.equal(deleteCount(), 0);
  assert.equal(field("dialog").open, false);
  assert.equal(document.activeElement, deletionButton());
  assert.match(resultMessage(), /已取消.*未提交删除/);
  assert.equal(statusError, false);
  assert.deepEqual(lastBody("/api/v1/projects/delete-preview"), actionPayload(previewEnvelope));

  // Each selection receives a fresh preview grant; the final delete uses only its exact grant.
  const choices = [previewFixture(false, "initial"), previewFixture(true, "included"), previewFixture(false, "retained")];
  let releaseSelection;
  respond = (path) => {
    if (path.endsWith("delete-preview")) {
      if (choices.length === 2) return new Promise((resolve) => { releaseSelection = () => resolve(choices.shift()); });
      return structuredClone(choices.shift());
    }
    return { deleted: true, projectAlias: "example", deletedSessionCount: 3 };
  };
  operation = openDeletion();
  await flush();
  const beforeSelection = previewCount();
  field("worktrees").focus();
  field("worktrees").checked = true;
  field("worktrees").dispatch("change");
  // Browsers blur an active checkbox when it becomes disabled during loading.
  document.body.focus();
  assert.equal(field("confirm").disabled, true);
  assert.equal(field("worktrees").disabled, true);
  assert.equal(field("scope").hidden, true);
  assert.deepEqual(lastBody("/api/v1/projects/delete-preview"),
    actionPayload(envelope("preview-initial"), { deleteWorktrees: true }));
  field("confirm").click();
  field("worktrees").dispatch("change");
  assert.equal(previewCount(), beforeSelection + 1);
  assert.equal(deleteCount(), 0);
  releaseSelection();
  await flush();
  assert.equal(document.activeElement, field("worktrees"));
  assert.equal(field("paths").hidden, false);
  assert.match(field("paths").textContent, /\/managed\/one/);
  assert.match(field("paths").textContent, /\/managed\/two/);
  field("worktrees").checked = false;
  field("worktrees").dispatch("change");
  await flush();
  assert.deepEqual(lastBody("/api/v1/projects/delete-preview"), actionPayload(envelope("preview-included")));
  assert.equal(field("paths").hidden, true);
  field("confirm").click();
  field("confirm").click();
  await operation;
  assert.deepEqual(lastBody("/api/v1/projects/delete"), actionPayload(envelope("delete-retained")));
  assert.equal(deleteCount(), 1);

  // Block duplicate starts throughout both preview and delete, including refreshed rows.
  let releasePreview;
  let releaseDelete;
  respond = (path) => path.endsWith("delete-preview")
    ? new Promise((resolve) => { releasePreview = resolve; })
    : new Promise((resolve) => { releaseDelete = resolve; });
  const beforeDuplicatePreview = previewCount();
  const beforeDuplicateDelete = deleteCount();
  const project = state.projects.items[0];
  operation = openDeletion();
  await deleteProject(project);
  await loadProjects();
  assert(deletionButton().disabled);
  await deleteProject(state.projects.items[0]);
  assert.equal(previewCount(), beforeDuplicatePreview + 1);
  releasePreview(structuredClone(preview));
  await flush();
  field("confirm").click();
  field("confirm").click();
  await flush();
  assert.equal(deleteCount(), beforeDuplicateDelete + 1);
  assert.deepEqual(lastBody("/api/v1/projects/delete"), actionPayload(deleteEnvelope));
  await deleteProject(state.projects.items[0]);
  assert.equal(deleteCount(), beforeDuplicateDelete + 1);
  projects = [];
  releaseDelete({ deleted: true, projectAlias: "example", deletedSessionCount: 3 });
  await operation;
  assert.match(resultMessage(), /已删除 3 个会话.*磁盘代码目录保留/);
  assert.equal(statusError, false);
  assert.equal(field("result").hidden, false);
  await loadProjects();
  assert.match(resultMessage(), /已删除 3 个会话/);

  // A 200 partial result remains visibly incomplete with inspectable remaining identities.
  projects = [projectFixture()];
  await loadProjects();
  respond = async (path) => path.endsWith("delete-preview") ? structuredClone(preview) : {
    deleted: false, projectAlias: "example", deletedSessionCount: 1,
    remainingSessionCount: 2, remainingSideCount: 1,
    remainingSessions: [{ bindingId: "full-binding-2", shortId: "short-2", scopeKey: "topic:chat:topic" }],
    failedBindingId: "full-binding-2", failedSideId: "side-1", code: "partial",
    message: "<img src=x onerror=alert(1)>原生删除结果未知",
  };
  await confirmDeletion();
  assert.equal(statusError, true);
  assert.match(resultMessage(), /仍保留.*删除尚未全部完成/);
  assert.match(resultMessage(), /已删除 1 个会话；剩余 2 个会话、1 个 Side/);
  assert.match(resultDetails(), /short-2.*topic:chat:topic.*full-binding-2/);
  assert.match(resultDetails(), /side-1/);
  assert.equal(field("result").querySelectorAll("img").length, 0);
  assert.doesNotMatch(resultMessage(), /删除成功|已回滚/);

  // A disconnected delete never re-posts, even when refreshing confirms an active server.
  projects = [{ ...projectFixture(), deleting: true,
    actions: { setEnabled: null, previewDelete: null } }];
  respond = async (path) => {
    if (path.endsWith("delete-preview")) return structuredClone(preview);
    throw new TypeError("offline");
  };
  const beforeDisconnect = deleteCount();
  await confirmDeletion();
  assert.equal(deleteCount(), beforeDisconnect + 1);
  assert.match(resultMessage(), /删除结果未确认.*可能仍在处理.*不要重复提交/);
  assert.doesNotMatch(resultMessage(), /删除成功|已回滚/);
  assert.equal(statusError, true);
  assert.match(document.querySelector("#projects-body").textContent, /正在删除/);
  assert.equal(deletionButton(), null);
  assert.equal(document.querySelector("#projects-body").querySelectorAll("button").length, 0);

  projects = [projectFixture()];
  await loadProjects();
  refreshFailed = true;
  await confirmDeletion();
  assert.match(resultMessage(), /删除结果未确认/);
  assert.match(resultMessage(), /Projects 刷新失败.*手动刷新/);
  assert.doesNotMatch(resultMessage(), /删除成功|已回滚/);
  refreshFailed = false;

  // Failure to refresh a changed choice invalidates the previous delete authorization.
  await loadProjects();
  let failChoice = false;
  respond = async () => {
    if (failChoice) throw new TypeError("sensitive internal transport args");
    return structuredClone(preview);
  };
  operation = openDeletion();
  await flush();
  failChoice = true;
  const beforeFailure = deleteCount();
  field("worktrees").checked = true;
  field("worktrees").dispatch("change");
  await flush();
  assert.equal(field("dialog").open, true);
  assert.equal(field("scope").hidden, true);
  assert.equal(field("confirm").disabled, true);
  assert.equal(field("worktrees").disabled, true);
  assert.match(field("note").textContent, /失败|无法读取/);
  assert.doesNotMatch(field("note").textContent, /sensitive internal/);
  field("confirm").click();
  assert.equal(deleteCount(), beforeFailure);
  field("close").click();
  await operation;
  assert.match(resultMessage(), /无法读取.*未提交删除/);
  assert.doesNotMatch(resultMessage(), /sensitive internal/);

  // An initial preview failure has the same safe state and can be dismissed.
  operation = openDeletion();
  await flush();
  assert.equal(field("scope").hidden, true);
  assert.equal(field("confirm").disabled, true);
  field("confirm").click();
  assert.equal(deleteCount(), beforeFailure);
  field("cancel").click();
  await operation;

  // Cancelling an in-flight preview permits reopening without late responses changing it.
  let releaseOldPreview;
  respond = () => new Promise((resolve) => { releaseOldPreview = resolve; });
  operation = openDeletion();
  const cancelEvent = field("dialog").dispatch("cancel");
  assert.equal(cancelEvent.prevented, true);
  await operation;
  assert.equal(field("dialog").open, false);
  const harmless = previewFixture(false, "new-dialog");
  harmless.project.cwd = "/workspace/<b>safe</b>";
  harmless.worktreeCount = 0;
  harmless.worktrees = [];
  projects = [{ ...projectFixture(), alias: "<img src=x onerror=alert(1)>" }];
  await loadProjects();
  respond = async () => structuredClone(harmless);
  operation = openDeletion();
  await flush();
  assert.equal(field("worktree-option").hidden, true);
  const currentText = field("dialog").textContent;
  assert.match(currentText, /<img src=x onerror=alert\(1\)>/);
  assert.match(currentText, /\/workspace\/<b>safe<\/b>/);
  assert.equal(field("dialog").querySelectorAll("img").length, 0);
  releaseOldPreview(previewFixture(true, "old-dialog"));
  await flush();
  assert.equal(field("dialog").open, true);
  assert.equal(field("dialog").textContent, currentText);
  assert.equal(field("worktrees").checked, false);
  field("close").click();
  await operation;
  assert.equal(deleteCount(), beforeFailure);

  // Authorization expiry releases the modal and prevents a pending scope becoming submittable.
  respond = async () => ({ unauthorized: true });
  operation = openDeletion();
  await operation;
  assert.equal(state.authExpired, true);
  assert.equal(field("dialog").open, false);
  assert.equal(field("confirm").disabled, true);
  field("confirm").click();
  assert.equal(deleteCount(), beforeFailure);
  assert(requests.every(({ path }) => path.startsWith("/api/v1/projects")));
  completed = true;
})().catch((error) => { console.error(error); process.exitCode = 1; });
