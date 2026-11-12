let latestAnswer = () => ({ threadId: "native", turnId: "turn-current", status: "completed",
  answer: "<script>inert</script>\nfinal", markdownHtml: "<p>&lt;script&gt;inert&lt;/script&gt;\nfinal</p>",
  startedAt: null, completedAt: null, readAt: "2026-11-03T08:00:00+00:00" });
const pageOf = (turns, nextCursor = null, limit = 10, threadId = "native") => ({
  threadId, turns, nextCursor, limit, readAt: "2026-11-12T08:00:00Z",
});
let conversationAnswer = () => pageOf([latestAnswer()], "older");
let labelsAnswer = async () => ({ items: [] });
const answerImageBlobs = [];
const activeAnswerImageUrls = new Set();
const revokedAnswerImageUrls = [];
URL.createObjectURL = (blob) => {
  answerImageBlobs.push(blob);
  const url = `blob:answer-${answerImageBlobs.length}`;
  activeAnswerImageUrls.add(url);
  return url;
};
URL.revokeObjectURL = (url) => {
  assert(activeAnswerImageUrls.delete(url), "each allocated image URL is released once");
  revokedAnswerImageUrls.push(url);
};
// Parse only trusted image slots, matching the shipped Markdown rendering sink.
Object.defineProperty(Element.prototype, "innerHTML", {
  get() { return this.html || ""; },
  set(value) {
    this.html = value;
    this.replaceChildren();
    for (const match of value.matchAll(/<img\b([^>]*)>/g)) {
      const attrs = Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)]
        .map((attribute) => [attribute[1], attribute[2]]));
      const node = new Element("img", attrs);
      node.replaceWith = (replacement) => {
        const parent = node.parentElement;
        const index = parent?.children.indexOf(node) ?? -1;
        if (index < 0) return;
        replacement.parentElement = parent;
        parent.children.splice(index, 1, replacement);
        node.parentElement = null;
      };
      this.append(node);
    }
  },
});
Element.prototype.prepend = function (...nodes) {
  const previous = [...this.children];
  this.replaceChildren(...nodes, ...previous);
};
function requestAnimationFrame(callback) { queueMicrotask(callback); }
const scroll = document.querySelector("#session-answer-scroll");
scroll.scrollTop = 0;
Object.defineProperty(scroll, "scrollHeight", { get() { return document.querySelector("#session-answer-content").children.length * 100; } });
const normalRect = Element.prototype.getBoundingClientRect;
Element.prototype.getBoundingClientRect = function () {
  if (this.className === "conversation-turn") {
    const top = this.parentElement.children.indexOf(this) * 100 - scroll.scrollTop + 20;
    return { top, bottom: top + 100 };
  }
  return normalRect.call(this);
};
const imageAnswer = (id = "image-0") => ({
  turnId: "image-turn", status: "completed", answer: "![小猫](/tmp/cat.png)", readAt: "now",
  markdownHtml: `<p><img data-answer-image="${id}" alt="小猫" loading="lazy" decoding="async"></p>`,
  images: [{ id, mime: "image/png", data: "aW1hZ2U=" }],
});
const priorFetch = fetch;
fetch = async (path, options = {}) => {
  const url = new URL(path, "https://admin");
  if (!["/api/v1/sessions/conversation", "/api/v1/chat-labels"].includes(url.pathname)) return priorFetch(path, options);
  requests.push({ path: url.pathname, query: url.searchParams, options });
  const data = await (url.pathname.endsWith("conversation") ? conversationAnswer() : labelsAnswer());
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
  assert.match(document.querySelector(state.sessionTab === "temporary" ? "#sides-error" : "#sessions-error").textContent, /上次成功/);
  ordinaryAnswer = async () => ({ items: [], nextCursor: null });
  await refresh("sessions");
  assert(document.querySelector("#sessions-body").querySelector(".list-empty"));
  ordinaryAnswer = async () => { throw new Error("new project unavailable"); };
  await openProjectSessions("netizen");
  assert.equal(state.sessions, null);
  assert.equal(document.querySelector("#sessions-body").querySelector(".list-empty"), null, "failed navigation cannot claim no matches");
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
    assert.deepEqual(body.querySelectorAll(".session-group-title").map((title) => title.textContent), ["z", "a", "项目不可用"]);
    assert.deepEqual(body.children.filter((row) => row.dataset[identity]).map((row) => row.dataset[identity]),
      [`${kind}-0`, `${kind}-2`, `${kind}-1`, `${kind}-3`]);
    gate.resolve({ items: items.map((item, index) => ({ chatId: item.chatId, chatLabel: `resolved-${index}`, chatLabelResolved: true })) });
    await flush();
    for (const [index, row] of rows.entries()) assert.equal(row.querySelector(".chat-link").textContent, `resolved-${index}`);
    control.value = "compact"; control.dispatch("change");
    assert(body.children.length === rows.length && body.children.every((row, index) => row === rows[index]), "layout retains row identities");
    assert.equal(requests.length, count);
  }

  ordinaryAnswer = null; labelsAnswer = async () => ({ items: [] });
  await selectTab("sessions", "ordinary");
  const archived = { ...ordinary, nativeThreadId: "native", canReadLatestAnswer: true, catalogState: "archived" };
  state.sessions = { items: [archived] }; renderSessions(state.sessions);
  const readButton = document.querySelector("#sessions-body").querySelectorAll("button").find((button) => button.textContent === "查看对话记录");
  assert(readButton, "archived materialized sessions remain readable");
  const beforeAnswer = requests.length;
  readButton.click(); await flush();
  assert.equal(requests.length, beforeAnswer + 1);
  const answerRequest = requests.at(-1);
  assert.equal(answerRequest.path, "/api/v1/sessions/conversation");
  assert.equal(answerRequest.query.get("bindingId"), archived.bindingId);
  assert.equal(answerRequest.query.get("threadId"), "native");
  assert.equal(answerRequest.options.method, undefined, "answer is a read-only GET");
  assert.equal(answerNode("title").textContent, "对话记录");
  assert.equal(answerNode("close").getAttribute("aria-label"), "关闭对话记录");
  const current = () => conversationView.turns.at(-1);
  assert.equal(current().source.textContent, "<script>inert</script>\nfinal");
  assert.equal(current().preview.innerHTML, latestAnswer().markdownHtml);
  assert.equal(current().preview.hidden, false);
  assert.equal(current().source.hidden, true);
  assert.equal(current().preview.querySelector("script"), null);
  assert(!current().node.textContent.includes("开始"));
  assert.equal(scroll.scrollTop, scroll.scrollHeight, "opening scrolls to latest");
  const afterAnswer = requests.length;
  answerNode("toggle").click();
  assert.equal(current().preview.hidden, true);
  assert.equal(current().source.hidden, false);
  answerNode("toggle").click();
  assert.equal(requests.length, afterAnswer, "toggle is local");
  scroll.dispatch("scroll"); await flush();
  assert.equal(requests.length, afterAnswer, "scroll never loads a page");

  // Click only; disable concurrent loads and preserve the first visible Turn.
  scroll.scrollTop = 25;
  const anchor = current().node;
  const beforeTop = anchor.getBoundingClientRect().top;
  const older = deferred(); conversationAnswer = () => older.promise;
  answerNode("earlier").click(); await flush();
  assert(answerNode("earlier").disabled);
  const loadingCount = requests.length;
  answerNode("earlier").click(); await flush();
  assert.equal(requests.length, loadingCount);
  assert.equal(requests.at(-1).query.get("cursor"), "older");
  assert.equal(requests.at(-1).query.get("limit"), "10");
  older.resolve(pageOf([{ turnId: "older-2", status: "failed", error: "failed input", inputs: [{ text: "request", source: "飞书消息" }] },
    { turnId: "older-1", status: "interrupted" }], "oldest"));
  await flush();
  assert.deepEqual(conversationView.turns.map((turn) => turn.node.dataset.turnId), ["older-1", "older-2", "turn-current"]);
  assert.equal(anchor.getBoundingClientRect().top, beforeTop, "prepend preserves the visible anchor");
  assert(answerNode("content").textContent.includes("request"));
  assert(answerNode("content").textContent.includes("failed input"));
  conversationAnswer = () => { throw new Error("earlier failed"); };
  await loadEarlierConversation();
  assert.equal(conversationView.turns.length, 3);
  assert.equal(conversationView.cursor, "oldest");
  assert.match(answerNode("note").textContent, /上次读取的快照/);
  conversationAnswer = () => { const error = new Error("游标失效，请刷新"); error.code = "invalid_cursor"; throw error; };
  await loadEarlierConversation();
  assert(answerNode("earlier").disabled);
  const failedCount = requests.length;
  await loadEarlierConversation(); assert.equal(requests.length, failedCount);
  conversationAnswer = () => pageOf([latestAnswer()], "reset");
  await refreshConversation();
  assert.equal(conversationView.turns.length, 1);
  assert.equal(conversationView.cursor, "reset");
  assert.equal(requests.at(-1).query.has("cursor"), false);
  assert.equal(requests.at(-1).query.get("limit"), "10");
  assert.equal(scroll.scrollTop, scroll.scrollHeight);
  conversationAnswer = () => ({ ...pageOf([{ turnId: "one-large", status: "completed", oversized: true }], "one-next", 1),
    pageMessage: "本页内容较大，已改为逐轮加载" });
  await loadEarlierConversation();
  assert.equal(conversationView.limit, 1);
  assert.match(answerNode("preview-note").textContent, /逐轮加载/);
  assert.match(answerNode("content").textContent, /该轮内容过大/);
  conversationAnswer = () => pageOf([{ turnId: "earliest", status: "completed", answer: "old" }], null, 1);
  await loadEarlierConversation();
  assert.equal(requests.at(-1).query.get("limit"), "1");
  assert(answerNode("earlier").hidden);

  const repeated = imageAnswer();
  repeated.markdownHtml += repeated.markdownHtml;
  repeated.images.push({ id: "unused", mime: "image/png", data: "aW1hZ2U=" });
  conversationAnswer = () => pageOf([repeated]);
  await refreshConversation();
  let images = current().preview.querySelectorAll("img");
  assert.equal(images.length, 2);
  assert.equal(answerImageBlobs.length, 1);
  assert.equal(images[0].src, images[1].src);
  assert.equal(await answerImageBlobs[0].text(), "image");
  assert.equal(conversationView.turns[0].images, undefined, "snapshot does not retain base64");
  answerNode("toggle").click(); answerNode("toggle").click();
  assert.equal(answerImageBlobs.length, 1);
  conversationAnswer = () => { throw new Error("refresh failed"); };
  await refreshConversation();
  assert(activeAnswerImageUrls.has(images[0].src));
  assert.match(answerNode("note").textContent, /读取的快照/);
  conversationAnswer = () => pageOf([{ turnId: "newest", status: "completed", answer: "large source", previewMessage: "已显示原文" }]);
  await refreshConversation();
  assert.equal(activeAnswerImageUrls.size, 0);
  assert.equal(current().source.textContent, "large source");
  assert.equal(current().source.hidden, false);
  assert.equal(answerNode("toggle").hidden, true);
  const afterRefresh = requests.length;
  await advanceTime(60000); assert.equal(requests.length, afterRefresh, "no history polling");
  conversationAnswer = () => pageOf([{ turnId: "running", status: "inProgress" }]);
  await refreshConversation();
  assert.match(current().node.textContent, /运行中/);

  for (const imageData of [[], [{ id: "image-0", mime: "image/png", data: "!invalid!" }],
    [{ id: "image-0", mime: "image/svg+xml", data: "aW1hZ2U=" }]]) {
    conversationAnswer = () => pageOf([{ ...imageAnswer(), images: imageData }]);
    await refreshConversation();
    assert.equal(current().preview.querySelectorAll("img").length, 0);
    assert.match(current().preview.textContent, /小猫.*图片预览不可用/);
    assert.equal(activeAnswerImageUrls.size, 0);
  }
  conversationAnswer = () => pageOf([{ ...imageAnswer(),
    markdownHtml: imageAnswer().markdownHtml + imageAnswer("image-1").markdownHtml,
    images: [{ id: "image-0", mime: "image/png", data: "!invalid!" }, ...imageAnswer("image-1").images],
  }]);
  await refreshConversation();
  assert.match(current().preview.textContent, /小猫.*图片预览不可用/);
  assert.equal(current().preview.querySelectorAll("img").length, 1, "one malformed image preserves the other images");
  assert.equal(activeAnswerImageUrls.size, 1);
  conversationAnswer = () => pageOf([imageAnswer()]);
  await refreshConversation();
  current().preview.querySelector("img").dispatch("error");
  assert.match(current().preview.textContent, /小猫.*图片预览不可用/);
  assert.equal(current().source.textContent, imageAnswer().answer);
  const slow = deferred(); conversationAnswer = () => slow.promise;
  const beforeLate = answerImageBlobs.length;
  const pending = refreshConversation(); closeConversation();
  assert.equal(activeAnswerImageUrls.size, 0);
  slow.resolve(pageOf([imageAnswer()])); await pending;
  assert.equal(answerImageBlobs.length, beforeLate);
  assert.equal(answerNode("drawer").open, false);
  assert.equal(answerNode("content").textContent, "");

  state.sessions = { items: [{ ...ordinary, canReadLatestAnswer: false }] }; renderSessions(state.sessions);
  assert(!document.querySelector("#sessions-body").querySelectorAll("button").some((button) => button.textContent === "查看对话记录"));
  conversationAnswer = () => pageOf([imageAnswer()]);
  await openConversation(archived);
  assert.equal(activeAnswerImageUrls.size, 1);
  const beforeOldSession = answerImageBlobs.length;
  const oldSession = deferred(); conversationAnswer = () => oldSession.promise;
  const opening = openConversation(archived);
  assert.equal(activeAnswerImageUrls.size, 0, "opening a session releases the preceding snapshot immediately");
  conversationAnswer = () => pageOf([{ turnId: "other", status: "completed", answer: "new session" }], null, 10, "other");
  await openConversation({ ...archived, bindingId: "other", nativeThreadId: "other" });
  oldSession.resolve(pageOf([imageAnswer()])); await opening;
  assert.equal(answerImageBlobs.length, beforeOldSession);
  assert.equal(current().source.textContent, "new session");
  conversationAnswer = () => pageOf([imageAnswer()]);
  await openConversation(archived);
  const staleRefresh = deferred(); conversationAnswer = () => staleRefresh.promise;
  const refreshing = refreshConversation();
  conversationAnswer = () => pageOf([{ ...imageAnswer(), answer: "fresh snapshot" }]);
  await refreshConversation();
  const beforeStale = answerImageBlobs.length;
  staleRefresh.resolve(pageOf([imageAnswer()])); await refreshing;
  assert.equal(answerImageBlobs.length, beforeStale);
  assert.equal(current().source.textContent, "fresh snapshot");
  conversationAnswer = () => pageOf([imageAnswer()], "cursor", 10, "wrong");
  await refreshConversation();
  assert(conversationView.blocked);
  assert.equal(current().source.textContent, "fresh snapshot");
  expireAdminSession();
  assert.equal(activeAnswerImageUrls.size, 0);
  assert.equal(revokedAnswerImageUrls.length, answerImageBlobs.length);
  assert.equal(answerNode("drawer").open, false);
  assert.equal(answerNode("content").textContent, "");
})().catch((error) => { console.error(error); process.exitCode = 1; });
