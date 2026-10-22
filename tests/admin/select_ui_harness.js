// Browser-only DOM details not needed by the other controller fixtures.
const originalMatches = Element.prototype.matches;
Element.prototype.matches = function(selector) {
  if (selector === ":disabled") {
    for (let node = this; node; node = node.parentElement) {
      if (node.disabled && (node === this || node.tagName === "fieldset")) return true;
    }
    return false;
  }
  return originalMatches.call(this, selector);
};
Element.prototype.closest = function(selector) {
  for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;
  return null;
};
Element.prototype.remove = function() {
  this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
  this.parentElement = null;
};
Element.prototype.scrollIntoView = function() {};
Element.prototype.dispatchEvent = function(event) { return this.dispatch(event.type); };
Element.prototype.getBoundingClientRect = () => ({ left: 50, top: 100, bottom: 135, width: 240, height: 200 });
window.innerHeight = 800;
const windowEvents = new Map();
window.addEventListener = (name, callback) => {
  const listeners = windowEvents.get(name) || [];
  listeners.push(callback);
  windowEvents.set(name, listeners);
};
const dispatchWindow = (name, event = {}) => {
  for (const callback of windowEvents.get(name) || []) callback(event);
};
const observers = [];
class MutationObserver {
  constructor(callback) { this.callback = callback; this.connected = true; observers.push(this); }
  observe() {}
  disconnect() { this.connected = false; }
}

// SHIPPED_SELECT_CONTROLS

const menu = () => document.querySelector(".admin-select-popover");
const rows = () => menu().querySelectorAll(".admin-select-option");
const key = (node, value) => node.dispatch("keydown", { key: value, stopPropagation() { this.stopped = true; } });
const option = (value, label, attrs = {}) => {
  const item = new Element("option", { value, ...attrs }); item.textContent = label; return item;
};
const pageSize = document.querySelector("#session-page-size");
const kind = document.querySelector("#defaults-chat-kind");
let inputs = 0, changes = 0;
pageSize.addEventListener("input", () => inputs++);
pageSize.addEventListener("change", () => changes++);

for (const select of document.querySelectorAll("select")) assert.equal(select.getAttribute("aria-expanded"), "false");
pageSize.focus();
assert.equal(menu(), null, "focus alone must not open any selector");
assert.equal(pageSize.dispatch("pointerdown", { button: 0 }).prevented, true);
assert.equal(pageSize.dispatch("mousedown", { button: 0 }).prevented, true);
assert.equal(menu(), null, "early native suppression must not toggle twice");
pageSize.click();
assert.equal(menu().parentElement, document.body);
assert.equal(rows().length, 4);
assert.equal(rows()[1].getAttribute("aria-selected"), "true");
rows()[2].click();
assert.equal(pageSize.value, "50");
assert.equal(inputs, 1); assert.equal(changes, 1);
assert.equal(new URLSearchParams(new FormData(document.querySelector("#session-filter"))).get("pageSize"), "50");
assert.equal(document.activeElement, pageSize);
pageSize.click(); rows()[2].click();
assert.equal(changes, 1, "same selection must not dispatch spurious changes");

key(pageSize, "ArrowDown");
key(pageSize, "End");
key(pageSize, "Enter");
assert.equal(pageSize.value, "100");
assert.equal(changes, 2);
key(pageSize, "F4");
key(pageSize, "ArrowLeft");
assert.equal(pageSize.value, "100", "navigation only highlights until committed");
key(pageSize, "Enter");
assert.equal(pageSize.value, "50");
key(pageSize, "F4"); key(pageSize, "F4");
assert.equal(menu(), null);
pageSize.click();
const escape = key(pageSize, "Escape");
assert.equal(escape.prevented, true); assert.equal(escape.stopped, true);
assert.equal(menu(), null);
pageSize.click();
assert.equal(key(pageSize, "Tab").prevented, false);
assert.equal(document.activeElement, pageSize, "Tab resumes native field order");
assert.equal(menu(), null);

kind.click();
assert.equal(menu().parentElement, document.querySelector("#defaults-drawer"), "popup stays in dialog's top layer");
assert.equal(rows().length, 2, "hidden unknown chat kind is not selectable");
assert.equal(rows()[0].textContent, "群聊");
rows()[1].click();
assert.equal(kind.value, "p2p");
kind.disabled = true; kind.click();
assert.equal(menu(), null);
kind.disabled = false;
const fields = kind.closest("fieldset");
fields.disabled = true; kind.click();
assert.equal(menu(), null, "fieldset locking also disables menu");
fields.disabled = false; kind.click();
fields.disabled = true; rows()[0].click();
assert.equal(kind.value, "p2p", "late lock cannot be bypassed by stale option");
observers.at(-1).callback();
assert.equal(menu(), null);
fields.disabled = false;

const project = document.querySelector("#defaults-project");
project.replaceChildren(option("", "选择 Project"), ...Array.from({ length: 10 }, (_, i) => option(`p${i}`, `Project ${i}`)));
project.value = "";
project.click();
const search = menu().querySelector("input");
assert.equal(search.hidden, false);
assert.equal(document.activeElement, search);
search.value = "project 8"; search.dispatch("input");
assert.equal(rows().filter((row) => !row.hidden).length, 1);
key(search, "Enter");
assert.equal(project.value, "p8");
assert("required" in project.attrs, "original required field is preserved");
project.click();
menu().querySelector("input").value = "no-match";
menu().querySelector("input").dispatch("input");
assert.equal(menu().querySelector(".multi-filter-empty").hidden, false);
key(menu().querySelector("input"), "Enter");
assert.equal(project.value, "p8");
window.NetizenSelect.close();

project.replaceChildren(option("old", "Old")); project.value = "old"; project.click();
const stale = rows()[0];
project.replaceChildren(option("new", "New")); project.value = "new";
stale.click();
assert.equal(project.value, "new", "removed catalog choices cannot be applied");
observers.at(-1).callback();
assert.equal(menu(), null);
project.replaceChildren(option("enabled", "Enabled"), option("disabled", "Disabled", { disabled: "" }));
project.value = "enabled"; project.click(); rows()[1].click();
assert.equal(project.value, "enabled");
key(project, "End"); key(project, "Enter");
assert.equal(project.value, "enabled");

pageSize.click(); kind.focus();
assert.equal(menu(), null, "focus outside closes popup");
pageSize.click(); kind.dispatch("pointerdown");
assert.equal(menu(), null, "outside pointer closes popup");
pageSize.click(); dispatchWindow("scroll", { target: menu().querySelector(".multi-filter-options") });
assert(menu(), "option-list scrolling must not close its own popup");
dispatchWindow("scroll", { target: document });
assert.equal(menu(), null);
kind.click(); document.querySelector("#defaults-drawer").dispatch("close");
assert.equal(menu(), null);
pageSize.click(); dispatchWindow("resize");
assert.equal(menu(), null);
pageSize.getBoundingClientRect = () => ({ left: 50, top: 745, bottom: 780, width: 240, height: 35 });
pageSize.click();
assert.equal(menu().style.top, "auto", "upward menu must not stretch from viewport top");
assert.equal(menu().style.bottom, "61px");
window.NetizenSelect.close();
assert(observers.every((observer) => !observer.connected));
