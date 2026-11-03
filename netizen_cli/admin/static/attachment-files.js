"use strict";

// Fixed instance-owned attachments. Only a freshly authorized single file can
// be deleted; confirmation never implies the Agent has finished reading it.
window.NetizenAttachmentFiles = (() => {
  let dependencies;
  function query({ identity, path, version }) {
    const params = new URLSearchParams();
    if (identity) params.set("identity", identity);
    if (path !== undefined) params.set("path", path);
    if (version) params.set("version", version);
    return params;
  }
  async function remove(file, { isCurrent, signal }) {
    const options = await dependencies.api(`/api/v1/attachment-files/delete-options?${query(file)}`, { signal });
    if (!isCurrent() || dependencies.isExpired()) return null;
    if (!window.confirm(`确认删除附件“${options.displayName || options.name}”？\n\n${options.confirmation || "删除可能影响正在执行或后续任务的读取。需要时请重新发送文件。"}`)) return null;
    if (!isCurrent() || dependencies.isExpired()) return null;
    const action = options.action;
    try {
      return await dependencies.api("/api/v1/attachment-files/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csrfToken: action.csrfToken, actionToken: action.actionToken, target: action.target }),
      });
    } catch (error) {
      // A failed read of the grant is harmless; a failed mutation may already
      // have deleted the file. Never automatically retry a POST.
      if (!error.status || error.status >= 500) error.uncertain = true;
      throw error;
    }
  }
  function open() {
    if (dependencies.isExpired()) return;
    return dependencies.browser.open({
      rootId: "attachments", title: "文件附件", eyebrow: "INPUT ATTACHMENTS", emptyTitle: "浏览文件附件",
      description: "展开保存批次，选择附件查看信息或删除。支持预览的格式可直接查看内容。",
      entryLabel(entry) {
        if (entry.kind !== "directory" || !entry.savedAt) return entry.displayName || entry.name;
        const saved = new Date(entry.savedAt);
        if (Number.isNaN(saved.getTime())) return entry.displayName || entry.name;
        return `${saved.toLocaleString(undefined, { year: "numeric", month: "2-digit", day: "2-digit",
          hour: "2-digit", minute: "2-digit" })} · ${entry.name.slice(0, 8)}`;
      },
      url(operation, selection) { return `/api/v1/attachment-files/${operation}?${query(selection)}`; },
      remove,
    });
  }
  return { init(options) { dependencies = options; }, open };
})();
