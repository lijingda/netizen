"use strict";

// Answer links open their own directory read-only; no Project identity or Git state.
window.NetizenPathFiles = (() => {
  let browser;
  function open(target) {
    return browser.open({
      changes: false, rootId: "path", title: "查看文件", eyebrow: "对话中的文件",
      emptyTitle: "浏览目录", description: "链接所在目录的当前内容，只读。",
      initial: target.file ? { file: target.file, line: target.line || null } : null,
      url(operation, { identity, path, cwd }) {
        // Resolve the link's spelling once; subsequent reads pin its canonical cwd.
        const params = new URLSearchParams({ root: operation === "root" ? target.root : cwd });
        if (identity) params.set("identity", identity);
        if (path !== undefined) params.set("path", path);
        return `/api/v1/path-files/${operation}?${params}`;
      },
    });
  }
  return { init(options) { browser = options.browser; }, open,
    close() { browser?.close(); }, expire() { browser?.expire(); } };
})();
