const requests = [];
const confirmations = [];
let confirmation = true;
let expired = false;
let status = "";
let safe = true;
let disconnect = false;
let creationResult;
let settingsResult;
let branchResults = new Map();
window.confirm = (text) => { confirmations.push(text); return confirmation; };
const grant = (kind) => ({ csrfToken: `${kind}-csrf`, actionToken: `${kind}-once`, target: { resource: "worktree", targetId: "one" } });
const row = (state = "ready") => ({ id: "one", projectAlias: "demo", root: "/worktrees/one", cwd: "/worktrees/one/sub",
  state, branch: null, head: "abc123", orphaned: state === "missing", associatedSessionCount: 2,
  available: false,
  actions: { previewDelete: grant("preview") } });
let rows = [row(), { ...row("missing"), id: "missing" }, { ...row("unknown"), id: "unknown" }, { ...row("replaced"), id: "replaced" }];
const branches = { isGit: true, items: [
  { name: "main", ref: "refs/heads/main", remote: false, occupiedPaths: ["/source"] },
  { name: "feature", ref: "refs/heads/feature", remote: false, occupiedPaths: [] },
  { name: "origin/main", ref: "refs/remotes/origin/main", remote: true, occupiedPaths: [] },
], actions: { create: grant("create") } };
const settings = { root: "", effectiveRoot: "/instance/worktrees", fetchBeforeCreate: false,
  editable: true, actions: { save: grant("settings") } };
const input = (id) => document.querySelector(`#worktree-${id}`);
const trigger = async (id, kind = "click") => {
  const element = document.querySelector(`#${id}`);
  return element.listeners.get(kind)[0]({ target: element, currentTarget: element, preventDefault() {} });
};
const rowAction = () => document.querySelector("#worktrees-body").querySelector("button");
const clickRemove = () => rowAction().listeners.get("click")[0]();
const calls = (suffix) => requests.filter((request) => request.path.endsWith(suffix));
async function api(path, options = {}) {
  requests.push({ path, options });
  if (path === "/api/v1/worktrees/settings") {
    if (!options.method) return settingsResult ? settingsResult() : structuredClone(settings);
    return structuredClone(settings);
  }
  if (path.startsWith("/api/v1/worktrees?")) return { items: structuredClone(rows), nextCursor: path.includes("cursor") ? null : "second-page" };
  if (path.startsWith("/api/v1/worktrees/branches?")) {
    const project = new URLSearchParams(path.split("?")[1]).get("project");
    return branchResults.has(project) ? branchResults.get(project)() : structuredClone(branches);
  }
  if (path.endsWith("/create")) {
    if (creationResult) return creationResult();
    return { resource: row() };
  }
  if (path.endsWith("/delete-preview")) return {
    resource: row(), safe, reasons: safe ? [] : ["未保留的文件"],
    deleteSessions: JSON.parse(options.body).deleteSessions,
    bindings: [{ shortId: "binding-one", nativeThreadId: "native-one" }], protectedSessionCount: 1,
    actions: safe ? { delete: grant("delete") } : {},
  };
  if (path.endsWith("/delete")) {
    if (disconnect) throw new TypeError("internal transport args");
    return { deleted: true, code: "deleted" };
  }
  throw new Error(`unexpected path ${path}`);
}
// SHIPPED_WORKTREES
(async () => {
  const controller = window.NetizenWorktrees;
  controller.init({ api, isExpired: () => expired, setStatus: (text) => { status = text; },
    queryProjectOptions: async () => [{ alias: "demo", enabled: true }, { alias: "other", enabled: true }, { alias: "disabled", enabled: false }],
    actionPayload: (envelope, extra = {}) => ({ csrfToken: envelope.csrfToken, actionToken: envelope.actionToken, target: envelope.target, ...extra }),
  });
  await controller.load();
  assert.equal(input("root").value, "");
  assert.equal(input("fetch").checked, false);
  assert.equal(input("mode").value, "detached");
  assert.match(input("effective-root").textContent, /\/instance\/worktrees/);
  assert.equal(input("project").children.length, 3);
  assert.match(document.querySelector("#worktrees-body").textContent, /目录缺失/);
  assert.match(document.querySelector("#worktrees-body").textContent, /状态未取得/);
  assert.match(document.querySelector("#worktrees-body").textContent, /路径身份已改变/);
  assert.match(document.querySelector("#worktrees-body").textContent, /原 Project 已删除/);
  assert.match(document.querySelector("#worktrees-body").textContent, /不可用于新会话/);
  assert.equal(document.querySelector("#worktrees-body").querySelector("input").checked, false);
  await trigger("worktrees-next");
  assert.match(requests.at(-1).path, /cursor=second-page/);
  assert.match(document.querySelector("#worktrees-page").textContent, /第 2 页/);
  await trigger("worktrees-previous");
  assert.doesNotMatch(requests.at(-1).path, /cursor=/);

  input("project").value = "demo";
  await trigger("worktree-project", "change");
  assert.equal(input("create-submit").disabled, false);
  assert.equal(input("existing").children.find((item) => item.value === "main").disabled, true);
  assert.equal(input("existing").children.some((item) => item.value === "origin/main"), false);
  input("mode").value = "existing";
  await trigger("worktree-mode", "change");
  assert.equal(input("ref-label").hidden, true);
  assert.equal(input("existing-label").hidden, false);
  input("existing").value = "feature";
  await trigger("worktree-existing", "input");
  await trigger("worktree-create", "submit");
  const creation = JSON.parse(calls("/create").at(-1).options.body);
  assert.equal(creation.mode, "existing");
  assert.equal(creation.branch, "feature");
  assert.equal(creation.ref, "refs/heads/feature");
  assert.match(status, /尚未创建会话/);
  await trigger("worktree-create", "submit");
  assert.equal(calls("/create").length, 1);

  // Late branch results cannot create a grant for the old Project.
  let resolveOld;
  branchResults.set("demo", () => new Promise((resolve) => { resolveOld = resolve; }));
  input("project").value = "demo";
  const old = trigger("worktree-project", "change");
  input("project").value = "other";
  await trigger("worktree-project", "change");
  resolveOld({ isGit: false, items: [], actions: {} });
  await old;
  input("mode").value = "new";
  input("new-branch").value = "feature-new";
  await trigger("worktree-mode", "change");
  assert.equal(input("create-submit").disabled, false);
  assert.equal(input("new-label").hidden, false);
  creationResult = () => { throw new TypeError("lost response"); };
  await trigger("worktree-create", "submit");
  assert.equal(JSON.parse(calls("/create").at(-1).options.body).ref, "HEAD");
  assert.match(status, /创建结果未确认.*不要重复提交/);
  await trigger("worktree-create", "submit");
  assert.equal(calls("/create").length, 2);

  // Unsafe previews and cancelled confirmation never POST delete.
  safe = false;
  await clickRemove();
  assert.match(status, /不能安全删除.*未保留的文件/);
  assert.equal(calls("/delete").length, 0);
  safe = true; confirmation = false;
  await clickRemove();
  assert.equal(calls("/delete").length, 0);
  assert.match(confirmations.at(-1), /保留会话历史与原工作目录/);
  assert.match(confirmations.at(-1), /binding-one.*native-one/);
  assert.match(confirmations.at(-1), /另有 1 个已知目录使用者.*不在级联清单/);
  assert.match(status, /已取消/);

  // The selected cascade flag only goes in preview; final delete uses its grant.
  confirmation = true;
  document.querySelector("#worktrees-body").querySelector("input").checked = true;
  disconnect = true;
  await clickRemove();
  assert.equal(JSON.parse(calls("/delete-preview").at(-1).options.body).deleteSessions, true);
  assert.match(confirmations.at(-1), /同时永久删除关联会话/);
  assert.equal(Object.hasOwn(JSON.parse(calls("/delete").at(-1).options.body), "deleteSessions"), false);
  assert.equal(calls("/delete").length, 1);
  assert.match(status, /删除结果未确认.*不要重复提交/);
  assert.doesNotMatch(status, /internal transport/);

  // A settings refresh never overwrites a draft typed after the GET started.
  controller.discard();
  let resolveSettings;
  settingsResult = () => new Promise((resolve) => { resolveSettings = resolve; });
  const refresh = controller.load();
  input("root").value = "/new/draft";
  await trigger("worktree-root", "input");
  resolveSettings(structuredClone(settings));
  assert.equal(await refresh, false);
  assert.equal(input("root").value, "/new/draft");
  assert.equal(controller.dirty(), true);
  expired = true; controller.expire();
  assert.equal(input("settings-save").disabled, true);
  assert.equal(input("create-submit").disabled, true);
})().catch((error) => { console.error(error); process.exitCode = 1; });
