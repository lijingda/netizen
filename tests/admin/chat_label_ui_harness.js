let chatLabelAnswer;
const fetchList = fetch;
fetch = async (path, options = {}) => {
  const url = new URL(path, "https://admin");
  if (url.pathname !== "/api/v1/chat-labels") return fetchList(path, options);
  requests.push({ path: url.pathname, query: url.searchParams, options });
  const data = await chatLabelAnswer(url.searchParams);
  return { ok: true, status: 200, json: async () => data };
};
const labelReads = () => requests.filter((request) => request.path === "/api/v1/chat-labels");
const resolvedLabel = (chatId, name = "补齐的名称", chatMode = "p2p") => ({
  chatId, chatLabel: name, chatLabelResolved: true, chatMode, chatType: chatMode,
  chatOpenUrl: `https://applink.feishu.cn/chat/${chatId}`,
});
const labelResponse = (...items) => ({ requestId: "names", items });
// SHIPPED_ADMIN_PAGE
(async () => {
  await flush();
  ordinary.actions = { stop: grant("ordinary-stop") };
  for (const [view, kind, key] of [["ordinary", "session", "sessions"], ["temporary", "side", "sides"]]) {
    const base = kind === "side" ? side : ordinary;
    const pending = { ...base, chatLabel: base.chatId, chatLabelResolved: false, chatMode: null };
    const duplicate = { ...pending, bindingId: "binding-second", sideId: "side-second" };
    const cached = { ...base, bindingId: "binding-cached", sideId: "side-cached", chatId: "oc_cached", chatLabel: "缓存命中" };
    const read = async () => ({ items: structuredClone([pending, duplicate, cached]), nextCursor: "next-page" });
    if (kind === "side") sideAnswer = read;
    else ordinaryAnswer = read;
    const slow = deferred();
    chatLabelAnswer = () => slow.promise;
    const before = labelReads().length;
    assert.equal(await selectTab("sessions", view), true, "list must finish while name response is still pending");
    const body = document.querySelector(`#${key}-body`);
    const rows = [...body.children];
    const link = rows[0].querySelector(".chat-link");
    const locationCell = rows[0].querySelector(".session-location");
    assert.equal(rows.length, 3);
    assert(rows[0].textContent.includes("名称加载中"));
    assert.equal(locationCell.getAttribute("aria-busy"), "true");
    assert.equal(rows[2].querySelector(".chat-link").textContent, "缓存命中");
    assert.equal(labelReads().length, before + 1);
    assert.equal(labelReads().at(-1).query.get("chatIds"), base.chatId, "deduplicate and omit resolved labels");
    const action = rows[0].querySelector(".actions").querySelector("button");
    const details = rows[0].querySelector("details");
    details.open = true;
    const input = document.querySelector(`#${kind}-chat-filter`);
    input.value = "oc_unsaved_filter";
    action.focus();
    const page = state[`${kind}Page`];
    const facts = state[key];
    const runtime = facts.items[0].runtime;
    const grants = facts.items[0].actions;
    const refreshedAt = state[`${kind}RefreshedAt`];
    const freshness = document.querySelector("#sessions-refreshed-at").textContent;
    const totalBefore = requests.length;
    await advanceTime(60000);
    assert.equal(requests.length, totalBefore, "pending names must not poll or retry");
    assert.equal(statusNode.textContent, "已更新。");
    slow.resolve(labelResponse(resolvedLabel(base.chatId, "<img src=x> 用户", "p2p")));
    await flush();
    assert.equal(body.children[0], rows[0]);
    assert.equal(rows[0].querySelector(".chat-link"), link, "patch the existing link rather than replace it");
    assert.equal(link.textContent, "<img src=x> 用户");
    assert.equal(link.querySelector("img"), null, "names must be rendered as plain text");
    assert.equal(link.href, base.topicOpenUrl || `https://applink.feishu.cn/chat/${base.chatId}`);
    assert.equal(rows[1].querySelector(".chat-link").textContent, link.textContent);
    assert.equal(locationCell.querySelector(".badge").textContent, "单聊");
    assert.equal(locationCell.getAttribute("aria-busy"), "false");
    assert.equal(rows[0].querySelector(".actions").querySelector("button"), action);
    assert.equal(document.activeElement, action);
    assert.equal(details.open, true);
    assert.equal(input.value, "oc_unsaved_filter");
    assert.equal(state[key], facts);
    assert.equal(facts.items[0].runtime, runtime);
    assert.equal(facts.items[0].actions, grants);
    assert.equal(state[`${kind}Page`], page);
    assert.equal(state[`${kind}RefreshedAt`], refreshedAt);
    assert.equal(document.querySelector("#sessions-refreshed-at").textContent, freshness);
    assert.equal(statusNode.textContent, "已更新。");

    // Failures only fall back in the cells; the next explicit refresh retries.
    chatLabelAnswer = async () => { throw new Error("names unavailable"); };
    await refresh("sessions"); await flush();
    assert(!body.textContent.includes("名称加载中"));
    assert.equal(statusNode.textContent, "已更新。");
    const afterFailure = requests.length;
    await advanceTime(60000);
    assert.equal(requests.length, afterFailure);
    chatLabelAnswer = async () => labelResponse(resolvedLabel(base.chatId, "手动刷新成功"));
    await refresh("sessions"); await flush();
    assert.equal(body.children[0].querySelector(".chat-link").textContent, "手动刷新成功");
    for (const result of [labelResponse(), labelResponse({ ...resolvedLabel(base.chatId), chatLabelResolved: false })]) {
      chatLabelAnswer = async () => result;
      await refresh("sessions"); await flush();
      const cell = body.children[0].querySelector(".session-location");
      assert(!cell.textContent.includes("名称加载中"));
      assert.equal(cell.querySelector(".chat-link").href, base.topicOpenUrl || state[key].items[0].chatOpenUrl);
      assert.equal(statusNode.textContent, "已更新。");
    }

    // Failed replacement reads retain old facts, but must clear invalidated
    // loading placeholders and ignore their eventual name response.
    const obsolete = deferred();
    chatLabelAnswer = () => obsolete.promise;
    await refresh("sessions");
    assert(body.textContent.includes("名称加载中"));
    const retained = state[key];
    const failedRead = async () => { throw new Error("replacement list failed"); };
    if (kind === "side") sideAnswer = failedRead;
    else ordinaryAnswer = failedRead;
    assert.equal(await refresh("sessions"), false);
    assert.equal(state[key], retained);
    assert(!body.textContent.includes("名称加载中"));
    const retainedText = body.textContent;
    const failureStatus = statusNode.textContent;
    obsolete.resolve(labelResponse(resolvedLabel(base.chatId, "过时名称")));
    await flush();
    assert.equal(body.textContent, retainedText);
    assert.equal(statusNode.textContent, failureStatus);

    // Cache-only and empty pages never launch an enrichment request.
    for (const items of [[cached], []]) {
      const cachedRead = async () => ({ items: structuredClone(items), nextCursor: null });
      if (kind === "side") sideAnswer = cachedRead;
      else ordinaryAnswer = cachedRead;
      const prior = labelReads().length;
      await refresh("sessions");
      assert.equal(labelReads().length, prior);
    }
    input.value = "";
  }

  // Ordinary topic sessions keep their exact topic link just like Side rows.
  const topic = { ...ordinary, chatLabelResolved: false, topicId: "omt_ordinary",
    topicOpenUrl: "https://applink.feishu.cn/topic/omt_ordinary", sessionType: "topic", scopeKind: "topic" };
  ordinaryAnswer = async () => ({ items: [structuredClone(topic)], nextCursor: null });
  chatLabelAnswer = async () => labelResponse(resolvedLabel(topic.chatId, "普通话题所在群", "group"));
  await selectTab("sessions", "ordinary"); await flush();
  assert.equal(document.querySelector("#sessions-body").querySelector(".chat-link").href, topic.topicOpenUrl);

  // All navigation paths invalidate the previous page's enrichment. Use the
  // same chat identity on new pages so stale writes cannot hide behind IDs.
  const pendingOrdinary = { ...ordinary, chatLabelResolved: false };
  ordinaryAnswer = async () => ({ items: [structuredClone(pendingOrdinary)], nextCursor: "page-2" });
  sideAnswer = async () => ({ items: [structuredClone(side)], nextCursor: null });
  for (const transition of ["page", "sort", "tab", "navigation"]) {
    const old = deferred();
    chatLabelAnswer = () => old.promise;
    await selectTab("sessions", "ordinary");
    const previousRow = document.querySelector("#sessions-body").children[0];
    assert(previousRow.textContent.includes("名称加载中"));
    const current = deferred();
    chatLabelAnswer = () => current.promise;
    if (transition === "page") await moveSessionPage("next");
    if (transition === "sort") await sortSessionPage("session", "createdAt");
    if (transition === "tab") await selectSessionTab("temporary");
    if (transition === "navigation") await selectTab("projects");
    const currentFacts = state.sessions;
    const currentPage = state.sessionPage;
    const currentStatus = statusNode.textContent;
    const currentText = document.querySelector("#sessions-body").textContent;
    assert(!previousRow.textContent.includes("名称加载中"));
    old.resolve(labelResponse(resolvedLabel(ordinary.chatId, "不应出现的旧名称")));
    await flush();
    assert.equal(state.sessions, currentFacts);
    assert.equal(state.sessionPage, currentPage);
    assert.equal(document.querySelector("#sessions-body").textContent, currentText);
    assert.equal(statusNode.textContent, currentStatus);
    if (["page", "sort"].includes(transition)) {
      current.resolve(labelResponse(resolvedLabel(ordinary.chatId, "当前页名称")));
      await flush();
      assert.equal(document.querySelector("#sessions-body").querySelector(".chat-link").textContent, "当前页名称");
    }
  }

  // Expired authentication keeps its global notice and never accepts late names.
  const expired = deferred();
  chatLabelAnswer = () => expired.promise;
  await selectTab("sessions", "ordinary");
  expireAdminSession();
  const frozen = document.querySelector("#sessions-body").textContent;
  assert(!frozen.includes("名称加载中"));
  expired.resolve(labelResponse(resolvedLabel(ordinary.chatId, "登录过期后的名称")));
  await flush();
  assert.equal(document.querySelector("#sessions-body").textContent, frozen);
  assert.equal(statusNode.textContent, "登录已过期，请重新登录。");
})().catch((error) => { console.error(error); process.exitCode = 1; });
