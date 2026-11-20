"use strict";

window.NetizenSessionLists = (() => {
  let dependencies;
  // Only one visible ordinary/Side list may commit, including its name enrichment.
  let sessionListRequest = 0;
  let pendingSessionChatLabels = null;

  function invalidate() {
    sessionListRequest += 1;
    // A failed replacement read can leave these same rows on screen. Their old
    // enrichment must stop showing a loading placeholder when it is invalidated.
    pendingSessionChatLabels?.();
    pendingSessionChatLabels = null;
  }

  // Copies protect the owner's results, including nested grants, from callers.
  function readonlyCopy(value) {
    if (!value || typeof value !== "object") return value;
    return Object.freeze(Array.isArray(value) ? value.map(readonlyCopy)
      : Object.fromEntries(Object.entries(value).map(([key, item]) => [key, readonlyCopy(item)])));
  }

  function create(kind, options) {
    const name = kind === "side" ? "sides" : "sessions";
    let appliedPage = dependencies.firstSessionPage();
    let currentData = null;
    let refreshedAt = null;
    let cachedSnapshot;
    rebuildSnapshot();
    // Keep existing rows so folding never recreates action grants.
    const groupView = { project: new Set(), chat: new Set(), layout: "compact", groups: [] };

    function sessionSort() {
      const query = new URLSearchParams(appliedPage.query || "");
      return { by: query.get("sortBy") || "updatedAt", direction: query.get("sortDirection") || "desc" };
    }

    function sessionFilterPage({ resetSort = false } = {}) {
      const query = options.draftQuery();
      const sort = resetSort ? { by: "updatedAt", direction: "desc" } : sessionSort();
      query.set("sortBy", kind === "session" && query.get("inventoryState") === "lazy" ? "createdAt" : sort.by);
      query.set("sortDirection", sort.direction);
      return dependencies.firstSessionPage(query.toString());
    }

    function appliedSessionConditions(query) {
      const labels = { searchTerm: "搜索", inventoryState: "会话状态", project: "Project", chatId: "群聊",
        scopeKind: "Scope", current: "当前指针", identity: "会话 ID", topicId: "话题 ID", state: "会话状态" };
      const conditions = [];
      for (const [key, label] of Object.entries(labels)) {
        const values = query.getAll(key);
        if (!values.length || (key === "inventoryState" && values[0] === "active")
          || (key === "state" && [...values].sort().join(",") === "creating,open")) continue;
        const names = values.map((value) => key === "inventoryState" ? dependencies.catalogStateLabel(value)
          : key === "current" ? value === "true" ? "当前" : "非当前"
            : key === "scopeKind" ? ({ direct: "单聊", group: "群聊", topic: "话题" }[value] || value)
              : key === "state" ? value === "all" ? "全部" : dependencies.sideStateLabel(value) : value);
        conditions.push({ key, label: `${label}：${names.join("、")}`, advanced: ["scopeKind", "current"].includes(key) });
      }
      for (const field of kind === "side" ? ["created"] : ["created", "updated"]) {
        const from = query.get(`${field}From`) || "", before = query.get(`${field}Before`) || "";
        const preset = query.get(`${field}Range`) || "custom";
        if (field === "created" && !from && !before) continue;
        if (field === "updated" && (query.get("inventoryState") === "lazy" || (preset === "last-3-months" && !before))) continue;
        conditions.push({ key: field, label: `${field === "created" ? "创建时间" : "更新时间"}：${dependencies.timeRange.compactTimeRange(from, before, preset)}`,
          title: dependencies.timeRange.timeRangeSummary(from, before, preset), advanced: true });
      }
      return conditions;
    }

    function renderAppliedSessionFilters() {
      const query = new URLSearchParams(appliedPage.query || "");
      const conditions = appliedSessionConditions(query);
      const chips = document.querySelector(`#${kind}-filter-chips`);
      chips.replaceChildren();
      for (const condition of conditions) {
        const button = document.createElement("button"); button.type = "button"; button.className = "filter-chip";
        button.dataset.filterKey = condition.key;
        button.textContent = `${condition.label} ×`;
        button.title = condition.title || condition.label;
        button.setAttribute("aria-label", `移除${condition.label}`);
        button.addEventListener("click", () => removeSessionCondition(condition.key));
        chips.append(button);
      }
      chips.hidden = !conditions.length;
      document.querySelector(`#${kind}-more-filters`).textContent = `更多筛选（${conditions.filter((item) => item.advanced).length}）`;
    }

    function removeSessionCondition(key) {
      if (dependencies.isExpired()) return;
      const query = new URLSearchParams(appliedPage.query);
      const defaults = options.defaultQuery();
      const fields = ["created", "updated"].includes(key) ? [`${key}From`, `${key}Before`, `${key}Range`] : [key];
      for (const field of fields) {
        query.delete(field);
        for (const value of defaults.getAll(field)) query.append(field, value);
      }
      options.resetCondition(key, query);
      if (key === "inventoryState") {
        // Leaving lazy restores the default date query without submitting date drafts.
        if (!query.has("updatedFrom") && !query.has("updatedBefore") && !query.has("updatedRange")) {
          query.set("updatedFrom", defaults.get("updatedFrom")); query.set("updatedRange", "last-3-months");
        }
      }
      return dependencies.navigate(dependencies.firstSessionPage(query.toString()));
    }

    function resizeSessionPage() {
      const query = new URLSearchParams(appliedPage.query || options.defaultQuery());
      query.set("pageSize", document.querySelector(`#${kind}-page-size`).value);
      return dependencies.navigate(dependencies.firstSessionPage(query.toString()));
    }

    function renderSessionSort() {
      const sort = sessionSort();
      const applied = new URLSearchParams(appliedPage.query || "");
      const lazy = kind === "session" && applied.get("inventoryState") === "lazy";
      for (const button of document.querySelectorAll("[data-session-sort]")) {
        if (button.dataset.sessionSort !== kind) continue;
        const selected = button.dataset.sortBy === sort.by;
        const descending = selected && sort.direction === "desc";
        const label = button.dataset.sortBy === "createdAt" ? lazy ? "登记时间" : "创建时间" : "最近更新";
        button.disabled = lazy && button.dataset.sortBy === "updatedAt";
        const labelNode = button.querySelector(".sort-label");
        if (labelNode) {
          labelNode.textContent = label;
          button.title = lazy ? "Netizen 登记时间；此时尚无 Codex 创建时间" : "Codex 会话创建时间，与创建时间筛选口径一致；按浏览器本地时区显示";
        }
        button.parentElement.setAttribute("aria-sort", selected ? descending ? "descending" : "ascending" : "none");
        button.setAttribute("aria-label", `按${label}${descending ? "由远及近" : "由近及远"}排序`
          + (selected ? `（当前${descending ? "由近及远" : "由远及近"}）` : ""));
        button.querySelector(".sort-indicator").textContent = selected ? descending ? "↓" : "↑" : "↕";
      }
    }

    async function sortSessionPage(by) {
      const page = appliedPage;
      // Sorting operates on the displayed result, not unsubmitted filter drafts.
      const query = new URLSearchParams(page.query ?? sessionFilterPage().query);
      if (kind === "session" && query.get("inventoryState") === "lazy" && by === "updatedAt") return false;
      const previous = sessionSort();
      query.set("sortBy", by);
      query.set("sortDirection", previous.by === by && previous.direction === "desc" ? "asc" : "desc");
      return dependencies.navigate(dependencies.firstSessionPage(query.toString()));
    }

    function renderSessionPagination() {
      const name = kind === "side" ? "sides" : "sessions";
      const page = appliedPage;
      document.querySelector(`#${name}-previous`).disabled = page.previousCursors.length === 0;
      document.querySelector(`#${name}-next`).disabled = !page.nextCursor;
      document.querySelector(`#${name}-page`).textContent = `第 ${page.number} 页 · ${currentData?.items?.length || 0} 条`;
    }

    function resetSessionFilters() {
      options.resetDrafts();
      const page = kind === "session" ? dependencies.firstSessionPage(options.defaultQuery().toString())
        : sessionFilterPage({ resetSort: true });
      const query = new URLSearchParams(page.query);
      query.delete("identity");
      if (kind === "session") options.restoreControls(query);
      page.query = query.toString();
      return dependencies.navigate(page);
    }

    async function loadSessionChatLabels(data, page, request) {
      const name = kind === "side" ? "sides" : "sessions";
      const isCurrent = () => !dependencies.isExpired() && request === sessionListRequest
        && dependencies.isVisible(kind)
        && currentData === data && appliedPage === page;
      if (!isCurrent()) return;
      const body = document.querySelector(`#${name}-body`);
      const identity = kind === "side" ? "sideId" : "bindingId";
      const rows = new Map([...body.children].filter((row) => row.dataset[identity])
        .map((row) => [row.dataset[identity], row]));
      const targets = data.items.flatMap((item) => item.chatLabelResolved || !item.chatId
        ? [] : [{ item, cell: rows.get(item[identity])?.querySelector(".session-location") }])
        .filter(({ cell }) => cell);
      if (!targets.length) return;
      const showIdentity = kind !== "side";
      const finish = () => {
        for (const { item, cell } of targets) dependencies.updateSessionLocationCell(cell, item, { showIdentity });
        updateSessionGroupLabels();
      };
      pendingSessionChatLabels = finish;
      for (const { item, cell } of targets) dependencies.updateSessionLocationCell(cell, item, { showIdentity, loading: true });
      try {
        const query = new URLSearchParams({ chatIds: [...new Set(targets.map(({ item }) => item.chatId))].join(",") });
        const labels = await dependencies.api(`/api/v1/chat-labels?${query}`);
        if (!isCurrent()) return;
        const byChat = new Map(labels.items.map((label) => [label.chatId, label]));
        let changed = false;
        for (const { item } of targets) {
          const label = byChat.get(item.chatId);
          if (!label) continue;
          // Enrichment changes display facts only; retain exact identities, topic
          // links, runtime and action grants from the successful list response.
          for (const field of ["chatLabel", "chatLabelResolved", "chatMode", "chatType", "chatOpenUrl"]) {
            if (Object.hasOwn(label, field) && item[field] !== label[field]) {
              item[field] = label[field];
              changed = true;
            }
          }
        }
        if (changed) rebuildSnapshot();
      } catch {
        // Name failures are local to the location cells. Authentication expiry is
        // already handled by api(), and list status/freshness stay unchanged.
      } finally {
        if (isCurrent()) finish();
        if (pendingSessionChatLabels === finish) pendingSessionChatLabels = null;
      }
    }

    // Detailed outcomes are for callers that must distinguish supersession from failure.
    // The public list/refresh wrappers retain their boolean success contract.
    async function loadSessionPageResult(page) {
      invalidate();
      const request = sessionListRequest;
      const isCurrent = () => request === sessionListRequest;
      const name = kind === "side" ? "sides" : "sessions";
      try {
        const query = page.query == null
          ? options.draftQuery()
          : new URLSearchParams(page.query);
        if (!query.has("sortBy")) query.set("sortBy", kind === "session" && query.get("inventoryState") === "lazy" ? "createdAt" : "updatedAt");
        if (!query.has("sortDirection")) query.set("sortDirection", "desc");
        if (kind === "session" && document.body.dataset.backend === "claude") {
          query.set("sortBy", "createdAt");
          for (const key of ["updatedFrom", "updatedBefore", "updatedRange", "searchTerm"]) query.delete(key);
        }
        const candidate = { ...page, query: query.toString(), previousCursors: [...page.previousCursors] };
        // Options own their control-level failure handling, independently of this page.
        void options.loadProjectOptions(isCurrent);
        for (const field of ["created", "updated"]) query.delete(`${field}Range`);
        if (page.cursor) query.set("cursor", page.cursor);
        const data = await dependencies.api(`${options.endpoint}?${query}`);
        if (!isCurrent()) return "superseded";
        // Rows and their applied query/cursor stack are one successful result.
        // Draft edits made while awaiting the response remain in the form.
        currentData = data;
        appliedPage = { ...candidate, nextCursor: data.nextCursor };
        refreshedAt = Date.now();
        rebuildSnapshot();
        dependencies.list.error(name, "");
        renderSessionSort();
        const context = options.beforeRender?.(data.items);
        options.render(data, context);
        applySessionLayout(context);
        options.afterRender?.();
        renderAppliedSessionFilters();
        renderSessionPagination();
        dependencies.renderFreshness();
        for (const root of document.querySelector(`#${kind}-filter`).querySelectorAll("[data-time-range]")) {
          dependencies.timeRange.controllerFor(root)?.refreshSummary();
        }
        dependencies.onApplied();
        dependencies.list.error(name, "");
        void loadSessionChatLabels(data, appliedPage, request);
        return "applied";
      } catch (error) {
        if (isCurrent() && !dependencies.isExpired()) dependencies.list.error(name, error.message + (currentData ? " 当前仍显示上次成功读取的结果。" : ""), () => loadSessionPage(page));
        return isCurrent() ? "failed" : "superseded";
      }
    }

    async function loadSessionPage(page) {
      return await loadSessionPageResult(page) === "applied";
    }

    function updateSessionGroupLabels() {
      const view = groupView;
      if (view.layout !== "chat") return;
      for (const group of view.groups) {
        const first = group.items[0];
        const named = group.items.find((item) => item.chatLabelResolved && item.chatLabel?.trim());
        const known = group.items.find((item) => item.chatMode || item.chatType) || first;
        group.title.textContent = named?.chatLabel || dependencies.shortIdentity(first.chatId)
          || `聊天不可用 · ${dependencies.shortIdentity(first.bindingId || first.sideId)}`;
        group.type.textContent = dependencies.chatModeLabel(known.chatMode || known.chatType, known.scopeKind);
        group.button.title = [group.title.textContent, first.chatId, first.appId].filter(Boolean).join("\n");
      }
    }

    function updateSessionGroupControls() {
      const view = groupView;
      document.querySelector(`#${kind}-group-actions`).hidden = view.layout === "compact";
      document.querySelector(`#${kind}-groups-expand`).disabled = dependencies.isExpired()
        || !view.groups.some((group) => view[view.layout].has(group.key));
      document.querySelector(`#${kind}-groups-collapse`).disabled = dependencies.isExpired()
        || !view.groups.some((group) => !view[view.layout].has(group.key));
    }

    function setSessionGroupsExpanded(expanded, groups = groupView.groups) {
      const view = groupView;
      if (dependencies.isExpired() || view.layout === "compact") return;
      for (const group of groups) {
        if (expanded) view[view.layout].delete(group.key);
        else view[view.layout].add(group.key);
        group.button.setAttribute("aria-expanded", String(expanded));
        for (const row of group.rows) row.hidden = !expanded;
      }
      updateSessionGroupControls();
      options.onLayout?.();
    }

    function applySessionLayout(context) {
      if (dependencies.isExpired()) return;
      const name = kind === "side" ? "sides" : "sessions";
      const identity = kind === "side" ? "sideId" : "bindingId";
      const body = document.querySelector(`#${name}-body`);
      const view = groupView;
      const selected = document.querySelector(`#${kind}-layout`).value;
      const layout = ["project", "chat"].includes(selected) ? selected : "compact";
      const grouped = layout !== "compact";
      view.layout = layout;
      view.groups = [];
      document.querySelector(`#${kind}-group-note`).hidden = !grouped;
      const rows = new Map([...body.children].filter((row) => row.dataset[identity])
        .map((row) => [row.dataset[identity], row]));
      const items = currentData?.items || [];
      const groups = new Map();
      for (const item of items) {
        const key = layout === "chat"
          ? JSON.stringify(item.chatId ? ["chat", item.appId || "", item.chatId] : ["missing", item[identity]])
          : layout === "project" ? JSON.stringify(item.projectAlias || null) : "";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(item);
      }
      body.replaceChildren();
      if (currentData && !items.length && document.querySelector(`#${name}-error`).hidden) {
        dependencies.list.empty(body, kind === "side" ? 7 : 9,
          `当前范围内没有${kind === "side" ? "临时" : "普通"}会话，可调整或重置筛选。`, "重置", () => resetSessionFilters());
      }
      for (const [key, groupItems] of groups) {
        const groupRows = groupItems.map((item) => rows.get(item[identity])).filter(Boolean);
        const expanded = !grouped || !view[layout].has(key);
        if (grouped) {
          const heading = document.createElement("tr");
          heading.className = "session-group-row";
          const label = document.createElement("th");
          label.setAttribute("colspan", kind === "side" ? "7" : "9");
          const button = document.createElement("button");
          button.type = "button";
          button.className = "session-group-toggle";
          button.setAttribute("aria-expanded", String(expanded));
          button.disabled = Boolean(dependencies.isExpired());
          const chevron = document.createElement("span");
          chevron.className = "session-group-chevron";
          chevron.setAttribute("aria-hidden", "true");
          const title = document.createElement("span");
          title.className = "session-group-title";
          title.textContent = groupItems[0].projectAlias || "项目不可用";
          const type = document.createElement("span");
          type.className = "badge session-group-type";
          type.hidden = layout !== "chat";
          const count = document.createElement("span");
          count.className = "session-group-count";
          count.textContent = `本页 ${groupItems.length} 条`;
          button.append(chevron, title, type, count);
          const group = { key, items: groupItems, rows: groupRows, button, title, type };
          view.groups.push(group);
          button.addEventListener("click", () => setSessionGroupsExpanded(
            button.getAttribute("aria-expanded") !== "true", [group]));
          label.append(button);
          heading.append(label);
          body.append(heading);
        }
        for (const row of groupRows) {
          row.hidden = !expanded;
          body.append(row);
        }
      }
      dependencies.list.labelRows(body);
      updateSessionGroupLabels();
      updateSessionGroupControls();
      options.onLayout?.(context);
    }

    function restore(query) {
      options.restoreControls(query);
      appliedPage = dependencies.firstSessionPage(query.toString());
      currentData = null;
      refreshedAt = null;
      rebuildSnapshot();
      document.querySelector(`#${name}-body`).replaceChildren();
      applySessionLayout();
      renderAppliedSessionFilters();
      renderSessionPagination();
      renderSessionSort();
    }

    // Omitting the page refreshes the applied result, without submitting drafts.
    async function load(page = appliedPage) {
      options.ensureChatFilter();
      return loadSessionPageResult(page);
    }

    function move(direction) {
      const candidate = dependencies.stepCursorPage(appliedPage, direction);
      if (candidate) return dependencies.navigate(candidate);
    }

    function renderFreshness() {
      dependencies.list.freshness("sessions", refreshedAt ?? null);
    }

    // Rebuild only when owned data changes; selection/layout reads reuse the same
    // deeply frozen copy without exposing mutable DTOs or exact action grants.
    function rebuildSnapshot() {
      cachedSnapshot = readonlyCopy({ items: currentData?.items || [], page: appliedPage, refreshedAt });
    }

    function snapshot() {
      return cachedSnapshot;
    }

    // Editors retain their exact row even after it leaves the displayed page.
    // Keep their grant consumption in sync with the cache only while it is owned.
    function updateActions(item, actions) {
      Object.assign(item.actions, actions);
      if (currentData?.items.includes(item)) rebuildSnapshot();
    }

    for (const button of document.querySelectorAll("[data-session-sort]")) {
      if (button.dataset.sessionSort === kind) button.addEventListener("click", () => sortSessionPage(button.dataset.sortBy));
    }
    document.querySelector(`#${kind}-layout`).addEventListener("change", () => applySessionLayout());
    document.querySelector(`#${kind}-groups-expand`).addEventListener("click", () => setSessionGroupsExpanded(true));
    document.querySelector(`#${kind}-groups-collapse`).addEventListener("click", () => setSessionGroupsExpanded(false));
    document.querySelector(`#${kind}-filter`).addEventListener("submit", (event) => {
      event.preventDefault();
      dependencies.navigate(sessionFilterPage());
    });
    document.querySelector(`#${kind}-filter-reset`).addEventListener("click", resetSessionFilters);
    document.querySelector(`#${kind}-page-size`).addEventListener("change", resizeSessionPage);
    document.querySelector(`#${name}-previous`).addEventListener("click", () => move("previous"));
    document.querySelector(`#${name}-next`).addEventListener("click", () => move("next"));

    return { load, move, sort: sortSessionPage, filterPage: sessionFilterPage, reset: resetSessionFilters,
      restore, snapshot, updateActions, appliedQuery: () => appliedPage.query,
      pageIdentity: () => JSON.stringify([appliedPage.query, appliedPage.cursor]), renderFreshness };
  }

  function init(options) {
    dependencies = options;
    return { session: create("session", options.session), side: create("side", options.side) };
  }

  return { init, invalidate, leave: invalidate, expire: invalidate };
})();
