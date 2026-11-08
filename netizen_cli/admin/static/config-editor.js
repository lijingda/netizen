"use strict";

// The pages own their fields; this helper owns reads, drafts and one-use saves.
window.createConfigEditor = (options) => {
  const { node, definition, restore } = options;
  let baseline = "";
  let generation = 0;
  const editor = {
    snapshot: null, pending: false, loading: false, expired: false,
    get blocked() { return editor.expired || editor.loading || editor.pending; },
    get editable() { return editor.snapshot && (editor.snapshot.editable ?? true); },
    dirty: () => Boolean(editor.snapshot) && JSON.stringify(definition()) !== baseline,
    busy: () => editor.pending,
    changed() { generation += 1; render(); },
    discard() {
      generation += 1;
      editor.loading = false;
      restore(editor.snapshot);
      baseline = JSON.stringify(definition());
      render();
    },
    async load() {
      if (editor.expired || editor.pending || !confirmDiscardChanges(editor.dirty())) return false;
      options.initialize?.();
      const version = ++generation;
      if (options.lockRead) { editor.loading = true; render(); }
      try {
        const value = await api(options.path);
        // Editing, discarding or expiry invalidates a pending read.
        if (editor.expired || version !== generation) return false;
        accept(value);
        node("note").textContent = (options.lockRead ? value.editable : value.saveAction)
          ? options.loaded : "实例配置不可编辑。";
        return true;
      } catch (error) {
        if (options.lockRead && !editor.expired && version === generation) {
          clearGrant();
          node("note").textContent = "读取失败，请刷新后重试。";
        }
        throw error;
      } finally {
        if (version === generation) { editor.loading = false; render(); }
      }
    },
    expire() { editor.expired = true; generation += 1; render(); },
  };
  const grant = () => editor.snapshot?.actions ? editor.snapshot.actions.save : editor.snapshot?.saveAction;
  function clearGrant() {
    if (editor.snapshot?.actions) editor.snapshot.actions.save = null;
    else if (editor.snapshot) editor.snapshot.saveAction = null;
  }
  function render() {
    options.render?.();
    node("fields").disabled = editor.blocked || !editor.editable;
    node("save").disabled = editor.blocked || !grant() || !editor.dirty() || Boolean(options.invalid?.());
    node("cancel").disabled = editor.blocked || !editor.dirty();
    node("unsaved").hidden = !editor.dirty();
    if (options.lockRead) {
      node("save").textContent = editor.pending ? "保存中…" : "保存配置";
      node("form").setAttribute("aria-busy", String(editor.loading || editor.pending));
    }
  }
  function accept(value) {
    editor.snapshot = value;
    editor.discard();
    options.accepted?.(value);
  }
  async function save(event) {
    event.preventDefault();
    if (editor.blocked || !grant() || !editor.dirty() || options.invalid?.()) return;
    const fields = definition();
    const body = JSON.stringify(actionPayload(grant(), fields));
    if (options.beforeSave && !options.beforeSave(fields)) return;
    clearGrant();
    editor.pending = true;
    generation += 1;
    render();
    try {
      const value = await api(options.savePath || options.path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body,
      });
      if (editor.expired) return;
      accept(value);
      node("note").textContent = options.saved;
      setStatus(options.savedStatus);
    } catch (error) {
      if (!editor.expired) {
        node("note").textContent = options.failed;
        setStatus(error.message, true);
      }
    } finally {
      editor.pending = false;
      render();
    }
  }
  node("form").addEventListener("submit", save);
  node("cancel").addEventListener("click", editor.discard);
  return editor;
};

// Exact IDs remain the form value; resolved names are presentation only.
window.createConfigChatList = (node, { id, changed, blocked, sortOnAdd = false }) => {
  let picker = null;
  const names = new Map();
  const rawIds = () => node("chats").value.split(/\s+/).filter(Boolean);
  const ids = () => [...new Set(rawIds())].sort();
  return {
    ids,
    initialize() {
      if (picker) return;
      picker = window.createChatPicker(node("picker"), {
        id, label: "添加群聊", fetchPage: fetchChatPage,
        onChange(chat) {
          if (!chat || blocked()) return;
          names.set(chat.chatId, chat.name);
          node("chats").value = [...new Set([...(sortOnAdd ? ids() : rawIds()), chat.chatId])].join("\n");
          picker.reset();
          changed();
        },
      });
    },
    restore(chatIds, chats = []) {
      node("chats").value = chatIds.join("\n");
      names.clear();
      for (const chat of chats) names.set(chat.chatId, chat.name);
    },
    render(disabled) {
      picker?.setDisabled(disabled);
      const selected = node("selected");
      if (!selected) return;
      selected.replaceChildren();
      for (const chatId of ids()) {
        const item = document.createElement("li");
        const name = names.get(chatId);
        item.textContent = name && name !== chatId ? `${name} · ${chatId}` : chatId;
        selected.append(item);
      }
    },
    close: () => picker?.close(),
  };
};
