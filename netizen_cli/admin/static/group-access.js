"use strict";

window.NetizenGroupAccess = (() => {
  const node = (id) => document.querySelector(`#group-access-${id}`);
  const chats = window.createConfigChatList(node, {
    id: "group-access-group", sortOnAdd: true,
    changed: () => editor.changed(), blocked: () => editor.expired || editor.pending,
  });
  const editor = window.createConfigEditor({
    node, path: "/api/v1/group-access", savePath: "/api/v1/group-access/save",
    definition: () => ({ mode: node("mode").value, chatIds: chats.ids() }),
    initialize: () => chats.initialize(),
    restore(snapshot) {
      if (snapshot) {
        node("mode").value = snapshot.mode;
        chats.restore(snapshot.chatIds, snapshot.chats);
        node("current").textContent = `当前模式：${snapshot.mode === "all" ? "全部群聊" : "仅已启用的群聊"}`;
        node("risk").hidden = snapshot.mode !== "all";
      }
      chats.close();
    },
    render: () => chats.render(editor.blocked || !editor.snapshot),
    beforeSave(value) {
      if (value.chatIds.length > 1000) {
        setStatus("最多可启用 1000 个群聊，请删除超出数量的聊天 ID 后再保存。", true);
        return false;
      }
      return !(editor.snapshot.mode === "all" && value.mode === "only") ||
        window.NetizenConfirm.ask({ title: "限制群聊准入", confirmLabel: "保存配置", body: `切换为仅已启用的群聊？保存后只有名单中的 ${value.chatIds.length} 个群可以执行，其他群的新输入不会执行；实例管理员可在群里发送 /enable 启用。` });
    },
    loaded: "保存后应用于新的输入；正在执行的任务继续运行。",
    saved: "群聊准入已保存并生效。",
    savedStatus: "群聊准入已保存。",
    failed: "请刷新核对当前规则后再保存；本次修改未自动重试。",
  });
  for (const [field, event] of [["mode", "change"], ["chats", "input"]]) {
    node(field).addEventListener(event, editor.changed);
  }
  return { load: editor.load, dirty: editor.dirty, discard: editor.discard,
    busy: editor.busy, expire: editor.expire, closePickers: chats.close };
})();
