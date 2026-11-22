// Driven by test_browser_smoke.py, which owns the deadline, process group and profile.
"use strict";

const { spawn } = require("node:child_process");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { setTimeout: poll } = require("node:timers/promises");

const config = JSON.parse(readFileSync(0, "utf8"));
const result = { browserPath: config.browser, browserVersion: config.browserVersion, visits: [], interactions: [] };
const launchTimeout = 30000;
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

// Chrome reads NUL-terminated CDP messages from fd 3 and writes replies and
// events to fd 4. A flattened session routes page commands over the same pipe.
function devtoolsPipe(browser, onEvent) {
  const [, , , input, output] = browser.stdio;
  const pending = new Map();
  let sequence = 0;
  let partial = "";
  let failure;

  function disconnect(error) {
    failure ??= error;
    for (const reply of pending.values()) {
      clearTimeout(reply.timeout);
      reply.reject(failure);
    }
    pending.clear();
  }
  browser.once("error", disconnect);
  browser.once("exit", () => disconnect(new Error("Chrome exited")));
  input.on("error", disconnect);
  output.on("error", disconnect);
  output.once("end", () => disconnect(new Error("Chrome closed the DevTools pipe")));
  output.setEncoding("utf8").on("data", chunk => {
    const messages = (partial + chunk).split("\0");
    partial = messages.pop();
    for (const message of messages) {
      let packet;
      try {
        packet = JSON.parse(message);
      } catch (error) {
        disconnect(new Error("Invalid DevTools pipe response: " + error.message));
        return;
      }
      const { id, result, error, method, params, sessionId } = packet;
      if (!id) {
        onEvent(method, params, sessionId);
        continue;
      }
      const reply = pending.get(id);
      if (reply) {
        pending.delete(id);
        clearTimeout(reply.timeout);
        if (error) reply.reject(new Error(reply.method + ": " + error.message));
        else reply.resolve(result);
      }
    }
  });

  return function call(method, params, sessionId, end = Math.min(deadline, performance.now() + stepTimeout)) {
    return new Promise((resolve, reject) => {
      if (failure) throw failure;
      const id = ++sequence;
      const remaining = end - performance.now();
      if (remaining <= 0) throw new Error(method + " timed out");
      const timeout = setTimeout(() => {
        pending.delete(id);
        reject(new Error(method + " timed out"));
      }, remaining);
      pending.set(id, { method, timeout, resolve, reject });
      input.write(JSON.stringify({ id, method, params, sessionId }) + "\0");
    });
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
    "--remote-debugging-pipe",
    "--user-data-dir=" + config.profile, "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"] });
  let stderrTail = "";
  browser.stderr.setEncoding("utf8").on("data", chunk => { stderrTail = (stderrTail + chunk).slice(-3000); });
  const closed = new Promise(resolve => browser.once("close", resolve));
  let sessionId;
  const call = devtoolsPipe(browser, (method, params, session) => {
    if (sessionId && session === sessionId) onEvent(method, params);
  });
  const page = (method, params) => call(method, params, sessionId);
  let launchError;
  try {
    const launchEnd = performance.now() + launchTimeout;
    const launch = (method, params) => call(method, params, sessionId, launchEnd);
    let target;
    while (!target) {
      const { targetInfos } = await launch("Target.getTargets");
      target = targetInfos.find(info => info.type === "page");
      if (!target) await poll(40);
    }
    ({ sessionId } = await launch("Target.attachToTarget", { targetId: target.targetId, flatten: true }));
    for (const domain of ["Page", "Runtime", "Network", "Log"]) await launch(domain + ".enable");
    await launch("Runtime.addBinding", { name: "reportSmokeCsp" });
    await launch("Page.addScriptToEvaluateOnNewDocument", {
      source: "document.addEventListener('securitypolicyviolation', e => reportSmokeCsp(e.effectiveDirective + ' ' + e.blockedURI))",
    });

    async function evaluate(expression) {
      const reply = await page("Runtime.evaluate", { expression, returnByValue: true });
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
      await page("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
      await page("Emulation.setTouchEmulationEnabled", { enabled: mobile });
      await page("Network.clearBrowserCookies");
      for (const path of ["/login", ...config.paths]) {
        step = `${viewport} ${path}`;
        if (!landmarks[path]) throw new Error("Missing ready landmark");
        const url = config.origin + path;
        const nav = await page("Page.navigate", { url });
        if (nav.errorText) throw new Error(nav.errorText);
        await until("document response", () => documents.has(nav.loaderId));
        const response = documents.get(nav.loaderId);
        documents.delete(nav.loaderId);
        if (response.url !== url || response.status !== 200) throw new Error(`Document returned ${response.status} at ${response.url}`);
        await until(landmarks[path], () => evaluate(`document.readyState === "complete" && ${visible(landmarks[path])}`));
        await settled(phoneWidth);
        result.visits.push(step);
        if (path === "/projects") {
          const alias = config.ownerOnly ? "pending" : "test";
          await click(`[data-project-alias="${alias}"] .project-delete-trigger`);
          await until("verified deletion scope", () => evaluate(visible("#project-delete-confirm:enabled")));
          const summary = await evaluate(element("#project-delete-dialog") + ".innerText");
          if (config.ownerOnly) {
            assert.match(summary, /1 个未开始的 Claude Code 会话登记/);
            assert.doesNotMatch(summary, /Codex|Goal|Side|App\/CLI|归档|定时/);
          } else {
            assert.match(summary, /Codex 会话、派生子会话、Codex App\/CLI 历史和本地会话登记将永久删除，无法恢复。/);
            assert.match(summary, /未开始 0，已创建 1/);
          }
          await click("#project-delete-cancel");
          await settled(phoneWidth);
          result.interactions.push(step + " · deletion preview");
        }
        if (path === "/maintenance") {
          await click(".service-restart summary");
          await click(".diagnostics-environment summary");
          const restart = await evaluate(element(".service-restart") + ".innerText");
          const directories = await evaluate(element(".diagnostics-environment") + ".innerText");
          if (config.ownerOnly) {
            assert.match(restart, /中断正在执行的 Claude Code 任务/);
            assert.doesNotMatch(restart, /Codex|Goal|Side/);
            assert.match(directories, /实例目录/);
            assert.doesNotMatch(directories, /Codex 目录/);
          } else {
            assert.match(restart, /暂停 Goal，并结束临时 Side 会话/);
            assert.match(restart, /重启本实例的 Netizen 与 Codex 运行环境/);
            assert.match(directories, /Codex 目录/);
          }
          result.interactions.push(step + " · restart and directories");
        }
        if (path === "/sessions/ordinary") {
          await until("session identity details", () => evaluate('document.querySelector("#sessions-body .session-identities") !== null'));
          const visibility = await evaluate(`(() => {
            const unrelated = document.createElement("div");
            unrelated.className = "section-head";
            unrelated.hidden = true;
            document.body.append(unrelated);
            const displays = [getComputedStyle(unrelated).display, getComputedStyle(document.querySelector("#session-rename-generate")).display];
            unrelated.remove();
            return displays;
          })()`);
          assert.equal(visibility[0], "flex");
          if (config.manualOnly || config.ownerOnly) assert.equal(visibility[1], "none");
          else assert.notEqual(visibility[1], "none");
          if (config.ownerOnly) {
            const details = await evaluate(`(() => {
              const cell = document.querySelector("#sessions-body .runtime-state").cloneNode(false);
              const runtime = {primaryStatus: "idle", primaryStatusResolution: "local"};
              const text = observations => {
                updateRuntimeCell(cell, {...runtime, observations});
                return cell.querySelector(".runtime-observations")?.textContent || "";
              };
              return [text({}), text({model: {label: "模型", availability: "observed", value: "sample"}}), text({})];
            })()`);
            assert.deepEqual(details, ["", "模型：sample", ""]);
          }
          result.interactions.push(step + " · provider fields and computed visibility");
          const identities = await evaluate('[...document.querySelectorAll("#sessions-body .session-identities")].map(el => el.textContent)');
          for (const identity of identities) {
            if (config.ownerOnly) {
              assert.match(identity, /Claude Code 会话 ID/);
              assert.doesNotMatch(identity, /Codex|跨会话工具/);
            } else {
              assert.match(identity, /完整 Codex 会话 ID/);
            }
          }
          await click("#sessions-body button", "配置");
          await until("session configuration loaded", () => evaluate(visible("#session-settings-save:enabled")));
          const settings = await evaluate(element("#session-settings-drawer") + ".innerText");
          if (config.ownerOnly) {
            assert.match(settings, /模型沿用 Claude Code 的原生配置/);
            assert.doesNotMatch(settings, /Codex/);
          } else {
            assert.match(settings, /选择继承 Codex 时，沿用 Codex 的原生配置/);
          }
          await click("#session-settings-cancel");
          result.interactions.push(step + " · session configuration");
        }
        if (path === "/login") {
          const cookie = await page("Network.setCookie", { ...config.cookie, url: config.origin, httpOnly: true });
          if (!cookie.success) throw new Error("Could not install fixture session cookie");
        } else if (path === "/projects" && !config.ownerOnly) {
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
        } else if (config.ownerOnly && path !== "/login") {
          step += " · owner controls";
          const unsupported = ["#nav-temporary", "#nav-schedules", "#nav-progress-card", "#nav-question-notifications", "#nav-session-tools", "#admin-user-add"];
          for (const selector of unsupported) {
            if (await evaluate(visible(selector))) throw Error("Unsupported owner action is visible: " + selector);
          }
          if (path === "/sessions/ordinary") {
            const created = '[data-session-sort="session"][data-sort-by="createdAt"]';
            const updated = '[data-session-sort="session"][data-sort-by="updatedAt"]';
            const verify = () => evaluate(`document.querySelector('${created} .sort-label').textContent === "登记时间"
              && document.querySelector('${updated}').disabled
              && [...document.querySelectorAll("#sessions-body [data-binding-id]")].some(row =>
                row.textContent.includes("原生 Claude 设置") && row.textContent.includes("空闲")
                && /[0-9]/.test(row.querySelector(".session-time")?.textContent))`);
            await until("local registration time, settings and status", verify);
            await click('#sessions-body [aria-haspopup="menu"]');
            await until("Claude row menu contains only available actions", () => evaluate(
              '[...document.querySelectorAll(\'[role="menuitem"]\')].some(el => el.textContent === "设为当前") && ![...document.querySelectorAll(\'[role="menuitem"]\')].some(el => el.textContent.includes("创建定时任务"))'));
            await evaluate('document.dispatchEvent(new KeyboardEvent("keydown", {key:"Escape", bubbles:true}))');
            await click(created);
            await until("local sort after refresh", () => evaluate(`document.querySelector('${created}').parentElement.getAttribute("aria-sort") === "ascending"`));
            await until("local columns after refresh", verify);
          }
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
  } catch (error) {
    if (step !== "launch Chrome") throw error;
    launchError = error;
  } finally {
    if (sessionId && !launchError) {
      try { await call("Browser.close", {}, undefined, performance.now() + 1000); } catch { /* Pipe may close first. */ }
    }
    browser.stdio[3].end();
    // Wait for close (including stderr EOF), even after a handshake failure.
    // A wrapper's descendants can hold the inherited descriptors open after exit.
    const kill = setTimeout(() => browser.kill("SIGKILL"), 1000);
    const drain = setTimeout(() => {
      for (const stream of browser.stdio.slice(2)) stream.destroy();
    }, 2500);
    await closed;
    clearTimeout(kill);
    clearTimeout(drain);
  }
  if (launchError) {
    throw new Error(`${launchError.message} (handshake budget ${launchTimeout / 1000} s, exit code=${browser.exitCode}, signal=${browser.signalCode})\nChrome stderr (tail): ${stderrTail || "<empty>"}`);
  }
}

run().catch(error => {
  result.error = step + ": " + error.message;
  process.exitCode = 1;
}).finally(() => console.log(JSON.stringify(result)));
