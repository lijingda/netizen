const assert = require("node:assert/strict");
const state = { tab: "updates", updates: null };
const elements = new Map();
const document = { querySelector(id) {
  if (!elements.has(id)) elements.set(id, { textContent: "", disabled: false });
  return elements.get(id);
} };
let redirected = null;
const window = { location: { assign(value) { redirected = value; } } };
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
function freshStatus(operation = null) {
  return {
    current: { version: "1.0.0", source: "published" }, supported: true,
    available: true, latest: { ...target, notes: "<img src=x onerror=alert(1)>",
      url: "https://github.com/lijingda/netizen/releases/tag/v1.1.0" },
    operation, actions: { check: envelope, install: envelope },
  };
}
const operation = (phase, id = "new-operation") => ({
  operationId: id, phase, code: "none", target,
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
  assert.match(elements.get("#update-detail").textContent, /原升级操作不标记为成功/);
  assert.equal(elements.get("#update-install").disabled, false);
  assert.equal(updateNeedsPolling(), false);

  answer = async () => response(freshStatus());
  await loadUpdates();
  answer = async () => response({message: "已有安装操作"}, 409);
  await installUpdate();
  assert.equal(updateExpectedTarget, null);
  assert.match(status, /已有安装操作/);
  assert.equal(state.updates.actions.install, null);
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
})().catch((error) => { console.error(error); process.exitCode = 1; });
