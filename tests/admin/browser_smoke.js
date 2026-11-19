// Driven by test_browser_smoke.py, which owns the deadline, process group and profile.
"use strict";

const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { readFileSync } = require("node:fs");
const { setTimeout: poll } = require("node:timers/promises");

const config = JSON.parse(readFileSync(0, "utf8"));
const result = { visits: [], interactions: [] };
const stepTimeout = 8000;
const deadline = performance.now() + 115000; // Leave time for the Python supervisor's cleanup.
let step = "launch Chrome";

// Prefer the result of the page's initial API read over the static HTML shell.
const landmarks = {
  "/login": "#login-start:enabled",
  "/": "#projects-body [data-project-alias]",
  "/projects": "#projects-body [data-project-alias]",
  "/sessions/ordinary": "#sessions-ordinary #sessions-body [data-binding-id]",
  "/sessions/temporary": "#sessions-temporary #sides-body td",
  "/schedules": "#schedules-body .schedule-target .chat-link",
  "/defaults": "#defaults-refreshed-at",
  "/maintenance": "#updates-refreshed-at",
  "/system": "#admin-users li",
  "/admins": "#admin-users li",
  "/attachments": "#attachments-refreshed-at",
  "/worktrees": "#worktrees-refreshed-at",
  "/group-access": "#group-access-refreshed-at",
  "/session-tools": "#session-tools-refreshed-at",
  "/progress-card": "#progress-card-refreshed-at",
  "/question-notifications": "#question-notifications-refreshed-at",
};

function devtoolsEndpoint(browser) {
  return new Promise((resolve, reject) => {
    let tail = "";
    const timeout = setTimeout(() => reject(new Error("Chrome did not publish its DevTools URL: " + tail)), stepTimeout);
    browser.once("error", error => { clearTimeout(timeout); reject(error); });
    browser.once("exit", (code, signal) => {
      clearTimeout(timeout);
      reject(new Error(`Chrome exited (${code ?? signal}): ${tail}`));
    });
    browser.stderr.setEncoding("utf8").on("data", chunk => {
      tail = (tail + chunk).slice(-3000);
      const match = tail.match(/DevTools listening on (ws:\/\/127\.0\.0\.1:\d+)\/devtools\/browser\/[^\s]+/);
      if (match) {
        clearTimeout(timeout);
        resolve(match[1].replace("ws:", "http:"));
      }
    });
  });
}

async function attach(endpoint, onEvent) {
  const signal = AbortSignal.timeout(stepTimeout);
  let target;
  while (!target) {
    const targets = await fetch(endpoint + "/json/list", { signal });
    target = (await targets.json()).find(item => item.type === "page");
    if (!target) await poll(40, undefined, { signal });
  }
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await once(socket, "open", { signal: AbortSignal.timeout(stepTimeout) });
  const pending = new Map();
  let sequence = 0;
  socket.addEventListener("message", ({ data }) => {
    const { id, result, error, method, params } = JSON.parse(data);
    if (!id) return onEvent(method, params);
    const reply = pending.get(id);
    if (reply) {
      pending.delete(id);
      clearTimeout(reply.timeout);
      if (error) reply.reject(new Error(error.message));
      else reply.resolve(result);
    }
  });
  socket.addEventListener("close", () => {
    for (const reply of pending.values()) {
      clearTimeout(reply.timeout);
      reply.reject(new Error("Chrome closed the DevTools connection"));
    }
    pending.clear();
  });
  return {
    close: () => socket.close(),
    call(method, params = {}) {
      return new Promise((resolve, reject) => {
        const id = ++sequence;
        if (performance.now() >= deadline) throw new Error("Overall smoke deadline exceeded");
        const timeout = setTimeout(() => {
          pending.delete(id);
          reject(new Error(method + " timed out"));
        }, Math.min(stepTimeout, deadline - performance.now()));
        pending.set(id, { timeout, resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
  };
}

async function run() {
  const errors = [];
  const inflight = new Map();
  const documents = new Map();
  let lastActivity = 0;
  function onEvent(method, p) {
    if (method === "Network.requestWillBeSent") {
      const url = new URL(p.request.url);
      if (url.origin === config.origin) {
        inflight.set(p.requestId, url.pathname);
        lastActivity = performance.now();
      } else if (["http:", "https:"].includes(url.protocol)) {
        errors.push("Unexpected external request " + url.origin + url.pathname);
      }
    } else if (method === "Network.responseReceived" && inflight.has(p.requestId)) {
      if (p.type === "Document") documents.set(p.loaderId, p.response);
      if (p.response.status >= 400) errors.push(`HTTP ${p.response.status} ${inflight.get(p.requestId)}`);
    } else if (method === "Network.loadingFinished" || method === "Network.loadingFailed") {
      const path = inflight.get(p.requestId);
      if (path && method === "Network.loadingFailed") errors.push(`${p.errorText || "Request failed"} ${path}`);
      if (inflight.delete(p.requestId)) lastActivity = performance.now();
    } else if (method === "Runtime.exceptionThrown") {
      errors.push(p.exceptionDetails.exception?.description || p.exceptionDetails.text);
    } else if (method === "Runtime.consoleAPICalled" && p.type === "error") {
      errors.push("console.error " + p.args.map(a => a.value ?? a.description).join(" "));
    } else if (method === "Log.entryAdded" && (p.entry.level === "error" || /content security policy/i.test(p.entry.text))) {
      errors.push(p.entry.text);
    } else if (method === "Runtime.bindingCalled" && p.name === "reportSmokeCsp") {
      errors.push("securitypolicyviolation " + p.payload);
    }
  }
  function checkErrors() {
    if (errors.length) throw new Error(errors.slice(0, 5).join("\n"));
  }
  async function until(label, condition) {
    const end = Math.min(performance.now() + stepTimeout, deadline);
    do {
      checkErrors();
      if (await condition()) return;
      await poll(40);
    } while (performance.now() < end);
    throw new Error("Timed out waiting for " + label + "; in flight: " + [...inflight.values()].join(", "));
  }

  const browser = spawn(config.browser, [
    "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--disable-gpu", "--disable-component-update",
    "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
    "--user-data-dir=" + config.profile, "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  let cdp;
  try {
    cdp = await attach(await devtoolsEndpoint(browser), onEvent);
    for (const domain of ["Page", "Runtime", "Network", "Log"]) await cdp.call(domain + ".enable");
    await cdp.call("Runtime.addBinding", { name: "reportSmokeCsp" });
    await cdp.call("Page.addScriptToEvaluateOnNewDocument", {
      source: "document.addEventListener('securitypolicyviolation', e => reportSmokeCsp(e.effectiveDirective + ' ' + e.blockedURI))",
    });

    async function evaluate(expression) {
      const reply = await cdp.call("Runtime.evaluate", { expression, returnByValue: true });
      if (reply.exceptionDetails) throw new Error(reply.exceptionDetails.exception?.description || reply.exceptionDetails.text);
      return reply.result.value;
    }
    const element = selector => `document.querySelector(${JSON.stringify(selector)})`;
    const visible = selector => `${element(selector)}?.checkVisibility()`;
    async function click(selector, label) {
      let target = element(selector);
      if (label) target = `[...document.querySelectorAll(${JSON.stringify(selector)})].find(el => el.textContent === ${JSON.stringify(label)})`;
      await until("clickable " + (label || selector), () => evaluate(
        `(() => { const el = ${target}; if (!el?.checkVisibility() || el.disabled) return false; el.click(); return true; })()`));
    }
    async function settled(phoneWidth) {
      const start = performance.now();
      await until("network quiet", () => !inflight.size && performance.now() - Math.max(start, lastActivity) >= 160);
      if (phoneWidth) {
        const [width, viewport] = await evaluate("[document.documentElement.scrollWidth, innerWidth]");
        // Mobile Chrome can expand innerWidth to include overflowing content.
        if (width > Math.min(phoneWidth, viewport) + 1) {
          throw new Error(`Horizontal overflow: scrollWidth=${width}, innerWidth=${viewport}, phone=${phoneWidth}`);
        }
      }
      checkErrors();
    }
    for (const [viewport, width, height, mobile] of [["desktop", 1280, 800, false], ["phone", 390, 844, true]]) {
      const phoneWidth = mobile ? width : null;
      await cdp.call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
      await cdp.call("Emulation.setTouchEmulationEnabled", { enabled: mobile });
      await cdp.call("Network.clearBrowserCookies");
      for (const path of ["/login", ...config.paths]) {
        step = `${viewport} ${path}`;
        if (!landmarks[path]) throw new Error("Missing ready landmark");
        const url = config.origin + path;
        const nav = await cdp.call("Page.navigate", { url });
        if (nav.errorText) throw new Error(nav.errorText);
        await until("document response", () => documents.has(nav.loaderId));
        const response = documents.get(nav.loaderId);
        documents.delete(nav.loaderId);
        if (response.url !== url || response.status !== 200) throw new Error(`Document returned ${response.status} at ${response.url}`);
        await until(landmarks[path], () => evaluate(`document.readyState === "complete" && ${visible(landmarks[path])}`));
        await settled(phoneWidth);
        result.visits.push(step);
        if (path === "/login") {
          const cookie = await cdp.call("Network.setCookie", { ...config.cookie, url: config.origin, httpOnly: true });
          if (!cookie.success) throw new Error("Could not install fixture session cookie");
        } else if (path === "/projects") {
          step += " · Project Markdown preview";
          await click("#projects-body button", "查看文件");
          await click('#file-browser-entries button[data-path="readme.md"]');
          await click("#file-browser-preview");
          await until("decoded Markdown image", () => evaluate(
            `${visible("#file-browser-markdown img")} && ${element("#file-browser-markdown img")}.naturalWidth === 1`));
          await settled(phoneWidth);
          await click("#file-browser-close");
          await until("closed file browser", () => evaluate('!document.querySelector("#file-browser-drawer").open'));
          await settled(phoneWidth);
          result.interactions.push(step);
        } else if (path === "/sessions/ordinary") {
          step += " · conversation";
          await click('#sessions-body [aria-haspopup="menu"]');
          await click('[role="menuitem"]', "查看对话记录");
          await until("rendered Markdown and decoded answer blob", () => evaluate(
            `${element(".conversation-answer h1")}?.textContent === "Smoke answer" &&
             ${element('.conversation-answer img[src^="blob:"]')}?.naturalWidth === 1`));
          await settled(phoneWidth);
          result.interactions.push(step);
          step += " · open source link";
          await click('.conversation-answer [data-answer-file-name="hello.py"]');
          await until("source file in editor", () => evaluate(
            `${visible("#file-browser-editor")} && ${element("#file-browser-editor .ace_text-layer")}?.textContent.includes("hello smoke")`));
          await settled(phoneWidth);
          result.interactions.push(step);
        }
      }
    }
    checkErrors();
  } finally {
    if (cdp && browser.exitCode === null && browser.signalCode === null) {
      const exited = once(browser, "exit", { signal: AbortSignal.timeout(2000) }).catch(() => {});
      try { await cdp.call("Browser.close"); } catch { /* Chrome may close the socket first. */ }
      cdp.close();
      await exited;
    }
    browser.kill("SIGKILL");
  }
}

run().catch(error => {
  result.error = step + ": " + error.message;
  process.exitCode = 1;
}).finally(() => console.log(JSON.stringify(result)));
