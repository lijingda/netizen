"use strict";

window.NetizenSessionAccess = (() => {
  const node = (id) => document.querySelector(`#session-access-${id}`);
  const groups = ["discovery", "readSend"];
  const chats = new Map(groups.map((group) => [group, window.createConfigChatList(
    (name) => node(`${group}-${name}`), {
      id: `session-access-${group}-group`,
      changed: () => editor.changed(), blocked: () => editor.expired || editor.pending,
    },
  )]));
  const listed = (group) => ["only", "exclude"].includes(node(`${group}-mode`).value);
  const closePickers = () => { for (const chat of chats.values()) chat.close(); };
  const editor = window.createConfigEditor({
    node, path: "/api/v1/session-tool-access", savePath: "/api/v1/session-tool-access/save",
    definition: () => Object.fromEntries(groups.map((group) => [group, {
      mode: node(`${group}-mode`).value, chatIds: listed(group) ? chats.get(group).ids() : [],
    }])),
    initialize() { for (const chat of chats.values()) chat.initialize(); },
    restore(snapshot) {
      for (const group of groups) {
        if (snapshot) {
          node(`${group}-mode`).value = snapshot[group].mode;
          chats.get(group).restore(snapshot[group].chatIds);
        }
      }
      closePickers();
    },
    render() {
      for (const group of groups) {
        const disabled = editor.blocked || !editor.snapshot || !listed(group);
        node(`${group}-targets`).hidden = !listed(group);
        node(`${group}-chats`).disabled = disabled;
        chats.get(group).render(disabled);
      }
    },
    loaded: "保存后立即应用于新的工具访问。",
    saved: "跨会话工具权限已保存并生效。",
    savedStatus: "跨会话工具权限已保存。",
    failed: "请刷新核对当前权限后再保存；本次修改未自动重试。",
  });
  for (const group of groups) {
    for (const field of ["mode", "chats"]) {
      node(`${group}-${field}`).addEventListener(field === "mode" ? "change" : "input", editor.changed);
    }
  }
  return { load: editor.load, dirty: editor.dirty, discard: editor.discard,
    busy: editor.busy, expire: editor.expire, closePickers };
})();
