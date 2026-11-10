// Both layouts use the shipped browser, provider adapters, and static DOM.
const media = { matches: true, listeners: [], addEventListener(kind, callback) {
  assert.equal(kind, "change"); this.listeners.push(callback);
} };
window.matchMedia = (query) => { assert.equal(query, "(max-width: 720px)"); return media; };
const changeWidth = (compact) => {
  media.matches = compact;
  for (const listener of media.listeners) listener({ matches: compact });
};
const until = async (condition, message) => {
  for (let turn = 0; turn < 100 && !condition(); turn += 1) await Promise.resolve();
  assert(condition(), message);
};
const listing = (path, entries) => ({ path, entries, truncated: false });
const directoryEntry = (path) => ({ path, name: path.split("/").at(-1), kind: "directory" });
const fileEntry = (path, extra = {}) => ({ path, name: path.split("/").at(-1), kind: "file", size: 100, ...extra });
const visiblePaths = () => input("entries").children.filter((row) => row.dataset.path !== undefined).map((row) => row.dataset.path);
const screen = () => input("drawer").dataset.screen;
const scrollPane = () => input("entries").parentElement;
const filter = async (value) => { input("filter").value = value; await trigger("filter", "input"); };
const readRequests = (route) => requests.filter((request) => request.route.endsWith(`/${route}`));
const latestRead = (route) => readRequests(route).at(-1);
const rootEntries = [directoryEntry("docs"), directoryEntry("assets"), fileEntry("readme.md"), fileEntry("a.py"), fileEntry("unsupported.bin")];
const docEntries = [directoryEntry("docs/sub"), fileEntry("docs/guide.md"), fileEntry("docs/needle.md")];
const subEntries = [fileEntry("docs/sub/deep.md")];
const projectDirectory = "/api/v1/project-files/directory";
const projectContent = "/api/v1/project-files/content";
const ace = window.ace;
const markdownLink = async (path, directoryHint = false) => {
  const anchor = document.createElement("a");
  anchor.dataset.fileBrowserPath = path;
  if (directoryHint) anchor.dataset.fileBrowserDirectory = "true";
  anchor.closest = () => anchor;
  input("markdown").append(anchor);
  let prevented = false;
  await input("markdown").listeners.get("click")[0]({ target: anchor, preventDefault() { prevented = true; } });
  assert(prevented, "local links stay inside the shared browser");
};

(async () => {
  const browser = window.NetizenFileBrowser.create({ container: input("drawer"), api, isExpired: () => expired });
  const projects = window.NetizenProjectFiles;
  const attachments = window.NetizenAttachmentFiles;
  projects.init({ browser });
  attachments.init({ browser, api, isExpired: () => expired });
  const resetProject = async () => {
    overrides.clear();
    window.ace = ace;
    expired = false;
    changeWidth(true);
    overrides.set(`${projectDirectory}:`, () => listing("", rootEntries));
    overrides.set(`${projectDirectory}:docs`, () => listing("docs", docEntries));
    overrides.set(`${projectDirectory}:docs/sub`, () => listing("docs/sub", subEntries));
    for (const path of ["docs/guide.md", "docs/needle.md", "docs/sub/deep.md"]) {
      overrides.set(`${projectContent}:${path}`, () => ({ path, kind: "text", language: "markdown", text: path, markdownHtml: `<p>${path}</p>` }));
    }
    overrides.set(`${projectContent}:unsupported.bin`, () => ({ path: "unsupported.bin", kind: "unsupported", previewMessage: "此文件不支持预览" }));
    await projects.open(project);
  };

  await resetProject();
  assert.equal(screen(), "directory");
  assert.equal(input("back").hidden, true);
  assert.equal(input("collapse").hidden, true);
  assert.deepEqual(visiblePaths(), rootEntries.map((item) => item.path));
  assert.equal(entry("docs").getAttribute("aria-expanded"), null, "mobile folders are navigation links, not an expanded tree");
  scrollPane().scrollTop = 231;
  entry("docs").focus();
  await openFile("docs");
  assert.deepEqual(visiblePaths(), docEntries.map((item) => item.path));
  assert.equal(input("back").hidden, false);
  assert.equal(scrollPane().scrollTop, 0, "a newly entered directory starts at the top");
  scrollPane().scrollTop = 87;
  entry("docs/sub").focus();
  await openFile("docs/sub");
  assert.deepEqual(visiblePaths(), ["docs/sub/deep.md"]);
  const cachedReads = requests.length;
  await trigger("back");
  assert.equal(scrollPane().scrollTop, 87);
  assert.equal(document.activeElement.dataset.path, "docs/sub");
  await trigger("back");
  assert.equal(scrollPane().scrollTop, 231);
  assert.equal(document.activeElement.dataset.path, "docs");
  assert.equal(requests.length, cachedReads, "back navigation reuses cached directory layers");
  await filter("needle");
  assert.deepEqual(visiblePaths(), [], "mobile filtering does not include descendants from the shared cache");
  assert.equal(requests.length, cachedReads, "filtering never requests additional directories");
  await filter("docs");
  await openFile("docs");
  assert.equal(input("filter").value, "", "each directory has its own filter");
  await filter("guide");
  scrollPane().scrollTop = 54;
  await openFile("docs/guide.md");
  assert.equal(screen(), "file");
  assert.equal(input("title").textContent, "guide.md");
  // Compare identities as booleans: failure must not format the cyclic DOM graph.
  assert(document.activeElement === input("back"), "opening a file focuses Back");
  assert.equal(editors.at(-1).text, "docs/guide.md");
  const beforeReturn = requests.length;
  await trigger("back");
  assert.equal(screen(), "directory");
  assert.equal(input("filter").value, "guide");
  assert.deepEqual(visiblePaths(), ["docs/guide.md"]);
  assert.equal(scrollPane().scrollTop, 54);
  assert.equal(document.activeElement.dataset.path, "docs/guide.md");
  assert.equal(requests.length, beforeReturn);
  await trigger("back");
  assert.equal(input("filter").value, "docs", "returning to a parent restores that directory's filter");

  // A directory response may populate the cache after Back, never navigate.
  await resetProject();
  const slowDirectory = defer();
  overrides.set(`${projectDirectory}:docs`, () => slowDirectory.promise);
  const navigating = openFile("docs");
  await until(() => latestRead("directory").query.get("path") === "docs", "directory request started");
  assert.equal(input("entries").getAttribute("aria-busy"), "true");
  await trigger("back");
  slowDirectory.resolve(listing("docs", docEntries));
  await navigating;
  assert.equal(screen(), "directory");
  assert.deepEqual(visiblePaths(), rootEntries.map((item) => item.path));
  assert.equal(input("breadcrumbs").textContent, "根目录");
  assert.equal(input("back").hidden, true);

  // A delayed directory listing must not take focus back after the user starts
  // filtering or moves to another control while it loads.
  for (const control of ["filter", "close"]) {
    await resetProject();
    const delayedDirectory = defer();
    overrides.set(`${projectDirectory}:docs`, () => delayedDirectory.promise);
    const entering = openFile("docs");
    await until(() => latestRead("directory").query.get("path") === "docs", "directory request started");
    input(control).focus();
    if (control === "filter") await filter("guide");
    delayedDirectory.resolve(listing("docs", docEntries));
    await entering;
    assert(document.activeElement === input(control), "directory completion preserves the user's newer focus");
    if (control === "filter") {
      assert.equal(input("filter").value, "guide");
      assert.deepEqual(visiblePaths(), ["docs/guide.md"]);
    }
  }

  // Directory failures, empty results and bounded listings stay on the visible
  // directory screen, with refresh available to recover the same location.
  await resetProject();
  overrides.set(`${projectDirectory}:docs`, () => { throw new Error("目录读取失败"); });
  await openFile("docs");
  assert.equal(screen(), "directory");
  assert.deepEqual(visiblePaths(), []);
  assert.match(input("directory-note").textContent, /目录读取失败/);
  assert.equal(input("entries").getAttribute("aria-busy"), "false");
  assert.equal(input("back").hidden, false);
  overrides.set(`${projectDirectory}:docs`, () => listing("docs", []));
  await trigger("refresh");
  assert.equal(screen(), "directory");
  assert.deepEqual(visiblePaths(), []);
  assert.match(input("directory-note").textContent, /为空/);
  overrides.set(`${projectDirectory}:docs`, () => ({ ...listing("docs", [docEntries[1]]), truncated: true }));
  await trigger("refresh");
  assert.deepEqual(visiblePaths(), ["docs/guide.md"]);
  assert.match(input("directory-note").textContent, /仅显示部分/);
  await openFile("docs/guide.md");
  assert.equal(screen(), "file", "entries from a truncated directory remain readable");

  overrides.set(`${projectDirectory}:docs`, () => { throw new Error("父目录暂时不可读"); });
  const beforeFailedRefresh = readRequests("content").length;
  await trigger("refresh");
  assert.equal(screen(), "file", "refresh errors stay on the screen whose refresh was requested");
  assert.match(input("note").textContent, /父目录暂时不可读/);
  assert.equal(input("back").hidden, false);
  assert.equal(input("editor").hidden, true);
  assert.equal(readRequests("content").length, beforeFailedRefresh, "a failed directory refresh cannot reread stale file metadata");
  overrides.set(`${projectDirectory}:docs`, () => listing("docs", docEntries));
  await trigger("refresh");
  assert.equal(screen(), "file");
  assert.equal(editors.at(-1).text, "docs/guide.md", "refresh retries the file after its parent directory recovers");
  assert.equal(input("note").textContent, "");

  // Returning while content is reading cancels its read and invalidates its result.
  await resetProject();
  const slowContent = defer();
  overrides.set(`${projectContent}:a.py`, () => slowContent.promise);
  const reading = openFile("a.py");
  assert.equal(screen(), "file", "loading has the same content screen as a successful preview");
  assert.match(input("note").textContent, /正在读取/);
  const contentRequest = latestRead("content");
  await trigger("back");
  assert.equal(contentRequest.options.signal.aborted, true);
  const editorCount = editors.length;
  slowContent.resolve({ path: "a.py", kind: "text", text: "late content", language: "python" });
  await reading;
  assert.equal(screen(), "directory");
  assert.equal(editors.length, editorCount);
  assert.equal(input("editor").hidden, true);
  assert.equal(document.activeElement.dataset.path, "a.py");
  overrides.set(`${projectContent}:a.py`, () => { throw new Error("读取失败"); });
  await openFile("a.py");
  assert.equal(screen(), "file");
  assert.match(input("note").textContent, /读取失败/);
  await trigger("back");
  await openFile("unsupported.bin");
  assert.equal(screen(), "file");
  assert.match(input("note").textContent, /不支持预览/);
  await trigger("back");
  assert.equal(screen(), "directory");

  // Lazy editor loading can settle after navigation; neither success nor failure
  // may resurrect a preview or inject an obsolete file error into the directory.
  await resetProject();
  for (const fail of [true, false]) {
    window.ace = undefined;
    const priorScripts = document.querySelectorAll("script").length;
    const lazyRead = openFile("a.py");
    await until(() => document.querySelectorAll("script").length > priorScripts, "lazy editor script requested");
    const script = document.querySelectorAll("script").at(-1);
    const beforeEditors = editors.length;
    await trigger("back");
    if (fail) script.onerror();
    else { window.ace = ace; script.onload(); }
    await lazyRead;
    assert.equal(screen(), "directory");
    assert.equal(editors.length, beforeEditors);
    assert.equal(input("note").textContent, "");
    assert.equal(input("editor").hidden, true);
  }

  await resetProject();
  await openFile("readme.md");
  const beforeDirectoryLink = readRequests("content").length;
  overrides.set(`${projectDirectory}:docs`, () => { throw new Error("链接所在目录暂时不可读"); });
  await markdownLink("docs/guide.md");
  assert.equal(screen(), "file");
  assert.equal(input("filename").textContent, "readme.md");
  assert.match(input("note").textContent, /链接所在目录暂时不可读/,
    "a failed linked file lookup exposes its directory error on the current content screen");
  assert.equal(readRequests("content").length, beforeDirectoryLink);
  overrides.set(`${projectDirectory}:docs`, () => listing("docs", docEntries));
  await markdownLink("docs");
  assert.equal(screen(), "directory", "an unhinted local directory link opens the directory screen");
  assert.deepEqual(visiblePaths(), docEntries.map((item) => item.path));
  assert.equal(readRequests("content").length, beforeDirectoryLink);
  await openFile("docs/guide.md");
  await markdownLink("docs/sub/deep.md");
  assert.equal(screen(), "file");
  assert.equal(input("filename").textContent, "docs/sub/deep.md");
  assert.equal(editors.at(-1).text, "docs/sub/deep.md");
  await trigger("back");
  assert.deepEqual(visiblePaths(), ["docs/sub/deep.md"], "returning from a linked file opens its containing directory");
  assert.equal(document.activeElement.dataset.path, "docs/sub/deep.md", "returning from a linked file focuses that file in its directory");
  await openFile("docs/sub/deep.md");
  await markdownLink("", true);
  assert.equal(screen(), "directory");
  assert.deepEqual(visiblePaths(), rootEntries.map((item) => item.path));

  // Resizing projects the same cached tree and live preview without new I/O.
  await resetProject();
  await openFile("docs");
  await openFile("docs/guide.md");
  const sharedEditor = editors.at(-1);
  const beforeResize = requests.length;
  changeWidth(false);
  assert.equal(input("back").hidden, true);
  assert.equal(input("collapse").hidden, false);
  assert(entry("docs") && entry("a.py") && entry("docs/guide.md"));
  assert.equal(entry("docs").getAttribute("aria-expanded"), "true");
  assert.equal(input("editor").hidden, false);
  assert.equal(editors.at(-1), sharedEditor);
  changeWidth(true);
  assert.equal(screen(), "file");
  assert.equal(input("back").hidden, false);
  assert.deepEqual(visiblePaths(), docEntries.map((item) => item.path));
  assert.equal(editors.at(-1), sharedEditor);
  assert.equal(requests.length, beforeResize);
  await trigger("back");
  changeWidth(false);
  const beforeDesktopExpand = editors.length;
  await openFile("docs/sub");
  assert(entry("a.py") && entry("docs/sub/deep.md"), "desktop expansion retains its siblings");
  assert.equal(editors.length, beforeDesktopExpand);
  await trigger("collapse");
  const beforeCollapsedResize = requests.length;
  changeWidth(true);
  assert.deepEqual(visiblePaths(), rootEntries.map((item) => item.path),
    "entering mobile layout after desktop cache collapse falls back to a cached directory");
  assert.equal(input("breadcrumbs").textContent, "根目录");
  assert.equal(requests.length, beforeCollapsedResize);

  // A changed root clears both mobile screens and cannot restore stale metadata.
  await resetProject();
  overrides.set(`${projectContent}:a.py`, () => { throw Object.assign(new Error("工作目录已变化"), { code: "root_changed" }); });
  await openFile("a.py");
  assert.equal(screen(), "directory");
  assert.equal(input("back").hidden, true);
  assert.deepEqual(visiblePaths(), []);
  assert.equal(input("filename").textContent, "选择文件查看");
  assert.equal(input("cwd").textContent, "");
  assert.match(input("directory-note").textContent, /工作目录已变化/);
  overrides.set("/api/v1/project-files/root:", () => ({ identity: "new-root", rootId: "project", cwd: "/new-project" }));
  await trigger("refresh");
  assert.equal(latestRead("directory").query.get("identity"), "new-root");
  assert.deepEqual(visiblePaths(), rootEntries.map((item) => item.path));

  const batch = directoryEntry("batch");
  const otherBatch = directoryEntry("other");
  const pdf = fileEntry("batch/material.pdf", { version: "pdf-v1", deletable: true });
  const txt = fileEntry("batch/note.txt", { version: "txt-v1", deletable: true });
  const otherFile = fileEntry("other/remaining.txt", { version: "other-v1", deletable: true });
  const attachmentDirectory = "/api/v1/attachment-files/directory";
  const deleteOptions = "/api/v1/attachment-files/delete-options";
  const deleteRoute = "/api/v1/attachment-files/delete";
  const grant = { name: pdf.name, path: pdf.path,
    action: { csrfToken: "csrf", actionToken: "once", target: { kind: "attachment", path: pdf.path } } };
  let batchFiles;
  let confirmations = 0;
  const posts = () => requests.filter((request) => request.options.method === "POST");
  const resetAttachments = async () => {
    overrides.clear();
    changeWidth(true);
    batchFiles = [pdf, txt];
    confirmResponder = () => { confirmations += 1; return true; };
    overrides.set("/api/v1/attachment-files/root:", () => ({ rootId: "attachments", identity: "attachment-root", cwd: "/retained" }));
    overrides.set(`${attachmentDirectory}:`, () => listing("", [...(batchFiles.length ? [batch] : []), otherBatch]));
    overrides.set(`${attachmentDirectory}:batch`, () => {
      if (!batchFiles.length) throw Object.assign(new Error("附件批次已不存在"), { status: 404, code: "not_found" });
      return listing("batch", batchFiles);
    });
    overrides.set(`${attachmentDirectory}:other`, () => listing("other", [otherFile]));
    for (const item of [pdf, txt, otherFile]) {
      overrides.set(`/api/v1/attachment-files/content:${item.path}`, () => {
        if (item === pdf) throw Object.assign(new Error("此格式不支持预览"), { status: 422 });
        return { ...item, kind: "text", text: item.name, language: "text" };
      });
      overrides.set(`${deleteOptions}:${item.path}`, () => ({ ...grant, name: item.name, path: item.path }));
    }
    await attachments.open();
    await openFile("batch");
    await openFile(pdf.path);
    assert.equal(screen(), "file");
    assert.equal(input("delete").disabled, false, "unsupported preview retains exact attachment deletion");
  };

  await resetAttachments();
  overrides.set(`${deleteRoute}:`, () => { batchFiles = [txt]; return { deleted: true, path: pdf.path, removedBatch: false }; });
  await trigger("delete");
  assert.equal(screen(), "directory");
  assert.deepEqual(visiblePaths(), [txt.path]);
  assert.match(input("breadcrumbs").textContent, /batch/);
  assert.equal(input("delete").hidden, true);
  await trigger("back");
  await filter("t");
  assert.deepEqual(visiblePaths(), ["batch", "other"]);
  await openFile("batch");
  await filter("note");
  await openFile(txt.path);
  overrides.set(`${deleteRoute}:`, () => { batchFiles = []; return { deleted: true, path: txt.path, removedBatch: true }; });
  await trigger("delete");
  assert.equal(screen(), "directory");
  assert.equal(input("filter").value, "t", "deleting the final attachment restores the root's filter instead of its batch's filter");
  assert.deepEqual(visiblePaths(), ["other"]);
  assert.equal(input("breadcrumbs").textContent, "根目录");
  assert.equal(input("back").hidden, true);

  // The read after a confirmed deletion must not restore an obsolete filter or
  // focus after the user has resumed browsing, in this batch or back at root.
  for (const [returnToRoot, focusedControl, newFilter] of [
    [false, "filter", "note"], [true, "filter", "other"],
    [false, "close", null], [false, "refresh", null],
  ]) {
    await resetAttachments();
    const refreshedBatch = defer();
    let refreshStarted = false;
    overrides.set(`${attachmentDirectory}:batch`, () => { refreshStarted = true; return refreshedBatch.promise; });
    overrides.set(`${deleteRoute}:`, () => { batchFiles = [txt]; return { deleted: true, path: pdf.path, removedBatch: false }; });
    const refreshingDelete = trigger("delete");
    await until(() => refreshStarted, "confirmed deletion starts its directory refresh");
    if (returnToRoot) await trigger("back");
    input(focusedControl).focus();
    if (newFilter !== null) await filter(newFilter);
    refreshedBatch.resolve(listing("batch", [txt]));
    await refreshingDelete;
    assert.equal(screen(), "directory");
    assert.equal(input("filter").value, newFilter ?? "", "late deletion refresh preserves newly entered filtering");
    assert(document.activeElement === input(focusedControl), `late deletion refresh preserves focus on ${focusedControl}`);
    assert.deepEqual(visiblePaths(), returnToRoot ? ["other"] : [txt.path]);
  }

  await resetAttachments();
  const authorization = defer();
  overrides.set(`${deleteOptions}:${pdf.path}`, () => authorization.promise);
  const authorizing = trigger("delete");
  const authorizationRequest = requests.at(-1);
  const beforePosts = posts().length;
  const beforeConfirmations = confirmations;
  await trigger("back");
  assert.equal(authorizationRequest.options.signal.aborted, true);
  authorization.resolve(grant);
  await authorizing;
  assert.equal(posts().length, beforePosts, "Back during authorization cannot submit a deletion");
  assert.equal(confirmations, beforeConfirmations);
  assert.equal(screen(), "directory");
  assert.deepEqual(visiblePaths(), [pdf.path, txt.path]);

  await resetAttachments();
  const submittedDelete = defer();
  overrides.set(`${deleteRoute}:`, () => submittedDelete.promise);
  const priorPosts = posts().length;
  const deleting = trigger("delete");
  await until(() => posts().length > priorPosts, "deletion submitted");
  await trigger("back");
  await trigger("back");
  await openFile("other");
  await openFile(otherFile.path);
  const otherPreview = editors.at(-1);
  const beforeLateDelete = requests.length;
  batchFiles = [];
  submittedDelete.resolve({ deleted: true, path: pdf.path, removedBatch: true });
  await deleting;
  assert.equal(screen(), "file", "a submitted deletion cannot navigate away from a later selected file");
  assert.equal(input("filename").textContent, otherFile.path);
  assert.equal(editors.at(-1), otherPreview);
  assert.equal(requests.length, beforeLateDelete, "a removed batch requires no extra reads of another active screen");
  await trigger("back");
  assert.deepEqual(visiblePaths(), [otherFile.path]);
  await trigger("back");
  assert.deepEqual(visiblePaths(), ["other"], "late deletion still reconciles the shared cache");

  await resetAttachments();
  overrides.set(`${deleteRoute}:`, () => { batchFiles = []; throw new Error("response lost after deletion"); });
  await trigger("delete");
  assert.equal(screen(), "file");
  assert.match(input("action-note").textContent, /结果尚未确认/);
  assert.equal(input("delete").disabled, true);
  const postsAfterUnknown = posts().length;
  await trigger("refresh");
  assert.equal(screen(), "directory");
  assert.deepEqual(visiblePaths(), ["other"]);
  assert.equal(input("back").hidden, true);
  assert.equal(posts().length, postsAfterUnknown, "refresh after an uncertain deletion only reconciles reads");

  // External deletion of the last attachment returns refresh to a valid root.
  await resetAttachments();
  batchFiles = [];
  overrides.set(`${attachmentDirectory}:`, () => { throw new Error("附件根目录暂时不可读"); });
  await trigger("refresh");
  assert.equal(screen(), "directory", "a failed root recovery leaves its error on the visible directory screen");
  assert.match(input("directory-note").textContent, /附件根目录暂时不可读/);
  assert.equal(input("note").textContent, "", "a removed batch cannot leave a stale file-refresh indicator");
  assert.equal(input("back").hidden, true);
  overrides.set(`${attachmentDirectory}:`, () => listing("", [otherBatch]));
  await trigger("refresh");
  assert.equal(screen(), "directory");
  assert.deepEqual(visiblePaths(), ["other"]);
  assert.equal(input("breadcrumbs").textContent, "根目录");
  assert.equal(input("back").hidden, true);
  assert.equal(input("delete").hidden, true);
  console.log("mobile directory navigation, cached layout transitions, preview races and attachment deletion passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
