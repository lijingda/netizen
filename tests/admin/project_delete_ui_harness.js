const state = { projects: null };
const requests = [];
const refreshes = [];
let status = "";
let statusError = false;
function setStatus(message, error = false) { status = message; statusError = error; }
const confirmations = [];
let confirmation = true;
window.confirm = (message) => { confirmations.push(message); return confirmation; };
const envelope = (kind) => ({
  csrfToken: `${kind}-csrf`, actionToken: `${kind}-once`,
  target: { resource: "project", targetId: "example", revision: 7 },
});
const previewEnvelope = envelope("preview");
const deleteEnvelope = envelope("delete");
const projectFixture = () => ({
  alias: "example", cwd: "/workspace/example", enabled: true, deleting: false,
  bindingCount: 3, lazyBindingCount: 1, archivedBindingCount: 1, lastActivatedAt: null,
  actions: { setEnabled: envelope("enable"), previewDelete: structuredClone(previewEnvelope) },
});
const preview = {
  project: { alias: "example", cwd: "/workspace/example", revision: 7 },
  sessionCount: 3, lazySessionCount: 1, materializedSessionCount: 2, sideCount: 2, scheduledPlanCount: 2, scheduledRunCount: 1,
  actions: { delete: deleteEnvelope },
};
let projects = [projectFixture()];
let respond;
let refreshFailed = false;
async function api(path, options = {}) {
  requests.push({ path, options });
  if (path.startsWith("/api/v1/projects?")) {
    if (refreshFailed) throw new TypeError("offline");
    return { items: structuredClone(projects), nextCursor: null };
  }
  return respond(path, options);
}
async function refresh(tab) {
  refreshes.push(tab);
  try { await loadProjects(); return true; } catch { return false; }
}
const postCount = (path) => requests.filter((request) => request.path === path).length;
const resultMessage = () => document.querySelector("#project-delete-message").textContent;
const resultDetails = () => document.querySelector("#project-delete-remaining").textContent;
const deletionButton = () => document.querySelector("#projects-body").querySelectorAll("button")
  .find((button) => button.textContent === "删除 Project 及关联 Sessions");
// SHIPPED_PROJECT_CONTROLLER
(async () => {
  await loadProjects();
  assert(deletionButton());
  assert.equal(document.querySelector("#project-delete-result").getAttribute("aria-live"), "polite");
  confirmation = false;
  respond = async (path) => {
    assert.equal(path, "/api/v1/projects/delete-preview");
    return preview;
  };
  await deletionButton().listeners.get("click")[0]();
  assert.equal(postCount("/api/v1/projects/delete-preview"), 1);
  assert.equal(postCount("/api/v1/projects/delete"), 0);
  assert.match(resultMessage(), /已取消.*未提交删除/);
  assert.equal(statusError, false);
  assert(confirmations[0].includes("example"));
  assert(confirmations[0].includes("关联 Sessions：3（Lazy 1，已创建 Thread 2）"));
  assert.match(confirmations[0], /所有归档会话.*不受 Sessions 页面筛选影响/);
  assert.match(confirmations[0], /关联 Side：2/);
  assert.match(confirmations[0], /关联定时计划：2.*未决的定时执行：1/);
  assert.match(confirmations[0], /会话清理失败也不会恢复计划/);
  assert.match(confirmations[0], /派生子会话.*永久删除.*无法恢复/);
  assert.match(confirmations[0], /磁盘代码目录保留：\/workspace\/example/);
  const previewRequest = requests.find((request) => request.path.endsWith("delete-preview"));
  assert.deepEqual(JSON.parse(previewRequest.options.body), actionPayload(previewEnvelope));

  // Block duplicate clicks throughout both preview and delete, including refreshed rows.
  confirmation = true;
  let releasePreview;
  let releaseDelete;
  respond = (path) => path.endsWith("delete-preview")
    ? new Promise((resolve) => { releasePreview = resolve; })
    : new Promise((resolve) => { releaseDelete = resolve; });
  const project = state.projects.items[0];
  const first = deletionButton().listeners.get("click")[0]();
  await deleteProject(project);
  await loadProjects();
  assert(deletionButton().disabled);
  await deleteProject(state.projects.items[0]);
  assert.equal(postCount("/api/v1/projects/delete-preview"), 2);
  releasePreview(preview);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(postCount("/api/v1/projects/delete"), 1);
  assert.deepEqual(JSON.parse(requests.find((request) => request.path.endsWith("/delete")).options.body),
    actionPayload(deleteEnvelope));
  await deleteProject(state.projects.items[0]);
  assert.equal(postCount("/api/v1/projects/delete"), 1);
  projects = [];
  releaseDelete({ deleted: true, projectAlias: "example", deletedSessionCount: 3 });
  await first;
  assert.match(resultMessage(), /已删除 3 个 Session.*磁盘代码目录保留/);
  assert.equal(statusError, false);
  assert.equal(document.querySelector("#project-delete-result").hidden, false);
  await loadProjects();
  assert.match(resultMessage(), /已删除 3 个 Session/);

  // A 200 partial result is visibly incomplete with inspectable remaining identities.
  projects = [projectFixture()];
  await loadProjects();
  respond = async (path) => path.endsWith("delete-preview") ? preview : {
    deleted: false, projectAlias: "example", deletedSessionCount: 1,
    remainingSessionCount: 2, remainingSideCount: 1,
    remainingSessions: [{ bindingId: "full-binding-2", shortId: "short-2", scopeKey: "topic:chat:topic" }],
    failedBindingId: "full-binding-2", failedSideId: "side-1", code: "partial",
    message: "<img src=x onerror=alert(1)>原生删除结果未知",
  };
  await deleteProject(state.projects.items[0]);
  assert.equal(statusError, true);
  assert.match(resultMessage(), /仍保留.*删除尚未全部完成/);
  assert.match(resultMessage(), /已删除 1 个 Session；剩余 2 个 Session、1 个 Side/);
  assert.match(resultDetails(), /short-2.*topic:chat:topic.*full-binding-2/);
  assert.match(resultDetails(), /side-1/);
  assert.equal(document.querySelector("#project-delete-result").querySelectorAll("img").length, 0);
  assert.doesNotMatch(resultMessage(), /删除成功|已回滚/);

  // A disconnected delete never re-posts, even when refreshing confirms an active server.
  projects = [{ ...projectFixture(), deleting: true,
    actions: { setEnabled: null, previewDelete: null } }];
  respond = async (path) => {
    if (path.endsWith("delete-preview")) return preview;
    throw new TypeError("offline");
  };
  const beforeDisconnect = postCount("/api/v1/projects/delete");
  await deleteProject(state.projects.items[0]);
  assert.equal(postCount("/api/v1/projects/delete"), beforeDisconnect + 1);
  assert.match(resultMessage(), /删除结果未确认.*可能仍在处理.*不要重复提交/);
  assert.doesNotMatch(resultMessage(), /删除成功|已回滚/);
  assert.equal(statusError, true);
  assert.match(document.querySelector("#projects-body").textContent, /正在删除/);
  assert.equal(deletionButton(), undefined);
  assert.equal(document.querySelector("#projects-body").querySelectorAll("button").length, 0);

  projects = [projectFixture()];
  await loadProjects();
  refreshFailed = true;
  await deleteProject(state.projects.items[0]);
  assert.match(resultMessage(), /删除结果未确认/);
  assert.match(resultMessage(), /Projects 刷新失败.*手动刷新/);
  assert.doesNotMatch(resultMessage(), /删除成功|已回滚/);
  refreshFailed = false;

  await loadProjects();
  respond = async () => { throw new TypeError("sensitive internal transport args"); };
  const beforePreviewFailure = postCount("/api/v1/projects/delete");
  await deleteProject(state.projects.items[0]);
  assert.equal(postCount("/api/v1/projects/delete"), beforePreviewFailure);
  assert.match(resultMessage(), /无法读取.*未提交删除/);
  assert.doesNotMatch(resultMessage(), /sensitive internal/);
  assert(refreshes.every((tab) => tab === "projects"));
})().catch((error) => { console.error(error); process.exitCode = 1; });
