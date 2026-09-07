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
const envelope = { csrfToken: "csrf", actionToken: "action", target };
const restartEnvelope = { ...envelope, target: restartTarget };
function freshStatus(operation = null) {
  return {
    current: { version: "1.0.0", source: "published" }, supported: true,
    available: true, latest: { ...target, notes: "<img src=x onerror=alert(1)>",
      url: "https://github.com/lijingda/netizen/releases/tag/v1.1.0" },
    restartSupported: true, restartAvailable: true,
    operation, actions: { check: envelope, install: envelope, restart: restartEnvelope },
  };
}
const operation = (phase, id = "new-operation") => ({
  operationId: id, phase, code: "none", target,
});
const restartOperation = (phase, id = "new-restart") => ({
  schema: 2, kind: "restart", operationId: id, phase, code: "none",
  target: { version: "1.0.0", releaseDigest: restartTarget.targetId },
  previousRelease: restartTarget.targetId,
});
// SHIPPED_UPDATE_CONTROLLER
(async () => {
  answer = async () => response(freshStatus(operation("rolled_back", "old-operation")));
  await loadUpdates();
  assert.equal(elements.get("#update-notes").textContent, "<img src=x onerror=alert(1)>");
  assert.equal(elements.get("#update-release-link").href,
    "https://github.com/lijingda/netizen/releases/tag/v1.1.0");
  assert.equal(elements.get("#update-install").disabled, false);
  assert.equal(elements.get("#update-message").textContent, "发现新版本。");

  let rejectSubmit;
  answer = () => new Promise((_resolve, reject) => { rejectSubmit = reject; });
  const first = installUpdate();
  await installUpdate();
  await restartService();
  assert.equal(confirmations.length, 0);
  assert.equal(elements.get("#service-restart").disabled, true);
  assert.equal(requests.filter(({options}) => options.method === "POST").length, 1);
  rejectSubmit(new TypeError("connection lost"));
  await first;
  assert.equal(updateNeedsPolling(), true);
  assert.equal(elements.get("#update-install").disabled, true);
  assert.match(status, /未确认/);

  // The previous attempt of the same version is not evidence for this request.
  answer = async () => response(freshStatus(operation("rolled_back", "old-operation")));
  await pollUpdateStatus();
  assert.equal(updateExpectedTarget, target);
  assert.match(elements.get("#update-phase").textContent, /提交结果尚未确认/);
  await installUpdate();
  assert.equal(requests.filter(({options}) => options.method === "POST").length, 1);

  answer = async () => response(freshStatus(operation("accepted")));
  await pollUpdateStatus();
  assert.equal(updateExpectedTarget, null);
  assert.equal(elements.get("#update-phase").textContent, "升级已受理");
  assert.equal(updateNeedsPolling(), true);
  // Reconnection to an active old or new service alone never reports success.
  assert.doesNotMatch(elements.get("#update-phase").textContent, /成功/);
  assert(requests.every(({path}) => path.startsWith("/api/v1/updates")));

  answer = async () => { throw new TypeError("offline"); };
  await pollUpdateStatus();
  assert.equal(updatePollDelay, 4000);
  assert.match(elements.get("#update-phase").textContent, /连接暂时中断/);
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

  answer = async () => response(freshStatus(operation("succeeded")));
  await loadUpdates();
  assert.equal(updatePollExpired, false);
  assert.equal(updateNeedsPolling(), false);
  assert.equal(elements.get("#update-phase").textContent, "升级成功");
  assert.equal(updatePollTimer, null);

  const restricted = freshStatus(operation("recovery_required"));
  restricted.available = false;
  acceptUpdateStatus(restricted);
  assert.doesNotMatch(elements.get("#update-message").textContent, /已是最新/);
  restricted.supported = false;
  restricted.current.source = "source";
  restricted.message = "请在源码目录运行 ./dev-install.sh。";
  restricted.actions.install = null;
  acceptUpdateStatus(restricted);
  assert.equal(elements.get("#update-install").disabled, true);
  assert.match(elements.get("#update-phase").textContent, /未确认/);
  assert.match(elements.get("#update-message").textContent, /dev-install/);
  assert.equal(updateNeedsPolling(), false);

  const recovered = freshStatus({...operation("recovered"), code: "manual_recovery"});
  acceptUpdateStatus(recovered);
  assert.equal(elements.get("#update-phase").textContent, "已通过安装器恢复");
  assert.match(elements.get("#update-detail").textContent, /原操作不标记为成功/);
  assert.equal(elements.get("#update-install").disabled, false);
  assert.equal(updateNeedsPolling(), false);

  answer = async () => response(freshStatus());
  await loadUpdates();
  answer = async () => response({message: "已有安装操作"}, 409);
  await installUpdate();
  assert.equal(updateExpectedTarget, null);
  assert.match(status, /已有安装操作/);
  assert.equal(state.updates.actions.install, null);
  assert.equal(state.updates.actions.restart, null);
  const postCount = requests.filter(({options}) => options.method === "POST").length;
  await installUpdate();
  assert.equal(requests.filter(({options}) => options.method === "POST").length, postCount);

  // A lost check response also consumes the one-shot grant locally.
  answer = async () => { throw new TypeError("offline"); };
  await checkUpdate();
  assert.equal(state.updates.actions.check, null);
  const checkedCount = requests.filter(({options}) => options.method === "POST").length;
  await checkUpdate();
  assert.equal(requests.filter(({options}) => options.method === "POST").length, checkedCount);

  acceptUpdateStatus(freshStatus(operation("restarting")));
  answer = async () => response({message: "登录失效"}, 401);
  stopUpdatePolling();
  await pollUpdateStatus();
  assert.equal(redirected, "/login");
  assert.equal(updatePollTimer, null);
  assert.doesNotMatch(elements.get("#update-phase").textContent, /成功/);

  // Restart is independent of release checks, including a managed source install.
  const sourceStatus = freshStatus();
  sourceStatus.current.source = "source";
  sourceStatus.supported = false;
  sourceStatus.available = false;
  sourceStatus.latest = null;
  sourceStatus.actions.install = null;
  acceptUpdateStatus(sourceStatus);
  assert.equal(elements.get("#service-restart").disabled, false);
  assert.equal(elements.get("#update-install").disabled, true);
  confirmation = false;
  const beforeCancel = requests.length;
  await restartService();
  assert.equal(requests.length, beforeCancel);
  assert.equal(updateExpectedTarget, null);
  assert.equal(state.updates.actions.restart, restartEnvelope);
  assert.match(confirmations.at(-1), /保持当前版本/);
  assert.match(confirmations.at(-1), /中断正在执行的任务.*暂停 Goal.*结束临时 Side/);
  assert.match(confirmations.at(-1), /不会自动续跑.*重新登录/);

  const noUpdate = freshStatus(restartOperation("succeeded", "old-restart"));
  noUpdate.available = false;
  noUpdate.latest.version = noUpdate.current.version;
  noUpdate.actions.install = null;
  acceptUpdateStatus(noUpdate);
  assert.equal(elements.get("#service-restart").disabled, false);
  confirmation = true;
  answer = () => new Promise((_resolve, reject) => { rejectSubmit = reject; });
  const restart = restartService();
  await restartService();
  await installUpdate();
  assert.equal(requests.at(-1).path, "/api/v1/updates/restart");
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), actionPayload(restartEnvelope));
  assert.equal(elements.get("#service-restart").disabled, true);
  assert.equal(elements.get("#update-install").disabled, true);
  rejectSubmit(new TypeError("connection lost"));
  await restart;
  assert.equal(updateNeedsPolling(), true);
  assert.match(status, /重启提交结果未确认/);
  const restartPostCount = requests.filter(({options}) => options.method === "POST").length;

  // An old restart, a different digest or an upgrade cannot resolve this submission.
  for (const unrelated of [
    restartOperation("succeeded", "old-restart"),
    { ...restartOperation("succeeded"), target: { version: "1.0.0", releaseDigest: "d".repeat(64) } },
    { ...restartOperation("succeeded"), schema: 1, kind: undefined },
  ]) {
    answer = async () => response(freshStatus(unrelated));
    await pollUpdateStatus();
    assert.equal(updateExpectedTarget, restartTarget);
    assert.match(elements.get("#update-phase").textContent, /重启提交结果尚未确认/);
    await restartService();
    await installUpdate();
  }
  assert.equal(requests.filter(({options}) => options.method === "POST").length, restartPostCount);
  answer = async () => response(freshStatus(restartOperation("restarting")));
  await pollUpdateStatus();
  assert.equal(updateExpectedTarget, null);
  assert.equal(elements.get("#update-phase").textContent, "正在重启");
  assert.doesNotMatch(elements.get("#update-detail").textContent, /已确认/);

  // After login, only the persisted result proves completion.
  answer = async () => response(freshStatus(restartOperation("succeeded")));
  await loadUpdates();
  assert.equal(elements.get("#update-phase").textContent, "重启成功");
  assert.match(elements.get("#update-detail").textContent, /保持版本 1.0.0.*服务就绪已确认/);
  assert.equal(updateNeedsPolling(), false);
  assert.equal(elements.get("#service-restart").disabled, false);
  acceptUpdateStatus(freshStatus({ ...restartOperation("failed"), code: "restart_failed" }));
  assert.equal(elements.get("#update-phase").textContent, "重启失败");
  assert.match(elements.get("#update-detail").textContent, /检查服务日志/);
  acceptUpdateStatus(freshStatus({ ...restartOperation("recovered"), code: "manual_recovery" }));
  assert.match(elements.get("#update-detail").textContent, /原操作不标记为成功/);

  const unsupportedRestart = freshStatus();
  unsupportedRestart.restartSupported = false;
  acceptUpdateStatus(unsupportedRestart);
  const beforeUnsupported = requests.length;
  await restartService();
  assert.equal(elements.get("#service-restart").disabled, true);
  assert.equal(requests.length, beforeUnsupported);
  const blockedRestart = freshStatus(restartOperation("recovery_required"));
  blockedRestart.restartAvailable = false;
  blockedRestart.actions.restart = null;
  acceptUpdateStatus(blockedRestart);
  assert.equal(elements.get("#service-restart").disabled, true);
  assert.match(elements.get("#restart-message").textContent, /暂不可重启/);

  acceptUpdateStatus(freshStatus());
  redirected = null;
  answer = async () => response({ message: "登录失效" }, 401);
  await restartService();
  assert.equal(redirected, "/login");
  assert.equal(updatePollTimer, null);
})().catch((error) => { console.error(error); process.exitCode = 1; });
