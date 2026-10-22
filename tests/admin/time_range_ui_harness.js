window.location = { pathname: "/sessions/ordinary", search: "", assign() {} };
window.NetizenIdentity = { loadProfile: async () => {}, loadUsers: async () => {}, expire() {} };
const sessionStorage = { getItem() {}, setItem() {}, removeItem() {} };
window.innerWidth = 600;
window.matchMedia = () => ({ matches: true, addEventListener() {} });
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
