// 任务详情：执行中轮询进度，结束后渲染报告。三种视图共用一份数据。

const taskId = new URLSearchParams(location.search).get('id');
let timer = null;
let current = null;
let view = 'exec';
let reco = null;            // 该任务对目标平台组的标杆比较
let placementCfg = null;    // 维度顺序与判定阈值

const HINT = {
  recommend: '可以接入，按正常节奏使用即可。',
  observe: '可以接入，但要盯一段时间，建议开启每日巡检。',
  downgrade: '先降级使用，不要放量，问题解决后再复测。',
  reject: '暂时不要接入，先按下面的建议处理。',
  manual: '需要人工确认后再决定，重点看下面标出的疑点。',
};

async function tick() {
  try {
    const t = await get(`/api/tasks/${taskId}`);
    current = t;
    // 任务结束后才拉推荐：执行器是先写推荐再翻终态的，所以这时一定读得到
    if (t.report && (!reco || !placementCfg)) {
      try {
        [reco, placementCfg] = await Promise.all([
          get(`/api/tasks/${taskId}/recommendation`),
          get('/api/placement-config'),
        ]);
      } catch { /* 推荐拉不到不影响看报告 */ }
    }
    render(t);
    const running = t.status === 'queued' || t.status === 'running';
    if (!running && timer) { clearInterval(timer); timer = null; }
  } catch (e) {
    showError(e.message);
    if (timer) { clearInterval(timer); timer = null; }
  }
}

function render(t) {
  const s = t.snapshot || {};
  $('#title').textContent = `${KIND_NAME[t.kind] || t.kind} #${t.id}`;
  $('#subtitle').innerHTML =
    `${esc(t.target_name || s.name || '-')}　·　${esc(s.model || '-')}` +
    `　·　${esc(t.pack_name)} ${esc(t.pack_version)}　·　提交于 ${fmtTime(t.created_at)}`;
  $('#head-actions').innerHTML = statusTag(t.status);

  const running = t.status === 'queued' || t.status === 'running';
  $('#progress-card').classList.toggle('hide', !running);
  if (running) renderProgress(t);

  $('#report-area').classList.toggle('hide', !t.report);
  if (t.report) renderReport(t.report);

  $('#timeline').innerHTML = (t.events || []).map((e) => `
    <li class="${e.level === 'info' ? 'ok' : e.level === 'error' ? 'bad' : ''}">
      <b>${esc(e.stage || '-')}</b>　${esc(e.message)}
      <span class="small muted">　${fmtTime(e.ts)}</span>
    </li>`).join('') || '<li class="muted">暂无记录</li>';
}

function renderProgress(t) {
  const p = t.progress || {};
  const done = p.done || 0;
  const total = p.total || 1;
  $('#p-current').textContent = p.current ? `正在执行：${p.current}` : (p.stage || '排队中…');
  $('#p-status').innerHTML = `${done} / ${total}`;
  $('#p-bar').style.width = `${Math.min(100, (done / total) * 100)}%`;
  const bits = [];
  if (p.failed) bits.push(`失败 ${p.failed}`);
  if (p.eta) bits.push(`预计还需 ${p.eta}s`);
  if (p.tokens) bits.push(`累计 ${p.tokens} tokens`);
  if (p.cost) bits.push(`约 ¥${p.cost.toFixed(4)}`);
  if (p.last_error) bits.push(`最近错误：${p.last_error}`);
  if (t.local_runner) bits.push(`本地执行器：${t.local_runner.name} · ${t.local_runner.status === 'online' ? '在线' : '离线'} · 作业 ${t.local_runner.job_status}`);
  $('#p-detail').textContent = bits.join('　·　');
}

function renderReport(rep) {
  if (rep.kind === 'scheduled_measurement') {
    return renderScheduledMeasurementReport(rep);
  }
  const c = rep.conclusion;
  $('#verdict').className = `verdict v-${c.code}`;
  $('#verdict').textContent = c.verdict;
  $('#verdict-hint').textContent = HINT[c.code] || '';
  $('#view-body').innerHTML = view === 'exec' ? viewExec(rep)
    : view === 'ops' ? viewOps(rep) : viewTech(rep);
  // 按钮每次重渲染都是新节点，所以在这里绑而不是在加载时绑一次
  const asBench = $('#btn-as-bench');
  if (asBench) asBench.addEventListener('click', saveAsBenchmark);
  const confirmPlacement = $('#confirm-placement');
  if (confirmPlacement) confirmPlacement.addEventListener('click', decidePlacement);
  const rejectPlacement = $('#reject-placement');
  if (rejectPlacement) rejectPlacement.addEventListener('click', rejectPlacementDecision);
}

function renderScheduledMeasurementReport(rep) {
  const integrity = rep.integrity || {};
  $('#verdict').className = `verdict ${integrity.ok ? 'v-recommend' : 'v-manual'}`;
  $('#verdict').textContent = integrity.ok ? '定时监测报告已封存' : '定时监测证据完整性失败';
  $('#verdict-hint').textContent = '稳定性和速度用于持续监测；系统不会据此自动改变渠道业务状态或准入结论。';
  const metrics = (rep.models || []).map((model) => {
    const speed = model.speed || {};
    const rate = model.success_rate == null ? '数据不足' : `${(model.success_rate * 100).toFixed(1)}%`;
    const upstream = model.upstream_error_rate == null ? '数据不足' : `${(model.upstream_error_rate * 100).toFixed(1)}%`;
    return `<section class="card"><h3>${esc(model.canonical_model)}</h3><div class="metrics">${metric('稳定性成功率', rate, `${model.success_count}/${model.stability_denominator} 分母`)}${metric('上游错误率', upstream, `${model.upstream_error_count} 个明确上游错误`)}${metric('归因待确认', String(model.attribution_pending_count || 0))}${metric('平台系统错误', String(model.platform_error_count || 0))}${metric('TTFT 中位数', speed.ttft_median == null ? '数据不足' : `${speed.ttft_median.toFixed(3)}s`, `${(speed.ttft_samples || []).length} 个有效样本`)}${metric('短响应总耗时 P95', speed.short_duration_p95 == null ? '数据不足' : `${speed.short_duration_p95.toFixed(3)}s`, `${(speed.short_duration_samples || []).length} 个有效样本`)}${metric('中等响应总耗时 P95', speed.medium_duration_p95 == null ? '数据不足' : `${speed.medium_duration_p95.toFixed(3)}s`, `${(speed.medium_duration_samples || []).length} 个有效样本`)}</div></section>`;
  }).join('');
  $('#view-body').innerHTML = `<p class="help">计划版本 #${esc(rep.plan_version_id)} · 证据记录 ${esc(integrity.records || 0)} 条 · ${integrity.ok ? '完整性通过' : '完整性失败，仅保留历史'}</p>${metrics || '<p class="muted">暂无模型测量结果。</p>'}`;
}

async function saveAsBenchmark() {
  if (!current.target_id) return showError('原渠道模型已经不存在，无法设置为当前标杆');
  location.href = `workspace.html?benchmark_target=${current.target_id}`;
}

function targetComparison() {
  return reco && reco.result && reco.result.comparisons
    ? reco.result.comparisons[0] || null : null;
}

// ① 管理层摘要：结论、依据、要做什么、成本
function viewExec(rep) {
  const m = rep.metrics;
  const c = rep.conclusion;
  const speed = m.fixed_speed || {};
  const simple = Boolean(m.admission && m.fixed_speed);
  return `
    <b>判断依据</b>
    <ul class="plain">${c.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
    <b>下一步做什么</b>
    <ul class="plain">${c.actions.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>
    <div class="metrics" style="margin-top:16px">
      ${metric('可用性成功率', `${(m.pass_rate * 100).toFixed(0)}%`)}
      ${simple ? metric('智力题', `${m.admission.passed_items}/${m.admission.expected_items}`) : ''}
      ${simple ? metric('流式稳定率', `${(m.admission.stability_rate * 100).toFixed(0)}%`) : ''}
      ${simple ? metric('固定速度题首字 P50 / P95', `${speed.p50_ttft ?? '-'}s / ${speed.p95_ttft ?? '-'}s`) : metric('P95 延迟', `${m.p95_latency}s`)}
      ${simple ? metric('固定速度题总耗时 P50 / P95', `${speed.p50_latency ?? '-'}s / ${speed.p95_latency ?? '-'}s`) : metric('生成速度', `${m.avg_tokens_per_second || 0} tokens/s`)}
      ${metric('本次花费', `¥${m.cost.toFixed(4)}`)}
      ${m.load ? metric('稳定并发', m.load.safe_concurrency) : ''}
      ${m.capability
        ? metric('综合得分', m.capability.overall == null
            ? '-' : `${(m.capability.overall * 100).toFixed(0)}%`) : ''}
      ${m.hard
        ? metric('硬题正确率', m.hard.rate == null
            ? '未测到' : `${(m.hard.rate * 100).toFixed(0)}%`) : ''}
    </div>
    ${evaluationPackageBlock(rep)}
    ${radarBlock(rep)}
    ${specialtyBlock(rep)}
    ${selectedBenchmarkBlock(rep)}
    ${loadBlock(rep)}
    ${trustBlock(rep)}
    ${gateBlock(rep)}
    ${recoBlock()}`;
}

function specialtyBlock(rep) {
  const specialties = rep.metrics.specialties || {};
  const entries = Object.values(specialties);
  if (!entries.length) return '';
  const status = {
    suitable: ['适合该用途', 'ok'], not_suitable: ['未达到专项标准', 'bad'],
    evidence_insufficient: ['该用途证据不足', 'warn'],
  };
  return `<h2>用途专项标签</h2><div class="metrics">${entries.map((item) => {
    const [label, cls] = status[item.status] || [item.status, ''];
    const score = item.score == null ? label : `${label} · ${(item.score * 100).toFixed(0)}%`;
    return `<div class="metric"><div class="k">${esc(item.name)} <span class="tag ${cls}">${esc(item.version)}</span></div><div class="v">${esc(score)}</div><div class="help">${item.graded}/${item.total} 项有证据${item.recommended ? ' · 系统曾推荐' : ''}</div></div>`;
  }).join('')}</div><p class="help">专项结论彼此独立，也不改变上方基础准入结论。</p>`;
}

function evaluationPackageBlock(rep) {
  const m = rep.metrics || {};
  const admission = m.admission;
  const agent = m.agent_stability;
  const development = m.development_speed;
  const context = m.long_context;
  if (!admission && !agent && !development && !context) return '';
  const admissionLabels = {
    passed: '通过', provisional_pass: '暂定通过',
    failed: '未通过', insufficient_evidence: '证据不足',
  };
  const simpleAdmission = Boolean(admission && m.fixed_speed);
  const workload = development && development.workloads ? development.workloads : {};
  return `<h2>${simpleAdmission ? '简单测试结果' : '分层评测结果'}</h2><div class="metrics">
    ${admission ? metric('最终判断', `${admissionLabels[admission.status] || admission.status}`) : ''}
    ${simpleAdmission ? metric('智力是否足够', admission.intelligence_passed ? `是 · ${admission.passed_items}/${admission.expected_items}` : `否 · ${admission.passed_items}/${admission.expected_items}`) : ''}
    ${simpleAdmission ? metric('流式稳定性', `${(admission.stability_rate * 100).toFixed(0)}% · ${admission.streamed_questions}/${admission.expected_streamed_questions} 题流式`) : ''}
    ${agent ? metric('Agent 稳定性', agent.pass_rate == null ? '证据不足' : `${(agent.pass_rate * 100).toFixed(0)}%`) : ''}
    ${agent && agent.workflow_pass_rate != null ? metric('工作流完成率', `${(agent.workflow_pass_rate * 100).toFixed(0)}%`) : ''}
    ${development ? metric('开发固定负载', `${development.graded}/${development.expected} 项形成判分`) : ''}
    ${workload.short ? metric('短响应 P50 TTFT', `${workload.short.p50_ttft}s`) : ''}
    ${workload.medium ? metric('中等代码 P95', `${workload.medium.p95_total}s`) : ''}
    ${workload.long ? metric('长代码输出速度', workload.long.median_tokens_per_second == null ? '-' : `${workload.long.median_tokens_per_second} tokens/s`) : ''}
    ${context ? metric('可靠上下文', context.reliable_context_tier === '8k' ? '8K' : '8K 未证明') : ''}
    ${context ? metric('缓存', context.cache_status === 'measured' ? '已形成证据' : '尚未验证') : ''}
    ${context && context.cache_hit_rate != null ? metric('缓存命中率', `${(context.cache_hit_rate * 100).toFixed(0)}%`) : ''}
  </div>`;
}

function radarBlock(rep) {
  const cap = rep.metrics.capability;
  if (!cap || !cap.dims) return '';
  const names = cap.dim_order || Object.keys(cap.dims);
  const values = names.map((name) => Number((cap.dims[name] || {}).score || 0));
  const comparison = targetComparison();
  const selectedDims = comparison && comparison.comparable
    ? Object.fromEntries(Object.entries(comparison.per_dim || {})
      .filter(([, value]) => value.bench != null)
      .map(([name, value]) => [name, value.bench]))
    : null;
  const bench = selectedDims || (rep.benchmark && rep.benchmark.dims) || {};
  const benchValues = names.map((name) => Number(bench[name] || 0));
  const cx = 180, cy = 145, radius = 96;
  const point = (value, index, extra = 0) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / names.length;
    const r = radius * value + extra;
    return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
  };
  const polygon = (vals) => vals.map((value, i) => point(value, i).join(',')).join(' ');
  const rings = [0.2, 0.4, 0.6, 0.8, 1].map((value) =>
    `<polygon points="${polygon(names.map(() => value))}" class="radar-grid"/>`
  ).join('');
  const axes = names.map((name, i) => {
    const [x, y] = point(1, i);
    const [lx, ly] = point(1, i, 28);
    return `<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}" class="radar-axis"/>
      <text x="${lx}" y="${ly}" text-anchor="middle" dominant-baseline="middle"
        class="radar-label">${esc(name)}</text>`;
  }).join('');
  return `<h2>四维能力图</h2>
    <div class="radar-wrap"><svg viewBox="0 0 360 300" role="img"
      aria-label="候选渠道四维能力得分与标杆对比">
      ${rings}${axes}
      ${Object.keys(bench).length ? `<polygon points="${polygon(benchValues)}"
        class="radar-benchmark"/>` : ''}
      <polygon points="${polygon(values)}" class="radar-current"/>
    </svg></div>
    <div class="small muted" style="text-align:center">蓝色：本次结果${Object.keys(bench).length
      ? '　橙色：目标平台组标杆' : '　·　目标组尚未设置标杆'}</div>`;
}

function loadBlock(rep) {
  const load = rep.metrics.load;
  if (!load || !load.rounds) return '';
  const rows = load.rounds.map((r) => `<tr>
    <td>${r.concurrency}</td><td>${r.requests}</td>
    <td>${(r.success_rate * 100).toFixed(0)}%</td>
    <td>${r.throughput} req/s</td><td>${r.p95_ttft}s</td>
    <td>${r.p95_latency}s</td><td>${r.tokens_per_second}</td>
    <td>${(r.speed_decline * 100).toFixed(0)}%</td>
    <td>${esc(r.cache_signal || '-')}</td>
    <td>${r.stable ? '<span class="tag ok">稳定</span>' : '<span class="tag bad">停止</span>'}</td>
  </tr>`).join('');
  const runner = rep.metrics.local_runner || {};
  return `<h2>压力测试结果</h2>
    <div class="metrics">${metric('最大稳定并发', load.safe_concurrency)}
      ${metric('缓存观察', load.cache_signal || '-')}
      ${runner.cpu_count ? metric('本机 CPU 核数', runner.cpu_count) : ''}
      ${runner.network_response_bytes != null ? metric('本机接收流量', `${runner.network_response_bytes} bytes`) : ''}
      ${runner.generator_saturated != null ? metric('发生器饱和', runner.generator_saturated ? '是' : '否') : ''}</div>
    <table><thead><tr><th>并发</th><th>请求</th><th>成功率</th><th>吞吐</th>
      <th>P95 TTFT</th><th>P95 总延迟</th><th>tokens/s</th><th>速度衰减</th><th>缓存观察</th><th>结论</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

// 本轮评分不可信：上游注入 / 答非所问。放在最前面，因为它让所有分数失去意义。
function trustBlock(rep) {
  const t = rep.metrics.trust;
  if (!t || t.ok) return '';
  return `
    <div class="notice err" style="margin-top:16px">
      <b>本轮评分不可信，下面的分数不能作为定档依据</b>
      <ul class="plain">${(t.reasons || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
      我们只发一条 user 消息，正文里不该出现自我介绍或接受指令的话。
      出现说明上游在转发链路上注入了 system prompt，或请求模板被改写了。
    </div>`;
}

function selectedBenchmarkBlock(rep) {
  const cap = rep.metrics.capability;
  if (!cap || !cap.dims) return '';
  const cmp = targetComparison();
  if (!cmp) return `<h2>目标平台组标杆</h2>
    <div class="notice warn">该模型尚未归入平台倍率组，本次结果已保留。</div>`;
  if (!cmp.comparable) return `<h2>目标平台组标杆</h2>
    <div class="notice warn">${esc(cmp.skip_reason || '目标组同模型槽位尚未设置有效标杆')}。本次结果已保留，可从工作台设置标杆。</div>`;
  const dimBits = (placementCfg ? placementCfg.dim_order : []).map((name) => {
    const pd = (cmp.per_dim && cmp.per_dim[name]) || {};
    if (pd.ratio == null) return `${esc(name)} -`;
    const weak = (cmp.weak_dims || []).includes(name);
    return `<span class="${weak ? 'bad' : 'muted'}">${esc(name)} ${(pd.ratio * 100).toFixed(0)}%</span>`;
  }).join('　');
  return `<h2>目标平台组标杆</h2>
    <div class="card" style="margin-top:8px;background:var(--surface-soft)">
      <div class="between"><b>${esc(cmp.group_name)}</b>
        <span class="tag ${cmp.qualified ? 'ok' : 'bad'}">${cmp.qualified ? '达到目标' : '未达目标'}</span></div>
      <div class="small muted" style="margin-top:8px">标杆：${esc(cmp.benchmark_name || '-')}
       　综合比值：${cmp.ratio == null ? '-' : esc((cmp.ratio * 100).toFixed(0) + '%')}</div>
      <div class="small muted" style="margin-top:6px">${dimBits || '-'}</div>
    </div>`;
}

// 硬门槛（步骤 1）：过不了就终止分组推荐
function gateBlock(rep) {
  const g = rep.metrics.gates;
  if (!g || !g.items || !g.items.length) return '';
  // 超时率已经是 gates.items 里的一项，不要在这里再补一行
  const rows = g.items.map((x) => `
    <tr><td>${esc(x.name)}</td>
      <td>${x.passed ? '<span class="tag ok">通过</span>'
                     : '<span class="tag bad">未通过</span>'}</td>
      <td class="muted small">${esc(x.detail)}</td></tr>`).join('');
  return `
    <h2>硬门槛</h2>
    <table><thead><tr><th>门槛项</th><th>结果</th><th>说明</th></tr></thead>
    <tbody>${rows}</tbody></table>
    ${g.passed ? '' : '<div class="help">门槛未通过，不进入分组推荐。</div>'}`;
}

// 技术分组比较：优先判断意向组，再以同次、同版本证据寻找可用备选组。
function recoBlock() {
  if (!reco || !reco.result) return '';
  const r = reco.result;
  const cls = { target_met: 'ok', alternate_group: 'warn', observe_pool: 'warn', reject: 'bad', target_missed: 'bad',
                untrusted: 'bad', unassigned: 'warn', no_benchmark: 'warn' }[r.status] || 'warn';

  const rows = (r.comparisons || []).map((c) => {
    if (!c.comparable) {
      return `<tr><td>${esc(c.group_name)}</td><td>${c.multiplier}x</td>
        <td colspan="4" class="muted">${esc(c.skip_reason || '不可比')}</td></tr>`;
    }
    const dimBits = (placementCfg ? placementCfg.dim_order : [])
      .map((d) => {
        const pd = c.per_dim[d] || {};
        if (pd.ratio == null) return `${esc(d)} -`;
        const weak = (c.weak_dims || []).includes(d);
        return `<span class="${weak ? 'bad' : 'muted'}">${esc(d)} ${
          (pd.ratio * 100).toFixed(0)}%</span>`;
      }).join('　');
    return `<tr>
      <td>${esc(c.group_name)}</td>
      <td>${c.multiplier}x</td>
      <td><b>${c.ratio == null ? '-' : (c.ratio * 100).toFixed(0) + '%'}</b></td>
      <td class="muted">${c.cosine == null ? '-' : (c.cosine * 100).toFixed(0) + '%'}</td>
      <td>${c.qualified ? '<span class="tag ok">够格</span>'
                        : '<span class="tag">不够</span>'}</td>
      <td class="small">${dimBits}</td>
    </tr>`;
  }).join('');

  const decision = reco.status === 'pending' && r.decision_required ? `<div class="card" style="margin-top:10px"><b>人工最终确认</b><div class="flex" style="margin-top:8px"><select id="placement-group"><option value="">观察池</option>${(r.comparisons || []).filter((item) => item.comparable).map((item) => `<option value="${item.group_id}" ${item.group_id === r.suggested_group_id ? 'selected' : ''}>${esc(item.group_name)}${item.qualified ? ' · 达标' : ' · 未达标'}</option>`).join('')}</select><input id="placement-note" placeholder="复核备注"><button class="primary" id="confirm-placement">确认归属</button><button id="reject-placement">驳回建议</button></div></div>` : '';
  return `
    <h2>技术分组建议</h2>
    <div class="verdict v-${cls === 'ok' ? 'recommend' : cls === 'bad' ? 'reject' : 'observe'}"
         style="font-size:15px">${esc(r.headline)}</div>
    <ul class="plain">${(r.reasons || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
    ${rows ? `<table style="margin-top:10px"><thead><tr>
      <th>目标组</th><th>倍率</th><th>加权比值</th><th>结构相似度</th>
      <th>判定</th><th>各维度比值</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="help">先判断意向组；未达到时只用同一次、同版本结果比较其他具有有效同模型标杆的技术组。商业倍率不参与。</div>` : ''}
    ${decision}`;
}

async function decidePlacement() {
  try {
    const value = $('#placement-group').value;
    reco = await post(`/api/recommendations/${reco.id}/decide`, {
      accept: true, group_id: value ? Number(value) : null,
      note: $('#placement-note').value,
    });
    await tick();
  } catch (error) { showError(error.message); }
}

async function rejectPlacementDecision() {
  try {
    reco = await post(`/api/recommendations/${reco.id}/decide`, {
      accept: false, group_id: null, note: $('#placement-note').value,
    });
    await tick();
  } catch (error) { showError(error.message); }
}

// 能力评测维度表：得分 vs 标杆 vs 差值。没跑评测就不渲染。
function capBlock(rep) {
  const cap = rep.metrics.capability;
  if (!cap || !cap.dims || !Object.keys(cap.dims).length) return '';
  const target = targetComparison();
  const cmp = target && target.comparable ? target : null;
  const b = cmp ? {
    name: cmp.benchmark_name,
    overall: cmp.bench_overall,
    dims: Object.fromEntries(Object.entries(cmp.per_dim || {})
      .filter(([, value]) => value.bench != null)
      .map(([name, value]) => [name, value.bench])),
  } : rep.benchmark;
  const base = (b && b.dims) || {};
  const tol = b && b.tolerance != null ? b.tolerance : 0.10;
  const pct = (v) => (v == null ? '未测到' : `${(v * 100).toFixed(0)}%`);

  const rows = Object.entries(cap.dims).map(([dim, v]) => {
    const want = base[dim];
    let gap = '-';
    let note = '<span class="muted">无标杆</span>';
    if (v.score == null) {
      note = '<span class="tag warn">本次未测到</span>';
    } else if (want != null) {
      const diff = v.score - want;
      gap = `${diff >= 0 ? '+' : ''}${(diff * 100).toFixed(0)}%`;
      const weak = cmp ? (cmp.weak_dims || []).includes(dim) : diff < -tol;
      note = weak ? '<span class="tag bad">不达标</span>'
                  : '<span class="tag ok">达标</span>';
    }
    return `<tr>
      <td>${esc(dim)}</td>
      <td class="muted">${(v.weight * 100).toFixed(0)}%</td>
      <td><b>${pct(v.score)}</b></td>
      <td class="muted">${pct(v.coverage)}</td>
      <td class="muted">${pct(want)}</td>
      <td>${gap}</td>
      <td>${note}</td>
    </tr>`;
  }).join('');

  const head = b
    ? `标杆：${esc(b.name)}${cmp ? '（目标平台组）'
      : `（容差 ${(tol * 100).toFixed(0)}%）`}`
    : '尚未设定标杆，本次只出得分，不做达标判定';
  const over = cap.overall;
  const bover = b && b.overall;

  // 内容能力题的预算是按「简短作答」定的，思考 token 也算在里面。
  // 推理模型可能烧穿预算、正文返回空 —— 那些题记为未测到，总分只由剩下的题算出，
  // 拿它去和标杆比是在比两个不同的题目子集，必须说清楚。
  const trunc = cap.truncated_unscored || 0;
  const weakCoverage = cap.coverage_enforced
    ? Object.entries(cap.dims).filter(([, v]) => Number(v.coverage || 0) < 0.85)
    : [];
  const canBenchmark = !trunc && !weakCoverage.length;
  const truncNote = trunc ? `
    <div class="notice warn" style="margin-bottom:12px">
      ${trunc} 道内容能力题撞上 token 上限没测到（${(cap.truncated_items || [])
    .map(esc).join('、')}）。思考 token 也算在预算里，这个模型的思考量超出了内容题预算。
      <b>本次综合得分只由剩下的题算出，不能用于判档或存成标杆</b> ——
      与标杆比会变成两个不同题目子集的比较。请调整模型输出或题目预算后重测。
    </div>` : '';
  const coverageNote = weakCoverage.length && !trunc ? `
    <div class="notice warn" style="margin-bottom:12px">
      内容题有效覆盖率不足 85%（${weakCoverage.map(([name, value]) =>
        `${esc(name)} ${pct(value.coverage)}`).join('、')}），本次不能判档或存成标杆。
    </div>` : '';

  return `
    <h2>能力评测</h2>
    <div class="small muted" style="margin-bottom:8px">
      题库版本 ${esc(cap.pack_version)}　·　${head}</div>
    ${truncNote}
    ${coverageNote}
    <div class="metrics" style="margin-bottom:12px">
      ${metric('综合得分', pct(over))}
      ${bover != null ? metric('标杆综合', pct(bover)) : ''}
      ${bover != null && over != null
        ? metric('差值', `${over - bover >= 0 ? '+' : ''}${((over - bover) * 100).toFixed(0)}%`)
        : ''}
    </div>
    <table><thead><tr><th>维度</th><th>权重</th><th>得分</th><th>有效覆盖</th>
      <th>标杆</th><th>差值</th><th>判定</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div class="actions">
      <button class="sm" id="btn-as-bench" ${canBenchmark ? '' : 'disabled'}>把本次结果存为标杆</button>
      <span class="small muted">存之前先确认这条渠道当前状态可信。</span>
    </div>`;
}

// 硬题块：总正确率 + 分题库 + 分学科 + 答错清单。没跑硬题就不渲染。
// 与能力评测块并列，但刻意分开 —— 硬题不进四维能力总分，也不参与任何门槛。
function hardBlock(rep) {
  const h = rep.metrics.hard;
  if (!h || !h.total) return '';
  const b = rep.benchmark;
  const hb = (b && b.hard) || null;
  const stale = b && b.hard_stale;
  const pct = (v) => (v == null ? '未测到' : `${(v * 100).toFixed(0)}%`);

  const bankRows = Object.entries(h.banks || {}).map(([key, v]) => {
    const want = hb && hb.banks && hb.banks[key] ? hb.banks[key].rate : null;
    let gap = '-';
    if (v.rate != null && want != null && !stale) {
      const d = v.rate - want;
      gap = `${d >= 0 ? '+' : ''}${(d * 100).toFixed(0)}%`;
    }
    return `<tr>
      <td>${esc(v.name || key)}</td>
      <td><b>${pct(v.rate)}</b></td>
      <td class="muted">${v.correct} / ${v.graded}</td>
      <td class="muted">${want == null ? '-' : pct(want)}</td>
      <td>${gap}</td>
    </tr>`;
  }).join('');

  // 分组题量少，只列对/总，不用百分比制造精度错觉。
  const groupRows = Object.entries(h.groups || {})
    .sort((x, y) => x[0].localeCompare(y[0]))
    .map(([g, v]) => `<tr><td>${esc(g)}</td>
      <td class="muted">${v.correct} / ${v.graded}${v.ungraded
        ? `<span class="muted">（${v.ungraded} 未测到）</span>` : ''}</td></tr>`).join('');

  const wrong = Object.entries(h.items || {})
    .filter(([, v]) => v.graded && !v.correct)
    .map(([id, v]) => `<tr>
      <td class="muted">${esc(id)}</td>
      <td>${esc(v.answer || '（空）')}</td>
      <td class="muted">${esc(v.expected)}</td>
    </tr>`).join('');
  const missed = Object.entries(h.items || {}).filter(([, v]) => !v.graded);

  return `
    <h2>硬题（HardcoreLogic 无解识别）</h2>
    <div class="small muted" style="margin-bottom:8px">
      题库版本 ${esc(h.hard_version)}　·　独立计分，不进四维能力总分，
      也不参与可用性成功率、超时门槛与延迟分位
      ${stale ? '　·　<span class="tag warn">标杆硬题版本不一致，不做对比</span>' : ''}
    </div>
    <div class="metrics" style="margin-bottom:12px">
      ${metric('硬题正确率', pct(h.rate))}
      ${metric('答对', `${h.correct} / ${h.graded}`)}
      ${hb && hb.rate != null && !stale ? metric('标杆正确率', pct(hb.rate)) : ''}
      ${h.ungraded ? metric('未测到', h.ungraded) : ''}
      ${h.truncated_unscored ? metric('其中被截断', h.truncated_unscored) : ''}
      ${h.timeout_count ? metric('其中超时', h.timeout_count) : ''}
      ${h.p95_latency ? metric('硬题 P95', `${h.p95_latency}s`) : ''}
      ${h.reasoning_tokens_max
        ? metric('最大思考 token', h.reasoning_tokens_max.toLocaleString()) : ''}
    </div>
    ${h.truncated_unscored ? `<div class="notice warn" style="margin-bottom:12px">
      ${h.truncated_unscored} 道题撞上 token 上限且没给出答案，记为「未测到」，<b>不算答错</b>。
      思考 token 也算在 max_tokens 里，所以推理模型可能把预算全烧在思考上、正文一个字没输出。
      ${h.rate == null
        ? '本次没有一道题判上分，硬题正确率不可用，不能拿来判档。'
        : `正确率只由判上分的 ${h.graded} 道算出，与别的模型比较时要看一眼这个分母。`}
      调高预算：环境变量 <code>TEST_HARD_TOKENS_LOGIC</code>
      （改预算不影响已有硬题标杆的可比性）。
    </div>` : ''}
    <table><thead><tr><th>题库</th><th>正确率</th><th>对/已判</th>
      <th>标杆</th><th>差值</th></tr></thead><tbody>${bankRows}</tbody></table>
    ${groupRows ? `<div class="small muted" style="margin:14px 0 6px">
      <b>分学科</b>（每科题量少，只列对/总，不算百分比）</div>
      <table><thead><tr><th>学科</th><th>对/已判</th></tr></thead>
      <tbody>${groupRows}</tbody></table>` : ''}
    ${wrong ? `<div class="small muted" style="margin:14px 0 6px"><b>答错的题</b></div>
      <table><thead><tr><th>题目</th><th>模型答案</th><th>正确答案</th></tr></thead>
      <tbody>${wrong}</tbody></table>` : ''}
    ${missed.length ? `<div class="help" style="margin-top:10px">
      ${missed.length} 道题因请求失败没判上分（超时或上游错误），
      按「未测到」计，不算答错。</div>` : ''}
    ${h.rate === 0 ? `<div class="help" style="margin-top:10px">
      全错不一定是渠道问题：无解识别题用于拉开能力差距。
      判档看的是同一把尺子下的相对差距，不是绝对分。</div>` : ''}`;
}

// ② 运营可读版：分项通过情况 + 核心指标
function viewOps(rep) {
  const m = rep.metrics;
  const speed = m.fixed_speed || {};
  const simple = Boolean(m.admission && m.fixed_speed);
  const rows = rep.items.map((it) => `
    <tr>
      <td>${esc(it.step)}</td>
      <td>${it.ok ? '<span class="tag ok">通过</span>'
                  : '<span class="tag bad">失败</span>'}</td>
      <td>${esc(it.reason || '-')}</td>
      <td class="muted">${esc(it.detail)}</td>
    </tr>`).join('');
  const reasons = Object.entries(m.reason_counts || {});
  return `
    <div class="metrics">
      ${metric('可用性', `${m.ok} / ${m.total}`)}
      ${m.capability_total
        ? metric('能力题得分', `${m.capability_score} / ${m.capability_total}`) : ''}
      ${simple ? metric('固定速度题首字 P50', `${speed.p50_ttft ?? '-'}s`) : metric('P50 延迟', `${m.p50_latency}s`)}
      ${simple ? metric('固定速度题首字 P95', `${speed.p95_ttft ?? '-'}s`) : metric('P95 延迟', `${m.p95_latency}s`)}
      ${simple ? metric('固定速度题总耗时 P50', `${speed.p50_latency ?? '-'}s`) : metric('平均首 token', `${m.avg_first_token}s`)}
      ${simple ? metric('固定速度题总耗时 P95', `${speed.p95_latency ?? '-'}s`) : metric('平均生成速度', `${m.avg_tokens_per_second || 0} tokens/s`)}
      ${metric('断流率', `${(m.stream_break_rate * 100).toFixed(0)}%`)}
      ${metric('tokens 入/出', `${m.tokens_in} / ${m.tokens_out}`)}
    </div>
    <p class="help">可用性只算连通类测试；能力题答错记在能力题得分里，不拉低可用性。
      硬题完全不参与可用性、超时门槛与延迟分位，它单独一块。</p>
    ${evaluationPackageBlock(rep)}
    ${loadBlock(rep)}
    ${capBlock(rep)}
    ${specialtyBlock(rep)}
    ${hardBlock(rep)}
    ${reasons.length ? `<h2>失败分布</h2><div class="flex">${reasons
      .map(([k, v]) => `<span class="tag bad">${esc(k)} ${v} 次</span>`).join('')}</div>` : ''}
    <h2>分项结果</h2>
    <table><thead><tr><th>测试项</th><th>结果</th><th>失败原因</th><th>说明</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// ③ 技术明细：诊断证据，Key 已脱敏
function viewTech(rep) {
  const t = rep.target;
  const retryLabel = {
    drop_temperature: '移除 temperature',
    use_max_completion_tokens: '改用 max_completion_tokens',
    use_max_tokens: '改用 max_tokens',
    network_retry: '网络重试',
  };
  const rows = rep.evidence.map((e) => {
    const reasons = (e.retry_reasons || []).map((reason) => retryLabel[reason] || reason);
    const effective = e.effective_parameters || {};
    const grade = e.grade || {};
    const eligibility = e.eligibility || {};
    const transport = `${e.attempts || 1} 次`
      + (reasons.length ? ` · ${reasons.join('、')}` : '')
      + (effective.token_parameter ? ` · ${effective.token_parameter}` : '')
      + (effective.temperature ? ` · temperature ${effective.temperature}` : '');
    return `<tr>
      <td>${esc(e.step)}</td>
      <td>${e.ok ? '通过' : `<span class="bad">失败</span>`}</td>
      <td>${esc(grade.status || '不适用')}</td>
      <td class="muted small">${esc(eligibility.ability || '-')} / ${esc(eligibility.stability || '-')} / ${esc(eligibility.performance || '-')}</td>
      <td class="mono">${esc(e.actual_model || '-')}</td>
      <td>${e.latency}s${e.first_token ? ` / 首 ${e.first_token}s` : ''}</td>
      <td>${e.usage.prompt < 0 ? '缺失' : e.usage.prompt} /
          ${e.usage.completion < 0 ? '缺失' : e.usage.completion}</td>
      <td class="muted small">${esc(transport)}</td>
      <td class="muted small">${esc(e.reply_summary || e.detail)}</td>
    </tr>`;
  }).join('');
  return `
    <table><tbody>
      <tr><th style="width:120px">上游地址</th><td class="mono">${esc(t.base_url)}</td></tr>
      <tr><th>申报模型</th><td class="mono">${esc(t.model)}</td></tr>
      <tr><th>协议</th><td>${esc(t.protocol)}</td></tr>
      <tr><th>分组 / 环境</th><td>${esc(t.group || '-')} / ${esc(t.env || '-')}</td></tr>
      <tr><th>API Key</th><td class="mono">${esc(t.key_masked || '-')}</td></tr>
      <tr><th>计价</th><td>入 ¥${rep.metrics.price_in} / 出 ¥${rep.metrics.price_out}
        （每 1M token）</td></tr>
    </tbody></table>
    <h2>逐条证据</h2>
    <table><thead><tr><th>测试项</th><th>请求</th><th>内容判分</th><th>证据资格（能力/稳定/性能）</th><th>上游返回模型</th>
      <th>延迟</th><th>tokens 入/出</th><th>请求口径</th><th>摘要</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p class="help">摘要已截断并脱敏，完整 Key 不会出现在任何报告或日志里。</p>`;
}

function metric(k, v) {
  return `<div class="metric"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;
}

// ---- 交互 ----

$$('.tabs button').forEach((b) => b.addEventListener('click', () => {
  $$('.tabs button').forEach((x) => x.classList.remove('on'));
  b.classList.add('on');
  view = b.dataset.view;
  if (current && current.report) renderReport(current.report);
}));

$$('[data-fmt]').forEach((b) => b.addEventListener('click', () => {
  location.href = `/api/tasks/${taskId}/export?fmt=${b.dataset.fmt}`;
}));

$('#btn-cancel').addEventListener('click', async () => {
  if (!confirm('取消这个任务？已完成的部分仍会保留报告。')) return;
  try {
    await post(`/api/tasks/${taskId}/cancel`);
    tick();
  } catch (e) { showError(e.message); }
});

$('#btn-retry').addEventListener('click', async () => {
  try {
    const r = await post(`/api/tasks/${taskId}/retry`);
    location.href = `task.html?id=${r.task_id}`;
  } catch (e) { showError(e.message); }
});

if (!taskId) {
  showError('缺少任务号');
} else {
  tick();
  timer = setInterval(tick, 2000);
}
