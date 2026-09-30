const assert = require("node:assert/strict");
const state = { tab: "updates", updates: null };
const elements = new Map();
const document = { querySelector(id) {
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
async function fetch(path, options) {
  requests.push({ path, options });
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
    current: { version: "1.0.0", source: "python" }, supported: false,
    available: false, latest: null,
    restartSupported: true, restartAvailable: true,
    operation, actions: { check: null, install: null, restart: restartEnvelope },
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

  // Historical upgrade/restart records remain readable without offering installation.
  acceptUpdateStatus(freshStatus(operation("rolled_back")));
  assert.equal(elements.get("#update-phase").textContent, "升级失败，已回滚");
  assert.match(elements.get("#update-detail").textContent, /目标版本 1.1.0/);
  acceptUpdateStatus(freshStatus({ ...restartOperation("succeeded"), schema: 2,
    target: { version: "1.0.0", releaseDigest: restartTarget.targetId } }));
  assert.equal(elements.get("#update-phase").textContent, "重启成功");
  assert.match(elements.get("#update-detail").textContent, /保持版本 1.0.0/);

  acceptUpdateStatus(freshStatus(restartOperation("restarting")));
  answer = async () => response({message: "登录失效"}, 401);
  stopUpdatePolling();
  await pollUpdateStatus();
  assert.equal(redirected, "/login");
  assert.equal(updatePollTimer, null);
  assert.doesNotMatch(elements.get("#update-phase").textContent, /成功/);
})().catch((error) => { console.error(error); process.exitCode = 1; });
