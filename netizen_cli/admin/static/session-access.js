"use strict";

window.NetizenSessionAccess = (() => {
  const node = (id) => document.querySelector(`#session-access-${id}`);
  const groups = ["discovery", "readSend"];
  const pickers = new Map();
  let snapshot = null;
  let baseline = "";
  let pending = false;
  let expired = false;
  let generation = 0;

  function rule(group) {
    const mode = node(`${group}-mode`).value;
    return { mode, chatIds: ["only", "exclude"].includes(mode)
      ? [...new Set(node(`${group}-chats`).value.split(/\s+/).filter(Boolean))].sort() : [] };
  }

  function definition() {
    return Object.fromEntries(groups.map((group) => [group, rule(group)]));
  }

  function dirty() {
    return Boolean(snapshot) && JSON.stringify(definition()) !== baseline;
  }

  function renderControls() {
    node("fields").disabled = expired || pending || !snapshot;
    for (const group of groups) {
      const listed = ["only", "exclude"].includes(node(`${group}-mode`).value);
      node(`${group}-targets`).hidden = !listed;
      node(`${group}-chats`).disabled = expired || pending || !snapshot || !listed;
      pickers.get(group)?.setDisabled(expired || pending || !snapshot || !listed);
    }
    node("save").disabled = expired || pending || !snapshot?.saveAction || !dirty();
    node("cancel").disabled = expired || pending || !dirty();
  }

  function discard() {
    generation += 1;
    for (const group of groups) {
      if (snapshot) {
        node(`${group}-mode`).value = snapshot[group].mode;
        node(`${group}-chats`).value = snapshot[group].chatIds.join("\n");
      }
      pickers.get(group)?.close();
    }
    baseline = JSON.stringify(definition());
    renderControls();
  }

  function initializePickers() {
    for (const group of groups) {
      if (pickers.has(group)) continue;
      pickers.set(group, window.createChatPicker(node(`${group}-picker`), {
        id: `session-access-${group}-group`, label: "添加群聊", fetchPage: fetchChatPage,
        onChange(chat) {
          if (chat && !pending && !expired) {
            generation += 1;
            const ids = new Set(node(`${group}-chats`).value.split(/\s+/).filter(Boolean));
            ids.add(chat.chatId);
            node(`${group}-chats`).value = [...ids].join("\n");
            pickers.get(group).reset();
            renderControls();
          }
        },
      }));
    }
  }

  async function load() {
    if (expired || pending || !confirmDiscardChanges(dirty())) return false;
    initializePickers();
    const version = ++generation;
    const value = await api("/api/v1/session-tool-access");
    // A late refresh must not overwrite edits made while the GET was pending.
    if (expired || version !== generation) return false;
    snapshot = value;
    discard();
    node("note").textContent = value.saveAction ? "保存后立即应用于新的工具访问。" : "实例配置不可编辑。";
    return true;
  }

  async function save(event) {
    event.preventDefault();
    if (expired || pending || !snapshot?.saveAction || !dirty()) return;
    const body = actionPayload(snapshot.saveAction, definition());
    pending = true;
    generation += 1;
    renderControls();
    try {
      const value = await api("/api/v1/session-tool-access/save", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (expired) return;
      snapshot = value;
      discard();
      node("note").textContent = "跨会话工具权限已保存并生效。";
      setStatus("跨会话工具权限已保存。");
    } catch (error) {
      if (!expired) {
        // A consumed grant or unknown result is never silently replayed.
        snapshot.saveAction = null;
        node("note").textContent = "请刷新核对当前权限后再保存；本次修改未自动重试。";
        setStatus(error.message, true);
      }
    } finally {
      pending = false;
      renderControls();
    }
  }

  for (const group of groups) {
    for (const field of ["mode", "chats"]) {
      node(`${group}-${field}`).addEventListener(field === "mode" ? "change" : "input", () => {
        generation += 1;
        renderControls();
      });
    }
  }
  node("form").addEventListener("submit", save);
  node("cancel").addEventListener("click", discard);
  return {
    load, dirty, discard, busy: () => pending,
    expire() { expired = true; generation += 1; renderControls(); },
  };
})();
