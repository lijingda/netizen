(async () => {
  const browser = window.NetizenFileBrowser.create({ container: input("drawer"), api, isExpired: () => expired });
  const projects = window.NetizenProjectFiles;
  projects.init({ browser });
  const attachments = window.NetizenAttachmentFiles;
  attachments.init({ browser, api, isExpired: () => expired });
  let approved = true;
  const confirmations = [];
  confirmResponder = (message) => { confirmations.push(message); return approved; };
  const rootRoute = "/api/v1/attachment-files/root";
  const directoryRoute = "/api/v1/attachment-files/directory";
  const contentRoute = "/api/v1/attachment-files/content";
  const optionsRoute = "/api/v1/attachment-files/delete-options";
  const deleteRoute = "/api/v1/attachment-files/delete";
  const batch = { name: "abcdef", path: "abcdef", kind: "directory", displayName: "2026-11-03 10:30 · abcdef", savedAt: "2026-11-03T02:30:00Z" };
  const pdf = { name: "材料.pdf", path: "abcdef/材料.pdf", kind: "file", size: 2048,
    modifiedAt: "2026-11-02T09:00:00Z", savedAt: "2026-11-03T02:30:00Z", version: "pdf-v1", deletable: true };
  const txt = { name: "说明.txt", path: "abcdef/说明.txt", kind: "file", size: 100,
    savedAt: "2026-11-03T02:30:00Z", version: "txt-v1", deletable: true };
  let batchFiles = [pdf, txt];
  const listing = (path, entries) => ({ path, entries, truncated: false });
  const grant = { name: pdf.name, path: pdf.path, size: pdf.size,
    confirmation: "删除可能影响正在执行或后续任务的读取。需要时请重新发送文件。",
    action: { csrfToken: "csrf", actionToken: "once", target: { kind: "attachment", path: pdf.path } } };
  overrides.set(`${rootRoute}:`, () => ({ rootId: "attachments", cwd: "/instance/attachments/retained", identity: "attachment-root" }));
  overrides.set(`${directoryRoute}:`, () => listing("", batchFiles.length ? [batch] : []));
  overrides.set(`${directoryRoute}:abcdef`, () => {
    if (!batchFiles.length) throw Object.assign(new Error("附件批次已不存在"), { status: 404, code: "not_found" });
    return listing("abcdef", batchFiles);
  });
  overrides.set(`${contentRoute}:${pdf.path}`, () => { throw Object.assign(new Error("此格式不支持预览"), { status: 422 }); });
  overrides.set(`${contentRoute}:${txt.path}`, () => ({ path: txt.path, kind: "text", text: "说明", language: "text", size: 100, version: txt.version }));
  overrides.set(`${optionsRoute}:${pdf.path}`, () => grant);
  overrides.set(`${optionsRoute}:${txt.path}`, () => ({ ...grant, name: txt.name, path: txt.path }));
  const postCount = () => requests.filter((request) => request.options.method === "POST").length;
  assert.equal(requests.length, 0);
  await attachments.open();
  assert.equal(requests.length, 2, "opening attachments reads only the fixed root and one directory layer");
  assert(requests.every((request) => !request.query.has("project") && !request.query.has("worktree")));
  assert.equal(input("root").hidden, true);
  assert.equal(input("more-roots").hidden, true);
  assert.equal(input("permission").textContent, "可查看、删除");
  assert.match(entry(batch.path).textContent, /2026.* · abcdef/);
  assert(!entry(batch.path).textContent.includes("T02:30:00Z"), "batch labels use readable local saved time");
  assert.equal(input("delete").hidden, true, "a directory is never a delete target");
  await openFile(batch.path);
  await openFile(pdf.path);
  assert.match(input("note").textContent, /不支持预览/);
  assert.match(input("meta").textContent, /2.0 KiB.*保存于/);
  assert.equal(input("filename").textContent, pdf.path);
  assert.equal(input("delete").disabled, false, "metadata and deletion do not depend on successful content preview");
  assert.equal(input("tools").hidden, true);

  approved = false;
  await trigger("delete");
  assert.equal(postCount(), 0, "canceling confirmation does not mutate");
  assert.equal(requests.at(-1).query.get("identity"), "attachment-root");
  assert.equal(requests.at(-1).query.get("version"), "pdf-v1");
  assert.match(confirmations.at(-1), /材料.pdf/);
  assert.match(confirmations.at(-1), /正在执行或后续任务/);
  approved = true;

  const staleGrant = defer();
  overrides.set(`${optionsRoute}:${pdf.path}`, () => staleGrant.promise);
  const preparing = trigger("delete");
  const priorConfirmations = confirmations.length;
  await openFile(txt.path);
  staleGrant.resolve(grant);
  await preparing;
  assert.equal(confirmations.length, priorConfirmations, "a grant for a file no longer selected is not confirmed");
  assert.equal(postCount(), 0);
  overrides.set(`${optionsRoute}:${pdf.path}`, () => grant);
  await openFile(pdf.path);

  overrides.set(`${deleteRoute}:`, () => { throw Object.assign(new Error("目标已变化，请刷新"), { status: 409 }); });
  await trigger("delete");
  assert.match(input("action-note").textContent, /目标已变化/);
  assert.equal(input("delete").disabled, false);
  overrides.set(`${deleteRoute}:`, () => { throw new Error("connection lost"); });
  await trigger("delete");
  assert.match(input("action-note").textContent, /结果尚未确认.*刷新/);
  assert.equal(input("delete").disabled, true);
  const uncertainPosts = postCount();
  await trigger("delete");
  assert.equal(postCount(), uncertainPosts, "an uncertain mutation is never automatically retried");
  await trigger("refresh");
  assert.equal(input("delete").disabled, false, "explicit refresh reacquires metadata before another attempt");

  const deleting = defer();
  overrides.set(`${deleteRoute}:`, () => deleting.promise);
  const deletion = trigger("delete");
  for (let i = 0; i < 30 && postCount() === uncertainPosts; i += 1) await Promise.resolve();
  assert(postCount() > uncertainPosts, "confirmed deletion has reached the POST");
  const inFlightCount = postCount();
  assert.equal(input("delete").disabled, true);
  await trigger("delete");
  assert.equal(postCount(), inFlightCount);
  const submitted = requests.filter((request) => request.options.method === "POST").at(-1);
  assert.deepEqual(JSON.parse(submitted.options.body), grant.action);
  const beforeDeleteRefresh = requests.length;
  batchFiles = [txt];
  deleting.resolve({ deleted: true, path: pdf.path, removedBatch: false });
  await deletion;
  assert.equal(entry(pdf.path), undefined);
  assert(entry(txt.path));
  assert.equal(input("filename").textContent, "选择文件查看");
  assert.match(input("action-note").textContent, /已删除 材料.pdf/);
  assert.deepEqual(requests.slice(beforeDeleteRefresh).map((request) => [request.route, request.query.get("path")]),
    [[directoryRoute, "abcdef"]], "deletion refreshes only the affected batch");

  await openFile(txt.path);
  overrides.set(`${deleteRoute}:`, () => { batchFiles = []; return { deleted: true, path: txt.path, removedBatch: true }; });
  const beforeEmptyDelete = requests.length;
  await trigger("delete");
  assert.equal(entry(batch.path), undefined, "an empty removed batch disappears without recursively listing the root");
  assert.equal(requests.slice(beforeEmptyDelete).filter((request) => request.route === directoryRoute).length, 0);

  // Another Admin can remove the current file while this browser retains its
  // preview; refresh clears that selection but only reads the affected batch.
  batchFiles = [pdf, txt];
  await attachments.open();
  await openFile(batch.path);
  await openFile(txt.path);
  batchFiles = [pdf];
  const beforeExternalDeleteRefresh = requests.length;
  await trigger("refresh");
  assert.equal(entry(txt.path), undefined);
  assert.equal(input("filename").textContent, "选择文件查看");
  assert.equal(input("editor").hidden, true);
  assert.equal(input("delete").hidden, true);
  assert.deepEqual(requests.slice(beforeExternalDeleteRefresh).map((request) => [request.route, request.query.get("path")]),
    [[directoryRoute, "abcdef"]], "a removed file clears the stale selection without a root scan");
  await trigger("refresh");
  assert.equal(requests.at(-1).route, directoryRoute, "later refreshes no longer try the deleted file content");

  // If the other Admin removed the last file, the batch itself no longer
  // exists. One root listing replaces the stale batch and restores navigation.
  await openFile(pdf.path);
  batchFiles = [];
  const beforeMissingBatchRefresh = requests.length;
  await trigger("refresh");
  assert.deepEqual(requests.slice(beforeMissingBatchRefresh).map((request) => [request.route, request.query.get("path")]),
    [[directoryRoute, "abcdef"], [directoryRoute, ""]]);
  assert.equal(entry(batch.path), undefined);
  assert.equal(input("breadcrumbs").textContent, "根目录");
  assert.equal(input("delete").hidden, true);
  await trigger("refresh");
  assert.equal(requests.at(-1).query.get("path"), "", "refresh remains at the restored root");

  // The identical reconciliation works when our deletion completed but its
  // HTTP response was lost; it never replays the mutation.
  batchFiles = [pdf];
  await attachments.open();
  await openFile(batch.path);
  await openFile(pdf.path);
  overrides.set(`${deleteRoute}:`, () => { batchFiles = []; throw new Error("response lost after deletion"); });
  await trigger("delete");
  assert.match(input("action-note").textContent, /结果尚未确认/);
  const postsAfterLostResponse = postCount();
  const beforeUnknownRefresh = requests.length;
  await trigger("refresh");
  assert.deepEqual(requests.slice(beforeUnknownRefresh).map((request) => [request.route, request.query.get("path")]),
    [[directoryRoute, "abcdef"], [directoryRoute, ""]]);
  assert.equal(postCount(), postsAfterLostResponse);
  assert.equal(entry(batch.path), undefined);
  assert.equal(input("actions").hidden, true);
  assert.equal(input("filename").textContent, "选择文件查看");
  assert.match(input("note").textContent, /原目录已移除/);

  batchFiles = [pdf, txt];
  await attachments.open();
  await openFile(batch.path);
  await openFile(pdf.path);
  const switchedDelete = defer();
  overrides.set(`${deleteRoute}:`, () => switchedDelete.promise);
  const switching = trigger("delete");
  await Promise.resolve();
  await Promise.resolve();
  await projects.open(project);
  await openFile("a.py");
  const projectRequests = requests.length;
  switchedDelete.resolve({ deleted: true, path: pdf.path, removedBatch: false });
  await switching;
  assert.equal(input("title").textContent, "demo · 查看文件");
  assert.equal(input("filename").textContent, "a.py");
  assert.equal(input("actions").hidden, true, "Project provider never offers mutations");
  assert.equal(input("permission").textContent, "只读");
  assert.equal(requests.length, projectRequests, "a previous attachment mutation cannot refresh or change the new provider");

  await attachments.open();
  await openFile(batch.path);
  await openFile(pdf.path);
  const expiredGrant = defer();
  overrides.set(`${optionsRoute}:${pdf.path}`, () => expiredGrant.promise);
  const authorizing = trigger("delete");
  const postsBeforeExpire = postCount();
  const grantRequest = requests.at(-1);
  expired = true;
  projects.expire();
  assert.equal(grantRequest.options.signal.aborted, true, "read-only authorization preparation is canceled on expiration");
  expiredGrant.resolve(grant);
  await authorizing;
  assert.equal(postCount(), postsBeforeExpire);
  assert.equal(input("drawer").open, false);
  assert.equal(input("entries").children.length, 0);
  assert.equal(input("filename").textContent, "选择文件查看");
  assert.equal(input("meta").textContent, "");
  assert.equal(input("action-note").textContent, "");
  console.log("shared attachment navigation, metadata and exact deletion behavior passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
