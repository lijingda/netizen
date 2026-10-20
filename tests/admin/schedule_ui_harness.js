const state = { tab: "schedules", schedules: null, sessionPage: { cursor: "sessions-page-three", number: 3 } };
function selectTab(name) { state.tab = name; }
function syncPageUrl() {}
function sessionSettingsHasChanges() { return false; }
function defaultEditorHasChanges() { return false; }
const windowListeners = new Map();
window.addEventListener = (name, callback) => windowListeners.set(name, callback);
const flush = () => new Promise((resolve) => setImmediate(resolve));
let status = "";
let statusError = false;
function setStatus(message, error = false) { status = message; statusError = error; }
let confirmation = true;
const confirmations = [];
window.confirm = (message) => { confirmations.push(message); return confirmation; };
const envelope = (mode) => ({
  csrfToken: `${mode}-csrf`, actionToken: `${mode}-once`,
  target: { resource: mode === "create" ? "schedule-registry" : "schedule", targetId: "plan-exact" },
});
let createGrant = envelope("create");
let chatValidationGate = null;
let scheduleOptionsGate = null;
const workspaceOptions = new Map(["project-one", "project-two"].map((project, index) => [project, {
  projectAlias: project, projectRevision: index + 1, isGit: true,
  items: [{ id: `wt-${project}`, cwd: `/managed/${project}`, branch: null, available: true }],
  branches: [{ ref: "refs/heads/main", name: "main" }],
}]));
const workspaceErrors = new Map();
let runRecords = null;
let unavailableProject = null;
let openTimeRangeFilter = null;
const defaultSessionSettings = { turn_settings: { model_id: "native-default", effort_id: "medium", service_tier_id: "default" },
  reaction_pulse_enabled: false, progress_card_enabled: false, completion_mention_enabled: true, message_context_mode: "current-only" };
const models = [{ id: "native-default", display_name: "Native default", default_effort_id: "medium", default_service_tier_id: "default",
  efforts: [{ id: "medium", description: "Medium" }, { id: "high", description: "High" }],
  service_tiers: [{ id: "default", name: "Standard" }, { id: "priority", name: "Fast" }] }];
let catalogError = null;
let chatInfoError = null;
let contextAvailable = true;
const fixturePlan = () => ({
  id: "plan-exact", revision: 7, name: "<b>Report</b>",
  project_alias: "project-one", chat_id: "oc_other",
  chat: { chatId: "oc_other", chatLabel: "<b>测试会话</b>", chatLabelResolved: true, chatOpenUrl: "https://applink.feishu.cn/client/chat/open?openChatId=oc_other" }, instructions: "<img src=x onerror=alert(1)>\nReport.",
  schedule: { kind: "daily", at: "09:00", timezone: "Asia/Shanghai" },
  next_due_local: "2030-01-01T09:00:00+08:00", enabled: true,
  lifecycle: { ended: false, has_future: true, has_trigger: true },
  execution: { kind: "none", status: "not_started", is_last: false, due_local: null },
  actions: { update: envelope("update"), delete: envelope("delete"), run_now: envelope("run_now") },
  session_settings: structuredClone(defaultSessionSettings),
  workspace: { mode: "project" }, workspace_error: null, workspace_available: true,
});
let plans = [fixturePlan()];
let posts = [];
let gets = [];
let respond = null;
let previewResolve = null;
let previewWait = false;
let ambiguous = false;
let ambiguousEnd = false;
let canonicalEndAt = null;
let previewTimes = null;
let previewError = null;
let chatValidationError = null;
let viewedPlan = null;
let failList = false;
let failView = false;
let failRuns = false;
const times = [{ utc: "2030-01-01T01:00:00Z", local: "2030-01-01T09:00:00+08:00" }];
async function api(path, options = {}) {
  if (options.method === "POST") {
    posts.push({ path, body: JSON.parse(options.body) });
    if (respond) return respond(path, options);
    return { ok: true, plan: plans[0] };
  }
  gets.push(path);
  const query = new URL(path, "http://localhost").searchParams;
  if (path.startsWith("/api/v1/chats/validate?")) {
    if (chatValidationError) throw chatValidationError;
    if (chatValidationGate) return new Promise((resolve) => { chatValidationGate.resolve = resolve; });
    return { chat: { chatId: query.get("chatId"), avatarUrl: "https://example.feishucdn.com/saved.jpg" } };
  }
  if (path.startsWith("/api/v1/chats?")) return { items: [{ chatId: "oc_picker", name: "研发群",
    avatarUrl: "https://example.feishucdn.com/schedule.jpg" }], nextCursor: null };
  if (path.startsWith("/api/v1/projects/options")) return {
    items: (query.has("cursor") ? [{ alias: "retired", enabled: false }] : [
      { alias: "project-one", enabled: true }, { alias: "project-two", enabled: true }])
      .filter((project) => project.alias !== unavailableProject),
    nextCursor: query.has("cursor") ? null : "project-page-two",
  };
  const mode = query.get("mode") || "list";
  if (mode === "options") {
    const result = { ok: true, session_settings: structuredClone(defaultSessionSettings),
      models: catalogError ? [] : structuredClone(models), context_mode_available: chatInfoError ? null : contextAvailable,
      chat_info_error: chatInfoError,
      model_catalog_error: catalogError, workspace_options: workspaceOptions.get(query.get("project")) || null,
      workspace_options_error: workspaceErrors.get(query.get("project")) || null };
    if (scheduleOptionsGate) return new Promise((resolve) => {
      scheduleOptionsGate.resolve = (overrides = {}) => resolve({ ...result, ...overrides });
    });
    return result;
  }
  if (mode === "preview") {
    if (previewError) throw previewError;
    const schedule = JSON.parse(query.get("schedule"));
    if (schedule.kind === "once" && ambiguous && !query.get("utc_offset")) {
      const error = new Error("所选时间会出现两次，请选择触发时刻。");
      error.code = "ambiguous_local_time";
      error.choices = [{ utc_offset: "-04:00" }, { utc_offset: "-05:00" }];
      throw error;
    }
    if (schedule.kind === "once") schedule.at = `${query.get("local_at")}${query.get("utc_offset") || "+08:00"}`;
    if (query.get("local_end_at")) {
      if (ambiguousEnd && !query.get("end_utc_offset")) {
        const error = new Error("截止时间会出现两次，请选择时刻。");
        error.code = "ambiguous_end_time";
        error.choices = [{ utc_offset: "-04:00" }, { utc_offset: "-05:00" }];
        throw error;
      }
      schedule.end_at = canonicalEndAt || `${query.get("local_end_at")}${query.get("end_utc_offset") || "+08:00"}`;
    }
    const result = { ok: true, schedule, preview: previewTimes || times };
    if (schedule.kind === "interval" && !schedule.anchor) schedule.anchor = 1893456000;
    if (previewWait) return new Promise((resolve) => { previewResolve = () => resolve(result); });
    return result;
  }
  if (mode === "view" && failView) throw new Error("详情刷新失败");
  if (mode === "runs" && failRuns) throw new Error("记录刷新失败");
  if (mode === "view") return { ok: true, plan: viewedPlan || fixturePlan(), preview: previewTimes || times, inflight: true };
  if (mode === "runs") return { ok: true, runs: runRecords || [
    { id: "run-one", binding_id: "binding-exact", due_at: 1893459600, status: "released", trigger_source: "manual",
      feishu_url: "https://applink.feishu.cn/client/message/link?token=om_root" },
  ], next_cursor: "runs-page" };
  if (failList) throw new Error("列表暂不可用");
  return { ok: true, plans: structuredClone(plans), next_cursor: "page-two",
    default_timezone: "Asia/Shanghai", actions: { create: createGrant } };
}
async function refresh() { await loadSchedules(); return true; }
// SHIPPED_SCHEDULE_CONTROLLER
const scheduleFilterRoots = new Map(scheduleInput("filter").querySelectorAll("[data-schedule-filter]")
  .map((root) => [root.dataset.scheduleFilter, root]));
const filterControl = (name, className) => scheduleFilterRoots.get(name).querySelector(`.${className}`);
const filterOption = (name, value) => scheduleFilterRoots.get(name).querySelectorAll("input")
  .find((input) => input.type === "radio" && input.value === value);
function chooseFilter(name, value) {
  if (filterControl(name, "multi-filter-popover").hidden) filterControl(name, "multi-filter-trigger").click();
  filterOption(name, value).click();
}
function clearFilter(name) {
  if (filterControl(name, "multi-filter-popover").hidden) filterControl(name, "multi-filter-trigger").click();
  filterControl(name, "multi-filter-clear").click();
}
async function openNewScheduleEditor() {
  await openScheduleEditor();
  scheduleInput("name").value = "Test schedule";
  scheduleInput("instructions").value = "Test instructions";
  scheduleInput("project").value = "project-one";
  chatTarget("schedule").set({ chatId: "oc_test", chat: { chatMode: "group", chatLabel: "测试群" } });
}
async function chooseScheduleTarget(value) {
  const inputs = scheduleInput("target-kind").querySelectorAll("input");
  for (const input of inputs) input.checked = input.value === value;
  const selected = inputs.find((input) => input.checked);
  selected.dispatch("input");
  selected.dispatch("change");
  await flush();
}
(async () => {
  await loadSchedules();
  const rows = document.querySelector("#schedules-body").querySelectorAll("tr");
  assert.equal(rows.length, 1);
  assert(rows[0].textContent.includes("<b>Report</b>"));
  assert.equal(rows[0].querySelector("b"), null);
  assert.equal(document.querySelector("#schedules-next").hidden, false);
  const chatLink = rows[0].querySelector("a");
  assert.equal(chatLink.textContent, "<b>测试会话</b>");
  assert.equal(chatLink.querySelector("b"), null);
  assert.equal(new URL(chatLink.href).searchParams.get("openChatId"), "oc_other");
  assert.deepEqual([...new FormData(scheduleInput("filter"))], [["project", ""], ["enabled", ""], ["ended", "false"]]);
  const defaultList = new URL(gets.find((path) => path.startsWith("/api/v1/schedules?")), "http://localhost").searchParams;
  assert.equal(defaultList.get("ended"), "false");
  assert.equal(defaultList.get("enabled"), null);
  assert(rows[0].textContent.includes("启停：已启用"));
  assert(rows[0].textContent.includes("结束：未结束"));
  assert(rows[0].textContent.includes("尚未执行"));
  assert(!rows[0].textContent.includes("plan-exact"));
  assert(rows[0].textContent.includes("下次：2030-01-01 09:00+08:00"));
  assert.equal(scheduleLocalTime("2030-01-01T09:00+08:00"), "2030-01-01 09:00+08:00");
  for (const code of ["publishing_failed", "scope_conflict", "dispatch_rejected", "deleted"]) {
    assert.notEqual(scheduleRunStatus(code), "结果暂不可用");
  }

  // Scheduled filters reuse the searchable dropdown while retaining single-value drafts.
  const projectTrigger = filterControl("project", "multi-filter-trigger");
  const projectSearch = filterControl("project", "multi-filter-search");
  const requestsBeforeFilter = gets.length;
  projectTrigger.click();
  assert.equal(projectTrigger.type, "button");
  assert.equal(projectTrigger.getAttribute("aria-expanded"), "true");
  assert.equal(document.activeElement, projectSearch);
  projectSearch.value = "RETIRED";
  projectSearch.dispatch("input");
  assert.equal(filterOption("project", "project-one").parentElement.hidden, true);
  assert.equal(filterOption("project", "retired").parentElement.hidden, false);
  assert(filterOption("project", "retired").parentElement.textContent.includes("已停用"));
  assert(projectSearch.dispatch("keydown", { key: "Enter" }).prevented);
  filterOption("project", "retired").click();
  assert.equal(scheduleInput("filter-project").value, "retired");
  assert.equal(filterControl("project", "multi-filter-popover").hidden, true);
  assert.equal(document.activeElement, projectTrigger);
  chooseFilter("project", "project-one");
  assert.equal(filterOption("project", "retired").checked, false);
  assert.equal(filterOption("project", "project-one").checked, true);
  assert.deepEqual([...new FormData(scheduleInput("filter"))].filter(([name]) => name === "project"),
    [["project", "project-one"]]);
  projectTrigger.click();
  projectSearch.value = "no such project";
  projectSearch.dispatch("input");
  assert.equal(filterControl("project", "multi-filter-empty").hidden, false);
  assert(projectSearch.dispatch("keydown", { key: "Escape" }).prevented);
  assert.equal(document.activeElement, projectTrigger);
  assert.equal(scheduleInput("filter-project").value, "project-one");
  assert.equal(gets.length, requestsBeforeFilter, "filter choices and search do not apply the draft");

  // Refreshing options preserves a removed Project until explicitly cleared.
  unavailableProject = "project-one";
  await loadSchedules();
  assert.equal(scheduleInput("filter-project").value, "project-one");
  assert(filterOption("project", "project-one").parentElement.textContent.includes("已不可用"));
  unavailableProject = null;
  clearFilter("project");
  assert.equal(scheduleInput("filter-project").value, "");
  assert.equal(filterControl("project", "multi-filter-summary").textContent, "全部");
  clearFilter("ended");
  await loadSchedules(null, null);
  const clearedQuery = new URL(gets.filter((path) => path.startsWith("/api/v1/schedules?")).at(-1), "http://localhost").searchParams;
  assert.equal(clearedQuery.get("project"), null);
  assert.equal(clearedQuery.get("ended"), null, "clear means all, not the initial unfinished filter");
  chooseFilter("ended", "false");
  assert.equal(filterControl("ended", "multi-filter-summary").textContent, "未结束");
  projectTrigger.click();
  filterControl("enabled", "multi-filter-trigger").dispatch("keydown", { key: "ArrowDown" });
  assert.equal(filterControl("project", "multi-filter-popover").hidden, true);
  assert.equal(document.activeElement, filterOption("enabled", "true"));
  filterOption("enabled", "true").dispatch("keydown", { key: "ArrowDown" });
  assert.equal(document.activeElement, filterOption("enabled", "false"));
  assert(filterOption("enabled", "false").dispatch("keydown", { key: "Enter" }).prevented);
  assert.equal(scheduleInput("filter-enabled").value, "false");
  assert.equal(filterControl("enabled", "multi-filter-popover").hidden, true);
  assert.equal(document.activeElement, filterControl("enabled", "multi-filter-trigger"));
  clearFilter("enabled");
  filterControl("enabled", "multi-filter-trigger").click();
  document.body.dispatch("pointerdown");
  assert.equal(filterControl("enabled", "multi-filter-popover").hidden, true);

  // Independent enablement/completion and exact current execution control row actions.
  for (const scenario of [
    { enabled: false, ended: false, trigger: true, kind: "none", status: "not_started", label: "尚未执行", toggle: "启用" },
    { enabled: true, ended: true, trigger: false, kind: "latest", status: "completed", label: "上次已完成" },
    { enabled: false, ended: true, trigger: false, kind: "none", status: "expired", label: "已过期，未执行" },
    { enabled: true, ended: false, trigger: false, kind: "current", status: "inProgress", label: "最后一次执行中" },
    { enabled: false, ended: false, trigger: false, kind: "current", status: "starting", label: "最后一次启动中" },
    { enabled: true, ended: false, trigger: false, kind: "current", status: "unknown", label: "最后一次结果待确认" },
    { enabled: true, ended: false, trigger: true, kind: "latest", status: "failed", label: "上次失败", toggle: "暂停" },
    { enabled: true, ended: true, trigger: false, kind: "latest", status: "deleted", label: "执行会话已删除" },
  ]) {
    const plan = fixturePlan();
    plan.enabled = scenario.enabled;
    // An unprocessed stored cursor may remain after its grace period expires.
    plan.next_due_local = scenario.enabled ? plan.next_due_local : null;
    plan.lifecycle = { ended: scenario.ended, has_future: scenario.trigger, has_trigger: scenario.trigger };
    plan.execution = { kind: scenario.kind, status: scenario.status, is_last: !scenario.trigger,
      due_local: scenario.kind === "none" ? null : "2030-01-01T09:00:00+08:00" };
    plan.blocked_reason = "project_disabled";
    plan.chat.chatLabelResolved = false;
    plan.chat.chatLabel = "?";
    plans = [plan];
    await loadSchedules();
    const row = document.querySelector("#schedules-body").querySelector("tr");
    assert.equal(row.querySelectorAll("td").length, 7);
    assert(row.textContent.includes(`启停：${scenario.enabled ? "已启用" : "已暂停"}`));
    assert(row.textContent.includes(`结束：${scenario.ended ? "已结束" : "未结束"}`));
    assert(row.textContent.includes(scenario.label));
    if (!scenario.trigger) {
      assert(row.textContent.includes("无后续触发"));
      assert(!row.textContent.includes("下次："));
    }
    assert(row.textContent.includes("Project 已停用"));
    assert.equal(row.querySelector("a").textContent, "会话名称暂不可用");
    const buttons = row.querySelectorAll("button").map((button) => button.textContent);
    assert(buttons.includes("编辑") && buttons.includes("删除"));
    assert.deepEqual(buttons.filter((label) => ["启用", "暂停"].includes(label)), scenario.toggle ? [scenario.toggle] : []);
  }
  chooseFilter("ended", "true");
  chooseFilter("enabled", "false");
  plans = [];
  await loadSchedules(null, null);
  const emptyQuery = new URL(gets.filter((path) => path.startsWith("/api/v1/schedules?")).at(-1), "http://localhost").searchParams;
  assert.equal(emptyQuery.get("ended"), "true");
  assert.equal(emptyQuery.get("enabled"), "false");
  assert(document.querySelector("#schedules-body").textContent.includes("没有符合筛选条件"));
  chooseFilter("ended", "false");
  clearFilter("enabled");
  plans = [fixturePlan()];
  await loadSchedules(null, null);
  assert.deepEqual(scheduleFilterRoots.get("project").querySelectorAll("input")
    .filter((input) => input.type === "radio").map((input) => input.value),
    ["project-one", "project-two", "retired"]);
  chooseFilter("project", "project-one");
  await loadSchedules(null, null);
  await loadSchedules("page-two");
  chooseFilter("project", "project-two");
  assert(gets.some((path) => path.startsWith("/api/v1/schedules?") && new URL(path, "http://localhost").searchParams.get("cursor") === "page-two"));

  await openNewScheduleEditor();
  assert.equal(scheduleInput("timezone").value, "Asia/Shanghai");
  assert.equal(scheduleInput("save").disabled, true);
  assert.equal(scheduleInput("editor").hidden, false);
  assert.equal(scheduleInput("drawer").open, true);
  assert.equal(scheduleInput("session-settings").open, true);
  assert.equal(scheduleInput("end-details").open, false);
  assert.equal(scheduleInput("target-note").hidden, true);
  assert.equal(scheduleInput("binding-field").hidden, true);
  assert.equal(scheduleTargetKind(), "new_topic");
  assert.equal(document.activeElement, scheduleInput("name"));
  assert.equal(scheduleInput("effort").querySelectorAll("option")[0].textContent, "medium");
  assert.deepEqual(scheduleInput("project").querySelectorAll("option").map((o) => o.value),
    ["", "project-one", "project-two"]);
  scheduleInput("name").value = "My report";
  scheduleInput("project").value = "project-two";
  scheduleInput("chat").value = "oc_explicit";
  scheduleInput("instructions").value = "One.\nTwo.";
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, false);
  assert(scheduleInput("preview-times").textContent.includes("+08:00"));

  // Editing a rule invalidates preview; a late response cannot approve a changed rule.
  previewWait = true;
  const late = previewSchedule();
  scheduleInput("time").value = "10:00";
  invalidateSchedulePreview();
  previewResolve();
  await late;
  assert.equal(scheduleInput("save").disabled, true);
  assert.equal(schedulePrepared, null);
  previewWait = false;
  await previewSchedule();
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, "/api/v1/schedules/create");
  assert.deepEqual(posts[0].body, actionPayload(envelope("create"), { definition: {
    workspace: { mode: "project" },
    name: "My report", project: "project-two", chat_id: "oc_explicit", instructions: "One.\nTwo.",
    schedule: { kind: "daily", timezone: "Asia/Shanghai", at: "10:00" }, enabled: true,
    session_settings: defaultSessionSettings,
  } }));
  assert.equal(scheduleInput("editor").hidden, true);
  assert.equal(scheduleInput("drawer").open, false);
  assert.equal(scheduleInput("filter-project").value, "project-two");
  assert.equal(scheduleListCursor, "page-two");
  assert.equal(new URLSearchParams(scheduleListQuery).get("project"), "project-one");
  const pageAfterSave = new URL(gets.filter((path) => path.startsWith("/api/v1/schedules?")).at(-1), "http://localhost").searchParams;
  assert.equal(pageAfterSave.get("project"), "project-one");
  assert.equal(pageAfterSave.get("cursor"), "page-two");

  // Escape/cancel discards the form without changing the list or writing a plan.
  const beforeCancel = posts.length;
  scheduleInput("new").focus();
  await openNewScheduleEditor();
  scheduleInput("name").value = "Discard this draft";
  scheduleInput("drawer").dispatch("cancel");
  assert.equal(scheduleInput("drawer").open, false);
  assert.equal(posts.length, beforeCancel);
  assert.equal(document.activeElement, scheduleInput("new"));
  assert.equal(scheduleInput("filter-project").value, "project-two");

  // All four rules serialize their own fields, including an exact previewed interval anchor.
  await openNewScheduleEditor();
  scheduleInput("kind").value = "weekly";
  scheduleInput("weekdays").querySelectorAll("input")[0].checked = true;
  scheduleInput("weekdays").querySelectorAll("input")[4].checked = true;
  invalidateSchedulePreview();
  assert.deepEqual(readScheduleRule(), { kind: "weekly", timezone: "Asia/Shanghai", at: "09:00", weekdays: [0, 4] });
  assert.equal(scheduleInput("weekdays").hidden, false);
  scheduleInput("kind").value = "once";
  scheduleInput("at").value = "2030-01-01T09:00";
  assert.deepEqual(readScheduleRule(), { kind: "once", timezone: "Asia/Shanghai", at: "2030-01-01T09:00" });
  await previewSchedule();
  let onceQuery = new URL(gets.at(-1), "http://localhost").searchParams;
  assert.equal(onceQuery.get("local_at"), "2030-01-01T09:00");
  assert(!("at" in JSON.parse(onceQuery.get("schedule"))));
  assert.equal(schedulePrepared.schedule.at, "2030-01-01T09:00+08:00");

  // Only DST ambiguity asks for an offset; changing date/timezone clears that choice.
  ambiguous = true;
  scheduleInput("timezone").value = "America/New_York";
  scheduleInput("at").value = "2030-11-03T01:30";
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, true);
  assert.equal(scheduleInput("offset-field").hidden, false);
  assert.equal(scheduleInput("offset").getAttribute("aria-invalid"), "true");
  assert.equal(document.activeElement, scheduleInput("offset"));
  scheduleInput("offset").value = "-05:00";
  scheduleInput("offset").dispatch("input");
  await previewSchedule();
  assert.equal(schedulePrepared.schedule.at, "2030-11-03T01:30-05:00");
  scheduleInput("at").value = "2030-11-04T01:30";
  scheduleInput("at").dispatch("input");
  assert.equal(scheduleInput("offset-field").hidden, true);
  assert.equal(scheduleInput("offset").value, "");
  assert.equal(scheduleInput("save").disabled, true);
  ambiguous = false;
  scheduleInput("timezone").value = "Asia/Shanghai";
  scheduleInput("kind").value = "interval";
  scheduleInput("every").value = "90";
  await previewSchedule();
  assert.equal(schedulePrepared.schedule.anchor, 1893456000);
  assert.equal(schedulePrepared.schedule.every_minutes, 90);
  const existingInterval = fixturePlan();
  existingInterval.schedule = { kind: "interval", timezone: "Asia/Shanghai", every_minutes: 60, anchor: 1893448800 };
  await openScheduleEditor(existingInterval);
  scheduleInput("every").value = "120";
  await previewSchedule();
  assert.equal(schedulePrepared.schedule.anchor, 1893448800);
  assert.equal(schedulePrepared.schedule.every_minutes, 120);

  const secondOccurrence = fixturePlan();
  secondOccurrence.schedule = { kind: "once", timezone: "America/New_York", at: "2030-11-03T06:30+00:00" };
  secondOccurrence.once_local_at = "2030-11-03T01:30";
  secondOccurrence.project_alias = "retired";
  await openScheduleEditor(secondOccurrence);
  assert.equal(scheduleInput("at").value, "2030-11-03T01:30");
  assert.equal(scheduleInput("project").value, "retired");
  assert.match(scheduleInput("project").textContent, /已停用/);

  // Edit obtains the latest exact revision grant; saves don't use an active group default.
  await editSchedule(state.schedules.plans[0]);
  assert.equal(scheduleInput("project").value, "project-one");
  assert.equal(scheduleInput("chat").value, "oc_other");
  assert.equal(scheduleInput("instructions").value, fixturePlan().instructions);
  await previewSchedule();
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts[1].path, "/api/v1/schedules/update");
  assert.deepEqual(posts[1].body.target, envelope("update").target);
  assert.equal(posts[1].body.definition.instructions, fixturePlan().instructions);

  const plan = state.schedules.plans[0];
  await changeSchedule(plan, "update");
  assert.deepEqual(posts.at(-1).body.definition, { enabled: false });
  assert.equal(status, "计划已暂停。");
  confirmation = false;
  const count = posts.length;
  await changeSchedule(state.schedules.plans[0], "delete");
  assert.equal(posts.length, count);
  assert.match(confirmations.at(-1), /已认领.*继续.*历史话题.*保留/);

  // Duplicate clicks while the exact mutation is outstanding stay single-use.
  confirmation = true;
  let releaseDelete;
  respond = () => new Promise((resolve) => { releaseDelete = resolve; });
  const deleting = changeSchedule(state.schedules.plans[0], "delete");
  await changeSchedule(state.schedules.plans[0], "delete");
  assert.equal(posts.length, count + 1);
  releaseDelete({ ok: true, id: "plan-exact" });
  await deleting;
  respond = null;
  assert.match(status, /历史会话保留/);
  assert.equal(statusError, false);

  await showSchedule("plan-exact");
  assert.equal(scheduleInput("detail").hidden, false);
  assert.equal(scheduleInput("detail-instructions").textContent, fixturePlan().instructions);
  assert.equal(scheduleInput("detail-instructions").querySelector("img"), null);
  assert.match(scheduleInput("detail-state").textContent, /不会取消/);
  const links = scheduleInput("runs-body").querySelectorAll("a");
  assert.equal(links.length, 2);
  assert(links[0].href.startsWith("https://applink.feishu.cn/"));
  assert.equal(new URL(links[1].href, "http://localhost").searchParams.get("identity"), "binding-exact");
  assert.equal(scheduleInput("runs-next").hidden, false);

  // A failed save keeps user input and makes its spent action unusable.
  await editSchedule(state.schedules.plans[0]);
  await previewSchedule();
  respond = () => { throw new Error("计划已变化，请刷新。"); };
  const beforeFailure = posts.length;
  await saveSchedule({ preventDefault() {} });
  assert.equal(scheduleInput("instructions").value, fixturePlan().instructions);
  assert.equal(scheduleInput("save").disabled, true);
  assert.match(scheduleInput("form-error").textContent, /请刷新列表并重新打开编辑/);
  assert.equal(scheduleInput("form-error").hidden, false);
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, true);
  assert.match(scheduleInput("form-error").textContent, /请刷新列表并重新打开编辑/);
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.length, beforeFailure + 1);
  assert.match(status, /已变化/);
  assert.equal(statusError, true);

  // Submission cannot be dismissed/reopened or submitted twice while pending.
  respond = null;
  await openNewScheduleEditor();
  await previewSchedule();
  let finishSave;
  respond = () => new Promise((resolve) => { finishSave = resolve; });
  const pendingSave = saveSchedule({ preventDefault() {} });
  await flush();
  const pendingCount = posts.length;
  scheduleInput("drawer").dispatch("cancel");
  closeScheduleEditor();
  assert.equal(scheduleInput("drawer").open, true);
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.length, pendingCount);
  finishSave({ ok: true });
  await pendingSave;
  assert.equal(scheduleInput("drawer").open, false);

  // A successful save followed by a failed refresh must stay visibly successful.
  respond = null;
  await openNewScheduleEditor();
  await previewSchedule();
  failList = true;
  await saveSchedule({ preventDefault() {} });
  assert.match(status, /计划已保存.*列表刷新失败/);
  assert.equal(scheduleInput("drawer").open, false);
  assert.equal(document.activeElement, scheduleInput("new"));
  failList = false;

  // The native options select supplies defaults and combinations; feedback
  // and context remain independent per-occurrence choices.
  respond = null;
  await openNewScheduleEditor();
  assert.equal(scheduleInput("session-settings").open, true);
  assert.equal(scheduleInput("model").value, "native-default");
  assert.equal(scheduleInput("effort").value, "medium");
  scheduleInput("effort").value = "high";
  changeScheduleSessionSettings("effort");
  scheduleInput("tier").value = "priority";
  changeScheduleSessionSettings("tier");
  scheduleInput("reactions").checked = true;
  changeScheduleSessionSettings("reactions");
  scheduleInput("progress").checked = true;
  changeScheduleSessionSettings("progress");
  scheduleInput("completion-mention").checked = false;
  changeScheduleSessionSettings("completion-mention");
  scheduleInput("context").value = "catch-up";
  changeScheduleSessionSettings("context");
  assert.equal(scheduleInput("save").disabled, true);
  await previewSchedule();
  const previewQuery = new URL(gets.at(-1), "http://localhost").searchParams;
  const selectedSettings = { turn_settings: { model_id: "native-default", effort_id: "high", service_tier_id: "priority" },
    reaction_pulse_enabled: true, progress_card_enabled: true, completion_mention_enabled: false, message_context_mode: "catch-up" };
  assert.deepEqual(JSON.parse(previewQuery.get("session_settings")), selectedSettings);
  await saveSchedule({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.session_settings, selectedSettings);

  // Binding targeting now delegates to Sessions without an independent list lookup.
  await openNewScheduleEditor();
  assert.equal(scheduleInput("model").value, "native-default");
  assert.equal(scheduleInput("effort").disabled, false);
  assert.equal(scheduleInput("session-note").textContent, "");
  assert.deepEqual(scheduleSessionPatch(), defaultSessionSettings);
  scheduleInput("effort").value = "high";
  changeScheduleSessionSettings("effort");
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, false);
  assert.equal(JSON.parse(new URL(gets.at(-1), "http://localhost").searchParams.get("session_settings")).turn_settings.effort_id, "high");
  await chooseScheduleTarget("binding");
  assert.equal(scheduleInput("save").disabled, true);
  assert.equal(scheduleInput("target-note").hidden, false);
  assert.equal(scheduleInput("select-session").hidden, false);
  assert.match(scheduleInput("binding-summary").textContent, /Sessions/);
  assert.equal(scheduleInput("binding").value, "");
  assert.equal(scheduleInput("preview").disabled, true);
  assert.equal(scheduleInput("effort").value, "high");
  await chooseScheduleTarget("new_topic");
  assert.equal(scheduleInput("target-note").hidden, true);
  assert.equal(scheduleInput("binding-field").hidden, true);
  assert.equal(scheduleInput("session-settings").hidden, false);
  assert.equal(scheduleInput("session-settings").open, true);
  assert.equal(scheduleInput("project").required, true);
  assert.equal(scheduleInput("chat-target").hidden, false);
  closeScheduleEditor();

  // Catalog failure or a retired model never replaces a persisted setting.
  const retired = fixturePlan();
  retired.session_settings.turn_settings = { model_id: "retired-model", effort_id: "max", service_tier_id: "old-tier" };
  catalogError = { code: "model_catalog_unavailable", message: "目录暂不可用。" };
  await openScheduleEditor(retired);
  assert.equal(scheduleInput("model").value, "retired-model");
  assert.equal(scheduleInput("effort").value, "max");
  assert.equal(scheduleInput("tier").value, "old-tier");
  assert.equal(scheduleInput("effort").disabled, true);
  assert.match(scheduleInput("session-note").textContent, /配置保持不变/);
  assert.deepEqual(scheduleSessionPatch(), {});
  scheduleInput("progress").checked = true;
  changeScheduleSessionSettings("progress");
  assert.deepEqual(scheduleSessionPatch(), { progress_card_enabled: true });
  await previewSchedule();
  await saveSchedule({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.session_settings, { progress_card_enabled: true });
  await changeSchedule(state.schedules.plans[0], "update");
  assert.deepEqual(posts.at(-1).body.definition, { enabled: false });
  await changeSchedule(state.schedules.plans[0], "delete");
  assert.equal(posts.at(-1).path, "/api/v1/schedules/delete");

  // A user can explicitly return to inherited settings, even without catalog.
  await openScheduleEditor(retired);
  scheduleInput("model").value = "";
  changeScheduleSessionSettings("model");
  assert.deepEqual(scheduleSessionPatch(), { turn_settings: null });
  catalogError = null;
  contextAvailable = false;
  await openScheduleEditor(fixturePlan());
  assert.equal(scheduleInput("context").value, "current-only");
  assert(scheduleInput("context").querySelectorAll("option").find((option) => option.value === "catch-up").disabled);
  assert.match(scheduleInput("session-note").textContent, /私聊.*不支持/);

  // Recurring deadlines are canonicalized by the backend and shown in summaries.
  contextAvailable = true;
  const deadlinePlan = fixturePlan();
  deadlinePlan.schedule = { ...deadlinePlan.schedule, end_at: "2030-03-01T18:00+08:00" };
  deadlinePlan.end_local_at = "2030-03-01T18:00";
  plans = [deadlinePlan];
  viewedPlan = deadlinePlan;
  await loadSchedules();
  const deadlineRow = document.querySelector("#schedules-body").querySelector("tr");
  assert.match(deadlineRow.textContent, /截止 2030-03-01 18:00\+08:00（含）/);
  await showSchedule(deadlinePlan.id);
  assert.match(scheduleInput("detail-rule").textContent, /截止 2030-03-01 18:00\+08:00（含）/);
  await openScheduleEditor(deadlinePlan);
  assert.equal(scheduleInput("end-at").value, deadlinePlan.end_local_at);
  assert.equal(scheduleInput("end-details").open, true);
  previewTimes = [times[0]]; // The service owns the cutoff calculation.
  await previewSchedule();
  let deadlineQuery = new URL(gets.at(-1), "http://localhost").searchParams;
  assert.equal(deadlineQuery.get("local_end_at"), deadlinePlan.end_local_at);
  assert.equal(deadlineQuery.get("plan_id"), deadlinePlan.id);
  assert(!("end_at" in JSON.parse(deadlineQuery.get("schedule"))));
  assert.equal(schedulePrepared.schedule.end_at, deadlinePlan.schedule.end_at);
  assert.equal(scheduleInput("preview-times").querySelectorAll("li").length, 1);
  const beforeCollapse = { gets: gets.length, posts: posts.length, prepared: schedulePrepared };
  scheduleInput("end-details").open = false;
  scheduleInput("end-details").dispatch("toggle");
  assert.equal(scheduleInput("end-at").value, deadlinePlan.end_local_at);
  assert.equal(scheduleInput("end-condition").disabled, false);
  assert.equal(scheduleInput("save").disabled, false);
  assert.equal(schedulePrepared, beforeCollapse.prepared);
  assert.equal(gets.length, beforeCollapse.gets);
  assert.equal(posts.length, beforeCollapse.posts);
  await saveSchedule({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.schedule, deadlinePlan.schedule);

  // Clearing the deadline replaces the rule without inheriting the old value.
  await openScheduleEditor(deadlinePlan);
  scheduleInput("end-at").value = "";
  scheduleInput("end-at").dispatch("input");
  await previewSchedule();
  deadlineQuery = new URL(gets.at(-1), "http://localhost").searchParams;
  assert.equal(deadlineQuery.get("local_end_at"), null);
  assert(!("end_at" in schedulePrepared.schedule));
  await saveSchedule({ preventDefault() {} });
  assert(!("end_at" in posts.at(-1).body.definition.schedule));

  // A deadline has its own DST ambiguity choice and does not borrow once offsets.
  await openNewScheduleEditor();
  scheduleInput("timezone").value = "America/New_York";
  scheduleInput("end-at").value = "2030-11-03T01:30";
  scheduleInput("end-details").open = false;
  ambiguousEnd = true;
  await previewSchedule();
  assert.equal(scheduleInput("end-details").open, true, "deadline ambiguity choices must be visible even after collapsing");
  assert.equal(scheduleInput("end-offset-field").hidden, false);
  assert.equal(scheduleInput("offset-field").hidden, true);
  assert.equal(scheduleInput("save").disabled, true);
  scheduleInput("end-offset").value = "-05:00";
  scheduleInput("end-offset").dispatch("input");
  await previewSchedule();
  deadlineQuery = new URL(gets.at(-1), "http://localhost").searchParams;
  assert.equal(deadlineQuery.get("end_utc_offset"), "-05:00");
  assert.equal(deadlineQuery.get("utc_offset"), null);
  assert.equal(schedulePrepared.schedule.end_at, "2030-11-03T01:30-05:00");
  scheduleInput("end-at").value = "2030-11-04T01:30";
  scheduleInput("end-at").dispatch("input");
  assert.equal(scheduleInput("end-offset-field").hidden, true);
  assert.equal(scheduleInput("end-offset").value, "");
  assert.equal(schedulePrepared, null);
  ambiguousEnd = false;

  // An unchanged deadline keeps the exact canonical instant supplied by the server.
  const secondEnd = { ...deadlinePlan,
    schedule: { ...deadlinePlan.schedule, timezone: "America/New_York", end_at: "2030-11-03T06:30+00:00" },
    end_local_at: "2030-11-03T01:30" };
  await openScheduleEditor(secondEnd);
  canonicalEndAt = secondEnd.schedule.end_at;
  await previewSchedule();
  assert.equal(schedulePrepared.schedule.end_at, secondEnd.schedule.end_at);
  assert.equal(new URL(gets.at(-1), "http://localhost").searchParams.get("end_utc_offset"), null);
  canonicalEndAt = null;

  // Changing the deadline rejects a late preview; once rules omit and disable it.
  previewWait = true;
  const staleDeadline = previewSchedule();
  scheduleInput("end-at").value = "2030-11-05T01:30";
  scheduleInput("end-at").dispatch("input");
  previewResolve();
  await staleDeadline;
  assert.equal(schedulePrepared, null);
  assert.equal(scheduleInput("save").disabled, true);
  previewWait = false;
  scheduleInput("kind").value = "once";
  scheduleInput("kind").dispatch("input");
  scheduleInput("at").value = "2030-11-04T09:00";
  assert.equal(scheduleInput("end-condition").hidden, true);
  assert.equal(scheduleInput("end-condition").disabled, true);
  assert(!("end_at" in readScheduleRule()));
  await previewSchedule();
  deadlineQuery = new URL(gets.at(-1), "http://localhost").searchParams;
  assert.equal(deadlineQuery.get("local_end_at"), null);
  assert.equal(deadlineQuery.get("end_utc_offset"), null);

  // Exhausted plans still allow metadata updates and deliberately ending the rule.
  const exhausted = { ...deadlinePlan,
    lifecycle: { ended: true, has_future: false, has_trigger: false } };
  previewTimes = [];
  await openScheduleEditor(exhausted);
  scheduleInput("instructions").value = "Updated instructions after the final execution.";
  scheduleInput("instructions").dispatch("input");
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, false);
  assert.match(scheduleInput("preview-message").textContent, /没有后续.*仍可保存/);
  await saveSchedule({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.schedule, exhausted.schedule);
  assert.equal(posts.at(-1).body.definition.instructions, "Updated instructions after the final execution.");
  assert.match(status, /已保存计划.*没有后续触发/);
  await openScheduleEditor(deadlinePlan);
  scheduleInput("end-at").value = "2029-12-01T18:00";
  scheduleInput("end-at").dispatch("input");
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, false);
  assert.equal(schedulePrepared.schedule.end_at, "2029-12-01T18:00+08:00");
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.at(-1).body.definition.schedule.end_at, "2029-12-01T18:00+08:00");
  await openNewScheduleEditor();
  assert.equal(scheduleInput("end-at").value, "");
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, true);

  // Manual execution stays available for paused/ended plans and displays its
  // source independently from enablement, due time, and execution status.
  closeScheduleEditor();
  for (const scenario of [
    { enabled: true, ended: false }, { enabled: false, ended: false },
    { enabled: false, ended: true }, { enabled: true, ended: true },
    { runNow: false }, { runNow: false, inflight: true }, { runNow: false, blocked_reason: "blocked_unknown" },
    { runNow: false, blocked_reason: "project_disabled" }, { runNow: false, blocked_reason: "project_unavailable" },
  ]) {
    const manualPlan = { ...fixturePlan(), ...scenario };
    if (scenario.runNow === false) manualPlan.actions.run_now = null;
    manualPlan.lifecycle = { ended: scenario.ended || false, has_future: !scenario.ended, has_trigger: !scenario.ended };
    manualPlan.execution = { kind: "latest", status: "completed", trigger_source: "manual" };
    plans = [manualPlan];
    await loadSchedules();
    const row = document.querySelector("#schedules-body").querySelector("tr");
    const button = row.querySelectorAll("button").find((item) => item.textContent === "立即运行");
    assert(button);
    assert.equal(button.disabled, scenario.runNow === false);
    assert.match(row.querySelector(".schedule-execution").textContent, /手动触发/);
    assert.match(row.querySelector(".schedule-execution").textContent, /上次已完成/);
    const before = posts.length;
    if (button.disabled) await runSchedule(state.schedules.plans[0], button);
    assert.equal(posts.length, before);
  }
  assert.equal(scheduleTriggerSource("scheduled"), "定时触发");
  // The action grant governs availability even when display metadata differs.
  plans = [{ ...fixturePlan(), inflight: true, blocked_reason: "target_inactive" }];
  viewedPlan = fixturePlan();
  viewedPlan.execution = { kind: "current", status: "starting", trigger_source: "manual" };
  await showSchedule("plan-exact");
  assert.match(scheduleInput("detail-state").textContent, /手动触发/);
  assert.match(scheduleInput("runs-body").textContent, /手动触发/);

  // One click starts an exact saved plan, without editing or confirmation.
  // Duplicate clicks cannot race the outstanding request or its consumed grant.
  await loadSchedules();
  const manualPlan = state.schedules.plans[0];
  const manualButton = document.querySelector("#schedules-body").querySelectorAll("button")
    .find((item) => item.textContent === "立即运行");
  assert.equal(manualButton.disabled, false);
  assert.match(manualButton.title, /原会话已切走或归档/);
  const beforeManual = posts.length;
  const beforeConfirm = confirmations.length;
  let releaseRun;
  respond = () => new Promise((resolve) => { releaseRun = resolve; });
  const running = runSchedule(manualPlan, manualButton);
  assert.equal(manualButton.disabled, true);
  await runSchedule(manualPlan, manualButton);
  assert.equal(posts.length, beforeManual + 1);
  assert.equal(confirmations.length, beforeConfirm);
  assert.deepEqual(posts.at(-1), { path: "/api/v1/schedules/run-now", body: actionPayload(envelope("run_now")) });
  releaseRun({ ok: true, accepted: true, plan_id: "plan-exact", revision: 7,
    run_id: "manual-exact", replayed: false,
    run: { status: "starting", trigger_source: "manual", feishu_url: "https://applink.feishu.cn/client/message/link?token=om_manual" } });
  await running;
  assert.match(status, /已受理.*手动运行.*原定时安排保持不变/);
  assert(!status.includes("已完成"));
  assert.equal(scheduleInput("run-receipt").hidden, false);
  assert.match(scheduleInput("run-receipt").textContent, /manual-exact/);
  assert.equal(scheduleInput("run-receipt").querySelector("a").href,
    "https://applink.feishu.cn/client/message/link?token=om_manual");
  assert.equal(scheduleInput("run-receipt").querySelector("b"), null);

  // Accepted work remains visibly accepted even if refreshing the list fails.
  failList = true;
  respond = () => ({ ok: true, accepted: true, run_id: "accepted-before-refresh", run: null });
  await runSchedule(state.schedules.plans[0]);
  assert.match(status, /已受理.*列表刷新失败/);
  assert.equal(statusError, false);
  assert.match(scheduleInput("run-receipt").textContent, /accepted-before-refresh/);
  assert.equal(scheduleInput("run-receipt").querySelector("a"), null);

  // Detail/history refresh failures remain visible in the read-only drawer
  // without replacing the accepted receipt or retrying the operation.
  failList = false;
  await showSchedule("plan-exact");
  for (const mode of ["view", "runs"]) {
    await loadSchedules();
    const plan = state.schedules.plans[0];
    const before = posts.length;
    failView = mode === "view";
    failRuns = mode === "runs";
    const runId = `accepted-before-${mode}-failure`;
    respond = () => ({ ok: true, accepted: true, run_id: runId, run: null });
    await runSchedule(plan);
    await runSchedule(plan);
    assert.equal(posts.length, before + 1);
    assert.match(scheduleInput(mode === "view" ? "detail-note" : "runs-note").textContent,
      mode === "view" ? /详情刷新失败/ : /记录刷新失败/);
    assert.equal(scheduleInput(mode === "view" ? "detail-retry" : "runs-retry").hidden, false);
    assert.match(status, /已受理/);
    assert.equal(statusError, false);
    assert.equal(scheduleInput("run-receipt").hidden, false);
    assert.match(scheduleInput("run-receipt").textContent, /已受理/);
    assert(scheduleInput("run-receipt").textContent.includes(runId));
  }
  failView = false;
  failRuns = false;

  // An unconfirmed response never obtains a fresh grant or automatically retries.
  failList = false;
  await loadSchedules();
  const unknownPlan = state.schedules.plans[0];
  const beforeUnknown = posts.length;
  const queriesBeforeUnknown = gets.length;
  respond = () => { throw new Error("请求超时，触发结果尚未确认。"); };
  await runSchedule(unknownPlan);
  await runSchedule(unknownPlan);
  assert.equal(posts.length, beforeUnknown + 1);
  assert.equal(gets.length, queriesBeforeUnknown);
  assert.equal(unknownPlan.actions.run_now, null);
  assert.match(status, /触发结果尚未确认.*查看最近记录/);
  assert.equal(statusError, true);
  assert.equal(scheduleInput("run-receipt").hidden, true);

  // An existing-session plan is selected from Sessions, never from a second binding list.
  respond = null;
  await openScheduleEditor();
  await chooseScheduleTarget("binding");
  assert.equal(scheduleInput("project-field").hidden, true);
  assert.equal(scheduleInput("chat-target").hidden, true);
  assert.equal(scheduleInput("session-settings").hidden, true);
  assert.equal(scheduleInput("project").required, false);
  assert.equal(scheduleInput("binding").value, "");
  assert.equal(scheduleInput("preview").disabled, true);
  assert.equal(scheduleInput("select-session").hidden, false);
  scheduleInput("name").value = "继续检查";
  scheduleInput("instructions").value = "检查刚才的问题";
  scheduleInput("kind").value = "weekly";
  scheduleInput("timezone").value = "America/New_York";
  scheduleInput("time").value = "11:35";
  scheduleInput("at").value = "2030-11-03T01:30";
  scheduleInput("end-at").value = "2030-11-03T01:45";
  scheduleInput("every").value = "17";
  scheduleInput("enabled").checked = false;
  scheduleInput("weekdays").querySelectorAll("input")[2].checked = true;
  scheduleSelectOptions(scheduleInput("offset"), [["-04:00", "第一次"], ["-05:00", "第二次"]], "-05:00");
  scheduleSelectOptions(scheduleInput("end-offset"), [["-04:00", "第一次"], ["-05:00", "第二次"]], "-04:00");
  scheduleInput("offset-field").hidden = false;
  scheduleInput("end-offset-field").hidden = false;
  const expectedDraft = captureScheduleDraft();
  scheduleInput("select-session").click();
  assert.equal(state.tab, "sessions");
  assert.equal(scheduleInput("drawer").open, false);
  assert.equal(document.querySelector("#session-schedule-selection").hidden, false);
  assert.deepEqual(pendingScheduleDraft, expectedDraft);
  const targetSession = { bindingId: "binding-inactive", shortId: "short-exact", nativeTitle: "<img src=x onerror=alert(1)>",
    chatId: "oc_original", chatLabel: "<b>原群</b>", projectAlias: "project-one", topicId: "omt_exact",
    pointerState: "other", catalogState: "archived" };
  const opener = document.createElement("button");
  document.body.append(opener);
  const position = structuredClone(state.sessionPage);
  const oldGrant = state.schedules.actions.create;
  createGrant = { ...envelope("create"), actionToken: "fresh-session-create-grant" };
  const beforeTargetOpen = gets.length;
  await openScheduleForSession(targetSession, opener);
  assert.equal(gets[beforeTargetOpen], "/api/v1/schedules?mode=list");
  assert.notDeepEqual(scheduleEditor.action, oldGrant);
  assert.deepEqual(scheduleEditor.action, createGrant);
  assert.deepEqual(captureScheduleDraft(), expectedDraft, "switching to Sessions must preserve every entered schedule field");
  assert.equal(scheduleInput("end-details").open, true);
  assert.equal(pendingScheduleDraft, null);
  assert.equal(document.querySelector("#session-schedule-selection").hidden, true);
  assert.equal(scheduleInput("target-kind").disabled, true);
  assert.equal(scheduleInput("binding").value, "binding-inactive");
  assert.equal(scheduleInput("select-session").hidden, true);
  assert.match(scheduleInput("binding-summary").textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(scheduleInput("binding-summary").textContent, /自动暂停/);
  assert.match(scheduleInput("binding-summary").textContent, /omt_exact/);
  assert.equal(scheduleInput("binding-summary").querySelector("img"), null);
  assert.equal(scheduleInput("binding-summary").querySelector("input"), null);
  assert.deepEqual(state.sessionPage, position);
  await previewSchedule();
  const bindingPreview = new URL(gets.at(-1), "http://localhost").searchParams;
  assert.equal(bindingPreview.get("target_kind"), "binding");
  assert.equal(bindingPreview.get("target_binding_id"), "binding-inactive");
  assert.equal(bindingPreview.get("chat_id"), null);
  assert.equal(bindingPreview.get("session_settings"), null);
  const beforeBindingSave = gets.length;
  await saveSchedule({ preventDefault() {} });
  const bindingCreate = posts.at(-1).body.definition;
  assert.equal(bindingCreate.target_kind, "binding");
  assert.equal(bindingCreate.target_binding_id, "binding-inactive");
  for (const name of ["session_settings", "project", "chat_id", "workspace", "expected_project_revision"]) assert.equal(name in bindingCreate, false);
  assert.equal(posts.at(-1).body.actionToken, "fresh-session-create-grant");
  assert.equal(gets.length, beforeBindingSave, "saving from Sessions must not reload or move its list");
  assert.equal(state.tab, "sessions");
  assert.deepEqual(state.sessionPage, position);
  assert.equal(document.activeElement, opener);
  assert.equal(document.querySelector("#session-schedule-saved").hidden, false);
  document.querySelector("#session-schedule-view").click();
  assert.equal(state.tab, "schedules");

  // Canceling a selection discards its page-local draft, including a later row click.
  await openScheduleEditor();
  scheduleInput("name").value = "必须丢弃的草稿";
  scheduleInput("instructions").value = "discard me";
  await chooseScheduleTarget("binding");
  scheduleInput("select-session").click();
  document.querySelector("#session-schedule-cancel").click();
  assert.equal(pendingScheduleDraft, null);
  assert.equal(document.querySelector("#session-schedule-selection").hidden, true);
  await openScheduleForSession(targetSession, opener);
  assert.equal(scheduleInput("name").value, "");
  assert.equal(scheduleInput("instructions").value, "");
  closeScheduleEditor();
  assert.equal(pendingScheduleDraft, null);
  state.tab = "schedules";

  const boundPlan = { ...fixturePlan(), target_kind: "binding", target_binding_id: "binding-original",
    target_label: "已有问题", session_settings: null, workspace: null, blocked_reason: "target_inactive", enabled: true,
    effective_workspace: { working_cwd: "/managed/original/subproject", worktree_id: "wt-original",
      source: "binding", native_cwd_unresolved: true },
    execution: { kind: "latest", status: "input_steered", is_last: false } };
  boundPlan.actions.run_now = null;
  plans = [boundPlan];
  await loadSchedules();
  const boundRow = document.querySelector("#schedules-body").querySelector("tr");
  assert(boundRow.textContent.includes("启停：已启用"));
  assert(boundRow.textContent.includes("自动暂停"));
  assert(boundRow.textContent.includes("已追加当前任务"));
  assert(boundRow.textContent.includes("原会话 · 已有问题"));
  assert.equal(boundRow.querySelectorAll("button").find((node) => node.textContent === "立即运行").disabled, true);
  const previousViewedPlan = viewedPlan;
  viewedPlan = boundPlan;
  await showSchedule(boundPlan.id);
  assert(scheduleInput("detail-settings").textContent.includes("已登记工作目录：/managed/original/subproject"));
  assert(scheduleInput("detail-settings").textContent.includes("Worktree：wt-original"));
  assert(scheduleInput("detail-settings").textContent.includes("实际目录由原会话确定"));
  closeScheduleDetail();
  viewedPlan = previousViewedPlan;
  await openScheduleEditor(boundPlan);
  assert.equal(scheduleInput("target-kind").disabled, true);
  assert.equal(scheduleInput("binding").type, "hidden");
  assert.equal(scheduleInput("select-session").hidden, true);
  assert.equal(scheduleInput("binding").value, "binding-original");
  assert.equal(scheduleInput("workspace-fields").hidden, true);
  assert.equal(scheduleInput("workspace").disabled, true);
  assert(scheduleInput("target-note").textContent.includes("实际工作目录"));
  assert(scheduleInput("binding-summary").textContent.includes("已登记工作目录：/managed/original/subproject"));
  assert(scheduleInput("binding-summary").textContent.includes("Worktree：wt-original"));
  assert(scheduleInput("binding-summary").textContent.includes("本次未查询原生目录"));
  assert.equal(scheduleEditorHasChanges(), false);
  assert(scheduleInheritedWorkspace({ effective_workspace: { working_cwd: "/selected/lazy",
    native_cwd_unresolved: false } }).includes("已选工作目录：/selected/lazy"));
  const unknownDirectory = scheduleInheritedWorkspace({ project_cwd: "/must-not-fallback",
    effective_workspace: { working_cwd: null, worktree_id: "wt-retained", native_cwd_unresolved: true } });
  assert(unknownDirectory.includes("工作目录尚未登记"));
  assert(unknownDirectory.includes("wt-retained"));
  assert(!unknownDirectory.includes("/must-not-fallback"));
  await previewSchedule();
  await saveSchedule({ preventDefault() {} });
  const bindingUpdate = posts.at(-1).body.definition;
  for (const name of ["target_kind", "target_binding_id", "session_settings", "project", "chat_id", "workspace", "expected_project_revision"]) {
    assert.equal(name in bindingUpdate, false);
  }
  assert.equal(scheduleRunStatus("input_started"), "已启动新一轮");
  assert.equal(scheduleRunStatus("input_unknown"), "输入接收情况待确认");

  // The new-topic flow uses the same group picker and an explicit, explained p2p mode.
  contextAvailable = true;
  await openScheduleEditor();
  assert.equal(scheduleInput("chat-kind").value, "group");
  assert.equal(scheduleInput("group-picker").hidden, false);
  assert.equal(scheduleInput("chat-field").hidden, true);
  assert.equal(scheduleInput("chat-id-help").hidden, true);
  assert.equal(scheduleInput("group-picker").querySelector(".chat-picker-summary").textContent, "请选择群聊");
  assert.equal(scheduleInput("group-picker").querySelector(".chat-picker-empty-option"), null);
  scheduleInput("group").click();
  await flush();
  scheduleInput("group-picker").querySelector(".chat-picker-option").click();
  await flush();
  assert.equal(scheduleInput("chat").value, "oc_picker");
  assert.equal(scheduleInput("group-picker").querySelector(".chat-picker-selected-avatar").querySelector("img").src,
    "https://example.feishucdn.com/schedule.jpg");
  scheduleInput("group").click();
  const scheduleSearch = scheduleInput("group-search");
  scheduleSearch.value = "另一个群";
  scheduleSearch.dispatch("input");
  const cancelGroupSearch = scheduleSearch.dispatch("keydown", { key: "Escape" });
  assert(cancelGroupSearch.prevented);
  assert.equal(scheduleInput("chat").value, "oc_picker", "cancelled searches preserve the scheduled plan target");
  assert.equal(scheduleInput("editor").hidden, false);
  scheduleInput("chat-manual").checked = true;
  scheduleInput("chat-manual").dispatch("change");
  await flush();
  assert.equal(scheduleInput("chat").value, "oc_picker");
  assert.equal(scheduleInput("chat-field").hidden, false);
  const idHelp = scheduleInput("chat-id-help");
  assert.equal(idHelp.hidden, false, "manual group IDs should expose their acquisition guide");
  const idHelpLink = idHelp.querySelector("a");
  assert.equal(idHelpLink.textContent, "如何获取聊天 ID");
  assert.equal(idHelpLink.getAttribute("href"), "https://open.feishu.cn/document/server-docs/group/chat/chat-id-description");
  assert.equal(idHelpLink.getAttribute("target"), "_blank");
  assert.equal(idHelpLink.getAttribute("rel"), "noopener noreferrer");
  assert(scheduleInput("chat").getAttribute("aria-describedby").split(" ").includes(idHelp.id));
  scheduleInput("chat-kind").value = "p2p";
  scheduleInput("chat-kind").dispatch("change");
  assert.equal(scheduleInput("chat").value, "");
  assert.equal(scheduleInput("group-picker").hidden, true);
  assert.equal(scheduleInput("chat-manual-field").hidden, true);
  assert.match(scheduleInput("chat-help").textContent, /\/cron/);
  assert.match(scheduleInput("chat-help").textContent, /目标单聊.*选择“当前聊天”/);
  assert.equal(idHelp.hidden, false);
  assert.match(idHelp.textContent, /不是用户 ID/);
  assert.match(idHelp.textContent, /与本机器人的聊天/);
  scheduleInput("chat").value = "oc_typed_group";
  scheduleInput("chat").dispatch("input");
  scheduleInput("chat").dispatch("change");
  await flush();
  assert.equal(scheduleInput("chat-kind").value, "group");
  assert.equal(scheduleInput("chat-field").hidden, false);
  assert.equal(scheduleInput("group-picker").hidden, true);
  assert.equal(scheduleInput("chat").value, "oc_typed_group");
  closeScheduleEditor();

  contextAvailable = null;
  await openScheduleEditor(fixturePlan());
  assert.equal(scheduleInput("chat-kind").value, "unknown");
  assert.equal(scheduleInput("chat-field").hidden, false);
  assert.equal(scheduleInput("chat").value, "oc_other");
  assert.match(scheduleInput("chat-help").textContent, /保留原聊天 ID/);
  assert.equal(idHelp.hidden, false, "unresolved editable targets must retain their ID help");
  contextAvailable = false;
  await loadScheduleSessionOptions();
  assert.equal(scheduleInput("chat-kind").value, "p2p");
  assert.equal(scheduleInput("chat").value, "oc_other");
  closeScheduleEditor();
  const p2pPlan = fixturePlan();
  p2pPlan.chat = { chatType: "p2p", chatMode: "group", chatLabel: "单聊" };
  await openScheduleEditor(p2pPlan);
  assert.equal(scheduleInput("chat-kind").value, "p2p");
  assert.equal(scheduleInput("chat-field").hidden, false);
  assert.equal(scheduleInput("context-field").hidden, true);
  closeScheduleEditor();

  // Duplicate submissions are rejected before an outstanding group-validation read.
  contextAvailable = true;
  await openNewScheduleEditor();
  await previewSchedule();
  const beforeValidationPost = posts.length;
  const beforeValidationRead = gets.filter((path) => path.startsWith("/api/v1/chats/validate?")).length;
  chatValidationGate = {};
  const validatingSave = saveSchedule({ preventDefault() {} });
  assert.equal(scheduleInput("fields").disabled, true);
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.length, beforeValidationPost);
  assert.equal(gets.filter((path) => path.startsWith("/api/v1/chats/validate?")).length, beforeValidationRead + 1);
  chatValidationGate.resolve({ chatId: "oc_test" });
  chatValidationGate = null;
  await validatingSave;
  assert.equal(posts.length, beforeValidationPost + 1);

  // A late options response can invalidate a preview while group validation awaits.
  // Saving must then stop safely, without consuming the grant or submitting new settings.
  await openNewScheduleEditor();
  scheduleOptionsGate = {};
  const refreshingOptions = loadScheduleSessionOptions();
  await previewSchedule();
  const preparedBeforeOptions = schedulePrepared;
  const grantBeforeOptions = scheduleEditor.action;
  const beforeOptionsRacePost = posts.length;
  chatValidationGate = {};
  const savingDuringOptions = saveSchedule({ preventDefault() {} });
  assert.equal(scheduleInput("fields").disabled, true);
  scheduleOptionsGate.resolve({ session_settings: {
    ...structuredClone(defaultSessionSettings), progress_card_enabled: !defaultSessionSettings.progress_card_enabled,
  } });
  scheduleOptionsGate = null;
  await refreshingOptions;
  assert(preparedBeforeOptions);
  assert.equal(schedulePrepared, null, "late changed session defaults must invalidate the prepared schedule");
  chatValidationGate.resolve({ chatId: "oc_test" });
  chatValidationGate = null;
  await assert.doesNotReject(() => savingDuringOptions);
  assert.equal(posts.length, beforeOptionsRacePost);
  assert.equal(scheduleEditor.action, grantBeforeOptions);
  assert.equal(scheduleInput("fields").disabled, false);
  assert.equal(scheduleInput("close").disabled, false);
  assert.equal(scheduleInput("save").disabled, true);
  assert.match(scheduleInput("preview-message").textContent, /重新预览/);
  closeScheduleEditor();
  // Inline validation rejects only applicable, locally knowable field errors.
  // It never sends a mutation or consumes the grant for a correctable draft.
  await openNewScheduleEditor();
  const beforeInvalidPost = posts.length;
  const unspentGrant = scheduleEditor.action;
  for (const field of ["name", "instructions", "project"]) scheduleInput(field).value = " ";
  await previewSchedule(); // Time preview does not require the task text yet.
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.length, beforeInvalidPost);
  assert.equal(scheduleEditor.action, unspentGrant);
  assert.equal(document.activeElement, scheduleInput("name"));
  for (const field of ["name", "instructions", "project"]) {
    assert.equal(scheduleInput(field).getAttribute("aria-invalid"), "true");
    assert.equal(scheduleInput(`${field}-error`).hidden, false);
    assert(scheduleInput(field).getAttribute("aria-describedby").includes(`schedule-${field}-error`));
    scheduleInput(field).value = field === "project" ? "project-one" : "fixed";
    scheduleInput(field).dispatch("input");
    assert.equal(scheduleInput(field).getAttribute("aria-invalid"), null);
    assert.equal(scheduleInput(`${field}-error`).hidden, true);
  }
  assert.equal(scheduleInput("form-error").hidden, true);

  // Empty timing/target fields surface together and skip the preview request.
  chatTarget("schedule").set();
  scheduleInput("timezone").value = "";
  scheduleInput("time").value = "";
  const beforeInvalidPreview = gets.filter(path => path.includes("mode=preview")).length;
  await previewSchedule();
  assert.equal(gets.filter(path => path.includes("mode=preview")).length, beforeInvalidPreview);
  assert.deepEqual([...scheduleFieldErrors.keys()], ["chat", "timezone", "time"]);
  assert.equal(document.activeElement, scheduleInput("group"));
  assert.equal(scheduleInput("chat-error").parentElement, scheduleInput("group-picker"));
  scheduleInput("chat-manual").checked = true;
  scheduleInput("chat-manual").dispatch("change");
  assert.equal(scheduleInput("group").getAttribute("aria-invalid"), null);
  await previewSchedule();
  assert.equal(document.activeElement, scheduleInput("chat"));
  assert.equal(scheduleInput("chat-error").parentElement, scheduleInput("chat-field"));
  closeScheduleEditor();

  await openNewScheduleEditor();
  scheduleInput("kind").value = "interval";
  for (const value of ["", "0", "-1", "1.5"]) {
    scheduleInput("every").value = value;
    await previewSchedule();
    assert.equal(scheduleInput("every").getAttribute("aria-invalid"), "true");
    assert.equal(schedulePrepared, null);
  }
  scheduleInput("kind").value = "weekly";
  scheduleInput("kind").dispatch("input");
  assert.equal(scheduleInput("every").getAttribute("aria-invalid"), null);
  await previewSchedule();
  assert.equal(scheduleInput("weekdays").getAttribute("aria-invalid"), "true");
  assert.equal(document.activeElement, scheduleInput("weekdays").querySelector("input"));
  scheduleInput("weekdays").querySelector("input").click();
  assert.equal(scheduleInput("weekdays").getAttribute("aria-invalid"), null);
  await previewSchedule();
  assert(schedulePrepared);

  // Browser-native partial/invalid date input in a closed optional section.
  scheduleInput("end-details").open = false;
  scheduleInput("end-at").validity = { valid: false };
  scheduleInput("end-at").validationMessage = "请填写完整的截止时间。";
  await previewSchedule();
  assert.equal(scheduleInput("end-details").open, true);
  assert.equal(document.activeElement, scheduleInput("end-at"));
  assert(scheduleInput("end-at").getAttribute("aria-describedby").includes("schedule-timezone-note"));
  delete scheduleInput("end-at").validity;
  scheduleInput("end-at").dispatch("input");
  assert.equal(scheduleInput("end-at").getAttribute("aria-describedby"), "schedule-timezone-note schedule-end-note");
  closeScheduleEditor();

  // Broad server errors are not guessed into a field by parsing their message.
  await openNewScheduleEditor();
  previewError = Object.assign(new Error("时区或其他规则无效 <img src=x>"), { code: "invalid_schedule" });
  await previewSchedule();
  assert.equal(scheduleFieldErrors.size, 0);
  assert.equal(scheduleInput("form-error").hidden, false);
  assert.equal(scheduleInput("form-error").querySelector("img"), null);
  previewError = null;
  await previewSchedule();
  assert.equal(scheduleInput("form-error").hidden, true);
  chatValidationError = Object.assign(new Error("聊天 ID 无效"), { code: "invalid_chat_id" });
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.length, beforeInvalidPost);
  assert(scheduleEditor.action);
  assert.equal(scheduleInput("fields").disabled, false);
  assert.equal(document.activeElement, scheduleInput("group"));
  assert.equal(scheduleInput("group").getAttribute("aria-invalid"), "true");
  chatValidationError = null;
  closeScheduleEditor();

  // Compare actual form values rather than a sticky "touched" flag. Opening,
  // async catalog hydration, previewing and reverting values are all clean.
  scheduleOptionsGate = {};
  const loadingCleanEditor = openScheduleEditor();
  assert.equal(scheduleEditorHasChanges(), false);
  scheduleInput("name").value = "Typed while options load";
  scheduleOptionsGate.resolve();
  scheduleOptionsGate = null;
  await loadingCleanEditor;
  assert.equal(scheduleEditorHasChanges(), true, "async defaults must not mark user input as saved");
  scheduleInput("name").value = "";
  assert.equal(scheduleEditorHasChanges(), false);
  const beforeCleanClose = confirmations.length;
  scheduleInput("close").click();
  assert.equal(confirmations.length, beforeCleanClose);
  await openScheduleEditor(fixturePlan());
  await previewSchedule();
  assert.equal(scheduleEditorHasChanges(), false);
  scheduleInput("name").value = "a changed name";
  confirmation = false;
  const actionBeforeDiscard = scheduleEditor.action;
  for (const cancel of [() => scheduleInput("close").click(), () => scheduleInput("cancel").click(),
    () => scheduleInput("drawer").dispatch("cancel")]) {
    cancel();
    assert.equal(scheduleInput("drawer").open, true);
    assert.equal(scheduleInput("name").value, "a changed name");
    assert.equal(scheduleEditor.action, actionBeforeDiscard);
  }
  const unload = () => {
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    windowListeners.get("beforeunload")(event);
    return event;
  };
  assert.equal(unload().prevented, true);
  scheduleInput("name").value = fixturePlan().name;
  assert.equal(scheduleEditorHasChanges(), false);
  assert.equal(unload().prevented, false);
  const beforeRevertedClose = confirmations.length;
  scheduleInput("drawer").dispatch("cancel");
  assert.equal(scheduleInput("drawer").open, false);
  assert.equal(confirmations.length, beforeRevertedClose);

  // Choosing an exact session is an internal step, not abandoning the draft.
  await openScheduleEditor();
  scheduleInput("name").value = "Preserved target-selection draft";
  await chooseScheduleTarget("binding");
  const beforeChoosing = confirmations.length;
  chooseScheduleSession();
  assert.equal(confirmations.length, beforeChoosing);
  assert.equal(scheduleInput("drawer").open, false);
  assert.equal(hasUnsavedEditors(), true);
  assert.equal(unload().prevented, true);
  cancelScheduleSelection();
  assert(pendingScheduleDraft, "declining discard keeps the pending selection and its draft");
  await openScheduleForSession(targetSession, opener);
  assert.equal(scheduleInput("name").value, "Preserved target-selection draft");
  assert.equal(hasUnsavedEditors(), true);
  confirmation = true;
  scheduleInput("cancel").click();
  assert.equal(hasUnsavedEditors(), false);
  assert.equal(unload().prevented, false);

  await openNewScheduleEditor();
  await previewSchedule();
  const beforeSavedClose = confirmations.length;
  await saveSchedule({ preventDefault() {} });
  assert.equal(scheduleEditor, null);
  assert.equal(confirmations.length, beforeSavedClose, "save success must never ask to discard");

  // Exact-session plans inherit their configuration. Saving or discarding the
  // handed-off draft need not wait for the shared drawer's model catalog.
  for (const finish of ["save", "cancel"]) {
    previewTimes = times;
    await openScheduleEditor();
    scheduleInput("name").value = "Draft handed off before catalog";
    scheduleInput("instructions").value = "Use the original session settings";
    await chooseScheduleTarget("binding");
    chooseScheduleSession();
    scheduleOptionsGate = {};
    const opening = openScheduleForSession(targetSession, opener);
    await flush();
    assert.equal(scheduleInput("drawer").open, true);
    assert.equal(scheduleInput("session-settings").hidden, true);
    assert.equal(pendingScheduleDraft, null, "ownership transfers before options finish");
    assert.equal(document.querySelector("#session-schedule-selection").hidden, true);
    assert.equal(hasUnsavedEditors(), true, "the active editor still protects its draft");
    const postsBeforeFinish = posts.length;
    if (finish === "save") {
      await previewSchedule();
      assert.equal(scheduleInput("save").disabled, false, scheduleInput("preview-message").textContent
        + scheduleInput("form-error").textContent);
      const beforeSaveConfirm = confirmations.length;
      await saveSchedule({ preventDefault() {} });
      assert.equal(posts.length, postsBeforeFinish + 1);
      assert.equal(posts.at(-1).body.definition.target_binding_id, targetSession.bindingId);
      assert.equal("session_settings" in posts.at(-1).body.definition, false);
      assert.equal(confirmations.length, beforeSaveConfirm);
    } else {
      confirmation = false;
      scheduleInput("cancel").click();
      assert.equal(scheduleInput("drawer").open, true);
      assert.equal(hasUnsavedEditors(), true);
      confirmation = true;
      scheduleInput("cancel").click();
      assert.equal(posts.length, postsBeforeFinish);
    }
    assert.equal(scheduleEditor, null);
    assert.equal(hasUnsavedEditors(), false);
    assert.equal(unload().prevented, false);
    scheduleOptionsGate.resolve();
    scheduleOptionsGate = null;
    await opening;
    assert.equal(hasUnsavedEditors(), false);
    assert.equal(pendingScheduleDraft, null);
    assert.equal(document.querySelector("#session-schedule-selection").hidden, true);
    assert.equal(scheduleInput("drawer").open, false);
  }

  // Workspace choices use the shared selector while remaining independent from model reads.
  state.tab = "schedules";
  plans = [fixturePlan()];
  catalogError = { code: "unavailable", message: "模型目录读取失败" };
  for (const workspace of [
    { mode: "existing", worktree_id: "wt-project-one" },
    { mode: "new" },
    { mode: "new", ref: "refs/heads/main" },
  ]) {
    await openNewScheduleEditor();
    await changeScheduleProject();
    assert.equal(new URL(gets.at(-1), "http://localhost").searchParams.get("project"), "project-one");
    assert(scheduleInput("session-note").textContent.includes("模型目录读取失败"));
    scheduleInput("workspace").value = workspace.mode;
    scheduleInput("workspace").dispatch("change");
    assert.equal(scheduleInput("worktree-field").hidden, workspace.mode !== "existing");
    assert.equal(scheduleInput("workspace-ref-field").hidden, workspace.mode !== "new");
    if (workspace.worktree_id) {
      scheduleInput("worktree").value = workspace.worktree_id;
      scheduleInput("worktree").dispatch("change");
    }
    if (workspace.ref) {
      scheduleInput("workspace-ref").value = `  ${workspace.ref}  `;
      scheduleInput("workspace-ref").dispatch("input");
    }
    assert.deepEqual(scheduleEditor.workspace, workspace);
    assert.equal(scheduleEditorHasChanges(), true);
    await previewSchedule();
    assert.equal(scheduleInput("save").disabled, false);
    await saveSchedule({ preventDefault() {} });
    assert.deepEqual(posts.at(-1).body.definition.workspace, workspace);
    assert.equal(posts.at(-1).body.definition.expected_project_revision, 1);
  }
  catalogError = null;

  // A chat lookup error is independent: preserve drafts and the other option groups.
  chatInfoError = { code: "chat_info_unavailable", message: "聊天信息读取超时，请稍后重试" };
  const chatUnknownPlan = fixturePlan();
  chatUnknownPlan.session_settings.message_context_mode = "catch-up";
  chatUnknownPlan.workspace = { mode: "existing", worktree_id: "wt-project-one" };
  await openScheduleEditor(chatUnknownPlan);
  assert.equal(scheduleEditor.contextAvailable, null);
  assert.equal(scheduleInput("chat-kind").value, "unknown");
  assert(scheduleInput("session-note").textContent.includes("聊天信息读取超时"));
  assert.equal(scheduleInput("context").value, "catch-up");
  assert.equal(scheduleInput("context").querySelectorAll("option").find((option) => option.value === "catch-up").disabled, false);
  assert(scheduleInput("model").querySelectorAll("option").some((option) => option.value === "native-default"));
  assert.equal(scheduleInput("worktree").value, "wt-project-one");
  assert.equal(scheduleInput("workspace").querySelectorAll("option").find((option) => option.value === "existing").disabled, false);
  assert.equal(scheduleEditorHasChanges(), false);
  scheduleInput("name").value = "preserved chat error draft";
  scheduleInput("instructions").value = "preserved instructions";
  scheduleInput("effort").value = "high";
  scheduleInput("effort").dispatch("change");
  await loadScheduleSessionOptions();
  assert.equal(scheduleInput("name").value, "preserved chat error draft");
  assert.equal(scheduleInput("instructions").value, "preserved instructions");
  assert.equal(scheduleInput("effort").value, "high");
  assert.equal(scheduleInput("context").value, "catch-up");
  assert.equal(scheduleInput("worktree").value, "wt-project-one");
  closeScheduleEditor();
  chatInfoError = null;

  // An options refresh can revoke a choice while final chat validation is pending.
  await openNewScheduleEditor();
  await changeScheduleProject();
  scheduleInput("workspace").value = "existing";
  scheduleInput("workspace").dispatch("change");
  scheduleInput("worktree").value = "wt-project-one";
  scheduleInput("worktree").dispatch("change");
  await previewSchedule();
  chatValidationGate = {};
  const savingWorkspace = saveSchedule({ preventDefault() {} });
  const pendingWorkspacePosts = posts.length;
  workspaceOptions.get("project-one").items[0].available = false;
  await loadScheduleSessionOptions();
  chatValidationGate.resolve();
  chatValidationGate = null;
  await savingWorkspace;
  assert.equal(posts.length, pendingWorkspacePosts);
  assert.equal(scheduleInput("worktree").value, "wt-project-one");
  assert.equal(scheduleInput("save").disabled, true);
  closeScheduleEditor();
  workspaceOptions.get("project-one").items[0].available = true;

  // Exact unavailable choices are retained and editable without replacing their workspace.
  const unavailable = { ...fixturePlan(), workspace: { mode: "existing", worktree_id: "wt-deleted-exact" },
    workspace_available: false, workspace_error: { code: "not_found", message: "所选 Worktree 登记已删除" },
    blocked_reason: "workspace_unavailable", suspended_reason: "workspace_unavailable",
    execution: { kind: "latest", status: "workspace_unavailable", is_last: false } };
  unavailable.actions.run_now = null;
  assert(scheduleBlockedLabel({ ...unavailable, blocked_reason: "blocked_unknown" }).includes("执行结果待确认"));
  assert(scheduleBlockedLabel({ ...unavailable, blocked_reason: "blocked_unknown" }).includes("登记已删除"));
  await openScheduleEditor(unavailable);
  assert.equal(scheduleInput("workspace").value, "existing");
  assert.equal(scheduleInput("worktree").value, "wt-deleted-exact");
  assert(scheduleInput("worktree").textContent.includes("不可用"));
  assert(scheduleInput("workspace-help").textContent.includes("登记已删除"));
  assert.equal(scheduleEditorHasChanges(), false, "options hydration must not dirty the saved policy");
  scheduleInput("name").value = "rename without replacing workspace";
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, false);
  await saveSchedule({ preventDefault() {} });
  assert.equal("workspace" in posts.at(-1).body.definition, false);
  assert.equal("expected_project_revision" in posts.at(-1).body.definition, false);

  await openScheduleEditor(unavailable);
  scheduleInput("worktree").value = "wt-project-one";
  scheduleInput("worktree").dispatch("change");
  assert(!scheduleInput("workspace-help").textContent.includes("登记已删除"));
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, false);
  scheduleInput("workspace").value = "new";
  scheduleInput("workspace").dispatch("change");
  assert.equal(schedulePrepared, null, "workspace changes must invalidate the reviewed preview");
  await previewSchedule();
  await saveSchedule({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.workspace, { mode: "new" });
  assert.equal(posts.at(-1).body.definition.expected_project_revision, 1);

  // Failed workspace reads preserve policy and session drafts; unchanged policy still permits editing.
  workspaceErrors.set("project-one", { code: "unavailable", message: "工作区目录状态读取超时" });
  const validOptions = workspaceOptions.get("project-one");
  workspaceOptions.delete("project-one");
  await openScheduleEditor(unavailable);
  assert(scheduleInput("workspace-help").textContent.includes("读取超时"));
  scheduleInput("progress").checked = true;
  scheduleInput("progress").dispatch("change");
  scheduleInput("name").value = "keep all drafts";
  await loadScheduleSessionOptions();
  assert.equal(scheduleInput("name").value, "keep all drafts");
  assert.equal(scheduleInput("progress").checked, true);
  assert.equal(scheduleInput("worktree").value, "wt-deleted-exact");
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, false);
  closeScheduleEditor();
  await openNewScheduleEditor();
  await changeScheduleProject();
  scheduleInput("workspace").value = "existing";
  changeScheduleWorkspace("workspace");
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, true);
  const rejectedPosts = posts.length;
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.length, rejectedPosts);
  closeScheduleEditor();
  workspaceOptions.set("project-one", validOptions);
  workspaceErrors.clear();

  // Project changes reset the workspace visibly and stale reads cannot restore old choices.
  await openNewScheduleEditor();
  await changeScheduleProject();
  scheduleInput("workspace").value = "new";
  scheduleInput("workspace").dispatch("change");
  scheduleInput("workspace-ref").value = "refs/heads/feature";
  scheduleInput("workspace-ref").dispatch("input");
  scheduleOptionsGate = {};
  const oldOptions = loadScheduleSessionOptions();
  const oldGate = scheduleOptionsGate;
  scheduleOptionsGate = null;
  workspaceOptions.get("project-two").isGit = false;
  scheduleInput("project").value = "project-two";
  await changeScheduleProject();
  assert.deepEqual(scheduleEditor.workspace, { mode: "project" });
  assert.equal(scheduleInput("workspace-ref").value, "");
  assert(scheduleInput("workspace-help").textContent.includes("不是 Git"));
  assert(scheduleInput("workspace").querySelectorAll("option")
    .filter((option) => option.value !== "project").every((option) => option.disabled));
  scheduleInput("name").value = "new project draft";
  oldGate.resolve();
  await oldOptions;
  assert.equal(scheduleEditor.workspaceOptions.projectAlias, "project-two");
  assert.equal(scheduleInput("name").value, "new project draft");
  assert.deepEqual(scheduleEditor.workspace, { mode: "project" });
  await previewSchedule();
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.at(-1).body.definition.project, "project-two");
  assert.equal(posts.at(-1).body.definition.expected_project_revision, 2);
  workspaceOptions.get("project-two").isGit = true;

  // List/detail retain enabled intent and exact reasons; failed Runs expose retained resource receipts.
  plans = [unavailable];
  viewedPlan = unavailable;
  unavailable.execution.worktree_id = "wt-retained-exact";
  unavailable.execution.worktree = { id: "wt-retained-exact", cwd: "/managed/retained/subproject",
    root: "/managed/retained", creation_state: "ready" };
  runRecords = [{ id: "skipped", due_at: 1893459600, status: "workspace_unavailable" },
    { id: "retained", due_at: 1893459601, status: "workspace_handoff_failed",
      worktree_id: "wt-retained-exact", worktree: unavailable.execution.worktree },
    { id: "unknown", due_at: 1893459602, status: "workspace_creation_unknown", worktree_id: "wt-unknown-exact",
      worktree: { id: "wt-unknown-exact", cwd: "/managed/unknown", root: "/managed/unknown", creation_state: "unknown" } },
    { id: "removed", due_at: 1893459603, status: "workspace_handoff_failed", worktree_id: "wt-removed-exact",
      worktree: { id: "wt-removed-exact", cwd: null, root: null, creation_state: null } }];
  await loadSchedules();
  const unavailableRow = document.querySelector("#schedules-body").querySelector("tr");
  assert(unavailableRow.textContent.includes("启停：已启用"));
  assert(unavailableRow.textContent.includes("wt-deleted-exact"));
  assert(unavailableRow.textContent.includes("登记已删除"));
  assert(unavailableRow.textContent.includes("wt-retained-exact"));
  assert.equal(unavailableRow.querySelectorAll("button").find((button) => button.textContent === "立即运行").disabled, true);
  await showSchedule(unavailable.id);
  assert(scheduleInput("detail-state").textContent.includes("登记已删除"));
  assert(scheduleInput("detail-state").textContent.includes("/managed/retained/subproject"));
  assert(scheduleInput("detail-settings").textContent.includes("wt-deleted-exact"));
  assert(scheduleInput("runs-body").textContent.includes("工作区不可用，本次未执行"));
  assert(scheduleInput("runs-body").textContent.includes("wt-retained-exact"));
  assert(scheduleInput("runs-body").textContent.includes("工作区已保留"));
  assert(scheduleInput("runs-body").textContent.includes("工作区创建结果待确认"));
  assert(scheduleInput("runs-body").textContent.includes("根目录：/managed/retained"));
  assert(scheduleInput("runs-body").textContent.includes("/managed/unknown"));
  assert(scheduleInput("runs-body").textContent.includes("wt-removed-exact"));
  assert(scheduleInput("runs-body").textContent.includes("资源登记已不可用"));
  assert.equal(scheduleInput("runs-body").querySelectorAll("a").length, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
