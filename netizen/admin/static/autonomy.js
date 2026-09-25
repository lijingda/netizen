"use strict";

// Optional experiment controller: removing this file and its small host hooks
// leaves the ordinary Admin and session execution controllers independent.
const autonomyDefaults = {
  jev: { base_url: "https://api.typesafe.ai", model: "jev-1.13.0", input_budget: 32000 },
  laya: { base_url: "http://127.0.0.1:8000", model: "multilingual", input_budget: 1024 },
};
let autonomyStatus = null;
let autonomyBusy = false;

function renderAutonomyStatus() {
  const data = autonomyStatus;
  const labels = {
    unconfigured: "尚未配置；不影响原有消息模式。",
    ready: "配置已应用。连接可用性以测试或后续判断结果为准。",
    unavailable: "自主判断不可用；显式 @ 和控制操作仍可用。",
  };
  document.querySelector("#autonomy-state").textContent = data?.supported === false
    ? "当前实例未装配自主模式实验功能。"
    : `${labels[data?.state] || "状态尚未确认。"}${data?.error ? ` ${data.error}` : ""}`;
  document.querySelector("#autonomy-fields").disabled = autonomyBusy || !data?.actions.configure;
  document.querySelector("#autonomy-test").disabled = autonomyBusy || !data?.actions.test;
  document.querySelector("#autonomy-clear").disabled = autonomyBusy
    || !data?.actions.configure || !data?.configured;
  document.querySelector("#autonomy-key-status").textContent = data?.config?.has_api_key
    ? "已保存密钥；留空保留。" : "未保存密钥。";
}

function fillAutonomyForm() {
  const config = autonomyStatus.config || { provider: "jev", ...autonomyDefaults.jev, timeout_seconds: 10 };
  document.querySelector("#autonomy-provider").value = config.provider;
  document.querySelector("#autonomy-base-url").value = config.base_url;
  document.querySelector("#autonomy-model").value = config.model;
  document.querySelector("#autonomy-timeout").value = config.timeout_seconds;
  document.querySelector("#autonomy-budget").value = config.input_budget;
  document.querySelector("#autonomy-key").value = "";
  document.querySelector("#autonomy-clear-key").checked = false;
}

async function loadAutonomy() {
  if (autonomyBusy) return;
  autonomyStatus = await api("/api/v1/autonomy");
  fillAutonomyForm();
  renderAutonomyStatus();
}

async function mutateAutonomy(kind, config = null) {
  if (autonomyBusy || !autonomyStatus?.actions[kind]) return;
  const grant = autonomyStatus.actions[kind];
  autonomyBusy = true;
  renderAutonomyStatus();
  try {
    const payload = { csrfToken: grant.csrfToken, actionToken: grant.actionToken, target: grant.target };
    if (config !== null) payload.config = config;
    const result = await api(`/api/v1/autonomy/${kind}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (kind === "configure") {
      autonomyStatus = result;
      fillAutonomyForm();
      setStatus(result.configured ? "决策配置已保存并应用，下次判断生效。" : "决策配置已清除。会话选择未改变。");
    } else {
      autonomyStatus = await api("/api/v1/autonomy");
      setStatus(result.ok ? "连接测试通过；不代表群聊判断质量已验证。" : (result.error || "连接测试失败。"), !result.ok);
    }
  } catch (error) {
    // Never retry an uncertain mutation. Refresh grants without overwriting the
    // user's non-secret draft; an explicit refresh loads the saved configuration.
    try { autonomyStatus = await api("/api/v1/autonomy"); }
    catch (_refreshError) { autonomyStatus = null; }
    setStatus(`${error.message} 请刷新核对已保存配置后再操作。`, true);
  } finally {
    document.querySelector("#autonomy-key").value = "";
    autonomyBusy = false;
    renderAutonomyStatus();
  }
}

document.querySelector("#autonomy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await mutateAutonomy("configure", {
    provider: document.querySelector("#autonomy-provider").value,
    base_url: document.querySelector("#autonomy-base-url").value.trim(),
    model: document.querySelector("#autonomy-model").value.trim(),
    api_key: document.querySelector("#autonomy-key").value,
    clear_api_key: document.querySelector("#autonomy-clear-key").checked,
    timeout_seconds: Number(document.querySelector("#autonomy-timeout").value),
    input_budget: Number(document.querySelector("#autonomy-budget").value),
  });
});
document.querySelector("#autonomy-provider").addEventListener("change", (event) => {
  const preset = autonomyDefaults[event.target.value];
  document.querySelector("#autonomy-base-url").value = preset.base_url;
  document.querySelector("#autonomy-model").value = preset.model;
  document.querySelector("#autonomy-budget").value = preset.input_budget;
});
document.querySelector("#autonomy-test").addEventListener("click", () => mutateAutonomy("test"));
document.querySelector("#autonomy-clear").addEventListener("click", () => {
  if (window.confirm("清除决策模型连接及密钥？已有自主会话将停止自动判断，但显式 @ 仍可用。")) {
    return mutateAutonomy("configure", { clear: true });
  }
});
