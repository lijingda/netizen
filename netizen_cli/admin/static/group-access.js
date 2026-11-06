"use strict";

window.NetizenGroupAccess = (() => {
  const node = (id) => document.querySelector(`#group-access-${id}`);
  let snapshot = null;
  let baseline = "";
  let pending = false;
  let expired = false;
  let generation = 0;
  let picker = null;
  const names = new Map();

  function ids() {
    return [...new Set(node("chats").value.split(/\s+/).filter(Boolean))].sort();
  }

  function switching() { return snapshot?.mode === "all" && node("mode").value === "only"; }

  function definition() {
    return { mode: node("mode").value, chatIds: ids() };
  }

  function dirty() { return Boolean(snapshot) && JSON.stringify(definition()) !== baseline; }

  function renderChats(target, chats) {
    target.replaceChildren();
    for (const chat of chats) {
      const item = document.createElement("li");
      item.textContent = chat.name && chat.name !== chat.chatId ? `${chat.name} · ${chat.chatId}` : chat.chatId;
      target.append(item);
    }
  }

  function renderControls() {
    node("fields").disabled = expired || pending || !snapshot;
    picker?.setDisabled(expired || pending || !snapshot);
    node("save").disabled = expired || pending || !snapshot?.saveAction || !dirty();
    node("cancel").disabled = expired || pending || !dirty();
    node("unsaved").hidden = !dirty();
    renderChats(node("selected"), ids().map((chatId) => ({ chatId, name: names.get(chatId) })));
  }

  function discard() {
    generation += 1;
    if (snapshot) {
      node("mode").value = snapshot.mode;
      node("chats").value = snapshot.chatIds.join("\n");
      node("current").textContent = `当前模式：${snapshot.mode === "all" ? "全部群聊" : "仅已启用的群聊"}`;
      node("risk").hidden = snapshot.mode !== "all";
      names.clear();
      for (const chat of snapshot.chats) names.set(chat.chatId, chat.name);
    }
    picker?.close();
    baseline = JSON.stringify(definition());
    renderControls();
  }

  async function load() {
    if (expired || pending || !confirmDiscardChanges(dirty())) return false;
    if (!picker) picker = window.createChatPicker(node("picker"), {
      id: "group-access-group", label: "添加群聊", fetchPage: fetchChatPage,
      onChange(chat) {
        if (!chat || expired || pending) return;
        generation += 1;
        names.set(chat.chatId, chat.name);
        node("chats").value = [...new Set([...ids(), chat.chatId])].join("\n");
        picker.reset();
        renderControls();
      },
    });
    const version = ++generation;
    const value = await api("/api/v1/group-access");
    if (expired || version !== generation) return false;
    snapshot = value;
    discard();
    node("note").textContent = value.saveAction ? "保存后应用于新的输入；正在执行的任务继续运行。" : "实例配置不可编辑。";
    return true;
  }

  async function save(event) {
    event.preventDefault();
    if (expired || pending || !snapshot?.saveAction || !dirty()) return;
    const value = definition();
    if (value.chatIds.length > 1000) {
      setStatus("最多可启用 1000 个群聊，请删除超出数量的聊天 ID 后再保存。", true);
      return;
    }
    const body = JSON.stringify(actionPayload(snapshot.saveAction, value));
    if (switching() && !window.confirm(`切换为仅已启用的群聊？保存后只有名单中的 ${value.chatIds.length} 个群可以执行，其他群的新输入不会执行；实例管理员可在群里发送 /enable 启用。`)) return;
    pending = true;
    generation += 1;
    renderControls();
    try {
      const result = await api("/api/v1/group-access/save", {
        method: "POST", headers: { "Content-Type": "application/json" }, body,
      });
      if (expired) return;
      snapshot = result;
      discard();
      node("note").textContent = "群聊准入已保存并生效。";
      setStatus("群聊准入已保存。");
    } catch (error) {
      if (!expired) {
        snapshot.saveAction = null;
        node("note").textContent = "请刷新核对当前规则后再保存；本次修改未自动重试。";
        setStatus(error.message, true);
      }
    } finally {
      pending = false;
      renderControls();
    }
  }

  for (const [field, event] of [["mode", "change"], ["chats", "input"]]) {
    node(field).addEventListener(event, () => { generation += 1; renderControls(); });
  }
  node("form").addEventListener("submit", save);
  node("cancel").addEventListener("click", discard);
  return { load, dirty, discard, closePickers: () => picker?.close(), busy: () => pending,
    expire() { expired = true; generation += 1; renderControls(); } };
})();
