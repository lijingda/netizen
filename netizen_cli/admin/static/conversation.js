"use strict";

window.NetizenConversation = (() => {
  let dependencies;
  let conversationView = null;
  let conversationRequest = 0;
  const answerNode = (name) => document.querySelector(`#session-answer-${name}`);

  function releaseConversationImages(view) {
    if (!view) return;
    for (const url of view.imageUrls.values()) URL.revokeObjectURL(url);
    view.imageUrls.clear();
  }

  function installConversationImages(view, container, turnId, images, imageUrls) {
    const slots = new Map();
    for (const node of container.querySelectorAll("[data-answer-image]")) {
      const id = node.dataset.answerImage;
      if (!slots.has(id)) slots.set(id, []);
      slots.get(id).push(node);
    }
    const unavailable = (node) => node.replaceWith(document.createTextNode(
      `〔图片：${node.getAttribute("alt") || "图片"}；图片预览不可用〕`,
    ));
    for (const [id, nodes] of slots) {
      const image = Array.isArray(images) ? images.find((item) => item?.id === id) : null;
      try {
        if (image?.mime !== "image/png" || typeof image.data !== "string") throw new Error("Invalid answer image");
        const decoded = atob(image.data);
        const bytes = new Uint8Array(decoded.length);
        for (let index = 0; index < decoded.length; index += 1) bytes[index] = decoded.charCodeAt(index);
        const url = URL.createObjectURL(new Blob([bytes], { type: image.mime }));
        const key = `${turnId}:${id}`;
        imageUrls.set(key, url);
        for (const node of nodes) {
          node.addEventListener("error", () => {
            if (conversationView === view && view.imageUrls.get(key) === url) unavailable(node);
          }, { once: true });
          node.width = image.width;
          node.height = image.height;
          node.src = url;
        }
      } catch (_error) {
        for (const node of nodes) unavailable(node);
      }
    }
  }

  function closeConversation() {
    conversationRequest += 1;
    const previous = conversationView;
    conversationView = null;
    releaseConversationImages(previous);
    document.body.classList.toggle("session-answer-open", false);
    answerNode("content").replaceChildren();
    answerNode("metadata").textContent = "";
    if (answerNode("drawer").open) answerNode("drawer").close();
    if (!dependencies.isExpired() && previous?.returnFocus?.isConnected !== false) previous?.returnFocus?.focus();
  }

  async function openConversation(session) {
    if (dependencies.isExpired() || !session.canReadLatestAnswer) return;
    releaseConversationImages(conversationView);
    conversationView = { session, returnFocus: document.activeElement, preview: true,
      turns: [], cursor: null, blocked: false, pending: false, imageUrls: new Map() };
    answerNode("title").textContent = "对话记录";
    answerNode("identity").textContent = `${session.nativeTitle || "未命名会话"} · ${session.shortId}`;
    answerNode("content").replaceChildren();
    answerNode("toggle").hidden = true;
    answerNode("earlier").hidden = true;
    answerNode("preview-note").textContent = "";
    answerNode("details").open = false;
    answerNode("metadata").textContent = "";
    document.body.classList.toggle("session-answer-open", true);
    if (!answerNode("drawer").open) answerNode("drawer").showModal();
    await refreshConversation();
  }

  function renderConversation(view) {
    for (const turn of view.turns) {
      turn.preview.hidden = !turn.hasPreview || !view.preview;
      turn.source.hidden = turn.hasPreview && view.preview;
    }
    answerNode("toggle").hidden = !view.turns.some((turn) => turn.hasPreview);
    answerNode("toggle").textContent = view.preview ? "查看原文" : "排版预览";
    answerNode("toggle").setAttribute("aria-pressed", String(!view.preview));
    answerNode("earlier").hidden = !view.cursor;
    answerNode("earlier").disabled = view.pending || view.blocked;
  }

  function captureConversationPosition(view, toLatest = false) {
    const scroll = answerNode("scroll");
    const request = conversationRequest;
    const bottom = toLatest || scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop <= 1;
    const top = scroll.getBoundingClientRect().top;
    const anchor = view.turns.find((turn) => turn.node.getBoundingClientRect().bottom > top)?.node;
    const offset = anchor ? anchor.getBoundingClientRect().top - top : 0;
    return () => {
      if (conversationView !== view || conversationRequest !== request) return;
      if (bottom) scroll.scrollTop = scroll.scrollHeight;
      else if (anchor) scroll.scrollTop += anchor.getBoundingClientRect().top - scroll.getBoundingClientRect().top - offset;
    };
  }

  function conversationTurn(view, result, imageUrls) {
    const node = document.createElement("article");
    node.className = "conversation-turn";
    node.dataset.turnId = result.turnId;
    node.tabIndex = -1;
    const status = { completed: "已完成", interrupted: "已停止", failed: "失败", inProgress: "运行中" }[result.status] || "状态未知";
    const metadata = document.createElement("p");
    metadata.className = "conversation-turn-meta";
    const labels = [status, `轮次 ${result.turnId.slice(0, 12)}`];
    if (result.startedAt != null) labels.push(`开始 ${dependencies.nativeSessionDateLabel(result.startedAt)}`);
    if (result.completedAt != null) labels.push(`完成 ${dependencies.nativeSessionDateLabel(result.completedAt)}`);
    metadata.textContent = labels.join(" · ");
    node.append(metadata);
    for (const input of result.inputs || []) {
      const block = document.createElement("section");
      block.className = "conversation-input";
      const label = document.createElement("p");
      label.className = "conversation-input-label";
      label.textContent = ["用户输入", input.source, input.display_name,
        input.raw ? "格式未识别，显示原文" : null,
        input.quoted_count ? `引用 ${input.quoted_count} 条` : null,
        input.supplemental_count ? `补充上下文 ${input.supplemental_count} 条` : null,
      ].filter(Boolean).join(" · ");
      const text = document.createElement("pre");
      text.className = "session-answer-source";
      text.textContent = input.text;
      block.append(label, text);
      if (input.attachments?.length) {
        const files = document.createElement("p");
        files.textContent = `附件：${input.attachments.join("、")}`;
        block.append(files);
      }
      node.append(block);
    }
    const answer = document.createElement("section");
    answer.className = "conversation-answer";
    const label = document.createElement("p");
    label.textContent = result.status === "inProgress" ? "本轮运行中" : "最终回答";
    answer.append(label);
    const preview = document.createElement("div");
    preview.className = "markdown-preview";
    const source = document.createElement("pre");
    source.className = "session-answer-source";
    source.textContent = result.answer || (result.oversized ? "该轮内容过大，Admin 不显示" : "暂无最终回答。");
    const hasPreview = typeof result.markdownHtml === "string";
    window.NetizenContentPreview.renderMarkdown(preview, hasPreview ? result.markdownHtml : null);
    installConversationImages(view, preview, result.turnId, result.images, imageUrls);
    if (result.status !== "inProgress") answer.append(preview, source);
    if (result.error || result.previewMessage) {
      const note = document.createElement("p");
      note.className = "conversation-turn-meta";
      note.textContent = [result.error, result.previewMessage].filter(Boolean).join(" · ");
      answer.append(note);
    }
    node.append(answer);
    return { node, preview, source, hasPreview };
  }

  async function refreshConversation() { return loadConversation(false); }
  async function loadEarlierConversation() { return loadConversation(true); }

  async function loadConversation(earlier) {
    const view = conversationView;
    if (!view || dependencies.isExpired() || (earlier && (view.pending || view.blocked || !view.cursor))) return;
    const request = ++conversationRequest;
    const isCurrent = () => conversationView === view && request === conversationRequest && !dependencies.isExpired();
    view.pending = true;
    renderConversation(view);
    answerNode("refresh").disabled = true;
    answerNode("drawer").setAttribute("aria-busy", "true");
    answerNode("note").textContent = earlier ? "正在加载更早的对话…" : "正在读取对话记录…";
    answerNode("note").classList.toggle("error", false);
    try {
      const query = new URLSearchParams({ bindingId: view.session.bindingId, threadId: view.session.nativeThreadId });
      if (earlier) query.set("cursor", view.cursor);
      const result = await dependencies.api(`/api/v1/sessions/conversation?${query}`);
      if (!isCurrent()) return;
      const position = captureConversationPosition(view, !earlier);
      const existing = new Set(earlier ? view.turns.map((turn) => turn.node.dataset.turnId) : []);
      const staged = { imageUrls: new Map() };
      const added = [];
      try {
        for (const turn of [...result.turns].reverse()) {
          if (existing.has(turn.turnId)) continue;
          added.push(conversationTurn(view, turn, staged.imageUrls));
          existing.add(turn.turnId);
        }
      } catch (error) {
        releaseConversationImages(staged);
        throw error;
      }
      if (earlier) {
        answerNode("content").prepend(...added.map((turn) => turn.node));
        view.turns = [...added, ...view.turns];
      } else {
        answerNode("content").replaceChildren(...added.map((turn) => turn.node));
        releaseConversationImages(view);
        view.turns = added;
      }
      for (const [key, url] of staged.imageUrls) view.imageUrls.set(key, url);
      view.cursor = result.nextCursor;
      view.blocked = false;
      renderConversation(view);
      answerNode("note").textContent = view.turns.length ? `已加载 ${view.turns.length} 轮` : "会话尚无执行轮次。";
      answerNode("preview-note").textContent = [result.pageMessage, result.previewMessage].filter(Boolean).join(" · ");
      answerNode("metadata").textContent = `读取于 ${dependencies.sessionDateLabel(result.readAt)}`;
      if (earlier && answerNode("earlier").hidden) added[0]?.node.focus({ preventScroll: true });
      position();
      requestAnimationFrame(position);
    } catch (error) {
      if (!isCurrent()) return;
      if (["invalid_cursor", "stale_or_conflict", "not_found"].includes(error.code)) view.blocked = true;
      answerNode("note").textContent = `${error.message}${view.turns.length ? " 当前仍显示上次读取的快照。" : ""}`;
      answerNode("note").classList.toggle("error", true);
    } finally {
      if (isCurrent()) {
        view.pending = false;
        renderConversation(view);
        if (earlier && !answerNode("earlier").hidden) answerNode("earlier").focus({ preventScroll: true });
        answerNode("refresh").disabled = false;
        answerNode("drawer").setAttribute("aria-busy", "false");
      }
    }
  }

  function init(options) {
    dependencies = options;
    answerNode("close").addEventListener("click", closeConversation);
    answerNode("drawer").addEventListener("cancel", (event) => { event.preventDefault(); closeConversation(); });
    answerNode("refresh").addEventListener("click", refreshConversation);
    answerNode("earlier").addEventListener("click", loadEarlierConversation);
    answerNode("content").addEventListener("click", (event) => {
      const anchor = event.target.closest?.("a[data-answer-file-root]");
      if (!anchor) return;
      event.preventDefault();
      if (dependencies.isExpired()) return;
      anchor.focus(); // Safari does not focus links on click; returnFocus needs it.
      dependencies.openPathFiles({ root: anchor.dataset.answerFileRoot,
        file: anchor.dataset.answerFileName || null, line: Number(anchor.dataset.answerFileLine) || null });
    });
    answerNode("toggle").addEventListener("click", () => {
      if (!conversationView) return;
      const position = captureConversationPosition(conversationView);
      conversationView.preview = !conversationView.preview;
      renderConversation(conversationView);
      position();
      requestAnimationFrame(position);
    });
  }

  return { init, open: openConversation, close: closeConversation,
    leave: closeConversation, expire: closeConversation, pageHide: closeConversation };
})();
