let latestAnswer = () => ({ threadId: "native", turnId: "turn-current", status: "completed",
  answer: "<script>inert</script>\nfinal", startedAt: null, completedAt: null, readAt: "2026-11-03T08:00:00+00:00" });
let labelsAnswer = async () => ({ items: [] });
const priorFetch = fetch;
fetch = async (path, options = {}) => {
  const url = new URL(path, "https://admin");
  if (!["/api/v1/sessions/latest-answer", "/api/v1/chat-labels"].includes(url.pathname)) return priorFetch(path, options);
  requests.push({ path: url.pathname, query: url.searchParams, options });
  const data = await (url.pathname.endsWith("latest-answer") ? latestAnswer() : labelsAnswer());
  return { ok: true, status: 200, json: async () => data };
};
// SHIPPED_ADMIN_PAGE
(async () => {
  await flush();
  state.sessionPage = firstSessionPage("searchTerm=old&project=old&identity=old&chatId=old&inventoryState=archived&updatedRange=all&pageSize=100&sortDirection=asc");
  await openProjectSessions("docs"); await flush();
  let query = requests.findLast((item) => item.path === "/api/v1/sessions").query;
  assert.deepEqual(query.getAll("project"), ["docs"]);
  for (const key of ["searchTerm", "identity", "chatId", "cursor"]) assert(!query.has(key));
  assert.equal(query.get("inventoryState"), "active");
  assert.equal(query.get("pageSize"), defaultSessionQuery().get("pageSize"));
  assert.equal(query.get("sortDirection"), "desc");
  assert(query.has("updatedFrom"), "Project navigation shares normal initial time defaults");
  assert.equal(document.querySelector("#session-search").value, "");

  const search = document.querySelector("#session-search");
  const beforeTyping = requests.length;
  search.value = " SDK "; search.dispatch("input");
  await flush(); assert.equal(requests.length, beforeTyping, "typing does not query");
  state.sessionPage.cursor = "old-cursor";
  document.querySelector("#session-filter").dispatch("submit"); await flush();
  query = requests.findLast((item) => item.path === "/api/v1/sessions").query;
  assert.equal(query.get("searchTerm"), "SDK");
  assert(!query.has("cursor"));
  assert.equal(new URLSearchParams(window.location.search).get("searchTerm"), "SDK");
  const previous = state.sessionPage, previousRows = state.sessions, previousUrl = window.location.search;
  ordinaryAnswer = async () => { throw new Error("native search unavailable"); };
  search.value = "other"; document.querySelector("#session-filter").dispatch("submit"); await flush();
  assert.equal(state.sessionPage, previous);
  assert.equal(state.sessions, previousRows);
  assert.equal(window.location.search, previousUrl);
  assert.match(statusNode.textContent, /上次成功/);
  ordinaryAnswer = async () => ({ items: [], nextCursor: null });
  await refresh("sessions");
  assert.equal(document.querySelector("#sessions-empty").hidden, false);
  ordinaryAnswer = async () => { throw new Error("new project unavailable"); };
  await openProjectSessions("netizen");
  assert.equal(state.sessions, null);
  assert.equal(document.querySelector("#sessions-empty").hidden, true, "failed navigation cannot claim no matches");
  ordinaryAnswer = null;

  // Group only the current page, retaining existing rows/actions and pending label targets.
  for (const [kind, tab, name, base, identity] of [
    ["session", "ordinary", "sessions", ordinary, "bindingId"],
    ["side", "temporary", "sides", side, "sideId"],
  ]) {
    const items = ["z", "a", "z", null].map((projectAlias, index) => ({ ...structuredClone(base),
      [identity]: `${kind}-${index}`, projectAlias, chatId: `chat-${index}`, chatLabel: `chat-${index}`,
      chatLabelResolved: false }));
    const read = async () => ({ items: structuredClone(items), nextCursor: "next" });
    if (kind === "session") ordinaryAnswer = read; else sideAnswer = read;
    const gate = deferred(); labelsAnswer = () => gate.promise;
    await selectTab("sessions", tab);
    const body = document.querySelector(`#${name}-body`);
    const rows = [...body.children];
    const facts = state[name], page = state[`${kind}Page`], freshness = state[`${kind}RefreshedAt`];
    const count = requests.length;
    const control = document.querySelector(`#${kind}-layout`);
    control.value = "project"; control.dispatch("change"); await flush();
    assert.equal(requests.length, count);
    assert.equal(state[name], facts); assert.equal(state[`${kind}Page`], page);
    assert.equal(state[`${kind}RefreshedAt`], freshness);
    assert.deepEqual(body.children.filter((row) => row.className === "session-project-group").map((row) => row.textContent), ["z", "a", "项目不可用"]);
    assert.deepEqual(body.children.filter((row) => row.dataset[identity]).map((row) => row.dataset[identity]),
      [`${kind}-0`, `${kind}-2`, `${kind}-1`, `${kind}-3`]);
    gate.resolve({ items: items.map((item, index) => ({ chatId: item.chatId, chatLabel: `resolved-${index}`, chatLabelResolved: true })) });
    await flush();
    for (const [index, row] of rows.entries()) assert.equal(row.querySelector(".chat-link").textContent, `resolved-${index}`);
    control.value = "compact"; control.dispatch("change");
    assert.deepEqual(body.children, rows);
    assert.equal(requests.length, count);
  }

  ordinaryAnswer = null; labelsAnswer = async () => ({ items: [] });
  await selectTab("sessions", "ordinary");
  const archived = { ...ordinary, nativeThreadId: "native", canReadLatestAnswer: true, catalogState: "archived" };
  state.sessions = { items: [archived] }; renderSessions(state.sessions);
  const readButton = document.querySelector("#sessions-body").querySelectorAll("button").find((button) => button.textContent === "查看最近回答");
  assert(readButton, "archived materialized sessions remain readable");
  const beforeAnswer = requests.length;
  readButton.click(); await flush();
  assert.equal(requests.length, beforeAnswer + 1);
  const answerRequest = requests.at(-1);
  assert.equal(answerRequest.path, "/api/v1/sessions/latest-answer");
  assert.equal(answerRequest.query.get("bindingId"), archived.bindingId);
  assert.equal(answerRequest.query.get("threadId"), "native");
  assert.equal(answerRequest.options.method, undefined, "answer is a read-only GET");
  assert.equal(answerNode("content").textContent, "<script>inert</script>\nfinal");
  assert.equal(answerNode("content").querySelector("script"), null);
  assert(!answerNode("metadata").textContent.includes("开始"));
  const afterAnswer = requests.length;
  await advanceTime(60000); assert.equal(requests.length, afterAnswer, "no answer polling");
  latestAnswer = () => ({ turnId: "newest", status: "inProgress", answer: null, readAt: "2026-11-03T09:00:00Z" });
  await refreshLatestAnswer();
  assert.equal(answerNode("content").hidden, true);
  assert.match(answerNode("note").textContent, /运行中.*暂无最终回答/);
  latestAnswer = async () => { throw new Error("history unavailable"); };
  await refreshLatestAnswer();
  assert.match(answerNode("note").textContent, /读取的快照/);
  const slow = deferred(); latestAnswer = () => slow.promise;
  const pending = refreshLatestAnswer(); closeLatestAnswer();
  slow.resolve({ turnId: "stale", status: "completed", answer: "must not appear" }); await pending;
  assert.equal(answerNode("drawer").open, false);
  assert(!answerNode("content").textContent.includes("must not appear"));
  state.sessions = { items: [{ ...ordinary, canReadLatestAnswer: false }] }; renderSessions(state.sessions);
  assert(!document.querySelector("#sessions-body").querySelectorAll("button").some((button) => button.textContent === "查看最近回答"));
})().catch((error) => { console.error(error); process.exitCode = 1; });
