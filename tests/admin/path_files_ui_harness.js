const media = { matches: false, listeners: [], addEventListener(_, listener) { this.listeners.push(listener); } };
const bodyClasses = new Set();
document.body.classList = {
  toggle(name, enabled) { if (enabled) bodyClasses.add(name); else bodyClasses.delete(name); },
  contains(name) { return bodyClasses.has(name); },
};
window.matchMedia = (query) => query === "(max-width: 720px)" ? media : { matches: false, addEventListener() {} };
const until = async (condition) => {
  for (let step = 0; step < 100 && !condition(); step += 1) await Promise.resolve();
  assert(condition(), "condition must settle within the bounded microtask budget");
};
const originalEdit = window.ace.edit;
window.ace.edit = () => {
  const editor = originalEdit();
  const setValue = editor.setValue;
  editor.calls = [];
  editor.setValue = (value) => { editor.calls.push(["setValue", value]); setValue(value); };
  editor.gotoLine = (line) => { editor.calls.push(["gotoLine", line]); editor.line = line; };
  return editor;
};

(async () => {
  const browser = window.NetizenFileBrowser.create({ container: input("drawer"), api, isExpired: () => expired });
  const paths = window.NetizenPathFiles;
  let provider;
  paths.init({ browser: { open(value) { provider = value; return browser.open(value); }, close: browser.close } });
  const answer = document.querySelector("#session-answer-drawer");
  const link = document.createElement("a");
  document.querySelector("#session-answer-content").append(link);
  answer.showModal();
  document.body.classList.toggle("session-answer-open", true);
  link.focus();
  overrides.set("/api/v1/path-files/root:", () => ({ rootId: "path", cwd: "/canonical", identity: "path-identity" }));
  await paths.open({ root: "/alias", file: "a.py", line: 12 });
  assert.equal(answer.open, true, "opening a second modal retains the conversation");
  assert.equal(input("drawer").open, true);
  assert.equal(input("root").hidden, true);
  assert.equal(input("difference").hidden, true);
  assert.equal(input("filename").textContent, "a.py");
  assert.deepEqual(editors.at(-1).calls, [["setValue", files["a.py"].text], ["gotoLine", 12]]);
  for (const request of requests) {
    assert(request.route.startsWith("/api/v1/path-files/"));
    assert.equal(request.query.get("root"), request.route.endsWith("/root") ? "/alias" : "/canonical");
    if (!request.route.endsWith("/root")) assert.equal(request.query.get("identity"), "path-identity");
  }
  await trigger("close");
  assert.equal(document.activeElement, link);
  assert(answer.open);
  assert(document.body.classList.contains("session-answer-open"));
  assert(!document.body.classList.contains("file-browser-open"));

  await paths.open({ root: "/alias", file: "src", line: 12 });
  assert.equal(requests.at(-1).route, "/api/v1/path-files/directory");
  assert.equal(requests.at(-1).query.get("path"), "src");
  assert.equal(input("drawer").dataset.screen, "directory");
  await paths.open({ root: "/alias" });
  assert.equal(requests.at(-1).query.get("path"), "");
  assert.equal(input("drawer").dataset.screen, "directory");

  overrides.set("/api/v1/path-files/directory:", () => ({ entries: [], truncated: true, path: "" }));
  await paths.open({ root: "/alias", file: "a.py", line: 5 });
  assert.equal(editors.at(-1).text, files["a.py"].text, "a target outside the bounded listing is still read directly");
  assert.equal(editors.at(-1).line, 5);
  const beforeRefresh = requests.length;
  await trigger("refresh");
  assert.equal(input("filename").textContent, "a.py");
  assert.equal(editors.at(-1).text, files["a.py"].text, "refresh retains files outside a truncated listing");
  assert.equal(editors.at(-1).line, undefined, "refresh does not repeat the initial line jump");
  assert(requests.slice(beforeRefresh).some((request) => request.route.endsWith("/content") && request.query.get("path") === "a.py"));
  overrides.set("/api/v1/path-files/directory:", () => ({ entries: [], truncated: false, path: "" }));
  await trigger("refresh");
  assert.equal(input("editor").hidden, true, "a complete listing still clears a missing file");
  assert.match(input("note").textContent, /原文件不在当前目录列表中/);
  overrides.set("/api/v1/path-files/directory:", () => ({ entries: [], truncated: true, path: "" }));
  overrides.set("/api/v1/path-files/content:gone", () => { throw new Error("文件或目录不存在。"); });
  const beforeMissing = requests.length;
  await paths.open({ root: "/alias", file: "gone" });
  assert.match(input("note").textContent, /文件或目录不存在/);
  assert.equal(requests.length - beforeMissing, 3, "missing files do not search or fall back");
  overrides.delete("/api/v1/path-files/directory:");

  // Delay the shared root-option phase to expose the interval after the tree
  // renders and before initial selection, without timers or a private hook.
  const rootPage = defer();
  overrides.set("/held-roots:", () => rootPage.promise);
  const opening = browser.open({ ...provider, initial: { file: "a.py", line: 15 },
    rootPage: () => "/held-roots", rootOptions: () => [] });
  await until(() => entry("readme.md"));
  const beforeChoice = requests.length;
  await openFile("readme.md");
  rootPage.resolve({ items: [], nextCursor: null });
  await opening;
  assert.equal(editors.at(-1).text, files["readme.md"].text);
  assert.equal(editors.at(-1).line, undefined);
  assert(!requests.slice(beforeChoice).some((request) => request.route.endsWith("/content") && request.query.get("path") === "a.py"));

  for (const narrow of [false, true]) {
    media.matches = narrow;
    const heldRoots = defer();
    const heldDirectory = defer();
    overrides.set("/held-roots:", () => heldRoots.promise);
    overrides.set("/api/v1/path-files/directory:src", () => heldDirectory.promise);
    const directoryOpening = browser.open({ ...provider, initial: { file: "a.py", line: 15 },
      rootPage: () => "/held-roots", rootOptions: () => [] });
    await until(() => entry("src"));
    const beforeDirectoryChoice = requests.length;
    const navigating = openFile("src");
    await until(() => requests.slice(beforeDirectoryChoice).some((request) => request.query.get("path") === "src"));
    heldRoots.resolve({ items: [], nextCursor: null });
    await directoryOpening;
    assert(!requests.slice(beforeDirectoryChoice).some((request) => request.route.endsWith("/content")),
      "choosing a directory cancels initial selection, including while its compact listing is loading");
    heldDirectory.resolve({ entries: [], truncated: false, path: "src" });
    await navigating;
    overrides.delete("/api/v1/path-files/directory:src");
  }
  media.matches = false;

  const slowContent = defer();
  overrides.set("/api/v1/path-files/content:a.py", () => slowContent.promise);
  const slowOpening = paths.open({ root: "/alias", file: "a.py", line: 7 });
  await until(() => input("filename").textContent === "a.py");
  await openFile("readme.md");
  slowContent.resolve({ path: "a.py", ...files["a.py"] });
  await slowOpening;
  assert.equal(editors.at(-1).text, files["readme.md"].text);
  assert.equal(editors.at(-1).line, undefined, "a late initial read cannot jump in another file");
  overrides.delete("/api/v1/path-files/content:a.py");

  const slowMarkdown = defer();
  overrides.set("/api/v1/path-files/content:readme.md", () => slowMarkdown.promise);
  const previewOpening = paths.open({ root: "/alias", file: "readme.md", line: 8 });
  await until(() => input("filename").textContent === "readme.md");
  // A Markdown mode becomes available after metadata arrives. Hold Ace's load
  // while the user switches to preview so the pending source jump is discarded.
  const ace = window.ace;
  window.ace = null;
  slowMarkdown.resolve({ path: "readme.md", ...files["readme.md"] });
  await until(() => !input("preview").hidden && !input("preview").disabled);
  await trigger("preview");
  assert.equal(input("markdown").hidden, false);
  window.ace = ace;
  const script = document.querySelectorAll("script").find((child) => child.src === "/static/vendor/ace/ace.js");
  script.onload();
  await previewOpening;
  await trigger("source");
  assert.equal(editors.at(-1).line, undefined, "preview mode consumes the pending line permanently");
  overrides.delete("/api/v1/path-files/content:readme.md");

  media.matches = true;
  for (const listener of media.listeners) listener({ matches: true });
  browser.close(); link.focus();
  await paths.open({ root: "/alias", file: "a.py", line: 3 });
  assert.equal(input("drawer").dataset.screen, "file");
  assert.equal(input("back").hidden, false);
  await trigger("back");
  assert.equal(input("drawer").dataset.screen, "directory");
  assert(entry("a.py"));
  assert.equal(input("back").hidden, true);
  await trigger("close");
  assert.equal(document.activeElement, link);

  overrides.set("/api/v1/path-files/root:", () => { throw new Error("没有读取此文件或目录的权限。"); });
  const beforeFailure = requests.length;
  await paths.open({ root: "/alias", file: "a.py", line: 2 });
  assert.match(input("directory-note").textContent, /没有读取/);
  assert.equal(requests.length, beforeFailure + 1);
  assert.equal(input("editor").hidden, true);
  browser.close();
  answer.close();
  console.log("answer-link directory navigation, lines, races, modal focus, and compact return passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
