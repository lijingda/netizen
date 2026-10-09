const debounceJobs = new Map();
let nextTimer = 0;
function setTimeout(callback) { const id = ++nextTimer; debounceJobs.set(id, callback); return id; }
function clearTimeout(id) { debounceJobs.delete(id); }
function flushDebounce() {
  const jobs = [...debounceJobs.values()];
  debounceJobs.clear();
  for (const job of jobs) job();
}
async function settle() { await Promise.resolve(); await Promise.resolve(); }
// SHIPPED_CHAT_PICKER

let nextPicker = 0;
function setup() {
  const root = document.createElement("div");
  document.body.append(root);
  const requests = [];
  const changes = [];
  const picker = window.createChatPicker(root, {
    id: `picker-${++nextPicker}`, label: "选择群聊",
    fetchPage(args) {
      return new Promise((resolve, reject) => requests.push({ args, resolve, reject }));
    },
    onChange: (item) => changes.push(item),
  });
  const input = root.querySelector("input");
  return { root, picker, requests, changes, input,
    rows: () => root.querySelector(".chat-picker-options").children,
    type(value) { input.value = value; input.dispatch("input"); },
  };
}

async function remoteQueriesIgnoreStaleResults() {
  const f = setup();
  assert.equal(f.requests.length, 0, "initialization must not query");
  f.picker.focus();
  assert.deepEqual(f.requests[0].args, { query: "", cursor: null });
  f.type("old keyword");
  f.type("new keyword");
  f.requests[0].resolve({ items: [{ chatId: "oc_stale", name: "stale" }] });
  await settle();
  assert.equal(f.rows().length, 0, "an earlier query must not overwrite current results");
  flushDebounce();
  assert.equal(f.requests.length, 2, "typing should debounce remote queries");
  assert.deepEqual(f.requests[1].args, { query: "new keyword", cursor: null });
  f.type("latest keyword");
  flushDebounce();
  f.requests[2].resolve({ items: [{ chatId: "oc_latest", name: "latest" }], nextCursor: null });
  await settle();
  f.requests[1].resolve({ items: [{ chatId: "oc_old", name: "old" }], nextCursor: "stale" });
  await settle();
  assert.equal(f.rows().length, 1);
  assert.match(f.rows()[0].textContent, /latest/);
  assert.equal(f.root.querySelector(".chat-picker-more").hidden, true);
  f.picker.close();
}

async function paginationRetryAndPlainText() {
  const f = setup();
  f.picker.focus();
  f.requests[0].resolve({ items: [{ chatId: "oc_first", name: '<img src=x onerror="alert(1)">' }], nextCursor: "page2" });
  await settle();
  assert.equal(f.root.querySelector("img"), null, "server names must be rendered as plain text");
  assert.match(f.rows()[0].textContent, /<img/);
  const more = f.root.querySelector(".chat-picker-more");
  assert.equal(more.hidden, false);
  more.click();
  more.click();
  assert.equal(f.requests.length, 2, "double-click must not duplicate page requests");
  assert.deepEqual(f.requests[1].args, { query: "", cursor: "page2" });
  f.requests[1].reject(new Error("暂时无法连接"));
  await settle();
  assert.equal(f.rows().length, 1, "pagination failures must keep earlier results");
  assert.match(f.root.textContent, /群聊读取失败：暂时无法连接/);
  const retry = f.root.querySelector(".chat-picker-retry");
  assert.equal(retry.hidden, false);
  retry.click();
  assert.deepEqual(f.requests[2].args, { query: "", cursor: "page2" });
  f.requests[2].resolve({ items: [
    { chatId: "oc_first", name: "duplicate" }, { chatId: "oc_second", name: "second" },
  ], nextCursor: "page2", notice: "请缩小搜索范围" });
  await settle();
  assert.equal(f.rows().length, 2, "pagination must deduplicate chat IDs");
  assert.equal(more.hidden, true, "a repeated cursor must not create an endless paging loop");
  assert.match(f.root.textContent, /请缩小搜索范围/);
  f.picker.close();
}

async function selectionKeyboardAndEmptyStates() {
  const f = setup();
  f.picker.setSelection({ chatId: "oc_existing", name: "Existing" });
  assert.equal(f.input.value, "Existing");
  assert.equal(f.requests.length, 0);
  assert.equal(f.changes.length, 0, "programmatic changes must not notify");
  const copy = f.picker.getSelection();
  copy.chatId = "changed";
  assert.equal(f.picker.getSelection().chatId, "oc_existing");
  f.type("replacement");
  assert.equal(f.picker.getSelection(), null, "editing visible text must clear the previous selected ID immediately");
  assert.deepEqual(f.changes, [null]);
  flushDebounce();
  f.requests[0].resolve({ items: [{ chatId: "oc_a", name: "A" }, { chatId: "oc_b", name: "B" }] });
  await settle();
  assert.equal(f.input.getAttribute("aria-expanded"), "true");
  assert.equal(f.input.dispatch("keydown", { key: "ArrowDown" }).prevented, true);
  assert.equal(f.input.getAttribute("aria-activedescendant"), `${f.input.id}-option-0`);
  f.input.dispatch("keydown", { key: "ArrowUp" });
  assert.equal(f.input.getAttribute("aria-activedescendant"), `${f.input.id}-option-1`);
  assert.equal(f.input.dispatch("keydown", { key: "Enter" }).prevented, true);
  assert.deepEqual(f.picker.getSelection(), { chatId: "oc_b", name: "B", avatarUrl: null });
  assert.equal(f.input.getAttribute("aria-expanded"), "false");
  assert.equal(f.input.getAttribute("aria-activedescendant"), null);
  assert.deepEqual(f.changes[1], { chatId: "oc_b", name: "B", avatarUrl: null });
  f.root.querySelector(".chat-picker-clear").click();
  assert.equal(f.input.value, "");
  assert.equal(f.picker.getSelection(), null);
  assert.deepEqual(f.changes[2], null);
  f.requests[1].resolve({ items: [], nextCursor: null });
  await settle();
  assert.match(f.root.textContent, /暂无可选择的群聊/);
  f.type("not-found");
  flushDebounce();
  f.requests[2].resolve({ items: [], nextCursor: null });
  await settle();
  assert.match(f.root.textContent, /未找到匹配群聊/);
  let stopped = false;
  const escape = f.input.dispatch("keydown", { key: "Escape", stopPropagation() { stopped = true; } });
  assert.equal(escape.prevented, true);
  assert.equal(stopped, true, "Escape should close the menu without closing its drawer");
  assert.equal(f.input.getAttribute("aria-expanded"), "false");
}

async function retryAndEscapeFromPaging() {
  const f = setup();
  f.picker.focus();
  assert.equal(f.input.dispatch("keydown", { key: "Enter" }).prevented, true,
    "Enter with no selected candidate must not submit a surrounding form");
  f.requests[0].reject(new Error("权限不足"));
  await settle();
  assert.match(f.root.textContent, /权限不足/);
  assert.equal(f.root.querySelector(".chat-picker-retry").hidden, false);
  f.root.querySelector(".chat-picker-retry").click();
  assert.deepEqual(f.requests[1].args, { query: "", cursor: null });
  f.requests[1].resolve({ items: [{ chatId: "oc_a", name: "A" }], nextCursor: "more" });
  await settle();
  const more = f.root.querySelector(".chat-picker-more");
  more.focus();
  more.dispatch("keydown", { key: "Escape" });
  assert.equal(document.activeElement, f.input);
  assert.equal(f.input.getAttribute("aria-expanded"), "false");
  assert.equal(f.requests.length, 2, "restoring focus on Escape must not reopen or query");
}

async function focusedPagingButtonsSurviveBusyRendering() {
  const f = setup();
  const more = f.root.querySelector(".chat-picker-more");
  const retry = f.root.querySelector(".chat-picker-retry");
  for (const button of [more, retry]) {
    let disabled = button.disabled;
    Object.defineProperty(button, "disabled", {
      get() { return disabled; },
      set(value) {
        disabled = value;
        // Model Chromium's real blur when a focused button becomes disabled.
        if (value && document.activeElement === button) {
          document.activeElement = document.body;
          button.dispatch("focusout", { relatedTarget: null });
        }
      },
    });
  }
  f.picker.focus();
  f.requests[0].resolve({ items: [], nextCursor: "page2" });
  await settle();
  more.focus();
  more.click();
  assert.equal(document.activeElement, f.input);
  assert.equal(f.input.getAttribute("aria-expanded"), "true");
  f.requests[1].reject(new Error("temporary failure"));
  await settle();
  retry.focus();
  retry.click();
  assert.equal(document.activeElement, f.input);
  assert.equal(f.input.getAttribute("aria-expanded"), "true");
  f.requests[2].resolve({ items: [{ chatId: "oc_page2", name: "Second page" }], nextCursor: null });
  await settle();
  assert.equal(f.rows().length, 1, "a paging result must not be invalidated by busy-state focus loss");
  f.rows()[0].click();
  assert.equal(f.picker.getSelection().chatId, "oc_page2");
}

async function lifecycleAndComposition() {
  const f = setup();
  f.picker.focus();
  f.picker.setSelection({ chatId: "oc_unavailable" });
  f.requests[0].reject(new Error("late failure"));
  await settle();
  assert.equal(f.input.value, "oc_unavailable", "unresolved saved IDs must remain visible");
  assert.equal(f.input.getAttribute("aria-expanded"), "false");
  assert.doesNotMatch(f.root.textContent, /late failure/);
  f.picker.setDisabled(true);
  f.picker.focus();
  f.input.dispatch("click");
  assert.equal(f.input.disabled, true);
  assert.equal(f.requests.length, 1);
  f.picker.setDisabled(false);
  f.input.dispatch("compositionstart");
  f.type("研");
  flushDebounce();
  assert.equal(f.requests.length, 1, "IME composition must not trigger intermediate searches");
  f.input.value = "研发";
  f.input.dispatch("compositionend");
  flushDebounce();
  assert.deepEqual(f.requests[1].args, { query: "研发", cursor: null });
  f.requests[1].resolve({ items: [{ chatId: "oc_dev", name: "研发群" }] });
  await settle();
  f.rows()[0].click();
  assert.deepEqual(f.picker.getSelection(), { chatId: "oc_dev", name: "研发群", avatarUrl: null });
  const changeCount = f.changes.length;
  f.picker.reset();
  assert.equal(f.changes.length, changeCount);
  assert.equal(f.picker.getSelection(), null);
  assert.equal(f.requests.length, 2, "reset must not query");
  f.picker.focus();
  f.input.dispatch("focusout", { relatedTarget: document.body });
  f.requests[2].resolve({ items: [{ chatId: "oc_late", name: "late" }] });
  await settle();
  assert.equal(f.input.getAttribute("aria-expanded"), "false");
  assert.equal(f.rows().length, 0);
}

async function avatarsAndDisplayOnlyRefresh() {
  const f = setup();
  const avatarUrl = "https://example.feishucdn.com/group.jpg";
  const freshAvatarUrl = "https://example.feishucdn.com/fresh.jpg";
  f.picker.focus();
  f.requests[0].resolve({ items: [
    { chatId: "oc_avatar", name: "<b>同名群</b>", avatarUrl },
    { chatId: "oc_missing", name: "同名群" },
  ] });
  await settle();
  const row = f.rows()[0];
  assert.equal(row.querySelector(".chat-picker-name").textContent, "<b>同名群</b>");
  assert.equal(row.querySelector("b"), null);
  assert.equal(row.querySelector(".chat-picker-id").textContent, "oc_avatar");
  assert.equal(row.querySelector(".chat-picker-avatar").getAttribute("aria-hidden"), "true");
  assert.equal(row.querySelector("img").src, avatarUrl);
  assert.equal(row.querySelector("img").alt, "");
  assert.equal(row.querySelector("img").referrerPolicy, "no-referrer");
  assert.equal(f.rows()[1].querySelector("img"), null);
  assert(row.querySelector(".chat-picker-avatar-fallback"));
  assert(f.rows()[1].querySelector(".chat-picker-avatar-fallback"));
  row.querySelector("img").dispatch("load");
  assert.equal(row.querySelector(".chat-picker-avatar-fallback").hidden, true,
    "loaded transparent avatars must not show a placeholder underneath");
  row.querySelector("img").dispatch("error");
  assert.equal(row.querySelector("img").hidden, true, "broken images reveal the group placeholder");
  assert.equal(row.querySelector(".chat-picker-avatar-fallback").hidden, false);
  row.click();
  const selected = f.root.querySelector(".chat-picker-selected-avatar");
  const selectedId = f.root.querySelector(".chat-picker-selected-id");
  assert.equal(selected.hidden, false);
  assert.equal(selected.querySelector("img").src, avatarUrl);
  assert.equal(selectedId.textContent, "oc_avatar");
  assert.equal(f.picker.getSelection().avatarUrl, avatarUrl);
  assert.equal(f.changes[0].avatarUrl, avatarUrl);
  selected.querySelector("img").dispatch("load");
  assert.equal(selected.querySelector(".chat-picker-avatar-fallback").hidden, true);
  selected.querySelector("img").dispatch("error");
  assert.equal(selected.querySelector("img").hidden, true);
  assert.equal(selected.querySelector(".chat-picker-avatar-fallback").hidden, false);
  assert.equal(f.picker.getSelection().chatId, "oc_avatar", "image failure must preserve target identity");
  f.picker.updateSelectionAvatar("oc_other", freshAvatarUrl);
  assert.equal(selected.querySelector("img").src, avatarUrl, "metadata for another target is ignored");
  f.picker.updateSelectionAvatar("oc_avatar", freshAvatarUrl);
  assert.equal(selected.querySelector("img").src, freshAvatarUrl);
  assert.equal(f.changes.length, 1, "avatar refresh must not emit target changes");
  assert.equal(f.input.value, "<b>同名群</b>");
  f.type("new search");
  assert.equal(selected.hidden, true);
  assert.equal(selectedId.hidden, true);
  f.picker.close();
  f.picker.setSelection({ chatId: "oc_missing", name: "群聊", avatarUrl: {} });
  assert.equal(selected.querySelector("img"), null);
  assert(selected.querySelector(".chat-picker-avatar-fallback"));
  assert.equal(selected.hidden, false);
  f.picker.reset();
  assert.equal(selected.hidden, true);
}

(async () => {
  await remoteQueriesIgnoreStaleResults();
  await paginationRetryAndPlainText();
  await selectionKeyboardAndEmptyStates();
  await retryAndEscapeFromPaging();
  await focusedPagingButtonsSurviveBusyRendering();
  await lifecycleAndComposition();
  await avatarsAndDisplayOnlyRefresh();
})().catch((error) => { console.error(error); process.exitCode = 1; });
