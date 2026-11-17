"use strict";

window.NetizenSessionBatch = (() => {
  let dependencies;
  // Selection belongs only to the successfully applied page, never to a filter draft.
  const sessionSelection = { page: null, ids: new Set() };
  let sessionBatch = null;
  const batchNode = (name) => document.querySelector(`#session-batch-${name}`);
  const sessionPageIdentity = () => dependencies.list.pageIdentity();

  function sessionBatchActions(query = new URLSearchParams(dependencies.list.appliedQuery() || "")) {
    const inventory = query.get("inventoryState") || "active";
    return {
      archive: inventory === "active" ? { key: "archive", path: "/api/v1/sessions/archive", label: "归档" } : null,
      delete: ["active", "archived", "lazy"].includes(inventory)
        ? { key: inventory === "lazy" ? "deleteLazy" : "deleteMaterialized",
          path: `/api/v1/sessions/delete-${inventory === "lazy" ? "lazy" : "materialized"}`, label: "删除" } : null,
    };
  }

  function sessionBatchGrant(session, action) {
    return action && session.actions?.[action.key];
  }

  function sessionBatchSelectable(session, actions) {
    return Boolean(sessionBatchGrant(session, actions.archive) || sessionBatchGrant(session, actions.delete));
  }

  function clearSessionSelection() {
    sessionSelection.ids.clear();
    sessionSelection.page = sessionPageIdentity();
    renderSessionSelection();
  }

  function reconcileSessionSelection(items, actions) {
    const page = sessionPageIdentity();
    const present = new Set(items.filter((item) => sessionBatchSelectable(item, actions)).map((item) => item.bindingId));
    if (page !== sessionSelection.page) sessionSelection.ids.clear();
    else for (const id of sessionSelection.ids) if (!present.has(id)) sessionSelection.ids.delete(id);
    sessionSelection.page = page;
  }

  function sessionSelectionCell(row, session) {
    const td = document.createElement("td"); td.className = "session-selection";
    const label = document.createElement("label"); label.className = "session-select-hit";
    const input = document.createElement("input"); input.type = "checkbox"; input.className = "session-select";
    input.setAttribute("aria-label", `选择会话：${dependencies.sessionName(session)} · ${session.shortId}`);
    input.addEventListener("change", () => {
      if (input.disabled) return;
      if (input.checked) sessionSelection.ids.add(session.bindingId);
      else sessionSelection.ids.delete(session.bindingId);
      renderSessionSelection();
    });
    label.append(input); td.append(label); row.append(td);
  }

  function renderSessionSelection(actions = sessionBatchActions()) {
    const items = dependencies.list.snapshot().items;
    const available = new Set(items.filter((item) => sessionBatchSelectable(item, actions)).map((item) => item.bindingId));
    const busy = Boolean(sessionBatch && sessionBatch.phase !== "finished");
    let hidden = 0;
    for (const row of document.querySelector("#sessions-body").children) {
      const input = row.querySelector(".session-select");
      if (!input) continue;
      input.checked = sessionSelection.ids.has(row.dataset.bindingId);
      input.disabled = Boolean(dependencies.isExpired()) || busy || !available.has(row.dataset.bindingId);
      input.title = available.has(row.dataset.bindingId) ? "" : "此会话不支持批量操作";
      if (input.checked && row.hidden) hidden += 1;
    }
    document.querySelector("#session-batch").hidden = !items.length;
    const selected = [...available].filter((id) => sessionSelection.ids.has(id)).length;
    const all = document.querySelector("#session-select-page");
    all.checked = available.size > 0 && selected === available.size;
    all.indeterminate = selected > 0 && selected < available.size;
    all.disabled = Boolean(dependencies.isExpired()) || busy || !available.size;
    batchNode("count").textContent = `已选 ${sessionSelection.ids.size} 项`
      + (hidden ? `（其中 ${hidden} 项在折叠分组中）` : "");
    for (const operation of ["archive", "delete"]) {
      const button = batchNode(operation);
      button.hidden = !items.some((item) => sessionBatchGrant(item, actions[operation]));
      button.disabled = Boolean(dependencies.isExpired()) || busy;
      // Keep empty-selection actions focusable so closing results can return focus
      // to the original trigger. The handler enforces this disabled state as well.
      button.setAttribute("aria-disabled", String(button.disabled || !sessionSelection.ids.size));
    }
    batchNode("clear").disabled = Boolean(dependencies.isExpired()) || busy || !sessionSelection.ids.size;
  }

  function sessionRunningOrUnconfirmed(session) {
    const runtime = session.runtime;
    return Boolean(runtime && (runtime.primaryStatusResolution === "local" || runtime.primaryStatus === "goal-active"
      || ["deferred", "unavailable"].includes(runtime.primaryStatusResolution)));
  }

  function sessionBatchConfirmation(action, targets, dropped, unsupported, trigger) {
    const count = targets.length;
    const archive = action.key === "archive", lazy = action.key === "deleteLazy";
    const body = [archive ? `将归档以下 ${count} 个会话，归档后可在“已归档”中恢复。`
      : lazy ? `将永久删除以下 ${count} 个尚未开始的会话。` : dependencies.materializedDeleteWarning];
    const current = targets.filter((item) => item.pointerState === "current").length;
    const running = targets.filter(sessionRunningOrUnconfirmed).length;
    if (current) body.push(`其中 ${current} 个是所在聊天的当前会话，执行后该聊天没有当前会话。`);
    if (running) body.push(`其中 ${running} 个正在运行或状态未确认，Codex 会关闭正在运行的任务。`);
    const skipped = [dropped ? `${dropped} 个已移出本页` : "", unsupported ? `${unsupported} 个不支持该操作` : ""].filter(Boolean);
    if (skipped.length) body.push(`另有 ${skipped.join("、")}，不会执行。`);
    return { title: archive ? "批量归档会话" : lazy ? "批量删除未开始的会话" : "批量永久删除会话",
      body: body.join("\n"), danger: !archive, trigger,
      confirmLabel: `${archive ? "归档" : "永久删除"} ${count} 个会话`,
      details: targets.map((item) => `${dependencies.sessionName(item)} · ${item.shortId} · ${item.projectAlias || "—"} · ${item.chatLabelResolved
        ? item.chatLabel || "聊天不可用" : item.chatId ? dependencies.locationFallback(item) : "聊天不可用"}`
        + (item.pointerState === "current" ? "（当前会话）" : "")
        + (sessionRunningOrUnconfirmed(item) ? `（${dependencies.runtimeLabel(item.runtime)}）` : "")),
    };
  }

  function sessionBatchSummary(batch) {
    return ["已完成", "未完成", "结果未确认", "未执行"].map((status) => {
      const count = batch.results.filter((item) => item.status === status).length;
      return count ? `${status} ${count} 项` : "";
    }).filter(Boolean).join("，") + "。";
  }

  function renderSessionBatch(batch) {
    if (dependencies.isExpired() || batch.detached) return;
    const finished = batch.phase === "finished";
    batchNode("title").textContent = `批量${batch.action.label}会话${finished ? "结果" : ""}`;
    batchNode("progress").textContent = finished ? sessionBatchSummary(batch)
      : batch.phase === "refreshing" ? "正在刷新列表…"
        : `正在${batch.action.label}第 ${batch.index + 1} / ${batch.results.length} 项`;
    batchNode("note").textContent = batch.refreshFailed ? "列表刷新失败，请手动刷新核对。" : "";
    batchNode("note").hidden = !batch.refreshFailed;
    batchNode("results").replaceChildren();
    for (const result of batch.results) {
      if (!result.status) continue;
      const item = document.createElement("li");
      item.textContent = `${dependencies.sessionName(result.session)} · ${result.session.shortId} · ${result.status}`
        + (result.reason ? `（${result.reason}）` : "");
      if (result.status !== "已完成") item.className = "error";
      batchNode("results").append(item);
    }
    batchNode("results").hidden = !batchNode("results").children.length;
    batchNode("stop").hidden = finished;
    batchNode("stop").disabled = batch.stopped || batch.phase !== "running";
    batchNode("close").hidden = !finished;
  }

  function stopSessionBatch() {
    const batch = sessionBatch;
    if (!batch || batch.phase === "finished") return;
    batch.stopped = true;
    batch.wake();
  }

  function sessionBatchControlVisible(control) {
    return control && !control.disabled && document.body.contains(control) && control.getClientRects().length > 0;
  }

  function closeSessionBatch() {
    const batch = sessionBatch;
    if (!batch || batch.phase !== "finished") return;
    sessionBatch = null;
    if (batchNode("dialog").open) batchNode("dialog").close();
    document.body.classList.toggle("session-batch-open", false);
    renderSessionSelection();
    // Navigation or deleting the last row can hide both the action and its list.
    const trigger = sessionBatchControlVisible(batch.trigger)
      ? batch.trigger : [...document.querySelectorAll("[data-refresh]")].find((button) => button.dataset.refresh === "sessions");
    if (!dependencies.isExpired() && sessionBatchControlVisible(trigger)) trigger.focus({ preventScroll: true });
  }

  function expireSessionBatch() {
    const batch = sessionBatch;
    if (!batch) return "";
    stopSessionBatch();
    sessionBatch = null;
    if (batchNode("dialog").open) batchNode("dialog").close();
    document.body.classList.toggle("session-batch-open", false);
    if (["preparing", "confirming"].includes(batch.phase)) return "登录已过期，批量操作未执行。";
    if (batch.phase === "finished") return `登录已过期。批量结果：${sessionBatchSummary(batch)}`;
    const completed = batch.results.filter((item) => item.status === "已完成").length;
    return `登录已过期，批量操作已停止：已完成 ${completed} 项，请重新登录后刷新核对。`;
  }

  async function runSessionBatch(operation, trigger) {
    if (dependencies.isExpired() || sessionBatch || !sessionSelection.ids.size) return;
    if (window.NetizenConfirm.busy()) {
      dependencies.setStatus("另一个确认窗口已打开，批量操作未执行。", true);
      return;
    }
    const action = sessionBatchActions()[operation];
    if (!action) return;
    const selected = new Set(sessionSelection.ids);
    const page = dependencies.list.snapshot().page, identity = sessionPageIdentity();
    const isCurrentPage = () => dependencies.isVisible() && identity === sessionPageIdentity();
    const batch = { action, trigger, phase: "preparing", stopped: false, results: [], index: 0 };
    batch.stop = new Promise((resolve) => { batch.wake = resolve; });
    sessionBatch = batch;
    renderSessionSelection();
    let confirmationCancelled = false;
    try {
      const refreshed = await dependencies.list.load(page);
      if (dependencies.isExpired() || batch.detached) return;
      if (refreshed === "superseded" || !isCurrentPage() || batch.stopped) {
        dependencies.setStatus("列表已变化，批量操作未执行。", true);
        return;
      }
      if (refreshed === "failed") {
        dependencies.setStatus("刷新当前页失败，未执行任何操作。", true);
        return;
      }
      const remaining = dependencies.list.snapshot().items.filter((item) => selected.has(item.bindingId));
      const byId = new Map(remaining.map((item) => [item.bindingId, item]));
      const targets = [...document.querySelector("#sessions-body").children]
        .map((row) => byId.get(row.dataset.bindingId)).filter((item) => item?.actions?.[action.key]);
      if (!targets.length) {
        dependencies.setStatus("所选会话已不在本页或不支持该操作，未执行。", true);
        return;
      }
      if (window.NetizenConfirm.busy()) {
        dependencies.setStatus("另一个确认窗口已打开，批量操作未执行。", true);
        return;
      }
      batch.phase = "confirming";
      if (!await window.NetizenConfirm.ask(sessionBatchConfirmation(action, targets,
        selected.size - remaining.length, remaining.length - targets.length, trigger))) {
        confirmationCancelled = true;
        return;
      }
      if (dependencies.isExpired() || batch.stopped || identity !== sessionPageIdentity()) return;
      batch.results = targets.map((session) => ({ session, status: null, reason: "" }));
      batch.phase = "running";
      renderSessionBatch(batch);
      document.body.classList.toggle("session-batch-open", true);
      batchNode("dialog").showModal(); batchNode("stop").focus();
      for (const [index, result] of batch.results.entries()) {
        if (batch.stopped || dependencies.isExpired()) break;
        batch.index = index;
        renderSessionBatch(batch);
        const response = dependencies.api(action.path, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(dependencies.actionPayload(result.session.actions[action.key])),
        });
        // The race stops observing batch results; api still handles global expiry.
        const outcome = await Promise.race([response.then(() => ({}), (error) => ({ error })), batch.stop]);
        if (dependencies.isExpired()) return;
        if (batch.stopped) {
          result.status = "结果未确认";
          result.reason = "已停止等待，服务端可能仍在完成";
          break;
        }
        if (!outcome.error) result.status = "已完成";
        else {
          result.reason = outcome.error.message;
          result.status = [409, 404].includes(outcome.error.status) ? "未完成" : "结果未确认";
          if (result.status === "结果未确认") break;
        }
      }
      for (const result of batch.results) if (!result.status) result.status = "未执行";
      clearSessionSelection();
      batch.phase = "refreshing";
      renderSessionBatch(batch);
      if (batch.detached) return;
      if (isCurrentPage()) {
        const refreshed = await dependencies.list.load(dependencies.list.snapshot().page);
        batch.refreshFailed = isCurrentPage() && refreshed === "failed";
      }
      if (dependencies.isExpired() || batch.detached) return;
      batch.phase = "finished";
      renderSessionBatch(batch);
      const message = sessionBatchSummary(batch) + (batch.refreshFailed ? " 列表刷新失败，请手动刷新核对。" : "");
      dependencies.setStatus(message, batch.refreshFailed || batch.results.some((item) => item.status !== "已完成"));
      batchNode("close").focus();
    } finally {
      if (sessionBatch === batch && batch.phase !== "finished") sessionBatch = null;
      renderSessionSelection();
      const focus = document.activeElement;
      const confirmation = document.querySelector("#admin-confirm");
      // Chrome can retain the closed dialog's cancel button until the next layout.
      const focusLost = !focus || focus === document.body || (!confirmation.open && confirmation.contains(focus));
      if (confirmationCancelled && !dependencies.isExpired() && focusLost
          && sessionBatchControlVisible(trigger)) trigger.focus({ preventScroll: true });
    }
  }

  function beforeRender(items) {
    const actions = sessionBatchActions();
    reconcileSessionSelection(items, actions);
    return actions;
  }

  function leave({ tab, sessionTab }) {
    if (tab !== "sessions" || sessionTab !== "ordinary") {
      clearSessionSelection();
      stopSessionBatch();
      if (sessionBatch?.phase === "confirming") window.NetizenConfirm.cancel();
    }
  }

  function expire() {
    const message = expireSessionBatch();
    clearSessionSelection();
    return message;
  }

  function pageHide() {
    clearSessionSelection();
    if (!sessionBatch) return;
    sessionBatch.detached = true;
    stopSessionBatch();
    window.NetizenConfirm.cancel();
    if (batchNode("dialog").open) batchNode("dialog").close();
    document.body.classList.toggle("session-batch-open", false);
    if (sessionBatch.phase === "finished") sessionBatch = null;
  }

  function init(options) {
    dependencies = options;
    document.querySelector("#session-select-page").addEventListener("change", (event) => {
      if (event.target.disabled) return;
      const actions = sessionBatchActions();
      sessionSelection.ids.clear();
      if (event.target.checked) for (const item of dependencies.list.snapshot().items) {
        if (sessionBatchSelectable(item, actions)) sessionSelection.ids.add(item.bindingId);
      }
      renderSessionSelection(actions);
    });
    batchNode("clear").addEventListener("click", () => {
      if (!batchNode("clear").disabled) clearSessionSelection();
    });
    for (const operation of ["archive", "delete"]) {
      batchNode(operation).addEventListener("click", () => runSessionBatch(operation, batchNode(operation)));
    }
    batchNode("stop").addEventListener("click", stopSessionBatch);
    batchNode("close").addEventListener("click", closeSessionBatch);
    batchNode("dialog").addEventListener("cancel", (event) => {
      event.preventDefault();
      closeSessionBatch();
    });
    batchNode("dialog").addEventListener("close", () => {
      const batch = sessionBatch;
      if (!batch || dependencies.isExpired() || batch.detached || batchNode("dialog").open) return;
      if (batch.phase === "finished") closeSessionBatch();
      else if (["running", "refreshing"].includes(batch.phase)) batchNode("dialog").showModal();
    });
  }

  return { init, beforeRender, onLayout: renderSessionSelection, selectionCell: sessionSelectionCell,
    leave, expire, pageHide };
})();
