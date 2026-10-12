
document.head = document.querySelector("head");
const state = { tab: "system" };
const statusMessages = [];
function setStatus(message) { statusMessages.push(message); }
let pollsStopped = 0;
function stopUpdatePolling() { pollsStopped += 1; }
let nextTimer = 0;
const timers = new Map();
function setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; }
function clearTimeout(id) { timers.delete(id); }
let confirmed = false;
const confirmations = [];
window.confirm = (text) => { confirmations.push(text); return confirmed; };
let navigation = null;
let reloads = 0;
window.location = { assign(path) { navigation = path; }, reload() { reloads += 1; } };
const calls = [];
let answer;
async function fetch(path, options = {}) { calls.push({ path, options }); return answer(path, options); }
const response = (data, status = 200) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const settle = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve(); };
const grant = (name) => ({ csrfToken: `csrf-${name}`, actionToken: `action-${name}`, target: { resource: "admin-users", targetId: name } });
const alice = { openId: "ou_alice", name: "<img src=x onerror=alert(1)>", avatarUrl: "https://images.example/alice.png", removeAction: grant("alice") };
const bob = { openId: "ou_bob", name: "Bob", avatarUrl: "javascript:alert(1)", removeAction: grant("bob") };
let members = [alice, bob];
let revision = 1;
const listing = () => ({ revision, currentOpenId: alice.openId, users: members, addAction: grant("add"), selectorAction: grant("selector") });
let config;
let selectedCallback;
let unmounts = 0;
let candidate;
let candidateData;
window.webComponent = {
  async config(value) { config = value; },
  render(kind, options, mount) {
    assert.equal(kind, "Selector");
    assert.deepEqual(options.searchEntityTypes, [1]);
    assert.equal(mount.id, "admin-selector");
    selectedCallback = options.onSelect;
    // The official SDK mounts its dropdown outside the picker container.
    candidate = document.createElement("button");
    candidate.addEventListener("click", () => {
      assert.equal(document.querySelectorAll("dialog").some((dialog) => dialog.open), false);
      options.onSelect(candidateData);
    });
    document.body.append(candidate);
    return { unmount() { unmounts += 1; candidate.disabled = true; } };
  },
  onError() {}, onAuthError() {},
};
const append = document.head.append.bind(document.head);
document.head.append = (script) => { append(script); queueMicrotask(() => script.onload()); };
answer = async (path, options) => {
  if (path === "/api/v1/identity") return response(alice);
  if (path === "/api/v1/admin-users") return response(listing());
  if (path.endsWith("/selector-config")) return response({ openId: "ou_alice", appId: "cli_app", signature: "signed", timestamp: "123", nonceStr: "nonce", url: "http://localhost/", jsApiList: ["Selector"], locale: "zh-CN" });
  if (path.endsWith("/add")) {
    const body = JSON.parse(options.body);
    members = [...members, { ...body, removeAction: grant(body.openId) }]; revision += 1;
    return response({});
  }
  if (path.endsWith("/remove")) {
    const body = JSON.parse(options.body);
    members = members.filter((item) => item.openId !== body.openId); revision += 1;
    return response({ loggedOut: body.openId === alice.openId });
  }
  throw new Error(`Unexpected path ${path}`);
};
// SHIPPED_IDENTITY_CONTROLLER
(async () => {
  await Promise.all([window.NetizenIdentity.loadProfile(), window.NetizenIdentity.loadProfile()]);
  assert.equal(calls.filter((call) => call.path === "/api/v1/identity").length, 1);
  const current = document.querySelector("#current-user");
  assert.match(current.textContent, /<img src=x/);
  assert.equal(current.querySelectorAll("img").length, 1);
  current.querySelector("img").dispatch("error");
  assert.equal(current.querySelector("img").hidden, true);
  assert.equal(current.children[0].hidden, false);
  await window.NetizenIdentity.loadUsers();
  await window.NetizenIdentity.loadUsers();
  assert.equal(calls.filter((call) => call.path === "/api/v1/identity").length, 1);
  const list = document.querySelector("#admin-users");
  assert.equal(list.children.length, 2);
  assert.equal(list.children[1].querySelectorAll("img").length, 0);
  assert.equal(document.head.querySelectorAll("script").filter((script) => script.src).length, 0);

  list.children[1].querySelector("button").click();
  await settle();
  assert.equal(confirmations.length, 1);
  assert.equal(calls.filter((call) => call.path.endsWith("/remove")).length, 0);
  confirmed = true;
  list.children[1].querySelector("button").click();
  await settle();
  assert.equal(members.length, 1);
  assert.equal(list.children[0].querySelector("button").disabled, true);
  list.children[0].querySelector("button").click();
  await settle();
  assert.equal(calls.filter((call) => call.path.endsWith("/remove")).length, 1);

  document.querySelector("#admin-user-add").click();
  await settle();
  assert.equal(document.querySelector("#admin-user-panel").hidden, false);
  assert.equal(document.querySelector("#admin-user-add").getAttribute("aria-expanded"), "true");
  const openedCalls = calls.length;
  document.querySelector("#admin-user-add").click();
  await settle();
  assert.equal(calls.length, openedCalls);
  const scripts = document.head.querySelectorAll("script").filter((script) => script.src);
  assert.equal(scripts.length, 1);
  assert.equal(scripts[0].src, "https://lf3-cdn-tos.bytegoofy.com/obj/goofy/locl/lark/external_js_sdk/h5-js-sdk-1.2.21.js");
  assert.equal(config.signature, "signed");
  assert.equal(config.openId, "ou_alice");
  assert.equal("accessToken" in config, false);
  selectedCallback({ type: "user", id: "ou_new", entity: { id: "ou_other" } });
  assert.equal(document.querySelector("#admin-user-confirm").disabled, true);
  assert.equal(document.querySelector("#admin-selector-retry").hidden, false);
  document.querySelector("#admin-selector-retry").click();
  await settle();
  candidateData = { type: "user", id: "ou_new", entity: { id: "ou_new", name: "<script>test</script>", avatarUrl: "data:text/html,bad" } };
  candidate.click();
  const selected = document.querySelector("#admin-user-selection");
  assert.match(selected.textContent, /<script>test<\/script>/);
  assert.equal(selected.querySelectorAll("script").length, 0);
  assert.equal(selected.querySelectorAll("img").length, 0);
  document.querySelector("#admin-user-confirm").click();
  document.querySelector("#admin-user-confirm").click();
  await settle();
  assert.equal(calls.filter((call) => call.path.endsWith("/add")).length, 1);
  const payload = JSON.parse(calls.find((call) => call.path.endsWith("/add")).options.body);
  assert.equal(payload.openId, "ou_new");
  assert.equal(payload.entityId, "ou_new");
  assert.equal(payload.revision, 2);
  assert.equal(payload.actionToken, "action-add");
  assert.equal(document.querySelector("#admin-user-panel").hidden, true);
  assert.equal(document.querySelector("#admin-user-add").getAttribute("aria-expanded"), "false");
  assert(unmounts >= 2);

  // Closing a picker invalidates late SDK callbacks without granting access.
  document.querySelector("#admin-user-add").click();
  await settle();
  const lateSelection = selectedCallback;
  document.querySelector("#admin-user-close").click();
  lateSelection({ type: "user", id: "ou_late", entity: { id: "ou_late", name: "Late" } });
  assert.equal(document.querySelector("#admin-user-confirm").disabled, true);

  // Leaving this management tab unmounts the body-mounted dropdown too.
  document.querySelector("#admin-user-add").click();
  await settle();
  const beforeNavigation = unmounts;
  window.NetizenIdentity.closePanel();
  assert.equal(unmounts, beforeNavigation + 1);
  assert.equal(document.querySelector("#admin-user-panel").hidden, true);

  // SDK config can remain pending after a network failure. Timeout must offer a
  // fresh page, not queue more attempts behind it or revive a late result.
  let finishConfig;
  let stalledConfigCalls = 0;
  window.webComponent.config = () => {
    stalledConfigCalls += 1;
    return new Promise((resolve) => { finishConfig = resolve; });
  };
  document.querySelector("#admin-user-add").click();
  await settle();
  assert.equal(stalledConfigCalls, 1);
  const configTimeout = [...timers.values()].find((timer) => timer.delay === 15000);
  assert(configTimeout);
  document.querySelector("#admin-user-close").click();
  document.querySelector("#admin-user-add").click();
  await settle();
  assert.equal(stalledConfigCalls, 1);
  configTimeout.callback();
  await settle();
  // The queued opener must not mistake a skipped config for success when the
  // old request finally completes after its timeout.
  finishConfig();
  await settle();
  assert.equal(document.querySelector("#admin-selector-retry").hidden, true);
  assert.equal(document.querySelector("#admin-selector-reload").hidden, false);
  assert.match(document.querySelector("#admin-selector-status").textContent, /重新加载页面/);
  assert.equal(document.querySelector("#admin-user-confirm").disabled, true);
  const stalledCalls = calls.length;
  document.querySelector("#admin-user-close").click();
  document.querySelector("#admin-user-add").click();
  await settle();
  assert.equal(calls.length, stalledCalls);
  assert.equal(stalledConfigCalls, 1);
  assert.equal(document.querySelector("#admin-selector-reload").hidden, false);
  assert.equal(document.querySelector("#admin-user-confirm").disabled, true);
  assert.equal(document.querySelector("#admin-selector").children.length, 0);
  assert.match(document.querySelector("#admin-selector-status").textContent, /重新加载页面/);
  document.querySelector("#admin-selector-reload").click();
  assert.equal(reloads, 1);
  document.querySelector("#admin-user-close").click();

  // A successful response whose JSON arrives after revocation cannot restore UI state.
  const ordinaryAnswer = answer;
  let finishBody;
  answer = async (path, options) => path === "/api/v1/slow"
    ? { status: 200, ok: true, json: () => new Promise((resolve) => { finishBody = resolve; }) }
    : ordinaryAnswer(path, options);
  const lateRequest = api("/api/v1/slow");
  await settle();

  // Removing oneself confirms exactly once, invalidates the UI, and stops requests.
  const priorConfirmations = confirmations.length;
  list.children[0].querySelector("button").click();
  await settle();
  assert.equal(confirmations.length, priorConfirmations + 1);
  assert.match(confirmations.at(-1), /将退出 Admin/);
  assert.equal(state.authExpired, true);
  assert.equal(document.querySelector("#auth-expired").hidden, false);
  assert.equal(document.querySelector("#system").inert, true);
  assert.equal(navigation, null);
  assert.equal(pollsStopped, 1);
  finishBody({ stale: "success" });
  await assert.rejects(lateRequest, (error) => error.status === 401);
  const requestCount = calls.length;
  await assert.rejects(api("/api/v1/admin-users"), (error) => error.status === 401);
  assert.equal(calls.length, requestCount);
})().catch((error) => { console.error(error); process.exitCode = 1; });
