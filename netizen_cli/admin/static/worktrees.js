"use strict";

// Management-only adapter: all Git and lifecycle decisions stay in the shared
// application service. Mutation grants are consumed once, never replayed.
window.NetizenWorktrees = (() => {
  const node = (name) => document.querySelector(`#worktree-${name}`);
  let dependencies;
  let settings = null;
  let branches = null;
  let pending = false;
  let serial = 0;
  let branchSerial = 0;
  let formSerial = 0;
  let edited = false;
  let cursor = null;
  let nextCursor = null;
  let previous = [];
  const stateLabels = { ready: "可用", available: "可用", missing: "目录缺失", unknown: "状态未取得",
    replaced: "路径身份已改变", creating: "创建中", failed: "创建未完成", deleting: "删除中" };

  function expired() { return !dependencies || dependencies.isExpired(); }
  function message(text, error = false) {
    node("result").textContent = text;
    node("result").classList.toggle("error", error);
    dependencies.setStatus(text, error);
  }
  function controls() {
    const disabled = expired() || pending;
    for (const form of [node("settings"), node("create")]) {
      for (const field of form.querySelectorAll("input, select, button")) field.disabled = disabled;
    }
    for (const field of node("settings").querySelectorAll("input")) field.disabled = disabled || !settings?.editable;
    node("settings-save").disabled = disabled || !settings?.actions?.save;
    const mode = node("mode").value;
    node("ref-label").hidden = mode === "existing";
    node("existing-label").hidden = mode !== "existing";
    node("new-label").hidden = mode !== "new";
    node("new-branch").required = mode === "new";
    node("existing").required = mode === "existing";
    node("create-submit").disabled = disabled || !branches?.actions?.create
      || (mode === "existing" && !node("existing").value);
    for (const button of document.querySelector("#worktrees-body").querySelectorAll("button, input")) button.disabled = disabled;
    document.querySelector("#worktrees-previous").disabled = disabled || !previous.length;
    document.querySelector("#worktrees-next").disabled = disabled || !nextCursor;
  }
  function option(select, value, label, disabled = false) {
    const item = document.createElement("option");
    item.value = value;
    item.textContent = label;
    item.disabled = disabled;
    select.append(item);
  }
  async function post(path, envelope, extra = {}) {
    return dependencies.api(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(dependencies.actionPayload(envelope, extra)) });
  }
  async function loadBranches() {
    const request = ++branchSerial;
    const project = node("project").value;
    branches = null;
    node("existing").replaceChildren();
    node("refs").replaceChildren();
    controls();
    if (!project || expired()) return;
    try {
      const result = await dependencies.api(`/api/v1/worktrees/branches?${new URLSearchParams({ project })}`);
      if (expired() || request !== branchSerial || node("project").value !== project) return;
      branches = result;
      option(node("existing"), "", "选择未被占用的本地分支");
      for (const branch of result.items) {
        option(node("refs"), branch.ref, branch.name);
        if (!branch.remote) option(node("existing"), branch.name,
          `${branch.name}${branch.occupiedPaths?.length ? "（已被其他 checkout 占用）" : ""}`, Boolean(branch.occupiedPaths?.length));
      }
      node("create-note").textContent = result.isGit === false ? "该 Project 不是 Git 仓库，不能创建 Worktree。"
        : "默认从此 Project 当前 HEAD 检出；不复制未提交、未跟踪或忽略文件，不执行环境初始化。不创建会话。";
    } catch (error) {
      if (request === branchSerial && !expired()) node("create-note").textContent = error.message;
    } finally {
      if (request === branchSerial) controls();
    }
  }
  function renderRows(items) {
    const body = document.querySelector("#worktrees-body");
    body.replaceChildren();
    for (const resource of items) {
      const row = document.createElement("tr");
      const cell = (text) => { const element = document.createElement("td"); element.textContent = text; row.append(element); return element; };
      cell(`${resource.projectAlias}${resource.orphaned ? "（原 Project 已删除）" : ""}\n${resource.id}`);
      cell(resource.cwd || resource.root);
      cell(`${stateLabels[resource.state] || resource.state || "状态未取得"}${resource.creationState && resource.creationState !== "ready" ? ` · ${resource.creationState}` : ""}${resource.state === "ready" && resource.available === false ? " · 不可用于新会话" : ""}`);
      cell(`${resource.head ? resource.branch || "detached" : "分支未取得"}\n${resource.head || "HEAD 未取得"}`);
      cell(resource.associatedSessionCount == null ? "未取得" : String(resource.associatedSessionCount));
      const actions = cell("");
      const choice = document.createElement("input"); choice.type = "checkbox";
      const label = document.createElement("label"); label.className = "cascade-choice";
      label.append(choice, document.createTextNode("同时删除关联会话"));
      actions.append(label);
      if (resource.actions?.previewDelete) {
        const button = document.createElement("button"); button.type = "button"; button.className = "action danger";
        button.textContent = resource.state === "missing" ? "清理缺失登记…" : "删除…";
        button.addEventListener("click", () => remove(resource, choice.checked));
        actions.append(button);
      }
      body.append(row);
    }
    if (!items.length) {
      const row = document.createElement("tr"); const empty = document.createElement("td");
      empty.colSpan = 6; empty.textContent = "本页没有受管 Worktree。"; row.append(empty); body.append(row);
    }
    document.querySelector("#worktrees-page").textContent = `第 ${previous.length + 1} 页 · ${items.length} 条`;
  }
  async function loadRows(candidate = cursor, history = previous) {
    const request = ++serial;
    const query = new URLSearchParams({ pageSize: "20" });
    if (candidate) query.set("cursor", candidate);
    const result = await dependencies.api(`/api/v1/worktrees?${query}`);
    if (expired() || request !== serial) return;
    cursor = candidate; previous = history; nextCursor = result.nextCursor;
    renderRows(result.items); controls();
  }
  async function load() {
    if (expired() || pending) return false;
    if (edited && !window.confirm("放弃未保存的 Worktree 表单内容并刷新？")) return false;
    edited = false;
    const request = ++formSerial;
    const [value, projects] = await Promise.all([
      dependencies.api("/api/v1/worktrees/settings"), dependencies.queryProjectOptions(), loadRows(),
    ]);
    if (expired() || request !== formSerial) return false;
    settings = value;
    node("root").value = value.root || "";
    node("fetch").checked = value.fetchBeforeCreate;
    node("effective-root").textContent = `实际新建根目录：${value.effectiveRoot || value.root}${value.editable ? "" : "（当前配置只读）"}`;
    const selected = node("project").value;
    node("project").replaceChildren(); option(node("project"), "", "选择 Project");
    for (const project of projects.filter((item) => item.enabled)) option(node("project"), project.alias, project.alias);
    node("project").value = projects.some((item) => item.enabled && item.alias === selected) ? selected : "";
    node("mode").value = "detached"; node("ref").value = ""; node("new-branch").value = "";
    await loadBranches(); controls();
    return true;
  }
  async function saveSettings(event) {
    event.preventDefault();
    if (expired() || pending || !settings?.actions?.save) return;
    const grant = settings.actions.save; settings.actions.save = null;
    const extra = { root: node("root").value.trim(), fetchBeforeCreate: node("fetch").checked };
    pending = true; controls();
    try {
      await post("/api/v1/worktrees/settings", grant, extra);
      edited = false; message("Worktree 配置已保存；已有目录未移动。");
    } catch (error) { message(error.status ? error.message : "配置保存结果未确认；请刷新查看，不要重复提交。", true); }
    finally { pending = false; controls(); }
    if (!expired() && !edited) {
      try { await load(); }
      catch { message(`${node("result").textContent}\n配置刷新失败，请手动刷新查看当前值。`, true); }
    }
  }
  async function create(event) {
    event.preventDefault();
    if (expired() || pending || !branches?.actions?.create) return;
    const mode = node("mode").value;
    const branch = mode === "existing" ? node("existing").value : mode === "new" ? node("new-branch").value.trim() : null;
    const ref = mode === "existing" ? `refs/heads/${branch}` : node("ref").value.trim() || "HEAD";
    const grant = branches.actions.create; branches.actions.create = null;
    pending = true; controls(); message("正在创建 Worktree…");
    try {
      const result = await post("/api/v1/worktrees/create", grant, { mode, branch, ref });
      edited = false; message(`Worktree 已创建：${result.resource.cwd}。可在飞书 /new 中选择；尚未创建会话。`);
    } catch (error) { message(error.status ? error.message : "创建结果未确认，请刷新 Worktrees 查看；不要重复提交。", true); }
    finally { pending = false; controls(); }
    // Read-only reconciliation may discover a registered resource after a lost
    // response. Never repeat the create request or remove a successful checkout.
    if (!expired()) { try { await loadRows(); } catch { message(`${node("result").textContent}\n列表刷新失败，请手动刷新查看创建结果。`, true); } }
  }
  async function remove(resource, deleteSessions) {
    if (expired() || pending || !resource.actions?.previewDelete) return;
    const grant = resource.actions.previewDelete; resource.actions.previewDelete = null;
    pending = true; controls(); let submitted = false;
    try {
      const preview = await post("/api/v1/worktrees/delete-preview", grant, { deleteSessions });
      if (expired()) return;
      if (!preview.safe || !preview.actions?.delete) {
        message(`不能安全删除：${(preview.reasons || []).map((reason) => typeof reason === "string" ? reason : reason.message || reason.code).join("；") || "状态未取得"}。请先处理后重新预览。`, true); return;
      }
      const bindings = preview.bindings || [];
      const identity = bindings.map((binding) => `${binding.shortId || binding.bindingId || binding.id}${binding.nativeThreadId ? ` (${binding.nativeThreadId})` : ""}`).join("、");
      const protectedNotice = preview.protectedSessionCount ? `\n另有 ${preview.protectedSessionCount} 个已知目录使用者，不在级联清单中，将保留其会话与原 cwd。` : "";
      if (!window.confirm(`${preview.resource.state === "missing" ? "清理已缺失 Worktree 的登记" : "删除受管 Worktree"}？\ncheckout 根目录：${preview.resource.root || resource.root}\n会话工作目录：${preview.resource.cwd || resource.cwd}\n\n本实例关联会话：${bindings.length}${identity ? `\n${identity}` : ""}\n${preview.deleteSessions ? "同时永久删除关联会话及原生派生历史，无法恢复。" : "保留会话历史与原工作目录；目录删除后继续请求可能失败，不自动切回项目目录。"}${protectedNotice}\n仅依据本实例已知引用，无法排除外部客户端使用。目录没有快照恢复；操作会复核安全状态，部分成功不回滚。`)) {
        message("已取消，未提交删除。"); return;
      }
      submitted = true;
      const result = await post("/api/v1/worktrees/delete", preview.actions.delete);
      if (result.deleted) message(`Worktree 删除已确认。已删除 ${result.deletedSessionCount || 0} 个关联会话；保留 ${result.retainedSessionCount || 0} 个关联会话。`);
      else message(`删除尚未完成（${result.code || "结果未确认"}）；已确认删除 ${result.deletedSessionCount || 0} 个关联会话，剩余 ${result.remainingSessionCount ?? "未取得"} 个。\n${result.message || ""}\n已成功项不回滚。请刷新检查剩余会话与目录后重新确认。`, true);
    } catch (error) {
      message(submitted && (!error.status || error.status >= 500)
        ? "删除结果未确认，服务端可能仍在处理；请手动刷新检查，不要重复提交。"
        : error.status ? error.message : "无法读取删除范围，未提交删除。", true);
    } finally {
      pending = false;
      if (!expired()) { try { await loadRows(); } catch { message(`${node("result").textContent}\nWorktrees 刷新失败，请手动刷新检查。`, true); } }
      controls();
    }
  }
  function discard() { edited = false; formSerial += 1; branchSerial += 1; }
  function init(values) {
    dependencies = values;
    node("project").addEventListener("change", () => { edited = true; formSerial += 1; return loadBranches(); });
    node("mode").addEventListener("change", () => { edited = true; formSerial += 1; controls(); });
    for (const name of ["root", "fetch", "ref", "existing", "new-branch"]) {
      node(name).addEventListener("input", () => { edited = true; formSerial += 1; controls(); });
    }
    node("settings").addEventListener("submit", saveSettings);
    node("create").addEventListener("submit", create);
    for (const direction of ["previous", "next"]) document.querySelector(`#worktrees-${direction}`).addEventListener("click", async () => {
      if (pending || expired()) return;
      const candidate = direction === "next" ? nextCursor : previous.at(-1);
      if (direction === "next" ? !nextCursor : !previous.length) return;
      try { await loadRows(candidate, direction === "next" ? [...previous, cursor] : previous.slice(0, -1)); }
      catch (error) { message(error.message, true); }
    });
    controls();
  }
  return { init, load, dirty: () => edited, busy: () => pending, discard,
    expire() { serial += 1; branchSerial += 1; formSerial += 1; controls(); } };
})();
