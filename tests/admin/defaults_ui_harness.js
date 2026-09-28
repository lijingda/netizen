const state = { defaults: null };
let status = "";
function setStatus(value) { status = value; }
window.confirm = () => true;
const settings = { turn_settings: { model_id: "retired", effort_id: "high", service_tier_id: "priority" },
  reaction_pulse_enabled: false, progress_card_enabled: true, completion_mention_enabled: true, message_context_mode: "catch-up" };
const envelope = (mode, id) => ({ csrfToken: `${id}-${mode}-csrf`, actionToken: `${id}-${mode}-once`,
  target: { resource: "default-rule", targetId: id } });
const rule = (id, kind = "group_name") => ({ id, kind, chat_id: kind === "chat" ? "oc_exact" : null,
  keyword: kind === "group_name" ? `<b>${id}</b>` : null, project: "missing-project",
  revision: 3, session_settings: structuredClone(settings),
  actions: { save: envelope("save", id), delete: envelope("delete", id) } });
const rules = [rule("first"), rule("second"), rule("third")];
const exact = rule("exact", "chat");
let contextAvailable = true;
let viewExact = null;
let viewEffective = rules[0];
let failPost = false;
let posts = [];
let gets = [];
let pendingOptions = null;
let pendingView = null;
let pendingPost = null;
function pauseRequest(gate, result) {
  return new Promise((resolve, reject) => {
    gate.resolve = () => resolve(result);
    gate.reject = (message) => reject(new Error(message));
  });
}
async function api(path, options = {}) {
  if (options.method === "POST") {
    posts.push({ path, body: JSON.parse(options.body) });
    if (pendingPost) return pauseRequest(pendingPost, {});
    if (failPost) throw new Error("配置已变化");
    return {};
  }
  gets.push(path);
  if (path.startsWith("/api/v1/projects/options")) return { items: [{ alias: "available", enabled: true }], nextCursor: null };
  const query = new URL(path, "http://localhost").searchParams;
  if (query.get("mode") === "options") {
    const result = { models: [{ id: "available-model", display_name: "Available model", default_effort_id: "medium",
      default_service_tier_id: "default", efforts: [{ id: "medium" }], service_tiers: [{ id: "default", name: "Standard" }] }],
    session_settings: { ...settings, turn_settings: null, message_context_mode: "current-only" },
    model_catalog_error: null, context_mode_available: contextAvailable };
    if (pendingOptions) return pauseRequest(pendingOptions, result);
    return result;
  }
  if (query.get("mode") === "view") {
    const result = { exact: viewExact, effective: viewExact || viewEffective,
      chat_kind: "group", match_error: null, actions: { create: envelope("create", "chat") } };
    return pendingView ? pauseRequest(pendingView, result) : result;
  }
  const kind = query.get("kind");
  return { items: structuredClone(kind === "chat" ? [exact] : rules), has_more: kind === "chat", offset: Number(query.get("offset") || 0),
    order_revision: 7, actions: { create: envelope("create", kind), reorder: envelope("reorder", "order") } };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
// SHIPPED_DEFAULTS_CONTROLLER
(async () => {
  await loadDefaults();
  assert.equal(defaultsInput("rules-body").querySelectorAll("tr").length, 3);
  const first = defaultsInput("rules-body").querySelector("tr");
  assert(first.textContent.includes("<b>first</b>"));
  assert.equal(first.querySelector("b"), null);
  assert(first.querySelectorAll("button")[2].disabled);
  assert.equal(defaultsInput("next").disabled, false);

  openDefaultEditor("chat", state.defaults.chat.items[0]);
  await flush();
  assert.equal(defaultsInput("project").value, "missing-project");
  assert.equal(defaultsInput("model").value, "retired");
  assert.equal(defaultsInput("effort").value, "high");
  assert.equal(defaultsInput("tier").value, "priority");
  assert(defaultsInput("note").textContent.includes("模型当前不可用"));
  assert(defaultsInput("chat").disabled);
  defaultsInput("progress").checked = false;
  changeDefaultSettings("progress");
  await saveDefault({ preventDefault() {} });
  const saved = posts.at(-1);
  assert.equal(saved.path, "/api/v1/defaults/save");
  assert.equal(saved.body.definition.project, "missing-project");
  assert.equal(saved.body.definition.chat_id, "oc_exact");
  assert.deepEqual(saved.body.definition.session_settings.turn_settings, settings.turn_settings);
  assert.equal(saved.body.definition.session_settings.progress_card_enabled, false);
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
  assert(status.includes("配置已变化"));
  assert(defaultsInput("save").disabled);
  await saveDefault({ preventDefault() {} });
  assert.equal(posts.length, beforeFailure + 1);
  failPost = false;
  closeDefaultEditor();

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
})().catch((error) => { console.error(error); process.exitCode = 1; });
