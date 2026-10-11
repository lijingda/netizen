"use strict";

(() => {
  const node = (id) => document.querySelector(`#login-${id}`);
  const nonce = node("nonce").value;
  let current = null;
  let busy = false;
  let reading = false;
  let generation = 0;
  let timer = null;
  let started = false;

  function message(text, error = false) {
    node("status").textContent = text;
    node("status").classList.toggle("error", error);
  }

  function buttons() {
    const pending = ["starting", "pending"].includes(current?.state);
    node("start").disabled = busy || !nonce || started || pending || current?.state === "authenticated";
    node("start").hidden = started || pending;
    node("cancel").hidden = !pending;
    node("cancel").disabled = busy;
    node("check").hidden = !pending;
    node("check").disabled = busy || reading;
  }

  function render(data) {
    current = data;
    if (data.state !== "idle") started = true;
    clearTimeout(timer);
    const pending = data.state === "pending";
    node("authorization").hidden = !pending;
    node("retry").hidden = !started || ["starting", "pending", "authenticated"].includes(data.state);
    const labels = {
      idle: started ? "此次登录已结束，可以重新开始。" : "点击按钮，为当前浏览器发起飞书登录。",
      starting: "正在申请飞书授权…",
      pending: "请在飞书确认本次登录，完成后返回此页。",
      denied: "未获准登录，请使用已获授权的管理员账号重新登录。",
      expired: "此次登录已过期，请重新开始。",
      failed: "此次登录未完成，请重新开始。",
      cancelled: "此次登录已取消。",
      authenticated: "登录成功，正在打开管理页…",
    };
    message(data.message || labels[data.state] || "此次登录未完成，请重新开始。",
      ["denied", "expired", "failed", "error"].includes(data.state));
    if (pending) {
      let uri;
      try { uri = new URL(data.verificationUri); } catch { uri = null; }
      const valid = uri?.protocol === "https:" && !uri.username && !uri.password;
      node("link").hidden = !valid;
      if (valid) node("link").href = uri.href;
      else node("link").removeAttribute("href");
      node("code").textContent = data.userCode || "—";
      node("qr").hidden = data.qrUrl !== "/api/v1/login/qr";
      if (!node("qr").hidden && !node("qr").getAttribute("src")) {
        node("qr").setAttribute("src", data.qrUrl);
      }
      node("deadline").textContent = Number.isFinite(data.expiresAt)
        ? `本次授权有效至 ${new Date(data.expiresAt * 1000).toLocaleTimeString()}。` : "";
    }
    if (data.state === "authenticated") {
      window.location.assign("/");
    } else if (["starting", "pending"].includes(data.state)) {
      const interval = Number.isFinite(data.interval) ? data.interval : 2;
      timer = setTimeout(check, Math.min(30, Math.max(1, interval)) * 1000);
    }
    buttons();
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/v1/login/${path}`, {
        cache: "no-store", credentials: "same-origin", ...options, signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || "无法完成登录请求，请重新开始。");
      return data;
    } finally { clearTimeout(timeout); }
  }

  async function check() {
    if (reading || busy) return;
    clearTimeout(timer);
    const version = generation;
    reading = true;
    buttons();
    try {
      const data = await request("state");
      if (version === generation) render(data);
    } catch (error) {
      if (version !== generation) return;
      message(error.message, true);
      node("retry").hidden = false;
      // Reading failed: stop polling and let the user explicitly check or restart.
    } finally { reading = false; buttons(); }
  }

  async function change(action) {
    if (busy || (action === "start" && (started || !nonce))) return;
    busy = true;
    const version = ++generation;
    clearTimeout(timer);
    if (action === "start") started = true;
    buttons();
    message(action === "start" ? "正在申请飞书授权…" : "正在取消此次登录…");
    try {
      const data = await request(action, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nonce }),
      });
      if (version === generation) render(data);
    } catch (error) {
      if (version !== generation) return;
      message(error.message, true);
      node("retry").hidden = false;
    } finally { busy = false; buttons(); }
  }

  node("start").addEventListener("click", () => { void change("start"); });
  node("cancel").addEventListener("click", () => { void change("cancel"); });
  node("check").addEventListener("click", () => { void check(); });
  node("qr").addEventListener("error", () => { node("qr").hidden = true; });
  window.addEventListener("pageshow", () => {
    if (["starting", "pending"].includes(current?.state)) void check();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && ["starting", "pending"].includes(current?.state)) void check();
  });
  if (nonce) void check();
  else { message("登录暂不可用，请稍后重新打开登录页。", true); buttons(); }
})();
