let insightState = { overview: null, rankings: null, trend: [], gaps: [], profiles: [], reviews: [], channels: [] };

const statusName = { healthy: '健康', warning: '需关注', critical: '异常', no_data: '无数据' };
const usageName = { general: '通用', agent: 'Agent', coding: '编程', customer_service: '客服' };

function number(value) {
  return new Intl.NumberFormat('zh-CN').format(Number(value || 0));
}

function percent(value, digits = 1) {
  return value == null ? '-' : `${(Number(value) * 100).toFixed(digits)}%`;
}

function duration(value) {
  if (value == null) return '-';
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

function filterQuery() {
  const params = new URLSearchParams();
  const fields = {
    platform_group: '#filter-group', model_family: '#filter-family',
    model: '#filter-model', usage_profile: '#filter-usage', channel: '#filter-channel',
  };
  for (const [name, selector] of Object.entries(fields)) {
    const value = $(selector).value;
    if (value) params.set(name, value);
  }
  return params.toString();
}

function setOptions(selector, values, labels = {}) {
  const select = $(selector);
  const selected = select.value;
  select.innerHTML = '<option value="">全部</option>' + [...new Set(values)].filter(Boolean)
    .sort().map((value) => `<option value="${esc(value)}">${esc(labels[value] || value)}</option>`).join('');
  if ([...select.options].some((option) => option.value === selected)) select.value = selected;
}

function populateFilters(rows) {
  setOptions('#filter-group', rows.map((row) => row.platform_group));
  setOptions('#filter-family', rows.map((row) => row.model_family));
  setOptions('#filter-model', rows.map((row) => row.model));
  setOptions('#filter-usage', insightState.profiles.map((profile) => profile.code),
    Object.fromEntries(insightState.profiles.map((profile) => [profile.code, profile.name])));
  setOptions('#filter-channel', rows.map((row) => row.channel));
}

function renderFreshness(overview) {
  const banner = $('#freshness');
  const enabledSources = overview.sources.filter((source) => source.enabled);
  const latest = overview.latest_bucket ? fmtTime(overview.latest_bucket) : '尚无数据';
  if (overview.stale) {
    banner.className = 'freshness-banner stale';
    const errors = enabledSources.filter((source) => source.last_error).map((source) => source.name).join('、');
    banner.textContent = `数据过期 · 最新分钟桶 ${latest}${errors ? ` · 采集失败：${errors}` : ''}`;
  } else {
    banner.className = 'freshness-banner';
    banner.textContent = `数据正常 · 最新分钟桶 ${latest} · 延迟 ${Math.round(overview.data_delay_seconds || 0)} 秒`;
  }
}

function renderSummary(rows) {
  const requests = rows.reduce((sum, row) => sum + row.request_count, 0);
  const attempts = rows.reduce((sum, row) => sum + row.attempt_count, 0);
  const successes = rows.reduce((sum, row) => sum + row.success_count, 0);
  const timeouts = rows.reduce((sum, row) => sum + row.timeout_count, 0);
  const breaks = rows.reduce((sum, row) => sum + row.stream_break_count, 0);
  const critical = rows.filter((row) => row.status === 'critical').length;
  const cards = [
    ['近 5 分钟请求', number(requests), `${number(attempts)} 次渠道尝试`, ''],
    ['整体成功率', percent(attempts ? successes / attempts : null), `${number(successes)} 次成功`, attempts && successes / attempts < .95 ? 'bad' : ''],
    ['超时', number(timeouts), percent(attempts ? timeouts / attempts : null), timeouts ? 'warn' : ''],
    ['流式中断', number(breaks), percent(attempts ? breaks / attempts : null), breaks ? 'warn' : ''],
    ['异常渠道', number(critical), `共 ${number(rows.length)} 条健康记录`, critical ? 'bad' : ''],
  ];
  $('#summary-cards').innerHTML = cards.map(([label, value, note, cls]) => `
    <article class="summary-card ${cls}"><span>${label}</span><strong>${value}</strong><small>${note}</small></article>
  `).join('');
}

function renderTrend(rows) {
  const root = $('#trend-chart');
  if (rows.length < 2) {
    root.className = 'trend-chart empty';
    root.textContent = rows.length ? '至少需要两个分钟桶才能形成趋势' : '等待指标数据';
    return;
  }
  root.className = 'trend-chart';
  const width = 1000;
  const height = 150;
  const padding = 24;
  const maxRequests = Math.max(...rows.map((row) => row.request_count), 1);
  const points = (field, maximum) => rows.map((row, index) => {
    const x = padding + index * (width - padding * 2) / (rows.length - 1);
    const raw = field === 'success_rate'
      ? (row.attempt_count ? row.success_count / row.attempt_count : 0)
      : row[field];
    const y = height - padding - raw / maximum * (height - padding * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  root.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="请求量与成功率趋势">
    <line class="chart-grid" x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}"></line>
    <line class="chart-grid" x1="${padding}" y1="${padding}" x2="${width - padding}" y2="${padding}"></line>
    <polyline class="chart-line" points="${points('request_count', maxRequests)}"></polyline>
    <polyline class="chart-success" points="${points('success_rate', 1)}"></polyline>
    <text class="chart-label" x="${padding}" y="12">请求量（蓝） · 成功率（绿）</text>
    <text class="chart-label" x="${width - 100}" y="${height - 5}">${esc(fmtTime(rows.at(-1).bucket_start))}</text>
  </svg>`;
}

function renderHealth(rows) {
  $('#health-count').textContent = `${rows.length} 条记录`;
  $('#health-list').innerHTML = rows.length ? rows.map((row) => `
    <details class="health-row">
      <summary>
        <span class="health-name"><strong>${esc(row.channel)}</strong><small>${esc(row.platform_group)} · ${esc(row.model)} · ${esc(usageName[row.usage_profile] || row.usage_profile)}</small></span>
        <span class="health-status ${esc(row.status)}">${esc(statusName[row.status] || row.status)}</span>
        <span class="health-metric"><span>请求</span><b>${number(row.request_count)}</b></span>
        <span class="health-metric"><span>成功率</span><b>${percent(row.success_rate)}</b></span>
        <span class="health-metric"><span>P95</span><b>${duration(row.latency_p95_ms)}</b></span>
      </summary>
      <div class="health-detail">
        <div><span>渠道尝试</span><b>${number(row.attempt_count)}</b></div>
        <div><span>TTFT P95</span><b>${duration(row.ttft_p95_ms)}</b></div>
        <div><span>超时</span><b>${number(row.timeout_count)}</b></div>
        <div><span>断流</span><b>${number(row.stream_break_count)}</b></div>
        <div><span>429</span><b>${number(row.rate_limit_count)}</b></div>
        <div><span>上游 5xx</span><b>${number(row.upstream_5xx_count)}</b></div>
      </div>
    </details>`).join('') : '<div class="empty-state">当前筛选条件下没有分钟指标</div>';
}

function renderRankList(rootSelector, rows, kind) {
  const root = $(rootSelector);
  root.className = 'rank-list';
  root.innerHTML = rows.length ? rows.map((row) => {
    let title;
    let note;
    let value;
    if (kind === 'demand') {
      title = `${row.platform_group} · ${row.model}`;
      note = `${usageName[row.usage_profile] || row.usage_profile} · ${number(row.active_users)} 匿名活跃用户`;
      value = number(row.request_count);
    } else if (kind === 'stability') {
      title = row.channel;
      note = `${row.platform_group} · ${row.model} · 下界 ${percent(row.success_rate_lower_bound, 2)}`;
      value = row.stability_score.toFixed(3);
    } else {
      title = row.channel;
      note = `${row.model} · ${usageName[row.usage_profile] || row.usage_profile} · ${row.output_length_band}`;
      value = duration(row.latency_p95_ms);
    }
    return `<div class="rank-row"><span class="rank-number">${row.rank}</span><span><strong>${esc(title)}</strong><small>${esc(note)}</small></span><span class="rank-value">${esc(value)}</span></div>`;
  }).join('') : '<div class="empty-state">达到正式样本门槛后显示</div>';
}

function renderGaps(rows) {
  const visible = rows.filter((row) => row.reasons && row.reasons.length);
  $('#supply-gaps').innerHTML = visible.length ? visible.map((row) => `
    <article class="gap-item ${row.status === 'observe' ? 'observe' : ''}">
      <div><h3>${esc(row.platform_group)} · ${esc(row.model)} · ${esc(usageName[row.usage_profile] || row.usage_profile)}</h3>
      <p>${row.reasons.map(esc).join('；')}</p></div>
      <div class="gap-count"><strong>${row.qualified_channels}/${row.required_channels || '-'}</strong><span>${row.status === 'observe' ? '观察' : '合格渠道/目标'}</span>${row.status !== 'observe' ? `<button type="button" class="sm" data-link-review="${row.id}">登记上线复盘</button>` : ''}</div>
    </article>`).join('') : '<div class="empty-state">当前没有需要补充的供应缺口</div>';
}

function renderReviews(rows) {
  const labels = { effective: '有效', no_clear_effect: '无明显效果', negative: '负面', insufficient_evidence: '证据不足' };
  $('#recommendation-reviews').innerHTML = rows.length ? rows.map((row) => {
    const latest = row.review_30d.classification ? row.review_30d : row.review_7d;
    return `<article class="review-item"><span><strong>${esc(row.channel_name)} → ${esc(row.platform_group)} · ${esc(row.model)}</strong><small>生产标识 ${esc(row.production_channel)} · 上线 ${fmtTime(row.activated_at)}</small></span><span><b>${esc(labels[latest.classification] || (row.status === 'scheduled' ? '等待 7 天' : '等待 30 天'))}</b><small>下次 ${fmtTime(row.next_review_at)}</small></span><span class="review-actions"><button type="button" data-run-review="${row.id}" data-days="7">复算 7 天</button><button type="button" data-run-review="${row.id}" data-days="30">复算 30 天</button></span></article>`;
  }).join('') : '<div class="empty-state">渠道真实上线后，可从供给缺口候选中登记复盘。</div>';
}

function renderSources(sources, quality) {
  $('#source-list').innerHTML = sources.length ? sources.map((source) => `
    <div class="source-row" data-source-id="${source.id}">
      <span><strong>${esc(source.name)}</strong><small>${source.enabled ? '自动采集中' : '已停用'} · 协议 v${esc(source.protocol_version)}</small></span>
      <span><small>${esc(source.endpoint)}</small><small>${source.last_error ? `错误：${esc(source.last_error)}` : `游标：${esc(source.cursor || '尚未采集')}`}</small></span>
      <span class="source-buttons"><button data-action="collect">立即采集</button><button data-action="toggle">${source.enabled ? '停用' : '启用'}</button><button data-action="delete">删除</button></span>
    </div>`).join('') : '<div class="empty-state">尚未配置中转站只读指标接口</div>';
  $('#quality-list').innerHTML = quality.length ? quality.map((row) => `
    <div class="quality-row"><span>数据源 #${row.source_id}</span><span>${row.completeness == null ? '尚无分钟桶' : `完整率 ${percent(row.completeness, 2)}`}</span><span>${number(row.missing_count || 0)} 个缺桶</span></div>
  `).join('') : '<div class="empty-state">暂无完整率数据</div>';
}

function renderInsufficient(rows) {
  $('#insufficient-list').innerHTML = rows.length ? `<div class="rank-list">${rows.map((row) => `
    <div class="rank-row"><span class="rank-number">-</span><span><strong>${esc(row.channel)}</strong><small>${esc(row.model)} · ${row.active_days} 个活跃日</small></span><span class="rank-value">${number(row.attempt_count)} 尝试</span></div>
  `).join('')}</div>` : '<div class="empty-state">没有低样本渠道</div>';
}

async function loadDashboard({ resetFilters = false } = {}) {
  clearError();
  const query = filterQuery();
  const suffix = query ? `&${query}` : '';
  try {
    const [overview, trend, rankings, gaps, profiles, reviews, channels] = await Promise.all([
      get(`/api/insights/overview?minutes=5${suffix}`),
      get(`/api/insights/trend?hours=24${suffix}`),
      get('/api/insights/rankings?days=7'),
      get('/api/insights/supply-gaps'),
      get('/api/insights/usage-profiles'),
      get('/api/insights/recommendation-reviews'),
      get('/api/channels'),
    ]);
    insightState = { overview, trend, rankings, gaps, profiles, reviews, channels };
    if (resetFilters) populateFilters(overview.rows);
    renderFreshness(overview);
    renderSummary(overview.rows);
    renderTrend(trend);
    renderHealth(overview.rows);
    renderRankList('#demand-ranking', rankings.demand_top, 'demand');
    renderRankList('#stability-ranking', rankings.stability_top, 'stability');
    renderRankList('#speed-ranking', rankings.speed_top, 'speed');
    renderGaps(gaps);
    renderReviews(reviews);
    renderSources(overview.sources, overview.collection_quality);
    renderInsufficient(rankings.insufficient);
  } catch (error) {
    showError(error.message);
  }
}

async function refreshSourcesOnly() {
  const overview = await get('/api/insights/overview?minutes=5');
  renderSources(overview.sources, overview.collection_quality);
}

$('#refresh-all').addEventListener('click', () => loadDashboard());
$('#refresh-gaps').addEventListener('click', async () => {
  try {
    renderGaps(await post('/api/insights/supply-gaps/refresh'));
  } catch (error) { showError(error.message); }
});
$('#supply-gaps').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-link-review]');
  if (!button) return;
  const choices = insightState.channels.map((channel) => `${channel.id} = ${channel.name}`).join('\n');
  const channelId = Number(prompt(`填写已经人工上线的测试平台渠道 ID：\n${choices}`));
  if (!channelId) return;
  const channel = insightState.channels.find((item) => item.id === channelId);
  if (!channel) return showError('渠道 ID 不存在');
  const productionChannel = prompt('填写生产分钟指标里的实际渠道标识', channel.name);
  if (!productionChannel) return;
  const date = prompt('实际上线时间（本地时间，YYYY-MM-DD HH:mm）', new Date().toISOString().slice(0, 16).replace('T', ' '));
  const activatedAt = new Date(date.replace(' ', 'T')).getTime() / 1000;
  if (!Number.isFinite(activatedAt)) return showError('上线时间格式无效');
  try { await post(`/api/insights/supply-gaps/${button.dataset.linkReview}/reviews`, { channel_id: channelId, production_channel: productionChannel, activated_at: activatedAt }); await loadDashboard(); } catch (error) { showError(error.message); }
});
$('#recommendation-reviews').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-run-review]');
  if (!button) return;
  button.disabled = true;
  try { await post(`/api/insights/recommendation-reviews/${button.dataset.runReview}/run?horizon_days=${button.dataset.days}`); await loadDashboard(); } catch (error) { button.disabled = false; showError(error.message); }
});
$$('.insight-filters select').forEach((select) => select.addEventListener('change', () => loadDashboard()));

$('#show-source-form').addEventListener('click', () => $('#source-form').classList.remove('hide'));
$('#cancel-source').addEventListener('click', () => $('#source-form').classList.add('hide'));
$('#source-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await post('/api/insights/sources', {
      name: $('#source-name').value,
      endpoint: $('#source-endpoint').value,
      token: $('#source-token').value,
      protocol_version: '1',
      enabled: true,
      poll_interval_seconds: Number($('#source-interval').value),
    });
    event.target.reset();
    event.target.classList.add('hide');
    await loadDashboard({ resetFilters: true });
  } catch (error) { showError(error.message); }
});

$('#source-list').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-action]');
  const row = event.target.closest('[data-source-id]');
  if (!button || !row) return;
  const sourceId = Number(row.dataset.sourceId);
  const source = insightState.overview.sources.find((item) => item.id === sourceId);
  button.disabled = true;
  try {
    if (button.dataset.action === 'collect') await post(`/api/insights/sources/${sourceId}/collect`);
    if (button.dataset.action === 'toggle') await api(`/api/insights/sources/${sourceId}`, {
      method: 'PATCH', body: JSON.stringify({ enabled: !source.enabled }),
    });
    if (button.dataset.action === 'delete') await del(`/api/insights/sources/${sourceId}`);
    await loadDashboard({ resetFilters: true });
  } catch (error) {
    showError(error.message);
    button.disabled = false;
  }
});

loadDashboard({ resetFilters: true });
