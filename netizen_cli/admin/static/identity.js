"use strict";

window.NetizenIdentity = (() => {
  const sdkUrl = "https://lf3-cdn-tos.bytegoofy.com/obj/goofy/locl/lark/external_js_sdk/h5-js-sdk-1.2.21.js";
  const node = (id) => document.querySelector(`#${id}`);
  let users = null;
  let profileRequest = null;
  let sdkRequest = null;
  let configTail = Promise.resolve();
  let configStalled = false;
  let selector = null;
  let generation = 0;
  let selected = null;
  let pending = false;
  let expired = false;
  let selectionSnapshot = null;
  const reloadMessage = "飞书选人组件验证超时，请重新加载页面后再试。";

  function avatarUrl(raw) {
    try {
      const url = new URL(raw);
      return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  }

  function person(container, user) {
    container.replaceChildren();
    const name = typeof user.name === "string" && user.name ? user.name : user.openId;
    const fallback = document.createElement("span");
    fallback.className = "person-placeholder";
    fallback.textContent = name?.slice(0, 1) || "人";
    fallback.setAttribute("aria-hidden", "true");
    container.append(fallback);
    const url = avatarUrl(user.avatarUrl);
    if (url) {
      const avatar = document.createElement("img");
      avatar.className = "person-avatar";
      avatar.alt = "";
      avatar.referrerPolicy = "no-referrer";
      avatar.src = url;
      fallback.hidden = true;
      avatar.addEventListener("error", () => { avatar.hidden = true; fallback.hidden = false; });
      container.append(avatar);
    }
    const text = document.createElement("span");
    text.className = "person-name";
    text.textContent = name || "未提供姓名";
    container.append(text);
  }

  function loadProfile() {
    // Only the first full-page initialization requests the current user's profile.
    if (!profileRequest) profileRequest = api("/api/v1/identity").then((value) => {
      if (!expired) person(node("current-user"), value);
    });
    return profileRequest;
  }

  function renderUsers() {
    const list = node("admin-users");
    list.replaceChildren();
    for (const user of users?.users || []) {
      const row = document.createElement("li");
      const identity = document.createElement("div");
      identity.className = "person";
      person(identity, user);
      row.append(identity);
      const remove = actionButton("删除", () => { void removeUser(user); }, true);
      remove.disabled = expired || pending || users.users.length <= 1 || !user.removeAction;
      if (users.users.length <= 1) remove.title = "至少保留一位管理员";
      row.append(remove);
      list.append(row);
    }
    node("admin-user-add").disabled = expired || pending || !users?.addAction;
  }

  async function loadUsers() {
    const value = await api("/api/v1/admin-users");
    if (expired) return;
    users = value;
    renderUsers();
  }

  async function post(path, envelope, extra = {}) {
    return api(`/api/v1/admin-users/${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionPayload(envelope, extra)),
    });
  }

  function clearSelector() {
    generation += 1;
    selected = null;
    try { selector?.unmount?.(); } catch { /* Closing the local picker remains possible. */ }
    selector = null;
    node("admin-selector").replaceChildren();
    node("admin-user-selection").hidden = true;
    node("admin-user-confirm").disabled = true;
  }

  function closePanel() {
    clearSelector();
    node("admin-user-panel").hidden = true;
    node("admin-user-add").setAttribute("aria-expanded", "false");
    selectionSnapshot = null;
  }

  function current(version) {
    return !expired && version === generation && !node("admin-user-panel").hidden;
  }

  function sdk() {
    if (!sdkRequest) sdkRequest = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = sdkUrl;
      script.async = true;
      let done = false;
      const finish = (error) => {
        if (done) return;
        done = true;
        clearTimeout(timeout);
        script.onload = null;
        script.onerror = null;
        if (error) reject(new Error(error));
        else resolve(window.webComponent);
      };
      const timeout = setTimeout(() => finish("飞书选人组件加载超时。"), 15000);
      script.onload = () => finish(typeof window.webComponent?.config === "function"
        && typeof window.webComponent?.render === "function" ? null : "飞书选人组件不可用。");
      script.onerror = () => finish("无法加载飞书选人组件。");
      document.head.append(script);
    }).catch((error) => { sdkRequest = null; throw error; });
    return sdkRequest;
  }

  function selectorError(version, message) {
    if (!current(version)) return;
    clearSelector();
    node("admin-selector-status").textContent = message;
    node("admin-selector-status").classList.toggle("error", true);
    node("admin-selector-retry").hidden = configStalled;
    node("admin-selector-reload").hidden = !configStalled;
  }

  function selectPerson(data, version) {
    if (!current(version) || pending) return;
    const openId = data?.id;
    const entityId = data?.entity?.id;
    if (data?.type !== "user" || typeof openId !== "string" || !openId
        || openId.length > 256 || (entityId != null && entityId !== openId)) {
      selectorError(version, "选人结果无法确认，请重新选择。");
      return;
    }
    selected = {
      openId, entityId: entityId || null, type: "user",
      name: typeof data.entity?.name === "string" ? data.entity.name.slice(0, 256) : "",
      avatarUrl: avatarUrl(data.entity?.avatarUrl || data.entity?.avatar) || "",
    };
    person(node("admin-user-selection"), selected);
    node("admin-user-selection").hidden = false;
    node("admin-user-confirm").disabled = false;
    node("admin-selector-status").textContent = "确认添加后，此人将拥有本实例的管理权限。";
  }

  async function loadSelector() {
    clearSelector();
    const version = generation;
    if (configStalled) { selectorError(version, reloadMessage); return; }
    node("admin-selector-status").classList.toggle("error", false);
    node("admin-selector-status").textContent = "正在加载飞书选人组件…";
    node("admin-selector-retry").hidden = true;
    node("admin-selector-reload").hidden = true;
    try {
      // Reload single-use grants explicitly when the picker is opened or retried.
      await loadUsers();
      if (!current(version)) return;
      selectionSnapshot = users;
      const config = await post("selector-config", selectionSnapshot.selectorAction);
      if (!current(version)) return;
      const component = await sdk();
      if (!current(version)) return;
      const fail = () => selectorError(version, "飞书选人组件未能完成验证，请重试。");
      component.onError?.(fail);
      component.onAuthError?.(fail);
      const { openId, appId, signature, timestamp, nonceStr, url, jsApiList, locale } = config;
      configTail = configTail.catch(() => {}).then(async () => {
        if (!configStalled && current(version)) await component.config({ openId, appId, signature, timestamp, nonceStr, url, jsApiList, locale });
      });
      let timeout;
      try {
        await Promise.race([configTail, new Promise((_resolve, reject) => {
          timeout = setTimeout(() => {
            // SDK configuration changes global auth and cannot be cancelled.
            // A stalled attempt requires a fresh page, even if it resolves late.
            configStalled = true;
            reject(new Error(reloadMessage));
          }, 15000);
        })]);
      } finally { clearTimeout(timeout); }
      if (!current(version)) return;
      if (configStalled) throw new Error(reloadMessage);
      selector = component.render("Selector", {
        searchEntityTypes: [1], placeholder: "搜索人员姓名",
        triggerWidth: Math.min(360, Math.max(200, node("admin-selector").clientWidth || 280)),
        onSelect: (data) => selectPerson(data, version),
      }, node("admin-selector"));
      if (!selector) throw new Error("飞书选人组件无法显示，请重试。");
      if (!current(version)) { clearSelector(); return; }
      node("admin-selector-status").textContent = "请选择要添加的管理员。";
    } catch (error) { selectorError(version, configStalled ? reloadMessage : error.message); }
  }

  function openPanel() {
    if (expired || pending || !users?.addAction || !node("admin-user-panel").hidden) return;
    node("admin-user-panel").hidden = false;
    node("admin-user-add").setAttribute("aria-expanded", "true");
    void loadSelector();
  }

  async function addUser() {
    if (expired || pending || !selected || !selectionSnapshot?.addAction) return;
    const target = selected;
    const snapshot = selectionSnapshot;
    pending = true;
    node("admin-user-confirm").disabled = true;
    renderUsers();
    try {
      await post("add", snapshot.addAction, { revision: snapshot.revision, ...target });
      closePanel();
      await loadUsers();
      setStatus("已添加管理员。");
    } catch (error) {
      if (!expired) {
        closePanel();
        // Refresh facts and grants; never replay a possibly accepted mutation.
        try { await loadUsers(); } catch (_refreshError) { /* Preserve the original operation result. */ }
        setStatus(error.message, true);
      }
    } finally { pending = false; renderUsers(); }
  }

  async function removeUser(user) {
    if (expired || pending || !user.removeAction || users.users.length <= 1) return;
    const self = user.openId === users.currentOpenId;
    if (!window.confirm(`确认删除管理员“${user.name || user.openId}”？${self ? "\n确认后你将退出 Admin。" : "\n该用户将立即失去此实例的管理权限。"}`)) return;
    const revision = users.revision;
    pending = true;
    renderUsers();
    try {
      const result = await post("remove", user.removeAction, { revision, openId: user.openId });
      if (self || result?.loggedOut) { expireAdminSession(); return; }
      await loadUsers();
      setStatus("已删除管理员。");
    } catch (error) {
      if (!expired) {
        try { await loadUsers(); } catch (_refreshError) { /* Do not retry the deletion. */ }
        setStatus(error.message, true);
      }
    } finally { pending = false; renderUsers(); }
  }

  node("admin-user-add").addEventListener("click", openPanel);
  node("admin-user-close").addEventListener("click", closePanel);
  node("admin-user-confirm").addEventListener("click", () => { void addUser(); });
  node("admin-selector-retry").addEventListener("click", () => { void loadSelector(); });
  node("admin-selector-reload").addEventListener("click", () => { window.location.reload(); });
  return {
    loadProfile, loadUsers, closePanel,
    expire() { expired = true; closePanel(); renderUsers(); },
  };
})();
