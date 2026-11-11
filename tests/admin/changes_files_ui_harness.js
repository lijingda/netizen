const media = { matches: false, listeners: [], addEventListener(_, callback) { this.listeners.push(callback); } };
const pointer = { matches: false, listeners: [], addEventListener(_, callback) { this.listeners.push(callback); } };
window.matchMedia = (query) => query === "(pointer: coarse)" ? pointer : media;
const windowListeners = new Map();
window.addEventListener = (name, callback) => {
  if (!windowListeners.has(name)) windowListeners.set(name, []);
  windowListeners.get(name).push(callback);
};
const domListen = Element.prototype.addEventListener;
Element.prototype.addEventListener = function (name, callback, options) {
  if (!(options === true || options?.capture)) return domListen.call(this, name, callback);
  this.captures ||= new Map();
  if (!this.captures.has(name)) this.captures.set(name, []);
  this.captures.get(name).push(callback);
};
Element.prototype.removeEventListener = function (name, callback, options) {
  const registry = options === true || options?.capture ? this.captures : this.listeners;
  registry?.set(name, (registry.get(name) || []).filter((item) => item !== callback));
};
Element.prototype.dispatch = function (name, props = {}) {
  const event = { target: this, prevented: false,
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...props };
  const path = [];
  for (let node = this; node; node = node.parentElement) path.push(node);
  for (const node of [...path].reverse()) {
    event.currentTarget = node;
    for (const callback of node.captures?.get(name) || []) callback(event);
    if (event.stopped) return event;
  }
  for (const node of path) {
    event.currentTarget = node;
    for (const callback of node.listeners.get(name) || []) callback(event);
    if (event.stopped) break;
  }
  return event;
};
Element.prototype.setPointerCapture = function (id) { this.capturedPointer = id; };
Element.prototype.hasPointerCapture = function (id) { return this.capturedPointer === id; };
Element.prototype.releasePointerCapture = function () {
  const pointerId = this.capturedPointer; this.capturedPointer = null; this.dispatch("lostpointercapture", { pointerId });
};
const observers = [];
class Observer {
  constructor(callback) { this.callback = callback; this.targets = new Set(); observers.push(this); }
  observe(target) { this.targets.add(target); }
  disconnect() { this.targets.clear(); }
  notify() { if (this.targets.size) this.callback(); }
}
window.MutationObserver = window.ResizeObserver = Observer;
const until = async (condition) => {
  for (let turn = 0; turn < 200 && !condition(); turn += 1) await Promise.resolve();
  assert(condition(), "bounded UI condition did not settle");
};
const changeWidth = async (compact) => {
  media.matches = compact;
  for (const callback of media.listeners) callback();
  await until(() => input("diff").hidden || diffViews.at(-1)?.model.inline === (compact || storedLayout === "unified" ? "b" : false));
};
const changesRoute = "/api/v1/project-files/changes";
const diffRoute = "/api/v1/project-files/diff";
const directoryRoute = "/api/v1/project-files/directory";
const countReads = (operation) => requests.filter((request) => request.route.endsWith(`/${operation}`)).length;
const paths = () => input("entries").children.filter((row) => row.dataset.path).map((row) => row.dataset.path);
const filterChanges = async (value) => {
  input("changes-only").checked = value;
  await trigger("changes-only", "change");
};
const filterName = async (value) => { input("filter").value = value; await trigger("filter", "input"); };
const diffViews = [];
let chunkFixture = null;
class Range {
  constructor(startRow, startColumn, endRow, endColumn) {
    this.start = { row: startRow, column: startColumn }; this.end = { row: endRow, column: endColumn };
  }
}
const chunk = (start, end, oldStart = start, oldEnd = end) => ({
  new: new Range(start, 0, end, 0), old: new Range(oldStart, 0, oldEnd, 0),
});
const emitter = (target) => {
  target.events = new Map();
  target.on = (name, callback, capturing = false) => {
    const callbacks = target.events.get(name) || [];
    callbacks[capturing ? "unshift" : "push"](callback); target.events.set(name, callbacks);
  };
  target.off = (name, callback) => target.events.set(name, (target.events.get(name) || []).filter((fn) => fn !== callback));
  target.emit = (name, event = {}) => {
    for (const callback of target.events.get(name) || []) { callback(event); if (event.stopped) break; }
  };
  target.listenerCount = () => [...target.events.values()].reduce((sum, list) => sum + list.length, 0);
};
let extensionLoaded = false, storedLayout = null, storageThrows = false, storageKey;
window.localStorage = {
  getItem(key) { if (storageThrows) throw new Error("storage denied"); storageKey = key; return storedLayout; },
  setItem(key, value) { if (storageThrows) throw new Error("storage denied"); assert.equal(key, storageKey); storedLayout = value; },
};
const liveViews = () => {
  assert(diffViews.filter((view) => !view.destroyed).length <= 1, "at most one live diff view");
  assert(editors.filter((editor) => !editor.destroyed && !editor.diff).length <= 1, "at most one live source editor");
};
const extension = { createDiffView(model) {
  liveViews();
  assert(diffViews.every((view) => view.destroyed), "destroy the old diff before constructing its replacement");
  const a = window.ace.edit(), b = window.ace.edit();
  a.diff = b.diff = true;
  a.container = document.createElement("div"); b.container = document.createElement("div");
  a.text = model.valueA; b.text = model.valueB;
  const view = { model, editorA: a, editorB: b, sessionA: a.session, sessionB: b.session,
    resize() { assert(!this.destroyed); }, onInput() {
      this.computed = true;
      if (chunkFixture) this.chunks = chunkFixture;
      else {
        const old = a.text.split(/\r\n|\r|\n/), fresh = b.text.split(/\r\n|\r|\n/);
        let start = 0, oldEnd = old.length, end = fresh.length;
        while (start < Math.min(oldEnd, end) && old[start] === fresh[start]) start++;
        while (oldEnd > start && end > start && old[oldEnd - 1] === fresh[end - 1]) { oldEnd--; end--; }
        this.chunks = start === oldEnd && start === end ? [] : [chunk(start, end, start, oldEnd)];
      }
    },
    destroy() {
      assert(!this.destroyed, "destroy each diff exactly once");
      for (const editor of [a, b]) {
        assert.equal(editor.listenerCount(), 0, "remove editor listeners before disposal");
        assert.equal(editor.session.listenerCount(), 0, "remove scroll listeners before disposal");
        editor.container.listeners.clear();
      }
      this.destroyed = true; a.destroy(); b.destroy();
    } };
  view.explicitAdds = [];
  const render = () => {
    for (const editor of [a, b]) {
      editor.container.replaceChildren();
      for (const [index, fold] of editor.session.folds.entries()) {
        const bar = document.createElement("span"); bar.className = "ace_fold";
        bar.textContent = fold.placeholder; bar.fold = fold;
        bar.getBoundingClientRect = () => ({ left: editor === a ? 40 : 440, top: 100 + index * 60,
          right: editor === a ? 340 : 740, bottom: 120 + index * 60, width: 300, height: 20 });
        editor.container.append(bar);
      }
    }
    for (const observer of observers) observer.notify();
  };
  for (const editor of [a, b]) {
    emitter(editor); emitter(editor.session);
    const session = editor.session, side = editor === a ? "old" : "new", other = editor === a ? b.session : a.session;
    session.folds = [];
    session.getAllFolds = () => session.folds.slice();
    session.getFoldAt = (row, column) => session.folds.find((fold) =>
      row >= fold.start.row && row <= fold.end.row && (row !== fold.start.row || column >= fold.start.column)
      && (row !== fold.end.row || column <= fold.end.column));
    session.addFold = (placeholder, range, mirrored = false) => {
      assert(view.computed, "fold only after computing chunks");
      assert(range instanceof Range);
      range.end.column = Math.min(range.end.column, editor.text.split(/\r\n|\r|\n/)[range.end.row].length);
      const fold = { placeholder, range, start: range.start, end: range.end };
      session.folds.push(fold); session.folds.sort((a, b) => a.start.row - b.start.row);
      if (!mirrored) {
        view.explicitAdds.push(side);
        let offset = 0;
        for (const change of view.chunks) if (change[side].end.row <= range.start.row)
          offset = change[side === "new" ? "old" : "new"].end.row - change[side].end.row;
        fold.other = other.addFold(placeholder, new Range(range.start.row + offset, range.start.column,
          range.end.row + offset, range.end.column), true);
        fold.other.other = fold;
        render();
      }
      return fold;
    };
    session.removeFold = (fold, mirrored = false) => {
      assert(session.folds.includes(fold), "remove only an existing fold");
      session.folds.splice(session.folds.indexOf(fold), 1);
      if (!mirrored) { other.removeFold(fold.other, true); render(); }
    };
    editor.renderer = { screenToTextCoordinates(x, y) {
      const bar = editor.container.children.find((bar) => {
        const rect = bar.getBoundingClientRect(); return y >= rect.top && y <= rect.bottom;
      });
      return bar ? { row: bar.fold.start.row, column: 0 } : { row: 0, column: 0 };
    } };
    editor.container.addEventListener("click", (domEvent) => {
      const event = { domEvent, getDocumentPosition: () => domEvent.target.fold?.start || { row: 0, column: 0 },
        stop() { this.stopped = true; domEvent.preventDefault(); domEvent.stopPropagation(); } };
      editor.emit("click", event);
      if (!event.stopped && domEvent.target.fold) session.removeFold(domEvent.target.fold);
    });
  }
  diffViews.push(view);
  liveViews();
  return view;
} };
window.ace.require = (name) => {
  if (name === "ace/range") return { Range };
  assert.equal(name, "ace/ext/diff"); return extensionLoaded ? extension : null;
};
const changed = (path, kind, directory = false) => ({ path, kind, directory,
  group: ["untracked", "ignored"].includes(kind) ? kind : "tracked" });
const snapshot = [changed("a.py", "modified"), changed("gone.py", "deleted"), changed("empty.txt", "added"),
  changed("readme.md", "untracked"), changed("README.md", "deleted"), changed("src/child.py", "modified"), changed("src/gone.py", "deleted"),
  changed("removed/deep/gone.py", "deleted"), changed("nested", "untracked", true),
  changed("ignored", "ignored", true), changed("unknown", "ignored"), changed("conflict.py", "conflict")];
const result = (patch = {}) => ({ git: "available", head: "head-one", complete: true,
  groupsComplete: { tracked: true, untracked: true, ignored: true }, entries: snapshot, ...patch });
const diffResult = (patch = {}) => ({ git: "available", head: "head-one", kind: "modified",
  old: "old\n", new: "new\n", language: "python", ...patch });
const listing = (path, names) => ({ path, entries: names.map(([name, kind = "file"]) => ({ name,
  path: path ? `${path}/${name}` : name, kind })), truncated: false });
const directoryFixtures = () => {
  overrides.set(`${directoryRoute}:`, () => listing("", [["src", "directory"], ["nested", "directory"],
    ["ignored", "directory"], ["clean", "directory"], ...Object.keys(files).map((name) => [name]), ["conflict.py"]]));
  overrides.set(`${directoryRoute}:src`, () => listing("src", [["child.py"], ["clean.py"]]));
  overrides.set(`${directoryRoute}:nested`, () => listing("nested", [["inner", "directory"], ["anything.py"]]));
  overrides.set(`${directoryRoute}:nested/inner`, () => listing("nested/inner", [["any.txt"]]));
  overrides.set(`${directoryRoute}:ignored`, () => listing("ignored", [["child.py"]]));
  overrides.set(`${directoryRoute}:clean`, () => listing("clean", [["unchanged.txt"]]));
};
const foldRows = (session) => session.getAllFolds().map((fold) => [fold.start.row, fold.end.row + 1]);
const menus = () => input("diff").querySelectorAll(".file-browser-diff-menu");
const menuOptions = () => menus()[0].children.map((item) => item.dataset.direction);
const clickFold = (editor, index = 0) => {
  const bar = editor.container.children[index], count = editor.session.getAllFolds().length;
  assert(bar, "requested fold bar exists");
  bar.dispatch("click");
  assert.equal(menus().length, 1, "only one fold menu");
  assert.equal(editor.session.getAllFolds().length, count, "capturing click suppresses Ace expand-all");
  assert.equal(document.activeElement, menus()[0].children[0], "menu receives focus");
  return bar;
};
const expand = (direction) => {
  const item = menus()[0].children.find((item) => item.dataset.direction === direction);
  assert(item, `offered ${direction}`); item.click();
  assert.equal(menus().length, 0, "action closes menu");
};
const longChunks = [chunk(30, 31, 30, 33), chunk(90, 92, 92, 93)];
const setDiffFixture = (rows = 160, chunks = longChunks, oldRows = 161) => {
  chunkFixture = chunks;
  const text = (length) => Array.from({ length }, (_, row) => `line ${row}`).join("\n");
  overrides.set(`${diffRoute}:a.py`, () => diffResult({ old: text(oldRows), new: text(rows) }));
};
const listenerCount = (element) => [...(element.captures?.values() || []), ...element.listeners.values()]
  .reduce((total, callbacks) => total + callbacks.length, 0);
input("diff").getBoundingClientRect = () => ({ left: 20, top: 70, right: 800, bottom: 650, width: 780, height: 580 });

(async () => {
  const originalDocumentListeners = listenerCount(document);
  const browser = window.NetizenFileBrowser.create({ container: input("drawer"), api, isExpired: () => expired });
  window.NetizenProjectFiles.init({ browser });
  window.NetizenAttachmentFiles.init({ browser, api, isExpired: () => expired });
  const open = () => window.NetizenProjectFiles.open(project);
  await open();
  assert.equal(input("changes-tools").hidden, true);
  await openFile("a.py");
  assert.equal(input("difference").hidden, true, "no Git entry for non repositories");

  const loading = defer();
  directoryFixtures();
  overrides.set(`${changesRoute}:`, () => loading.promise);
  await open();
  assert(entry("a.py"), "Git must not delay the directory tree");
  assert.equal(input("changes-tools").hidden, false);
  assert.equal(input("changes-only").disabled, true);
  assert.match(input("changes-note").textContent, /正在/);
  assert.equal(countReads("diff"), 0);
  loading.resolve({ git: "unavailable", message: "Git busy" });
  await until(() => !input("changes-retry").hidden);
  assert.equal(input("changes-only").disabled, true);
  assert.equal(input("changes-current").hidden, true);
  assert.match(input("changes-note").textContent, /Git busy/);
  await openFile("src");
  assert.equal(input("changes-current").hidden, false);
  overrides.set(`${changesRoute}:src`, () => result({ entries: snapshot.filter((item) => item.path.startsWith("src/")) }));
  await trigger("changes-current");
  assert.equal(requests.at(-1).query.get("path"), "src");
  assert.match(input("changes-note").textContent, /src/);
  assert.equal(input("changes-root").hidden, false);
  await filterChanges(true);
  assert.deepEqual(paths(), ["src", "src/child.py", "src/gone.py"], "scope excludes other subtrees");
  overrides.set(`${changesRoute}:`, () => result());
  await trigger("changes-root");
  assert.equal(requests.at(-1).query.get("path"), "");
  assert.equal(input("changes-root").hidden, true);
  assert.equal(input("changes-count").textContent, "(10)");
  assert.equal(input("changes-only").disabled, false);
  assert(entry("gone.py") && entry("removed"));
  const sortedRoot = ["nested", "removed", "src", "a.py", "conflict.py", "empty.txt", "gone.py", "README.md", "readme.md"];
  assert.deepEqual(paths().filter((path) => !path.includes("/")), sortedRoot,
    "synthesized and disk rows share directory-first, case-insensitive name, raw-name ordering");
  assert(!entry("unknown") && !entry("ignored") && !entry("clean") && !entry("image.png"));
  assert.match(entry("gone.py").getAttribute("aria-label"), /删除/);
  assert.match(entry("empty.txt").getAttribute("aria-label"), /新增/);
  assert.match(entry("readme.md").getAttribute("aria-label"), /未跟踪/);
  assert.match(entry("conflict.py").getAttribute("aria-label"), /冲突/);

  const readsBeforeToggle = requests.length;
  const expanded = entry("src").getAttribute("aria-expanded");
  await filterChanges(false);
  assert.equal(entry("src").getAttribute("aria-expanded"), expanded);
  assert(entry("src/clean.py") && !entry("gone.py") && !entry("removed"));
  assert.match(entry("ignored").className, /file-browser-ignored/);
  assert.match(entry("unknown").getAttribute("aria-label"), /已忽略/);
  await filterChanges(true);
  assert.equal(entry("src").getAttribute("aria-expanded"), expanded);
  assert(!entry("src/clean.py"));
  assert.equal(requests.length, readsBeforeToggle, "filtering changes neither directory loading nor cached expansion");
  await openFile("src");
  assert.equal(entry("src").getAttribute("aria-expanded"), "false");
  await filterChanges(false); await filterChanges(true);
  assert.equal(entry("src").getAttribute("aria-expanded"), "false", "collapsed branches stay collapsed");
  assert.equal(requests.length, readsBeforeToggle);
  await openFile("src");
  await filterName("child");
  assert.deepEqual(paths(), ["src", "src/child.py"]);
  await filterName("");
  const beforeDeleted = requests.length;
  await openFile("removed"); await openFile("removed/deep");
  assert(entry("removed/deep/gone.py"));
  assert.match(entry("removed").getAttribute("aria-label"), /删除/);
  assert.equal(requests.length, beforeDeleted, "fully deleted directories expand using only the snapshot");
  await filterChanges(false); assert(!entry("removed")); await filterChanges(true);
  assert(entry("removed/deep/gone.py"), "synthesized branch expansion also survives the checkbox");
  const statusReads = countReads("changes");
  await openFile("nested"); await openFile("nested/inner");
  assert(entry("nested/anything.py") && entry("nested/inner/any.txt"), "nested repositories expose their disk listing");
  assert.equal(countReads("changes"), statusReads, "directory expansion never reads status");
  assert.match(entry("nested").getAttribute("aria-label"), /未跟踪/);

  overrides.set(`${diffRoute}:a.py`, () => diffResult());
  overrides.set(`${diffRoute}:gone.py`, () => diffResult({ kind: "deleted", new: "" }));
  overrides.set(`${diffRoute}:conflict.py`, () => diffResult({ kind: "conflict", old: null, new: null, reason: "conflict", message: "conflict" }));
  await filterChanges(false);
  await openFile("a.py");
  assert.equal(input("editor").hidden, false);
  assert.equal(countReads("diff"), 0);
  assert.equal(document.querySelectorAll("script").some((script) => script.src?.endsWith("ext-diff.js")), false);
  const firstDiff = trigger("difference");
  await until(() => document.querySelectorAll("script").some((script) => script.src?.endsWith("ext-diff.js")));
  const script = document.querySelectorAll("script").find((script) => script.src?.endsWith("ext-diff.js"));
  await openFile("readme.md");
  extensionLoaded = true; script.onload(); await firstDiff;
  assert.equal(input("diff").hidden, true, "late extension loading cannot revive a previous file");
  assert.equal(diffViews.length, 0);
  const markdownReads = countReads("content");
  await trigger("preview");
  assert.equal(input("markdown").hidden, false);
  await trigger("source");
  assert.equal(input("editor").hidden, false);
  assert.equal(countReads("content"), markdownReads);
  assert.equal(input("difference").hidden, true, "untracked content has no diff candidate");

  setDiffFixture();
  await filterChanges(true); await openFile("a.py");
  assert.equal(input("difference").getAttribute("aria-pressed"), "true");
  let wide = diffViews.at(-1);
  assert.equal(wide.model.inline, false);
  assert.deepEqual(foldRows(wide.sessionB), [[0, 27], [34, 87], [95, 160]]);
  assert.deepEqual(foldRows(wide.sessionA), [[0, 27], [36, 89], [96, 161]], "mirror rows account for insertions/deletions");
  assert(wide.explicitAdds.every((side) => side === "new"), "initial folds are added on B only");
  assert(wide.sessionB.folds.every((fold) => fold.placeholder.length === 120));
  for (const editor of [wide.editorA, wide.editorB]) {
    assert.equal(editor.worker, false); assert.equal(editor.readonly, true); assert.equal(editor.mode, "ace/mode/python");
    assert.equal(editor.options.enableMobileMenu, false, "diff never leaves Ace's touch menu alongside our fold menu");
  }
  clickFold(wide.editorB); assert.deepEqual(menuOptions(), ["up", "all"], "first gap has context only below");
  expand("up"); assert.deepEqual(foldRows(wide.sessionB)[0], [0, 7]);
  clickFold(wide.editorA); assert.deepEqual(menuOptions(), ["all"], "at most STEP rows offers only full expansion");
  expand("all"); assert.equal(wide.sessionB.folds.length, 2);
  clickFold(wide.editorA); assert.deepEqual(menuOptions(), ["down", "up", "all"]);
  expand("down");
  assert.deepEqual(foldRows(wide.sessionA)[0], [56, 89]);
  assert.deepEqual(foldRows(wide.sessionB)[0], [54, 87]);
  assert.equal(wide.explicitAdds.at(-1), "old", "shrink on the clicked session and let ext-diff mirror");
  clickFold(wide.editorB); expand("up");
  assert.deepEqual(foldRows(wide.sessionB)[0], [54, 67]);
  assert.deepEqual(foldRows(wide.sessionA)[0], [56, 69]);
  clickFold(wide.editorB, 1); assert.deepEqual(menuOptions(), ["down", "all"], "last gap has context only above");
  expand("down"); assert.deepEqual(foldRows(wide.sessionB)[1], [115, 160]);
  clickFold(wide.editorB);
  const anchor = clickFold(wide.editorB, 1);
  assert.equal(menus().length, 1, "opening another fold replaces its menu");
  const escaped = document.activeElement.dispatch("keydown", { key: "Escape" });
  assert(escaped.prevented && escaped.stopped); assert.equal(menus().length, 0);
  assert.equal(document.activeElement, anchor, "Escape restores fold focus");
  anchor.dispatch("keydown", { key: "Enter" }); assert.equal(menus().length, 1, "folds are keyboard reachable");
  input("filename").dispatch("pointerdown"); assert.equal(menus().length, 0);
  clickFold(wide.editorB); input("filename").dispatch("click"); assert.equal(menus().length, 0);
  for (const session of [wide.sessionA, wide.sessionB]) for (const name of ["changeScrollTop", "changeScrollLeft"]) {
    clickFold(wide.editorB); session.emit(name); assert.equal(menus().length, 0, "scroll dismisses menu");
  }
  for (const [rows, chunks, expected] of [
    [18, [chunk(5, 6), chunk(6, 7), chunk(12, 13)], [[0, 2], [16, 18]]],
    [17, [chunk(4, 5), chunk(12, 13)], []],
    [40, [chunk(0, 1), chunk(39, 40)], [[4, 36]]],
    [20, [chunk(0, 20)], []], [30, [], []],
  ]) {
    setDiffFixture(rows, chunks, rows); await openFile("a.py");
    assert.deepEqual(foldRows(diffViews.at(-1).sessionB), expected, "3 context rows, adjacent changes and tiny gaps");
  }
  setDiffFixture(50, [chunk(20, 20, 20, 23)], 53); await openFile("a.py");
  assert.deepEqual(foldRows(diffViews.at(-1).sessionB), [[0, 17], [23, 50]], "deletion-only chunk keeps both contexts");
  for (const direction of ["up", "down"]) for (const hidden of [20, 21, 22]) {
    setDiffFixture(hidden + 7, [direction === "up" ? chunk(hidden + 3, hidden + 4) : chunk(3, 4)], hidden + 7);
    await openFile("a.py");
    const view = diffViews.at(-1); clickFold(view.editorB);
    assert.deepEqual(menuOptions(), hidden <= 20 ? ["all"] : [direction, "all"]);
    expand(hidden <= 20 ? "all" : direction);
    assert.deepEqual(foldRows(view.sessionB), hidden === 22 ? [direction === "up" ? [0, 2] : [27, 29]] : [],
      "fewer than 2 remaining lines removes fold");
    assert.deepEqual(foldRows(view.sessionA), foldRows(view.sessionB));
  }
  setDiffFixture(); await openFile("a.py"); wide = diffViews.at(-1);
  const reads = requests.length;
  clickFold(wide.editorB);
  await trigger("diff-fold"); assert.equal(wide.sessionB.folds.length, 0); assert.match(input("diff-fold").textContent, /折叠/);
  assert.equal(menus().length, 0);
  await trigger("diff-fold"); assert.deepEqual(foldRows(wide.sessionB), [[0, 27], [34, 87], [95, 160]]);
  clickFold(wide.editorA); expand("up"); assert.deepEqual(foldRows(wide.sessionB)[0], [0, 7]);
  clickFold(wide.editorA);
  await trigger("unified");
  assert.equal(wide.destroyed, true); assert.equal(diffViews.at(-1).model.inline, "b");
  assert.equal(menus().length, 0);
  assert.equal(storedLayout, "unified"); assert.equal(diffViews.at(-1).sessionB.folds.length, 3);
  await trigger("split"); await changeWidth(true);
  await until(() => diffViews.at(-1).model.inline === "b");
  assert.equal(input("diff-layout").hidden, true);
  const compactView = diffViews.at(-1), bar = compactView.editorB.container.children[0];
  const point = { clientX: 450, clientY: 110 };
  assert(bar.dispatch("touchstart", { touches: [point] }).stopped);
  const tap = bar.dispatch("touchend", { changedTouches: [point] });
  assert(tap.prevented && tap.stopped, "suppress Ace touch selection/menu and compatibility click");
  assert.equal(menus().length, 1);
  assert(menus()[0].className.includes("file-browser-diff-menu"), "compact sizing hook");
  assert(parseFloat(menus()[0].style["--menu-width"]) <= 780);
  assert(parseFloat(menus()[0].style["--menu-x"]) >= 0);
  assert(parseFloat(menus()[0].style["--menu-y"]) >= 0);
  input("filename").dispatch("pointerdown");
  bar.dispatch("touchstart", { touches: [point] });
  bar.dispatch("touchmove", { touches: [{ clientX: 450, clientY: 150 }] });
  bar.dispatch("touchend", { changedTouches: [{ clientX: 450, clientY: 150 }] });
  assert.equal(menus().length, 0, "a swipe on a fold is not a tap");
  bar.dispatch("touchstart", { touches: [point] });
  bar.dispatch("touchend", { changedTouches: [{ clientX: 450, clientY: 150 }] });
  assert.equal(menus().length, 0, "touchend displacement also rejects a swipe");
  bar.dispatch("touchstart", { touches: [point] }); bar.dispatch("touchcancel");
  bar.dispatch("touchend", { changedTouches: [point] }); assert.equal(menus().length, 0);
  assert(!compactView.editorB.container.dispatch("touchstart", { touches: [point] }).stopped,
    "ordinary text touches remain owned by Ace");
  clickFold(compactView.editorB);
  await changeWidth(false); await until(() => diffViews.at(-1).model.inline === false);
  assert.equal(menus().length, 0);
  assert.equal(input("diff-layout").hidden, false);
  assert.equal(requests.length, reads, "layouts and breakpoints render cached texts");
  const diffReads = countReads("diff"), contentReads = countReads("content");
  for (let i = 0; i < 4; i += 1) {
    clickFold(diffViews.at(-1).editorB);
    await trigger("source"); liveViews();
    assert.equal(menus().length, 0);
    assert.equal(listenerCount(input("diff")), 0, "diff DOM listeners leave with the view");
    assert.equal(editors.at(-1).options.customScrollbar, undefined, "fine-pointer source options unchanged");
    assert.equal(editors.at(-1).options.enableMobileMenu, undefined, "source retains Ace's native touch menu");
    await trigger("difference"); liveViews();
    await trigger("unified"); await trigger("split"); liveViews();
  }
  assert.equal(countReads("diff"), diffReads, "diff fetched once for the open file");
  assert.equal(countReads("content"), contentReads + 1, "source fetched once for the open file");
  clickFold(diffViews.at(-1).editorB);
  await openFile("readme.md"); assert.equal(menus().length, 0, "file change closes menu");
  assert.equal(listenerCount(input("diff")), 0);
  await openFile("a.py");
  clickFold(diffViews.at(-1).editorB);
  input("root").value = "one"; await trigger("root", "change");
  assert.equal(menus().length, 0, "root change closes menu"); assert.equal(listenerCount(input("diff")), 0);
  await filterChanges(true); await openFile("a.py"); clickFold(diffViews.at(-1).editorB);
  browser.close(); assert.equal(menus().length, 0); assert.equal(listenerCount(input("diff")), 0);
  assert.equal(listenerCount(document), originalDocumentListeners, "outside/Escape listeners do not survive closing");
  assert(observers.every((observer) => observer.targets.size === 0), "diff observers disconnect");
  await open(); await filterChanges(true); await openFile("a.py");
  await trigger("source"); await trigger("copy"); assert.equal(clipboard, files["a.py"].text);
  chunkFixture = null;
  await openFile("gone.py");
  assert.equal(diffViews.at(-1).model.valueB, "");
  assert.equal(input("source").disabled, true); assert.equal(input("preview").disabled, true);
  assert.match(input("diff-note").textContent, /已删除/);
  const beforeDeletedSource = countReads("content");
  await trigger("source"); assert.equal(countReads("content"), beforeDeletedSource);
  await trigger("refresh"); assert.equal(input("diff").hidden, false, "refresh retains deleted selection");
  await openFile("conflict.py"); assert.equal(input("difference").getAttribute("aria-pressed"), "true");
  assert.match(input("diff-note").textContent, /conflict/);
  await openFile("empty.txt"); assert.equal(input("source").getAttribute("aria-pressed"), "true");
  assert.equal(input("difference").hidden, true);
  for (const [patch, note] of [[{ same: true, new: "old\n" }, /与 HEAD 相同/],
    [{ kind: "unchanged", old: null, new: null }, /未报告/],
    [{ reason: "mode_only", message: "mode only", old: null, new: null }, /mode only/],
    [{ head: "head-two" }, /HEAD 已变化/]]) {
    overrides.set(`${diffRoute}:a.py`, () => diffResult(patch));
    await openFile("a.py"); assert.match(input("diff-note").textContent, note);
    assert.equal(input("diff").hidden, patch.old === null);
  }

  await trigger("unified"); await open(); await openFile("a.py"); await trigger("difference");
  assert.equal(diffViews.at(-1).model.inline, "b", "wide preference persists across browser opens");
  storageThrows = true;
  await open(); await openFile("a.py"); await trigger("difference");
  assert.equal(diffViews.at(-1).model.inline, false, "denied storage defaults to split");
  await trigger("unified"); assert.equal(diffViews.at(-1).model.inline, "b", "denied writes do not break layout");
  storageThrows = false; await trigger("split");
  overrides.set(`${diffRoute}:a.py`, () => ({ git: "not_repository" }));
  await openFile("a.py"); await trigger("difference");
  assert.equal(input("changes-tools").hidden, true);
  assert.equal(input("difference").hidden, true, "authoritative non-repository response hides every Git entry");
  overrides.set(`${diffRoute}:a.py`, () => diffResult());
  await open();

  overrides.set(`${changesRoute}:`, () => result({ complete: false, groupsComplete: { tracked: true, untracked: false, ignored: false } }));
  await trigger("refresh");
  assert.match(input("changes-note").textContent, /部分/);
  assert.equal(input("changes-retry").hidden, false);
  await openFile("image.png"); assert.equal(input("difference").hidden, false, "incomplete snapshot allows authoritative diff lookup");
  overrides.set(`${diffRoute}:image.png`, () => diffResult({ kind: "unchanged", old: null, new: null }));
  await trigger("difference"); assert.match(input("diff-note").textContent, /未报告/);
  await changeWidth(true); await trigger("back"); await openFile("src");
  assert.deepEqual(paths(), ["src/child.py", "src/clean.py"]);
  await filterChanges(true); assert.deepEqual(paths(), ["src/child.py", "src/gone.py"]);
  await filterName("gone"); assert.deepEqual(paths(), ["src/gone.py"]); await filterName("");
  await trigger("changes-current");
  assert.match(input("changes-note").textContent, /src/);
  await trigger("changes-root"); await trigger("back");
  assert.deepEqual(paths(), sortedRoot, "compact levels use the same directory-first ordering");
  await openFile("removed"); assert.deepEqual(paths(), ["removed/deep"]);
  const beforeCompactDeleted = countReads("directory");
  await openFile("removed/deep"); assert.deepEqual(paths(), ["removed/deep/gone.py"]);
  assert.equal(countReads("directory"), beforeCompactDeleted);
  await trigger("back"); await trigger("back"); await filterChanges(false); await openFile("clean");
  const beforeEmpty = requests.length;
  await filterChanges(true); assert.deepEqual(paths(), []); assert.match(input("directory-note").textContent, /没有变更/);
  assert.equal(requests.length, beforeEmpty);
  await trigger("back"); await openFile("nested");
  assert.deepEqual(paths(), ["nested/inner", "nested/anything.py"]);
  await openFile("nested/anything.py"); assert.equal(input("difference").hidden, true, "no diff for nested-repo contents even with incomplete snapshot");
  await trigger("back"); await trigger("back");

  const lateDiff = defer();
  overrides.set(`${diffRoute}:a.py`, () => lateDiff.promise);
  const pendingFile = openFile("a.py");
  await trigger("back"); await openFile("readme.md");
  lateDiff.resolve(diffResult({ new: "STALE" })); await pendingFile;
  assert.equal(input("diff").hidden, true); assert.equal(editors.at(-1).text, files["readme.md"].text);
  await trigger("back");
  const lateChanges = defer(), rootDiff = defer();
  overrides.set(`${changesRoute}:`, () => lateChanges.promise);
  overrides.set(`${diffRoute}:a.py`, () => rootDiff.promise);
  const pendingDiff = openFile("a.py"), pendingChanges = trigger("changes-retry");
  input("root").value = "one";
  overrides.set(`${changesRoute}:`, () => ({ git: "not_repository" }));
  await trigger("root", "change");
  lateChanges.resolve(result()); rootDiff.resolve(diffResult()); await Promise.all([pendingDiff, pendingChanges]);
  assert.equal(input("changes-tools").hidden, true); assert.equal(input("diff").hidden, true);
  overrides.set(`${changesRoute}:`, () => result());
  await open(); await filterChanges(true);
  const closingDiff = defer(), closingChanges = defer();
  overrides.set(`${diffRoute}:a.py`, () => closingDiff.promise);
  overrides.set(`${changesRoute}:`, () => closingChanges.promise);
  const closeDiff = openFile("a.py"), closeChanges = trigger("changes-retry");
  browser.close(); closingDiff.resolve(diffResult()); closingChanges.resolve(result());
  await Promise.all([closeDiff, closeChanges]);
  assert.equal(input("diff").hidden, true); assert.equal(input("changes-tools").hidden, true);
  assert.equal(input("entries").children.length, 0);

  const beforeAttachment = requests.length;
  await window.NetizenAttachmentFiles.open();
  assert.equal(input("changes-tools").hidden, true);
  await openFile("a.py"); assert.equal(input("view-modes").hidden, true);
  await openFile("readme.md"); await trigger("preview"); assert.equal(input("markdown").hidden, false);
  assert.equal(input("difference").hidden, true);
  assert(requests.slice(beforeAttachment).every((request) => !request.route.endsWith("/changes") && !request.route.endsWith("/diff")));
  browser.close(); liveViews();
  const tracks = () => input("drawer").querySelectorAll(".file-browser-scrollbar");
  assert.equal(tracks().length, 0, "fine pointers create no extra preview scrollbars");
  pointer.matches = true;
  for (const callback of pointer.listeners) callback();
  await window.NetizenAttachmentFiles.open();
  assert.equal(tracks().length, 2, "one persistent indicator per preview container");
  await openFile("a.py");
  assert.equal(editors.at(-1).options.customScrollbar, true, "coarse pointer opts source into Ace custom scrollbar");
  assert.equal(editors.at(-1).options.enableMobileMenu, undefined);
  const markdown = input("markdown"), picture = input("image-wrap");
  for (const content of [markdown, picture]) {
    content.clientHeight = 200; content.scrollHeight = 1000; content.scrollTop = 0;
    content.getBoundingClientRect = () => ({ left: 20, top: 80, right: 260, bottom: 280, width: 240, height: 200 });
  }
  assert(tracks().every((track) => track.hidden), "source leaves preview indicators hidden");
  const markdownTrack = tracks().find((track) => track.dataset.preview === "markdown"), thumb = markdownTrack.children[0];
  await openFile("readme.md"); await trigger("preview");
  assert.equal(markdownTrack.hidden, false, "content rendering updates overflow visibility");
  assert.equal(parseFloat(markdownTrack.style["--scrollbar-height"]), 192);
  assert.equal(parseFloat(thumb.style["--scrollbar-thumb-height"]), 38.4);
  assert.equal(parseFloat(thumb.style["--scrollbar-thumb-y"]), 0);
  markdown.scrollTop = 400; markdown.dispatch("scroll");
  assert.equal(parseFloat(thumb.style["--scrollbar-thumb-y"]), 76.8, "scroll updates proportional thumb position");
  assert(thumb.dispatch("pointerdown", { pointerId: 1, button: 0, clientY: 100 }).prevented);
  assert(thumb.hasPointerCapture(1));
  thumb.dispatch("pointerup", { pointerId: 9 }); assert(thumb.hasPointerCapture(1), "unrelated pointer cannot end drag");
  thumb.dispatch("pointermove", { pointerId: 1, clientY: 138.4 });
  assert(Math.abs(markdown.scrollTop - 600) < 0.001, "drag delta maps through scroll range / thumb travel");
  thumb.dispatch("pointermove", { pointerId: 1, clientY: 1000 }); assert.equal(markdown.scrollTop, 800);
  thumb.dispatch("pointermove", { pointerId: 1, clientY: -1000 }); assert.equal(markdown.scrollTop, 0);
  thumb.dispatch("pointerup", { pointerId: 1 }); assert(!thumb.hasPointerCapture(1));
  thumb.dispatch("pointermove", { pointerId: 1, clientY: 300 }); assert.equal(markdown.scrollTop, 0);
  markdown.clientHeight = 400;
  for (const observer of observers) observer.notify();
  assert.equal(parseFloat(markdownTrack.style["--scrollbar-height"]), 392, "ResizeObserver updates track geometry");
  markdown.clientHeight = 500;
  for (const callback of windowListeners.get("resize")) callback();
  assert.equal(parseFloat(markdownTrack.style["--scrollbar-height"]), 492, "window resize also updates without observer delivery");
  markdown.scrollHeight = 500; markdown.dispatch("load");
  assert.equal(markdownTrack.hidden, true, "no overflow hides the indicator");
  markdown.scrollHeight = 1000; markdown.dispatch("load"); assert.equal(markdownTrack.hidden, false);
  await openFile("image.png");
  const imageTrack = tracks().find((track) => track.dataset.preview === "image-wrap");
  assert.equal(markdownTrack.hidden, true); assert.equal(imageTrack.hidden, false);
  picture.scrollTop = 800; picture.dispatch("scroll");
  assert.equal(parseFloat(imageTrack.children[0].style["--scrollbar-thumb-y"]), 153.6);
  picture.scrollHeight = 200; input("image").dispatch("load"); assert.equal(imageTrack.hidden, true);
  picture.scrollHeight = 1000; input("image").dispatch("load"); assert.equal(imageTrack.hidden, false);
  const previewListenerCounts = [markdown, picture].map(listenerCount);
  for (let turn = 0; turn < 3; turn++) {
    browser.close(); assert(tracks().every((track) => track.hidden));
    assert(observers.every((observer) => observer.targets.size === 0), "close disconnects preview observers");
    await window.NetizenAttachmentFiles.open(); await openFile("image.png");
    assert.equal(tracks().length, 2); assert.deepEqual([markdown, picture].map(listenerCount), previewListenerCounts);
  }
  imageTrack.children[0].dispatch("pointerdown", { pointerId: 2, button: 0, clientY: 100 });
  browser.close(); assert(!imageTrack.children[0].hasPointerCapture(2), "close ends an active drag");
  pointer.matches = false;
  for (const callback of pointer.listeners) callback();
  await window.NetizenAttachmentFiles.open(); await openFile("a.py");
  assert.equal(editors.at(-1).options.customScrollbar, undefined);
  await openFile("readme.md"); await trigger("preview");
  assert(tracks().every((track) => track.hidden), "return to fine pointer restores native previews");
  browser.close(); liveViews();
  assert.equal(input("diff").children.length, 0);
  assert(diffViews.every((view) => view.destroyed), "created and destroyed diff counts match after close");
  assert(editors.every((editor) => editor.destroyed), "created and destroyed editor counts match after close");
  console.log("Shared tree changes filter, view modes, diff layouts, folding, disposal and races passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
