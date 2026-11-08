"use strict";

window.NetizenQuestionNotifications = (() => {
  const node = (name) => document.querySelector(`#question-notifications-${name}`);
  let draft = null;
  const editor = window.createConfigEditor({
    node, path: "/api/v1/question-notifications/settings", lockRead: true,
    definition: () => ({ settings: { mentionInitiator: draft } }),
    restore: (snapshot) => { draft = snapshot ? snapshot.settings.mentionInitiator : null; },
    render() { node("enabled").checked = Boolean(draft); },
    loaded: "保存后对新发送的问题卡生效，已发送卡片不会补发提醒。",
    saved: "提问通知配置已保存，对新发送的问题卡生效。",
    savedStatus: "提问通知配置已保存。",
    failed: "请刷新核对当前配置后再保存；本次修改未自动重试。",
  });
  node("enabled").addEventListener("change", () => {
    if (editor.blocked || !editor.editable) return;
    draft = node("enabled").checked;
    editor.changed();
  });
  return { load: editor.load, dirty: editor.dirty, discard: editor.discard,
    busy: editor.busy, expire: editor.expire };
})();
