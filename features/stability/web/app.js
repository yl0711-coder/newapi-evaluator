const state = { inventory: [], channels: [], schedules: [], runs: [], reportGroups: [] };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function text(tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2200);
}

async function api(path, options = {}) {
  const response = await fetch(`.${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const value = (await response.json()).detail;
      detail = Array.isArray(value) ? value.map((item) => item.msg).join("；") : value || detail;
    } catch {}
    throw new Error(detail);
  }
  return response.json();
}

function formatDate(value) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "medium" }).format(new Date(value * 1000));
}

function percent(value) { return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "-"; }

async function loadHealth() {
  try {
    const data = await api("/api/health");
    const okay = data.status === "ok";
    $("#health-card").classList.toggle("bad", !okay);
    $("#health-label").textContent = okay ? "服务正常" : "服务降级";
    $("#health-detail").textContent = `${data.scheduler.active_runs} 个任务运行中`;
  } catch {
    $("#health-card").classList.add("bad");
    $("#health-label").textContent = "无法连接";
    $("#health-detail").textContent = "请检查服务状态";
  }
}

async function loadChannels() {
  state.channels = (await api("/api/channels")).channels;
  renderChannels();
}

async function loadInventory() {
  state.inventory = (await api("/api/channel-inventory")).inventory;
  const root = $("#inventory-list");
  if (!state.inventory.length) {
    root.replaceChildren(text("p", "资料库还没有渠道记录。", "empty"));
    return;
  }
  root.replaceChildren(...state.inventory.map((item) => {
    const card = text("article", "", "panel");
    card.append(text("h3", item.name));
    card.append(text("p", `${item.scope || "未分类"} · ${Number(item.multiplier).toFixed(2).replace(/\.00$/, "")}x`));
    card.append(text("p", `${item.base_url} · ${item.key_masked}`));
    return card;
  }));
}

function renderChannels() {
  const root = $("#channel-list");
  if (!state.channels.length) {
    root.replaceChildren(text("p", "还没有渠道，先添加一个需要巡检的模型端点。", "empty"));
    return;
  }
  root.replaceChildren(...state.channels.map((channel) => {
    const card = text("article", "", "panel");
    card.append(text("h3", channel.name));
    card.append(text("p", `${channel.model} · ${channel.protocol} · ${channel.enabled ? "已启用" : "已停用"}`));
    card.append(text("p", `${channel.base_url} · ${channel.key_masked}`));
    const actions = text("div", "", "actions");
    actions.append(action("编辑", () => openChannel(channel)));
    actions.append(action("删除", () => deleteChannel(channel), true));
    card.append(actions);
    return card;
  }));
}

function action(label, handler, danger = false) {
  const button = text("button", label, `text-button${danger ? " danger" : ""}`);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

async function openChannel(channel = null) {
  $("#channel-form").reset();
  $("#channel-id").value = channel?.id || "";
  $("#channel-title").textContent = channel ? "编辑渠道" : "新增渠道";
  $("#channel-name").value = channel?.name || "";
  $("#channel-model").value = channel?.model || "";
  $("#channel-protocol").value = channel?.protocol || "openai";
  $("#channel-enabled").checked = channel ? Boolean(channel.enabled) : true;
  await Workbench.picker($("#channel-registry"), channel?.registry_channel_id);
  $("#channel-error").textContent = "";
  $("#channel-dialog").showModal();
}

async function saveChannel(event) {
  event.preventDefault();
  const payload = {
    id: Number($("#channel-id").value) || null,
    name: $("#channel-name").value.trim(),
    registry_channel_id: Number($("#channel-registry").value),
    model: $("#channel-model").value.trim(),
    protocol: $("#channel-protocol").value,
    enabled: $("#channel-enabled").checked,
  };
  try {
    await api("/api/channels", { method: "POST", body: JSON.stringify(payload) });
    $("#channel-dialog").close();
    await Promise.all([loadChannels(), loadSchedules()]);
    toast("渠道已保存");
  } catch (error) { $("#channel-error").textContent = error.message; }
}

async function deleteChannel(channel) {
  if (!confirm(`删除渠道“${channel.name}”？历史记录会保留。`)) return;
  await api(`/api/channels/${channel.id}`, { method: "DELETE" });
  await Promise.all([loadChannels(), loadSchedules()]);
}

async function loadSchedules() {
  state.schedules = (await api("/api/schedules")).schedules;
  renderSchedules();
}

function renderSchedules() {
  const root = $("#schedule-list");
  if (!state.schedules.length) {
    root.replaceChildren(text("p", "还没有计划。创建后会按时区自动执行并保存历史。", "empty"));
    return;
  }
  root.replaceChildren(...state.schedules.map((schedule) => {
    const card = text("article", "", "panel");
    card.append(text("h3", schedule.name));
    const reportDelay = Number(schedule.notification_delay_seconds || 0) / 60;
    card.append(text("p", `${schedule.daily_times} · ${schedule.timezone} · ${schedule.rounds} 轮 · 开始后 ${reportDelay} 分钟发报告`));
    const names = schedule.channel_ids.map((id) => state.channels.find((item) => item.id === id)?.name || `#${id}`);
    card.append(text("p", `渠道：${names.join("、")} · ${schedule.enabled ? "已启用" : "已停用"}`));
    const speedRule = schedule.speed_threshold_mode === "adaptive"
      ? `P95 > 历史中位数 × ${schedule.speed_slow_ratio}（${schedule.speed_baseline_min_runs} 批后生效）`
      : schedule.speed_threshold_mode === "off" ? "不判定速度" : `P95 ≤ ${schedule.max_p95_ms} ms`;
    card.append(text("p", `成功率 ≥ ${percent(schedule.min_success_rate)} · 超时率 ≤ ${percent(schedule.max_timeout_rate)} · ${speedRule}`));
    const actions = text("div", "", "actions");
    actions.append(action("立即运行", () => runNow(schedule)));
    actions.append(action("编辑", () => openSchedule(schedule)));
    actions.append(action("删除", () => deleteSchedule(schedule), true));
    card.append(actions);
    return card;
  }));
}

function renderChannelPicker(selected = []) {
  const root = $("#channel-picker");
  root.replaceChildren(...state.channels.map((channel) => {
    const label = text("label", "");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = channel.id;
    input.checked = selected.includes(channel.id);
    label.append(input, document.createTextNode(`${channel.name} · ${channel.model}`));
    return label;
  }));
}

function openSchedule(schedule = null) {
  $("#schedule-form").reset();
  $("#schedule-id").value = schedule?.id || "";
  $("#schedule-title").textContent = schedule ? "编辑计划" : "新增计划";
  $("#schedule-name").value = schedule?.name || "";
  $("#schedule-times").value = schedule?.daily_times || "10:30,14:00,18:00";
  $("#schedule-timezone").value = schedule?.timezone || "Asia/Shanghai";
  $("#schedule-rounds").value = schedule?.rounds ?? 3;
  $("#schedule-interval").value = schedule?.round_interval_seconds ?? 15;
  $("#schedule-notify-delay").value = Number(schedule?.notification_delay_seconds || 0) / 60;
  $("#schedule-concurrency").value = schedule?.max_concurrency ?? 3;
  $("#schedule-success").value = schedule?.min_success_rate ?? 0.95;
  $("#schedule-timeout").value = schedule?.max_timeout_rate ?? 0.05;
  $("#schedule-break").value = schedule?.max_stream_break_rate ?? 0;
  $("#schedule-speed-mode").value = schedule?.speed_threshold_mode || "adaptive";
  $("#schedule-baseline-runs").value = schedule?.speed_baseline_min_runs ?? 5;
  $("#schedule-slow-ratio").value = schedule?.speed_slow_ratio ?? 1.5;
  $("#schedule-p95").value = schedule?.max_p95_ms ?? 30000;
  $("#schedule-enabled").checked = schedule ? Boolean(schedule.enabled) : true;
  renderChannelPicker(schedule?.channel_ids || []);
  $("#schedule-error").textContent = "";
  $("#schedule-dialog").showModal();
}

async function saveSchedule(event) {
  event.preventDefault();
  const payload = {
    id: Number($("#schedule-id").value) || null,
    name: $("#schedule-name").value.trim(),
    daily_times: $("#schedule-times").value.trim(),
    timezone: $("#schedule-timezone").value.trim(),
    channel_ids: $$("#channel-picker input:checked").map((item) => Number(item.value)),
    rounds: Number($("#schedule-rounds").value),
    round_interval_seconds: Number($("#schedule-interval").value),
    notification_delay_seconds: Math.round(Number($("#schedule-notify-delay").value) * 60),
    max_concurrency: Number($("#schedule-concurrency").value),
    min_success_rate: Number($("#schedule-success").value),
    max_timeout_rate: Number($("#schedule-timeout").value),
    max_stream_break_rate: Number($("#schedule-break").value),
    speed_threshold_mode: $("#schedule-speed-mode").value,
    speed_baseline_min_runs: Number($("#schedule-baseline-runs").value),
    speed_slow_ratio: Number($("#schedule-slow-ratio").value),
    max_p95_ms: Number($("#schedule-p95").value),
    enabled: $("#schedule-enabled").checked,
  };
  try {
    await api("/api/schedules", { method: "POST", body: JSON.stringify(payload) });
    $("#schedule-dialog").close();
    await loadSchedules();
    toast("计划已保存");
  } catch (error) { $("#schedule-error").textContent = error.message; }
}

async function runNow(schedule) {
  try {
    const data = await api(`/api/schedules/${schedule.id}/run`, { method: "POST" });
    toast(`任务 #${data.run_id} 已排队`);
    document.querySelector('[data-view="overview"]').click();
    await loadRuns();
  } catch (error) { toast(error.message); }
}

async function deleteSchedule(schedule) {
  if (!confirm(`删除计划“${schedule.name}”？历史记录会保留。`)) return;
  await api(`/api/schedules/${schedule.id}`, { method: "DELETE" });
  await loadSchedules();
}

async function loadRuns() {
  state.runs = (await api("/api/runs")).runs;
  renderRuns();
}

function runBadge(run) {
  if (["pending", "running"].includes(run.status)) return [run.status === "pending" ? "等待" : "运行中", "running"];
  if (run.status !== "completed" || run.summary?.verdict === "fail") return ["异常", "fail"];
  return ["通过", ""];
}

function renderRuns() {
  const root = $("#run-list");
  const completedRuns = state.runs.filter((item) => item.status === "completed");
  const passed = completedRuns.filter((item) => item.summary?.verdict === "pass").length;
  const latest = completedRuns[0]?.summary || {};
  const stats = [
    ["运行总数", state.runs.length],
    ["通过批次", passed],
    ["最近成功率", percent(latest.pass_rate)],
    ["最近 P95", Number.isFinite(latest.p95_latency_ms) ? `${latest.p95_latency_ms} ms` : "-"],
  ];
  $("#summary-stats").replaceChildren(...stats.map(([label, value]) => {
    const node = text("div", "", "stat"); node.append(text("small", label), text("strong", String(value))); return node;
  }));
  if (!state.runs.length) { root.replaceChildren(text("p", "还没有运行记录。", "empty")); return; }
  root.replaceChildren(...state.runs.map((run) => {
    const [label, style] = runBadge(run);
    const node = text("article", "", "record");
    const title = text("div", ""); title.append(text("h3", run.schedule_name), text("p", `${run.source === "manual" ? "手动" : "定时"} · ${formatDate(run.scheduled_for)}`));
    const rates = text("div", ""); rates.append(text("p", "成功率"), text("strong", percent(run.summary?.pass_rate)));
    const latency = text("div", ""); latency.append(text("p", "P95"), text("strong", Number.isFinite(run.summary?.p95_latency_ms) ? `${run.summary.p95_latency_ms} ms` : "-"));
    const end = text("div", ""); end.append(text("span", label, `badge ${style}`), action("查看", () => showRun(run.id)));
    node.append(title, rates, latency, end);
    return node;
  }));
}

async function showRun(id) {
  const run = await api(`/api/runs/${id}`);
  const root = $("#run-detail");
  const summary = run.summary || {};
  root.replaceChildren(text("p", `${run.schedule_name} · ${formatDate(run.scheduled_for)} · ${run.status}`));
  if (summary.reasons?.length) root.append(text("p", `结论：${summary.reasons.join("、")}`, "form-error"));
  const table = document.createElement("table"); table.className = "detail-table";
  const head = document.createElement("thead"); const headRow = document.createElement("tr");
  ["渠道", "轮", "测试", "状态", "耗时", "首包", "模型证据", "Usage"].forEach((item) => headRow.append(text("th", item))); head.append(headRow);
  const body = document.createElement("tbody");
  for (const item of run.results) {
    const row = document.createElement("tr");
    const actualModel = item.actual_model || "-";
    const modelEvidence = item.model_mismatch ? `不一致：${actualModel}` : actualModel;
    [item.channel_name, item.round_number, item.probe_id, item.status, item.latency_ms == null ? "-" : `${item.latency_ms} ms`, item.ttft_ms == null ? "-" : `${item.ttft_ms} ms`, modelEvidence, item.usage_complete ? "完整" : "缺失"].forEach((value) => row.append(text("td", String(value))));
    body.append(row);
  }
  table.append(head, body); root.append(table); $("#run-dialog").showModal();
}

async function loadFeishu() {
  const data = await api("/api/settings/feishu");
  $("#feishu-state").textContent = data.configured ? "已配置。留空保存可清除。" : "未配置时，测试结果只保存在本地。";
}

async function loadReportGroups() {
  state.reportGroups = (await api("/api/settings/report-groups")).groups;
  const root = $("#report-group-list");
  if (!state.reportGroups.length) {
    root.replaceChildren(text("p", "还没有报告分组。", "empty"));
    return;
  }
  root.replaceChildren(...state.reportGroups.map((group) => {
    const card = text("article", "", "panel");
    card.append(text("h3", `${group.family} · ${group.label}`));
    card.append(text("p", group.always_normal
      ? "免测 · 固定显示正常"
      : `公共渠道 ID：${group.registry_channel_ids.join("、")}`));
    return card;
  }));
}

async function saveFeishu(event) {
  event.preventDefault();
  try {
    await api("/api/settings/feishu", { method: "PUT", body: JSON.stringify({ webhook: $("#feishu-webhook").value.trim() }) });
    $("#feishu-webhook").value = ""; await loadFeishu(); toast("通知设置已保存");
  } catch (error) { toast(error.message); }
}

async function boot() {
  $$(".tab").forEach((button) => button.addEventListener("click", () => {
    $$(".tab").forEach((item) => item.classList.toggle("active", item === button));
    $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${button.dataset.view}`));
  }));
  $$('[data-close]').forEach((button) => button.addEventListener("click", () => $(`#${button.dataset.close}`).close()));
  $("#new-channel").addEventListener("click", () => openChannel());
  $("#new-schedule").addEventListener("click", () => openSchedule());
  $("#channel-form").addEventListener("submit", saveChannel);
  $("#schedule-form").addEventListener("submit", saveSchedule);
  $("#refresh-runs").addEventListener("click", loadRuns);
  $("#feishu-form").addEventListener("submit", saveFeishu);
  $("#test-feishu").addEventListener("click", async () => { try { await api("/api/settings/feishu/test", { method: "POST" }); toast("测试通知已发送"); } catch (error) { toast(error.message); } });
  await loadHealth();
  await Promise.all([loadInventory(), loadChannels()]);
  await Promise.all([loadSchedules(), loadRuns(), loadFeishu(), loadReportGroups()]);
  setInterval(() => { loadHealth(); loadRuns(); }, 10000);
}

boot().catch((error) => toast(error.message));
