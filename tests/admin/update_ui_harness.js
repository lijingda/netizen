const assert = require("node:assert/strict");
const state = { tab: "updates", updates: null };
const elements = new Map();
const document = { body: { classList: { toggle() {} } }, querySelectorAll() { return []; }, querySelector(id) {
  if (!elements.has(id)) elements.set(id, { textContent: "", disabled: false });
  return elements.get(id);
} };
let redirected = null;
let confirmation = true;
const confirmations = [];
const window = {
  location: { assign(value) { redirected = value; } },
  confirm(message) { confirmations.push(message); return confirmation; },
};
let status = "";
function setStatus(message) { status = message; }
let now = 1000;
Date.now = () => now;
let timerIdentity = 0;
const timers = new Map();
function setTimeout(callback, delay) {
  const id = ++timerIdentity;
  timers.set(id, { callback, delay });
  return id;
}
function clearTimeout(id) { timers.delete(id); }
const requests = [];
let answer;
const diagnostics = {
  root: "/instance", checkedAt: 1900000000, bound: true, loaded: true, running: true, ready: true, enabled: true,
  binding: { python: "/env/bin/python", prefix: "/env", codexHome: null },
};
let diagnosticsAnswer = async () => response(diagnostics);
async function fetch(path, options) {
  requests.push({ path, options });
  if (path === "/api/v1/instance-diagnostics") return diagnosticsAnswer();
  return answer(path, options);
}
function response(data, status = 200) {
  return { status, ok: status >= 200 && status < 300, json: async () => data };
}
const target = {
  version: "1.1.0", releaseId: 12,
  installerSha256: "a".repeat(64), archiveSha256: "b".repeat(64),
};
const restartEnvelope = { csrfToken: "csrf", actionToken: "action", target: restartTarget };
function freshStatus(operation = null) {
  return {
    current: { version: "1.0.0", source: "python" },
    restartSupported: true, restartAvailable: true,
    operation, actions: { restart: restartEnvelope },
  };
}
const operation = (phase, id = "new-operation") => ({
  schema: 1, operationId: id, phase, code: "none", target,
});
const restartOperation = (phase, id = "new-restart") => ({
  schema: 3, kind: "restart", operationId: id, phase, code: "none",
  target: { version: "1.0.0", installationId: restartTarget.targetId },
});
// SHIPPED_UPDATE_CONTROLLER
(async () => {
  answer = async () => response(freshStatus(restartOperation("succeeded", "old-restart")));
  await loadUpdates();
  assert.equal(elements.get("#service-restart").disabled, false);
  assert.match(elements.get("#update-message").textContent, /netizen update/);
  assert(!elements.has("#update-install"));
  assert.equal(elements.get("#diagnostics-running").textContent, "运行中");
  assert.equal(elements.get("#diagnostics-ready").textContent, "已就绪");
  assert.equal(elements.get("#diagnostics-enabled").textContent, "已启用");
  assert.match(elements.get("#diagnostics-codex-home").textContent, /未显式指定/);
  assert.match(elements.get("#diagnostics-checked-at").textContent, /上次成功检查/);
  assert.equal(updatePollTimer, null, "successful diagnostics never starts background polling");
  const diagnosticReadsBeforeRestart = requests.filter(r => r.path === "/api/v1/instance-diagnostics").length;
  confirmation = false;
  await restartService();
  assert.equal(requests.filter(({options}) => options.method === "POST").length, 0);
  assert.match(confirmations.at(-1), /不更新软件包/);
  assert.match(confirmations.at(-1), /不会自动续跑.*重新登录/);

  confirmation = true;
  let rejectSubmit;
  answer = () => new Promise((_resolve, reject) => { rejectSubmit = reject; });
  const first = restartService();
  await restartService();
  assert.equal(requests.filter(({options}) => options.method === "POST").length, 1);
  assert.equal(requests.at(-1).path, "/api/v1/updates/restart");
  assert.equal(elements.get("#service-restart").disabled, true);
  rejectSubmit(new TypeError("connection lost"));
  await first;
  assert.equal(updateNeedsPolling(), true);
  assert.match(status, /重启提交结果未确认/);

  for (const unrelated of [
    restartOperation("succeeded", "old-restart"),
    { ...restartOperation("succeeded"), target: { version: "1.0.0", installationId: "d".repeat(64) } },
    operation("succeeded"),
    { ...restartOperation("succeeded"), schema: 2,
      target: { version: "1.0.0", releaseDigest: restartTarget.targetId } },
  ]) {
    answer = async () => response(freshStatus(unrelated));
    await pollUpdateStatus();
    assert.equal(updateExpectedTarget, restartTarget);
    assert.match(elements.get("#update-phase").textContent, /重启提交结果尚未确认/);
    await restartService();
  }
  assert.equal(requests.filter(({options}) => options.method === "POST").length, 1);
  answer = async () => response(freshStatus(restartOperation("restarting")));
  await pollUpdateStatus();
  assert.equal(updateExpectedTarget, null);
  assert.equal(elements.get("#update-phase").textContent, "正在重启");
  assert.doesNotMatch(elements.get("#update-detail").textContent, /已确认/);

  answer = async () => { throw new TypeError("offline"); };
  await pollUpdateStatus();
  assert.equal(updatePollDelay, 4000);
  await pollUpdateStatus();
  assert.equal(updatePollDelay, 8000);
  await pollUpdateStatus();
  assert.equal(updatePollDelay, 15000);
  now += 5 * 60 * 1000;
  await pollUpdateStatus();
  assert.equal(updatePollExpired, true);
  assert.equal(updatePollTimer, null);
  assert.match(elements.get("#update-detail").textContent, /手动检查/);
  assert.doesNotMatch(elements.get("#update-phase").textContent, /成功|失败/);
  assert.equal(requests.filter(r => r.path === "/api/v1/instance-diagnostics").length, diagnosticReadsBeforeRestart,
    "restart polling must not poll diagnostics");

  answer = async () => response(freshStatus(restartOperation("succeeded")));
  await loadUpdates();
  assert.equal(updatePollExpired, false);
  assert.equal(elements.get("#update-phase").textContent, "重启成功");
  assert.match(elements.get("#update-detail").textContent, /服务就绪已确认/);
  assert.equal(updateNeedsPolling(), false);

  const blocked = freshStatus({ ...restartOperation("recovery_required"), code: "restart_failed" });
  blocked.restartAvailable = false;
  blocked.actions.restart = null;
  acceptUpdateStatus(blocked);
  assert.equal(elements.get("#service-restart").disabled, true);
  assert.match(elements.get("#restart-message").textContent, /暂不可重启/);
  answer = async () => response(freshStatus({ ...restartOperation("recovered"), code: "service_ready" }));
  await loadUpdates();
  assert.equal(elements.get("#update-phase").textContent, "服务已恢复");
  assert.match(elements.get("#update-detail").textContent, /原重启操作不标记为成功/);

  answer = async () => response({ message: "已有维护操作" }, 409);
  await restartService();
  assert.equal(updateExpectedTarget, null);
  assert.equal(state.updates.actions.restart, null);
  const count = requests.length;
  await restartService();
  assert.equal(requests.length, count);

  // Hide old upgrade outcomes, but preserve restart results and unresolved fences.
  for (const phase of ["succeeded", "failed", "rolled_back", "requires_action", "recovered"]) {
    acceptUpdateStatus(freshStatus(operation(phase)));
    assert.equal(elements.get("#update-operation").hidden, true);
    assert.equal(elements.get("#service-restart").disabled, false);
  }
  for (const phase of ["accepted", "restarting", "recovery_required", "unknown_phase"]) {
    const legacy = freshStatus(operation(phase));
    legacy.restartAvailable = false;
    legacy.actions.restart = null;
    acceptUpdateStatus(legacy);
    assert.equal(elements.get("#update-operation").hidden, false);
    assert.equal(elements.get("#service-restart").disabled, true);
    assert.match(elements.get("#update-phase").textContent, /历史维护/);
    assert.doesNotMatch(elements.get("#update-detail").textContent, /官方安装器/);
  }
  acceptUpdateStatus(freshStatus({ ...restartOperation("succeeded"), schema: 2,
    target: { version: "1.0.0", releaseDigest: restartTarget.targetId } }));
  assert.equal(elements.get("#update-phase").textContent, "重启成功");
  assert.match(elements.get("#update-detail").textContent, /保持版本 1.0.0/);
  assert.equal(elements.get("#update-operation").hidden, false);

  // Diagnostics failures clear stale healthy badges without changing restart eligibility.
  answer = async () => response(freshStatus());
  const checkedAt = elements.get("#diagnostics-checked-at").textContent;
  diagnosticsAnswer = async () => response({ message: "private manager detail" }, 503);
  await loadUpdates();
  for (const key of ["bound", "loaded", "running", "ready", "enabled"]) {
    assert.equal(elements.get(`#diagnostics-${key}`).textContent, "无法确认");
    assert.doesNotMatch(elements.get(`#diagnostics-${key}`).className, /\bon\b/);
  }
  assert.equal(elements.get("#diagnostics-checked-at").textContent, checkedAt);
  assert.match(elements.get("#diagnostics-message").textContent, /本次检查失败/);
  assert.doesNotMatch(elements.get("#diagnostics-message").textContent, /private/);
  assert.equal(elements.get("#service-restart").disabled, false);
  assert.equal(updatePollTimer, null);
  assert.equal(elements.get("#update-operation").hidden, true);

  // Null autostart is unknown, not disabled; raw paths are text, not HTML.
  diagnosticsAnswer = async () => response({ ...diagnostics, enabled: null, checkedAt: 1900000100,
    binding: { ...diagnostics.binding, python: "/env/<img src=x>/python" } });
  await loadUpdates();
  assert.equal(elements.get("#diagnostics-enabled").textContent, "无法确认");
  assert.equal(elements.get("#diagnostics-python").textContent, "/env/<img src=x>/python");
  assert.notEqual(elements.get("#diagnostics-checked-at").textContent, checkedAt);
  diagnosticsAnswer = async () => response({ ...diagnostics, bound: false, loaded: false,
    running: false, ready: false, enabled: null, binding: null });
  await loadUpdates();
  assert.equal(elements.get("#diagnostics-bound").textContent, "未注册");
  assert.equal(elements.get("#diagnostics-running").textContent, "未运行");
  assert.equal(elements.get("#diagnostics-python").textContent, "未绑定");

  let completeDiagnostics;
  diagnosticsAnswer = () => new Promise(resolve => { completeDiagnostics = resolve; });
  const pendingDiagnostics = loadInstanceDiagnostics();
  const readCount = requests.length;
  assert.equal(loadInstanceDiagnostics(), pendingDiagnostics);
  assert.equal(requests.length, readCount, "in-flight diagnostics must be deduplicated");
  completeDiagnostics(response(diagnostics));
  await pendingDiagnostics;
  assert.equal(updatePollTimer, null);

  // A failed maintenance read does not discard an independently successful diagnostic.
  answer = async () => response({ message: "maintenance unavailable" }, 503);
  diagnosticsAnswer = async () => response({ ...diagnostics, running: false, ready: false });
  await assert.rejects(loadUpdates(), /maintenance unavailable/);
  await Promise.resolve();
  assert.equal(elements.get("#diagnostics-running").textContent, "未运行");

  // A late diagnostic 401 must not turn the same refresh into a success after
  // maintenance already returned. Login expiry remains the terminal UI state.
  answer = async () => response(freshStatus());
  diagnosticsAnswer = () => new Promise(resolve => { completeDiagnostics = resolve; });
  const expiredRefresh = refresh("updates");
  for (let tick = 0; tick < 20 && updateReading; tick += 1) await Promise.resolve();
  assert.equal(updateReading, false);
  completeDiagnostics(response({ message: "expired" }, 401));
  assert.equal(await expiredRefresh, false);
  assert.match(status, /登录已过期/);
  assert.equal(state.authExpired, true);
  assert.equal(elements.get("#auth-expired").hidden, false);
  // Model a fresh login for the independent restart-poll expiry case below.
  state.authExpired = false;

  acceptUpdateStatus(freshStatus(restartOperation("restarting")));
  answer = async () => response({message: "登录失效"}, 401);
  stopUpdatePolling();
  await pollUpdateStatus();
  assert.equal(redirected, null);
  assert.equal(state.authExpired, true);
  assert.equal(elements.get("#auth-expired").hidden, false);
  assert.equal(updatePollTimer, null);
  const requestCount = requests.length;
  await assert.rejects(api("/api/v1/updates"), (error) => error.status === 401);
  assert.equal(requests.length, requestCount);
  assert.doesNotMatch(elements.get("#update-phase").textContent, /成功/);
})().catch((error) => { console.error(error); process.exitCode = 1; });
