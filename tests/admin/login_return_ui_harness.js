document.querySelector("#login-nonce").value = "browser-nonce";
const returnTarget = "/sessions/temporary?project=R%26D&sortDirection=asc";
document.querySelector("#login-next").value = returnTarget;
let navigation = null;
window.location = { assign(path) { navigation = path; } };
async function fetch(path) {
  assert.equal(path, "/api/v1/login/state");
  return { ok: true, json: async () => ({ state: "authenticated" }) };
}
// SHIPPED_IDENTITY_CONTROLLER
(async () => {
  for (let index = 0; index < 30; index += 1) await Promise.resolve();
  assert.equal(navigation, returnTarget);
  assert.equal(document.querySelector("#login-retry").href, `/login?next=${encodeURIComponent(returnTarget)}`);
  assert.equal(document.querySelector("#login-start").disabled, true);
})().catch((error) => { console.error(error); process.exitCode = 1; });
