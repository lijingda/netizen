"use strict";

// A read-only view of one explicit workspace. Requests never outlive that
// selection; the server owns paths, identity validation and preview limits.
window.NetizenProjectFiles = (() => {
  const node = (name) => document.querySelector(`#project-files-${name}`);
  const modes = new Set(["python", "json", "javascript", "markdown", "text"]);
  let dependencies;
  let current = null;
  let aceLoading = null;

  function active(view) { return current === view && !dependencies.isExpired(); }
  function note(name, text, error = false) {
    node(name).textContent = text;
    node(name).classList.toggle("error", error);
  }
  function stop(view, slot) {
    view.requests[slot]?.abort();
    delete view.requests[slot];
  }
  function stopAll(view) {
    for (const slot of Object.keys(view.requests)) stop(view, slot);
  }
  function clearFile(view) {
    view.file = null;
    view.fileSequence = (view.fileSequence || 0) + 1;
    view.preview = false;
    if (view.editor) {
      view.editor.setValue("", -1);
      view.editor.destroy();
      view.editor = null;
    }
    node("editor").replaceChildren();
    node("editor").hidden = true;
    node("markdown").replaceChildren();
    node("markdown").hidden = true;
    node("image").onerror = null;
    node("image").removeAttribute("src");
    node("image").alt = "";
    node("image-wrap").hidden = true;
    node("tools").hidden = true;
    node("filename").textContent = "选择文件查看";
    node("meta").textContent = "";
    node("search").value = "";
    node("line").value = "1";
    node("empty").hidden = false;
    note("note", "");
  }
  function clearRoot(view) {
    for (const slot of Object.keys(view.requests)) if (slot !== "worktrees") stop(view, slot);
    view.treeEpoch += 1;
    view.navigation += 1;
    view.tree.clear();
    view.identity = null;
    view.desiredFile = null;
    view.path = "";
    view.selectedPath = "";
    view.focusedPath = "";
    view.filter = "";
    node("filter").value = "";
    clearFile(view);
    node("entries").replaceChildren();
    node("breadcrumbs").replaceChildren();
    node("cwd").textContent = "";
  }
  function invalidateRoot(view, error) {
    clearRoot(view);
    view.tree.set("", { entries: null, error: error.message });
    renderEntries(view);
  }
  function close() {
    const view = current;
    if (!view) return;
    current = null;
    stopAll(view);
    clearFile(view);
    view.tree.clear();
    node("filter").value = "";
    node("root").replaceChildren();
    node("entries").replaceChildren();
    node("breadcrumbs").replaceChildren();
    node("cwd").textContent = "";
    note("root-note", "");
    note("directory-note", "");
    window.NetizenSelect?.close();
    if (node("drawer").open) node("drawer").close();
    document.body.classList.toggle("project-files-open", false);
    if (!dependencies.isExpired() && view.returnFocus?.isConnected !== false) view.returnFocus?.focus();
  }
  function query(view, path) {
    const params = new URLSearchParams({ project: view.project.alias });
    if (view.rootId !== "project") params.set("worktree", view.rootId);
    if (view.identity) params.set("identity", view.identity);
    if (path !== undefined) params.set("path", path);
    return params;
  }
  async function request(view, slot, url) {
    stop(view, slot);
    const controller = new AbortController();
    view.requests[slot] = controller;
    try {
      const data = await dependencies.api(url, { signal: controller.signal });
      if (!active(view) || view.requests[slot] !== controller) return null;
      return data;
    } catch (error) {
      if (!active(view) || view.requests[slot] !== controller || error.name === "AbortError") return null;
      if (error.code === "root_changed") { invalidateRoot(view, error); return null; }
      throw error;
    } finally {
      if (view.requests[slot] === controller) delete view.requests[slot];
    }
  }
  function button(label, action, className = "") {
    const result = document.createElement("button");
    result.type = "button";
    result.textContent = label;
    result.className = className;
    result.addEventListener("click", action);
    return result;
  }
  function renderBreadcrumbs(view) {
    const crumbs = node("breadcrumbs");
    crumbs.replaceChildren(button("根目录", () => directory(view, "")));
    let path = "";
    for (const part of view.path.split("/").filter(Boolean)) {
      path += `${path ? "/" : ""}${part}`;
      const target = path;
      crumbs.append(document.createTextNode(" / "), button(part, () => directory(view, target)));
    }
  }
  function sizeLabel(size) {
    if (size == null) return "";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
  }
  const MAX_TREE_ENTRIES = 10000;
  function parentPath(path) { return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ""; }
  function treeRows() { return Array.from(node("entries").querySelectorAll("button[data-path]")); }
  function focusTree(view, path) {
    view.focusedPath = path;
    for (const row of treeRows()) row.tabIndex = row.dataset.path === path && !row.disabled ? 0 : -1;
    const row = treeRows().find((item) => item.dataset.path === path);
    row?.focus();
    row?.scrollIntoView?.({ block: "nearest" });
  }
  function iconKind(entry) {
    if (entry.kind !== "file") return entry.kind === "directory" ? "directory" : "unsupported";
    if (/\.(png|jpe?g|gif|webp)$/i.test(entry.name)) return "image";
    if (/\.(md|markdown)$/i.test(entry.name)) return "markdown";
    if (/\.(py|pyw|js|mjs|cjs|json|ts|tsx|jsx|html|css|sh|go|rs|java|yaml|yml|toml)$/i.test(entry.name)) return "code";
    return "file";
  }
  function renderEntries(view) {
    const entriesNode = node("entries");
    const restoreFocus = entriesNode.contains(document.activeElement);
    const oldScroll = entriesNode.parentElement.scrollTop;
    const fragment = document.createDocumentFragment();
    const visibleRows = [];
    const root = view.tree.get("");
    const filter = view.filter.trim().toLocaleLowerCase();
    const matches = new Set();
    if (filter) {
      for (const branch of view.tree.values()) for (const entry of branch.entries || []) {
        if (!entry.path.toLocaleLowerCase().includes(filter)) continue;
        let path = entry.path;
        while (path) { matches.add(path); path = parentPath(path); }
      }
    }
    function hint(text, depth, error = false) {
      const message = document.createElement("div");
      message.className = `project-files-tree-note${error ? " error" : ""}`;
      message.style.setProperty("--depth", depth);
      message.textContent = text;
      fragment.append(message);
    }
    function appendBranch(branch, depth) {
      const entries = (branch.entries || []).filter((entry) => !filter || matches.has(entry.path));
      for (const [index, entry] of entries.entries()) {
        const directoryEntry = entry.kind === "directory";
        const child = directoryEntry ? view.tree.get(entry.path) : null;
        const expanded = directoryEntry && (filter ? Boolean(child?.entries || child?.loading || child?.error) : Boolean(child?.expanded));
        const row = button("", () => directoryEntry ? toggleDirectory(view, entry.path) : content(view, entry.path), "project-files-entry");
        row.disabled = !directoryEntry && entry.kind !== "file";
        row.dataset.path = entry.path;
        row.dataset.kind = entry.kind;
        row.style.setProperty("--depth", depth);
        row.setAttribute("role", "treeitem");
        row.setAttribute("aria-level", String(depth + 1));
        row.setAttribute("aria-posinset", String(index + 1));
        row.setAttribute("aria-setsize", String(entries.length));
        row.setAttribute("aria-selected", String(view.selectedPath === entry.path));
        row.setAttribute("aria-current", String(view.file?.path === entry.path));
        row.setAttribute("aria-label", entry.name);
        if (directoryEntry) row.setAttribute("aria-expanded", String(expanded));
        if (child?.loading) row.setAttribute("aria-busy", "true");
        row.title = entry.kind === "symlink" ? `${entry.path} · 符号链接，不跟随`
          : entry.kind === "unsupported" ? `${entry.path} · 不支持预览` : entry.path;
        const chevron = document.createElement("span");
        chevron.className = "project-files-chevron";
        chevron.setAttribute("aria-hidden", "true");
        const icon = document.createElement("span");
        icon.className = "project-files-icon";
        icon.dataset.kind = iconKind(entry);
        icon.setAttribute("aria-hidden", "true");
        const name = document.createElement("span");
        name.className = "project-files-entry-name";
        name.textContent = entry.name;
        row.append(chevron, icon, name);
        row.addEventListener("focus", () => { view.focusedPath = entry.path; });
        fragment.append(row);
        visibleRows.push(row);
        if (expanded && child) {
          if (child.loading) hint("正在加载…", depth + 1);
          else if (child.error) hint(`${child.error} 收起后可重试。`, depth + 1, true);
          else if (child.entries) {
            appendBranch(child, depth + 1);
            if (!child.entries.length) hint("空目录", depth + 1);
            if (child.truncated) hint("条目较多，仅显示部分内容。", depth + 1);
          }
        }
      }
    }
    if (root) appendBranch(root, 0);
    const rows = visibleRows.filter((row) => !row.disabled);
    const focus = rows.find((row) => row.dataset.path === view.focusedPath)
      || rows.find((row) => row.dataset.path === view.selectedPath) || rows[0];
    for (const row of visibleRows) row.tabIndex = row === focus ? 0 : -1;
    entriesNode.replaceChildren(fragment);
    entriesNode.setAttribute("aria-busy", String(Boolean(root?.loading)));
    note("directory-note", root?.loading ? "正在加载文件…" : root?.error || (root?.entries
      ? filter && !visibleRows.length ? "已加载的文件中没有匹配项。"
      : !root.entries.length ? "此目录为空。"
      : root.truncated ? "根目录条目较多，仅显示部分内容。" : "" : ""), Boolean(root?.error));
    if (restoreFocus && focus) focus.focus();
    entriesNode.parentElement.scrollTop = oldScroll;
  }
  function discardBranch(view, path) {
    for (const key of view.tree.keys()) {
      if (path === "" || key === path || key.startsWith(`${path}/`)) {
        stop(view, `directory:${key}`);
        view.tree.delete(key);
      }
    }
  }
  async function loadDirectory(view, path, force = false) {
    if (!active(view) || !view.identity) return null;
    const previous = view.tree.get(path);
    if (!force && previous?.entries) return previous;
    if (!force && previous?.pending) return previous.pending;
    if (force) discardBranch(view, path);
    const branch = { path, entries: null, expanded: previous?.expanded ?? true, loading: true, error: "" };
    view.tree.set(path, branch);
    const epoch = view.treeEpoch;
    renderEntries(view);
    branch.pending = (async () => {
      try {
        const data = await request(view, `directory:${path}`, `/api/v1/project-files/directory?${query(view, path)}`);
        if (!data || view.treeEpoch !== epoch || view.tree.get(path) !== branch) return null;
        const count = Array.from(view.tree.values()).reduce((sum, value) => sum + (value.entries?.length || 0), 0);
        if (count + data.entries.length > MAX_TREE_ENTRIES) throw new Error("已达到目录树加载上限，请收起所有目录后重试。");
        branch.entries = data.entries;
        branch.truncated = data.truncated;
        return branch;
      } catch (error) {
        if (active(view) && view.treeEpoch === epoch && view.tree.get(path) === branch) {
          branch.error = error.message;
        }
        return null;
      } finally {
        branch.loading = false;
        branch.pending = null;
        if (active(view) && view.treeEpoch === epoch) renderEntries(view);
      }
    })();
    return branch.pending;
  }
  async function toggleDirectory(view, path) {
    if (!active(view) || !view.identity) return;
    view.navigation += 1;
    const branch = view.tree.get(path);
    if (view.filter) {
      view.filter = "";
      node("filter").value = "";
      if (branch) branch.expanded = false;
    }
    if (branch?.expanded) {
      branch.expanded = false;
      renderEntries(view);
      return;
    }
    if (branch) branch.expanded = true;
    if (!view.desiredFile) {
      view.path = path;
      view.selectedPath = path;
      renderBreadcrumbs(view);
    }
    if (branch?.entries || branch?.pending) renderEntries(view);
    await loadDirectory(view, path);
  }
  async function directory(view, path, selectedFile = null) {
    if (!active(view) || !view.identity) return;
    const navigation = ++view.navigation;
    const epoch = view.treeEpoch;
    view.filter = "";
    node("filter").value = "";
    let branch = await loadDirectory(view, "");
    const parts = path.split("/").filter(Boolean);
    let target = "";
    for (const part of parts) {
      if (!branch || !active(view) || view.navigation !== navigation || view.treeEpoch !== epoch) return;
      target += `${target ? "/" : ""}${part}`;
      if (!branch.entries.some((entry) => entry.path === target && entry.kind === "directory")) {
        note("note", "目标目录不在已加载的列表中，可能已移除或目录条目较多。", true);
        return;
      }
      const known = view.tree.get(target);
      if (known) known.expanded = true;
      branch = await loadDirectory(view, target);
    }
    if (!branch || !active(view) || view.navigation !== navigation || view.treeEpoch !== epoch) return;
    branch.expanded = true;
    if (selectedFile !== null) {
      const entry = branch.entries.find((item) => item.path === selectedFile);
      if (entry?.kind === "directory") return directory(view, entry.path);
      if (entry?.kind === "file") return content(view, entry.path);
      note("note", entry ? "此目录项暂不支持预览。" : "目标不在当前目录列表中，可能已移除或目录条目较多。", true);
    } else {
      if (!view.desiredFile) { view.path = path; view.selectedPath = path; renderBreadcrumbs(view); }
      renderEntries(view);
      if (path) focusTree(view, path);
      else node("entries").parentElement.scrollTop = 0;
    }
  }
  function collapseTree(view) {
    if (!active(view) || !view.identity) return;
    view.navigation += 1;
    view.treeEpoch += 1;
    const root = view.tree.get("");
    discardBranch(view, "");
    if (root?.entries) view.tree.set("", { ...root, expanded: true, loading: false, pending: null });
    view.filter = "";
    node("filter").value = "";
    renderEntries(view);
    if (!root?.entries) return directory(view, "");
  }
  async function refreshTree(view) {
    if (!active(view) || !view.identity) return chooseRoot(view);
    const file = view.desiredFile;
    const path = file ? parentPath(file) : view.path;
    const epoch = view.treeEpoch;
    const navigation = ++view.navigation;
    stop(view, "content");
    stop(view, "image-check");
    clearFile(view);
    const branch = await loadDirectory(view, path, true);
    if (!branch || !active(view) || view.treeEpoch !== epoch || view.navigation !== navigation) return;
    await directory(view, path, file || null);
  }
  async function treeKey(view, event) {
    const row = event.target.closest?.("button[data-path]");
    if (!row || !node("entries").contains(row)) return;
    const keys = ["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const rows = treeRows().filter((item) => !item.disabled);
    const index = rows.indexOf(row);
    const path = row.dataset.path;
    if (event.key === "ArrowDown") return focusTree(view, rows[Math.min(index + 1, rows.length - 1)].dataset.path);
    if (event.key === "ArrowUp") return focusTree(view, rows[Math.max(index - 1, 0)].dataset.path);
    if (event.key === "Home") return focusTree(view, rows[0].dataset.path);
    if (event.key === "End") return focusTree(view, rows.at(-1).dataset.path);
    if (event.key === "ArrowRight" && row.dataset.kind === "directory") {
      if (row.getAttribute("aria-expanded") !== "true") return toggleDirectory(view, path);
      const next = rows[index + 1];
      if (next?.dataset.path.startsWith(`${path}/`)) focusTree(view, next.dataset.path);
    }
    if (event.key === "ArrowLeft") {
      if (row.dataset.kind === "directory" && row.getAttribute("aria-expanded") === "true") {
        if (view.filter) {
          view.filter = "";
          node("filter").value = "";
          const branch = view.tree.get(path);
          if (branch) branch.expanded = false;
          view.navigation += 1;
          renderEntries(view);
          return;
        }
        return toggleDirectory(view, path);
      }
      const parent = parentPath(path);
      if (parent) focusTree(view, parent);
    }
  }
  function ensureAce() {
    if (window.ace) return Promise.resolve(window.ace);
    if (!aceLoading) {
      aceLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "/static/vendor/ace/ace.js";
        script.onload = () => {
          if (window.ace) resolve(window.ace);
          else { aceLoading = null; reject(new Error("文本查看器未能加载，请刷新重试。")); }
        };
        script.onerror = () => { script.remove(); aceLoading = null; reject(new Error("文本查看器未能加载，请刷新重试。")); };
        document.querySelector("head").append(script);
      });
    }
    return aceLoading;
  }
  function renderMode(view) {
    node("editor").hidden = view.preview;
    node("markdown").hidden = !view.preview;
    node("search-form").hidden = view.preview;
    node("line-form").hidden = view.preview;
    node("wrap").hidden = view.preview;
    node("markdown-toggle").setAttribute("aria-pressed", String(view.preview));
    node("markdown-toggle").textContent = view.preview ? "查看源码" : "预览 Markdown";
    if (!view.preview) view.editor?.resize();
  }
  async function imageFailed(view, file) {
    if (!active(view) || view.file !== file) return;
    try {
      await request(view, "image-check", `/api/v1/project-files/content?${query(view, file.path)}`);
      if (active(view) && view.file === file) note("note", "图片未能加载，请刷新重试。", true);
    } catch (error) {
      if (active(view) && view.file === file) note("note", error.message, true);
    }
  }
  async function content(view, path) {
    if (!active(view) || !view.identity) return;
    view.navigation += 1;
    stop(view, "image-check");
    clearFile(view);
    view.desiredFile = path;
    node("empty").hidden = true;
    view.selectedPath = path;
    view.path = parentPath(path);
    renderBreadcrumbs(view);
    renderEntries(view);
    node("filename").textContent = path;
    note("note", "正在读取文件…");
    const rootIdentity = view.identity;
    const sequence = view.fileSequence;
    try {
      const data = await request(view, "content", `/api/v1/project-files/content?${query(view, path)}`);
      if (!data) return;
      view.file = data;
      const modified = new Date(data.modifiedAt);
      const modifiedLabel = Number.isNaN(modified.getTime()) ? "" : modified.toLocaleString(undefined, {
        year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      });
      node("meta").textContent = [sizeLabel(data.size), modifiedLabel].filter(Boolean).join(" · ");
      for (const row of treeRows()) row.setAttribute("aria-current", String(row.dataset.path === data.path));
      if (data.kind === "image") {
        node("image").onerror = () => imageFailed(view, data);
        node("image").alt = path;
        node("image").src = data.imageUrl;
        node("image-wrap").hidden = false;
        note("note", data.previewMessage || "");
        return;
      }
      if (data.kind !== "text") {
        note("note", data.previewMessage || "此文件暂不支持预览。");
        return;
      }
      const ace = await ensureAce();
      if (!active(view) || view.identity !== rootIdentity || view.file !== data) return;
      for (const key of ["basePath", "modePath", "themePath", "workerPath"]) ace.config.set(key, "/static/vendor/ace");
      node("editor").hidden = false;
      const editor = view.editor = ace.edit(node("editor"));
      editor.session.setUseWorker(false);
      editor.setReadOnly(true);
      editor.setOptions({ showPrintMargin: false, fontSize: 13, highlightActiveLine: false, showLineNumbers: true });
      editor.session.setMode(`ace/mode/${modes.has(data.language) ? data.language : "text"}`);
      editor.session.setUseWrapMode(view.wrap);
      editor.setValue(data.text, -1);
      editor.commands.addCommand({ name: "find", bindKey: { win: "Ctrl-F", mac: "Command-F" },
        exec: () => node("search").focus(), readOnly: true });
      node("wrap").setAttribute("aria-pressed", String(view.wrap));
      node("tools").hidden = false;
      node("markdown-toggle").hidden = typeof data.markdownHtml !== "string";
      // Only the server's bounded, sanitized Markdown projection enters HTML.
      if (typeof data.markdownHtml === "string") node("markdown").innerHTML = data.markdownHtml;
      renderMode(view);
      note("note", data.previewMessage || (data.text.length ? "" : "此文件为空。"));
    } catch (error) {
      if (active(view) && view.fileSequence === sequence) note("note", error.message, true);
    }
  }
  async function chooseRoot(view) {
    if (!active(view)) return;
    clearRoot(view);
    view.rootId = node("root").value;
    note("directory-note", "正在读取工作目录…");
    try {
      const data = await request(view, "root", `/api/v1/project-files/root?${query(view)}`);
      if (!data) return;
      view.identity = data.identity;
      node("cwd").textContent = data.cwd;
      await directory(view, "");
    } catch (error) {
      note("directory-note", error.message, true);
    }
  }
  async function worktrees(view, cursor = null) {
    if (!active(view)) return;
    node("more-roots").disabled = true;
    const params = new URLSearchParams({ project: view.project.alias, pageSize: "50" });
    if (cursor) params.set("cursor", cursor);
    try {
      const data = await request(view, "worktrees", `/api/v1/worktrees?${params}`);
      if (!data) return;
      for (const tree of data.items) {
        if (view.worktreeIds.has(tree.id)) continue;
        view.worktreeIds.add(tree.id);
        const option = document.createElement("option");
        option.value = tree.id;
        option.textContent = `worktree · ${tree.branch || tree.id} · ${tree.cwd}`;
        option.disabled = tree.creationState !== "ready" || tree.state !== "ready" || Boolean(tree.orphaned);
        if (option.disabled) option.textContent += "（不可用）";
        node("root").append(option);
      }
      view.nextCursor = data.nextCursor;
      node("more-roots").hidden = !view.nextCursor;
      note("root-note", "");
    } catch (error) {
      note("root-note", `worktree 列表读取失败：${error.message}`, true);
      node("more-roots").hidden = false;
    } finally {
      if (active(view)) node("more-roots").disabled = false;
    }
  }
  async function open(project) {
    if (dependencies.isExpired()) return;
    close();
    const view = current = { project, rootId: "project", identity: null, path: "", file: null, desiredFile: null, requests: {},
      worktreeIds: new Set(), nextCursor: null, wrap: true, editor: null, returnFocus: document.activeElement,
      tree: new Map(), treeEpoch: 0, navigation: 0, selectedPath: "", focusedPath: "", filter: "" };
    node("title").textContent = `${project.alias} · 查看文件`;
    const root = document.createElement("option");
    root.value = "project";
    root.textContent = `Project · ${project.cwd}`;
    node("root").replaceChildren(root);
    node("root").value = "project";
    node("more-roots").hidden = true;
    node("drawer").showModal();
    document.body.classList.toggle("project-files-open", true);
    node("root").focus();
    await Promise.all([chooseRoot(view), worktrees(view)]);
  }
  async function copy() {
    const view = current;
    if (!view || !active(view) || view.file?.kind !== "text") return;
    const file = view.file;
    const previousFocus = document.activeElement;
    try {
      let copied = false;
      try {
        if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(file.text); copied = true; }
      } catch (_) { /* HTTP and embedded browsers may require the selection fallback. */ }
      if (!active(view) || view.file !== file) return;
      if (!copied) {
        const field = document.createElement("textarea");
        field.value = file.text;
        field.readOnly = true;
        field.className = "project-files-copy-buffer";
        field.setAttribute("aria-label", "复制文件内容");
        node("drawer").append(field);
        try { field.focus(); field.select(); copied = document.execCommand("copy"); }
        finally { field.value = ""; field.remove(); previousFocus?.focus(); }
      }
      note("note", copied ? "已复制全文。" : "复制未成功，请在源码中选择文本后复制。", !copied);
    } catch (_) {
      if (active(view) && view.file === file) note("note", "复制未成功，请在源码中选择文本后复制。", true);
    }
  }
  function init(options) {
    dependencies = options;
    node("close").addEventListener("click", close);
    node("drawer").addEventListener("cancel", (event) => { event.preventDefault(); close(); });
    node("drawer").addEventListener("close", () => { if (!node("drawer").open) close(); });
    node("root").addEventListener("change", () => current && chooseRoot(current));
    node("more-roots").addEventListener("click", () => current && worktrees(current, current.nextCursor));
    node("refresh").addEventListener("click", () => {
      if (!current) return;
      return refreshTree(current);
    });
    node("filter").addEventListener("input", () => {
      if (!current) return;
      current.filter = node("filter").value;
      renderEntries(current);
    });
    node("collapse").addEventListener("click", () => current && collapseTree(current));
    node("entries").addEventListener("keydown", (event) => current && treeKey(current, event));
    node("copy").addEventListener("click", copy);
    node("wrap").addEventListener("click", () => {
      if (!current?.editor) return;
      current.wrap = !current.wrap;
      current.editor.session.setUseWrapMode(current.wrap);
      node("wrap").setAttribute("aria-pressed", String(current.wrap));
    });
    node("markdown-toggle").addEventListener("click", () => {
      if (!current?.editor || typeof current.file?.markdownHtml !== "string") return;
      current.preview = !current.preview;
      renderMode(current);
    });
    node("search-form").addEventListener("submit", (event) => {
      event.preventDefault();
      if (current?.editor && node("search").value) {
        const match = current.editor.find(node("search").value, { backwards: false, wrap: true, caseSensitive: false, regExp: false });
        note("note", match ? "" : "未找到匹配文本。");
      }
    });
    node("line-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const line = Number(node("line").value);
      if (current?.editor && Number.isSafeInteger(line) && line >= 1) { current.editor.gotoLine(line, 0, true); current.editor.focus(); }
    });
    node("markdown").addEventListener("error", (event) => {
      if (event.target.tagName?.toLowerCase() === "img" && current?.file) return imageFailed(current, current.file);
    }, true);
    node("markdown").addEventListener("click", (event) => {
      const anchor = event.target.closest?.("a[data-project-file]");
      if (!anchor || !node("markdown").contains(anchor) || !current) return;
      event.preventDefault();
      const path = anchor.dataset.projectFile;
      if (anchor.dataset.projectDirectory === "true" || path === "") return directory(current, path);
      const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
      return directory(current, parent, path);
    });
    window.addEventListener("resize", () => current?.editor?.resize());
  }
  return { init, open, close, expire: close };
})();
