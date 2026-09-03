const form = document.querySelector("#compare-form");
const presetSelect = document.querySelector("#preset");
const protocolHelp = document.querySelector("#protocol-help");
const startButton = document.querySelector("#start-button");
const startButtonLabel = startButton.querySelector(".button-label");
const stopButton = document.querySelector("#stop-button");
const formError = document.querySelector("#form-error");
const runStatus = document.querySelector("#run-status");
const completedCount = document.querySelector("#completed-count");
const totalQuestionRuns = document.querySelector("#total-question-runs");
const summaryTtft = document.querySelector("#summary-ttft");
const summaryTtftP95 = document.querySelector("#summary-ttft-p95");
const summaryTotal = document.querySelector("#summary-total");
const summaryTokenSpeed = document.querySelector("#summary-token-speed");
const summarySuccess = document.querySelector("#summary-success");
const roundsSelect = document.querySelector("#rounds");
const exportJsonButton = document.querySelector("#export-json");
const exportHtmlButton = document.querySelector("#export-html");
const reportHistory = document.querySelector("#report-history");
const reportDialog = document.querySelector("#report-dialog");
const closeReportButton = document.querySelector("#close-report");
const reportSubtitle = document.querySelector("#report-subtitle");
const reportContent = document.querySelector("#report-content");
const dialogExportJsonButton = document.querySelector("#dialog-export-json");
const dialogExportHtmlButton = document.querySelector("#dialog-export-html");
const questionList = document.querySelector("#question-list");
const questionTemplate = document.querySelector("#question-template");
const channelImport = document.querySelector("#channel-import");
const extractChannelButton = document.querySelector("#extract-channel");
const extractStatus = document.querySelector("#extract-status");
const referenceSource = document.querySelector("#reference-source");
const referenceKindLabel = document.querySelector("#reference-kind-label");
const referenceHelp = document.querySelector("#reference-help");
const officialKeyTools = document.querySelector("#official-key-tools");
const saveOfficialKeyButton = document.querySelector("#save-official-key");
const clearOfficialKeyButton = document.querySelector("#clear-official-key");
const officialKeyStatus = document.querySelector("#official-key-status");
const manageOnlineChannelsButton = document.querySelector("#manage-online-channels");
const onlineChannelDialog = document.querySelector("#online-channel-dialog");
const closeOnlineChannelsButton = document.querySelector("#close-online-channels");
const onlineChannelList = document.querySelector("#online-channel-list");
const onlineChannelCount = document.querySelector("#online-channel-count");
const onlineChannelForm = document.querySelector("#online-channel-form");
const onlineChannelEditorTitle = document.querySelector("#channel-editor-title");
const cancelChannelEditButton = document.querySelector("#cancel-channel-edit");
const saveOnlineChannelButton = document.querySelector("#save-online-channel");
const onlineChannelFormError = document.querySelector("#online-channel-form-error");
const onlineChannelImport = document.querySelector("#online-channel-import");
const extractOnlineChannelButton = document.querySelector("#extract-online-channel");
const onlineChannelExtractStatus = document.querySelector("#online-channel-extract-status");

const OFFICIAL_KEYS_STORAGE_KEY = "domestic-speed-bench.official-keys.v1";
const ONLINE_CHANNELS_STORAGE_KEY = "domestic-speed-bench.online-channels.v1";
const REPORTS_STORAGE_KEY = "domestic-speed-bench.reports.v1";
const MAX_REPORTS = 12;
const MAX_SAVED_OUTPUT_CHARS = 8000;

const fields = {
  channelBaseUrl: document.querySelector("#channel-base-url"),
  channelApiKey: document.querySelector("#channel-api-key"),
  channelModel: document.querySelector("#channel-model"),
  referenceBaseUrl: document.querySelector("#reference-base-url"),
  referenceApiKey: document.querySelector("#reference-api-key"),
  referenceModel: document.querySelector("#reference-model"),
};

const onlineChannelFields = {
  name: document.querySelector("#online-channel-name"),
  baseUrl: document.querySelector("#online-channel-base-url"),
  apiKey: document.querySelector("#online-channel-api-key"),
};

let presets = [];
let questions = [];
let controller = null;
let resultNodes = new Map();
let results = new Map();
let outputs = new Map();
let completedQuestions = 0;
let activeRound = 1;
let activeRounds = 1;
let currentReport = null;
let viewedReport = null;
let activeProvider = "";
let onlineChannels = [];
let editingOnlineChannelId = "";
const officialKeyDrafts = {};

function formatDuration(ms) {
  if (!Number.isFinite(ms)) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

function percentile(values, percentage) {
  const sorted = values.filter(Number.isFinite).sort((left, right) => left - right);
  if (!sorted.length) return null;
  return sorted[Math.max(0, Math.ceil((percentage / 100) * sorted.length) - 1)];
}

function resultKey(round, questionId, side) {
  return `${round}:${questionId}:${side}`;
}

function summarizeSide(side) {
  const successful = [...results.values()].filter((result) => result.side === side && result.ok);
  return {
    completed: successful.length,
    ttft_p50_ms: percentile(successful.map((result) => result.ttft_ms), 50),
    ttft_p95_ms: percentile(successful.map((result) => result.ttft_ms), 95),
    total_p50_ms: percentile(successful.map((result) => result.total_ms), 50),
    total_p95_ms: percentile(successful.map((result) => result.total_ms), 95),
    tokens_per_second_p50: percentile(successful.map((result) => result.tokens_per_second), 50),
    tokens_per_second_p95: percentile(successful.map((result) => result.tokens_per_second), 95),
  };
}

function currentSummary() {
  return {
    candidate: summarizeSide("candidate"),
    reference: summarizeSide("reference"),
    complete_responses: [...results.values()].filter((result) => result.ok).length,
    expected_responses: questions.length * activeRounds * 2,
  };
}

function safePublicUrl(value) {
  try {
    const parsed = new URL(value);
    parsed.username = "";
    parsed.password = "";
    parsed.search = "";
    parsed.hash = "";
    return parsed.toString().replace(/\/$/, "");
  } catch {
    return value.split(/[?#]/, 1)[0];
  }
}

function readReports() {
  try {
    const parsed = JSON.parse(localStorage.getItem(REPORTS_STORAGE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((report) => report?.version === 1) : [];
  } catch {
    return [];
  }
}

function buildSafeReport() {
  const preset = currentPreset();
  const referenceChannel = selectedOnlineChannel();
  const measurements = [...results.values()].map((result) => ({
    round: result.round,
    question_id: result.question_id,
    side: result.side,
    ok: result.ok,
    status: result.status,
    ttft_ms: result.ttft_ms ?? null,
    first_answer_ms: result.first_answer_ms ?? null,
    total_ms: result.total_ms ?? null,
    output_tokens: result.output_tokens ?? null,
    tokens_per_second: result.tokens_per_second ?? null,
    chars_per_second: result.chars_per_second ?? null,
    finish_reason: result.finish_reason || "",
    response_format: result.response_format || "",
  }));
  const responses = [];
  for (const question of questions) {
    for (const side of ["candidate", "reference"]) {
      const output = outputs.get(resultKey(activeRounds, question.id, side));
      if (!output) continue;
      const content = output.content.slice(0, MAX_SAVED_OUTPUT_CHARS);
      const reasoning = output.reasoning.slice(0, MAX_SAVED_OUTPUT_CHARS);
      responses.push({
        question_id: question.id,
        question_title: question.title,
        side,
        content,
        reasoning,
        truncated_for_storage:
          output.content.length > MAX_SAVED_OUTPUT_CHARS || output.reasoning.length > MAX_SAVED_OUTPUT_CHARS,
      });
    }
  }
  return {
    version: 1,
    id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
    created_at: new Date().toISOString(),
    model: { id: preset.id, label: preset.label, provider: preset.provider, protocol: preset.protocol },
    rounds: activeRounds,
    candidate: {
      label: "渠道端",
      base_url: safePublicUrl(fields.channelBaseUrl.value.trim()),
      model: fields.channelModel.value.trim(),
      protocol: preset.protocol,
    },
    reference: {
      label: referenceChannel?.name || `${preset.provider} 官方端`,
      base_url: safePublicUrl(fields.referenceBaseUrl.value.trim()),
      model: fields.referenceModel.value.trim(),
      protocol: preset.protocol,
    },
    summary: currentSummary(),
    measurements,
    responses,
  };
}

function saveReport(report) {
  try {
    const nextReports = [report, ...readReports().filter((item) => item.id !== report.id)].slice(0, MAX_REPORTS);
    localStorage.setItem(REPORTS_STORAGE_KEY, JSON.stringify(nextReports));
    renderReportHistory();
    return true;
  } catch {
    return false;
  }
}

function renderQuestions() {
  questionList.replaceChildren();
  resultNodes = new Map();
  questions.forEach((question, index) => {
    const fragment = questionTemplate.content.cloneNode(true);
    const article = fragment.querySelector(".question");
    article.dataset.questionId = question.id;
    fragment.querySelector(".question-id").textContent = `${String(index + 1).padStart(2, "0")} · ${question.difficulty === "hard" ? "难" : "易"}`;
    fragment.querySelector(".question-title").textContent = question.title;
    fragment.querySelector(".question-prompt").textContent = question.prompt;
    for (const lane of fragment.querySelectorAll(".lane")) {
      const side = lane.dataset.side;
      resultNodes.set(`${question.id}:${side}`, lane);
    }
    questionList.append(fragment);
  });
}

function makeElement(tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function reportDate(report) {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(report.created_at),
  );
}

function createReportTable(headers, rows) {
  const wrapper = document.createElement("div");
  wrapper.className = "report-table-wrap";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  headRow.append(...headers.map((header) => makeElement("th", header)));
  head.append(headRow);
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tableRow = document.createElement("tr");
    tableRow.append(...row.map((value) => makeElement("td", String(value))));
    body.append(tableRow);
  }
  table.append(head, body);
  wrapper.append(table);
  return wrapper;
}

function showReport(report) {
  viewedReport = report;
  reportSubtitle.textContent = `${reportDate(report)} · ${report.model.label} · ${report.rounds} 轮`;
  const endpoints = document.createElement("section");
  endpoints.append(
    makeElement("h3", "对比端点"),
    makeElement("p", `渠道端：${report.candidate.base_url} · ${report.candidate.model}`),
    makeElement("p", `${report.reference.label}：${report.reference.base_url} · ${report.reference.model}`),
  );

  const summary = document.createElement("section");
  summary.append(
    makeElement("h3", "多轮统计"),
    createReportTable(
      ["端点", "完整响应", "首包 P50", "首包 P95", "总耗时 P50", "总耗时 P95", "Token/s P50"],
      [
        [
          "渠道端",
          report.summary.candidate.completed,
          formatDuration(report.summary.candidate.ttft_p50_ms),
          formatDuration(report.summary.candidate.ttft_p95_ms),
          formatDuration(report.summary.candidate.total_p50_ms),
          formatDuration(report.summary.candidate.total_p95_ms),
          formatRate(report.summary.candidate.tokens_per_second_p50),
        ],
        [
          report.reference.label,
          report.summary.reference.completed,
          formatDuration(report.summary.reference.ttft_p50_ms),
          formatDuration(report.summary.reference.ttft_p95_ms),
          formatDuration(report.summary.reference.total_p50_ms),
          formatDuration(report.summary.reference.total_p95_ms),
          formatRate(report.summary.reference.tokens_per_second_p50),
        ],
      ],
    ),
  );

  const measurements = document.createElement("section");
  measurements.append(
    makeElement("h3", "逐次测量"),
    createReportTable(
      ["轮", "题", "端点", "状态", "首包", "首答", "总耗时", "Token/s"],
      report.measurements.map((item) => [
        item.round,
        item.question_id,
        item.side === "candidate" ? "渠道" : "参照",
        item.status,
        formatDuration(item.ttft_ms),
        formatDuration(item.first_answer_ms),
        formatDuration(item.total_ms),
        formatRate(item.tokens_per_second),
      ]),
    ),
  );

  const responses = document.createElement("section");
  responses.append(makeElement("h3", "末轮回答"));
  for (const response of report.responses) {
    const details = document.createElement("details");
    const side = response.side === "candidate" ? "渠道端" : "参照端";
    const suffix = response.truncated_for_storage ? " · 历史存储已截短" : "";
    details.append(makeElement("summary", `${response.question_title} · ${side}${suffix}`));
    const output = makeElement("pre", [response.reasoning, response.content].filter(Boolean).join("\n\n"), "report-output");
    details.append(output);
    responses.append(details);
  }
  reportContent.replaceChildren(endpoints, summary, measurements, responses);
  reportDialog.showModal();
}

function formatRate(value) {
  return Number.isFinite(value) ? value.toFixed(1) : "—";
}

function renderReportHistory() {
  const reports = readReports();
  if (!reports.length) {
    reportHistory.replaceChildren(makeElement("p", "完成一次测试后，脱敏报告会显示在这里。", "empty-state"));
    return;
  }
  const cards = reports.map((report) => {
    const card = document.createElement("article");
    card.className = "report-card";
    const copy = document.createElement("div");
    copy.append(
      makeElement("h4", `${report.model.label} · ${report.rounds} 轮`),
      makeElement(
        "p",
        `${reportDate(report)} · 完整响应 ${report.summary.complete_responses}/${report.summary.expected_responses}`,
      ),
    );
    const actions = document.createElement("div");
    actions.className = "report-card-actions";
    actions.append(
      createChannelAction("查看", "button button-secondary button-compact", () => showReport(report)),
      createChannelAction("HTML", "text-button", () => downloadReport(report, "html")),
      createChannelAction("JSON", "text-button", () => downloadReport(report, "json")),
      createChannelAction("删除", "text-button text-button-danger", () => deleteReport(report.id)),
    );
    card.append(copy, actions);
    return card;
  });
  reportHistory.replaceChildren(...cards);
}

function deleteReport(id) {
  const report = readReports().find((item) => item.id === id);
  if (!report || !window.confirm(`删除 ${reportDate(report)} 的测试报告？`)) return;
  localStorage.setItem(REPORTS_STORAGE_KEY, JSON.stringify(readReports().filter((item) => item.id !== id)));
  if (currentReport?.id === id) {
    currentReport = null;
    exportJsonButton.disabled = true;
    exportHtmlButton.disabled = true;
  }
  renderReportHistory();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function reportToHtml(report) {
  const summaryRows = [
    ["渠道端", report.summary.candidate],
    [report.reference.label, report.summary.reference],
  ]
    .map(
      ([label, stats]) =>
        `<tr><td>${escapeHtml(label)}</td><td>${stats.completed}</td><td>${escapeHtml(formatDuration(stats.ttft_p50_ms))}</td><td>${escapeHtml(formatDuration(stats.ttft_p95_ms))}</td><td>${escapeHtml(formatDuration(stats.total_p50_ms))}</td><td>${escapeHtml(formatDuration(stats.total_p95_ms))}</td><td>${escapeHtml(formatRate(stats.tokens_per_second_p50))}</td></tr>`,
    )
    .join("");
  const measurementRows = report.measurements
    .map(
      (item) =>
        `<tr><td>${item.round}</td><td>${escapeHtml(item.question_id)}</td><td>${item.side === "candidate" ? "渠道" : "参照"}</td><td>${escapeHtml(item.status)}</td><td>${escapeHtml(formatDuration(item.ttft_ms))}</td><td>${escapeHtml(formatDuration(item.first_answer_ms))}</td><td>${escapeHtml(formatDuration(item.total_ms))}</td><td>${escapeHtml(formatRate(item.tokens_per_second))}</td></tr>`,
    )
    .join("");
  const responseBlocks = report.responses
    .map(
      (response) =>
        `<details><summary>${escapeHtml(response.question_title)} · ${response.side === "candidate" ? "渠道端" : "参照端"}</summary><pre>${escapeHtml([response.reasoning, response.content].filter(Boolean).join("\n\n"))}</pre></details>`,
    )
    .join("");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escapeHtml(report.model.label)} 测试报告</title></head><body><main><h1>${escapeHtml(report.model.label)} 测试报告</h1><p>${escapeHtml(reportDate(report))} · ${report.rounds} 轮 · 完整响应 ${report.summary.complete_responses}/${report.summary.expected_responses}</p><h2>对比端点</h2><p>渠道端：${escapeHtml(report.candidate.base_url)} · ${escapeHtml(report.candidate.model)}</p><p>${escapeHtml(report.reference.label)}：${escapeHtml(report.reference.base_url)} · ${escapeHtml(report.reference.model)}</p><h2>多轮统计</h2><table border="1" cellpadding="6"><thead><tr><th>端点</th><th>完整响应</th><th>首包 P50</th><th>首包 P95</th><th>总耗时 P50</th><th>总耗时 P95</th><th>Token/s P50</th></tr></thead><tbody>${summaryRows}</tbody></table><h2>逐次测量</h2><table border="1" cellpadding="6"><thead><tr><th>轮</th><th>题</th><th>端点</th><th>状态</th><th>首包</th><th>首答</th><th>总耗时</th><th>Token/s</th></tr></thead><tbody>${measurementRows}</tbody></table><h2>末轮回答</h2>${responseBlocks}</main></body></html>`;
}

function downloadReport(report, format) {
  const body = format === "json" ? JSON.stringify(report, null, 2) : reportToHtml(report);
  const type = format === "json" ? "application/json" : "text/html";
  const url = URL.createObjectURL(new Blob([body], { type: `${type};charset=utf-8` }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `model-speed-report-${report.created_at.slice(0, 19).replaceAll(":", "-")}.${format}`;
  link.click();
  URL.revokeObjectURL(url);
}

function currentPreset() {
  return presets.find((item) => item.id === presetSelect.value);
}

function readOfficialKeys() {
  try {
    const parsed = JSON.parse(localStorage.getItem(OFFICIAL_KEYS_STORAGE_KEY) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function readOnlineChannels() {
  try {
    const parsed = JSON.parse(localStorage.getItem(ONLINE_CHANNELS_STORAGE_KEY) || "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (item) =>
        item &&
        typeof item.id === "string" &&
        typeof item.name === "string" &&
        typeof item.base_url === "string" &&
        typeof item.api_key === "string",
    );
  } catch {
    return [];
  }
}

function writeOnlineChannels(channels) {
  localStorage.setItem(ONLINE_CHANNELS_STORAGE_KEY, JSON.stringify(channels));
}

function findOnlineChannel(id) {
  return onlineChannels.find((channel) => channel.id === id);
}

function selectedOnlineChannel() {
  if (!referenceSource.value.startsWith("channel:")) return null;
  return findOnlineChannel(referenceSource.value.slice("channel:".length)) || null;
}

function showStoredKeyStatus(provider, apiKey) {
  officialKeyStatus.textContent = apiKey ? `已载入 ${provider} Key ····${apiKey.slice(-4)}` : `${provider} 尚未保存 Key`;
}

function renderReferenceOptions(preferredValue = referenceSource.value || "official") {
  const preset = currentPreset();
  const hasOfficialReference = Boolean(preset?.official_base_url);
  const nodes = [];

  if (hasOfficialReference) {
    const officialOption = document.createElement("option");
    officialOption.value = "official";
    officialOption.textContent = `${preset.provider}官方接口`;
    nodes.push(officialOption);
  }

  if (onlineChannels.length) {
    const group = document.createElement("optgroup");
    group.label = "已上线渠道";
    for (const channel of onlineChannels) {
      const option = document.createElement("option");
      option.value = `channel:${channel.id}`;
      option.textContent = channel.name;
      group.append(option);
    }
    nodes.push(group);
  }

  if (!nodes.length) {
    const unavailableOption = document.createElement("option");
    unavailableOption.value = "unavailable";
    unavailableOption.textContent = "先配置已上线渠道";
    nodes.push(unavailableOption);
  }

  referenceSource.replaceChildren(...nodes);
  referenceSource.disabled = !hasOfficialReference && onlineChannels.length === 0;
  const hasPreferred =
    (preferredValue === "official" && hasOfficialReference) || Boolean(selectedOnlineChannelByValue(preferredValue));
  const defaultValue = hasOfficialReference ? "official" : onlineChannels.length ? `channel:${onlineChannels[0].id}` : "unavailable";
  referenceSource.value = hasPreferred ? preferredValue : defaultValue;
}

function selectedOnlineChannelByValue(value) {
  if (!value.startsWith("channel:")) return null;
  return findOnlineChannel(value.slice("channel:".length)) || null;
}

function applyReferenceSource() {
  const preset = currentPreset();
  if (!preset) return;
  const channel = selectedOnlineChannel();
  fields.referenceModel.value = preset.model;

  if (channel) {
    referenceKindLabel.textContent = "已上线渠道";
    referenceHelp.textContent = `${channel.name} 的连接信息已从当前浏览器载入。`;
    officialKeyTools.hidden = true;
    fields.referenceBaseUrl.value = channel.base_url;
    fields.referenceApiKey.value = channel.api_key;
    return;
  }

  if (preset.official_base_url) {
    referenceKindLabel.textContent = "官方接口";
    referenceHelp.textContent = "使用模型厂商官方接口；也可以改为任一已上线渠道。";
    officialKeyTools.hidden = false;
    fields.referenceBaseUrl.value = preset.official_base_url;
    const storedKey = readOfficialKeys()[preset.provider] || "";
    fields.referenceApiKey.value = officialKeyDrafts[preset.provider] ?? storedKey;
    showStoredKeyStatus(preset.provider, storedKey);
    return;
  }

  referenceKindLabel.textContent = "待配置";
  referenceHelp.textContent = "此家族没有预置官方端，请先配置并选择一个已上线渠道。";
  officialKeyTools.hidden = true;
  fields.referenceBaseUrl.value = "";
  fields.referenceApiKey.value = "";
}

function applyPreset() {
  const preset = currentPreset();
  if (!preset) return;
  if (activeProvider && referenceSource.value === "official") {
    officialKeyDrafts[activeProvider] = fields.referenceApiKey.value;
  }
  fields.channelModel.value = preset.model;
  protocolHelp.textContent =
    preset.protocol === "anthropic"
      ? "Claude 使用原生 Anthropic Messages 流式协议，两端模型名仍可单独修改。"
      : "当前模型使用 OpenAI-compatible Chat Completions 流式协议。";
  activeProvider = preset.provider;
  renderReferenceOptions(referenceSource.value || "official");
  applyReferenceSource();
}

async function extractCredentials({ textArea, button, status, baseUrl, apiKey }) {
  const text = textArea.value.trim();
  status.textContent = "";
  if (!text) {
    status.textContent = "请先粘贴渠道连接信息。";
    textArea.focus();
    return;
  }
  button.disabled = true;
  button.dataset.state = "loading";
  button.textContent = "正在提取";
  try {
    const response = await fetch("/api/extract-channel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const extracted = await response.json();
    if (!response.ok) throw new Error(extracted.detail || `HTTP ${response.status}`);
    if (extracted.base_url) baseUrl.value = extracted.base_url;
    if (extracted.api_key) apiKey.value = extracted.api_key;
    const found = [extracted.has_url && "URL", extracted.has_key && "Key"].filter(Boolean);
    if (!found.length) throw new Error("没有识别到 URL 或 Key，请检查粘贴内容。");
    button.dataset.state = "success";
    status.textContent = `已提取：${found.join("、")}`;
  } catch (error) {
    button.dataset.state = "error";
    status.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "提取 URL 与 Key";
  }
}

function extractChannelCredentials() {
  return extractCredentials({
    textArea: channelImport,
    button: extractChannelButton,
    status: extractStatus,
    baseUrl: fields.channelBaseUrl,
    apiKey: fields.channelApiKey,
  });
}

function extractOnlineChannelCredentials() {
  return extractCredentials({
    textArea: onlineChannelImport,
    button: extractOnlineChannelButton,
    status: onlineChannelExtractStatus,
    baseUrl: onlineChannelFields.baseUrl,
    apiKey: onlineChannelFields.apiKey,
  });
}

function saveOfficialKey() {
  const preset = currentPreset();
  const apiKey = fields.referenceApiKey.value.trim();
  if (!preset || !apiKey) {
    officialKeyStatus.textContent = "请先填写官方 Key。";
    fields.referenceApiKey.focus();
    return;
  }
  try {
    const storedKeys = readOfficialKeys();
    storedKeys[preset.provider] = apiKey;
    localStorage.setItem(OFFICIAL_KEYS_STORAGE_KEY, JSON.stringify(storedKeys));
    officialKeyDrafts[preset.provider] = apiKey;
    saveOfficialKeyButton.dataset.state = "success";
    showStoredKeyStatus(preset.provider, apiKey);
  } catch {
    saveOfficialKeyButton.dataset.state = "error";
    officialKeyStatus.textContent = "浏览器拒绝保存，请检查站点存储权限。";
  }
}

function clearOfficialKey() {
  const preset = currentPreset();
  if (!preset) return;
  try {
    const storedKeys = readOfficialKeys();
    delete storedKeys[preset.provider];
    localStorage.setItem(OFFICIAL_KEYS_STORAGE_KEY, JSON.stringify(storedKeys));
    delete saveOfficialKeyButton.dataset.state;
    officialKeyStatus.textContent = `已清除 ${preset.provider} 的保存；当前输入仍可用于本轮。`;
  } catch {
    officialKeyStatus.textContent = "浏览器拒绝清除，请检查站点存储权限。";
  }
}

function resetOnlineChannelEditor() {
  editingOnlineChannelId = "";
  onlineChannelForm.reset();
  onlineChannelEditorTitle.textContent = "添加渠道";
  cancelChannelEditButton.hidden = true;
  saveOnlineChannelButton.textContent = "保存渠道";
  delete saveOnlineChannelButton.dataset.state;
  onlineChannelFormError.hidden = true;
  onlineChannelExtractStatus.textContent = "";
}

function createChannelAction(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function renderOnlineChannelList() {
  onlineChannelCount.textContent = `${onlineChannels.length} 个`;
  if (!onlineChannels.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "还没有保存渠道。添加后即可直接选作参照端。";
    onlineChannelList.replaceChildren(empty);
    return;
  }

  const cards = onlineChannels.map((channel) => {
    const card = document.createElement("article");
    card.className = "channel-card";
    const content = document.createElement("div");
    const name = document.createElement("h4");
    name.textContent = channel.name;
    const url = document.createElement("p");
    url.textContent = channel.base_url;
    const key = document.createElement("code");
    key.textContent = `Key ····${channel.api_key.slice(-4)}`;
    content.append(name, url, key);

    const actions = document.createElement("div");
    actions.className = "channel-card-actions";
    actions.append(
      createChannelAction("设为参照", "button button-secondary button-compact", () => useOnlineChannel(channel.id)),
      createChannelAction("编辑", "text-button", () => editOnlineChannel(channel.id)),
      createChannelAction("删除", "text-button text-button-danger", () => deleteOnlineChannel(channel.id)),
    );
    card.append(content, actions);
    return card;
  });
  onlineChannelList.replaceChildren(...cards);
}

function useOnlineChannel(id) {
  const value = `channel:${id}`;
  renderReferenceOptions(value);
  applyReferenceSource();
  onlineChannelDialog.close();
}

function editOnlineChannel(id) {
  const channel = findOnlineChannel(id);
  if (!channel) return;
  editingOnlineChannelId = channel.id;
  onlineChannelFields.name.value = channel.name;
  onlineChannelFields.baseUrl.value = channel.base_url;
  onlineChannelFields.apiKey.value = channel.api_key;
  onlineChannelEditorTitle.textContent = "编辑渠道";
  cancelChannelEditButton.hidden = false;
  saveOnlineChannelButton.textContent = "保存修改";
  delete saveOnlineChannelButton.dataset.state;
  onlineChannelFormError.hidden = true;
  onlineChannelFields.name.focus();
}

function deleteOnlineChannel(id) {
  const channel = findOnlineChannel(id);
  if (!channel || !window.confirm(`删除“${channel.name}”？此操作会移除当前浏览器里的连接信息。`)) return;
  try {
    onlineChannels = onlineChannels.filter((item) => item.id !== id);
    writeOnlineChannels(onlineChannels);
  } catch {
    onlineChannelFormError.textContent = "浏览器拒绝删除，请检查站点存储权限。";
    onlineChannelFormError.hidden = false;
    return;
  }
  if (editingOnlineChannelId === id) resetOnlineChannelEditor();
  const preferred = referenceSource.value === `channel:${id}` ? "official" : referenceSource.value;
  renderOnlineChannelList();
  renderReferenceOptions(preferred);
  applyReferenceSource();
}

function saveOnlineChannel(event) {
  event.preventDefault();
  onlineChannelFormError.hidden = true;
  if (!onlineChannelForm.checkValidity()) {
    onlineChannelForm.reportValidity();
    onlineChannelFormError.textContent = "请把渠道名称、URL 和 Key 填写完整。";
    onlineChannelFormError.hidden = false;
    return;
  }

  const id = editingOnlineChannelId || (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
  const channel = {
    id,
    name: onlineChannelFields.name.value.trim(),
    base_url: onlineChannelFields.baseUrl.value.trim(),
    api_key: onlineChannelFields.apiKey.value.trim(),
  };
  const existingIndex = onlineChannels.findIndex((item) => item.id === id);
  const nextChannels = [...onlineChannels];
  if (existingIndex >= 0) nextChannels[existingIndex] = channel;
  else nextChannels.push(channel);

  try {
    writeOnlineChannels(nextChannels);
  } catch {
    onlineChannelFormError.textContent = "浏览器拒绝保存，请检查站点存储权限。";
    onlineChannelFormError.hidden = false;
    saveOnlineChannelButton.dataset.state = "error";
    return;
  }

  onlineChannels = nextChannels;
  const preferred = referenceSource.value;
  resetOnlineChannelEditor();
  renderOnlineChannelList();
  renderReferenceOptions(preferred);
  applyReferenceSource();
  saveOnlineChannelButton.dataset.state = "success";
  saveOnlineChannelButton.textContent = "已保存";
}

function setButtonState(state) {
  startButton.dataset.state = state;
  startButton.disabled = state === "loading" || presets.length === 0;
  if (state === "loading") startButtonLabel.textContent = "测试进行中";
  if (state === "success") startButtonLabel.textContent = "再测一次";
  if (state === "error") startButtonLabel.textContent = "修正后重试";
  if (state === "idle") startButtonLabel.textContent = "开始 5 题快测";
}

function resetRun() {
  completedQuestions = 0;
  activeRound = 1;
  activeRounds = Number(roundsSelect.value);
  completedCount.textContent = "0";
  totalQuestionRuns.textContent = `/ ${questions.length * activeRounds}`;
  summaryTtft.textContent = "—";
  summaryTtftP95.textContent = "—";
  summaryTotal.textContent = "—";
  summaryTokenSpeed.textContent = "—";
  summarySuccess.textContent = `0 / ${questions.length * activeRounds * 2}`;
  startButton.style.setProperty("--run-progress", "0");
  results = new Map();
  outputs = new Map();
  currentReport = null;
  exportJsonButton.disabled = true;
  exportHtmlButton.disabled = true;
  renderQuestions();
}

function setLaneRunning(questionId, side) {
  const lane = resultNodes.get(`${questionId}:${side}`);
  if (!lane) return;
  lane.dataset.state = "running";
  lane.querySelector(".lane-state").textContent = "接收中";
  for (const metric of lane.querySelectorAll("[data-metric]")) metric.textContent = "—";
  lane.querySelector(".reasoning-label").hidden = true;
  lane.querySelector(".reasoning-output").textContent = "";
  lane.querySelector(".answer-label").textContent = "回答";
  lane.querySelector(".answer-output").textContent = "";
  const error = lane.querySelector(".lane-error");
  error.textContent = "";
  error.hidden = true;
}

function updateSummary() {
  const summary = currentSummary();
  summaryTtft.textContent = formatMetricPair(
    summary.candidate.ttft_p50_ms,
    summary.reference.ttft_p50_ms,
    formatDuration,
  );
  summaryTtftP95.textContent = formatMetricPair(
    summary.candidate.ttft_p95_ms,
    summary.reference.ttft_p95_ms,
    formatDuration,
  );
  summaryTotal.textContent = formatMetricPair(
    summary.candidate.total_p50_ms,
    summary.reference.total_p50_ms,
    formatDuration,
  );
  summaryTokenSpeed.textContent = formatMetricPair(
    summary.candidate.tokens_per_second_p50,
    summary.reference.tokens_per_second_p50,
    formatRate,
  );
  summarySuccess.textContent = `${summary.complete_responses} / ${summary.expected_responses}`;
}

function formatMetricPair(candidate, reference, formatter) {
  if (!Number.isFinite(candidate) && !Number.isFinite(reference)) return "—";
  return `渠 ${formatter(candidate)} / 参 ${formatter(reference)}`;
}

function signedDifference(delta) {
  const rounded = Math.round(Math.abs(delta));
  if (Math.abs(delta) < 1) return "基本持平";
  return delta < 0 ? `渠道快 ${formatDuration(rounded)}` : `参照快 ${formatDuration(rounded)}`;
}

function updateVerdict(questionId, round) {
  const article = questionList.querySelector(`[data-question-id="${questionId}"]`);
  const candidate = results.get(resultKey(round, questionId, "candidate"));
  const reference = results.get(resultKey(round, questionId, "reference"));
  if (!article || !candidate || !reference) return;
  const verdict = article.querySelector(".question-verdict");
  article.dataset.state = "done";
  if (!candidate.ok || !reference.ok) {
    verdict.textContent = "未完成对比";
    return;
  }
  verdict.textContent = signedDifference(candidate.ttft_ms - reference.ttft_ms);
}

function handleEvent(event) {
  if (event.type === "run_started") {
    activeRounds = event.rounds;
    totalQuestionRuns.textContent = `/ ${event.total_question_runs}`;
    return;
  }

  if (event.type === "round_started") {
    activeRound = event.round;
    runStatus.textContent = event.rounds === 1 ? "正在执行 5 道题。" : `正在执行第 ${event.round} / ${event.rounds} 轮。`;
    return;
  }

  if (event.type === "question_started") {
    activeRound = event.round;
    const article = questionList.querySelector(`[data-question-id="${event.question_id}"]`);
    if (article) {
      article.dataset.state = "running";
      article.querySelector(".question-verdict").textContent = "进行中";
      article.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    setLaneRunning(event.question_id, "candidate");
    setLaneRunning(event.question_id, "reference");
    runStatus.textContent =
      event.rounds === 1
        ? `正在执行第 ${event.index} 题，两端同时请求。`
        : `第 ${event.round} / ${event.rounds} 轮 · 第 ${event.index} 题，两端同时请求。`;
    return;
  }

  if (event.type === "chunk") {
    const lane = resultNodes.get(`${event.question_id}:${event.side}`);
    if (!lane) return;
    const key = resultKey(event.round, event.question_id, event.side);
    const output = outputs.get(key) || { content: "", reasoning: "", raw: false };
    output.content += event.content || "";
    output.reasoning += event.reasoning || "";
    output.raw ||= Boolean(event.raw);
    outputs.set(key, output);
    if (event.raw) {
      lane.querySelector(".answer-label").textContent = "上游原始响应";
      lane.querySelector(".stream-details").open = true;
    }
    if (event.reasoning) {
      lane.querySelector(".reasoning-label").hidden = false;
      lane.querySelector(".reasoning-output").textContent += event.reasoning;
    }
    if (event.content) lane.querySelector(".answer-output").textContent += event.content;
    return;
  }

  if (event.type === "side_finished") {
    const lane = resultNodes.get(`${event.question_id}:${event.side}`);
    if (!lane) return;
    results.set(resultKey(event.round, event.question_id, event.side), event);
    lane.querySelector('[data-metric="ttft"]').textContent = formatDuration(event.ttft_ms);
    lane.querySelector('[data-metric="first-answer"]').textContent = formatDuration(event.first_answer_ms);
    lane.querySelector('[data-metric="total"]').textContent = formatDuration(event.total_ms);
    lane.querySelector('[data-metric="output-tokens"]').textContent = Number.isFinite(event.output_tokens)
      ? String(event.output_tokens)
      : "—";
    lane.querySelector('[data-metric="tokens-per-second"]').textContent = Number.isFinite(event.tokens_per_second)
      ? event.tokens_per_second.toFixed(1)
      : "—";
    lane.querySelector('[data-metric="characters-per-second"]').textContent = Number.isFinite(event.chars_per_second)
      ? event.chars_per_second.toFixed(1)
      : "—";
    if (event.ok) {
      lane.dataset.state = "done";
      lane.querySelector(".lane-state").textContent = "已完成";
    } else {
      lane.dataset.state = "error";
      const statusLabels = {
        truncated: "被截断",
        empty: "空响应",
        unrecognized: "协议不兼容",
        error: "失败",
      };
      lane.querySelector(".lane-state").textContent = statusLabels[event.status] || "失败";
      const error = lane.querySelector(".lane-error");
      error.textContent = event.error || "请求失败";
      error.hidden = false;
    }
    updateVerdict(event.question_id, event.round);
    updateSummary();
    return;
  }

  if (event.type === "question_finished") {
    completedQuestions = event.completed_question_runs;
    completedCount.textContent = String(completedQuestions);
    startButton.style.setProperty("--run-progress", String(completedQuestions / event.total_question_runs));
    return;
  }

  if (event.type === "run_finished") {
    currentReport = buildSafeReport();
    const saved = saveReport(currentReport);
    exportJsonButton.disabled = false;
    exportHtmlButton.disabled = false;
    runStatus.textContent = saved
      ? "测试完成，脱敏报告已保存到本机历史。"
      : "测试完成；浏览器未能保存历史，可直接导出当前报告。";
  }
}

async function readNdjson(response) {
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `HTTP ${response.status}`);
  }
  if (!response.body) throw new Error("浏览器未提供流式响应体。\n");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (line.trim()) handleEvent(JSON.parse(line));
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) handleEvent(JSON.parse(buffer));
}

function requestPayload() {
  const preset = currentPreset();
  return {
    candidate: {
      base_url: fields.channelBaseUrl.value.trim(),
      api_key: fields.channelApiKey.value,
      model: fields.channelModel.value.trim(),
      protocol: preset.protocol,
    },
    reference: {
      base_url: fields.referenceBaseUrl.value.trim(),
      api_key: fields.referenceApiKey.value,
      model: fields.referenceModel.value.trim(),
      protocol: preset.protocol,
    },
    rounds: Number(roundsSelect.value),
  };
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.hidden = true;
  if (!form.checkValidity()) {
    form.reportValidity();
    formError.textContent = "请把两端的地址、Key 和模型名填写完整。";
    formError.hidden = false;
    setButtonState("error");
    return;
  }

  resetRun();
  controller = new AbortController();
  setButtonState("loading");
  stopButton.hidden = false;
  runStatus.textContent = activeRounds === 1 ? "准备开始，两端将同时发出第一题。" : `准备开始 ${activeRounds} 轮测试。`;

  try {
    const response = await fetch("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload()),
      signal: controller.signal,
    });
    await readNdjson(response);
    setButtonState("success");
  } catch (error) {
    if (error.name === "AbortError") {
      runStatus.textContent = "已停止。本轮未完成的数据仍保留在页面上。";
      setButtonState("idle");
    } else {
      formError.textContent = `无法完成测试：${error.message}`;
      formError.hidden = false;
      runStatus.textContent = "本轮中断，请检查配置后重试。";
      setButtonState("error");
    }
  } finally {
    controller = null;
    stopButton.hidden = true;
  }
});

stopButton.addEventListener("click", () => controller?.abort());
roundsSelect.addEventListener("change", () => {
  if (controller) return;
  activeRounds = Number(roundsSelect.value);
  totalQuestionRuns.textContent = `/ ${questions.length * activeRounds}`;
  summarySuccess.textContent = `0 / ${questions.length * activeRounds * 2}`;
});
exportJsonButton.addEventListener("click", () => currentReport && downloadReport(currentReport, "json"));
exportHtmlButton.addEventListener("click", () => currentReport && downloadReport(currentReport, "html"));
closeReportButton.addEventListener("click", () => reportDialog.close());
reportDialog.addEventListener("click", (event) => {
  if (event.target === reportDialog) reportDialog.close();
});
dialogExportJsonButton.addEventListener("click", () => viewedReport && downloadReport(viewedReport, "json"));
dialogExportHtmlButton.addEventListener("click", () => viewedReport && downloadReport(viewedReport, "html"));
presetSelect.addEventListener("change", applyPreset);
extractChannelButton.addEventListener("click", extractChannelCredentials);
referenceSource.addEventListener("change", applyReferenceSource);
manageOnlineChannelsButton.addEventListener("click", () => {
  renderOnlineChannelList();
  onlineChannelDialog.showModal();
  onlineChannelFields.name.focus();
});
closeOnlineChannelsButton.addEventListener("click", () => onlineChannelDialog.close());
onlineChannelDialog.addEventListener("click", (event) => {
  if (event.target === onlineChannelDialog) onlineChannelDialog.close();
});
onlineChannelDialog.addEventListener("close", resetOnlineChannelEditor);
onlineChannelForm.addEventListener("submit", saveOnlineChannel);
cancelChannelEditButton.addEventListener("click", resetOnlineChannelEditor);
extractOnlineChannelButton.addEventListener("click", extractOnlineChannelCredentials);
saveOfficialKeyButton.addEventListener("click", saveOfficialKey);
clearOfficialKeyButton.addEventListener("click", clearOfficialKey);
fields.referenceApiKey.addEventListener("input", () => {
  if (activeProvider && referenceSource.value === "official") {
    officialKeyDrafts[activeProvider] = fields.referenceApiKey.value;
  }
  delete saveOfficialKeyButton.dataset.state;
});
onlineChannelForm.addEventListener("input", () => {
  delete saveOnlineChannelButton.dataset.state;
  if (!editingOnlineChannelId) saveOnlineChannelButton.textContent = "保存渠道";
});

async function initialize() {
  try {
    const response = await fetch("/api/meta", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const meta = await response.json();
    presets = meta.presets;
    questions = meta.questions;
    onlineChannels = readOnlineChannels();
    const presetGroups = new Map();
    for (const preset of presets) {
      if (!presetGroups.has(preset.provider)) presetGroups.set(preset.provider, []);
      presetGroups.get(preset.provider).push(preset);
    }
    presetSelect.replaceChildren(
      ...[...presetGroups].map(([provider, providerPresets]) => {
        const group = document.createElement("optgroup");
        group.label = provider;
        group.append(
          ...providerPresets.map((preset) => {
            const option = document.createElement("option");
            option.value = preset.id;
            option.textContent = preset.label;
            return option;
          }),
        );
        return group;
      }),
    );
    presetSelect.disabled = false;
    applyPreset();
    renderQuestions();
    renderReportHistory();
    setButtonState("idle");
  } catch (error) {
    formError.textContent = `初始化失败：${error.message}`;
    formError.hidden = false;
    runStatus.textContent = "工具未能加载测试题。";
  }
}

initialize();
