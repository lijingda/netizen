"use strict";

window.NetizenProgressCard = (() => {
  const node = (name) => document.querySelector(`#global-card-${name}`);
  let snapshot = null;
  let draft = null;
  let preview = null;
  let loading = false;
  let pending = false;
  let expired = false;
  let generation = 0;
  let previewGeneration = 0;

  const wrapper = () => ({ progress_card_enabled: true, progress_card_settings: draft });
  const dirty = () => Boolean(snapshot) && JSON.stringify(draft) !== JSON.stringify(snapshot.settings);

  function render() {
    if (draft) renderProgressCardSettings(node, wrapper());
    node("fields").disabled = expired || loading || pending || !snapshot?.editable;
    node("save").disabled = expired || loading || pending || !snapshot?.actions.save || !dirty()
      || Boolean(progressCardSettingsError(wrapper()));
    node("cancel").disabled = expired || loading || pending || !dirty();
    node("unsaved").hidden = !dirty();
    node("save").textContent = pending ? "保存中…" : "保存配置";
    node("form").setAttribute("aria-busy", String(loading || pending));
  }

  function discard() {
    generation += 1;
    loading = false;
    draft = snapshot ? structuredClone(snapshot.settings) : null;
    render();
  }

  async function read() {
    const version = ++previewGeneration;
    preview = null;
    try {
      const value = await api("/api/v1/progress-card/settings");
      if (!expired && version === previewGeneration) preview = value.settings;
      return value.settings;
    } catch (error) {
      if (!expired && version === previewGeneration) setStatus(`全局过程卡配置读取失败：${error.message}`, true);
      return null;
    }
  }

  async function load() {
    if (expired || pending || !confirmDiscardChanges(dirty())) return false;
    const version = ++generation;
    loading = true;
    render();
    try {
      const value = await api("/api/v1/progress-card/settings");
      if (expired || version !== generation) return false;
      snapshot = value;
      previewGeneration += 1;
      preview = value.settings;
      draft = structuredClone(value.settings);
      node("note").textContent = value.editable
        ? "保存后，从后续任务开始使用；正在执行的卡片保持不变。" : "实例配置不可编辑。";
      return true;
    } catch (error) {
      if (!expired && version === generation) {
        if (snapshot) snapshot.actions.save = null;
        node("note").textContent = "读取失败，请刷新后重试。";
      }
      throw error;
    } finally {
      if (version === generation) { loading = false; render(); }
    }
  }

  async function save(event) {
    event.preventDefault();
    if (expired || loading || pending || !snapshot?.actions.save || !dirty()
        || progressCardSettingsError(wrapper())) return;
    const body = actionPayload(snapshot.actions.save, { settings: draft });
    snapshot.actions.save = null;
    pending = true;
    generation += 1;
    render();
    try {
      const value = await api("/api/v1/progress-card/settings", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (expired) return;
      snapshot = value;
      previewGeneration += 1;
      preview = value.settings;
      draft = structuredClone(value.settings);
      node("note").textContent = "全局过程卡配置已保存，从后续任务开始使用。";
      setStatus("全局过程卡配置已保存。");
    } catch (error) {
      if (!expired) {
        node("note").textContent = "请刷新核对当前配置后再保存；本次修改未自动重试。";
        setStatus(error.message, true);
      }
    } finally {
      pending = false;
      render();
    }
  }

  for (const field of node("fields").querySelectorAll("input, select")) {
    field.addEventListener("change", () => {
      if (expired || loading || pending || !snapshot?.editable) return;
      const settings = wrapper();
      changeSessionSettingsField(node, settings, [], field.id.slice("global-card-".length));
      draft = settings.progress_card_settings;
      render();
    });
  }
  node("form").addEventListener("submit", save);
  node("cancel").addEventListener("click", discard);
  return { load, read, current: () => preview, dirty, discard, busy: () => pending,
    expire() { expired = true; generation += 1; render(); } };
})();
