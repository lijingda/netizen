let labelsAnswer = async () => ({ items: [] });
const previousFetch = fetch;
fetch = async (path, options = {}) => {
  const url = new URL(path, "https://admin");
  if (url.pathname !== "/api/v1/chat-labels") return previousFetch(path, options);
  requests.push({ path: url.pathname, query: url.searchParams, options });
  return { ok: true, status: 200, json: async () => labelsAnswer() };
};
// SHIPPED_ADMIN_PAGE
const kinds = [
  { kind: "session", tab: "ordinary", name: "sessions", identity: "bindingId", base: ordinary, path: "/api/v1/sessions" },
  { kind: "side", tab: "temporary", name: "sides", identity: "sideId", base: side, path: "/api/v1/side-topics" },
];
function sample(config, id, properties = {}) {
  const target = { resource: config.kind, [config.identity]: id, turnId: `turn-${id}` };
  return { ...structuredClone(config.base), appId: "app-one", [config.identity]: id,
    actions: { stop: { actionToken: `stop-${id}`, csrfToken: "csrf", target } }, ...properties };
}
function reader(config, answer) {
  if (config.kind === "session") ordinaryAnswer = answer;
  else sideAnswer = answer;
}
async function show(config, items) {
  reader(config, async () => ({ items: structuredClone(items), nextCursor: "second-page" }));
  await selectTab("sessions", config.tab);
  await flush();
}
function body(config) { return document.querySelector(`#${config.name}-body`); }
function rows(config) { return body(config).children.filter((row) => row.dataset[config.identity]); }
function headings(config) { return body(config).querySelectorAll(".session-group-toggle"); }
function titles(config) { return body(config).querySelectorAll(".session-group-title").map((node) => node.textContent); }
function choose(config, value) {
  const layout = document.querySelector(`#${config.kind}-layout`);
  assert(layout.querySelectorAll("option").some((option) => option.value === value), "layout is available in the shipped selector");
  layout.value = value; layout.dispatch("change");
}
function all(config, operation) { document.querySelector(`#${config.kind}-groups-${operation}`).click(); }
function snapshot(config) {
  return { data: state[config.name], page: state[`${config.kind}Page`],
    refreshedAt: state[`${config.kind}RefreshedAt`], requests: requests.length,
    pagination: document.querySelector(`#${config.name}-page`).textContent,
    freshness: document.querySelector("#sessions-refreshed-at").textContent };
}

async function checkIdentity() {
  for (const config of kinds) {
    const items = [
      sample(config, "main", { chatId: "chat-a", chatLabel: "同名群", topicId: null, projectAlias: "z" }),
      sample(config, "other-chat", { chatId: "chat-b", chatLabel: "同名群", projectAlias: "a" }),
      sample(config, "topic", { chatId: "chat-a", chatLabel: "同名群", topicId: "topic-a", sessionType: "topic", projectAlias: "z" }),
      sample(config, "other-app", { appId: "app-two", chatId: "chat-a", chatLabel: "同名群", projectAlias: null }),
      sample(config, "direct-main", { chatId: "chat-direct", chatMode: "p2p", chatLabel: "李京达", topicId: null, projectAlias: "z" }),
      sample(config, "direct-topic", { chatId: "chat-direct", chatMode: "p2p", chatLabel: "李京达", topicId: "direct-topic", projectAlias: "a" }),
      sample(config, "missing-a", { chatId: null, chatLabel: null, chatLabelResolved: false, projectAlias: null }),
      sample(config, "missing-b", { chatId: null, chatLabel: null, chatLabelResolved: false, projectAlias: null }),
    ];
    await show(config, items);
    const original = rows(config);
    const stop = original[2].querySelectorAll("button").find((button) => button.textContent === "停止");
    const saved = snapshot(config);
    choose(config, "chat");
    assert.deepEqual(snapshot(config), saved, "grouping is presentation-only");
    assert.equal(headings(config).length, 6, "chat identity, app identity and missing IDs remain distinct");
    assert.deepEqual(rows(config).map((row) => row.dataset[config.identity]),
      ["main", "topic", "other-chat", "other-app", "direct-main", "direct-topic", "missing-a", "missing-b"]);
    assert.deepEqual(titles(config).slice(0, 4), ["同名群", "同名群", "同名群", "李京达"]);
    const counts = body(config).querySelectorAll(".session-group-count");
    assert.match(counts[0].textContent, /本页\s*2\s*条/);
    assert.match(counts[3].textContent, /本页\s*2\s*条/);
    const first = headings(config)[0];
    assert.equal(first.tagName, "button", "native buttons provide keyboard activation");
    assert.equal(first.type, "button");
    assert.equal(first.getAttribute("aria-expanded"), "true");
    first.click();
    assert.equal(first.getAttribute("aria-expanded"), "false");
    assert.deepEqual(original.map((row) => row.hidden), [true, false, true, false, false, false, false, false]);
    first.click();
    choose(config, "project");
    assert.deepEqual(titles(config), ["z", "a", "项目不可用"]);
    all(config, "collapse");
    assert(rows(config).every((row) => row.hidden));
    choose(config, "compact");
    assert.deepEqual(body(config).children, original, "compact restores original row objects and server order");
    assert(original.every((row) => !row.hidden));
    assert.deepEqual(snapshot(config), saved, "collapse and layout changes retain pagination and freshness");
    assert(original[2].contains(stop), "layout preserves the existing exact-action button");
    stop.click(); await flush();
    const action = requests.findLast((request) => request.path === `${config.path}/stop`);
    assert.equal(action.options.method, "POST");
    assert.deepEqual(JSON.parse(action.options.body), items[2].actions.stop);
  }
}

async function checkCollapse() {
  for (const config of kinds) {
    const items = [sample(config, "a", { chatId: "chat-a", projectAlias: "shared" }),
      sample(config, "b", { chatId: "chat-b", projectAlias: "other" })];
    await show(config, items);
    choose(config, "chat");
    assert(headings(config).every((button) => button.getAttribute("aria-expanded") === "true"),
      "ordinary and Side do not share collapse state");
    headings(config)[0].click();
    choose(config, "project");
    assert(rows(config).every((row) => !row.hidden), "project and chat collapse independently");
    headings(config)[1].click();
    choose(config, "chat");
    assert.deepEqual(rows(config).map((row) => row.hidden), [true, false]);
    all(config, "collapse");
    await refresh("sessions"); await flush();
    assert(rows(config).every((row) => row.hidden), "refresh retains collapsed groups");
    if (config.kind === "session") assert.equal(!document.querySelector("#sessions-body").querySelector(".list-empty"), true);
    assert.equal(headings(config).length, 2, "a fully collapsed page still has its group headings");

    const pageTwo = [items[0], sample(config, "c", { chatId: "chat-c", projectAlias: "new" })];
    reader(config, async () => ({ items: structuredClone(
      requests.findLast((request) => request.path === config.path).query.has("cursor") ? pageTwo : items), nextCursor: "second-page" }));
    await moveSessionPage("next", config.kind); await flush();
    assert.equal(state[`${config.kind}Page`].number, 2);
    assert.deepEqual(rows(config).map((row) => row.hidden), [true, false], "known groups retain state; new groups default open");
    const saved = snapshot(config);
    all(config, "expand");
    assert.deepEqual(snapshot(config), saved, "expand all sends no request");
    await moveSessionPage("previous", config.kind); await flush();
    assert.deepEqual(rows(config).map((row) => row.hidden), [false, true], "expand all affects only current-page groups");
    choose(config, "project");
    assert.deepEqual(rows(config).map((row) => row.hidden), [false, true]);
    choose(config, "chat");
    await selectTab("projects");
    await selectTab("sessions", config.tab); await flush();
    assert.deepEqual(rows(config).map((row) => row.hidden), [false, true], "Tab navigation retains collapse choices");
    reader(config, async () => ({ items: [], nextCursor: null }));
    await refresh("sessions"); await flush();
    assert.equal(headings(config).length, 0, "empty results leave no stale group headings");
    assert(document.querySelector(`#${config.kind}-groups-expand`).disabled);
    assert(document.querySelector(`#${config.kind}-groups-collapse`).disabled);
    const empty = snapshot(config);
    all(config, "expand"); all(config, "collapse");
    assert.deepEqual(snapshot(config), empty);
  }
  const config = kinds[0];
  await show(config, [sample(config, "navigation", { chatId: "chat-navigation" })]);
  choose(config, "chat");
  assert.equal(headings(config).length, 1);
  reader(config, async () => { throw new Error("new project unavailable"); });
  await openProjectSessions("docs"); await flush();
  assert.equal(state.sessions, null);
  assert.equal(body(config).children.length, 0, "failed navigation does not retain old rows or group headings");
  assert(document.querySelector("#session-groups-expand").disabled);
  assert(document.querySelector("#session-groups-collapse").disabled);
}

async function checkLabels() {
  for (const config of kinds) {
    const longId = "oc_unresolved_chat_123456789";
    const items = [
      sample(config, "first", { chatId: "chat-shared", chatLabel: null, chatLabelResolved: false }),
      sample(config, "known", { chatId: "chat-shared", chatLabel: "现成的群名", topicId: "topic-known" }),
      sample(config, "slow", { chatId: longId, chatLabel: null, chatLabelResolved: false }),
    ];
    const pending = deferred(); labelsAnswer = () => pending.promise;
    await show(config, items);
    choose(config, "chat");
    assert.equal(titles(config)[0], "现成的群名", "any resolved row in the same chat supplies its group name");
    assert.match(headings(config)[1].textContent, /群聊/);
    assert(titles(config)[1].includes(longId.slice(0, 12)));
    const heading = headings(config)[1];
    heading.focus(); heading.click();
    const saved = snapshot(config);
    pending.resolve({ items: [{ chatId: longId, chatLabel: "<b>补齐群名</b>", chatLabelResolved: true }] });
    await flush();
    assert.equal(headings(config)[1], heading, "name enrichment updates the existing button");
    assert.equal(document.activeElement, heading, "enrichment retains keyboard focus");
    assert.equal(heading.getAttribute("aria-expanded"), "false");
    assert.equal(rows(config).find((row) => row.dataset[config.identity] === "slow").hidden, true);
    assert.equal(titles(config)[1], "<b>补齐群名</b>");
    assert.equal(heading.querySelector("b"), null, "chat names remain inert text");
    assert.deepEqual(snapshot(config), saved, "enrichment leaves exact row data envelope, pagination and freshness in place");

    const failure = deferred(); labelsAnswer = () => failure.promise;
    await refresh("sessions"); await flush();
    const status = statusNode.textContent, failedSnapshot = snapshot(config);
    failure.reject(new Error("chat service unavailable")); await flush();
    assert.match(headings(config)[1].textContent, /群聊/);
    assert(titles(config)[1].includes(longId.slice(0, 12)));
    assert(!titles(config)[1].includes("加载"));
    assert.equal(headings(config)[1].getAttribute("aria-expanded"), "false");
    assert.equal(statusNode.textContent, status);
    assert.deepEqual(snapshot(config), failedSnapshot);
  }
  const config = kinds[1];
  const old = deferred(); labelsAnswer = () => old.promise;
  await refresh("sessions"); await flush();
  const heading = headings(config)[1];
  const before = rows(config).map((row) => row.hidden);
  expireAdminSession();
  const count = requests.length;
  assert(heading.disabled);
  assert(document.querySelector("#side-groups-expand").disabled);
  heading.click(); all(config, "expand"); all(config, "collapse"); choose(config, "compact");
  old.resolve({ items: [{ chatId: "oc_unresolved_chat_123456789", chatLabel: "expired name", chatLabelResolved: true }] });
  await flush();
  assert.deepEqual(rows(config).map((row) => row.hidden), before, "expired controls cannot change groups");
  assert(!body(config).textContent.includes("expired name"));
  assert.equal(requests.length, count);
}

(async () => {
  await flush();
  await ({ identity: checkIdentity, collapse: checkCollapse, labels: checkLabels })[scenario]();
})().catch((error) => { console.error(error); process.exitCode = 1; });
