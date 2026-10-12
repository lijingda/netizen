
document.querySelector("#login-nonce").value = "browser-nonce";
const returnTarget = "/sessions/ordinary?project=R%26D&sortDirection=asc";
document.querySelector("#login-next").value = returnTarget;
let navigation = null;
window.location = { assign(path) { navigation = path; } };
const calls = [];
const timers = new Map();
let nextTimer = 0;
function setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; }
function clearTimeout(id) { timers.delete(id); }
const settle = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve(); };
const response = (data, status = 200) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
let server = { state: "idle" };
let delayedState = null;
async function fetch(path, options = {}) {
  calls.push({ path, options });
  if (path.endsWith("/state")) {
    if (delayedState) return new Promise((resolve) => { delayedState.resolve = resolve; });
    return response(server);
  }
  assert.equal(options.method, "POST");
  assert.deepEqual(JSON.parse(options.body), { nonce: "browser-nonce" });
  if (path.endsWith("/start")) {
    server = { state: "pending", userCode: "ABCD", verificationUri: "https://accounts.feishu.cn/auth?user_code=ABCD", qrUrl: "/api/v1/login/qr", interval: 5, expiresAt: 1000 };
  } else if (path.endsWith("/cancel")) server = { state: "idle" };
  else throw new Error(`Unexpected path ${path}`);
  return response(server);
}
// SHIPPED_IDENTITY_CONTROLLER
(async () => {
  await settle();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/v1/login/state");
  assert.equal(document.querySelector("#login-start").disabled, false);
  assert.equal(document.querySelector("#login-authorization").hidden, true);
  assert.equal(document.querySelector("#login-retry").href, `/login?next=${encodeURIComponent(returnTarget)}`);
  document.querySelector("#login-start").click();
  document.querySelector("#login-start").click();
  await settle();
  assert.equal(calls.filter((call) => call.path.endsWith("/start")).length, 1);
  assert.equal(document.querySelector("#login-authorization").hidden, false);
  assert.equal(document.querySelector("#login-code").textContent, "ABCD");
  assert.equal(document.querySelector("#login-qr").getAttribute("src"), "/api/v1/login/qr");
  assert.equal(document.querySelector("#login-link").href, server.verificationUri);
  assert.equal(navigation, null);
  assert([...timers.values()].some((timer) => timer.delay === 5000));

  // Never trust executable navigation or arbitrary image sources in an API response.
  server = { ...server, verificationUri: "javascript:alert(1)", qrUrl: "https://evil.example/qr" };
  document.querySelector("#login-check").click();
  await settle();
  assert.equal(document.querySelector("#login-link").hidden, true);
  assert.equal(document.querySelector("#login-qr").hidden, true);

  // Cancellation wins over an already in-flight poll returning success.
  delayedState = {};
  document.querySelector("#login-check").click();
  await settle();
  assert.equal(typeof delayedState.resolve, "function");
  document.querySelector("#login-cancel").click();
  await settle();
  delayedState.resolve(response({ state: "authenticated" }));
  await settle();
  assert.equal(navigation, null);
  assert.equal(document.querySelector("#login-authorization").hidden, true);
  assert.equal(document.querySelector("#login-retry").hidden, false);
  assert.match(document.querySelector("#login-status").textContent, /此次登录已结束/);
  assert.equal(timers.size, 0);
  document.querySelector("#login-start").click();
  await settle();
  assert.equal(calls.filter((call) => call.path.endsWith("/start")).length, 1);

})().catch((error) => { console.error(error); process.exitCode = 1; });
