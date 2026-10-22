window.location = { pathname: "/defaults", search: "", assign() {} };
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, expire() {} };
const sessionStorage = { getItem() {}, setItem() {}, removeItem() {} };
const status = () => document.querySelector("#status").textContent;
let discardConfirmed = true;
const discardPrompts = [];
window.confirm = (message) => { discardPrompts.push(message); return discardConfirmed; };
const settings = { turn_settings: { model_id: "retired", effort_id: "high", service_tier_id: "priority" },
  reaction_pulse_enabled: false, progress_card_enabled: true, completion_mention_enabled: true, message_context_mode: "catch-up",
  progress_card_settings: { show_commentary: false, show_operations: true, show_plan: false,
    commentary_limit: 6, operation_limit: 15, commentary_max_chars: 280, operation_max_chars: 210,
    organization: "time" } };
const globalProgress = { ...settings.progress_card_settings, show_commentary: true, commentary_limit: 7 };
const envelope = (mode, id) => ({ csrfToken: `${id}-${mode}-csrf`, actionToken: `${id}-${mode}-once`,
  target: { resource: "default-rule", targetId: id } });
const rule = (id, kind = "group_name") => ({ id, kind, chat_id: kind === "chat" ? "oc_exact" : null,
  keyword: kind === "group_name" ? `<b>${id}</b>` : null, project: "missing-project",
  revision: 3, session_settings: structuredClone(settings), workspace: { mode: "project" },
  actions: { save: envelope("save", id), delete: envelope("delete", id) } });
const rules = [rule("first"), rule("second"), rule("third")];
const exact = rule("exact", "chat");
exact.chat = { chatId: "oc_exact", chatLabel: "<b>研发讨论群</b>", chatLabelResolved: true,
  chatOpenUrl: "https://applink.feishu.cn/client/chat/open?openChatId=oc_exact" };
let chatItems = [exact];
let groupItems = rules;
let chatHasMore = true;
let emptyChatOffsets = new Set();
let contextAvailable = true;
let viewExact = null;
let viewEffective = rules[0];
let failPost = false;
let posts = [];
let gets = [];
let pendingOptions = null;
let pendingView = null;
let pendingPost = null;
let pendingList = null;
let failList = false;
let pendingAvatar = null;
let workspaceOptions = {};
let workspaceErrors = {};
let pendingWorkspace = {};
function pauseRequest(gate, result) {
  return new Promise((resolve, reject) => {
    gate.resolve = () => resolve(result);
    gate.reject = (message) => reject(new Error(message));
  });
}
async function fixtureResponse(path, options = {}) {
  if (options.method === "POST") {
    posts.push({ path, body: JSON.parse(options.body) });
    if (pendingPost) return pauseRequest(pendingPost, {});
    if (failPost) throw new Error("配置已变化");
    return {};
  }
  gets.push(path);
  if (path === "/api/v1/progress-card/settings") return { settings: structuredClone(globalProgress) };
  if (path.startsWith("/api/v1/chats/validate?")) {
    const result = { chat: { chatId: new URL(path, "http://localhost").searchParams.get("chatId"),
      name: "最新群名称", avatarUrl: "https://example.feishucdn.com/saved.jpg" } };
    return pendingAvatar ? pauseRequest(pendingAvatar, result) : result;
  }
  if (path.startsWith("/api/v1/chats?")) return { items: [{ chatId: "oc_picker", name: "研发群",
    avatarUrl: "https://example.feishucdn.com/picker.jpg" }], nextCursor: null };
  if (path.startsWith("/api/v1/projects/options")) return { items: [{ alias: "available", enabled: true }], nextCursor: null };
  const query = new URL(path, "http://localhost").searchParams;
  if (query.get("mode") === "options") {
    const result = { models: [{ id: "available-model", display_name: "Available model", default_effort_id: "medium",
      default_service_tier_id: "default", efforts: [{ id: "medium" }], service_tiers: [{ id: "default", name: "Standard" }] }],
    session_settings: { ...settings, turn_settings: null, message_context_mode: "current-only" },
    model_catalog_error: null, context_mode_available: contextAvailable,
    workspace_options: workspaceOptions[query.get("project")] || null,
    workspace_options_error: workspaceErrors[query.get("project")] || null };
    if (pendingWorkspace[query.get("project")]) return pauseRequest(pendingWorkspace[query.get("project")], result);
    if (pendingOptions) return pauseRequest(pendingOptions, result);
    return result;
  }
  if (query.get("mode") === "view") {
    const result = { exact: viewExact, effective: viewExact || viewEffective,
      chat_kind: "group", match_error: null, actions: { create: envelope("create", "chat") } };
    return pendingView ? pauseRequest(pendingView, result) : result;
  }
  const kind = query.get("kind");
  if (failList) throw new Error("list unavailable");
  const offset = Number(query.get("offset") || 0);
  const result = { items: structuredClone(kind === "chat" ? emptyChatOffsets.has(offset) ? [] : chatItems : groupItems),
    has_more: kind === "chat" && chatHasMore, offset,
    order_revision: 7, actions: { create: envelope("create", kind), reorder: envelope("reorder", "order") } };
  return pendingList && kind === "chat" ? pauseRequest(pendingList, result) : result;
}
async function fetch(path, options = {}) {
  try {
    const data = await fixtureResponse(path, options);
    return { ok: true, status: 200, json: async () => data };
  } catch (error) {
    return { ok: false, status: error.status || 400,
      json: async () => ({ message: error.message, code: error.code, choices: error.choices }) };
  }
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
// SHIPPED_DEFAULTS_CONTROLLER
(async () => {
  await flush();
  await loadDefaults();
  assert.equal(defaultsInput("rules-body").querySelectorAll("tr").length, 3);
  const first = defaultsInput("rules-body").querySelector("tr");
  assert(first.textContent.includes("<b>first</b>"));
  assert.equal(first.querySelector("b"), null);
  assert(first.querySelectorAll("button")[2].disabled);
  assert.equal(defaultsInput("next").disabled, false);
  const chatRow = defaultsInput("chat-body").querySelector("tr");
  assert(chatRow.textContent.includes("<b>研发讨论群</b>"));
  assert(chatRow.textContent.includes("oc_exact"));
  assert.equal(chatRow.querySelector("b"), null);
  assert.equal(defaultsInput("panel-chat").hidden, false);
  assert.equal(defaultsInput("panel-group_name").hidden, true);
  assert.equal(defaultsInput("tab-chat").getAttribute("aria-selected"), "true");
  defaultsInput("tab-group_name").click();
  assert.equal(defaultsInput("panel-chat").hidden, true);
  assert.equal(defaultsInput("panel-group_name").hidden, false);
  assert.equal(defaultsInput("tab-group_name").tabIndex, 0);
  assert.equal(defaultsInput("tab-chat").tabIndex, -1);
  defaultsInput("new").focus();
  defaultsInput("new").click();
  await flush();
  assert.equal(defaultsEditor.kind, "group_name");
  assert(defaultsInput("drawer").open);
  assert(defaultsInput("chat-field").hidden);
  assert.equal(defaultsInput("keyword-field").hidden, false);
  assert.equal(document.activeElement, defaultsInput("keyword"));
  defaultsInput("drawer").dispatch("cancel");
  assert.equal(defaultsEditor, null);
  assert.equal(defaultsInput("drawer").open, false);
  assert.equal(document.activeElement, defaultsInput("new"));
  const tabKey = defaultsInput("tab-group_name").dispatch("keydown", { key: "Home" });
  assert(tabKey.prevented);
  assert.equal(defaultsTab, "chat");
  assert.equal(document.activeElement, defaultsInput("tab-chat"));
  defaultsInput("tab-chat").dispatch("keydown", { key: "ArrowRight" });
  assert.equal(defaultsTab, "group_name");
  defaultsInput("tab-group_name").dispatch("keydown", { key: "ArrowLeft" });
  assert.equal(defaultsTab, "chat");

  openDefaultEditor("chat", state.defaults.chat.items[0]);
  await flush();
  assert.equal(defaultsInput("project").value, "missing-project");
  assert.equal(defaultsInput("model").value, "retired");
  assert.equal(defaultsInput("effort").value, "high");
  assert.equal(defaultsInput("tier").value, "priority");
  assert(defaultsInput("note").textContent.includes("模型当前不可用"));
  assert(defaultsInput("chat").disabled);
  assert.equal(defaultsInput("progress-organization").value, "time");
  assert.equal(defaultsInput("progress-show-commentary").checked, false);
  assert.equal(defaultsInput("progress-commentary-capacity").hidden, true);
  assert.equal(defaultEditorHasChanges(), false);
  defaultsInput("progress-organization").value = "content";
  defaultsInput("progress-organization").dispatch("change");
  assert.equal(defaultEditorHasChanges(), true);
  defaultsInput("progress-operation-limit").value = "17";
  defaultsInput("progress-operation-limit").dispatch("change");
  assert.equal(defaultsInput("save").disabled, true);
  const priorPosts = posts.length;
  await saveDefault({ preventDefault() {} });
  assert.equal(posts.length, priorPosts);
  defaultsInput("progress-operation-limit").value = "16";
  defaultsInput("progress-operation-limit").dispatch("change");
  assert.equal(defaultsInput("save").disabled, false);
  defaultsInput("progress-show-commentary").checked = true;
  defaultsInput("progress-show-commentary").dispatch("change");
  assert.equal(defaultsInput("progress-commentary-capacity").hidden, false);
  assert.equal(defaultsInput("progress-commentary-limit").value, 6);
  defaultsInput("progress").checked = false;
  changeDefaultSettings("progress");
  await saveDefault({ preventDefault() {} });
  const saved = posts.at(-1);
  assert.equal(saved.path, "/api/v1/defaults/save");
  assert.equal(saved.body.definition.project, "missing-project");
  assert.equal(saved.body.definition.chat_id, "oc_exact");
  assert.deepEqual(saved.body.definition.session_settings.turn_settings, settings.turn_settings);
  assert.equal(saved.body.definition.session_settings.progress_card_enabled, false);
  assert.deepEqual(saved.body.definition.session_settings.progress_card_settings,
    { ...settings.progress_card_settings, organization: "content", operation_limit: 16, show_commentary: true });
  assert.equal(saved.body.target.targetId, "exact");
  assert.equal(saved.body.definition.expected_revision, undefined);

  openDefaultEditor("chat");
  await flush();
  defaultsInput("chat").value = "oc_inherits";
  await loadDefaultOptions(true);
  assert(defaultsInput("note").textContent.includes("保存后成为本聊天的精确配置"));
  assert.equal(defaultsInput("project").value, "missing-project");
  await saveDefault({ preventDefault() {} });
  assert.equal(posts.at(-1).body.definition.chat_id, "oc_inherits");
  assert.equal(posts.at(-1).body.target.targetId, "chat");

  viewExact = exact;
  openDefaultEditor("chat");
  await flush();
  defaultsInput("chat").value = "oc_exact";
  await loadDefaultOptions(true);
  await saveDefault({ preventDefault() {} });
  assert.equal(posts.at(-1).body.target.targetId, "exact");
  viewExact = null;

  openDefaultEditor("chat");
  await flush();
  defaultsInput("chat").value = "oc_inherits";
  await loadDefaultOptions(true);
  assert.equal(defaultsInput("project").value, "missing-project");
  viewEffective = null;
  defaultsInput("chat").value = "oc_unconfigured";
  await loadDefaultOptions(true);
  assert.equal(defaultsInput("project").value, "");
  assert.equal(defaultsEditor.settings.turn_settings, null);
  assert.equal(defaultsEditor.settings.message_context_mode, "current-only");
  assert(defaultsInput("note").textContent.includes("尚未配置"));
  closeDefaultEditor();
  viewEffective = rules[0];

  await moveDefaultRule(1, -1);
  assert.equal(posts.at(-1).path, "/api/v1/defaults/reorder");
  assert.deepEqual(posts.at(-1).body.rule_ids, ["second", "first", "third"]);
  assert.equal(posts.at(-1).body.order_revision, undefined);

  contextAvailable = false;
  openDefaultEditor("chat", state.defaults.chat.items[0]);
  await flush();
  assert(defaultsInput("context").querySelectorAll("option").find((option) => option.value === "catch-up").disabled);
  assert.equal(defaultsEditor.settings.message_context_mode, "catch-up");
  assert(defaultsInput("note").textContent.includes("单聊不支持"));
  defaultsInput("context").value = "current-only";
  changeDefaultSettings("context");
  assert.equal(defaultsEditor.settings.message_context_mode, "current-only");
  defaultsInput("model").value = "available-model";
  changeDefaultSettings("model");
  assert.deepEqual(defaultsEditor.settings.turn_settings, { model_id: "available-model", effort_id: "medium", service_tier_id: "default" });

  failPost = true;
  const beforeFailure = posts.length;
  await saveDefault({ preventDefault() {} });
  assert(status().includes("配置已变化"));
  assert(defaultsInput("save").disabled);
  await saveDefault({ preventDefault() {} });
  assert.equal(posts.length, beforeFailure + 1);
  failPost = false;
  closeDefaultEditor();
  assert(defaultsInput("new").disabled);
  assert(defaultsInput("chat-body").querySelector("button").disabled);
  await loadDefaults();

  await deleteDefault(state.defaults.chat.items[0]);
  assert.equal(posts.at(-1).path, "/api/v1/defaults/delete");
  assert.equal(posts.at(-1).body.target.targetId, "exact");
  await loadDefaults(50);
  assert(gets.some((path) => path.includes("offset=50")));
  assert.equal(defaultsInput("page").textContent, "第 2 页");
  assert.equal(defaultsInput("previous").disabled, false);

  pendingOptions = {};
  openDefaultEditor("group_name", state.defaults.group_name.items[0]);
  assert(defaultsInput("save").disabled);
  assert(defaultsInput("fields").disabled);
  pendingOptions.resolve();
  pendingOptions = null;
  await flush();
  assert.equal(defaultsInput("fields").disabled, false);
  defaultsInput("progress").checked = false;
  changeDefaultSettings("progress");
  assert.equal(defaultsEditor.settings.progress_card_enabled, false);
  closeDefaultEditor();

  // Both dependent reads keep the whole form disabled until the current
  // chat's configuration and model choices have finished loading.
  openDefaultEditor("chat");
  await flush();
  defaultsInput("chat").value = "oc_delayed";
  pendingView = {};
  pendingOptions = {};
  const delayedLookup = loadDefaultOptions(true);
  assert(defaultsInput("fields").disabled);
  assert(defaultsInput("save").disabled);
  assert.equal(defaultsInput("project").value, "");
  const beforeLookupSave = posts.length;
  await saveDefault({ preventDefault() {} });
  assert.equal(posts.length, beforeLookupSave);
  pendingView.resolve();
  pendingView = null;
  await flush();
  assert.equal(defaultsInput("project").value, "missing-project");
  assert(defaultsInput("fields").disabled);
  pendingOptions.resolve();
  pendingOptions = null;
  await delayedLookup;
  assert.equal(defaultsInput("fields").disabled, false);
  assert.equal(defaultsInput("save").disabled, false);

  // Errors restore editing, while failed chat resolution still requires
  // reopening the form before saving, as it did before this loading change.
  for (const stage of ["view", "options"]) {
    openDefaultEditor("chat");
    await flush();
    defaultsInput("chat").value = `oc_failed_${stage}`;
    const gate = {};
    if (stage === "view") pendingView = gate;
    else pendingOptions = gate;
    const failedLookup = loadDefaultOptions(true);
    await flush();
    assert(defaultsInput("fields").disabled);
    gate.reject(`${stage} unavailable`);
    pendingView = null;
    pendingOptions = null;
    await failedLookup;
    assert.equal(defaultsInput("fields").disabled, false);
    assert(defaultsInput("save").disabled);
    assert(defaultsInput("note").textContent.includes(`${stage} unavailable`));
  }
  pendingOptions = {};
  openDefaultEditor("group_name", state.defaults.group_name.items[0]);
  assert(defaultsInput("fields").disabled);
  pendingOptions.reject("catalog unavailable");
  pendingOptions = null;
  await flush();
  assert.equal(defaultsInput("fields").disabled, false);
  assert.equal(defaultsInput("save").disabled, false);
  assert.equal(defaultsInput("model").value, "retired");

  // A superseded request cannot release the current request's fieldset.
  openDefaultEditor("chat");
  await flush();
  defaultsInput("chat").value = "oc_old";
  const oldView = pendingView = {};
  const oldLookup = loadDefaultOptions(true);
  defaultsInput("chat").value = "oc_new";
  const newView = pendingView = {};
  pendingOptions = {};
  const newLookup = loadDefaultOptions(true);
  oldView.reject("old request failed");
  await oldLookup;
  assert(defaultsInput("fields").disabled);
  assert.equal(defaultsInput("note").textContent.includes("old request failed"), false);
  newView.resolve();
  pendingView = null;
  await flush();
  assert(defaultsInput("fields").disabled);
  pendingOptions.resolve();
  pendingOptions = null;
  await newLookup;
  assert.equal(defaultsInput("fields").disabled, false);

  const previousEditorOptions = pendingOptions = {};
  openDefaultEditor("group_name", state.defaults.group_name.items[0]);
  const currentEditorOptions = pendingOptions = {};
  openDefaultEditor("chat", state.defaults.chat.items[0]);
  previousEditorOptions.resolve();
  await flush();
  assert(defaultsInput("fields").disabled);
  currentEditorOptions.resolve();
  pendingOptions = null;
  await flush();
  assert.equal(defaultsInput("fields").disabled, false);

  // A loading callback must not enable a form while a separate list action
  // is still saving, and a failed mutation must not enable a pending query.
  for (const queryFirst of [true, false]) {
    pendingOptions = {};
    const lookup = loadDefaultOptions();
    pendingPost = {};
    const mutation = defaultMutation("delete", envelope("delete", "unrelated-rule"));
    assert(defaultsInput("fields").disabled);
    if (queryFirst) {
      pendingOptions.resolve();
      pendingOptions = null;
      await lookup;
      assert(defaultsInput("fields").disabled);
      pendingPost.reject("save unavailable");
      pendingPost = null;
      await mutation;
    } else {
      pendingPost.reject("save unavailable");
      pendingPost = null;
      await mutation;
      assert(defaultsInput("fields").disabled);
      pendingOptions.resolve();
      pendingOptions = null;
      await lookup;
    }
    assert.equal(defaultsInput("fields").disabled, false);
    assert(defaultsInput("save").disabled);
  }
  closeDefaultEditor();

  // Switching views and refreshing keep the exact-chat page, while saving a
  // group rule keeps the active group view and its priority order.
  await loadDefaults(50);
  defaultsInput("tab-group_name").click();
  await loadDefaults();
  assert.equal(defaultsTab, "group_name");
  assert.equal(defaultsOffset, 50);
  const groupEdit = defaultsInput("rules-body").querySelector("button");
  groupEdit.focus();
  groupEdit.click();
  await flush();
  pendingPost = {};
  const savingGroup = saveDefault({ preventDefault() {} });
  assert(defaultsInput("save").disabled);
  assert(defaultsInput("close").disabled);
  assert(defaultsInput("cancel").disabled);
  assert(defaultsInput("tab-chat").disabled);
  assert(defaultsInput("new").disabled);
  defaultsInput("drawer").dispatch("cancel");
  assert(defaultsInput("drawer").open);
  selectDefaultTab("chat");
  assert.equal(defaultsTab, "group_name");
  pendingPost.resolve();
  pendingPost = null;
  await savingGroup;
  assert.equal(defaultsInput("drawer").open, false);
  assert.equal(defaultsInput("editor").getAttribute("aria-busy"), "false");
  assert.equal(defaultsTab, "group_name");
  assert.equal(defaultsOffset, 50);
  assert.equal(document.activeElement, defaultsInput("rules-body").querySelector("button"));
  defaultsInput("tab-chat").click();
  assert.equal(defaultsInput("page").textContent, "第 2 页");

  // A dismissed, still-loading drawer cannot reopen itself or overwrite a
  // later editor. Closing remains available during reads, but not writes.
  pendingOptions = {};
  openDefaultEditor("chat");
  const abandoned = pendingOptions;
  defaultsInput("close").click();
  assert.equal(defaultsInput("drawer").open, false);
  pendingOptions = null;
  openDefaultEditor("group_name", rules[1]);
  await flush();
  abandoned.resolve();
  await flush();
  assert.equal(defaultsEditor.kind, "group_name");
  assert.equal(defaultsInput("keyword").value, rules[1].keyword);
  closeDefaultEditor();

  // A completed write with a failed refresh cannot reuse the old list's
  // one-shot actions. A fresh read restores the editing controls.
  openDefaultEditor("group_name", state.defaults.group_name.items[0]);
  await flush();
  failList = true;
  await saveDefault({ preventDefault() {} });
  assert(status().includes("列表刷新失败"));
  assert(defaultsInput("new").disabled);
  assert(defaultsInput("rules-body").querySelector("button").disabled);
  openDefaultEditor("group_name", state.defaults.group_name.items[0]);
  assert.equal(defaultsEditor, null);
  failList = false;
  await loadDefaults();
  assert.equal(defaultsInput("new").disabled, false);

  // Native dialogs cannot return focus to a disabled toolbar opener. After
  // the post-save read finishes, restore it without moving the list position.
  defaultsInput("new").focus();
  defaultsInput("new").click();
  await flush();
  defaultsInput("chat").value = "oc_new_saved";
  await loadDefaultOptions(true);
  pendingList = {};
  const savingNew = saveDefault({ preventDefault() {} });
  await flush();
  assert.equal(defaultsInput("drawer").open, false);
  assert(defaultsInput("new").disabled);
  document.activeElement = document.body;
  pendingList.resolve();
  pendingList = null;
  await savingNew;
  assert.equal(document.activeElement, defaultsInput("new"));

  // Empty pages after deletion retreat to a populated page. Empty registries
  // have a usable creation entry and no meaningless pagination controls.
  chatHasMore = false;
  emptyChatOffsets.add(50);
  await loadDefaults(50);
  assert.equal(defaultsOffset, 0);
  assert(defaultsInput("pagination").hidden);
  chatItems = [];
  groupItems = [];
  await loadDefaults();
  assert(defaultsInput("pagination").hidden);
  defaultsInput("chat-body").querySelector("button").click();
  await flush();
  assert.equal(defaultsEditor.kind, "chat");
  assert(defaultsInput("drawer").open);
  closeDefaultEditor();
  defaultsInput("tab-group_name").click();
  defaultsInput("rules-body").querySelector("button").click();
  await flush();
  assert.equal(defaultsEditor.kind, "group_name");
  closeDefaultEditor();

  // A slower list response cannot restore stale results over a newer refresh.
  chatItems = [exact];
  groupItems = rules;
  pendingList = {};
  const stale = pendingList;
  const oldList = loadDefaults();
  pendingList = null;
  chatItems = [{ ...exact, chat: { ...exact.chat, chatLabelResolved: false } }];
  await loadDefaults();
  stale.resolve();
  await oldList;
  assert.equal(defaultsInput("chat-body").textContent.includes("研发讨论群"), false);
  assert(defaultsInput("chat-body").textContent.includes("oc_exact"));

  // Exact defaults share the remote group picker, while p2p remains explicit ID entry.
  contextAvailable = true;
  viewExact = null;
  viewEffective = null;
  selectDefaultTab("chat");
  const directoryReads = gets.filter((path) => path.startsWith("/api/v1/chats?")).length;
  openDefaultEditor("chat");
  await flush();
  assert.equal(defaultsInput("chat-kind").value, "group");
  assert.equal(defaultsInput("group-picker").hidden, false);
  assert.equal(defaultsInput("chat-field").hidden, true);
  assert.equal(defaultsInput("chat-id-help").hidden, true);
  assert.equal(defaultsInput("group-picker").querySelector(".chat-picker-summary").textContent, "请选择群聊");
  assert.equal(defaultsInput("group-picker").querySelector(".chat-picker-empty-option"), null);
  assert.match(defaultsInput("group-picker").textContent, /当前已加入且可访问/);
  assert.equal(document.activeElement, defaultsInput("group"));
  assert.equal(defaultsInput("group").getAttribute("aria-expanded"), "false",
    "opening the drawer focuses the group trigger without expanding it");
  assert.equal(gets.filter((path) => path.startsWith("/api/v1/chats?")).length, directoryReads,
    "opening the drawer does not request the group directory");
  defaultsInput("group").click();
  await flush();
  defaultsInput("group-picker").querySelector(".chat-picker-option").click();
  await flush();
  assert.equal(defaultsInput("chat").value, "oc_picker");
  assert.equal(defaultsInput("group-picker").querySelector(".chat-picker-selected-avatar").querySelector("img").src,
    "https://example.feishucdn.com/picker.jpg");
  assert(gets.some((path) => path.includes("mode=view&chat_id=oc_picker")));
  const actionBeforeSearch = defaultsEditor.action;
  const settingsBeforeSearch = structuredClone(defaultsEditor.settings);
  const lookupsBeforeSearch = gets.filter((path) => path.includes("mode=view")).length;
  defaultsInput("group").click();
  const defaultsSearch = defaultsInput("group-search");
  defaultsSearch.value = "另一个群";
  defaultsSearch.dispatch("input");
  const cancelSearch = defaultsSearch.dispatch("keydown", { key: "Escape" });
  assert(cancelSearch.prevented);
  assert(defaultsInput("drawer").open, "cancelling picker search keeps the defaults editor open");
  assert.equal(defaultsInput("chat").value, "oc_picker");
  assert.equal(defaultsEditor.action, actionBeforeSearch);
  assert.deepEqual(defaultsEditor.settings, settingsBeforeSearch);
  assert.equal(gets.filter((path) => path.includes("mode=view")).length, lookupsBeforeSearch,
    "searching must not reload or discard settings for the selected exact target");
  defaultsInput("chat-manual").checked = true;
  defaultsInput("chat-manual").dispatch("change");
  await flush();
  assert.equal(defaultsInput("chat-field").hidden, false);
  assert.equal(defaultsInput("chat").disabled, false);
  const idHelp = defaultsInput("chat-id-help");
  assert.equal(idHelp.hidden, false, "editable manual IDs should expose their acquisition guide");
  const idHelpLink = idHelp.querySelector("a");
  assert.equal(idHelpLink.textContent, "如何获取聊天 ID");
  assert.equal(idHelpLink.getAttribute("href"), "https://open.feishu.cn/document/server-docs/group/chat/chat-id-description");
  assert.equal(idHelpLink.getAttribute("target"), "_blank");
  assert.equal(idHelpLink.getAttribute("rel"), "noopener noreferrer");
  assert(defaultsInput("chat").getAttribute("aria-describedby").split(" ").includes(idHelp.id));
  assert.equal(defaultsInput("chat").value, "oc_picker");
  defaultsInput("chat-manual").checked = false;
  defaultsInput("chat-manual").dispatch("change");
  assert.equal(defaultsInput("chat").value, "");
  assert.equal(idHelp.hidden, true, "group picker mode should not present an ID input tip");
  assert.equal(defaultsInput("save").disabled, true);
  defaultsInput("chat-kind").value = "p2p";
  defaultsInput("chat-kind").dispatch("change");
  assert.equal(defaultsInput("group-picker").hidden, true);
  assert.equal(defaultsInput("chat-field").hidden, false);
  assert.match(defaultsInput("chat-help").textContent, /\/defaults/);
  assert.match(defaultsInput("chat-help").textContent, /目标单聊.*直接配置该聊天/);
  assert.equal(idHelp.hidden, false);
  assert.match(idHelp.textContent, /不是用户 ID/);
  assert.match(idHelp.textContent, /与本机器人的聊天/);
  assert.equal(defaultsInput("context-field").hidden, true);
  contextAvailable = false;
  defaultsInput("chat").value = "oc_p2p";
  defaultsInput("chat").dispatch("input");
  defaultsInput("chat").dispatch("change");
  await flush();
  assert.equal(defaultsInput("chat-kind").value, "p2p");
  assert.equal(defaultsInput("chat").value, "oc_p2p");
  closeDefaultEditor();

  // Unknown saved targets stay visible and locked through failed/unresolved metadata.
  contextAvailable = null;
  const unresolved = { ...exact, chat: { chatLabel: "旧目标", chatType: null, chatMode: null } };
  openDefaultEditor("chat", unresolved);
  await flush();
  assert.equal(defaultsInput("chat-kind").value, "unknown");
  assert.equal(defaultsInput("chat").value, "oc_exact");
  assert.equal(defaultsInput("chat-field").hidden, false);
  assert.equal(defaultsInput("chat").disabled, true);
  assert.equal(idHelp.hidden, true, "read-only saved IDs should not look like editable targets");
  assert.match(defaultsInput("chat-help").textContent, /保留原聊天 ID/);
  contextAvailable = true;
  await loadDefaultOptions();
  assert.equal(defaultsInput("chat-kind").value, "group");
  assert.equal(defaultsInput("chat").value, "oc_exact");
  assert.equal(defaultsInput("chat-field").hidden, false);
  assert.equal(defaultsInput("chat").disabled, true);
  closeDefaultEditor();

  // chatType is authoritative; chatMode may describe message layout, not p2p identity.
  const target = chatTarget("defaults");
  target.set({ chatId: "oc_direct", chat: { chatType: "p2p", chatMode: "group" } });
  assert.equal(target.kind(), "p2p");
  assert.equal(defaultsInput("chat-field").hidden, false);
  target.set({ chatId: "oc_group", chat: { chatType: "group", chatMode: "p2p" } });
  assert.equal(target.kind(), "group");
  target.set({ chatId: "oc_corrected", chat: { chatType: "p2p" } });
  target.resolveKind(true);
  assert.equal(target.kind(), "group");
  assert.equal(defaultsInput("chat").value, "oc_corrected");
  assert.equal(defaultsInput("chat-field").hidden, false, "correcting a typed target must not hide its ID behind an empty picker");
  assert.equal(defaultsInput("group-picker").hidden, true);

  // Existing group targets retrieve display metadata without changing their identity,
  // name, locked state or edit/save capability. Late responses are never retargeted.
  const selectedAvatar = () => defaultsInput("group-picker").querySelector(".chat-picker-selected-avatar");
  target.set({ chatId: "oc_saved", chat: { chatType: "group", chatLabel: "已存群名" }, readOnly: true });
  await flush();
  assert.equal(selectedAvatar().querySelector("img").src, "https://example.feishucdn.com/saved.jpg");
  assert.equal(defaultsInput("group-picker").querySelector(".chat-picker-summary").textContent, "已存群名");
  assert.equal(defaultsInput("chat").value, "oc_saved");
  assert.equal(defaultsInput("group").disabled, true);
  pendingAvatar = {};
  const failedAvatar = pendingAvatar;
  target.set({ chatId: "oc_unavailable", chat: { chatType: "group" }, readOnly: true });
  const statusBeforeAvatarFailure = status();
  failedAvatar.reject("群头像暂不可用");
  pendingAvatar = null;
  await flush();
  assert.equal(selectedAvatar().querySelector("img"), null);
  assert.equal(defaultsInput("chat").value, "oc_unavailable");
  assert.equal(status(), statusBeforeAvatarFailure);
  pendingAvatar = {};
  const staleAvatar = pendingAvatar;
  target.set({ chatId: "oc_same", chat: { chatType: "group" } });
  pendingAvatar = null;
  target.set({ chatId: "oc_same", chat: { chatType: "group", avatarUrl: "https://example.feishucdn.com/new.jpg" } });
  staleAvatar.resolve();
  await flush();
  assert.equal(selectedAvatar().querySelector("img").src, "https://example.feishucdn.com/new.jpg");
  pendingAvatar = {};
  const closedAvatar = pendingAvatar;
  target.set({ chatId: "oc_closed", chat: { chatType: "group" } });
  target.close();
  pendingAvatar = null;
  closedAvatar.resolve();
  await flush();
  assert.equal(selectedAvatar().querySelector("img"), null);
  pendingAvatar = {};
  const clearedAvatar = pendingAvatar;
  target.set({ chatId: "oc_cleared", chat: { chatType: "group" } });
  defaultsInput("group-picker").querySelector(".chat-picker-clear").click();
  pendingAvatar = null;
  clearedAvatar.resolve();
  await flush();
  assert.equal(defaultsInput("chat").value, "");
  assert.equal(selectedAvatar().hidden, true);
  target.close();

  // An unavailable avatar does not disable editing or saving an existing exact rule.
  contextAvailable = true;
  pendingAvatar = {};
  const unavailableExisting = pendingAvatar;
  openDefaultEditor("chat", { ...exact, chat: { ...exact.chat, chatType: "group" } });
  await flush();
  const saveDisabledBefore = defaultsInput("save").disabled;
  unavailableExisting.reject("机器人已退出目标群");
  pendingAvatar = null;
  await flush();
  assert.equal(defaultsInput("save").disabled, saveDisabledBefore);
  defaultsInput("progress").checked = !defaultsEditor.settings.progress_card_enabled;
  changeDefaultSettings("progress");
  const beforeSavingWithoutAvatar = posts.length;
  await saveDefault({ preventDefault() {} });
  assert.equal(posts.length, beforeSavingWithoutAvatar + 1);
  assert.equal(posts.at(-1).body.definition.chat_id, "oc_exact");

  // Only user changes protect the drawer: hydration, reverting edits and saving
  // do not produce spurious confirmations, including asynchronously read defaults.
  pendingOptions = {};
  openDefaultEditor("group_name");
  assert.equal(defaultEditorHasChanges(), false);
  pendingOptions.resolve();
  pendingOptions = null;
  await flush();
  assert.equal(defaultEditorHasChanges(), false);
  const beforeNoopClose = discardPrompts.length;
  defaultsInput("close").click();
  assert.equal(discardPrompts.length, beforeNoopClose);
  openDefaultEditor("group_name", rules[0]);
  await flush();
  const originalKeyword = defaultsInput("keyword").value;
  defaultsInput("keyword").value = "changed keyword";
  discardConfirmed = false;
  for (const cancel of [() => defaultsInput("close").click(), () => defaultsInput("cancel").click(),
    () => defaultsInput("drawer").dispatch("cancel")]) {
    cancel();
    assert.equal(defaultsInput("drawer").open, true);
    assert.equal(defaultsInput("keyword").value, "changed keyword");
  }
  defaultsInput("keyword").value = originalKeyword;
  assert.equal(defaultEditorHasChanges(), false);
  const beforeRevertedClose = discardPrompts.length;
  defaultsInput("close").click();
  assert.equal(defaultsInput("drawer").open, false);
  assert.equal(discardPrompts.length, beforeRevertedClose);
  openDefaultEditor("group_name", rules[0]);
  await flush();
  defaultsInput("keyword").value = "saved keyword";
  const beforeSavedClose = discardPrompts.length;
  await saveDefault({ preventDefault() {} });
  assert.equal(defaultsInput("drawer").open, false);
  assert.equal(discardPrompts.length, beforeSavedClose);
  openDefaultEditor("group_name", rules[0]);
  await flush();
  defaultsInput("project").value = "available";
  discardConfirmed = true;
  defaultsInput("cancel").click();
  assert.equal(defaultsInput("drawer").open, false);
  assert.equal(defaultsEditor, null);

  // Workspace choices are explicit saved intent. Catalog hydration never
  // replaces a missing resource with the project directory.
  workspaceOptions = {
    available: { projectAlias: "available", projectRevision: 7, isGit: true,
      items: [{ id: "managed-one", cwd: "/worktrees/one", branch: "main", available: true },
        { id: "deleting-exact", cwd: "/worktrees/deleting", branch: null, state: "ready", available: false }] },
    nongit: { projectAlias: "nongit", projectRevision: 8, isGit: false, items: [] },
  };
  const existingRule = { ...rules[0], project: "available",
    workspace: { mode: "existing", worktree_id: "managed-one" } };
  openDefaultEditor("group_name", existingRule);
  await flush();
  assert.equal(defaultsInput("workspace").value, "existing");
  assert.equal(defaultsInput("worktree").value, "managed-one");
  assert.equal(defaultsInput("worktree-field").hidden, false);
  assert.equal(defaultEditorHasChanges(), false);
  await saveDefault({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.workspace, existingRule.workspace);
  assert.equal(posts.at(-1).body.definition.expected_project_revision, 7);

  // A Git-ready record can still be unavailable due to deletion admission.
  openDefaultEditor("group_name", { ...existingRule, workspace: { mode: "existing", worktree_id: "deleting-exact" } });
  await flush();
  assert.equal(defaultsInput("worktree").value, "deleting-exact");
  assert(defaultsInput("worktree").querySelectorAll("option")
    .find((option) => option.value === "deleting-exact").textContent.includes("不可用"));
  assert.equal(defaultsInput("save").disabled, true);
  assert.equal(defaultEditorHasChanges(), false);
  closeDefaultEditor();

  const unavailableRule = { ...existingRule, workspace: { mode: "existing", worktree_id: "deleted-worktree" } };
  openDefaultEditor("group_name", unavailableRule);
  await flush();
  assert.deepEqual(defaultsEditor.workspace, unavailableRule.workspace);
  assert.equal(defaultsInput("worktree").value, "deleted-worktree");
  assert(defaultsInput("workspace-help").textContent.includes("不可用"));
  assert(defaultsInput("save").disabled);
  assert.equal(defaultEditorHasChanges(), false);
  const beforeUnavailableSave = posts.length;
  await saveDefault({ preventDefault() {} });
  assert.equal(posts.length, beforeUnavailableSave);
  defaultsInput("worktree").value = "managed-one";
  defaultsInput("worktree").dispatch("change");
  assert.equal(defaultsInput("save").disabled, false);
  assert.equal(defaultEditorHasChanges(), true);
  defaultsInput("workspace").value = "project";
  defaultsInput("workspace").dispatch("change");
  await saveDefault({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.workspace, { mode: "project" });

  openDefaultEditor("group_name", existingRule);
  await flush();
  defaultsInput("workspace").value = "new";
  defaultsInput("workspace").dispatch("change");
  assert.equal(defaultsInput("workspace-ref-field").hidden, false);
  assert.equal(defaultsInput("worktree-field").hidden, true);
  assert.equal(defaultsInput("workspace-ref").value, "");
  assert.equal(defaultEditorHasChanges(), true);
  await saveDefault({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.workspace, { mode: "new" });
  openDefaultEditor("group_name", { ...existingRule, workspace: { mode: "new", ref: "refs/heads/main" } });
  await flush();
  assert.equal(defaultsInput("workspace-ref").value, "refs/heads/main");
  assert.equal(defaultEditorHasChanges(), false);
  defaultsInput("workspace-ref").value = "  refs/tags/v2  ";
  defaultsInput("workspace-ref").dispatch("input");
  await saveDefault({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.workspace, { mode: "new", ref: "refs/tags/v2" });

  openDefaultEditor("group_name", existingRule);
  await flush();
  defaultsInput("project").value = "nongit";
  defaultsInput("project").dispatch("change");
  await flush();
  assert.deepEqual(defaultsEditor.workspace, { mode: "project" });
  assert(defaultsInput("workspace").querySelectorAll("option").filter((item) => item.value !== "project")
    .every((item) => item.disabled));
  assert(defaultsInput("workspace-help").textContent.includes("不是 Git 仓库"));
  assert.equal(defaultsInput("save").disabled, false);
  assert.equal(defaultEditorHasChanges(), true);
  closeDefaultEditor();

  // Project changes, chat inheritance and delayed results all use the same
  // options request identity; old reads cannot restore a previous workspace.
  openDefaultEditor("group_name", existingRule);
  await flush();
  defaultsInput("project").value = "available";
  const oldWorkspace = pendingWorkspace.available = {};
  const oldWorkspaceLoad = changeDefaultProject();
  defaultsInput("project").value = "nongit";
  const newWorkspace = pendingWorkspace.nongit = {};
  const newWorkspaceLoad = changeDefaultProject();
  oldWorkspace.resolve();
  await oldWorkspaceLoad;
  assert(defaultsInput("fields").disabled);
  newWorkspace.resolve();
  await newWorkspaceLoad;
  assert.equal(defaultsEditor.workspaceOptions.projectAlias, "nongit");
  assert.deepEqual(defaultsEditor.workspace, { mode: "project" });
  pendingWorkspace = {};
  closeDefaultEditor();
  viewEffective = existingRule;
  openDefaultEditor("chat");
  await flush();
  defaultsInput("chat").value = "oc_inherits_workspace";
  await loadDefaultOptions(true);
  assert.deepEqual(defaultsEditor.workspace, existingRule.workspace);
  assert.equal(defaultsInput("worktree").value, "managed-one");
  closeDefaultEditor();

  workspaceErrors.available = "Project 已删除或不可用";
  delete workspaceOptions.available;
  openDefaultEditor("group_name", existingRule);
  await flush();
  assert.deepEqual(defaultsEditor.workspace, existingRule.workspace);
  assert(defaultsInput("workspace-help").textContent.includes("Project 已删除"));
  assert(defaultsInput("save").disabled);
  closeDefaultEditor();

  groupItems = [existingRule, { ...rules[1], workspace: { mode: "new", ref: "release" } }];
  await loadDefaults();
  assert(defaultsInput("rules-body").textContent.includes("使用已有 worktree：managed-one"));
  assert(defaultsInput("rules-body").textContent.includes("每个新会话新建 worktree · release"));

  const unsetRule = { ...rules[0], project: "available", workspace: { mode: "project" },
    session_settings: { ...settings, progress_card_settings: null } };
  openDefaultEditor("group_name", unsetRule);
  await flush();
  assert.equal(defaultsInput("progress-commentary-limit").value, 7);
  assert.equal(defaultEditorHasChanges(), false);
  defaultsInput("model").value = "available-model"; changeDefaultSettings("model");
  defaultsInput("progress").checked = false; changeDefaultSettings("progress");
  await saveDefault({ preventDefault() {} });
  assert.equal(posts.at(-1).body.definition.session_settings.progress_card_settings, null);

  openDefaultEditor("group_name", unsetRule);
  await flush();
  defaultsInput("progress-organization").value = "content";
  changeDefaultSettings("progress-organization");
  assert.deepEqual(defaultsEditor.settings.progress_card_settings, { ...globalProgress, organization: "content" });
  await saveDefault({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.session_settings.progress_card_settings,
    { ...globalProgress, organization: "content" });
  assert.equal(defaultScheduleSessionSettings().progress_card_settings, null);
})().catch((error) => { console.error(error); process.exitCode = 1; });
