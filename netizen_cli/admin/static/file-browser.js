"use strict";

// A browser for one explicitly selected resource. Providers own business roots
// and mutation authorization; requests and previews belong to this instance.
window.NetizenFileBrowser = {
  create(dependencies) {
  const node = (name) => name === "drawer" ? dependencies.container
    : dependencies.container.querySelector(`#file-browser-${name}`);
  const modes = new Set(["python", "json", "javascript", "markdown", "text"]);
  const compact = window.matchMedia("(max-width: 720px)");
  let current = null;
  let aceLoading = null;

  function active(view) { return current === view && !dependencies.isExpired(); }
  function renderLayout(view) {
    node("drawer").dataset.screen = view.screen;
    const reading = compact.matches && view.screen === "file";
    node("title").textContent = reading
      ? view.selectedFile?.displayName || view.selectedFile?.name || view.desiredFile?.split("/").at(-1) || view.provider.title
      : view.provider.title;
    node("back").hidden = !compact.matches || (!reading && !view.path);
    node("back").textContent = reading ? "‹ 返回目录" : "‹ 上一级";
    node("collapse").hidden = compact.matches;
    node("filter").placeholder = compact.matches ? "筛选当前目录…" : "筛选已加载的文件…";
    node("filter").setAttribute("aria-label", compact.matches ? "筛选当前目录" : "筛选已加载的文件");
    node("entries").setAttribute("role", compact.matches ? "group" : "tree");
  }
  // Positions live with the bounded directory cache, never in a second history.
  function rememberDirectory(view) {
    const branch = view.tree.get(view.path);
    if (compact.matches && view.screen === "directory" && branch) {
      branch.position = { scroll: node("entries").parentElement.scrollTop,
        focus: view.focusedPath, filter: view.filter };
    }
  }
  function selectDirectory(view, path, focus = "") {
    const position = view.tree.get(path)?.position;
    view.path = path;
    view.focusedPath = focus || position?.focus || "";
    view.selectedPath = view.focusedPath;
    view.filter = position?.filter || "";
    node("filter").value = view.filter;
  }
  function restoreDirectory(view) {
    const position = view.tree.get(view.path)?.position;
    const row = treeRows().find((item) => item.dataset.path === view.focusedPath && !item.disabled);
    const crumbs = node("breadcrumbs").children;
    (row || crumbs[crumbs.length - 1])?.focus({ preventScroll: true });
    node("entries").parentElement.scrollTop = position?.scroll || 0;
    if (!position) row?.scrollIntoView?.({ block: "nearest" });
  }
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
    view.screen = "directory";
    view.file = null;
    view.selectedFile = null;
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
    node("filename").removeAttribute("title");
    node("meta").textContent = "";
    node("search").value = "";
    node("line").value = "1";
    node("empty").hidden = false;
    note("note", "");
    renderActions(view);
    renderLayout(view);
  }
  function clearRoot(view) {
    for (const slot of Object.keys(view.requests)) if (slot !== "roots") stop(view, slot);
    view.treeEpoch += 1;
    view.navigation += 1;
    view.tree.clear();
    view.identity = null;
    view.desiredFile = null;
    view.path = "";
    view.selectedPath = "";
    view.focusedPath = "";
    view.filter = "";
    view.deleteNote = "";
    view.deleteUncertain = false;
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
    node("actions").hidden = true;
    note("action-note", "");
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
    document.body.classList.toggle("file-browser-open", false);
    if (!dependencies.isExpired() && view.returnFocus?.isConnected !== false) view.returnFocus?.focus();
  }
  function url(view, operation, path) {
    return view.provider.url(operation, { rootId: view.rootId, identity: view.identity, path });
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
  function entryLabel(view, entry) {
    return view.provider.entryLabel?.(entry) || entry.displayName || entry.name;
  }
  function renderBreadcrumbs(view) {
    const crumbs = node("breadcrumbs");
    crumbs.replaceChildren(button("根目录", () => directory(view, "")));
    let path = "";
    for (const part of view.path.split("/").filter(Boolean)) {
      path += `${path ? "/" : ""}${part}`;
      const target = path;
      const entry = view.tree.get(parentPath(target))?.entries?.find((item) => item.path === target);
      crumbs.append(document.createTextNode(" / "), button(entry ? entryLabel(view, entry) : part, () => directory(view, target)));
    }
  }
  function sizeLabel(size) {
    if (size == null) return "";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
  }
  function showMetadata(file) {
    const saved = Boolean(file.savedAt);
    const date = file.savedAt || file.modifiedAt;
    const time = date ? new Date(date) : null;
    const label = time && !Number.isNaN(time.getTime()) ? time.toLocaleString(undefined, {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }) : "";
    node("meta").textContent = [sizeLabel(file.size), label && `${saved ? "保存" : "修改"}于 ${label}`].filter(Boolean).join(" · ");
  }
  function renderActions(view) {
    const allowed = Boolean(view.provider.remove);
    node("actions").hidden = !allowed || (!view.selectedFile && !view.deleteNote && !view.deleting);
    node("delete").hidden = !view.selectedFile?.deletable;
    node("delete").disabled = !allowed || view.deleting || view.deleteUncertain || !view.selectedFile?.deletable;
    note("action-note", view.deleting ? "正在处理删除，请勿重复操作…" : view.deleteNote || "", Boolean(view.deleteError));
  }
  async function deleteSelected(view) {
    const file = view.selectedFile;
    if (!active(view) || !view.provider.remove || !file?.deletable || view.deleting || view.deleteUncertain) return;
    const identity = view.identity;
    const preparation = new AbortController();
    view.requests["delete-options"] = preparation;
    view.deleting = true;
    view.deleteNote = "";
    view.deleteError = false;
    renderActions(view);
    try {
      const result = await view.provider.remove({ ...file, identity }, {
        signal: preparation.signal,
        isCurrent: () => active(view) && view.identity === identity && view.selectedFile === file,
      });
      if (!result || !active(view) || view.identity !== identity) return;
      const parent = parentPath(file.path);
      let returned = false;
      if (view.selectedFile?.path === file.path) {
        returned = true;
        stop(view, "content");
        stop(view, "image-check");
        clearFile(view);
        view.desiredFile = null;
        view.path = parent;
        view.selectedPath = parent;
      }
      if (result.removedBatch) {
        discardBranch(view, parent);
        const root = view.tree.get("");
        if (root?.entries) root.entries = root.entries.filter((entry) => entry.path !== parent);
        if (view.path === parent || view.path.startsWith(`${parent}/`)) {
          returned = true;
          clearFile(view);
          view.desiredFile = null;
          view.path = "";
          view.selectedPath = "";
        }
      }
      if (returned && compact.matches) selectDirectory(view, view.path);
      const navigation = view.navigation;
      const filter = view.filter;
      const focusBeforeLoad = document.activeElement;
      if (!result.removedBatch) await loadDirectory(view, parent, true);
      if (!active(view) || view.identity !== identity) return;
      view.deleteNote = `已删除 ${file.displayName || file.name}。`;
      const restoreFocus = document.activeElement === focusBeforeLoad && document.activeElement !== node("filter");
      renderBreadcrumbs(view);
      renderEntries(view);
      renderLayout(view);
      if (returned && compact.matches && view.screen === "directory" && view.navigation === navigation
        && view.filter === filter && restoreFocus) restoreDirectory(view);
    } catch (error) {
      if (!active(view) || view.identity !== identity || error.name === "AbortError") return;
      if (error.code === "root_changed") { invalidateRoot(view, error); return; }
      view.deleteUncertain = Boolean(error.uncertain);
      view.deleteNote = error.uncertain ? "删除结果尚未确认，请刷新目录核对后再操作。" : error.message;
      view.deleteError = true;
    } finally {
      if (view.requests["delete-options"] === preparation) delete view.requests["delete-options"];
      view.deleting = false;
      if (active(view)) renderActions(view);
    }
  }
  const MAX_TREE_ENTRIES = 10000;
  function parentPath(path) { return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ""; }
  function treeRows() { return Array.from(node("entries").querySelectorAll("button[data-path]")); }
  function focusTree(view, path) {
    view.focusedPath = path;
    for (const row of treeRows()) row.tabIndex = (compact.matches || row.dataset.path === path) && !row.disabled ? 0 : -1;
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
    const root = view.tree.get(compact.matches ? view.path : "");
    const filter = view.filter.trim().toLocaleLowerCase();
    const matches = new Set();
    if (filter) {
      for (const branch of compact.matches ? [root || {}] : view.tree.values()) for (const entry of branch.entries || []) {
        const label = compact.matches ? entryLabel(view, entry) : `${entry.path} ${entryLabel(view, entry)}`;
        if (!label.toLocaleLowerCase().includes(filter)) continue;
        let path = entry.path;
        while (path) { matches.add(path); path = parentPath(path); }
      }
    }
    function hint(text, depth, error = false) {
      const message = document.createElement("div");
      message.className = `file-browser-tree-note${error ? " error" : ""}`;
      message.style.setProperty("--depth", depth);
      message.textContent = text;
      fragment.append(message);
    }
    function appendBranch(branch, depth) {
      const entries = (branch.entries || []).filter((entry) => !filter || matches.has(entry.path));
      for (const [index, entry] of entries.entries()) {
        const directoryEntry = entry.kind === "directory";
        const child = directoryEntry ? view.tree.get(entry.path) : null;
        const expanded = !compact.matches && directoryEntry && (filter ? Boolean(child?.entries || child?.loading || child?.error) : Boolean(child?.expanded));
        const row = button("", () => directoryEntry
          ? compact.matches ? directory(view, entry.path) : toggleDirectory(view, entry.path)
          : content(view, entry.path), "file-browser-entry");
        row.disabled = !directoryEntry && entry.kind !== "file";
        row.dataset.path = entry.path;
        row.dataset.kind = entry.kind;
        row.style.setProperty("--depth", depth);
        if (!compact.matches) {
          row.setAttribute("role", "treeitem");
          row.setAttribute("aria-level", String(depth + 1));
          row.setAttribute("aria-posinset", String(index + 1));
          row.setAttribute("aria-setsize", String(entries.length));
          row.setAttribute("aria-selected", String(view.selectedPath === entry.path));
        }
        row.setAttribute("aria-current", String(view.selectedFile?.path === entry.path));
        row.setAttribute("aria-label", entryLabel(view, entry));
        if (directoryEntry && !compact.matches) row.setAttribute("aria-expanded", String(expanded));
        if (child?.loading) row.setAttribute("aria-busy", "true");
        row.title = entry.kind === "symlink" ? `${entry.path} · 符号链接，不跟随`
          : entry.kind === "unsupported" ? `${entry.path} · 不支持预览` : entry.path;
        const chevron = document.createElement("span");
        chevron.className = "file-browser-chevron";
        chevron.setAttribute("aria-hidden", "true");
        const icon = document.createElement("span");
        icon.className = "file-browser-icon";
        icon.dataset.kind = iconKind(entry);
        icon.setAttribute("aria-hidden", "true");
        const name = document.createElement("span");
        name.className = "file-browser-entry-name";
        name.textContent = entryLabel(view, entry);
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
    for (const row of visibleRows) row.tabIndex = compact.matches || row === focus ? 0 : -1;
    entriesNode.replaceChildren(fragment);
    entriesNode.setAttribute("aria-busy", String(Boolean(root?.loading)));
    note("directory-note", root?.loading ? "正在加载文件…" : root?.error || (root?.entries
      ? filter && !visibleRows.length ? compact.matches ? "当前目录没有匹配项。" : "已加载的文件中没有匹配项。"
      : !root.entries.length ? "此目录为空。"
      : root.truncated ? "目录条目较多，仅显示部分内容。" : "" : ""), Boolean(root?.error));
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
    const branch = { path, entries: null, expanded: previous?.expanded ?? true,
      position: previous?.position, loading: true, error: "" };
    view.tree.set(path, branch);
    const epoch = view.treeEpoch;
    renderEntries(view);
    branch.pending = (async () => {
      try {
        const data = await request(view, `directory:${path}`, url(view, "directory", path));
        if (!data || view.treeEpoch !== epoch || view.tree.get(path) !== branch) return null;
        const count = Array.from(view.tree.values()).reduce((sum, value) => sum + (value.entries?.length || 0), 0);
        if (count + data.entries.length > MAX_TREE_ENTRIES) throw new Error(compact.matches
          ? "已达到目录树加载上限，请返回根目录并刷新后重试。" : "已达到目录树加载上限，请收起所有目录后重试。");
        branch.entries = data.entries;
        branch.truncated = data.truncated;
        return branch;
      } catch (error) {
        if (active(view) && view.treeEpoch === epoch && view.tree.get(path) === branch) {
          branch.error = error.message;
          branch.errorCode = error.code;
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
    const browsing = compact.matches && selectedFile === null;
    if (browsing) {
      const returningFile = view.screen === "file" && parentPath(view.desiredFile || "") === path ? view.desiredFile : "";
      rememberDirectory(view);
      stop(view, "content");
      stop(view, "image-check");
      stop(view, "delete-options");
      clearFile(view);
      view.desiredFile = null;
      selectDirectory(view, path, returningFile);
    } else {
      view.filter = "";
      node("filter").value = "";
    }
    if (browsing) {
      renderBreadcrumbs(view);
      renderLayout(view);
      renderEntries(view);
      restoreDirectory(view);
    }
    const focusBeforeLoad = document.activeElement;
    let branch = await loadDirectory(view, "");
    const parts = path.split("/").filter(Boolean);
    let target = "";
    for (const part of parts) {
      if (!active(view) || view.navigation !== navigation || view.treeEpoch !== epoch) return;
      if (!branch) {
        note("note", view.tree.get(target)?.error || "目录未能加载，请刷新重试。", true);
        return;
      }
      target += `${target ? "/" : ""}${part}`;
      if (!branch.entries.some((entry) => entry.path === target && entry.kind === "directory")) {
        note("note", "目标目录不在已加载的列表中，可能已移除或目录条目较多。", true);
        return;
      }
      const known = view.tree.get(target);
      if (known) known.expanded = true;
      branch = await loadDirectory(view, target);
    }
    if (!active(view) || view.navigation !== navigation || view.treeEpoch !== epoch) return;
    if (!branch) {
      if (selectedFile !== null) note("note", view.tree.get(target)?.error || "目录未能加载，请刷新重试。", true);
      return;
    }
    branch.expanded = true;
    if (selectedFile !== null) {
      const entry = branch.entries.find((item) => item.path === selectedFile);
      if (entry?.kind === "directory") return directory(view, entry.path);
      if (entry?.kind === "file") return content(view, entry.path);
      note("note", entry ? "此目录项暂不支持预览。" : "目标不在当前目录列表中，可能已移除或目录条目较多。", true);
    } else {
      // The user may have started typing while the directory was loading.
      const restorePosition = compact.matches && document.activeElement === focusBeforeLoad;
      if (!view.desiredFile) {
        view.path = path;
        if (!browsing) view.selectedPath = path;
        renderBreadcrumbs(view);
      }
      renderEntries(view);
      if (browsing) {
        if (restorePosition) restoreDirectory(view);
      } else if (path) focusTree(view, path);
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
    if (!view.desiredFile) {
      view.path = "";
      view.selectedPath = "";
      renderBreadcrumbs(view);
      renderLayout(view);
    }
    renderEntries(view);
    if (!root?.entries) return directory(view, "");
  }
  async function refreshTree(view) {
    if (!active(view) || !view.identity) return chooseRoot(view);
    rememberDirectory(view);
    view.deleteNote = "";
    view.deleteError = false;
    view.deleteUncertain = false;
    const file = view.desiredFile;
    const path = file ? parentPath(file) : view.path;
    const epoch = view.treeEpoch;
    const navigation = ++view.navigation;
    stop(view, "content");
    stop(view, "image-check");
    clearFile(view);
    if (compact.matches && file) {
      view.screen = "file";
      renderLayout(view);
      note("note", "正在刷新文件…");
    }
    const branch = await loadDirectory(view, path, true);
    if (!active(view) || view.treeEpoch !== epoch || view.navigation !== navigation) return;
    if (!branch) {
      // A removed batch (including a deletion whose response was lost) cannot
      // be refreshed in place. Reconcile one root listing without changing its
      // identity or scanning descendants; other read failures remain local.
      if (!path || view.tree.get(path)?.errorCode !== "not_found") {
        if (compact.matches && file) note("note", view.tree.get(path)?.error || "目录未能加载，请刷新重试。", true);
        return;
      }
      view.desiredFile = null;
      view.path = "";
      view.selectedPath = "";
      view.focusedPath = "";
      clearFile(view);
      renderBreadcrumbs(view);
      const root = await loadDirectory(view, "", true);
      if (!root || !active(view) || view.treeEpoch !== epoch || view.navigation !== navigation) return;
      await directory(view, "");
      if (active(view) && view.treeEpoch === epoch && view.navigation === navigation + 1) {
        note("note", "原目录已移除，已刷新根目录。");
      }
      return;
    }
    if (file && !branch.entries.some((entry) => entry.path === file)) {
      view.desiredFile = null;
      view.selectedPath = path;
      await directory(view, path);
      if (active(view) && view.treeEpoch === epoch && view.navigation === navigation + 1) {
        note("note", "原文件不在当前目录列表中，可能已移除或列表不完整。");
      }
      return;
    }
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
    if (compact.matches) {
      if (event.key === "ArrowLeft") return back(view);
      if (event.key === "ArrowRight" && row.dataset.kind === "directory") return directory(view, path);
    }
    if (event.key === "ArrowDown") return focusTree(view, rows[Math.min(index + 1, rows.length - 1)].dataset.path);
    if (event.key === "ArrowUp") return focusTree(view, rows[Math.max(index - 1, 0)].dataset.path);
    if (event.key === "Home") return focusTree(view, rows[0].dataset.path);
    if (event.key === "End") return focusTree(view, rows.at(-1).dataset.path);
    if (!compact.matches && event.key === "ArrowRight" && row.dataset.kind === "directory") {
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
      await request(view, "image-check", url(view, "content", file.path));
      if (active(view) && view.file === file) note("note", "图片未能加载，请刷新重试。", true);
    } catch (error) {
      if (active(view) && view.file === file) note("note", error.message, true);
    }
  }
  async function content(view, path) {
    if (!active(view) || !view.identity) return;
    if (view.screen === "directory") view.focusedPath = path;
    rememberDirectory(view);
    view.navigation += 1;
    stop(view, "image-check");
    clearFile(view);
    view.desiredFile = path;
    const entry = view.tree.get(parentPath(path))?.entries?.find((item) => item.path === path);
    view.selectedFile = entry ? { ...entry } : { path, name: path };
    view.screen = "file";
    showMetadata(view.selectedFile);
    renderActions(view);
    node("empty").hidden = true;
    view.selectedPath = path;
    view.path = parentPath(path);
    renderLayout(view);
    if (compact.matches) node("back").focus({ preventScroll: true });
    renderBreadcrumbs(view);
    renderEntries(view);
    node("filename").textContent = entry?.displayName || path;
    node("filename").setAttribute("title", path);
    note("note", "正在读取文件…");
    const rootIdentity = view.identity;
    const sequence = view.fileSequence;
    try {
      const data = await request(view, "content", url(view, "content", path));
      if (!data) return;
      view.file = data;
      Object.assign(view.selectedFile, { size: data.size, modifiedAt: data.modifiedAt, version: data.version });
      showMetadata(view.selectedFile);
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
      window.NetizenContentPreview.renderMarkdown(node("markdown"), data.markdownHtml);
      renderMode(view);
      note("note", data.previewMessage || (data.text.length ? "" : "此文件为空。"));
    } catch (error) {
      if (active(view) && view.fileSequence === sequence) note("note", error.message, true);
    }
  }
  async function chooseRoot(view) {
    if (!active(view)) return;
    clearRoot(view);
    view.rootId = view.provider.roots ? node("root").value : view.provider.rootId;
    note("directory-note", "正在读取目录…");
    try {
      const data = await request(view, "root", url(view, "root"));
      if (!data) return;
      view.identity = data.identity;
      node("cwd").textContent = data.cwd;
      await directory(view, "");
    } catch (error) {
      note("directory-note", error.message, true);
    }
  }
  function appendRoot(optionData) {
    const option = document.createElement("option");
    option.value = optionData.id;
    option.textContent = optionData.label;
    option.disabled = Boolean(optionData.disabled);
    node("root").append(option);
  }
  async function moreRoots(view, cursor = null) {
    if (!active(view) || !view.provider.rootPage) return;
    node("more-roots").disabled = true;
    try {
      const data = await request(view, "roots", view.provider.rootPage(cursor));
      if (!data) return;
      for (const root of view.provider.rootOptions(data)) {
        if (view.rootIds.has(root.id)) continue;
        view.rootIds.add(root.id);
        appendRoot(root);
      }
      view.nextCursor = data.nextCursor;
      node("more-roots").hidden = !view.nextCursor;
      note("root-note", "");
    } catch (error) {
      if (!active(view)) return;
      note("root-note", `目录选项读取失败：${error.message}`, true);
      node("more-roots").hidden = false;
    } finally {
      if (active(view)) node("more-roots").disabled = false;
    }
  }
  async function open(provider) {
    if (dependencies.isExpired()) return;
    close();
    const view = current = { provider, rootId: provider.rootId, identity: null, path: "", file: null,
      selectedFile: null, desiredFile: null, requests: {}, deleting: false, deleteUncertain: false,
      rootIds: new Set(), nextCursor: null, wrap: true, editor: null, returnFocus: document.activeElement,
      tree: new Map(), treeEpoch: 0, navigation: 0, selectedPath: "", focusedPath: "", filter: "", screen: "directory" };
    node("title").textContent = provider.title;
    node("eyebrow").textContent = provider.eyebrow;
    node("permission").textContent = provider.remove ? "可查看、删除" : "只读";
    node("description").textContent = provider.description;
    node("empty-title").textContent = provider.emptyTitle;
    node("entries").setAttribute("aria-label", provider.emptyTitle);
    node("root-label").hidden = !provider.roots;
    node("root").hidden = !provider.roots;
    node("root").replaceChildren();
    for (const root of provider.roots || []) { view.rootIds.add(root.id); appendRoot(root); }
    node("root").value = provider.rootId;
    node("more-roots").hidden = true;
    node("drawer").showModal();
    document.body.classList.toggle("file-browser-open", true);
    node(provider.roots ? "root" : "filter").focus();
    await Promise.all([chooseRoot(view), moreRoots(view)]);
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
        field.className = "file-browser-copy-buffer";
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
  function init() {
    node("close").addEventListener("click", close);
    node("back").addEventListener("click", () => current && back(current));
    node("delete").addEventListener("click", () => current && deleteSelected(current));
    node("drawer").addEventListener("cancel", (event) => { event.preventDefault(); close(); });
    node("drawer").addEventListener("close", () => { if (!node("drawer").open) close(); });
    node("root").addEventListener("change", () => current && chooseRoot(current));
    node("more-roots").addEventListener("click", () => current && moreRoots(current, current.nextCursor));
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
      const anchor = event.target.closest?.("a[data-file-browser-path]");
      if (!anchor || !node("markdown").contains(anchor) || !current) return;
      event.preventDefault();
      const path = anchor.dataset.fileBrowserPath;
      if (anchor.dataset.fileBrowserDirectory === "true" || path === "") return directory(current, path);
      const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
      return directory(current, parent, path);
    });
    window.addEventListener("resize", () => current?.editor?.resize());
    compact.addEventListener("change", () => {
      if (!current) return;
      current.filter = "";
      node("filter").value = "";
      renderLayout(current);
      renderEntries(current);
      if (compact.matches && current.screen === "file") node("back").focus({ preventScroll: true });
      current.editor?.resize();
    });
  }
  function back(view) {
    return directory(view, view.screen === "file" ? view.path : parentPath(view.path));
  }
  init();
  return { open, close, expire: close };
} };
