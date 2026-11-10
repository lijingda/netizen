"use strict";

// Presentation only: confirmation never acquires or retries an action grant.
window.NetizenConfirm = (() => {
  let active = null;
  const dialog = document.createElement("dialog");
  dialog.id = "admin-confirm";
  dialog.className = "project-delete-dialog admin-confirm";
  dialog.setAttribute("aria-labelledby", "admin-confirm-title");
  dialog.setAttribute("aria-describedby", "admin-confirm-body");
  const head = document.createElement("div"); head.className = "project-delete-head";
  const title = document.createElement("h3"); title.id = "admin-confirm-title";
  const close = document.createElement("button"); close.type = "button"; close.className = "drawer-close";
  close.textContent = "×"; close.setAttribute("aria-label", "取消并关闭");
  head.append(title, close);
  const content = document.createElement("div"); content.className = "project-delete-body";
  const body = document.createElement("p"); body.id = "admin-confirm-body";
  const details = document.createElement("ul"); details.className = "confirm-details";
  content.append(body, details);
  const footer = document.createElement("div"); footer.className = "project-delete-footer";
  const cancel = document.createElement("button"); cancel.id = "admin-confirm-cancel"; cancel.type = "button";
  cancel.className = "drawer-cancel"; cancel.textContent = "取消";
  const confirm = document.createElement("button"); confirm.id = "admin-confirm-submit"; confirm.type = "button";
  footer.append(cancel, confirm);
  dialog.append(head, content, footer); document.body.append(dialog);
  function finish(value) {
    if (!active) return;
    const request = active; active = null;
    dialog.close();
    if (request.trigger) {
      request.trigger.disabled = value && request.trigger.actionPending ? true : request.disabled;
      if (document.body.contains(request.trigger)) request.trigger.focus({ preventScroll: true });
    }
    request.resolve(value);
  }
  for (const button of [cancel, close]) button.addEventListener("click", () => finish(false));
  confirm.addEventListener("click", () => finish(true));
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) finish(false);
  });
  return {
    ask(options) {
      // Concurrent callers are cancelled, never joined to another action's consent.
      if (active || document.querySelector("#project-delete-dialog")?.open) return Promise.resolve(false);
      window.NetizenMenu?.close();
      title.textContent = options.title || "确认操作";
      body.textContent = options.body || "";
      details.replaceChildren();
      for (const value of options.details || []) { const item = document.createElement("li"); item.textContent = value; details.append(item); }
      details.hidden = !details.children.length;
      confirm.textContent = options.confirmLabel || "确认";
      confirm.className = options.danger ? "action primary danger" : "action primary";
      const trigger = options.trigger || document.activeElement;
      return new Promise((resolve) => {
        active = { resolve, trigger, disabled: trigger?.disabled };
        if (trigger) trigger.disabled = true;
        dialog.showModal(); cancel.focus();
      });
    },
    cancel: () => finish(false),
    busy: () => Boolean(active),
  };
})();

window.NetizenMenu = (() => {
  let active = null;
  let sequence = 0;
  function close(restoreFocus = false) {
    if (!active) return;
    const { menu, trigger } = active; active = null;
    menu.hidden = true; trigger.setAttribute("aria-expanded", "false");
    // Keep items with their row while closed; rebuilding a list releases them.
    trigger.parentElement?.append(menu);
    if (restoreFocus && document.body.contains(trigger)) trigger.focus();
  }
  function create(items, label = "更多操作") {
    const wrap = document.createElement("span"); wrap.className = "row-menu";
    const trigger = document.createElement("button"); trigger.type = "button";
    trigger.className = "action compact"; trigger.textContent = "更多";
    trigger.setAttribute("aria-haspopup", "menu"); trigger.setAttribute("aria-expanded", "false"); trigger.setAttribute("aria-label", label);
    const menu = document.createElement("div"); menu.className = "row-menu-popover"; menu.hidden = true;
    menu.id = `row-menu-${++sequence}`; menu.setAttribute("role", "menu"); menu.setAttribute("aria-label", label);
    trigger.setAttribute("aria-controls", menu.id);
    const danger = items.filter((item) => item.className.split(/\s+/).includes("danger"));
    const ordinary = items.filter((item) => !danger.includes(item));
    for (const [index, item] of [...ordinary, ...danger].entries()) {
      if (index === ordinary.length && index > 0 && danger.length) {
        const line = document.createElement("div"); line.setAttribute("role", "separator"); menu.append(line);
      }
      item.setAttribute("role", "menuitem"); item.setAttribute("tabindex", "-1");
      item.menuTrigger = trigger;
      menu.append(item);
    }
    const enabled = () => [...menu.querySelectorAll("button")].filter((item) => !item.disabled);
    function open(last = false) {
      close();
      // A menu inside a dialog must stay in that dialog's top layer.
      let host = trigger.parentElement;
      while (host && host.tagName.toLowerCase() !== "dialog") host = host.parentElement;
      (host || document.body).append(menu);
      menu.hidden = false; active = { menu, trigger }; trigger.setAttribute("aria-expanded", "true");
      const rect = trigger.getBoundingClientRect();
      menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8))}px`;
      menu.style.top = `${Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 8))}px`;
      (last ? enabled().at(-1) : enabled()[0])?.focus();
    }
    trigger.addEventListener("click", () => active?.trigger === trigger ? close(true) : open());
    trigger.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
      event.preventDefault(); open(event.key === "ArrowUp");
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(true); return; }
      const choices = enabled(); const index = choices.indexOf(document.activeElement);
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? choices.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length;
        choices[next]?.focus();
      } else if (["Enter", " "].includes(event.key)) { event.preventDefault(); document.activeElement?.click(); }
      else if (event.key === "Tab") close();
    });
    // Capture closes before the handler opens a drawer and establishes its focus.
    menu.addEventListener("click", (event) => { if (event.target.getAttribute("role") === "menuitem") close(true); }, true);
    wrap.append(trigger, menu); return wrap;
  }
  document.addEventListener("click", (event) => { if (active && !active.menu.contains(event.target) && !active.trigger.contains(event.target)) close(); });
  window.addEventListener("resize", () => close());
  window.addEventListener("scroll", (event) => { if (active && !active.menu.contains(event.target)) close(); }, true);
  return { create, close,
    compact(container, max = 3) {
      const items = [...container.children];
      if (items.length > max) { const extra = items.slice(max); for (const item of extra) item.remove(); container.append(create(extra)); }
    } };
})();

window.NetizenList = (() => {
  function labelRows(body) {
    if (!body) return;
    const headers = [...body.parentElement.querySelector("thead").querySelectorAll("th")];
    for (const row of body.children) {
      if (row.children.length !== headers.length) continue;
      [...row.children].forEach((cell, index) => {
        const header = headers[index];
        cell.setAttribute("data-label", header.textContent.replace(/[↕↑↓]/g, "").trim());
        for (const role of ["title", "status"]) {
          if (header.getAttribute(`data-card-${role}`) !== null) cell.setAttribute(`data-card-${role}`, "");
        }
        if (!cell.querySelector(".card-value")) {
          const value = document.createElement("div"); value.className = "card-value";
          const contents = [...cell.childNodes]; cell.replaceChildren();
          value.append(...contents); cell.append(value);
        }
      });
    }
  }
  function freshness(id, at = Date.now()) {
    const node = document.querySelector(`#${id}-refreshed-at`);
    if (!node) return;
    node.hidden = at == null;
    if (at == null) return;
    const date = new Date(at); node.dateTime = date.toISOString();
    node.textContent = `更新于 ${date.toLocaleTimeString("zh-CN", { hour12: false })}`;
    node.title = `${date.toLocaleString("zh-CN", { hour12: false })} 读取；列表不会自动刷新`;
  }
  function error(id, message, retry) {
    const node = document.querySelector(`#${id}-error`);
    if (!node) return;
    node.replaceChildren(); node.hidden = !message;
    if (!message) return;
    for (const row of document.querySelector(`#${id}-body`)?.querySelectorAll(".list-empty") || []) row.hidden = true;
    const text = document.createElement("span"); text.textContent = message;
    const button = document.createElement("button"); button.type = "button"; button.className = "action compact"; button.textContent = "重试";
    button.addEventListener("click", async () => { if (button.disabled) return; button.disabled = true; try { await retry(); } finally { button.disabled = false; } });
    node.append(text, button);
  }
  function empty(body, columns, message, label, action) {
    const row = document.createElement("tr"); row.className = "list-empty";
    const td = document.createElement("td"); td.colSpan = columns;
    const text = document.createElement("p"); text.textContent = message; td.append(text);
    if (action) { const button = document.createElement("button"); button.type = "button"; button.className = "action primary"; button.textContent = label; button.addEventListener("click", action); td.append(button); }
    row.append(td); body.append(row);
  }
  return { freshness, error, empty, labelRows,
    success(id) { labelRows(document.querySelector(`#${id}-body`)); error(id, ""); freshness(id); } };
})();
