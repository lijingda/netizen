"use strict";

// Project identity and managed-worktree selection stay out of the shared browser.
window.NetizenProjectFiles = (() => {
  let browser;
  function open(project) {
    return browser.open({
      changes: true,
      rootId: "project", title: `${project.alias} · 查看文件`, eyebrow: "Project 文件",
      emptyTitle: "浏览项目文件", description: "展开目录，选择文件查看内容。",
      roots: [{ id: "project", label: `Project · ${project.cwd}` }],
      url(operation, { rootId, identity, path }) {
        const params = new URLSearchParams({ project: project.alias });
        if (rootId !== "project") params.set("worktree", rootId);
        if (identity) params.set("identity", identity);
        if (path !== undefined) params.set("path", path);
        return `/api/v1/project-files/${operation}?${params}`;
      },
      rootPage(cursor) {
        const params = new URLSearchParams({ project: project.alias, pageSize: "50" });
        if (cursor) params.set("cursor", cursor);
        return `/api/v1/worktrees?${params}`;
      },
      rootOptions(data) {
        return data.items.map((tree) => {
          const disabled = tree.creationState !== "ready" || tree.state !== "ready" || Boolean(tree.orphaned);
          return { id: tree.id, disabled,
            label: `worktree · ${tree.branch || tree.id} · ${tree.cwd}${disabled ? "（不可用）" : ""}` };
        });
      },
    });
  }
  return { init(options) { browser = options.browser; }, open,
    close() { browser?.close(); }, expire() { browser?.expire(); } };
})();
