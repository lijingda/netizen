window.location = { pathname: "/sessions/ordinary", search: "", assign() {} };
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, expire() {} };
const sessionStorage = { getItem() {}, setItem() {}, removeItem() {} };
window.innerWidth = 600;
const timeRangeViewport = { matches: true, addEventListener() {} };
window.matchMedia = () => timeRangeViewport;
Element.prototype.hasAttribute = function(name) { return this.getAttribute(name) !== null; };
const bodyClasses = new Set();
document.body.classList.toggle = (name, enabled) => {
  if (enabled) bodyClasses.add(name);
  else bodyClasses.delete(name);
};
async function fetch(path) {
  assert(path.startsWith("/api/v1/sessions?") || path.startsWith("/api/v1/projects/options"));
  return { ok: true, status: 200, json: async () => ({ items: [], nextCursor: null }) };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
// SHIPPED_ADMIN_PAGE
(async () => {
  await flush();
  const now = new Date("2026-09-02T08:20:30.456Z");
  const today = presetTimeRange("today", now);
  const rolling = presetTimeRange("last-24-hours", now);
  const exactMinute = presetTimeRange("last-24-hours", new Date("2026-09-02T08:20:00Z"));
  const updated = presetTimeRange("last-3-months", now, true);
  const crossYear = presetTimeRange("last-3-months", new Date("2026-01-02T08:20:30Z"));
  const custom = customTimeRange("2026-08-27T00:00", "2026-09-02T23:59");
  const reversed = customTimeRange("2026-09-02T10:00", "2026-09-02T09:59");
  const gap = customTimeRange("2026-03-08T02:30", "2026-03-08T03:30");
  const fold = customTimeRange("2026-11-01T01:30", "2026-11-01T02:30");

  // Verify the initialized controls, not the template's spelling or controller names.
  const roots = document.querySelectorAll("[data-time-range]");
  assert.equal(roots.length, 3);
  const ids = new Set();
  for (const range of roots) {
    const part = (name) => range.querySelector(`[data-time-range-${name}]`);
    const trigger = part("trigger"), popover = part("popover");
    assert.equal(popover.getAttribute("role"), "dialog");
    assert.equal(trigger.getAttribute("aria-controls"), popover.id);
    assert.equal(popover.getAttribute("aria-labelledby"), part("dialog-title").id);
    for (const field of ["start", "end"]) {
      const input = part(field);
      assert.equal(input.type, "datetime-local");
      assert.deepEqual(input.getAttribute("aria-describedby").split(" "),
        [part("help").id, part("zone").id, part("error").id]);
    }
    for (const name of ["popover", "dialog-title", "help", "zone", "error", "start", "end"]) {
      assert(part(name).id && !ids.has(part(name).id), "each range has unique accessible references");
      ids.add(part(name).id);
    }
    assert.equal(part("error").getAttribute("role"), "alert");
    assert.equal(part("error").getAttribute("aria-live"), "polite");
    assert(!range.textContent.includes("ISO-8601"));
    assert.deepEqual(["clear", "cancel", "done"].map((name) => part(name).textContent), ["清除", "取消", "完成"]);
    assert.equal(part("from").name, `${range.dataset.timeRangeField || "created"}From`);
    assert.equal(part("before").name, `${range.dataset.timeRangeField || "created"}Before`);
    trigger.click();
    assert.equal(trigger.getAttribute("aria-expanded"), "true");
    assert(part("zone").textContent.includes(Intl.DateTimeFormat().resolvedOptions().timeZone));
    const presets = range.querySelectorAll("[data-time-range-preset]");
    assert.deepEqual(presets.map((button) => button.dataset.timeRangePreset),
      ["all", "today", "yesterday", "last-24-hours", "last-7-days", "last-30-days", "last-3-months"]);
    for (const button of presets) {
      button.click();
      assert.equal(button.getAttribute("aria-pressed"), "true");
      assert.equal(part("done").disabled, false);
    }
    part("start").value = "2026-08-27T00:00";
    part("end").value = "2026-09-02T23:59";
    part("end").dispatch("input"); part("done").click();
    assert.equal(part("from").value, custom.range.from);
    assert.equal(part("before").value, custom.range.before, "the selected end minute is inclusive");
    assert.match(part("from").value, /\.\d{6}\+00:00$/);
    trigger.click();
    part("end").value = "2026-08-26T23:59"; part("end").dispatch("input");
    assert.equal(part("end").getAttribute("aria-invalid"), "true");
    assert(part("error").textContent);
    assert.equal(part("done").disabled, true);
    assert(popover.dispatch("keydown", { key: "Escape" }).prevented);
    assert.equal(popover.hidden, true);
    assert.equal(trigger.getAttribute("aria-expanded"), "false");
    assert.equal(document.activeElement, trigger);
    assert.equal(part("before").value, custom.range.before, "Escape discards invalid draft edits");
    trigger.click(); part("clear").click(); part("done").click();
    assert.equal(part("from").value, ""); assert.equal(part("before").value, "");
  }

  // Desktop positioning keeps the popover inside the viewport even at the right edge.
  timeRangeViewport.matches = false;
  window.innerWidth = 900;
  const desktop = roots[0], desktopPopover = desktop.querySelector("[data-time-range-popover]");
  desktop.getBoundingClientRect = () => ({ left: 850, right: 890, width: 40 });
  desktop.querySelector("[data-time-range-trigger]").click();
  const left = 850 + Number.parseFloat(desktopPopover.style.left);
  assert(left >= 24 && left + 620 <= window.innerWidth - 24);
  assert.equal(desktopPopover.getAttribute("aria-modal"), null);
  assert.equal(desktop.querySelector("[data-time-range-backdrop]").hidden, true);
  desktop.querySelector("[data-time-range-cancel]").click();
  timeRangeViewport.matches = true;
  window.innerWidth = 600;

  const preexisting = document.querySelector("#auth-expired");
  preexisting.setAttribute("inert", "");
  const header = document.querySelector("header");
  const main = document.querySelector("main");
  const [root, otherRoot] = document.querySelector("#session-filter").querySelectorAll("[data-time-range]");
  const trigger = root.querySelector("[data-time-range-trigger]");
  const backdrop = root.querySelector("[data-time-range-backdrop]");
  const popover = root.querySelector("[data-time-range-popover]");
  trigger.click();
  const modalAfterOpen = bodyClasses.has("time-range-modal-open")
    && popover.getAttribute("aria-modal") === "true" && !popover.hidden;
  const isolatedAfterOpen = [header, otherRoot, trigger.parentElement]
    .every((node) => node.hasAttribute("inert"));
  const dialogPathAvailable = !main.hasAttribute("inert")
    && !root.hasAttribute("inert") && !popover.hasAttribute("inert")
    && !backdrop.hasAttribute("inert");
  timeRangeControllers.get(otherRoot).cancel({ restoreFocus: false });
  const unaffectedByClosedPeer = bodyClasses.has("time-range-modal-open")
    && trigger.parentElement.hasAttribute("inert");
  root.querySelector("[data-time-range-cancel]").click();
  const restoredAfterClose = !bodyClasses.has("time-range-modal-open")
    && [header, otherRoot, trigger.parentElement].every((node) => !node.hasAttribute("inert"))
    && preexisting.hasAttribute("inert") && document.activeElement === trigger;

  process.stdout.write(JSON.stringify({
    today, rolling, exactMinute, updated, crossYear, custom, reversed, gap, fold,
    modalAfterOpen, isolatedAfterOpen, dialogPathAvailable, unaffectedByClosedPeer, restoredAfterClose,
  }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
