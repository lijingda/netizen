const state = { schedules: null };
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
const defaultSessionSettings = { turn_settings: { model_id: "native-default", effort_id: "medium", service_tier_id: "default" },
  reaction_pulse_enabled: false, progress_card_enabled: false, message_context_mode: "current-only" };
const models = [{ id: "native-default", display_name: "Native default", default_effort_id: "medium", default_service_tier_id: "default",
  efforts: [{ id: "medium", description: "Medium" }, { id: "high", description: "High" }],
  service_tiers: [{ id: "default", name: "Standard" }, { id: "priority", name: "Fast" }] }];
let catalogError = null;
let contextAvailable = true;
const fixturePlan = () => ({
  id: "plan-exact", revision: 7, name: "<b>Report</b>",
  project_alias: "project-one", chat_id: "oc_other",
  chat: { chatId: "oc_other", chatLabel: "<b>测试会话</b>", chatLabelResolved: true, chatOpenUrl: "https://applink.feishu.cn/client/chat/open?openChatId=oc_other" }, instructions: "<img src=x onerror=alert(1)>\nReport.",
  schedule: { kind: "daily", at: "09:00", timezone: "Asia/Shanghai" },
  next_due_local: "2030-01-01T09:00:00+08:00", enabled: true,
  lifecycle: { ended: false, has_future: true, has_trigger: true },
  execution: { kind: "none", status: "not_started", is_last: false, due_local: null },
  actions: { update: envelope("update"), delete: envelope("delete") },
  session_settings: structuredClone(defaultSessionSettings),
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
let viewedPlan = null;
let failList = false;
const times = [{ utc: "2030-01-01T01:00:00Z", local: "2030-01-01T09:00:00+08:00" }];
async function api(path, options = {}) {
  if (options.method === "POST") {
    posts.push({ path, body: JSON.parse(options.body) });
    if (respond) return respond(path, options);
    return { ok: true, plan: plans[0] };
  }
  gets.push(path);
  const query = new URL(path, "http://localhost").searchParams;
  if (path.startsWith("/api/v1/projects/options")) return {
    items: query.has("cursor") ? [{ alias: "retired", enabled: false }] : [
      { alias: "project-one", enabled: true }, { alias: "project-two", enabled: true }],
    nextCursor: query.has("cursor") ? null : "project-page-two",
  };
  const mode = query.get("mode") || "list";
  if (mode === "options") return { ok: true, session_settings: structuredClone(defaultSessionSettings),
    models: catalogError ? [] : structuredClone(models), context_mode_available: contextAvailable, model_catalog_error: catalogError };
  if (mode === "preview") {
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
  if (mode === "view") return { ok: true, plan: viewedPlan || fixturePlan(), preview: previewTimes || times, inflight: true };
  if (mode === "runs") return { ok: true, runs: [
    { id: "run-one", binding_id: "binding-exact", due_at: 1893459600, status: "released",
      feishu_url: "https://applink.feishu.cn/client/message/link?token=om_root" },
  ], next_cursor: "runs-page" };
  if (failList) throw new Error("列表暂不可用");
  return { ok: true, plans: structuredClone(plans), next_cursor: "page-two",
    default_timezone: "Asia/Shanghai", actions: { create: envelope("create") } };
}
async function refresh() { await loadSchedules(); return true; }
// SHIPPED_SCHEDULE_CONTROLLER
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
  assert.equal(scheduleInput("filter").querySelectorAll("input").length, 0);
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
  scheduleInput("filter-ended").value = "true";
  scheduleInput("filter-enabled").value = "false";
  plans = [];
  await loadSchedules();
  const emptyQuery = new URL(gets.filter((path) => path.startsWith("/api/v1/schedules?")).at(-1), "http://localhost").searchParams;
  assert.equal(emptyQuery.get("ended"), "true");
  assert.equal(emptyQuery.get("enabled"), "false");
  assert(document.querySelector("#schedules-body").textContent.includes("没有符合筛选条件"));
  scheduleInput("filter-ended").value = "false";
  scheduleInput("filter-enabled").value = "";
  plans = [fixturePlan()];
  await loadSchedules();
  assert.deepEqual(scheduleInput("filter-project").querySelectorAll("option").map((o) => o.value),
    ["", "project-one", "project-two", "retired"]);
  scheduleInput("filter-project").value = "project-one";
  await loadSchedules();
  await loadSchedules("page-two");
  scheduleInput("filter-project").value = "project-two";
  assert(gets.some((path) => path.startsWith("/api/v1/schedules?") && new URL(path, "http://localhost").searchParams.get("cursor") === "page-two"));

  await openScheduleEditor();
  assert.equal(scheduleInput("timezone").value, "Asia/Shanghai");
  assert.equal(scheduleInput("save").disabled, true);
  assert.equal(scheduleInput("editor").hidden, false);
  assert.equal(scheduleInput("drawer").open, true);
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
  await openScheduleEditor();
  scheduleInput("name").value = "Discard this draft";
  scheduleInput("drawer").dispatch("cancel");
  assert.equal(scheduleInput("drawer").open, false);
  assert.equal(posts.length, beforeCancel);
  assert.equal(document.activeElement, scheduleInput("new"));
  assert.equal(scheduleInput("filter-project").value, "project-two");

  // All four rules serialize their own fields, including an exact previewed interval anchor.
  await openScheduleEditor();
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
  assert.equal(new URL(links[1].href, "http://localhost").searchParams.get("binding_id"), "binding-exact");
  assert.equal(scheduleInput("runs-next").hidden, false);

  // A failed save keeps user input and makes its spent action unusable.
  await editSchedule(state.schedules.plans[0]);
  await previewSchedule();
  respond = () => { throw new Error("计划已变化，请刷新。"); };
  const beforeFailure = posts.length;
  await saveSchedule({ preventDefault() {} });
  assert.equal(scheduleInput("instructions").value, fixturePlan().instructions);
  assert.equal(scheduleInput("save").disabled, true);
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, true);
  await saveSchedule({ preventDefault() {} });
  assert.equal(posts.length, beforeFailure + 1);
  assert.match(status, /已变化/);
  assert.equal(statusError, true);

  // Submission cannot be dismissed/reopened or submitted twice while pending.
  respond = null;
  await openScheduleEditor();
  await previewSchedule();
  let finishSave;
  respond = () => new Promise((resolve) => { finishSave = resolve; });
  const pendingSave = saveSchedule({ preventDefault() {} });
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
  await openScheduleEditor();
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
  await openScheduleEditor();
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
  scheduleInput("context").value = "catch-up";
  changeScheduleSessionSettings("context");
  assert.equal(scheduleInput("save").disabled, true);
  await previewSchedule();
  const previewQuery = new URL(gets.at(-1), "http://localhost").searchParams;
  const selectedSettings = { turn_settings: { model_id: "native-default", effort_id: "high", service_tier_id: "priority" },
    reaction_pulse_enabled: true, progress_card_enabled: true, message_context_mode: "catch-up" };
  assert.deepEqual(JSON.parse(previewQuery.get("session_settings")), selectedSettings);
  await saveSchedule({ preventDefault() {} });
  assert.deepEqual(posts.at(-1).body.definition.session_settings, selectedSettings);

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
  previewTimes = [times[0]]; // The service owns the cutoff calculation.
  await previewSchedule();
  let deadlineQuery = new URL(gets.at(-1), "http://localhost").searchParams;
  assert.equal(deadlineQuery.get("local_end_at"), deadlinePlan.end_local_at);
  assert.equal(deadlineQuery.get("plan_id"), deadlinePlan.id);
  assert(!("end_at" in JSON.parse(deadlineQuery.get("schedule"))));
  assert.equal(schedulePrepared.schedule.end_at, deadlinePlan.schedule.end_at);
  assert.equal(scheduleInput("preview-times").querySelectorAll("li").length, 1);
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
  await openScheduleEditor();
  scheduleInput("timezone").value = "America/New_York";
  scheduleInput("end-at").value = "2030-11-03T01:30";
  ambiguousEnd = true;
  await previewSchedule();
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
  await openScheduleEditor();
  assert.equal(scheduleInput("end-at").value, "");
  await previewSchedule();
  assert.equal(scheduleInput("save").disabled, true);
})().catch((error) => { console.error(error); process.exitCode = 1; });
