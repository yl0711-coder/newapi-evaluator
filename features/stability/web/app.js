const state = {
  inventory: [], channels: [], candidates: [], baselines: [], schedules: [], runs: [], reportGroups: [],
  maxConcurrentProbes: 2,
};
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
    const configuredLimit = Number(data.scheduler.max_concurrent_probes);
    if (Number.isInteger(configuredLimit) && configuredLimit > 0) {
      state.maxConcurrentProbes = configuredLimit;
      $("#probe-concurrency-hint").textContent = `实际请求全局最多同时执行 ${configuredLimit} 个`;
    }
    $("#health-card").classList.toggle("bad", !okay);
    $("#health-label").textContent = okay ? "服务正常" : "服务降级";
    $("#health-detail").textContent = `${data.scheduler.active_runs} 个任务运行中 · 请求最多 ${state.maxConcurrentProbes} 并发`;
    $("#layered-executor-status").textContent = data.layered_executor?.enabled
      ? "分层巡检执行已启用；按已保存计划的有限采样运行。"
      : "分层巡检执行默认关闭；可保存计划和查看历史。管理员显式设置 EVAL_INTEGRITY_EXECUTOR=live 后才执行。";
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
    if (schedule.plan_version === "layered-integrity-v1") {
      const config = schedule.layered_config;
      const n = config.registry_channel_ids.length;
      card.append(text("p", `分层 v1 · ${n} 渠道 · 工作日 ${n * 7 + 195} / 非工作日 ${n * 7} 次上限 · Canary 192 独立能力项 · 无每日费用上限`));
      card.append(text("p", `探活 ${config.health_model}/${config.health_protocol} · 指纹 Astra 与 Sol · 未校准 · metadata 缺证`));
      card.append(text("p", `${schedule.timezone} · 分层请求在途最多 1 · 零补采 · 新报告不发送通知`));
    } else {
      const reportDelay = Number(schedule.notification_delay_seconds || 0) / 60;
      card.append(text("p", `${schedule.daily_times} · ${schedule.timezone} · ${schedule.rounds} 轮 · 请求全局最多 ${state.maxConcurrentProbes} 并发 · 开始后 ${reportDelay} 分钟发报告`));
      const speedRule = schedule.speed_threshold_mode === "adaptive"
        ? `P95 > 历史中位数 × ${schedule.speed_slow_ratio}（${schedule.speed_baseline_min_runs} 批后生效）`
        : schedule.speed_threshold_mode === "off" ? "不判定速度" : `P95 ≤ ${schedule.max_p95_ms} ms`;
      card.append(text("p", `成功率 ≥ ${percent(schedule.min_success_rate)} · 超时率 ≤ ${percent(schedule.max_timeout_rate)} · ${speedRule}`));
    }
    const names = schedule.channel_ids.map((id) => state.channels.find((item) => item.id === id)?.name || `#${id}`);
    card.append(text("p", `渠道：${names.join("、")} · ${schedule.enabled ? "已启用" : "已停用"}`));
    const actions = text("div", "", "actions");
    actions.append(action("立即运行", () => runNow(schedule)));
    actions.append(action("编辑", () => openSchedule(schedule)));
    actions.append(action("删除", () => deleteSchedule(schedule), true));
    card.append(actions);
    return card;
  }));
}

function renderLegacyPicker(selected = []) {
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

function renderChannelPicker(selected = [], registrySelected = []) {
  const layered = $("#schedule-pack").value === "layered-integrity-v1";
  $("#layered-fields").hidden = $("#layered-budget-hint").hidden = !layered;
  for (const id of ["times","rounds","interval","notify-delay","concurrency","success","timeout","break","speed-mode","baseline-runs","slow-ratio","p95"]) $(`#schedule-${id}`).closest("label").hidden = layered;
  if (!layered) { renderLegacyPicker(selected); return; }
  const root = $("#channel-picker");
  root.replaceChildren(...state.candidates.map(channel => {
    const row = text("article", "", "panel");
    const label = text("label", "");
    const input = document.createElement("input"); input.type = "checkbox";
    input.dataset.registry = channel.registry_channel_id;
    input.checked = registrySelected.includes(channel.registry_channel_id);
    input.setAttribute("aria-label", `选择公共渠道 ${channel.name}`);
    const reason = !channel.enabled ? "registry_disabled" : channel.credential_status !== "available" ? `credential_${channel.credential_status}` : "";
    input.disabled = Boolean(reason);
    label.append(input, document.createTextNode(`${channel.name} · Registry #${channel.registry_channel_id} · ${channel.status} · 凭据 ${channel.credential_status} · ${channel.discovery_status}`));
    row.append(label);
    let eligible = !reason;
    for (const model of ["gpt-6-astra", "gpt-6.1-sol"]) {
      const select = document.createElement("select"); select.dataset.model = model; select.setAttribute("aria-label", `${channel.name} ${model} 模型与协议`);
      const choices = channel.models.filter(item => item.catalog_model === model);
      select.replaceChildren(...choices.map(item => new Option(`${item.model} / ${item.protocol}${item.reason ? " · " + item.reason : ""}`, JSON.stringify(item))));
      if (!choices.some(item => item.eligible)) eligible = false;
      select.disabled = !choices.length || choices.every(item => !item.eligible);
      row.append(text("span", model), select);
    }
    input.disabled = !eligible;
    if (!eligible) row.append(text("p", `不可执行：${reason || channel.models.filter(m => ["gpt-6-astra","gpt-6.1-sol"].includes(m.catalog_model)).map(m => m.reason).filter(Boolean).join(" / ") || "模型未登记"}；请核对公共渠道的已上线状态、启用、凭据及模型/协议映射`, "form-error"));
    return row;
  }));
  if (!state.candidates.length) root.append(text("p", "公共库暂无候选渠道。先在公共渠道页新增，再刷新。"));
}

function renderLayeredRun(root, run) {
  const summary = run.summary || {};
  root.append(text("p", `计划 ${summary.plan_version || "layered-integrity-v1"} · planned ${summary.planned ?? "-"} · attempted ${summary.attempted ?? 0} · valid ${summary.valid ?? 0} · invalid ${summary.invalid ?? 0} · unknown ${summary.unknown ?? 0} · skipped/not_run ${summary.not_run ?? 0}`));
  root.append(text("p", "普通 API 未校准；metadata 缺证；能力仅覆盖轮转 Astra。探活成功只覆盖所测模型/协议。"));
  root.append(text("p", `费用为配置价格估算，日账本：${JSON.stringify(summary.daily_usage || {})}`));
  const controls = text("div", "", "actions");
  if (["pending","running"].includes(run.status)) controls.append(action("取消", async () => { await api(`/api/runs/${run.id}/cancel`, {method:"POST"}); await showRun(run.id); }));
  if (["cancelled","incomplete","failed"].includes(run.status)) controls.append(action("安全恢复", async () => { try { await api(`/api/runs/${run.id}/resume`, {method:"POST"}); await showRun(run.id); } catch (error) { toast(error.message); } }));
  root.append(controls);
  for (const slot of run.slots || []) {
    const card = text("article", "", "panel"); const evidence = slot.summary || {}, score = evidence.score || {};
    card.append(text("h3", `${slot.slot} · Registry #${slot.registry_channel_id ?? "待轮转"} · ${slot.model}/${slot.protocol}`));
    card.append(text("p", `费用估算 USD ${evidence.fees?.estimated_usd ?? "unknown"} · reasoning 用量见导出；不设每日金额上限`));
    card.append(text("p", `${slot.status}${slot.reason ? " · " + slot.reason : ""} · 计划 ${evidence.planned ?? (slot.method === "canary" ? 192 : slot.method === "modeltrace" ? 3 : 1)} · 尝试 ${evidence.attempted ?? 0} · 有效 ${evidence.valid ?? 0} · 无效 ${evidence.invalid ?? 0} · unknown ${evidence.unknown ?? 0} · not_run ${evidence.not_run ?? "未测"}`));
    if (slot.method === "canary") {
      card.append(text("p", `当前成绩 ${percent(score.score)}（${score.correct ?? 0}/${score.total ?? 192}）· ${score.status || "incomplete"} · baseline ${evidence.baseline_id ?? "未选定，能力未评估"}`));
      if (score.comparison) card.append(text("p", JSON.stringify(score.comparison)));
      if (slot.status === "completed" && evidence.attempted === 192) card.append(action("显式锁定可信 baseline", async () => {
        const label = prompt("为这份完整同条件结果填写 baseline 名称（不会自动提升当前结果）", `Run ${run.id}`);
        if (!label) return;
        try { const result = await api("/api/baselines", {method:"POST", body:JSON.stringify({slot_key:slot.slot_key,label})}); toast(`baseline #${result.id} 已锁定；编辑计划可显式选择`); } catch (error) { toast(error.message); }
      }));
    } else if (score.source_verdict) card.append(text("p", `行为线索 ${score.source_verdict} · ${score.prediction || "证据不足"} · calibration unvalidated`));
    if (evidence.review_suggestion) card.append(text("p", `${evidence.incident_id} · ${evidence.review_suggestion}`));
    if (slot.registry_channel_id && slot.method !== "health") {
      for (const strategy of ["hlwy","kbf"]) {
        const link = text("a", `人工 ${strategy.toUpperCase()} 复核`);
        link.href = "/integrity/?" + new URLSearchParams({source_ref:`stability-run:${run.id}`, strategy, registry_channel_id:slot.registry_channel_id, model:slot.model, protocol:slot.protocol}); card.append(link, document.createTextNode(" "));
      }
    }
    root.append(card);
  }
}

async function openSchedule(schedule = null) {
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
  const [candidateData, baselineData] = await Promise.all([api("/api/candidates"), api("/api/baselines")]);
  state.candidates = candidateData.candidates; state.baselines = baselineData.baselines;
  $("#schedule-pack").value = schedule?.plan_version || "layered-integrity-v1";
  const config = schedule?.layered_config || {};
  for (const [id, key, fallback] of [["ttl","health_ttl_minutes",60],["health-times","health_times",["09:30","12:30","15:30","18:00"]],["astra-times","astra_times",["10:00","15:35"]],["sol-time","sol_time","15:50"],["mt-time","modeltrace_time","16:10"],["canary-time","canary_time","18:15"],["day-deadline","day_deadline","17:55"],["canary-deadline","canary_deadline","08:55"],["health-model","health_model","gpt-6-astra"],["health-protocol","health_protocol","responses"]]) {
    const value = config[key] ?? fallback; $(`#layered-${id}`).value = Array.isArray(value) ? value.join(",") : value;
  }
  $("#layered-baseline").replaceChildren(new Option("未选定，仅当前成绩", ""), ...state.baselines.map(b => new Option(`#${b.id} ${b.label} · ${percent(b.score.score)}`, b.id)));
  $("#layered-baseline").value = config.baseline_id || "";
  renderChannelPicker(schedule?.channel_ids || [], config.registry_channel_ids || []);
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
  payload.plan_version = $("#schedule-pack").value;
  if (payload.plan_version === "layered-integrity-v1") {
    payload.channel_ids = [];
    payload.layered_config = {
      health_ttl_minutes:Number($("#layered-ttl").value),
      health_times:$("#layered-health-times").value.split(",").map(x => x.trim()),
      astra_times:$("#layered-astra-times").value.split(",").map(x => x.trim()),
      sol_time:$("#layered-sol-time").value.trim(), modeltrace_time:$("#layered-mt-time").value.trim(),
      canary_time:$("#layered-canary-time").value.trim(), day_deadline:$("#layered-day-deadline").value.trim(),
      canary_deadline:$("#layered-canary-deadline").value.trim(),
      health_model:$("#layered-health-model").value.trim(), health_protocol:$("#layered-health-protocol").value,
      baseline_id:Number($("#layered-baseline").value) || null,
    };
    payload.targets = [];
    for (const input of $$("#channel-picker input[data-registry]:checked")) {
      const row = input.closest("article");
      for (const select of row.querySelectorAll("select[data-model]")) {
        const entry = JSON.parse(select.value);
        payload.targets.push({registry_channel_id:Number(input.dataset.registry), model:entry.model, protocol:entry.protocol, model_id:entry.model_id});
      }
      const channel = state.candidates.find(c => c.registry_channel_id === Number(input.dataset.registry));
      const health = channel.models.find(m => m.model === payload.layered_config.health_model && m.protocol === payload.layered_config.health_protocol);
      if (health && !payload.targets.some(t => t.registry_channel_id === channel.registry_channel_id && t.model === health.model && t.protocol === health.protocol)) {
        payload.targets.push({registry_channel_id:channel.registry_channel_id, model:health.model, protocol:health.protocol, model_id:health.model_id});
      }
    }
  }
  const submit = $("#schedule-form button[type=submit]"); submit.disabled = true;
  try {
    await api("/api/schedules", { method: "POST", body: JSON.stringify(payload) });
    $("#schedule-dialog").close();
    await loadSchedules();
    toast("计划已保存");
  } catch (error) { $("#schedule-error").textContent = error.message; } finally { submit.disabled = false; }
}

async function runNow(schedule) {
  try {
    const data = await api(`/api/schedules/${schedule.id}/run`, { method: "POST" });
    toast(`任务 #${data.run_id} 已排队${data.scope ? "，执行当日时刻与依赖，不额外补量" : ""}`);
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
  const exportLink = text("a", "导出 JSON"); exportLink.href = `./api/runs/${id}/export`; root.append(exportLink);
  if (run.snapshot?.plan_version === "layered-integrity-v1") {
    renderLayeredRun(root, run); $("#run-dialog").showModal(); return;
  }
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
  $("#new-schedule").addEventListener("click", () => openSchedule().catch(error => toast(error.message)));
  $("#schedule-pack").addEventListener("change", () => renderChannelPicker());
  $("#channel-form").addEventListener("submit", saveChannel);
  $("#schedule-form").addEventListener("submit", saveSchedule);
  $("#refresh-runs").addEventListener("click", loadRuns);
  $("#feishu-form").addEventListener("submit", saveFeishu);
  $("#test-feishu").addEventListener("click", async () => { try { await api("/api/settings/feishu/test", { method: "POST" }); toast("测试通知已发送"); } catch (error) { toast(error.message); } });
  await loadHealth();
  await Promise.all([loadInventory(), loadChannels()]);
  await Promise.all([loadSchedules(), loadRuns(), loadFeishu(), loadReportGroups()]);
  const reportId = new URLSearchParams(location.search).get("run");
  if (reportId && /^\d+$/.test(reportId)) await showRun(Number(reportId));
  setInterval(() => { loadHealth(); loadRuns(); }, 10000);
}

boot().catch((error) => toast(error.message));
