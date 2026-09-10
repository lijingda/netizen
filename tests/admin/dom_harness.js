const assert = require("node:assert/strict");

// Small DOM port: controller logic and static markup both come from shipped files.
class Element {
  constructor(tag, attrs = {}) {
    this.tagName = tag;
    this.attrs = { ...attrs };
    this.children = [];
    this.listeners = new Map();
    this.dataset = Object.fromEntries(Object.entries(attrs)
      .filter(([key]) => key.startsWith("data-"))
      .map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()), value || ""]));
    this.hidden = "hidden" in attrs;
    this.checked = "checked" in attrs;
    this.disabled = "disabled" in attrs;
    this.value = attrs.value || "";
    this.name = attrs.name || "";
    this.type = attrs.type || "";
    this.id = attrs.id || "";
    this.className = attrs.class || "";
    this.style = { removeProperty() {} };
    this.classList = { toggle() {} };
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node.tagName === "fragment") this.append(...node.children);
      else { node.parentElement = this; this.children.push(node); }
    }
  }
  replaceChildren(...nodes) { this.children = []; this.text = ""; this.append(...nodes); }
  get textContent() { return (this.text || "") + this.children.map((node) => node.textContent).join(""); }
  set textContent(value) { this.children = []; this.text = value; }
  setAttribute(key, value) {
    this.attrs[key] = value;
    if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  removeAttribute(key) { delete this.attrs[key]; }
  contains(target) { return target === this || this.children.some((child) => child.contains(target)); }
  getBoundingClientRect() { return { left: 20, width: 240 }; }
  matches(selector) {
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    if (selector.startsWith(".")) return this.className === selector.slice(1);
    if (selector.startsWith("[")) return selector.slice(1, -1) in this.attrs;
    if (selector.includes(":not(:disabled)")) {
      return this.tagName === selector.split(":")[0] && !this.disabled;
    }
    return this.tagName === selector;
  }
  querySelectorAll(selector) {
    if (selector.includes(" ") && !selector.includes(",")) {
      const [ancestor, descendant] = selector.split(" ");
      return this.querySelectorAll(ancestor).flatMap((node) => node.querySelectorAll(descendant));
    }
    const selectors = selector.split(",").map((value) => value.trim());
    return this.children.flatMap((child) => [
      ...(selectors.some((s) => child.matches(s)) ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(name, callback) {
    const callbacks = this.listeners.get(name) || [];
    callbacks.push(callback);
    this.listeners.set(name, callbacks);
  }
  dispatch(name, props = {}) {
    const event = { target: this, prevented: false,
      preventDefault() { this.prevented = true; }, ...props };
    for (let node = this; node; node = node.parentElement) {
      event.currentTarget = node;
      for (const callback of node.listeners.get(name) || []) callback(event);
    }
    return event;
  }
  click() {
    if (this.type === "checkbox") {
      this.checked = !this.checked;
      this.dispatch("change");
    }
    this.dispatch("click");
  }
  focus() {
    const previous = document.activeElement;
    document.activeElement = this;
    previous?.dispatch("focusout", { relatedTarget: this });
  }
  cloneNode() {
    const result = new Element(this.tagName, this.attrs);
    result.text = this.text;
    result.append(...this.children.map((child) => child.cloneNode(true)));
    return result;
  }
  showModal() { this.open = true; this.returnFocus = document.activeElement; }
  close() { this.open = false; this.returnFocus?.focus(); }
  reset() {
    for (const input of this.querySelectorAll("input, select")) {
      input.value = input.tagName === "select"
        ? input.children.find((option) => "selected" in option.attrs)?.value || input.children[0]?.value
        : input.attrs.value || "";
    }
  }
}
class HTMLTemplateElement extends Element {}
function inflate(node) {
  if (typeof node === "string") {
    const text = new Element("text"); text.text = node; return text;
  }
  const result = node.tag === "template"
    ? new HTMLTemplateElement(node.tag, node.attrs) : new Element(node.tag, node.attrs);
  if (node.tag === "template") {
    result.content = new Element("fragment");
    result.content.append(...node.children.map(inflate));
  } else result.append(...node.children.filter((child) => typeof child !== "string" || child.trim()).map(inflate));
  if (node.tag === "select") result.value = result.children
    .find((option) => "selected" in option.attrs)?.value || result.children[0]?.value;
  return result;
}
const document = inflate(htmlTree);
document.createElement = (tag) => new Element(tag);
document.createTextNode = (text) => { const node = new Element("text"); node.text = text; return node; };
document.body = document.querySelector("body");
const window = { innerWidth: 1200, addEventListener() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }) };
class FormData {
  constructor(form) {
    this.entries = form.querySelectorAll("input, select")
      .filter((input) => input.name && !input.disabled && (input.type !== "checkbox" || input.checked))
      .map((input) => [input.name, input.value]);
  }
  [Symbol.iterator]() { return this.entries[Symbol.iterator](); }
}
