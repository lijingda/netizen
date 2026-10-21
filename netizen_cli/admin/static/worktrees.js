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
  let editor = null;
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
  function editorMessage(text, error = false) {
    if (!editor) return;
    const status = node(`${editor.kind}-status`);
    status.textContent = text;
    status.classList.toggle("error", error);
  }
  function values(kind) {
    return kind === "settings"
      ? JSON.stringify([node("root").value, node("fetch").checked])
      : JSON.stringify([node("project").value, node("mode").value, node("ref").value,
        node("existing").value, node("new-branch").value]);
  }
  function dirty() { return Boolean(editor && !editor.loading && values(editor.kind) !== editor.base); }
  function controls() {
    const disabled = expired() || pending;
    for (const kind of ["settings", "create"]) {
      const active = editor?.kind === kind;
      const blocked = disabled || !active || editor.loading || editor.checking;
      for (const field of node(kind).querySelectorAll("input, select, button")) field.disabled = blocked;
      node(`${kind}-close`).disabled = disabled;
      node(`${kind}-cancel`).disabled = disabled;
      node(`${kind}-check`).hidden = !active || !editor.checkAvailable;
      node(`${kind}-check`).disabled = disabled || !active || editor.loading || editor.checking;
      node(kind).setAttribute("aria-busy", String(active && (pending || editor.loading || editor.checking)));
    }
    const settingsBlocked = disabled || editor?.kind !== "settings" || editor.loading || editor.checking;
    for (const field of node("settings").querySelectorAll("input")) field.disabled = settingsBlocked || !settings?.editable;
    node("settings-save").disabled = settingsBlocked || editor.uncertain || !settings?.actions?.save;
    node("settings-save").textContent = pending && editor?.kind === "settings" ? "正在保存…" : "保存配置";
    const mode = node("mode").value;
    node("ref-label").hidden = mode === "existing";
    node("existing-label").hidden = mode !== "existing";
    node("new-label").hidden = mode !== "new";
    node("new-branch").required = mode === "new";
    node("existing").required = mode === "existing";
    node("create-submit").disabled = disabled || editor?.kind !== "create" || editor.loading || editor.checking
      || editor.uncertain || !branches?.actions?.create || (mode === "existing" && !node("existing").value);
    node("create-submit").textContent = pending && editor?.kind === "create" ? "正在创建…" : "创建 worktree";
    node("new").disabled = disabled;
    node("configure").disabled = disabled;
    for (const button of document.querySelector("#worktrees-body").querySelectorAll("button, input")) button.disabled = disabled;
    document.querySelector("#worktrees-previous").disabled = disabled || !previous.length;
    document.querySelector("#worktrees-next").disabled = disabled || !nextCursor;
  }
  function closeEditor({ saved = false, confirmed = false } = {}) {
    if (pending) return false;
    if (!editor) return true;
    if (!saved && !confirmed && dirty() && !window.confirm("有未保存的修改，确定放弃吗？")) return false;
    const current = editor;
    editor = null;
    branchSerial += 1;
    branches = null;
    settings = null;
    window.NetizenSelect?.close();
    node(current.kind).hidden = true;
    node(`${current.kind}-drawer`).close();
    document.body.classList.toggle("workspace-drawer-open", false);
    (document.body.contains(current.returnFocus) ? current.returnFocus : node(current.kind === "create" ? "new" : "configure")).focus();
    controls();
    return true;
  }
  async function openEditor(kind, returnFocus) {
    if (expired() || pending || !closeEditor()) return false;
    const current = { kind, returnFocus, loading: true, checking: false, uncertain: false, checkAvailable: false, base: null };
    editor = current;
    settings = null;
    branches = null;
    if (kind === "settings") {
      node("root").value = ""; node("fetch").checked = false;
      node("effective-root").textContent = "";
    } else {
      node("project").replaceChildren(); option(node("project"), "", "选择 Project"); node("project").value = "";
      node("mode").value = "detached"; node("ref").value = ""; node("new-branch").value = "";
      node("existing").replaceChildren(); node("existing").value = ""; node("refs").replaceChildren();
      node("create-note").textContent = ""; node("create-config").textContent = "";
    }
    current.base = values(kind);
    editorMessage("正在读取…");
    controls();
    node(kind).hidden = false;
    node(`${kind}-drawer`).showModal();
    document.body.classList.toggle("workspace-drawer-open", true);
    node(`${kind}-drawer`).querySelector(".drawer-body").scrollTop = 0;
    node(`${kind}-close`).focus();
    try {
      if (kind === "settings") {
        const value = await dependencies.api("/api/v1/worktrees/settings");
        if (expired() || editor !== current) return false;
        settings = value;
        node("root").value = value.root || "";
        node("fetch").checked = value.fetchBeforeCreate;
        node("effective-root").textContent = `实际新建根目录：${value.effectiveRoot || value.root}${value.editable ? "" : "（当前配置只读）"}`;
      } else {
        const [value, projects] = await Promise.all([
          dependencies.api("/api/v1/worktrees/settings"), dependencies.queryProjectOptions(),
        ]);
        if (expired() || editor !== current) return false;
        for (const project of projects.filter((item) => item.enabled)) option(node("project"), project.alias, project.alias);
        node("create-config").textContent = `新建根目录：${value.effectiveRoot || value.root}\n创建前 fetch：${value.fetchBeforeCreate ? "开启" : "关闭"}`;
        node("create-note").textContent = "只检出已提交内容，不执行环境初始化。创建后可在飞书 /new 中选择；不创建会话。";
      }
      current.base = values(kind);
      editorMessage("");
    } catch (error) {
      if (!expired() && editor === current) editorMessage(error.message, true);
    } finally {
      if (editor === current) {
        current.loading = false;
        controls();
        const first = node(kind === "settings" ? "root" : "project");
        if (!first.disabled && document.activeElement === node(`${kind}-close`)) first.focus();
      }
    }
    return editor === current && !expired();
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
    const current = editor;
    if (current?.kind !== "create" || current.loading || pending || expired()) return;
    const request = ++branchSerial;
    const project = node("project").value;
    branches = null;
    node("existing").replaceChildren(); node("existing").value = "";
    node("refs").replaceChildren();
    controls();
    if (!project || expired()) return;
    try {
      const result = await dependencies.api(`/api/v1/worktrees/branches?${new URLSearchParams({ project })}`);
      if (expired() || editor !== current || request !== branchSerial || node("project").value !== project) return;
      branches = result;
      option(node("existing"), "", "选择未被占用的本地分支");
      for (const branch of result.items) {
        option(node("refs"), branch.ref, branch.name);
        if (!branch.remote) option(node("existing"), branch.name,
          `${branch.name}${branch.occupiedPaths?.length ? "（已被其他 checkout 占用）" : ""}`, Boolean(branch.occupiedPaths?.length));
      }
      node("create-note").textContent = result.isGit === false ? "该 Project 不是 Git 仓库，不能创建 worktree。"
        : "默认从此 Project 当前 HEAD 检出；不复制未提交、未跟踪或忽略文件，不执行环境初始化。不创建会话。";
    } catch (error) {
      if (editor === current && request === branchSerial && !expired()) node("create-note").textContent = error.message;
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
      empty.colSpan = 6; empty.textContent = "本页没有受管 worktree。";
      const button = document.createElement("button"); button.type = "button"; button.className = "action";
      button.textContent = "新建 worktree";
      button.addEventListener("click", () => openEditor("create", node("new")));
      empty.append(button); row.append(empty); body.append(row);
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
    await loadRows();
    return !expired();
  }
  async function checkResult() {
    const current = editor;
    if (!current || !current.checkAvailable || current.loading || current.checking || expired() || pending) return;
    current.checking = true; controls();
    try {
      if (current.kind === "settings") {
        const value = await dependencies.api("/api/v1/worktrees/settings");
        if (expired() || editor !== current) return;
        editorMessage(`已读取当前配置：新建根目录 ${value.effectiveRoot || value.root}；创建前 fetch：${value.fetchBeforeCreate ? "开启" : "关闭"}。请核对保存结果，当前输入已保留。`);
      } else {
        await loadRows();
        if (expired() || editor !== current) return;
        editorMessage("列表已刷新。请关闭抽屉核对 worktree；当前输入已保留，未重复提交创建。", current.uncertain);
      }
    } catch (error) {
      if (!expired() && editor === current) editorMessage(`刷新核对失败：${error.message}`, true);
    } finally {
      if (editor === current) { current.checking = false; controls(); }
    }
  }
  async function saveSettings(event) {
    event.preventDefault();
    const current = editor;
    if (expired() || pending || current?.kind !== "settings" || current.loading || current.checking
        || current.uncertain || !settings?.actions?.save) return;
    const grant = settings.actions.save; settings.actions.save = null;
    const extra = { root: node("root").value.trim(), fetchBeforeCreate: node("fetch").checked };
    pending = true; controls(); editorMessage("正在保存配置…");
    let saved = false;
    try {
      await post("/api/v1/worktrees/settings", grant, extra);
      saved = true;
    } catch (error) {
      if (!expired() && editor === current) {
        current.uncertain = !error.status || error.status >= 500;
        current.checkAvailable = true;
        editorMessage(current.uncertain ? "配置保存结果未确认；请刷新核对，不要重复提交。" : error.message, true);
        message(node("settings-status").textContent, true);
      }
    } finally { pending = false; controls(); }
    if (saved && !expired() && editor === current) {
      closeEditor({ saved: true }); message("worktree 配置已保存；已有目录未移动。");
    }
  }
  async function create(event) {
    event.preventDefault();
    const current = editor;
    if (expired() || pending || current?.kind !== "create" || current.loading || current.checking
        || current.uncertain || !branches?.actions?.create) return;
    const mode = node("mode").value;
    const branch = mode === "existing" ? node("existing").value : mode === "new" ? node("new-branch").value.trim() : null;
    const ref = mode === "existing" ? `refs/heads/${branch}` : node("ref").value.trim() || "HEAD";
    const grant = branches.actions.create; branches.actions.create = null;
    pending = true; controls(); editorMessage("正在创建 worktree…");
    let result;
    try {
      result = await post("/api/v1/worktrees/create", grant, { mode, branch, ref });
    } catch (error) {
      if (!expired() && editor === current) {
        current.uncertain = !error.status || error.status >= 500;
        current.checkAvailable = true;
        editorMessage(current.uncertain ? "创建结果未确认，请刷新核对 worktree；不要重复提交。" : error.message, true);
        message(node("create-status").textContent, true);
      }
    } finally { pending = false; controls(); }
    if (result && !expired() && editor === current) {
      closeEditor({ saved: true });
      message(`worktree 已创建：${result.resource.cwd}。可在飞书 /new 中选择；尚未创建会话。`);
    }
    // Read-only reconciliation never repeats the create request or reissues a
    // consumed grant, including when the original response was lost.
    if (!expired()) {
      try { await loadRows(); }
      catch {
        if (editor === current) editorMessage(`${node("create-status").textContent}\n列表刷新失败，请手动刷新核对创建结果。`, true);
        else message(`${node("result").textContent}\n列表刷新失败，请手动刷新查看创建结果。`, true);
      }
    }
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
      if (!window.confirm(`${preview.resource.state === "missing" ? "清理已缺失 worktree 的登记" : "删除受管 worktree"}？\ncheckout 根目录：${preview.resource.root || resource.root}\n会话工作目录：${preview.resource.cwd || resource.cwd}\n\n本实例关联会话：${bindings.length}${identity ? `\n${identity}` : ""}\n${preview.deleteSessions ? "同时永久删除关联会话及原生派生历史，无法恢复。" : "保留会话历史与原工作目录；目录删除后继续请求可能失败，不自动切回项目目录。"}${protectedNotice}\n仅依据本实例已知引用，无法排除外部客户端使用。目录没有快照恢复；操作会复核安全状态，部分成功不回滚。`)) {
        message("已取消，未提交删除。"); return;
      }
      submitted = true;
      const result = await post("/api/v1/worktrees/delete", preview.actions.delete);
      if (result.deleted) message(`worktree 删除已确认。已删除 ${result.deletedSessionCount || 0} 个关联会话；保留 ${result.retainedSessionCount || 0} 个关联会话。`);
      else message(`删除尚未完成（${result.code || "结果未确认"}）；已确认删除 ${result.deletedSessionCount || 0} 个关联会话，剩余 ${result.remainingSessionCount ?? "未取得"} 个。\n${result.message || ""}\n已成功项不回滚。请刷新检查剩余会话与目录后重新确认。`, true);
    } catch (error) {
      message(submitted && (!error.status || error.status >= 500)
        ? "删除结果未确认，服务端可能仍在处理；请手动刷新检查，不要重复提交。"
        : error.status ? error.message : "无法读取删除范围，未提交删除。", true);
    } finally {
      pending = false;
      if (!expired()) { try { await loadRows(); } catch { message(`${node("result").textContent}\nworktree 刷新失败，请手动刷新检查。`, true); } }
      controls();
    }
  }
  function discard() { return closeEditor({ confirmed: true }); }
  function init(values) {
    dependencies = values;
    node("new").addEventListener("click", (event) => openEditor("create", event.currentTarget));
    node("configure").addEventListener("click", (event) => openEditor("settings", event.currentTarget));
    node("project").addEventListener("change", loadBranches);
    for (const name of ["mode", "root", "fetch", "ref", "existing", "new-branch"]) {
      node(name).addEventListener(name === "mode" ? "change" : "input", controls);
    }
    for (const kind of ["settings", "create"]) {
      node(`${kind}-close`).addEventListener("click", () => closeEditor());
      node(`${kind}-cancel`).addEventListener("click", () => closeEditor());
      node(`${kind}-drawer`).addEventListener("cancel", (event) => { event.preventDefault(); closeEditor(); });
      node(`${kind}-check`).addEventListener("click", checkResult);
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
  return { init, load, dirty, busy: () => pending, discard,
    expire() { serial += 1; branchSerial += 1; controls(); } };
})();
