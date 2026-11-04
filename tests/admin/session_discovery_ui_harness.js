let latestAnswer = () => ({ threadId: "native", turnId: "turn-current", status: "completed",
  answer: "<script>inert</script>\nfinal", markdownHtml: "<p>&lt;script&gt;inert&lt;/script&gt;\nfinal</p>",
  startedAt: null, completedAt: null, readAt: "2026-11-03T08:00:00+00:00" });
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
// The small DOM port only needs to recognize the trusted renderer's image slots here.
const answerContent = document.querySelector("#session-answer-content");
Object.defineProperty(answerContent, "innerHTML", {
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
const imageAnswer = (id = "image-0") => ({
  turnId: "image-turn", status: "completed", answer: "![小猫](/tmp/cat.png)", readAt: "now",
  markdownHtml: `<p><img data-answer-image="${id}" alt="小猫" loading="lazy" decoding="async"></p>`,
  images: [{ id, mime: "image/png", data: "aW1hZ2U=" }],
});
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
  assert.equal(answerNode("source").textContent, "<script>inert</script>\nfinal");
  assert.equal(answerNode("content").innerHTML, latestAnswer().markdownHtml);
  assert.equal(answerNode("content").hidden, false);
  assert.equal(answerNode("source").hidden, true);
  assert.equal(answerNode("content").querySelector("script"), null);
  assert(!answerNode("metadata").textContent.includes("开始"));
  const afterAnswer = requests.length;
  answerNode("toggle").click();
  assert.equal(answerNode("content").hidden, true);
  assert.equal(answerNode("source").hidden, false);
  answerNode("toggle").click();
  assert.equal(answerNode("content").hidden, false);
  assert.equal(requests.length, afterAnswer, "switching preview/source reuses the same snapshot");

  const repeatedImage = imageAnswer();
  repeatedImage.markdownHtml += repeatedImage.markdownHtml;
  repeatedImage.images.push({ id: "unused", mime: "image/png", data: "aW1hZ2U=" });
  latestAnswer = () => repeatedImage;
  await refreshLatestAnswer();
  const imageNodes = answerNode("content").querySelectorAll("img");
  assert.equal(imageNodes.length, 2);
  assert.equal(answerImageBlobs.length, 1, "repeated image references share one Blob; unreferenced images are not decoded");
  assert.equal(imageNodes[0].src, imageNodes[1].src);
  assert.equal(answerImageBlobs[0].type, "image/png");
  assert.equal(await answerImageBlobs[0].text(), "image");
  assert.equal(latestAnswerView.result.images, undefined, "snapshot does not retain base64 image data");
  answerNode("toggle").click(); answerNode("toggle").click();
  assert.equal(answerImageBlobs.length, 1, "source/preview toggles retain the image resources");
  latestAnswer = async () => { throw new Error("refresh failed"); };
  await refreshLatestAnswer();
  assert.equal(answerNode("content").hidden, false, "refresh failure retains the rendered snapshot");
  assert.match(answerNode("note").textContent, /读取的快照/);
  assert(activeAnswerImageUrls.has(imageNodes[0].src), "failed refresh preserves the displayed images");
  latestAnswer = () => ({ turnId: "newest", status: "completed", answer: "large source", previewMessage: "已显示原文", readAt: "now" });
  await refreshLatestAnswer();
  assert.equal(activeAnswerImageUrls.size, 0, "successful replacement releases the previous images");
  assert.equal(answerNode("source").textContent, "large source");
  assert.equal(answerNode("source").hidden, false);
  assert.equal(answerNode("toggle").hidden, true);
  assert.equal(answerNode("preview-note").textContent, "已显示原文");
  const afterRefresh = requests.length;
  await advanceTime(60000); assert.equal(requests.length, afterRefresh, "no answer polling");
  latestAnswer = () => ({ turnId: "newest", status: "inProgress", answer: null, readAt: "2026-11-03T09:00:00Z" });
  await refreshLatestAnswer();
  assert.equal(answerNode("content").hidden, true);
  assert.equal(answerNode("source").hidden, true);
  assert.equal(answerNode("toggle").hidden, true);
  assert.match(answerNode("note").textContent, /运行中.*暂无最终回答/);
  latestAnswer = async () => { throw new Error("history unavailable"); };
  await refreshLatestAnswer();
  assert.match(answerNode("note").textContent, /读取的快照/);

  for (const images of [[], [{ id: "image-0", mime: "image/png", data: "!invalid!" }],
    [{ id: "image-0", mime: "image/svg+xml", data: "aW1hZ2U=" }]]) {
    latestAnswer = () => ({ ...imageAnswer(), images });
    await refreshLatestAnswer();
    assert.equal(answerNode("content").querySelectorAll("img").length, 0);
    assert.match(answerNode("content").textContent, /小猫.*图片预览不可用/);
    assert.equal(activeAnswerImageUrls.size, 0);
  }
  latestAnswer = () => ({ ...imageAnswer(),
    markdownHtml: imageAnswer().markdownHtml + imageAnswer("image-1").markdownHtml,
    images: [{ id: "image-0", mime: "image/png", data: "!invalid!" }, ...imageAnswer("image-1").images],
  });
  await refreshLatestAnswer();
  assert.match(answerNode("content").textContent, /小猫.*图片预览不可用/);
  assert.equal(answerNode("content").querySelectorAll("img").length, 1, "one malformed image does not hide the remaining images");
  assert.equal(activeAnswerImageUrls.size, 1);
  latestAnswer = () => imageAnswer();
  await refreshLatestAnswer();
  const failedImage = answerNode("content").querySelector("img");
  failedImage.dispatch("error");
  assert.match(answerNode("content").textContent, /小猫.*图片预览不可用/);
  assert.equal(answerNode("source").textContent, imageAnswer().answer);
  assert.equal(answerNode("toggle").hidden, false, "image decode failure does not disrupt answer text/toggle");

  const slow = deferred(); latestAnswer = () => slow.promise;
  const beforeLateAnswer = answerImageBlobs.length;
  const pending = refreshLatestAnswer(); closeLatestAnswer();
  assert.equal(activeAnswerImageUrls.size, 0, "closing the drawer releases image resources");
  slow.resolve({ ...imageAnswer(), answer: "must not appear" }); await pending;
  assert.equal(answerImageBlobs.length, beforeLateAnswer, "closed drawer ignores late image responses before allocating URLs");
  assert.equal(answerNode("drawer").open, false);
  assert.equal(answerNode("source").textContent, "");
  assert(!answerNode("content").textContent.includes("must not appear"));
  state.sessions = { items: [{ ...ordinary, canReadLatestAnswer: false }] }; renderSessions(state.sessions);
  assert(!document.querySelector("#sessions-body").querySelectorAll("button").some((button) => button.textContent === "查看最近回答"));
  latestAnswer = () => imageAnswer();
  await openLatestAnswer(archived);
  const otherPending = deferred(); latestAnswer = () => otherPending.promise;
  const opening = openLatestAnswer(archived);
  assert.equal(activeAnswerImageUrls.size, 0, "opening a session clears the preceding session's image resources immediately");
  latestAnswer = () => ({ turnId: "other", status: "completed", answer: "new session", markdownHtml: "<p>new session</p>" });
  await openLatestAnswer({ ...archived, bindingId: "other", nativeThreadId: "other" });
  const beforeOldSession = answerImageBlobs.length;
  otherPending.resolve(imageAnswer());
  await opening;
  assert.equal(answerImageBlobs.length, beforeOldSession, "late response from another session cannot allocate or display images");
  assert.equal(answerNode("source").textContent, "new session");
  assert.equal(answerNode("content").innerHTML, "<p>new session</p>");
  latestAnswer = () => imageAnswer();
  await refreshLatestAnswer();
  assert.equal(activeAnswerImageUrls.size, 1);
  const staleRefresh = deferred(); latestAnswer = () => staleRefresh.promise;
  const refreshing = refreshLatestAnswer();
  latestAnswer = () => ({ ...imageAnswer(), answer: "fresh snapshot" });
  await refreshLatestAnswer();
  const beforeStaleRefresh = answerImageBlobs.length;
  staleRefresh.resolve(imageAnswer()); await refreshing;
  assert.equal(answerImageBlobs.length, beforeStaleRefresh, "older refresh cannot allocate images after a newer successful refresh");
  assert.equal(answerNode("source").textContent, "fresh snapshot");
  expireAdminSession();
  assert.equal(activeAnswerImageUrls.size, 0, "authentication expiry releases image resources");
  assert.equal(revokedAnswerImageUrls.length, answerImageBlobs.length);
  assert.equal(answerNode("drawer").open, false);
  assert.equal(answerNode("source").textContent, "");
  assert.equal(answerNode("content").textContent, "");
})().catch((error) => { console.error(error); process.exitCode = 1; });
