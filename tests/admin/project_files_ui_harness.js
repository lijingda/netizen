const input = (id) => document.querySelector(`#file-browser-${id}`);
const trigger = async (id, kind = "click") => {
  const element = input(id);
  return element.listeners.get(kind)[0]({ target: element, currentTarget: element, preventDefault() {} });
};
Element.prototype.remove = function () {
  this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
};
Element.prototype.select = function () { document.selected = this.value; };
const makeElement = document.createElement;
document.createDocumentFragment = () => new Element("fragment");
document.createElement = (tag) => {
  const element = makeElement(tag);
  element.style.setProperty = (name, value) => { element.style[name] = value; };
  return element;
};
const originalMatches = Element.prototype.matches;
Element.prototype.matches = function (selector) {
  if (selector === "button[data-path]") return this.tagName === "button" && this.dataset.path !== undefined;
  return originalMatches.call(this, selector);
};
Element.prototype.closest = function (selector) {
  for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;
  return null;
};
const originalFocus = Element.prototype.focus;
Element.prototype.focus = function () { originalFocus.call(this); this.dispatch("focus"); };
const requests = [];
const editors = [];
let expired = false;
let clipboard = null;
let fallbackCopies = 0;
let overrides = new Map();
const navigator = { clipboard: { writeText: async (text) => { clipboard = text; } } };
document.execCommand = (command) => { assert.equal(command, "copy"); fallbackCopies += 1; clipboard = document.selected; return true; };
window.ace = {
  config: { set() {} },
  edit: () => {
    const editor = {
      session: { setUseWorker(value) { editor.worker = value; },
        setMode(value) { assert.equal(editor.worker, false); editor.mode = value; },
        setUseWrapMode(value) { editor.wrap = value; } },
      commands: { addCommand(command) { editor.findCommand = command; } },
      setReadOnly(value) { editor.readonly = value; }, setOptions(value) { editor.options = value; },
      setValue(value) { editor.text = value; }, destroy() { editor.destroyed = true; }, resize() {},
      find(value, options) { editor.search = [value, options]; return editor.text.includes(value); },
      gotoLine(line) { editor.line = line; }, focus() { editor.focused = true; },
    };
    editors.push(editor);
    return editor;
  },
};
const project = { alias: "demo", cwd: "/project" };
const defer = () => { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const files = {
  "readme.md": { kind: "text", text: "# Hello\nplain text", language: "markdown", markdownHtml: "<h1>Hello</h1>" },
  "a.py": { kind: "text", text: "print('hello')", language: "python" },
  "empty.txt": { kind: "text", text: "", language: "text" },
  "image.png": { kind: "image", imageUrl: "/api/v1/project-files/image?version=exact" },
  "unknown": { kind: "text", text: "custom text", language: "unshipped" },
};
async function api(url, options = {}) {
  const [route, search] = url.split("?");
  const query = new URLSearchParams(search);
  const path = query.get("path") || "";
  requests.push({ route, query, options });
  const override = overrides.get(`${route}:${path}`);
  if (override) return override(query);
  if (route === "/api/v1/worktrees") {
    assert(Number(query.get("pageSize")) >= 1 && Number(query.get("pageSize")) <= 50,
      "worktree pagination must respect the HTTP endpoint limit");
    return { items: [{ id: query.has("cursor") ? "two" : "one", state: "ready", creationState: "ready", available: false, cwd: "/worktree" },
      { id: "missing", state: "missing", available: false, cwd: "/gone" }], nextCursor: query.has("cursor") ? null : "next" };
  }
  if (route.endsWith("/root")) return { projectAlias: "demo", rootId: query.get("worktree") || "project", cwd: "/project", identity: query.get("worktree") ? "worktree-identity" : "project-identity" };
  if (route.endsWith("/changes")) return { git: "not_repository" };
  if (route.endsWith("/directory")) return { path, entries: path ? [] : [
    { name: "src", path: "src", kind: "directory" },
    ...Object.keys(files).map((name) => ({ name, path: name, kind: "file", size: 100 })),
    { name: "shortcut", path: "shortcut", kind: "symlink" },
  ], truncated: false };
  if (route.endsWith("/content")) return { path, size: 100, modifiedAt: "2026-11-02", version: "one", ...files[path] };
  throw new Error(`Unexpected ${url}`);
}
const entry = (path) => input("entries").children.find((row) => row.dataset.path === path);
const openFile = (path) => entry(path).listeners.get("click")[0]();
// SHIPPED_PROJECT_FILES
(async () => {
  const controller = window.NetizenProjectFiles;
  controller.init({ browser: window.NetizenFileBrowser.create({ container: input("drawer"), api, isExpired: () => expired }) });
  assert.equal(requests.length, 0, "the hidden browser does no background I/O");
  await controller.open(project);
  assert.equal(input("drawer").open, true);
  assert.equal(input("root").children.length, 3);
  assert.equal(input("root").children[1].disabled, false, "disabled Projects retain their ready worktree browsing");
  assert.equal(input("root").children[2].disabled, true);
  assert.equal(entry("shortcut").disabled, true);
  assert.equal(requests.find((r) => r.route.endsWith("/directory")).query.get("identity"), "project-identity");
  assert.equal(requests.filter((r) => r.route.endsWith("/worktrees")).length, 1, "worktrees are paged on demand");
  await trigger("more-roots");
  assert.equal(input("more-roots").hidden, true);
  assert.equal(input("root").children.length, 4, "overlapping pages do not duplicate root choices");

  await openFile("a.py");
  const editor = editors.at(-1);
  assert.equal(editor.readonly, true);
  assert.equal(editor.mode, "ace/mode/python");
  assert.equal(editor.text, "print('hello')");
  assert.equal(input("tools").hidden, false);
  input("search").value = "hello";
  await trigger("search-form", "submit");
  assert.equal(editor.search[0], "hello");
  assert.equal(editor.search[1].regExp, false);
  input("line").value = "3";
  await trigger("line-form", "submit");
  assert.equal(editor.line, 3);
  await trigger("wrap");
  assert.equal(editor.wrap, false);
  await trigger("copy");
  assert.equal(clipboard, "print('hello')");
  navigator.clipboard.writeText = async () => { throw new Error("HTTP permission"); };
  await trigger("copy");
  assert.equal(fallbackCopies, 1);
  assert.equal(clipboard, "print('hello')");
  assert.equal(input("drawer").querySelector("textarea"), null, "copy buffer is discarded immediately");

  await openFile("readme.md");
  assert.equal(editor.text, "");
  assert.equal(editor.destroyed, true);
  assert.equal(input("markdown").innerHTML, "<h1>Hello</h1>");
  assert.equal(input("markdown").hidden, true);
  await trigger("preview");
  assert.equal(input("markdown").hidden, false);
  assert.equal(input("editor").hidden, true);
  assert.equal(input("search-form").hidden, true);
  await trigger("source");
  assert.equal(input("editor").hidden, false);
  assert.equal(input("search-form").hidden, false);
  const localLink = document.createElement("a");
  localLink.dataset.fileBrowserPath = "a.py";
  localLink.closest = () => localLink;
  input("markdown").append(localLink);
  let prevented = false;
  await input("markdown").listeners.get("click")[0]({ target: localLink, preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(editors.at(-1).text, "print('hello')", "local Markdown links stay inside the selected workspace");
  async function markdownLink(path, directoryHint = false) {
    const anchor = document.createElement("a");
    anchor.dataset.fileBrowserPath = path;
    if (directoryHint) anchor.dataset.fileBrowserDirectory = "true";
    anchor.closest = () => anchor;
    input("markdown").append(anchor);
    return input("markdown").listeners.get("click")[0]({ target: anchor, preventDefault() {} });
  }
  await openFile("readme.md");
  let contentCount = requests.filter((request) => request.route.endsWith("/content")).length;
  await markdownLink("src");
  assert.equal(requests.at(-1).query.get("path"), "src", "a directory link without a trailing slash follows its listing kind");
  assert.equal(requests.filter((request) => request.route.endsWith("/content")).length, contentCount,
    "directory links never request file content");
  await markdownLink("", true);
  assert(entry("src") && entry("readme.md"), "a parent link keeps the root and its siblings visible");
  assert(entry("readme.md"));
  await openFile("readme.md");
  contentCount = requests.filter((request) => request.route.endsWith("/content")).length;
  const beforeHint = requests.length;
  await markdownLink("src", true);
  assert.equal(requests.length, beforeHint, "an already loaded directory reopens without another request");
  assert.equal(entry("src").getAttribute("aria-expanded"), "true");
  assert.equal(requests.filter((request) => request.route.endsWith("/content")).length, contentCount);
  await markdownLink("", true);
  await openFile("empty.txt");
  assert.match(input("note").textContent, /为空/);
  await openFile("unknown");
  assert.equal(editors.at(-1).mode, "ace/mode/text");
  await openFile("image.png");
  assert.equal(input("image-wrap").hidden, false);
  assert.equal(input("image").src, "/api/v1/project-files/image?version=exact");
  assert.equal(input("tools").hidden, true);
  await input("image").onerror();
  assert.match(input("note").textContent, /图片未能加载/);

  overrides.set("/api/v1/project-files/content:a.py", () => { throw new Error("文件超过预览大小限制"); });
  await openFile("a.py");
  assert.match(input("note").textContent, /超过预览/);
  assert.equal(input("editor").hidden, true);
  assert.equal(input("image-wrap").hidden, true);
  overrides.clear();
  for (const code of ["file_changed", "read_timeout"]) {
    let attempts = 0;
    overrides.set("/api/v1/project-files/content:a.py", () => {
      if (++attempts === 1) throw Object.assign(new Error("文件暂时无法读取，请刷新后重试。"), { code });
      return { path: "a.py", kind: "text", text: "recovered content", language: "python" };
    });
    await openFile("a.py");
    assert.equal(input("editor").hidden, true);
    await trigger("refresh");
    assert.equal(attempts, 2, "refresh retries the selected file even when its first content request failed");
    assert.equal(editors.at(-1).text, "recovered content");
    assert.equal(input("filename").textContent, "a.py");
    overrides.clear();
  }
  const retainedEditor = editors.at(-1);
  await trigger("collapse");
  await openFile("src");
  assert.match(input("entries").textContent, /空目录/);
  assert(entry("readme.md"), "expanding a folder retains its siblings");
  assert.equal(editors.at(-1), retainedEditor, "folder navigation does not replace the preview");
  await input("breadcrumbs").querySelector("button").listeners.get("click")[0]();

  const previousFile = defer();
  overrides.set("/api/v1/project-files/content:a.py", () => previousFile.promise);
  const previousRead = openFile("a.py");
  await openFile("readme.md");
  previousFile.resolve({ path: "a.py", kind: "text", text: "STALE", language: "python" });
  await previousRead;
  assert.equal(editors.at(-1).text, files["readme.md"].text, "a slower file cannot overwrite the latest selection");
  overrides.clear();

  await trigger("collapse");
  const slowFile = defer();
  overrides.set("/api/v1/project-files/content:a.py", () => slowFile.promise);
  const expandingDuringRead = openFile("a.py");
  await openFile("src");
  assert.equal(input("breadcrumbs").textContent, "根目录", "expanding a folder cannot change the loading file's breadcrumb");
  assert.equal(entry("a.py").getAttribute("aria-selected"), "true");
  slowFile.resolve({ path: "a.py", kind: "text", text: "slow content", language: "python" });
  await expandingDuringRead;
  assert.equal(input("filename").textContent, "a.py");
  assert.equal(editors.at(-1).text, "slow content");
  assert.equal(entry("a.py").getAttribute("aria-current"), "true");
  assert.equal(input("breadcrumbs").textContent, "根目录");
  overrides.clear();

  const late = defer();
  overrides.set("/api/v1/project-files/content:a.py", () => late.promise);
  const pending = openFile("a.py");
  const pendingRequest = requests.at(-1);
  input("root").value = "one";
  await trigger("root", "change");
  assert.equal(pendingRequest.options.signal.aborted, true);
  assert.equal(input("editor").hidden, true);
  assert.equal(requests.filter((r) => r.route.endsWith("/directory")).at(-1).query.get("identity"), "worktree-identity");
  late.resolve({ path: "a.py", kind: "text", text: "STALE", language: "python" });
  await pending;
  assert.equal(input("editor").hidden, true, "previous root's response cannot restore content");
  overrides.clear();

  const delayed = defer();
  overrides.set("/api/v1/project-files/content:a.py", () => delayed.promise);
  const closing = openFile("a.py");
  controller.close();
  assert.equal(input("drawer").open, false);
  overrides.clear();
  await controller.open(project);
  await trigger("drawer", "close");
  assert.equal(input("drawer").open, true, "a queued close event from the previous opening cannot close the new dialog");
  await openFile("empty.txt");
  delayed.resolve({ path: "a.py", kind: "text", text: "STALE", language: "python" });
  await closing;
  assert.equal(editors.at(-1).text, "", "reopening does not accept the old drawer's response");

  // Tree browsing retains siblings, previews and independently loaded branches.
  controller.close();
  const directoryRoute = "/api/v1/project-files/directory";
  const rootEntries = [
    { name: "src", path: "src", kind: "directory" },
    { name: "docs", path: "docs", kind: "directory" },
    { name: "a.py", path: "a.py", kind: "file" },
    { name: "readme.md", path: "readme.md", kind: "file" },
  ];
  const srcEntries = [
    { name: "lib", path: "src/lib", kind: "directory" },
    { name: "main.py", path: "src/main.py", kind: "file" },
  ];
  const listing = (path, entries) => ({ path, entries, truncated: false });
  files["src/lib/deep.py"] = { kind: "text", text: "deep file", language: "python" };
  overrides.set(`${directoryRoute}:`, () => listing("", rootEntries));
  const slowDirectory = defer();
  overrides.set(`${directoryRoute}:src`, () => slowDirectory.promise);
  overrides.set(`${directoryRoute}:docs`, () => listing("docs", [{ name: "guide.md", path: "docs/guide.md", kind: "file" }]));
  overrides.set(`${directoryRoute}:src/lib`, () => listing("src/lib", [{ name: "deep.py", path: "src/lib/deep.py", kind: "file" }]));
  const treeStart = requests.length;
  await controller.open(project);
  assert.deepEqual(requests.slice(treeStart).filter((r) => r.route === directoryRoute).map((r) => r.query.get("path")), [""],
    "opening a tree reads only the root directory");
  await openFile("a.py");
  const treeEditor = editors.at(-1);
  const loadingSource = openFile("src");
  const sourceRequest = requests.at(-1);
  await openFile("docs");
  assert.equal(sourceRequest.options.signal.aborted, false, "sibling expansion never cancels another branch");
  assert(entry("src") && entry("docs/guide.md") && entry("readme.md"));
  assert.equal(editors.at(-1), treeEditor, "expanding folders retains the current file preview");
  await openFile("src");
  assert.equal(entry("src").getAttribute("aria-expanded"), "false");
  slowDirectory.resolve(listing("src", srcEntries));
  await loadingSource;
  assert.equal(entry("src/main.py"), undefined, "late directory data cannot reopen a collapsed folder");
  const beforeReopen = requests.length;
  await openFile("src");
  assert(entry("src/main.py") && entry("docs/guide.md"));
  assert.equal(requests.length, beforeReopen, "reopening a loaded folder does not refetch it");
  await openFile("src/lib");
  assert.equal(entry("src/lib").getAttribute("aria-level"), "2");
  assert.equal(entry("src/lib/deep.py").getAttribute("aria-level"), "3");
  await openFile("src");
  assert.equal(entry("src/lib/deep.py"), undefined);
  await openFile("src");
  assert(entry("src/lib/deep.py"), "child expansion survives collapsing its ancestor");
  assert.equal(entry("a.py").getAttribute("aria-selected"), "true");

  const beforeFilter = requests.length;
  input("filter").value = "deep.py";
  await trigger("filter", "input");
  assert(entry("src") && entry("src/lib") && entry("src/lib/deep.py"));
  assert.equal(entry("docs"), undefined);
  assert.equal(requests.length, beforeFilter, "filtering loaded file names never scans the server");
  input("filter").value = "nothing-matches";
  await trigger("filter", "input");
  assert.match(input("directory-note").textContent, /没有匹配/);
  input("filter").value = "";
  await trigger("filter", "input");
  assert(entry("docs/guide.md") && entry("src/lib/deep.py"));

  async function treeKey(path, key) {
    const row = entry(path);
    row.focus();
    let prevented = false;
    await input("entries").listeners.get("keydown")[0]({ target: row, key, preventDefault() { prevented = true; } });
    assert(prevented);
  }
  await treeKey("src", "ArrowRight");
  assert.equal(document.activeElement.dataset.path, "src/lib");
  await treeKey("src/lib/deep.py", "ArrowLeft");
  assert.equal(document.activeElement.dataset.path, "src/lib");
  await treeKey("src/lib", "ArrowLeft");
  assert.equal(entry("src/lib").getAttribute("aria-expanded"), "false");
  await treeKey("src", "End");
  assert.equal(document.activeElement.dataset.path, "readme.md");
  assert.equal(input("entries").querySelectorAll("button").filter((row) => row.tabIndex === 0).length, 1);
  assert.equal(editors.at(-1), treeEditor);

  await openFile("readme.md");
  await markdownLink("src/lib/deep.py");
  assert.equal(editors.at(-1).text, "deep file");
  assert(entry("docs/guide.md"), "Markdown navigation preserves unrelated expanded directories");
  assert.equal(entry("src/lib/deep.py").getAttribute("aria-selected"), "true");
  await trigger("collapse");
  assert.equal(entry("src/lib"), undefined);
  assert.equal(entry("docs/guide.md"), undefined);
  assert.equal(editors.at(-1).text, "deep file", "collapse-all retains the open file");

  // A late Markdown path traversal must not override a later explicit file click.
  const slowJump = defer();
  overrides.set(`${directoryRoute}:src`, () => slowJump.promise);
  await openFile("readme.md");
  const jump = markdownLink("src/lib/deep.py");
  await Promise.resolve();
  await Promise.resolve();
  await openFile("a.py");
  slowJump.resolve(listing("src", srcEntries));
  await jump;
  assert.equal(editors.at(-1).text, files["a.py"].text);
  assert.equal(entry("src/lib/deep.py"), undefined, "cancelled navigation cannot continue reading descendants");

  // Switching roots rejects an in-flight directory result even if it ignores abort.
  await trigger("collapse");
  const oldBranch = defer();
  overrides.set(`${directoryRoute}:src`, () => oldBranch.promise);
  const oldBranchRead = openFile("src");
  const oldBranchRequest = requests.at(-1);
  input("root").value = "one";
  await trigger("root", "change");
  assert.equal(oldBranchRequest.options.signal.aborted, true);
  oldBranch.resolve(listing("src", srcEntries));
  await oldBranchRead;
  assert.equal(entry("src/main.py"), undefined);
  assert.equal(input("editor").hidden, true);

  // An identity failure invalidates the entire tree and cancels other pending reads.
  const invalidatedFile = defer();
  overrides.set("/api/v1/project-files/content:a.py", () => invalidatedFile.promise);
  const invalidatedRead = openFile("a.py");
  const invalidatedFileRequest = requests.at(-1);
  const invalidatedDirectory = defer();
  overrides.set(`${directoryRoute}:docs`, () => invalidatedDirectory.promise);
  const invalidatedDirectoryRead = openFile("docs");
  const invalidatedDirectoryRequest = requests.at(-1);
  overrides.set(`${directoryRoute}:src`, () => { throw Object.assign(new Error("根目录身份已改变"), { code: "root_changed" }); });
  await openFile("src");
  assert.equal(input("entries").children.length, 0);
  assert.equal(input("editor").hidden, true);
  assert.equal(input("cwd").textContent, "");
  assert.equal(input("breadcrumbs").textContent, "");
  assert.match(input("directory-note").textContent, /身份已改变/);
  assert.equal(invalidatedFileRequest.options.signal.aborted, true);
  assert.equal(invalidatedDirectoryRequest.options.signal.aborted, true);
  invalidatedFile.resolve({ path: "a.py", kind: "text", text: "STALE", language: "python" });
  invalidatedDirectory.resolve(listing("docs", [{ name: "stale.md", path: "docs/stale.md", kind: "file" }]));
  await Promise.all([invalidatedRead, invalidatedDirectoryRead]);
  assert.equal(input("entries").children.length, 0);
  assert.equal(input("editor").hidden, true);
  const rootRoute = "/api/v1/project-files/root";
  overrides.set(`${rootRoute}:`, () => ({ cwd: "/fresh-root", identity: "fresh-identity" }));
  const beforeRootRefresh = requests.filter((request) => request.route === rootRoute).length;
  await trigger("refresh");
  assert.equal(requests.filter((request) => request.route === rootRoute).length, beforeRootRefresh + 1,
    "refresh explicitly reacquires root identity after invalidation");
  assert.equal(requests.at(-1).query.get("identity"), "fresh-identity");
  assert(entry("src") && entry("a.py"));
  assert.equal(input("cwd").textContent, "/fresh-root");

  overrides.set("/api/v1/project-files/content:a.py", () => { throw Object.assign(new Error("内容读取时根目录身份已改变"), { code: "root_changed" }); });
  await openFile("a.py");
  assert.equal(input("entries").children.length, 0, "content identity failures clear cached directory entries too");
  assert.equal(input("breadcrumbs").textContent, "");
  assert.match(input("directory-note").textContent, /身份已改变/);
  await trigger("collapse");
  assert.match(input("directory-note").textContent, /身份已改变/, "collapse cannot dismiss root invalidation");
  await trigger("refresh");
  assert(entry("a.py"));
  assert.equal(input("filename").textContent, "选择文件查看", "a new identity never reopens the old root's file automatically");

  controller.close();
  overrides.clear();
  await controller.open(project);
  await openFile("image.png");
  overrides.set("/api/v1/project-files/content:image.png", () => { throw Object.assign(new Error("图片读取时根目录身份已改变"), { code: "root_changed" }); });
  await input("image").onerror();
  assert.equal(input("entries").children.length, 0, "image error checks share root invalidation with directory and content reads");
  assert.equal(input("image-wrap").hidden, true);
  assert.equal(input("cwd").textContent, "");
  assert.match(input("directory-note").textContent, /身份已改变/);
  // The aggregate budget spans multiple otherwise valid 2,000-entry directories.
  controller.close();
  overrides.clear();
  const largeRoots = Array.from({ length: 5 }, (_, index) => ({ name: `large${index}`, path: `large${index}`, kind: "directory" }));
  overrides.set(`${directoryRoute}:`, () => listing("", largeRoots));
  for (const root of largeRoots) overrides.set(`${directoryRoute}:${root.path}`, () => listing(root.path,
    Array.from({ length: 2000 }, (_, index) => ({ name: `file${index}.txt`, path: `${root.path}/file${index}.txt`, kind: "file" }))));
  await controller.open(project);
  async function loadCollapsed(path) {
    const reading = openFile(path);
    await openFile(path);
    await reading;
  }
  for (const root of largeRoots) await loadCollapsed(root.path);
  const beforeCachedRead = requests.length;
  await loadCollapsed("large3");
  assert.equal(requests.length, beforeCachedRead, "four valid large directories fit in the tree metadata cache");
  await openFile("large4");
  assert.equal(entry("large4/file0.txt"), undefined);
  assert.match(input("entries").textContent, /目录树加载上限/);
  await trigger("collapse");
  await loadCollapsed("large4");
  const beforeReleasedRead = requests.length;
  await loadCollapsed("large4");
  assert.equal(requests.length, beforeReleasedRead, "collapse-all releases the aggregate budget for another directory");
  controller.close();
  overrides.clear();
  delete files["src/lib/deep.py"];
  await controller.open(project);
  await openFile("empty.txt");

  overrides.set("/api/v1/project-files/directory:", () => { throw new Error("目录身份已改变"); });
  await trigger("refresh");
  assert.match(input("directory-note").textContent, /身份已改变/);
  assert.equal(input("entries").children.length, 0);
  assert.equal(input("editor").hidden, true);
  assert.equal(requests.at(-1).query.get("identity"), "project-identity", "refresh retains exact root identity");
  overrides.clear();
  await trigger("refresh");
  await openFile("a.py");
  const finalEditor = editors.at(-1);
  overrides.set("/api/v1/project-files/content:image.png", () => {
    expired = true;
    controller.expire();
    throw Object.assign(new Error("登录已过期"), { status: 401 });
  });
  await openFile("image.png");
  assert.equal(input("drawer").open, false);
  assert.equal(finalEditor.text, "");
  assert.equal(finalEditor.destroyed, true);
  assert.equal(input("entries").children.length, 0);
  assert.equal(input("root").children.length, 0);
  assert.equal(input("markdown").textContent, "");
  const count = requests.length;
  await controller.open(project);
  assert.equal(requests.length, count);
  console.log("project file navigation and preview behavior passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
