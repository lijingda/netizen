let latestAnswer = () => ({ threadId: "native", turnId: "turn-current", status: "completed",
  answer: "<script>inert</script>\nfinal", markdownHtml: '<p>&lt;script&gt;inert&lt;/script&gt;\nfinal</p><a href="#" data-answer-file-root="/tmp" data-answer-file-name="a.py" data-answer-file-line="12">file</a><a class="answer-file-link-unavailable" title="not available">blocked</a>',
  startedAt: null, completedAt: null, readAt: "2026-11-03T08:00:00+00:00" });
const pageOf = (turns, nextCursor = null, threadId = "native") => ({
  threadId, turns, nextCursor, readAt: "2026-11-12T08:00:00Z",
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
const openedAnswerFiles = [];
const pageHideListeners = [];
const addPageListener = window.addEventListener;
window.addEventListener = (name, listener, ...options) => {
  if (name === "pagehide") pageHideListeners.push(listener);
  return addPageListener.call(window, name, listener, ...options);
};
// Parse trusted image slots and answer anchors from the shipped rendering sink.
Object.defineProperty(Element.prototype, "innerHTML", {
  get() { return this.html || ""; },
  set(value) {
    this.html = value;
    this.replaceChildren();
    for (const match of value.matchAll(/<(img|a)\b([^>]*)>/g)) {
      const attrs = Object.fromEntries([...match[2].matchAll(/([\w-]+)="([^"]*)"/g)]
        .map((attribute) => [attribute[1], attribute[2]]));
      const node = new Element(match[1], attrs);
      node.closest = (selector) => selector === "a[data-answer-file-root]" && node.dataset.answerFileRoot ? node : null;
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
scroll.clientHeight = 60;
const turnHeight = (node) => node.querySelector(".markdown-preview")?.hidden === false ? 140 : 100;
Object.defineProperty(scroll, "scrollHeight", { get() {
  return document.querySelector("#session-answer-content").children.reduce((total, node) => total + turnHeight(node), 0);
} });
let scrollTop = 0;
Object.defineProperty(scroll, "scrollTop", {
  get() { return Math.min(scrollTop, Math.max(0, scroll.scrollHeight - scroll.clientHeight)); },
  set(value) { scrollTop = Math.max(0, Math.min(value, scroll.scrollHeight - scroll.clientHeight)); },
});
const atBottom = () => scroll.scrollTop === scroll.scrollHeight - scroll.clientHeight;
const normalRect = Element.prototype.getBoundingClientRect;
Element.prototype.getBoundingClientRect = function () {
  if (this.className === "conversation-turn") {
    const siblings = this.parentElement.children;
    const top = siblings.slice(0, siblings.indexOf(this)).reduce((sum, node) => sum + turnHeight(node), 0) - scroll.scrollTop + 20;
    return { top, bottom: top + turnHeight(this) };
  }
  return normalRect.call(this);
};
const imageAnswer = (id = "image-0") => ({
  turnId: "image-turn", status: "completed", answer: "![小猫](/tmp/cat.png)", readAt: "now",
  markdownHtml: `<p><img data-answer-image="${id}" alt="小猫" loading="lazy" decoding="async"></p>`,
  images: [{ id, mime: "image/png", data: "aW1hZ2U=", width: 400, height: 200 }],
});
Object.defineProperty(Element.prototype, "src", {
  get() { return this.imageSrc; },
  set(value) {
    if (this.dataset.answerImage) {
      assert.equal(this.width, 400, "native image width reserves space before src is installed");
      assert.equal(this.height, 200, "native image height reserves space before src is installed");
    }
    this.imageSrc = value;
  },
});
const priorFetch = fetch;
fetch = async (path, options = {}) => {
  const url = new URL(path, "https://admin");
  if (!["/api/v1/sessions/conversation", "/api/v1/chat-labels"].includes(url.pathname)) return priorFetch(path, options);
  requests.push({ path: url.pathname, query: url.searchParams, options });
  const data = await (url.pathname.endsWith("conversation") ? conversationAnswer() : labelsAnswer());
  return { ok: true, status: 200, json: async () => data };
};
const conversationNode = (name) => document.querySelector(`#session-answer-${name}`);
async function clickConversationControl(name) {
  conversationNode(name).click();
  await flush();
}
// SHIPPED_ADMIN_PAGE
// Install the public file-opening spy after the actual page modules have loaded.
window.NetizenPathFiles.open = (target) => { openedAnswerFiles.push(target); };
(async () => {
  await flush();
  sessionLists.session.restore(new URLSearchParams("searchTerm=old&project=old&identity=old&chatId=old&inventoryState=archived&updatedRange=all&pageSize=100&sortDirection=asc"));
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
  await sessionLists.session.load({ ...sessionLists.session.snapshot().page, cursor: "old-cursor" });
  document.querySelector("#session-filter").dispatch("submit"); await flush();
  query = requests.findLast((item) => item.path === "/api/v1/sessions").query;
  assert.equal(query.get("searchTerm"), "SDK");
  assert(!query.has("cursor"));
  assert.equal(new URLSearchParams(window.location.search).get("searchTerm"), "SDK");
  const previous = sessionLists.session.snapshot().page, previousRows = sessionLists.session.snapshot().items, previousUrl = window.location.search;
  ordinaryAnswer = async () => { throw new Error("native search unavailable"); };
  search.value = "other"; document.querySelector("#session-filter").dispatch("submit"); await flush();
  assert.deepEqual(sessionLists.session.snapshot().page, previous);
  assert.deepEqual(sessionLists.session.snapshot().items, previousRows);
  assert.equal(window.location.search, previousUrl);
  assert.match(document.querySelector(state.sessionTab === "temporary" ? "#sides-error" : "#sessions-error").textContent, /上次成功/);
  ordinaryAnswer = async () => ({ items: [], nextCursor: null });
  await refresh("sessions");
  assert(document.querySelector("#sessions-body").querySelector(".list-empty"));
  ordinaryAnswer = async () => { throw new Error("new project unavailable"); };
  await openProjectSessions("netizen");
  assert.deepEqual(sessionLists.session.snapshot().items, []);
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
    const facts = sessionLists[kind].snapshot().items, page = sessionLists[kind].snapshot().page, freshness = sessionLists[kind].snapshot().refreshedAt;
    const count = requests.length;
    const control = document.querySelector(`#${kind}-layout`);
    control.value = "project"; control.dispatch("change"); await flush();
    assert.equal(requests.length, count);
    assert.deepEqual(sessionLists[kind].snapshot().items, facts); assert.deepEqual(sessionLists[kind].snapshot().page, page);
    assert.equal(sessionLists[kind].snapshot().refreshedAt, freshness);
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
  const otherSession = { ...archived, bindingId: "other", nativeThreadId: "other" };
  ordinaryAnswer = async () => ({ items: [archived, otherSession], nextCursor: null });
  document.querySelectorAll("[data-refresh]").find((button) => button.dataset.refresh === "sessions").click();
  await flush();
  const readButton = document.querySelector("#sessions-body").querySelectorAll("button").find((button) => button.textContent === "查看对话记录");
  assert(readButton, "archived materialized sessions remain readable");
  const beforeAnswer = requests.length;
  const returnFocus = clickConversationMenu(archived.bindingId); await flush();
  assert.equal(requests.length, beforeAnswer + 1);
  const answerRequest = requests.at(-1);
  assert.equal(answerRequest.path, "/api/v1/sessions/conversation");
  assert.equal(answerRequest.query.get("bindingId"), archived.bindingId);
  assert.equal(answerRequest.query.get("threadId"), "native");
  assert.equal(answerRequest.options.method, undefined, "answer is a read-only GET");
  assert.equal(conversationNode("title").textContent, "对话记录");
  assert.equal(conversationNode("close").getAttribute("aria-label"), "关闭对话记录");
  const turns = () => conversationNode("content").querySelectorAll(".conversation-turn");
  const turn = (node) => ({ node, preview: node.querySelector(".markdown-preview"),
    source: node.querySelector(".conversation-answer").querySelector("pre") });
  const current = () => turn(turns().at(-1));
  assert.equal(current().source.textContent, "<script>inert</script>\nfinal");
  assert.equal(current().preview.innerHTML, latestAnswer().markdownHtml);
  assert.equal(current().preview.hidden, false);
  assert.equal(current().source.hidden, true);
  assert.equal(current().preview.querySelector("script"), null);
  assert(!current().node.textContent.includes("开始"));
  assert(atBottom(), "opening scrolls to latest");
  const fileLink = current().preview.querySelector("a");
  assert(fileLink.dispatch("click").prevented);
  assert.deepEqual(openedAnswerFiles, [{ root: "/tmp", file: "a.py", line: 12 }]);
  assert.equal(document.activeElement, fileLink, "click explicitly focuses the anchor before opening the viewer");
  current().preview.querySelector(".answer-file-link-unavailable").click();
  assert.equal(openedAnswerFiles.length, 1);
  const afterAnswer = requests.length;
  conversationNode("toggle").click();
  assert.equal(current().preview.hidden, true);
  assert.equal(current().source.hidden, false);
  assert(atBottom(), "switching to source preserves bottom position after reflow");
  conversationNode("toggle").click();
  assert(atBottom(), "switching to preview preserves bottom position after reflow");
  assert.equal(requests.length, afterAnswer, "toggle is local");

  // Click only; disable concurrent loads and preserve the first visible Turn.
  scroll.scrollTop = 25;
  const anchor = current().node;
  const beforeTop = anchor.getBoundingClientRect().top;
  const older = deferred(); conversationAnswer = () => older.promise;
  conversationNode("earlier").click(); await flush();
  assert(conversationNode("earlier").disabled);
  const loadingCount = requests.length;
  conversationNode("earlier").click(); await flush();
  assert.equal(requests.length, loadingCount);
  assert.equal(requests.at(-1).query.get("cursor"), "older");
  assert(!requests.at(-1).query.has("limit"));
  older.resolve(pageOf([{ turnId: "older-2", status: "failed", error: "failed input", inputs: [{ text: "request", source: "飞书消息" }] },
    { turnId: "older-1", status: "interrupted", answer: "earlier answer", markdownHtml: '<p>earlier answer</p><a href="#" data-answer-file-root="/older">directory</a>' }], "oldest"));
  await flush();
  assert.deepEqual(turns().map((node) => node.dataset.turnId), ["older-1", "older-2", "turn-current"]);
  assert.equal(anchor.getBoundingClientRect().top, beforeTop, "prepend preserves the visible anchor");
  assert(conversationNode("content").textContent.includes("request"));
  assert(conversationNode("content").textContent.includes("failed input"));
  assert.equal(document.activeElement, conversationNode("earlier"), "visible earlier button retains focus");
  const olderFileLink = turn(turns()[0]).preview.querySelector("a");
  olderFileLink.click();
  assert.deepEqual(openedAnswerFiles.at(-1), { root: "/older", file: null, line: null });
  assert.equal(document.activeElement, olderFileLink, "delegation also covers appended earlier turns");
  scroll.scrollTop = 150;
  const middle = turns()[1];
  const middleTop = middle.getBoundingClientRect().top;
  conversationNode("toggle").click(); await flush();
  assert.equal(middle.getBoundingClientRect().top, middleTop, "source reflow preserves the visible turn offset");
  conversationNode("toggle").click(); await flush();
  assert.equal(middle.getBoundingClientRect().top, middleTop, "preview reflow preserves the visible turn offset");
  conversationAnswer = () => { throw new Error("earlier failed"); };
  await clickConversationControl("earlier");
  assert.equal(turns().length, 3);
  assert.equal(requests.at(-1).query.get("cursor"), "oldest");
  assert.match(conversationNode("note").textContent, /上次读取的快照/);
  conversationAnswer = () => { const error = new Error("游标失效，请刷新"); error.code = "invalid_cursor"; throw error; };
  await clickConversationControl("earlier");
  assert.equal(requests.at(-1).query.get("cursor"), "oldest", "a failed page keeps its cursor for retry");
  assert(conversationNode("earlier").disabled);
  const failedCount = requests.length;
  await clickConversationControl("earlier"); assert.equal(requests.length, failedCount);
  conversationAnswer = () => pageOf([latestAnswer()], "reset");
  await clickConversationControl("refresh");
  assert.equal(turns().length, 1);
  assert.equal(conversationNode("earlier").hidden, false);
  assert.equal(requests.at(-1).query.has("cursor"), false);
  assert(!requests.at(-1).query.has("limit"));
  assert(atBottom());
  conversationAnswer = () => ({ ...pageOf([{ turnId: "one-large", status: "completed", oversized: true }], "one-next"),
    pageMessage: "PAGE FALLBACK" });
  await clickConversationControl("earlier");
  assert.equal(requests.at(-1).query.get("cursor"), "reset");
  assert.equal(conversationNode("preview-note").textContent, "PAGE FALLBACK");
  assert(turn(turns()[0]).source.textContent);
  assert.equal(turns()[0].querySelector(".conversation-answer").children.length, 3, "oversized explanation is not repeated as a preview note");
  conversationAnswer = () => pageOf([{ turnId: "earliest", status: "completed", answer: "old" }]);
  await clickConversationControl("earlier");
  assert(!requests.at(-1).query.has("limit"));
  assert.equal(conversationNode("preview-note").textContent, "", "large-page warning only describes its response");
  assert(conversationNode("earlier").hidden);
  assert.equal(document.activeElement, turns()[0], "exhausted cursor moves focus to the earliest inserted turn");
  assert.equal(document.activeElement.tabIndex, -1);

  const repeated = imageAnswer();
  repeated.markdownHtml += repeated.markdownHtml;
  repeated.images.push({ id: "unused", mime: "image/png", data: "aW1hZ2U=" });
  conversationAnswer = () => pageOf([repeated], "image-cursor");
  await clickConversationControl("refresh");
  let images = current().preview.querySelectorAll("img");
  assert.equal(images.length, 2);
  assert.equal(answerImageBlobs.length, 1);
  assert.equal(images[0].src, images[1].src);
  assert.equal(await answerImageBlobs[0].text(), "image");
  assert(atBottom(), "reserved image dimensions allow the initial bottom positioning");
  conversationNode("toggle").click(); conversationNode("toggle").click();
  assert.equal(answerImageBlobs.length, 1);
  conversationAnswer = () => { throw new Error("refresh failed"); };
  await clickConversationControl("refresh");
  assert(activeAnswerImageUrls.has(images[0].src));
  assert.match(conversationNode("note").textContent, /读取的快照/);
  const snapshotNodes = [...conversationNode("content").children];
  const snapshotUrls = [...activeAnswerImageUrls];
  const renderMarkdown = window.NetizenContentPreview.renderMarkdown;
  window.NetizenContentPreview.renderMarkdown = (container, html) => {
    if (html === "RENDER FAILURE") throw new Error("fixture rendering failure");
    return renderMarkdown(container, html);
  };
  conversationAnswer = () => pageOf([
    { turnId: "render-failure", status: "completed", answer: "new", markdownHtml: "RENDER FAILURE" },
    { ...imageAnswer(), turnId: "staged-image" },
  ], "uncommitted-cursor");
  await clickConversationControl("refresh");
  window.NetizenContentPreview.renderMarkdown = renderMarkdown;
  assert.deepEqual(conversationNode("content").children, snapshotNodes);
  assert(conversationNode("content").children.every((node, index) => node === snapshotNodes[index]),
    "rendering failure preserves the exact DOM snapshot");
  assert.deepEqual([...activeAnswerImageUrls], snapshotUrls, "only the uncommitted image URLs are released");
  conversationAnswer = () => pageOf([], "image-cursor");
  await clickConversationControl("earlier");
  assert.equal(requests.at(-1).query.get("cursor"), "image-cursor", "rendering failure keeps the old cursor usable");
  conversationAnswer = () => pageOf([{ turnId: "newest", status: "completed", answer: "large source", previewMessage: "已显示原文" }]);
  await clickConversationControl("refresh");
  assert.equal(activeAnswerImageUrls.size, 0);
  assert.equal(current().source.textContent, "large source");
  assert.equal(current().source.hidden, false);
  assert.equal(conversationNode("toggle").hidden, true);
  const afterRefresh = requests.length;
  await advanceTime(60000); assert.equal(requests.length, afterRefresh, "no history polling");
  conversationAnswer = () => pageOf([{ turnId: "running", status: "inProgress", inputs: [{ text: "正在处理的输入" }] }]);
  await clickConversationControl("refresh");
  assert.match(current().node.textContent, /运行中/);
  assert.match(current().node.textContent, /正在处理的输入/);
  assert.equal(current().node.querySelector(".conversation-answer").children.length, 1, "running answer area contains only its status");
  assert.equal(current().node.querySelector(".conversation-answer").querySelector("pre"), null);
  conversationAnswer = () => pageOf([{ turnId: "running", status: "inProgress" }]);
  await clickConversationControl("refresh");
  assert.match(current().node.textContent, /运行中/);
  assert.equal(current().node.querySelector(".conversation-input"), null, "missing native input is never invented");

  const input = { text: "$authored <tag>\n🌙", source: "SOURCE", display_name: "Alice", attachments: ["one.txt", "二.png"],
    quoted_count: 2, supplemental_count: 3 };
  conversationAnswer = () => pageOf([{ turnId: "metadata", status: "completed", answer: "",
    inputs: [input, { ...input, raw: true }] }]);
  await clickConversationControl("refresh");
  const inputBlocks = current().node.querySelectorAll(".conversation-input");
  assert.equal(inputBlocks.length, 2);
  for (const block of inputBlocks) {
    assert.equal(block.querySelector("pre").textContent, input.text);
    const label = block.querySelector(".conversation-input-label").textContent;
    for (const value of [input.source, input.display_name, "2", "3"]) assert(label.includes(value));
    for (const name of input.attachments) assert(block.children.at(-1).textContent.includes(name));
  }
  assert(inputBlocks[1].querySelector(".conversation-input-label").textContent.length >
    inputBlocks[0].querySelector(".conversation-input-label").textContent.length, "raw input has an additional visible annotation");
  const noAnswer = current().source.textContent;
  assert(noAnswer, "empty answer has the no-answer presentation");
  conversationAnswer = () => ({ ...pageOf([{ turnId: "no-answer", status: "completed", answer: null }]), previewMessage: "RAW PREVIEW" });
  await clickConversationControl("refresh");
  assert.equal(current().source.textContent, noAnswer);
  assert.equal(conversationNode("preview-note").textContent, "RAW PREVIEW");
  assert(!current().node.textContent.includes("RAW PREVIEW"), "page preview notice is not repeated within turns");

  for (const imageData of [[], [{ id: "image-0", mime: "image/png", data: "!invalid!" }],
    [{ id: "image-0", mime: "image/svg+xml", data: "aW1hZ2U=" }]]) {
    conversationAnswer = () => pageOf([{ ...imageAnswer(), images: imageData }]);
    await clickConversationControl("refresh");
    assert.equal(current().preview.querySelectorAll("img").length, 0);
    assert.match(current().preview.textContent, /小猫.*图片预览不可用/);
    assert.equal(activeAnswerImageUrls.size, 0);
  }
  conversationAnswer = () => pageOf([{ ...imageAnswer(),
    markdownHtml: imageAnswer().markdownHtml + imageAnswer("image-1").markdownHtml,
    images: [{ id: "image-0", mime: "image/png", data: "!invalid!" }, ...imageAnswer("image-1").images],
  }]);
  await clickConversationControl("refresh");
  assert.match(current().preview.textContent, /小猫.*图片预览不可用/);
  assert.equal(current().preview.querySelectorAll("img").length, 1, "one malformed image preserves the other images");
  assert.equal(activeAnswerImageUrls.size, 1);
  conversationAnswer = () => pageOf([imageAnswer()]);
  await clickConversationControl("refresh");
  current().preview.querySelector("img").dispatch("error");
  assert.match(current().preview.textContent, /小猫.*图片预览不可用/);
  assert.equal(current().source.textContent, imageAnswer().answer);
  const slow = deferred(); conversationAnswer = () => slow.promise;
  const beforeLate = answerImageBlobs.length;
  conversationNode("refresh").click(); conversationNode("close").click();
  assert.equal(activeAnswerImageUrls.size, 0);
  slow.resolve(pageOf([imageAnswer()])); await flush();
  assert.equal(answerImageBlobs.length, beforeLate);
  assert.equal(conversationNode("drawer").open, false);
  assert.equal(conversationNode("content").textContent, "");

  assert.equal(document.activeElement, returnFocus, "closing restores focus to the row menu trigger");
  ordinaryAnswer = async () => ({ items: [{ ...ordinary, canReadLatestAnswer: false }], nextCursor: null });
  document.querySelectorAll("[data-refresh]").find((button) => button.dataset.refresh === "sessions").click();
  await flush();
  assert(!document.querySelector("#sessions-body").querySelectorAll("button").some((button) => button.textContent === "查看对话记录"));
  ordinaryAnswer = async () => ({ items: [archived, otherSession], nextCursor: null });
  document.querySelectorAll("[data-refresh]").find((button) => button.dataset.refresh === "sessions").click();
  await flush();
  conversationAnswer = () => pageOf([imageAnswer()]);
  clickConversationMenu(archived.bindingId); await flush();
  assert.equal(activeAnswerImageUrls.size, 1);
  const beforeOldSession = answerImageBlobs.length;
  const oldSession = deferred(); conversationAnswer = () => oldSession.promise;
  clickConversationMenu(archived.bindingId);
  assert.equal(activeAnswerImageUrls.size, 0, "opening a session releases the preceding snapshot immediately");
  conversationAnswer = () => pageOf([{ turnId: "other", status: "completed", answer: "new session" }], null, "other");
  clickConversationMenu(otherSession.bindingId); await flush();
  assert.equal(requests.at(-1).query.get("bindingId"), "other");
  assert.equal(requests.at(-1).query.get("threadId"), "other");
  oldSession.resolve(pageOf([imageAnswer()])); await flush();
  assert.equal(answerImageBlobs.length, beforeOldSession);
  assert.equal(current().source.textContent, "new session");
  conversationAnswer = () => pageOf([imageAnswer()]);
  clickConversationMenu(archived.bindingId); await flush();
  const staleRefresh = deferred(); conversationAnswer = () => staleRefresh.promise;
  conversationNode("refresh").click();
  conversationAnswer = () => pageOf([{ ...imageAnswer(), answer: "fresh snapshot" }]);
  await clickConversationControl("refresh");
  const beforeStale = answerImageBlobs.length;
  staleRefresh.resolve(pageOf([imageAnswer()])); await flush();
  assert.equal(answerImageBlobs.length, beforeStale);
  assert.equal(current().source.textContent, "fresh snapshot");
  for (const lifecycle of ["navigation", "pagehide"]) {
    const late = deferred(); conversationAnswer = () => late.promise;
    conversationNode("refresh").click();
    const beforeLeave = answerImageBlobs.length;
    if (lifecycle === "navigation") {
      document.querySelectorAll(".tab").find((tab) => tab.dataset.tab === "projects").click();
      await flush();
    } else {
      assert(pageHideListeners.length, "pagehide is wired by the page");
      for (const listener of pageHideListeners) listener({ type: "pagehide" });
    }
    assert.equal(conversationNode("drawer").open, false, `${lifecycle} closes the drawer`);
    assert.equal(conversationNode("content").textContent, "");
    assert.equal(activeAnswerImageUrls.size, 0, `${lifecycle} releases the displayed images`);
    late.resolve(pageOf([imageAnswer()])); await flush();
    assert.equal(answerImageBlobs.length, beforeLeave, `${lifecycle} invalidates pending reads`);
    assert.equal(conversationNode("content").textContent, "");
    if (lifecycle === "navigation") {
      document.querySelectorAll(".tab").find((tab) => tab.dataset.tab === "sessions" && tab.dataset.sessionTab === "ordinary").click();
      await flush();
    }
    conversationAnswer = () => pageOf([imageAnswer()]);
    clickConversationMenu(archived.bindingId); await flush();
    assert.equal(activeAnswerImageUrls.size, 1, "returning can open a fresh conversation");
  }
  expireAdminSession();
  assert.equal(activeAnswerImageUrls.size, 0);
  assert.equal(revokedAnswerImageUrls.length, answerImageBlobs.length);
  assert.equal(conversationNode("drawer").open, false);
  assert.equal(conversationNode("content").textContent, "");
})().catch((error) => { console.error(error); process.exitCode = 1; });
