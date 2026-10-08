/* Shared, remote-search group combobox. It owns no credentials or chat directory. */
(() => {
  "use strict";

  window.createChatPicker = function createChatPicker(root, options) {
    const { id, label = "群聊", fetchPage, onChange = () => {},
      placeholder = "输入群名搜索或选择群聊" } = options;
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
    const input = make("input", "chat-picker-input");
    input.id = id;
    input.type = "text";
    input.placeholder = placeholder;
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    input.setAttribute("aria-controls", `${id}-options`);
    input.setAttribute("aria-describedby", `${id}-hint`);
    const clear = make("button", "chat-picker-clear", "×");
    clear.type = "button";
    clear.setAttribute("aria-label", "清除群聊");
    const popup = make("div", "chat-picker-popup");
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
    popup.append(list, status, retry, more);
    control.append(input, clear, popup);
    wrapper.append(caption, control, hint);
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
    let restoringFocus = false;

    function invalidate() {
      revision += 1;
      if (timer !== null) clearTimeout(timer);
      timer = null;
    }

    function render() {
      input.disabled = disabled;
      input.setAttribute("aria-expanded", String(expanded));
      list.setAttribute("aria-busy", String(loading));
      clear.hidden = !input.value;
      clear.disabled = disabled;
      popup.hidden = !expanded;
      list.replaceChildren();
      items.forEach((item, index) => {
        const row = make("div", "chat-picker-option");
        row.id = `${id}-option-${index}`;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(selection?.chatId === item.chatId));
        row.setAttribute("data-active", String(index === activeIndex));
        row.append(make("span", "chat-picker-name", item.name),
          make("span", "chat-picker-id", item.chatId));
        // Keep focus on the combobox when choosing an option with a pointer.
        row.addEventListener("pointerdown", (event) => event.preventDefault());
        row.addEventListener("click", () => choose(item));
        list.append(row);
      });
      if (expanded && activeIndex >= 0 && items[activeIndex]) {
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
          if (!item || typeof item.chatId !== "string" || !item.chatId) continue;
          if (!merged.has(item.chatId)) merged.set(item.chatId, {
            chatId: item.chatId, name: typeof item.name === "string" && item.name ? item.name : item.chatId,
          });
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
      if (!requested) void load();
    }

    function close() {
      expanded = false;
      activeIndex = -1;
      if (loading || timer !== null) requested = false;
      invalidate();
      loading = false;
      render();
    }

    function setSelection(item) {
      close();
      selection = item?.chatId ? { chatId: item.chatId, name: item.name || item.chatId } : null;
      input.value = selection?.name || "";
      query = "";
      items = [];
      nextCursor = null;
      notice = "";
      error = "";
      requested = false;
      render();
    }

    function choose(item) {
      if (disabled || !expanded) return;
      setSelection(item);
      onChange({ ...selection });
    }

    function search() {
      if (disabled) return;
      invalidate();
      if (selection) {
        selection = null;
        onChange(null);
      }
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

    input.addEventListener("focusin", () => { if (!restoringFocus) open(); });
    input.addEventListener("click", open);
    input.addEventListener("input", search);
    input.addEventListener("compositionstart", () => { composing = true; });
    input.addEventListener("compositionend", () => { composing = false; search(); });
    input.addEventListener("keydown", (event) => {
      if (disabled || composing || event.isComposing) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        open();
        if (items.length) {
          activeIndex = event.key === "ArrowDown" ? (activeIndex + 1) % items.length
            : (activeIndex < 0 ? items.length - 1 : (activeIndex - 1 + items.length) % items.length);
          render();
        }
      } else if (event.key === "Enter" && expanded) {
        event.preventDefault();
        if (activeIndex >= 0 && items[activeIndex]) choose(items[activeIndex]);
      }
    });
    wrapper.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !expanded || composing || event.isComposing) return;
      event.preventDefault();
      event.stopPropagation?.();
      close();
      // Retry and paging buttons are keyboard-reachable; do not leave focus hidden.
      restoringFocus = true;
      input.focus();
      restoringFocus = false;
    });
    wrapper.addEventListener("focusout", (event) => {
      if (!wrapper.contains(event.relatedTarget)) close();
    });
    clear.addEventListener("click", () => {
      if (disabled) return;
      setSelection(null);
      onChange(null);
      input.focus();
      open();
    });
    retry.addEventListener("click", () => { if (!loading) void load(failedCursor); });
    more.addEventListener("click", () => { if (!loading && nextCursor) void load(nextCursor); });
    render();

    return {
      setSelection,
      getSelection: () => selection ? { ...selection } : null,
      setDisabled(value) { disabled = Boolean(value); if (disabled) close(); else render(); },
      close,
      reset: () => setSelection(null),
      focus() { if (!disabled) { input.focus(); open(); } },
    };
  };
})();
