const state = { tab: "sessions", sessions: null, sides: null };
let now = 0;
const performance = { now: () => now };
let poll;
let interval;
function setInterval(callback, delay) { poll = callback; interval = delay; }
let requests = [];
let errors = [];
let answer;
function setStatus(message) { errors.push(message); }
async function api(path) {
  const query = new URL(path, "https://admin").searchParams;
  const request = {
    bindings: query.get("bindingIds")?.split(",") || [],
    sides: query.get("sideIds")?.split(",") || [],
    resolve: query.get("resolvePrimary") === "true",
    at: now,
  };
  requests.push(request);
  return answer(request);
}
for (const selector of ["#sessions-body", "#sides-body"]) {
  const body = document.querySelector(selector);
  Object.defineProperty(body, "rows", { get() { return this.children; } });
}
function runtime(bindingId, resolution = "unavailable", primaryStatus = null, activityRevision = 1) {
  return { bindingId, activityRevision, primaryStatus, primaryStatusResolution: resolution, subscriptionState: null };
}
function session(bindingId, catalogState = "active", resolution = "unavailable", primaryStatus = null) {
  return { bindingId, catalogState, runtime: runtime(bindingId, resolution, primaryStatus) };
}
function payload(bindings) { return { bindings, sides: [], missingSideIds: [] }; }
function mount(items) {
  state.tab = "sessions";
  state.sessions = { items };
  document.hidden = false;
  const body = document.querySelector("#sessions-body");
  body.replaceChildren();
  for (const item of items) {
    const row = document.createElement("tr");
    row.dataset.bindingId = item.bindingId;
    const cell = document.createElement("td");
    cell.className = "runtime-state";
    updateRuntimeCell(cell, item.runtime);
    row.append(cell);
    body.append(row);
  }
}
function cell(bindingId) {
  return rowByIdentity("#sessions-body", "bindingId", bindingId).querySelector(".runtime-state");
}
function pending() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function flush() { for (let i = 0; i < 12; i += 1) await Promise.resolve(); }
async function tick(at) { now = at; await poll(); }
// SHIPPED_RUNTIME_CONTROLLER
(async () => {
  assert.equal(interval, 5000);

  // Archived rows have no runtime claim, subscription display or network work.
  const archived = session("archived", "archived", "archived");
  archived.runtime.subscriptionState = "unloaded";
  mount([archived]);
  assert.equal(cell("archived").textContent, "—");
  assert.equal(cell("archived").querySelector(".runtime-primary").title, "已归档，不查询运行态");
  await tick(5000);
  await tick(10000);
  assert.equal(requests.length, 0);

  // A catalog-confirmed archive does not mask an unresolved local lifecycle.
  mount([session("archiving", "archived", "local", "lifecycle-unknown")]);
  answer = ({ bindings }) => payload(bindings.map((id) => runtime(id, "local", "lifecycle-unknown")));
  await tick(15000);
  assert.equal(cell("archiving").textContent, "lifecycle-unknown");
  answer = ({ bindings }) => payload(bindings.map((id) => runtime(id, "deferred", null, 2)));
  await tick(20000);
  assert.equal(cell("archiving").textContent, "—");
  assert(requests.every((request) => !request.resolve));
  const afterArchive = requests.length;
  await tick(25000);
  assert.equal(requests.length, afterArchive);

  // Unavailable -> background retry -> unavailable must not replace the DOM
  // node or flash a loading label. Overlapping timer ticks issue no requests.
  requests = [];
  const unavailable = session("retry");
  mount([unavailable]);
  const originalNode = cell("retry").querySelector(".runtime-primary");
  const blocked = pending();
  answer = ({ bindings, resolve }) => resolve ? blocked.promise
    : payload(bindings.map((id) => runtime(id, "deferred")));
  now = 5000;
  const firstPoll = poll();
  await flush();
  assert.equal(requests.length, 2);
  assert.equal(cell("retry").textContent, "状态暂不可用");
  assert.equal(cell("retry").querySelector(".runtime-primary"), originalNode);
  await poll();
  assert.equal(requests.length, 2);
  blocked.resolve(payload([runtime("retry")]));
  await firstPoll;
  assert.equal(cell("retry").querySelector(".runtime-primary"), originalNode);

  // Native failures back off 10/20/40/60 seconds, bounded per Session and
  // revision. The lightweight local observation continues every five seconds.
  answer = ({ bindings, resolve }) => payload(bindings.map((id) => runtime(id, resolve ? "unavailable" : "deferred")));
  for (let at = 10000; at <= 195000; at += 5000) await tick(at);
  assert.deepEqual(requests.filter((request) => request.resolve).map((request) => request.at),
    [5000, 15000, 35000, 75000, 135000, 195000]);
  assert.equal(requests.filter((request) => !request.resolve).length, 39);
  assert.equal(cell("retry").querySelector(".runtime-primary"), originalNode);

  // A new revision bypasses the old cooldown. Failure must not commit it;
  // recovery commits it, and unchanged resolved rows need no more native reads.
  answer = ({ bindings, resolve }) => payload(bindings.map((id) => runtime(id, resolve ? "unavailable" : "deferred", null, 2)));
  await tick(200000);
  assert.equal(requests.at(-1).resolve, true);
  assert.equal(unavailable.runtime.activityRevision, 1);
  answer = ({ bindings, resolve }) => payload(bindings.map((id) => runtime(id, resolve ? "resolved" : "deferred", resolve ? "idle" : null, 2)));
  await tick(205000);
  assert.equal(requests.at(-1).resolve, false);
  await tick(210000);
  assert.equal(cell("retry").textContent, "idle");
  assert.equal(unavailable.runtime.activityRevision, 2);
  const recoveredNode = cell("retry").querySelector(".runtime-primary");
  await tick(215000);
  assert.equal(requests.at(-1).resolve, false);
  assert.equal(cell("retry").querySelector(".runtime-primary"), recoveredNode);

  // Changed facts mark an old value unconfirmed; failed retries keep that
  // value and its annotation stable, then fresh local activity clears backoff.
  requests = [];
  answer = ({ bindings, resolve }) => payload(bindings.map((id) => runtime(id, resolve ? "unavailable" : "deferred", null, 3)));
  await tick(220000);
  assert.equal(cell("retry").textContent, "idle（待确认）");
  const staleNode = cell("retry").querySelector(".runtime-primary");
  await tick(225000);
  assert.equal(cell("retry").querySelector(".runtime-primary"), staleNode);
  answer = ({ bindings }) => payload(bindings.map((id) => runtime(id, "local", "running", 4)));
  await tick(230000);
  assert.equal(cell("retry").textContent, "running");
  answer = ({ bindings, resolve }) => payload(bindings.map((id) => runtime(id, resolve ? "resolved" : "deferred", resolve ? "idle" : null, 5)));
  await tick(235000);
  assert.equal(cell("retry").textContent, "idle");

  // HTTP failure of the follow-up uses the same retry policy. A manual page
  // refresh gets fresh Session objects, so it is not blocked by the old timer.
  requests = [];
  errors = [];
  mount([session("http")]);
  answer = ({ bindings, resolve }) => {
    if (resolve) throw new Error("temporary read failure");
    return payload(bindings.map((id) => runtime(id, "deferred")));
  };
  await tick(5000);
  await tick(10000);
  assert.equal(requests.filter((request) => request.resolve).length, 1);
  assert.deepEqual(errors, ["temporary read failure"]);
  assert.equal(cell("http").textContent, "状态暂不可用");
  mount([session("http")]);
  await tick(10000);
  assert.equal(requests.filter((request) => request.resolve).length, 2);

  // Restoring an archived row makes it eligible again. Unknown catalog state
  // never suppresses observation or resolution. Mixed pages omit only archives.
  requests = [];
  mount([session("archived", "active"), session("unknown", "unknown"), session("skip", "archived", "archived")]);
  answer = ({ bindings, resolve }) => payload(bindings.map((id) => runtime(id, resolve ? "resolved" : "deferred", resolve ? "idle" : null)));
  await tick(5000);
  assert.deepEqual(requests.map((request) => request.bindings), [["archived", "unknown"], ["archived", "unknown"]]);
  assert.equal(cell("archived").textContent, "idle");
  assert.equal(cell("unknown").textContent, "idle");
  assert.equal(cell("skip").textContent, "—");

  // IDs remain bounded to 50 per request on a large mixed page.
  requests = [];
  mount([session("skip", "archived", "archived"), ...Array.from({ length: 99 }, (_, i) => session(`row-${i}`))]);
  answer = ({ bindings }) => payload(bindings.map((id) => runtime(id, "local", "running")));
  await tick(5000);
  assert.deepEqual(requests.map((request) => request.bindings.length), [50, 49]);
  assert(requests.every((request) => !request.bindings.includes("skip")));

  // Hidden pages and other Admin panels do not poll. Hiding or replacing the
  // page during a request also prevents obsolete responses/follow-up reads.
  requests = [];
  mount([session("visibility")]);
  document.hidden = true;
  await tick(5000);
  assert.equal(requests.length, 0);
  document.hidden = false;
  state.tab = "projects";
  await tick(10000);
  assert.equal(requests.length, 0);
  state.tab = "sessions";
  const pendingLocal = pending();
  answer = () => pendingLocal.promise;
  const hiddenPoll = poll();
  document.hidden = true;
  pendingLocal.resolve(payload([runtime("visibility", "deferred")]));
  await hiddenPoll;
  assert.equal(requests.length, 1);
  document.hidden = false;
  const pendingResolved = pending();
  answer = ({ bindings, resolve }) => resolve ? pendingResolved.promise
    : payload(bindings.map((id) => runtime(id, "deferred")));
  const oldPagePoll = poll();
  await flush();
  mount([session("visibility", "archived", "archived")]);
  pendingResolved.resolve(payload([runtime("visibility", "resolved", "idle")]));
  await oldPagePoll;
  assert.equal(cell("visibility").textContent, "—");

  // Side polls share the in-flight guard, including navigation while pending.
  requests = [];
  state.tab = "side-topics";
  state.sides = { items: [{ sideId: "side-1", runtime: null }] };
  const pendingSide = pending();
  answer = () => pendingSide.promise;
  const sidePoll = poll();
  await poll();
  assert.equal(requests.length, 1);
  mount([session("after-side")]);
  await poll();
  assert.equal(requests.length, 1);
  pendingSide.resolve({ bindings: [], sides: [], missingSideIds: ["side-1"] });
  await sidePoll;
  assert.equal(state.sides.items[0].runtime, null);
})().catch((error) => { console.error(error); process.exitCode = 1; });
