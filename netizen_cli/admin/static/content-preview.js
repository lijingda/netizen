"use strict";

// Shared presentation for file previews and session answers.
window.NetizenContentPreview = {
  // Only HTML from the bounded Admin Markdown renderer belongs in this sink.
  renderMarkdown(target, html) {
    target.replaceChildren();
    if (typeof html === "string") target.innerHTML = html;
  },
};
