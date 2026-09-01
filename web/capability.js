// 能力评测：选渠道 → 选标杆 → 看成本 → 提交。
// 能力评测自动使用目标平台组的同模型标杆。

let targets = [];
let platformGroups = [];

async function load() {
  try {
    [targets, platformGroups] = await Promise.all([
      get('/api/targets'), get('/api/platform-groups'),
    ]);
    if (!targets.length) {
      $('#notice').className = 'notice info';
      $('#notice').innerHTML =
        '还没有已配置的模型，先去 <a href="admission.html">添加模型并检测</a>。';
      $('#notice').classList.remove('hide');
      $('#btn-submit').disabled = true;
      return;
    }
    const pick = Number(new URLSearchParams(location.search).get('target') || 0);
    $('#f-target').innerHTML = targetOptions(pick);
    renderBenchOptions();
    onPick();
  } catch (e) { showError(e.message); }
}

function targetOptions(pick) {
  const option = (target) => `<option value="${target.id}" ${target.id === pick ? 'selected' : ''}>
    ${esc(target.name)}　·　${esc(target.model)}</option>`;
  const grouped = platformGroups.map((group) => {
    const members = targets.filter((target) => target.platform_group_id === group.id);
    return members.length
      ? `<optgroup label="${esc(group.label)}">${members.map(option).join('')}</optgroup>`
      : '';
  }).join('');
  const ungrouped = targets.filter((target) => !target.platform_group_id);
  return grouped + (ungrouped.length
    ? `<optgroup label="未分组">${ungrouped.map(option).join('')}</optgroup>` : '');
}

function renderBenchOptions() {
  const t = targets.find((x) => x.id === Number($('#f-target').value));
  const group = t && platformGroups.find((g) => g.id === t.platform_group_id);
  const slot = group?.models.find((model) => model.model === t.model);
  const benchmark = slot?.benchmark;
  $('#f-bench').innerHTML = benchmark
    ? `<option value="${benchmark.id}">${esc(group.label)}　·　${esc(benchmark.name)}${benchmark.overall != null ? `　·　综合 ${(benchmark.overall * 100).toFixed(0)}%` : ''}${benchmark.stale ? '（题库版本不符）' : ''}</option>`
    : `<option value="">${group ? '目标平台组的该模型尚未设置标杆' : '该模型尚未选择平台组'}</option>`;
  $('#f-bench').disabled = true;
}

function onPick() {
  renderBenchOptions();
  onBench();
  const id = Number($('#f-target').value);
  loadEstimate(id);
  loadHistory(id);
}

function onBench() {
  const t = targets.find((x) => x.id === Number($('#f-target').value));
  const group = t && platformGroups.find((item) => item.id === t.platform_group_id);
  const b = group?.models.find((model) => model.model === t.model)?.benchmark;
  if (!b) {
    $('#bench-tip').textContent =
      '先在工作台选择平台倍率组并设置同模型标杆；评测会自动使用该标杆。';
    $('#bench-detail').innerHTML = '';
    return;
  }
  $('#bench-tip').textContent =
    `容差 ${(b.tolerance * 100).toFixed(0)}%：结果会自动与目标平台组的同模型标杆比较。`;
  const rows = Object.entries(b.dims || {}).map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td>${(v * 100).toFixed(0)}%</td></tr>`).join('');
  $('#bench-detail').innerHTML = `
    <table style="margin-top:10px"><thead><tr><th>维度</th><th>标杆分</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div class="help">来源：任务 #${b.source_task_id}${
    b.model ? `　·　标杆模型：${esc(b.model)}` : ''}</div>`;
}

function includeHard() {
  return $('#f-include_hard').checked;
}

async function loadEstimate(id) {
  try {
    const e = await get(`/api/estimate?kind=capability&target_id=${id}`
      + `&include_hard=${includeHard()}`);
    $('#est').innerHTML = [
      ['预计请求数', e.requests], ['预计 tokens', e.tokens.toLocaleString()],
      ['并发度', e.concurrency], ['预计费用', '¥' + e.cost.toFixed(4)],
    ].map(([k, v]) =>
      `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
    $('#f-limit').placeholder = `默认 ${(e.cost * 4).toFixed(2)}`;
    $('#capability-lead').textContent = `当前能力包共 ${e.requests_no_hard ?? (e.requests - e.hard_requests)} 次基础请求${e.has_hard ? `；可选再执行 ${e.hard_requests} 道独立硬题` : ''}。所有数量、版本与费用均来自后端实时计划。`;
    if (e.has_hard) {
      $('#hard-tip').innerHTML = includeHard()
        ? `已开启：多 ${e.hard_requests} 道题、约 ${e.hard_tokens.toLocaleString()} tokens，`
          + `贵约 ¥${e.hard_cost.toFixed(4)}。关掉则回到 ¥${e.cost_no_hard.toFixed(4)}。`
          + `　题库版本 ${e.hard_version || ''}`
        : `已关闭：本次只跑四维能力评分（¥${e.cost_no_hard.toFixed(4)}）。`
          + `不跑硬题的话，这次结果存成标杆时不会带硬题分。`;
    } else {
      $('#hard-tip').textContent = '当前测试包没有硬题。';
    }
  } catch (e) { showError(e.message); }
}

async function loadHistory(id) {
  try {
    const rows = await get(`/api/tasks?kind=capability&target_id=${id}&limit=10`);
    $('#history').innerHTML = rows.length ? `
      <table><thead><tr><th>任务</th><th>时间</th><th>状态</th><th>结论</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td><a href="task.html?id=${r.id}">#${r.id}</a></td>
        <td>${fmtTime(r.created_at)}</td>
        <td>${statusTag(r.status)}</td>
        <td>${esc(r.verdict || '-')}</td>
      </tr>`).join('')}</tbody></table>`
      : '<div class="empty">该渠道还没有做过能力评测。</div>';
  } catch (e) { showError(e.message); }
}

$('#f-target').addEventListener('change', onPick);
$('#f-bench').addEventListener('change', onBench);
$('#f-include_hard').addEventListener('change',
  () => loadEstimate(Number($('#f-target').value)));
$('#btn-submit').addEventListener('click', async () => {
  const btn = $('#btn-submit');
  btn.disabled = true;
  btn.textContent = '提交中…';
  const body = {
    kind: 'capability', target_id: Number($('#f-target').value),
    include_hard: includeHard(),
  };
  const limit = parseFloat($('#f-limit').value);
  if (!Number.isNaN(limit)) body.cost_limit = limit;
  try {
    const r = await post('/api/tasks', body);
    location.href = `task.html?id=${r.task_id}`;
  } catch (e) {
    showError(e.message);
    btn.disabled = false;
    btn.textContent = '开始评测';
  }
});
load();
