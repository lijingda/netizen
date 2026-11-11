const media = { matches: false, listeners: [], addEventListener(_, callback) { this.listeners.push(callback); } };
window.matchMedia = () => media;
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
  a.container = document.createElement("div"); b.container = model.inline ? a.container : document.createElement("div");
  a.text = model.valueA; b.text = model.valueB;
  const view = { model, editorA: a, editorB: b, sessionA: a.session, sessionB: b.session,
    resize() { assert(!this.destroyed); }, onInput() { this.computed = true; },
    foldUnchanged() { assert(this.computed, "fold only after computing the diff"); this.folded = true; },
    unfoldUnchanged() { assert(this.computed); this.folded = false; },
    destroy() { assert(!this.destroyed, "destroy each diff exactly once"); this.destroyed = true; a.destroy(); b.destroy(); } };
  diffViews.push(view);
  liveViews();
  return view;
} };
window.ace.require = (name) => { assert.equal(name, "ace/ext/diff"); return extensionLoaded ? extension : null; };
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

(async () => {
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

  await filterChanges(true); await openFile("a.py");
  assert.equal(input("difference").getAttribute("aria-pressed"), "true");
  const wide = diffViews.at(-1);
  assert.equal(wide.model.inline, false); assert.equal(wide.folded, true);
  for (const editor of [wide.editorA, wide.editorB]) {
    assert.equal(editor.worker, false); assert.equal(editor.readonly, true); assert.equal(editor.mode, "ace/mode/python");
  }
  const reads = requests.length;
  await trigger("diff-fold"); assert.equal(wide.folded, false); assert.match(input("diff-fold").textContent, /折叠/);
  await trigger("diff-fold"); assert.equal(wide.folded, true);
  await trigger("unified");
  assert.equal(wide.destroyed, true); assert.equal(diffViews.at(-1).model.inline, "b");
  assert.equal(storedLayout, "unified"); assert.equal(diffViews.at(-1).folded, true);
  await trigger("split"); await changeWidth(true);
  await until(() => diffViews.at(-1).model.inline === "b");
  assert.equal(input("diff-layout").hidden, true);
  await changeWidth(false); await until(() => diffViews.at(-1).model.inline === false);
  assert.equal(input("diff-layout").hidden, false);
  assert.equal(requests.length, reads, "layouts and breakpoints render cached texts");
  const diffReads = countReads("diff"), contentReads = countReads("content");
  for (let i = 0; i < 4; i += 1) {
    await trigger("source"); liveViews();
    await trigger("difference"); liveViews();
    await trigger("unified"); await trigger("split"); liveViews();
  }
  assert.equal(countReads("diff"), diffReads, "diff fetched once for the open file");
  assert.equal(countReads("content"), contentReads + 1, "source fetched once for the open file");
  await trigger("source"); await trigger("copy"); assert.equal(clipboard, files["a.py"].text);
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
  assert.equal(input("diff").children.length, 0);
  assert(diffViews.every((view) => view.destroyed), "created and destroyed diff counts match after close");
  assert(editors.every((editor) => editor.destroyed), "created and destroyed editor counts match after close");
  console.log("Shared tree changes filter, view modes, diff layouts, folding, disposal and races passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
