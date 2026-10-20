const requests = [];
const confirmations = [];
let confirmation = true;
let expired = false;
let status = "";
let safe = true;
let disconnect = false;
let creationResult;
let settingsResult;
let settingsSave;
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
    return settingsSave ? settingsSave() : structuredClone(settings);
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
  assert.equal(calls("/settings").length, 0, "list refresh does not initialize hidden editors");
  assert.equal(input("create").hidden, true);
  assert.equal(input("settings").hidden, true);
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

  // Create and settings are separate dialogs. Loading the list preserves drafts.
  await trigger("worktree-new");
  assert.equal(input("create-drawer").open, true);
  assert.equal(input("create").hidden, false);
  assert.equal(input("settings").hidden, true);
  assert.equal(document.activeElement, input("project"));
  assert.equal(input("mode").value, "detached");
  assert.match(input("create-config").textContent, /\/instance\/worktrees/);
  assert.match(input("create-config").textContent, /fetch：关闭/);
  assert.equal(input("project").children.length, 3);
  assert.equal(controller.dirty(), false);
  input("ref").value = "main";
  await trigger("worktree-ref", "input");
  assert.equal(controller.dirty(), true);
  const reads = calls("/settings").length;
  await controller.load();
  assert.equal(input("ref").value, "main");
  assert.equal(calls("/settings").length, reads);
  input("ref").value = "";
  await trigger("worktree-ref", "input");
  assert.equal(controller.dirty(), false, "reverting a value removes the discard prompt");
  const confirms = confirmations.length;
  await trigger("worktree-create-cancel");
  assert.equal(confirmations.length, confirms);
  assert.equal(input("create-drawer").open, false);
  assert.equal(document.activeElement, input("new"));

  await trigger("worktree-new");
  input("project").value = "demo";
  await trigger("worktree-project", "change");
  assert.equal(input("create-submit").disabled, false);
  assert.equal(input("existing").children.find((item) => item.value === "main").disabled, true);
  assert.equal(input("existing").children.some((item) => item.value === "origin/main"), false);
  confirmation = false;
  assert.equal(await trigger("worktree-configure"), false);
  assert.equal(input("create-drawer").open, true);
  assert.equal(input("settings").hidden, true);
  const cancelled = input("create-drawer").dispatch("cancel");
  assert.equal(cancelled.prevented, true);
  assert.equal(input("create-drawer").open, true);
  confirmation = true;
  input("mode").value = "existing";
  await trigger("worktree-mode", "change");
  assert.equal(input("ref-label").hidden, true);
  assert.equal(input("existing-label").hidden, false);
  input("existing").value = "feature";
  await trigger("worktree-existing", "input");
  let resolveCreate;
  creationResult = () => new Promise((resolve) => { resolveCreate = resolve; });
  const creating = trigger("worktree-create", "submit");
  assert.equal(controller.busy(), true);
  assert.equal(input("create-close").disabled, true);
  assert.equal(input("create-cancel").disabled, true);
  assert.equal(input("create-submit").disabled, true);
  assert.match(input("create-submit").textContent, /正在创建/);
  assert.equal(controller.discard(), false, "navigation cannot abandon an in-flight mutation");
  assert.equal(input("create-drawer").dispatch("cancel").prevented, true);
  assert.equal(input("create-drawer").open, true);
  await trigger("worktree-create", "submit");
  assert.equal(calls("/create").length, 1);
  resolveCreate({ resource: row() });
  await creating;
  const creation = JSON.parse(calls("/create").at(-1).options.body);
  assert.equal(creation.mode, "existing");
  assert.equal(creation.branch, "feature");
  assert.equal(creation.ref, "refs/heads/feature");
  assert.match(status, /尚未创建会话/);
  assert.equal(input("create-drawer").open, false);
  assert.equal(controller.dirty(), false);
  assert.equal(document.activeElement, input("new"));
  await trigger("worktree-create", "submit");
  assert.equal(calls("/create").length, 1);

  // Late branch results cannot create a grant for the old Project.
  await trigger("worktree-new");
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
  assert.match(input("create-status").textContent, /创建结果未确认.*不要重复提交/);
  assert.equal(input("create-drawer").open, true);
  assert.equal(input("new-branch").value, "feature-new");
  assert.equal(input("create-check").hidden, false);
  await trigger("worktree-create-check");
  assert.match(input("create-status").textContent, /列表已刷新/);
  input("project").value = "demo";
  branchResults.delete("demo");
  await trigger("worktree-project", "change");
  assert.equal(input("create-submit").disabled, true, "new branch reads cannot retry an unknown mutation");
  await trigger("worktree-create", "submit");
  assert.equal(calls("/create").length, 2);
  controller.discard();
  assert.equal(input("create-drawer").open, false);

  // Settings load independently, honor read-only state and use their own draft.
  await trigger("worktree-configure");
  assert.equal(input("settings-drawer").open, true);
  assert.equal(input("root").value, "");
  assert.equal(input("fetch").checked, false);
  assert.match(input("effective-root").textContent, /\/instance\/worktrees/);
  assert.equal(controller.dirty(), false);
  input("root").value = "/new/worktrees";
  input("fetch").checked = true;
  await trigger("worktree-root", "input");
  assert.equal(controller.dirty(), true);
  let resolveSave;
  settingsSave = () => new Promise((resolve) => { resolveSave = resolve; });
  const saved = trigger("worktree-settings", "submit");
  assert.equal(input("settings-close").disabled, true);
  assert.equal(input("settings-save").disabled, true);
  assert.equal(controller.busy(), true);
  const settingWrite = calls("/settings").filter((request) => request.options.method === "POST");
  assert.equal(settingWrite.length, 1);
  assert.deepEqual(JSON.parse(settingWrite[0].options.body), {
    ...grant("settings"), root: "/new/worktrees", fetchBeforeCreate: true,
  });
  resolveSave(structuredClone(settings));
  await saved;
  assert.equal(input("settings-drawer").open, false);
  assert.equal(controller.dirty(), false);
  assert.equal(document.activeElement, input("configure"));
  assert.match(status, /配置已保存/);
  assert.equal(input("new-branch").value, "feature-new", "saving settings never resets the other form");

  await trigger("worktree-configure");
  input("root").value = "/unknown/save";
  await trigger("worktree-root", "input");
  settingsSave = () => { throw new TypeError("lost settings result"); };
  await trigger("worktree-settings", "submit");
  assert.match(input("settings-status").textContent, /结果未确认.*不要重复提交/);
  await trigger("worktree-settings-check");
  assert.match(input("settings-status").textContent, /已读取当前配置.*fetch：关闭/);
  assert.equal(input("root").value, "/unknown/save");
  assert.equal(input("settings-save").disabled, true);
  await trigger("worktree-settings", "submit");
  assert.equal(calls("/settings").filter((request) => request.options.method === "POST").length, 2);
  controller.discard();

  settingsResult = () => ({ ...structuredClone(settings), editable: false, actions: {} });
  await trigger("worktree-configure");
  assert.equal(input("root").disabled, true);
  assert.equal(input("fetch").disabled, true);
  assert.equal(input("settings-save").disabled, true);
  await trigger("worktree-new");
  assert.equal(input("settings-drawer").open, false);
  input("project").value = "demo";
  await trigger("worktree-project", "change");
  assert.equal(input("create-submit").disabled, false, "read-only settings still allow creation");
  creationResult = () => { const error = new Error("分支名称不可用"); error.status = 400; throw error; };
  await trigger("worktree-create", "submit");
  assert.match(input("create-status").textContent, /分支名称不可用/);
  assert.equal(input("project").value, "demo");
  assert.equal(input("mode").value, "detached");
  const detached = JSON.parse(calls("/create").at(-1).options.body);
  assert.equal(detached.branch, null);
  assert.equal(detached.ref, "HEAD");
  controller.discard();
  settingsResult = null;

  // A dismissed dialog's late GET never overwrites a newer draft.
  let resolveSettings;
  settingsResult = () => new Promise((resolve) => { resolveSettings = resolve; });
  const opening = trigger("worktree-configure");
  assert.equal(input("root").disabled, true);
  controller.discard();
  settingsResult = null;
  await trigger("worktree-configure");
  input("root").value = "/new/draft";
  await trigger("worktree-root", "input");
  resolveSettings({ ...structuredClone(settings), root: "/old/value" });
  assert.equal(await opening, false);
  assert.equal(input("root").value, "/new/draft");
  assert.equal(controller.dirty(), true);
  controller.discard();

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

  // Empty lists offer the same drawer and expiration revokes every submission.
  rows = [];
  await controller.load();
  await rowAction().listeners.get("click")[0]();
  assert.equal(input("create-drawer").open, true);
  input("project").value = "demo";
  await trigger("worktree-project", "change");
  branchResults.set("other", () => ({ isGit: false, items: [], actions: {} }));
  input("project").value = "other";
  await trigger("worktree-project", "change");
  assert.match(input("create-note").textContent, /不是 Git 仓库/);
  assert.equal(input("create-submit").disabled, true);
  expired = true; controller.expire();
  assert.equal(input("settings-save").disabled, true);
  assert.equal(input("create-submit").disabled, true);
  assert.equal(input("new").disabled, true);
  assert.equal(input("configure").disabled, true);
})().catch((error) => { console.error(error); process.exitCode = 1; });
