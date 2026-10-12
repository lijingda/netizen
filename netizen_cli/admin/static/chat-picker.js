/* Shared, remote-search group combobox. It owns no credentials or chat directory. */
(() => {
  "use strict";

  window.createChatPicker = function createChatPicker(root, options) {
    const { id, label = "群聊", fetchPage, onChange = () => {},
      placeholder = "请选择群聊", emptyOption = null } = options;
    if (!id || typeof fetchPage !== "function") throw new Error("群聊选择器缺少配置");
    const make = (tag, className, text) => {
      const node = document.createElement(tag);
      node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    };
    const wrapper = make("div", "chat-picker");
    const caption = make("label", "chat-picker-label", label);
    caption.htmlFor = id;
    const control = make("div", "chat-picker-control");
    const trigger = make("button", "chat-picker-trigger");
    trigger.id = id;
    trigger.type = "button";
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-controls", `${id}-popup`);
    const selectedAvatar = make("span", "chat-picker-selected-avatar");
    const summary = make("span", "chat-picker-summary");
    const chevron = make("span", "chat-picker-chevron", "⌄");
    chevron.setAttribute("aria-hidden", "true");
    trigger.append(selectedAvatar, summary, chevron);
    const input = make("input", "chat-picker-input");
    input.id = `${id}-search`;
    input.type = "text";
    input.placeholder = "搜索群名";
    input.setAttribute("aria-label", "搜索群名");
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    input.setAttribute("aria-controls", `${id}-options`);
    input.setAttribute("aria-describedby", `${id}-selection ${id}-hint`);
    const clear = make("button", "chat-picker-clear", "×");
    clear.type = "button";
    clear.setAttribute("aria-label", "清除群聊");
    const popup = make("div", "chat-picker-popup");
    popup.id = `${id}-popup`;
    popup.setAttribute("role", "dialog");
    popup.setAttribute("aria-label", `${label}选择`);
    const list = make("div", "chat-picker-options");
    list.id = `${id}-options`;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", `${label}候选`);
    const status = make("p", "chat-picker-status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.setAttribute("aria-atomic", "true");
    const retry = make("button", "chat-picker-retry", "重试");
    retry.type = "button";
    const more = make("button", "chat-picker-more", "加载更多");
    more.type = "button";
    const hint = make("p", "chat-picker-hint", "仅支持选择机器人当前已加入且可访问的群聊。");
    hint.id = `${id}-hint`;
    const selectedId = make("p", "chat-picker-selected-id");
    selectedId.id = `${id}-selection`;
    trigger.setAttribute("aria-describedby", selectedId.id);
    popup.append(input, selectedId, list, status, retry, more, hint);
    control.append(trigger, clear, popup);
    wrapper.append(caption, control);
    root.replaceChildren(wrapper);

    let selection = null;
    let query = "";
    let items = [];
    let nextCursor = null;
    let failedCursor = null;
    let notice = "";
    let error = "";
    let expanded = false;
    let disabled = false;
    let loading = false;
    let requested = false;
    let activeIndex = -1;
    let revision = 0;
    let timer = null;
    let composing = false;
    let renderedAvatarUrl;

    function candidates() { return emptyOption === null ? items : [null, ...items]; }

    function normalize(item) {
      return item && typeof item.chatId === "string" && item.chatId ? {
        chatId: item.chatId,
        name: typeof item.name === "string" && item.name ? item.name : item.chatId,
        avatarUrl: typeof item.avatarUrl === "string" && item.avatarUrl ? item.avatarUrl : null,
      } : null;
    }

    function avatar(url) {
      const frame = make("span", "chat-picker-avatar");
      frame.setAttribute("aria-hidden", "true");
      const fallback = make("span", "chat-picker-avatar-fallback");
      frame.append(fallback);
      if (url) {
        const image = make("img", "chat-picker-avatar-image");
        image.alt = "";
        image.referrerPolicy = "no-referrer";
        image.addEventListener("load", () => { fallback.hidden = true; });
        image.addEventListener("error", () => { image.hidden = true; fallback.hidden = false; });
        image.src = url;
        frame.append(image);
      }
      return frame;
    }

    function invalidate() {
      revision += 1;
      if (timer !== null) clearTimeout(timer);
      timer = null;
    }

    function render() {
      trigger.disabled = disabled;
      trigger.setAttribute("aria-expanded", String(expanded));
      trigger.setAttribute("data-selected", String(Boolean(selection)));
      summary.textContent = selection?.name || placeholder;
      trigger.setAttribute("aria-label", `${label}：${summary.textContent}`);
      trigger.title = selection ? `${selection.name}\n${selection.chatId}` : placeholder;
      input.disabled = disabled;
      input.setAttribute("aria-expanded", String(expanded));
      list.setAttribute("aria-busy", String(loading));
      clear.hidden = !selection || disabled;
      clear.disabled = disabled;
      popup.hidden = !expanded;
      selectedAvatar.hidden = !selection;
      control.setAttribute("data-selected", String(Boolean(selection)));
      const avatarUrl = selection?.avatarUrl || null;
      if (renderedAvatarUrl !== avatarUrl) {
        selectedAvatar.replaceChildren(avatar(avatarUrl));
        renderedAvatarUrl = avatarUrl;
      }
      selectedId.hidden = !selection;
      selectedId.textContent = selection ? `${selection.name}\n${selection.chatId}` : "";
      list.replaceChildren();
      const choices = candidates();
      choices.forEach((item, index) => {
        const row = make("div", item ? "chat-picker-option" : "chat-picker-empty-option");
        row.id = `${id}-option-${index}`;
        row.setAttribute("role", "option");
        const selected = item ? selection?.chatId === item.chatId : !selection;
        row.setAttribute("aria-selected", String(selected));
        row.setAttribute("data-active", String(index === activeIndex));
        const text = make("span", "chat-picker-text");
        text.append(make("span", "chat-picker-name", item?.name || emptyOption));
        if (item) {
          text.append(make("span", "chat-picker-id", item.chatId));
          row.append(avatar(item.avatarUrl));
        }
        const check = make("span", "chat-picker-check", selected ? "✓" : "");
        check.setAttribute("aria-hidden", "true");
        row.append(text, check);
        // Keep focus on the combobox when choosing an option with a pointer.
        row.addEventListener("pointerdown", (event) => event.preventDefault());
        row.addEventListener("click", () => choose(item));
        list.append(row);
      });
      if (expanded && activeIndex >= 0 && activeIndex < choices.length) {
        input.setAttribute("aria-activedescendant", `${id}-option-${activeIndex}`);
        list.children[activeIndex]?.scrollIntoView?.({ block: "nearest" });
      } else input.removeAttribute("aria-activedescendant");
      status.className = error ? "chat-picker-status error" : "chat-picker-status";
      status.textContent = error ? `群聊读取失败：${error}`
        : loading ? (items.length ? "正在加载更多群聊…" : "正在查找群聊…")
          : notice || (items.length ? `已加载 ${items.length} 个群聊`
            : query ? "未找到匹配群聊，请更换关键词。" : "暂无可选择的群聊。");
      retry.hidden = !error;
      retry.disabled = disabled || loading;
      more.hidden = !nextCursor || Boolean(error);
      more.disabled = disabled || loading;
    }

    async function load(cursor = null) {
      if (disabled || !expanded) return;
      // Disabling the focused paging/retry button makes Chromium blur to null.
      // Keep focus inside before rendering busy state, or focusout closes the
      // popup and invalidates the very request the user just started.
      if (document.activeElement === more || document.activeElement === retry) input.focus();
      const current = ++revision;
      const requestedQuery = query;
      loading = true;
      requested = true;
      error = "";
      failedCursor = cursor;
      render();
      try {
        const page = await fetchPage({ query: requestedQuery, cursor });
        if (current !== revision || disabled || !expanded) return;
        const merged = new Map((cursor ? items : []).map((item) => [item.chatId, item]));
        for (const item of page.items || []) {
          const chat = normalize(item);
          if (chat && !merged.has(chat.chatId)) merged.set(chat.chatId, chat);
        }
        items = [...merged.values()];
        nextCursor = typeof page.nextCursor === "string" && page.nextCursor && page.nextCursor !== cursor
          ? page.nextCursor : null;
        notice = typeof page.notice === "string" ? page.notice : "";
        loading = false;
        render();
      } catch (reason) {
        if (current !== revision || disabled || !expanded) return;
        loading = false;
        error = reason instanceof Error ? reason.message : "请稍后重试";
        if (!error) error = "请稍后重试";
        render();
      }
    }

    function open() {
      if (disabled || expanded) return;
      expanded = true;
      render();
      const bounds = control.getBoundingClientRect();
      const width = popup.getBoundingClientRect().width;
      popup.style.left = `${Math.max(12 - bounds.left, Math.min(0, window.innerWidth - 12 - bounds.left - width))}px`;
      input.focus();
      if (!requested) void load();
    }

    function close({ restoreFocus = false } = {}) {
      expanded = false;
      activeIndex = -1;
      invalidate();
      loading = false;
      composing = false;
      input.value = "";
      query = "";
      items = [];
      nextCursor = null;
      notice = "";
      error = "";
      requested = false;
      render();
      if (restoreFocus) trigger.focus();
    }

    function setSelection(item) {
      close();
      selection = normalize(item);
      render();
    }

    function choose(item) {
      if (disabled || !expanded) return;
      setSelection(item);
      trigger.focus();
      onChange(selection ? { ...selection } : null);
    }

    function search() {
      if (disabled || !expanded) return;
      invalidate();
      query = input.value.trim();
      items = [];
      nextCursor = null;
      activeIndex = -1;
      notice = "";
      error = "";
      expanded = true;
      requested = false;
      loading = true;
      render();
      if (!composing) timer = setTimeout(() => { timer = null; void load(); }, 250);
    }

    trigger.addEventListener("click", () => {
      if (expanded) close({ restoreFocus: true });
      else open();
    });
    trigger.addEventListener("keydown", (event) => {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        open();
      }
    });
    input.addEventListener("input", search);
    input.addEventListener("compositionstart", () => { composing = true; });
    input.addEventListener("compositionend", () => { composing = false; search(); });
    input.addEventListener("keydown", (event) => {
      if (disabled || !expanded || composing || event.isComposing) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        open();
        const choices = candidates();
        if (choices.length) {
          activeIndex = event.key === "ArrowDown" ? (activeIndex + 1) % choices.length
            : (activeIndex < 0 ? choices.length - 1 : (activeIndex - 1 + choices.length) % choices.length);
          render();
        }
      } else if (event.key === "Enter" && expanded) {
        event.preventDefault();
        const choices = candidates();
        if (activeIndex >= 0 && activeIndex < choices.length) choose(choices[activeIndex]);
      }
    });
    wrapper.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !expanded || composing || event.isComposing) return;
      event.preventDefault();
      event.stopPropagation?.();
      close({ restoreFocus: true });
    });
    wrapper.addEventListener("focusout", (event) => {
      if (event.relatedTarget && !wrapper.contains(event.relatedTarget)) close();
    });
    document.addEventListener("pointerdown", (event) => {
      if (expanded && !wrapper.contains(event.target)) close();
    });
    clear.addEventListener("click", () => {
      if (disabled) return;
      setSelection(null);
      onChange(null);
      trigger.focus();
    });
    retry.addEventListener("click", () => { if (!loading) void load(failedCursor); });
    more.addEventListener("click", () => { if (!loading && nextCursor) void load(nextCursor); });
    render();

    return {
      setSelection,
      updateSelectionAvatar(chatId, avatarUrl) {
        if (selection?.chatId !== chatId) return;
        selection = normalize({ ...selection, avatarUrl });
        render();
      },
      getSelection: () => selection ? { ...selection } : null,
      setDisabled(value) { disabled = Boolean(value); if (disabled) close(); else render(); },
      close,
      reset: () => setSelection(null),
      focus() { if (!disabled) { open(); input.focus(); } },
    };
  };
})();
