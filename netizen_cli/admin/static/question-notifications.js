"use strict";

window.NetizenQuestionNotifications = (() => {
  const node = (name) => document.querySelector(`#question-notifications-${name}`);
  let snapshot = null;
  let draft = null;
  let loading = false;
  let pending = false;
  let expired = false;
  let generation = 0;
  const dirty = () => Boolean(snapshot) && draft !== snapshot.settings.mentionInitiator;

  function render() {
    node("enabled").checked = Boolean(draft);
    node("fields").disabled = expired || loading || pending || !snapshot?.editable;
    node("save").disabled = expired || loading || pending || !snapshot?.actions.save || !dirty();
    node("cancel").disabled = expired || loading || pending || !dirty();
    node("unsaved").hidden = !dirty();
    node("save").textContent = pending ? "保存中…" : "保存配置";
    node("form").setAttribute("aria-busy", String(loading || pending));
  }

  function discard() {
    generation += 1;
    loading = false;
    draft = snapshot ? snapshot.settings.mentionInitiator : null;
    render();
  }

  async function load() {
    if (expired || pending || !confirmDiscardChanges(dirty())) return false;
    const version = ++generation;
    loading = true;
    render();
    try {
      const value = await api("/api/v1/question-notifications/settings");
      if (expired || version !== generation) return false;
      snapshot = value;
      draft = value.settings.mentionInitiator;
      node("note").textContent = value.editable
        ? "保存后对新发送的问题卡生效，已发送卡片不会补发提醒。" : "实例配置不可编辑。";
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
    if (expired || loading || pending || !snapshot?.actions.save || !dirty()) return;
    const body = actionPayload(snapshot.actions.save, { settings: { mentionInitiator: draft } });
    snapshot.actions.save = null;
    pending = true;
    generation += 1;
    render();
    try {
      const value = await api("/api/v1/question-notifications/settings", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (expired) return;
      snapshot = value;
      draft = value.settings.mentionInitiator;
      node("note").textContent = "提问通知配置已保存，对新发送的问题卡生效。";
      setStatus("提问通知配置已保存。");
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

  node("enabled").addEventListener("change", () => {
    if (expired || loading || pending || !snapshot?.editable) return;
    draft = node("enabled").checked;
    render();
  });
  node("form").addEventListener("submit", save);
  node("cancel").addEventListener("click", discard);
  return { load, dirty, discard, busy: () => pending,
    expire() { expired = true; generation += 1; render(); } };
})();
