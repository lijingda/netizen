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
  await until(() => input("diff").hidden || input("diff").dataset.layout === (compact || storedLayout === "unified" ? "unified" : "split"));
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
// Native DOM insertion, including fragment consumption, for incremental expansion.
Element.prototype.remove = function () {
  if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
  this.parentElement = null;
};
Element.prototype.insertBefore = function (node, reference) {
  if (node.tagName === "fragment") {
    for (const child of [...node.children]) this.insertBefore(child, reference);
    return node;
  }
  node.remove();
  const index = reference ? this.children.indexOf(reference) : this.children.length;
  assert(index >= 0);
  this.children.splice(index, 0, node); node.parentElement = this;
  return node;
};
Object.defineProperty(Element.prototype, "firstChild", { get() { return this.children[0] || null; } });
Object.defineProperty(Element.prototype, "nextSibling", { get() {
  return this.parentElement?.children[this.parentElement.children.indexOf(this) + 1] || null;
} });
let chunkFixture = null;
class Range {
  constructor(startRow, startColumn, endRow, endColumn) {
    this.start = { row: startRow, column: startColumn }; this.end = { row: endRow, column: endColumn };
  }
}
const chunk = (start, end, oldStart = start, oldEnd = end) => ({
  new: new Range(start, 0, end, 0), old: new Range(oldStart, 0, oldEnd, 0),
});
let extensionLoaded = false, storedLayout = null, storageThrows = false, storageKey;
window.localStorage = {
  getItem(key) { if (storageThrows) throw new Error("storage denied"); storageKey = key; return storedLayout; },
  setItem(key, value) { if (storageThrows) throw new Error("storage denied"); assert.equal(key, storageKey); storedLayout = value; },
};
const liveViews = () => {
  assert(editors.filter((editor) => !editor.destroyed).length <= 1, "at most one live source editor");
  if (!input("diff").hidden) assert(editors.every((editor) => editor.destroyed), "diff creates no Ace editors");
};
const computations = [], refinements = [], tokenizations = [];
let clockTime = 0, refineCost = 0, refinement = () => ({ charChanges: [], hitTimeout: false });
const performance = { now: () => clockTime };
const extension = { DiffProvider: class {
  compute(old, fresh, options) {
    assert.deepEqual(options, { ignoreTrimWhitespace: false, maxComputationTimeMs: 500 });
    computations.push({ old, fresh });
    let chunks = chunkFixture;
    if (!chunks) {
      let start = 0, oldEnd = old.length, end = fresh.length;
      while (start < Math.min(oldEnd, end) && old[start] === fresh[start]) start++;
      while (oldEnd > start && end > start && old[oldEnd - 1] === fresh[end - 1]) { oldEnd--; end--; }
      chunks = start === oldEnd && start === end ? [] : [chunk(start, end, start, oldEnd)];
    }
    return chunks.map((change) => ({ ...change, charChanges: undefined,
      inlinePending: change.old.end.row > change.old.start.row && change.new.end.row > change.new.start.row }));
  }
  refine(old, fresh, change, options) {
    assert.equal(options.ignoreTrimWhitespace, false);
    assert(options.maxComputationTimeMs > 0 && options.maxComputationTimeMs <= 25);
    refinements.push({ old, fresh, change, options }); clockTime += refineCost;
    return refinement(change);
  }
} };
let modeLoaded = false, modeLoad = null, tokenizerUnavailable = false, themeLoads = 0, strictCSP;
class Mode {
  getTokenizer() {
    if (tokenizerUnavailable) throw new Error("tokenizer unavailable");
    const calls = []; tokenizations.push(calls);
    return { getLineTokens(line, state) {
      assert.equal(state, calls.length ? `line-${calls.length}` : "start", "tokenizer state follows each side independently");
      calls.push(line);
      return { state: `line-${calls.length}`, tokens: [
        { type: "keyword.control", value: line.slice(0, 3) }, { type: "string", value: line.slice(3) },
      ] };
    } };
  }
}
window.ace.config.loadModule = ([kind, name], callback) => {
  assert.equal(kind, "mode"); assert.equal(name, "ace/mode/python");
  if (modeLoad) modeLoad(callback);
  else { modeLoaded = true; callback({ Mode }); }
};
window.ace.config.get = (key) => { assert.equal(key, "useStrictCSP"); return strictCSP; };
window.ace.config.set = (key, value) => {
  if (key === "useStrictCSP") {
    assert(themeLoads > 0, "request the theme before flushing Ace's pending styles");
    strictCSP = value;
  }
};
window.ace.require = (name) => {
  if (name === "ace/ext/diff") return extensionLoaded ? extension : null;
  if (name === "ace/theme/textmate") { themeLoads++; return { cssClass: "ace-tm" }; }
  assert.equal(name, "ace/mode/python"); return modeLoaded ? { Mode } : null;
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
const diffRows = () => input("diff").querySelectorAll(".file-browser-diff-row");
const gapRows = () => input("diff").querySelectorAll(".file-browser-diff-gap");
const gapCounts = () => gapRows().map((row) => Number(row.children[0].textContent.split(" ")[0]));
const gapOptions = (index = 0) => gapRows()[index].querySelectorAll("button").map((item) => item.dataset.direction);
const expand = (direction, index = 0) => {
  const item = gapRows()[index].querySelectorAll("button").find((item) => item.dataset.direction === direction);
  assert(item, `offered ${direction}`); item.click();
};
const cells = () => diffRows().map((row) => row.children.map((cell) => cell.textContent));
const highlights = () => input("diff").querySelectorAll(".file-browser-diff-char").map((span) => span.textContent);
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
  assert.equal(computations.length, 0);
  const markdownReads = countReads("content");
  await trigger("preview");
  assert.equal(input("markdown").hidden, false);
  await trigger("source");
  assert.equal(input("editor").hidden, false);
  assert.equal(countReads("content"), markdownReads);
  assert.equal(input("difference").hidden, true, "untracked content has no diff candidate");

  await filterChanges(true);
  // Real public line/column conventions: zero based, half open; no trailing phantom row.
  chunkFixture = null;
  overrides.set(`${diffRoute}:a.py`, () => diffResult({ old: "same\r\nold\r\nlast\r\n", new: "same\nnew\nextra\nlast\n" }));
  const beforeEditors = editors.length;
  assert.equal(themeLoads, 0);
  assert.equal(strictCSP, undefined);
  await openFile("a.py"); liveViews();
  assert.equal(editors.length, beforeEditors, "no Ace editor created for diff");
  assert(themeLoads > 0, "the diff itself loads TextMate colors without creating a source editor");
  assert.equal(strictCSP, false, "initialize Ace's pending style injection without a VirtualRenderer");
  assert(input("diff").className.split(" ").includes("ace-tm"));
  assert.equal(input("diff").dataset.layout, "split");
  assert.deepEqual(cells(), [["1", "same", "1", "same"], ["2", "old", "2", "new"],
    ["", "", "3", "extra"], ["3", "last", "4", "last"]]);
  assert(diffRows()[2].children[1].className.includes("file-browser-diff-pad"));
  assert.deepEqual(tokenizations.at(-2), ["same", "old", "last"]);
  assert.deepEqual(tokenizations.at(-1), ["same", "new", "extra", "last"]);
  const computed = computations.length, refined = refinements.length, tokenized = tokenizations.length;
  const cachedReads = requests.length;
  strictCSP = true;
  await trigger("unified");
  assert.equal(strictCSP, true, "an explicitly configured CSP preference is preserved");
  strictCSP = false;
  assert.deepEqual(cells(), [["1", "1", " ", "same"], ["2", "", "-", "old"],
    ["", "2", "+", "new"], ["", "3", "+", "extra"], ["3", "4", " ", "last"]]);
  assert.equal(storedLayout, "unified");
  await trigger("split"); await changeWidth(true);
  await until(() => input("diff").dataset.layout === "unified" && !input("diff").hidden);
  assert.equal(input("diff-layout").hidden, true);
  await changeWidth(false);
  await until(() => input("diff").dataset.layout === "split" && !input("diff").hidden);
  assert.equal(input("diff-layout").hidden, false);
  assert.equal(computations.length, computed); assert.equal(refinements.length, refined);
  assert.equal(tokenizations.length, tokenized); assert.equal(requests.length, cachedReads);

  overrides.set(`${diffRoute}:a.py`, () => diffResult({ old: "a\nb\n", new: "a\n" }));
  await openFile("a.py");
  assert.deepEqual(cells(), [["1", "a", "1", "a"], ["2", "b", "", ""]], "shorter new side is padded");
  assert(diffRows()[1].children[3].className.includes("file-browser-diff-pad"));
  for (const [old, fresh, expected] of [["a\n", "a", 1], ["", "", 0], ["\n", "\n", 1]]) {
    overrides.set(`${diffRoute}:a.py`, () => diffResult({ old, new: fresh }));
    await openFile("a.py"); assert.equal(diffRows().length, expected);
  }
  const char = { old: new Range(0, 2, 0, 7), new: new Range(0, 2, 0, 7) };
  refinement = () => ({ charChanges: [char], hitTimeout: false });
  overrides.set(`${diffRoute}:a.py`, () => diffResult({ old: "<script>&abc\n", new: "<script>&xyz\n" }));
  await openFile("a.py");
  assert.deepEqual(highlights(), ["c", "ript", "c", "ript"], "character ranges split syntax tokens at both boundaries");
  assert.equal(diffRows()[0].children[1].textContent, "<script>&abc");
  assert.equal(input("diff").querySelectorAll("script").length, 0, "code is text, never injected markup");
  assert.equal(diffRows()[0].children[1].children[1].className, "ace_keyword ace_control file-browser-diff-char");
  await trigger("unified"); assert.deepEqual(highlights(), ["c", "ript", "c", "ript"]);
  await trigger("split");
  for (const count of [200, 201]) {
    refinement = () => ({ charChanges: Array(count).fill(char), hitTimeout: false });
    await openFile("a.py"); assert.equal(highlights().length, count === 200 ? 4 : 0, "range-count bound applies to the entire chunk");
  }
  refinement = () => ({ charChanges: [char], hitTimeout: true });
  await openFile("a.py"); assert.equal(highlights().length, 0, "timed out refinement leaves line highlighting");
  refinement = () => ({ charChanges: [{ old: new Range(0, 2, 1, 2), new: new Range(0, 2, 1, 2) }], hitTimeout: false });
  overrides.set(`${diffRoute}:a.py`, () => diffResult({ old: "abcd\nefgh", new: "ABCD\nEFGH" }));
  await openFile("a.py"); assert.deepEqual(highlights(), ["c", "d", "C", "D", "ef", "EF"], "multi-line ranges use exclusive columns");
  refinement = () => ({ charChanges: [], hitTimeout: false });
  refineCost = 25;
  setDiffFixture(60, Array.from({ length: 12 }, (_, i) => chunk(i * 5, i * 5 + 1)), 60);
  const refinedBefore = refinements.length;
  await openFile("a.py"); assert.equal(refinements.length - refinedBefore, 8, "refinement has a shared 200 ms budget");
  await trigger("unified"); assert.equal(refinements.length - refinedBefore, 8, "refinement is cached across layouts");
  refineCost = 0; chunkFixture = null;
  for (const language of ["text", "unsupported"]) {
    overrides.set(`${diffRoute}:a.py`, () => diffResult({ language }));
    const tokenCount = tokenizations.length;
    await openFile("a.py"); assert.equal(tokenizations.length, tokenCount);
    assert.equal(diffRows()[0].children[3].textContent, "old");
  }
  tokenizerUnavailable = true;
  overrides.set(`${diffRoute}:a.py`, () => diffResult()); await openFile("a.py");
  assert.equal(diffRows()[0].children[3].textContent, "old", "tokenizer failure falls back to plain text");
  tokenizerUnavailable = false;
  await trigger("split");

  setDiffFixture(); await openFile("a.py");
  assert.deepEqual(gapCounts(), [27, 53, 65]);
  assert.deepEqual(gapOptions(), ["up", "all"]);
  const stableRows = diffRows(), stableGap = gapRows()[1];
  input("diff").scrollTop = 280;
  stableRows[0].getBoundingClientRect = () => ({ top: input("diff").children
    .slice(0, input("diff").children.indexOf(stableRows[0]))
    .reduce((height, row) => height + (row.className === "file-browser-diff-gap" ? 44 : 21), 0) - input("diff").scrollTop });
  expand("up"); assert.deepEqual(gapCounts(), [7, 53, 65]);
  assert.equal(cells()[0][0], "8", "up reveals the bottom twenty lines of the leading run");
  assert.equal(gapRows()[1], stableGap); assert.equal(input("diff").scrollTop, 280);
  assert(stableRows.every((row) => diffRows().includes(row)), "expansion retains every already rendered row");
  assert.deepEqual(gapOptions(), ["all"]); expand("all");
  assert.equal(cells()[0][0], "1");
  assert.deepEqual(gapCounts(), [53, 65]); assert.deepEqual(gapOptions(), ["down", "up", "all"]);
  const stableTop = stableRows[0].getBoundingClientRect().top;
  expand("down"); assert.deepEqual(gapCounts(), [33, 65]);
  const beforeGap = input("diff").children[input("diff").children.indexOf(gapRows()[0]) - 1];
  assert.deepEqual(beforeGap.children.map((cell) => cell.textContent), ["56", "line 55", "54", "line 53"],
    "down reveals the top twenty lines, retaining independent old/new numbers");
  assert.equal(input("diff").scrollTop, 280, "down expansion does not move content above the expander");
  assert.equal(stableRows[0].getBoundingClientRect().top, stableTop);
  assert.equal(document.activeElement.dataset.direction, "down", "native buttons retain keyboard focus after expansion");
  expand("up"); assert.deepEqual(gapCounts(), [13, 65]);
  assert.deepEqual(gapRows()[0].nextSibling.children.map((cell) => cell.textContent), ["70", "line 69", "68", "line 67"]);
  assert.equal(stableRows[0].getBoundingClientRect().top, stableTop);
  assert.deepEqual(gapOptions(1), ["down", "all"]); expand("down", 1);
  assert.deepEqual(gapCounts(), [13, 45]);
  while (gapRows().length) expand("all");
  for (const [column, length] of [[0, 161], [2, 160]]) assert.deepEqual(cells()
    .map((row) => row[column]).filter(Boolean), Array.from({ length }, (_, index) => String(index + 1)),
  "incremental expansion neither loses nor duplicates any row");
  for (const [rows, chunks, expected] of [
    [18, [chunk(5, 6), chunk(6, 7), chunk(12, 13)], [2, 2]],
    [17, [chunk(4, 5), chunk(12, 13)], []],
    [40, [chunk(0, 1), chunk(39, 40)], [32]],
    [20, [chunk(0, 20)], []], [30, [], []],
  ]) {
    setDiffFixture(rows, chunks, rows); await openFile("a.py"); assert.deepEqual(gapCounts(), expected);
  }
  setDiffFixture(50, [chunk(20, 20, 20, 23)], 53); await openFile("a.py");
  assert.deepEqual(gapCounts(), [17, 27], "deletion-only chunk keeps both contexts");
  for (const direction of ["up", "down"]) for (const hidden of [20, 21, 22]) {
    setDiffFixture(hidden + 7, [direction === "up" ? chunk(hidden + 3, hidden + 4) : chunk(3, 4)], hidden + 7);
    await openFile("a.py");
    assert.deepEqual(gapOptions(), hidden <= 20 ? ["all"] : [direction, "all"]);
    expand(hidden <= 20 ? "all" : direction);
    assert.deepEqual(gapCounts(), hidden === 22 ? [2] : [], "fewer than 2 hidden lines reveals the whole remainder");
  }
  setDiffFixture(); await openFile("a.py");
  await trigger("diff-fold"); assert.deepEqual(gapCounts(), []); assert.match(input("diff-fold").textContent, /折叠/);
  assert.equal(diffRows().length, 162);
  await trigger("diff-fold"); assert.deepEqual(gapCounts(), [27, 53, 65]);
  expand("up"); await trigger("unified"); assert.deepEqual(gapCounts(), [27, 53, 65]);
  const diffReads = countReads("diff"), contentReads = countReads("content"), computedBeforeModes = computations.length;
  for (let i = 0; i < 4; i += 1) {
    await trigger("source"); liveViews();
    assert.equal(input("diff").children.length, 0);
    assert.equal(listenerCount(input("diff")), 0, "diff listener leaves with its rows");
    assert.equal(editors.at(-1).options.customScrollbar, undefined);
    assert.equal(editors.at(-1).options.enableMobileMenu, undefined);
    await trigger("difference"); liveViews();
    assert.equal(listenerCount(input("diff")), 1, "one delegated expander listener");
    await trigger("unified"); await trigger("split"); liveViews();
  }
  assert.equal(countReads("diff"), diffReads); assert.equal(countReads("content"), contentReads + 1);
  assert.equal(computations.length, computedBeforeModes, "mode switching retains computed chunks");
  await openFile("readme.md"); assert.equal(listenerCount(input("diff")), 0);
  await openFile("a.py"); input("root").value = "one"; await trigger("root", "change");
  assert.equal(listenerCount(input("diff")), 0); assert.equal(input("diff").children.length, 0);
  await filterChanges(true); await openFile("a.py"); browser.close();
  assert.equal(listenerCount(input("diff")), 0);
  assert.equal(listenerCount(document), originalDocumentListeners);
  assert(observers.every((observer) => observer.targets.size === 0));
  // Late tokenizer completion cannot restore a closed/changed file, mode or root.
  chunkFixture = null;
  overrides.set(`${diffRoute}:a.py`, () => diffResult());
  for (const leave of [() => openFile("readme.md"), () => trigger("source"),
    () => trigger("root", "change"), async () => browser.close()]) {
    await open(); await filterChanges(true);
    modeLoaded = false; const waiting = defer(); let resolveMode;
    modeLoad = (callback) => { resolveMode = callback; waiting.resolve(); };
    const pending = openFile("a.py"); await waiting.promise;
    await leave(); resolveMode({ Mode }); await pending;
    assert.equal(input("diff").hidden, true); assert.equal(input("diff").children.length, 0);
    modeLoad = null;
  }
  modeLoaded = true;
  await open(); await filterChanges(true); await openFile("a.py");
  await trigger("source"); await trigger("copy"); assert.equal(clipboard, files["a.py"].text);
  chunkFixture = null;
  await openFile("gone.py");
  assert.deepEqual(computations.at(-1).fresh, []);
  assert.deepEqual(cells(), [["1", "old", "", ""]], "deleted file has no phantom new-side line");
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
  assert.equal(input("diff").dataset.layout, "unified", "wide preference persists across browser opens");
  storageThrows = true;
  await open(); await openFile("a.py"); await trigger("difference");
  assert.equal(input("diff").dataset.layout, "split", "denied storage defaults to split");
  await trigger("unified"); assert.equal(input("diff").dataset.layout, "unified", "denied writes do not break layout");
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
  overrides.set(`${diffRoute}:a.py`, () => diffResult());
  overrides.set(`${changesRoute}:`, () => result());
  await open(); await filterChanges(true); await openFile("a.py");
  assert.equal(tracks().length, 3, "one persistent indicator per native scrolling container");
  const diffArea = input("diff"), diffTrack = tracks().find((track) => track.dataset.preview === "diff");
  diffArea.clientHeight = 200; diffArea.scrollHeight = 1000; diffArea.scrollTop = 0;
  diffArea.dispatch("scroll");
  assert.equal(diffTrack.hidden, false, "coarse-pointer indicator is attached to the native diff scroll container");
  const diffThumb = diffTrack.children[0];
  diffThumb.dispatch("pointerdown", { pointerId: 10, button: 0, clientY: 100 });
  diffThumb.dispatch("pointermove", { pointerId: 10, clientY: 138.4 });
  assert(Math.abs(diffArea.scrollTop - 200) < 0.001);
  await trigger("source"); assert.equal(diffTrack.hidden, true); assert(!diffThumb.hasPointerCapture(10));
  assert.equal(listenerCount(diffArea), 2, "only reusable indicator listeners survive mode changes");
  await trigger("difference"); assert.equal(listenerCount(diffArea), 3);
  browser.close(); assert.equal(diffTrack.hidden, true); assert.equal(listenerCount(diffArea), 2);
  await window.NetizenAttachmentFiles.open();
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
    assert.equal(tracks().length, 3); assert.deepEqual([markdown, picture].map(listenerCount), previewListenerCounts);
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
  assert.equal(listenerCount(input("diff")), 2, "only the reusable scroll indicator listeners remain");
  assert(editors.every((editor) => editor.destroyed), "created and destroyed editor counts match after close");
  console.log("Shared tree changes filter, view modes, diff layouts, folding, disposal and races passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
