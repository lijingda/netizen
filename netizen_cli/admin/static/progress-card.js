"use strict";

window.NetizenProgressCard = (() => {
  const node = (name) => document.querySelector(`#global-card-${name}`);
  let draft = null;
  let preview = null;
  let previewGeneration = 0;
  const wrapper = () => ({ progress_card_enabled: true, progress_card_settings: draft });
  const editor = window.createConfigEditor({
    node, path: "/api/v1/progress-card/settings", lockRead: true,
    definition: () => ({ settings: draft }),
    restore: (snapshot) => { draft = snapshot ? structuredClone(snapshot.settings) : null; },
    render() { if (draft) renderProgressCardSettings(node, wrapper()); },
    invalid: () => Boolean(progressCardSettingsError(wrapper())),
    accepted(value) { previewGeneration += 1; preview = value.settings; },
    loaded: "保存后，从后续任务开始使用；正在执行的卡片保持不变。",
    saved: "全局过程卡配置已保存，从后续任务开始使用。",
    savedStatus: "全局过程卡配置已保存。",
    failed: "请刷新核对当前配置后再保存；本次修改未自动重试。",
  });
  async function read() {
    const version = ++previewGeneration;
    preview = null;
    try {
      const value = await api("/api/v1/progress-card/settings");
      if (!editor.expired && version === previewGeneration) preview = value.settings;
      return value.settings;
    } catch (error) {
      if (!editor.expired && version === previewGeneration) setStatus(`全局过程卡配置读取失败：${error.message}`, true);
      return null;
    }
  }
  for (const field of node("fields").querySelectorAll("input, select")) {
    field.addEventListener("change", () => {
      if (editor.blocked || !editor.editable) return;
      const settings = wrapper();
      changeSessionSettingsField(node, settings, [], field.id.slice("global-card-".length));
      draft = settings.progress_card_settings;
      editor.changed();
    });
  }
  return { load: editor.load, read, current: () => preview, dirty: editor.dirty,
    discard: editor.discard, busy: editor.busy, expire: editor.expire };
})();
