(async () => {
  await flush();
  const refreshButton = document.querySelectorAll("[data-refresh]").find((button) => button.dataset.refresh === "sessions");
  const freshness = document.querySelector("#sessions-refreshed-at");
  for (const [view, kind, endpoint, dataKey] of [
    ["ordinary", "session", "/api/v1/sessions", "sessions"],
    ["temporary", "side", "/api/v1/side-topics", "sides"],
  ]) {
    ordinary.actions = { stop: grant("ordinary-stop") };
    let row = structuredClone(kind === "side" ? side : ordinary);
    const answer = async () => ({ items: [structuredClone(row)], nextCursor: null });
    if (kind === "side") sideAnswer = answer;
    else ordinaryAnswer = answer;
    await selectSessionTab(view);
    state.tab = "sessions";
    const body = document.querySelector(`#${dataKey}-body`);
    const firstText = body.textContent;
    const firstTimestamp = freshness.textContent;
    const beforeIdle = requests.length;
    row.runtime = kind === "side" ? { ...row.runtime, turnState: "idle" }
      : { ...row.runtime, primaryStatus: "running" };
    await advanceTime(60000);
    assert.equal(requests.length, beforeIdle, "idle Session lists must not make any background request");
    assert.equal(body.textContent, firstText, "new server facts remain undisplayed until an explicit read");
    assert.equal(freshness.textContent, firstTimestamp);

    refreshButton.click(); await flush();
    assert.equal(requests.filter((request) => request.path === endpoint).length >= 2, true);
    assert.notEqual(body.textContent, firstText);
    assert.notEqual(freshness.textContent, firstTimestamp);
    assert.equal(state[`${kind}RefreshedAt`], Date.now());
    assert(!requests.some((request) => request.path === "/api/v1/runtime-snapshots"));

    // Actions retain their exact grants and refresh even if the server rejects
    // one because facts changed since the last explicit list read.
    for (const failure of [false, true]) {
      actionFailure = failure;
      const readsBefore = requests.filter((request) => request.path === endpoint).length;
      const stop = body.querySelectorAll("button").find((button) => button.textContent === "停止");
      assert(stop);
      row.runtime = kind === "side" ? { ...row.runtime, turnState: failure ? "idle" : "running" }
        : { ...row.runtime, primaryStatus: failure ? "idle" : "running" };
      await advanceTime(1000);
      if (failure) {
        // The shared mutation handler refreshes and rethrows to its caller.
        await assert.rejects(stop.listeners.get("click")[0](), /会话状态已变化/);
      } else stop.click();
      await flush();
      const action = requests.findLast((request) => request.path.endsWith("/stop"));
      assert.equal(action.options.method, "POST");
      assert.equal(JSON.parse(action.options.body).actionToken, kind === "side" ? "stop" : "ordinary-stop");
      assert.equal(requests.filter((request) => request.path === endpoint).length, readsBefore + 1);
      assert.equal(state[dataKey].items[0].runtime[kind === "side" ? "turnState" : "primaryStatus"], failure ? "idle" : "running");
      assert.equal(state[`${kind}RefreshedAt`], Date.now());
    }
    actionFailure = false;

    // Failed reads retain both the visible facts and their freshness, and do
    // not schedule retry requests. A later manual refresh recovers immediately.
    const visible = body.textContent;
    const timestamp = freshness.textContent;
    const rejected = async () => { throw new Error("list read unavailable"); };
    if (kind === "side") sideAnswer = rejected;
    else ordinaryAnswer = rejected;
    await advanceTime(1000);
    refreshButton.click(); await flush();
    assert.equal(body.textContent, visible);
    assert.equal(freshness.textContent, timestamp);
    const afterFailure = requests.length;
    await advanceTime(60000);
    assert.equal(requests.length, afterFailure);
    if (kind === "side") sideAnswer = answer;
    else ordinaryAnswer = answer;
    refreshButton.click(); await flush();
    assert.notEqual(freshness.textContent, timestamp);
  }

  // One-shot list reads still render unavailable/archived/local runtime facts
  // faithfully; removing polling must never turn missing knowledge into idle.
  await selectSessionTab("ordinary");
  for (const [catalogState, resolution, primaryStatus, label] of [
    ["active", "unavailable", null, "状态暂不可用"],
    ["active", "deferred", null, "状态待确认"],
    ["active", "unavailable", "running", "running（待确认）"],
    ["archived", "archived", null, "—"],
    ["archived", "local", "lifecycle-unknown", "lifecycle-unknown"],
    ["unknown", "local", "running", "running"],
  ]) {
    ordinaryAnswer = async () => ({ items: [{ ...ordinary, catalogState,
      runtime: { ...ordinary.runtime, primaryStatus, primaryStatusResolution: resolution } }], nextCursor: null });
    await refresh("sessions");
    const runtime = document.querySelector("#sessions-body").querySelector(".runtime-state");
    assert.equal(runtime.textContent, label);
    if (resolution === "archived") assert.equal(runtime.querySelector(".runtime-primary").title, "已归档，不查询运行态");
  }

  await selectSessionTab("temporary");
  for (const [sessionState, runtime, stateLabel, label] of [
    ["creating", null, "创建中", "创建中"],
    ["closing", { ...side.runtime, state: "closing" }, "结束中", "结束中"],
    ["failed", null, "失败", "—"],
    ["closed", null, "已结束", "—"],
    ["unavailable", null, "状态待确认", "状态待确认"],
  ]) {
    sideAnswer = async () => ({ items: [{ ...side, sessionState, runtime }], nextCursor: null });
    await refresh("sessions");
    const body = document.querySelector("#sides-body");
    assert.equal(body.querySelector(".side-session-state").textContent, stateLabel);
    assert.equal(body.querySelector(".runtime-state").textContent, label);
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
