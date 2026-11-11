const media = { matches: false, listeners: [], addEventListener(_, callback) { this.listeners.push(callback); } };
window.matchMedia = () => media;
const until = async (condition) => {
  for (let turn = 0; turn < 200 && !condition(); turn += 1) await Promise.resolve();
  assert(condition(), "bounded UI condition did not settle");
};
const changeWidth = async (compact) => {
  media.matches = compact;
  for (const callback of media.listeners) callback();
  await until(() => diffViews.at(-1)?.model.inline === (compact ? "b" : false));
};
const changesRoute = "/api/v1/project-files/changes";
const diffRoute = "/api/v1/project-files/diff";
const changeRows = () => input("changes-entries").querySelectorAll("button");
const openChange = (path) => changeRows().find((row) => row.dataset.path === path).listeners.get("click")[0]();
const countReads = (operation) => requests.filter((request) => request.route.endsWith(`/${operation}`)).length;
const diffViews = [];
let extensionLoaded = false;
const extension = { createDiffView(model) {
  const a = window.ace.edit(), b = window.ace.edit();
  a.container = document.createElement("div"); b.container = document.createElement("div");
  a.text = model.valueA; b.text = model.valueB;
  const view = { model, editorA: a, editorB: b, resize() {}, onInput() { this.computed = true; },
    destroy() { this.destroyed = true; a.destroy(); b.destroy(); } };
  diffViews.push(view);
  return view;
} };
window.ace.require = (name) => { assert.equal(name, "ace/ext/diff"); return extensionLoaded ? extension : null; };
const result = (patch = {}) => ({ git: "available", head: "head-one", complete: true,
  groupsComplete: { tracked: true, untracked: true, ignored: true }, entries: [
    { path: "a.py", kind: "modified", group: "tracked", directory: false },
    { path: "gone.py", kind: "deleted", group: "tracked", directory: false },
    { path: "empty.txt", kind: "added", group: "tracked", directory: false },
    { path: "readme.md", kind: "untracked", group: "untracked", directory: false },
    { path: "src", kind: "ignored", group: "ignored", directory: true },
    { path: "unknown", kind: "ignored", group: "ignored", directory: false },
  ], ...patch });
const diffResult = (patch = {}) => ({ git: "available", head: "head-one", kind: "modified",
  old: "old\n", new: "new\n", language: "python", ...patch });

(async () => {
  const browser = window.NetizenFileBrowser.create({ container: input("drawer"), api, isExpired: () => expired });
  window.NetizenProjectFiles.init({ browser });
  window.NetizenAttachmentFiles.init({ browser, api, isExpired: () => expired });
  const open = () => window.NetizenProjectFiles.open(project);
  await open();
  assert.equal(input("tabs").hidden, true, "non repositories retain the original file browser");

  const loading = defer();
  overrides.set(`${changesRoute}:`, () => loading.promise);
  await open();
  assert(entry("a.py"), "Git must not delay the directory tree");
  assert.equal(countReads("diff"), 0);
  loading.resolve({ git: "unavailable", message: "Git busy" });
  await until(() => !input("changes-retry").hidden);
  await trigger("changes-tab");
  assert.equal(input("changes-panel").hidden, false);
  assert.equal(input("tree-panel").hidden, true);
  assert.match(input("changes-note").textContent, /Git busy/);

  overrides.set(`${changesRoute}:`, () => result());
  overrides.set(`${diffRoute}:a.py`, () => diffResult());
  overrides.set(`${diffRoute}:gone.py`, () => diffResult({ kind: "deleted", new: "" }));
  await trigger("changes-retry");
  const groups = input("changes-entries").children;
  assert.deepEqual(groups.map((group) => group.open), [true, true, false]);
  assert.deepEqual(groups.map((group) => group.children[0].textContent), ["已跟踪变更 (3)", "未跟踪 (1)", "已忽略 (2)"]);
  assert.match(entry("a.py").getAttribute("aria-label"), /修改/);
  assert.match(entry("empty.txt").getAttribute("aria-label"), /新增/);
  assert.match(entry("readme.md").getAttribute("aria-label"), /未跟踪/);
  assert.match(entry("src").className, /file-browser-ignored/);
  assert.match(entry("unknown").getAttribute("aria-label"), /已忽略/);
  assert.equal(document.querySelectorAll("script").some((script) => script.src?.endsWith("ext-diff.js")), false);
  await trigger("files-tab");
  await openFile("a.py");
  assert.equal(input("editor").hidden, false, "tree entry defaults to complete content");
  assert.equal(input("view-modes").hidden, false);
  assert.equal(countReads("diff"), 0);

  const firstDiff = trigger("diff-mode");
  await until(() => document.querySelectorAll("script").some((script) => script.src?.endsWith("ext-diff.js")));
  const script = document.querySelectorAll("script").find((script) => script.src?.endsWith("ext-diff.js"));
  await openFile("readme.md");
  extensionLoaded = true;
  script.onload();
  await firstDiff;
  assert.equal(input("diff").hidden, true, "late extension loading cannot revive a previous file");
  assert.equal(diffViews.length, 0);
  await openFile("a.py");
  await trigger("diff-mode");
  assert.equal(input("diff").hidden, false);
  assert.equal(input("tools").hidden, true);
  const wide = diffViews.at(-1);
  assert.equal(wide.model.inline, false);
  assert.equal(wide.computed, true);
  for (const editor of [wide.editorA, wide.editorB]) {
    assert.equal(editor.worker, false); assert.equal(editor.readonly, true); assert.equal(editor.mode, "ace/mode/python");
  }
  const reads = requests.length;
  await changeWidth(true);
  assert.equal(wide.destroyed, true);
  assert.equal(diffViews.at(-1).model.valueA, "old\n");
  await changeWidth(false);
  assert.equal(requests.length, reads, "breakpoint re-renders cached texts without requests");
  await trigger("full-mode");
  assert.equal(diffViews.at(-1).destroyed, true);
  assert.equal(input("editor").hidden, false);
  await trigger("copy");
  assert.equal(clipboard, files["a.py"].text);

  await trigger("changes-tab");
  await openChange("gone.py");
  assert.equal(diffViews.at(-1).model.valueB, "");
  assert.match(input("diff-note").textContent, /已删除/);
  await trigger("refresh");
  assert.equal(input("diff").hidden, false, "refresh retains a deleted selection absent from the tree");
  await openChange("empty.txt");
  assert.equal(input("editor").hidden, false);
  assert.equal(input("view-modes").hidden, true);
  await openChange("readme.md");
  await trigger("markdown-toggle");
  assert.equal(input("markdown").hidden, false);
  await openChange("unknown");
  assert.equal(input("editor").hidden, false);

  for (const [patch, note] of [[{ same: true, new: "old\n" }, /与 HEAD 相同/],
    [{ kind: "unchanged", old: null, new: null }, /未报告/],
    [{ reason: "mode_only", message: "mode only", old: null, new: null }, /mode only/],
    [{ head: "head-two" }, /HEAD 已变化/]]) {
    overrides.set(`${diffRoute}:a.py`, () => diffResult(patch));
    await openChange("a.py");
    assert.match(input("diff-note").textContent, note);
    assert.equal(input("diff").hidden, patch.old === null);
  }

  overrides.set("/api/v1/project-files/directory:src", () => ({ path: "src", entries: [
    { path: "src/child", name: "child", kind: "file" }], truncated: false }));
  await changeWidth(true);
  await openChange("src");
  assert.equal(input("tree-panel").hidden, false);
  assert.match(entry("src/child").className, /file-browser-ignored/);
  overrides.set(`${changesRoute}:`, () => result({ complete: false, groupsComplete: { tracked: true, untracked: false, ignored: false } }));
  await trigger("refresh");
  assert.equal(input("incomplete").hidden, false);
  await trigger("back");
  await openFile("image.png");
  assert.equal(input("view-modes").hidden, false, "incomplete candidates permit authoritative per-file lookup");
  await openFile("src");
  overrides.set(`${changesRoute}:src`, () => result({ entries: [] }));
  await trigger("changes-tab");
  await trigger("changes-current");
  assert.equal(requests.at(-1).query.get("path"), "src");
  assert.equal(input("changes-root").disabled, false);
  await trigger("changes-root");
  assert.equal(requests.at(-1).query.get("path"), "");

  const lateDiff = defer();
  overrides.set(`${diffRoute}:a.py`, () => lateDiff.promise);
  const pendingFile = openChange("a.py");
  // The changes list and filesystem are independent sources; return to the
  // root directory before choosing a file through its filesystem row.
  await trigger("back");
  await openFile("readme.md");
  lateDiff.resolve(diffResult({ new: "STALE" }));
  await pendingFile;
  assert.equal(input("diff").hidden, true);
  assert.equal(editors.at(-1).text, files["readme.md"].text);

  const lateChanges = defer(), rootDiff = defer();
  overrides.set(`${changesRoute}:`, () => lateChanges.promise);
  overrides.set(`${diffRoute}:a.py`, () => rootDiff.promise);
  const pendingDiff = openChange("a.py");
  const pendingChanges = trigger("changes-retry");
  input("root").value = "one";
  overrides.set(`${changesRoute}:`, () => ({ git: "not_repository" }));
  await trigger("root", "change");
  lateChanges.resolve(result()); rootDiff.resolve(diffResult());
  await Promise.all([pendingDiff, pendingChanges]);
  assert.equal(input("tabs").hidden, true);
  assert.equal(input("diff").hidden, true);
  assert.equal(input("changes-entries").children.length, 0);

  const beforeAttachment = requests.length;
  await window.NetizenAttachmentFiles.open();
  assert.equal(input("tabs").hidden, true);
  await openFile("a.py");
  assert.equal(input("view-modes").hidden, true);
  assert(requests.slice(beforeAttachment).every((request) => !request.route.endsWith("/changes") && !request.route.endsWith("/diff")));
  browser.close();
  assert.equal(input("diff").children.length, 0);
  assert(diffViews.every((view) => view.destroyed), "every diff view is disposed");
  console.log("Project HEAD changes, limits, modes and races passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
