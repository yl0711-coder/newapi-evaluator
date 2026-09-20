const $ = id => document.getElementById(id);
const api = (path, options) => Workbench.api('./api/' + path, options);
const node = (...args) => Workbench.node(...args);
const states = {passed:'通过', failed:'未通过', unconfirmed:'待确认', not_run:'未检测', running:'检测中', completed:'已完成', cancelled:'已停止', interrupted:'已中断'};
let metadata, channels = [], preview = null, active = null, poll = null, generation = 0;
const available = new Set(), selected = new Set();

function connection() {
  return {channel_id:Number($('channel').value) || null, base_url:$('base-url').value.trim()};
}
function plan() {
  if (!selected.size) throw new Error('请选择至少一个模型。');
  return {...connection(), models:[...selected].map(model => ({model}))};
}
function invalidate() {
  preview = null; $('start').disabled = true; $('preview-list').replaceChildren();
}
function error(value) { $('error').textContent = value?.message || ''; }
function busy(value) {
  $('setup').disabled = value; $('preview').disabled = value;
  $('start').disabled = value || !preview; $('stop').hidden = !active;
}
function renderModels() {
  const query = $('model-filter').value.trim().toLowerCase();
  const matches = [...available].filter(model => model.toLowerCase().includes(query));
  $('model-list').replaceChildren(...matches.slice(0, 200).map(model => {
    const label = node('label', model, 'check'), checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.checked = selected.has(model);
    checkbox.setAttribute('aria-label', '选择模型 ' + model);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked && selected.size >= metadata.max_models) {
        checkbox.checked = false; error(new Error('每批最多检测 5 个模型，请分批选择。')); return;
      }
      checkbox.checked ? selected.add(model) : selected.delete(model);
      invalidate(); renderModels();
    });
    label.prepend(checkbox); return label;
  }));
  $('model-count').textContent = `列表共 ${available.size} 个模型，匹配 ${matches.length} 个${matches.length > 200 ? '，当前显示前 200 个，请搜索缩小范围' : ''}。列表名称仅为上游声明，尚未检测。`;
  $('selection-count').textContent = `已选择 ${selected.size} 个${selected.size ? '：' + [...selected].join('、') : ''}`;
}
async function resetConnection() {
  const ticket = ++generation;
  invalidate(); available.clear(); selected.clear(); renderModels();
  $('report').hidden = true; $('fetch-status').textContent = ''; $('confirm-live').checked = false;
  if (!$('channel').value) return;
  try {
    const value = await api('models?channel_id=' + $('channel').value);
    if (ticket !== generation) return;
    value.models.forEach(model => available.add(model)); renderModels();
    $('fetch-status').textContent = value.ok ? `已载入上次获取的 ${value.models.length} 个模型${value.error ? '；最近获取未成功，可重新获取' : ''}。` : '尚未获取模型，可点击获取或手动输入。';
  } catch (e) { if (ticket === generation) error(e); }
}
async function history() {
  const data = await api('runs');
  $('history').replaceChildren(...data.runs.map(run => {
    const button = node('button', `${new Date(run.created_at * 1000).toLocaleString()} · ${run.mode === 'mock' ? 'Mock 演示' : '真实检测'} · ${states[run.state] || run.state}`, 'secondary');
    button.type = 'button'; button.onclick = () => show(run.id).catch(error); return button;
  }));
}
async function show(id) {
  const report = await api('runs/' + id);
  $('report').hidden = false;
  $('conclusion').replaceChildren(node('p', `${states[report.state] || report.state} · ${report.config.mode === 'mock' ? '本地 Mock 演示' : '真实上游检测'}`),
    ...report.warnings.map(value => node('p', value, 'error')));
  $('capabilities').replaceChildren(...report.capabilities.map(model => {
    const row = node('tr'); row.append(node('td', model.upstream_model));
    for (const protocol of Object.keys(metadata.protocols)) {
      const value = model.protocols[protocol], cell = node('td', value.label, value.status);
      cell.append(node('small', `普通：${value.details.basic.label} · 流式：${value.details.stream.label}`, 'hint'));
      row.append(cell);
    }
    const cell = node('td'), details = node('details'); details.append(node('summary', '工具与搜索'));
    for (const protocol of ['responses', 'anthropic']) details.append(node('p', `${model.protocols[protocol].name} 工具：${model.protocols[protocol].details.tool.label}`));
    details.append(node('p', 'Alpha Search：' + model.search.label)); cell.append(details); row.append(cell); return row;
  }));
  $('results').replaceChildren(...report.probes.map(probe => {
    const row = node('tr');
    const explanation = metadata.errors[probe.error_class] || probe.error_class || (probe.capability.status === 'supported' ? '本项通过' : probe.capability.label);
    for (const text of [`${probe.model} · ${probe.label}`, probe.capability.label, probe.http_status ?? '—', `${probe.total_ms ?? '—'} ms`, explanation]) row.append(node('td', String(text)));
    return row;
  }));
  for (const format of ['json', 'html']) $(`export-${format}`).href = `./api/runs/${id}/export/${format}`;
  if (active === id && report.state !== 'running') {
    active = null; clearInterval(poll); poll = null; busy(false); invalidate();
    $('message').textContent = '本轮模型与协议检测已结束。'; await history();
  }
}
$('channel').addEventListener('change', () => {
  $('temporary').hidden = Boolean($('channel').value); $('api-key').value = ''; resetConnection();
});
$('mode').addEventListener('change', () => {
  const live = $('mode').value === 'live'; $('live-confirm-label').hidden = !live;
  $('mode-hint').textContent = live ? '按所选模型实际发出请求，可能产生上游费用。临时密钥不保存到数据库或报告。' : '演示模式不会请求真实上游，结果仅供体验。';
  resetConnection();
});
for (const id of ['base-url', 'api-key']) $(id).addEventListener('input', resetConnection);
$('model-filter').addEventListener('input', renderModels);
$('confirm-live').addEventListener('change', invalidate);
$('add-model').addEventListener('click', () => {
  const model = $('manual-model').value.trim();
  if (!model) return;
  if (!selected.has(model) && selected.size >= metadata.max_models) { error(new Error('每批最多检测 5 个模型。')); return; }
  available.add(model); selected.add(model); $('manual-model').value = ''; invalidate(); renderModels();
});
$('fetch-models').addEventListener('click', async () => {
  error(); const ticket = ++generation;
  const body = {...connection(), api_key:$('api-key').value, mode:$('mode').value, confirm_live:$('confirm-live').checked};
  busy(true); $('fetch-status').textContent = '正在获取模型列表…';
  try {
    const data = await api('models', {method:'POST', body:JSON.stringify(body)});
    if (ticket !== generation) return;
    if (!data.ok) throw new Error('模型列表获取失败（' + data.error + '），可手动输入模型继续检测。');
    available.clear(); data.models.forEach(model => available.add(model));
    for (const model of selected) available.add(model);
    renderModels(); $('fetch-status').textContent = `${data.source === 'mock' ? 'Mock 演示列表' : '上游模型列表'}：${data.models.length} 个，尚未检测。`;
  } catch (e) { error(e); $('fetch-status').textContent = '本次获取失败，保留页面中原有模型。'; }
  finally { body.api_key = ''; busy(false); }
});
$('preview').addEventListener('click', async () => {
  error(); const ticket = generation;
  try {
    const current = plan(); busy(true);
    const value = await api('preview', {method:'POST', body:JSON.stringify(current)});
    if (ticket !== generation) return;
    preview = value;
    $('preview-list').replaceChildren(node('p', `${selected.size} 个模型，共 ${value.request_count} 次请求，每项 1 次。检测三种协议的普通/流式调用，以及 Responses/Claude 工具和 Alpha Search。最长 ${value.maximum_seconds} 秒。`));
  } catch (e) { invalidate(); error(e); }
  finally { busy(false); }
});
$('probe-form').addEventListener('submit', async event => {
  event.preventDefault(); error(); if (!preview) return;
  let body;
  try {
    body = {...plan(), api_key:$('api-key').value, mode:$('mode').value, confirm_live:$('confirm-live').checked, preview_fingerprint:preview.fingerprint};
    busy(true); const run = await api('runs', {method:'POST', body:JSON.stringify(body)});
    $('api-key').value = ''; active = run.id; busy(true); $('message').textContent = '正在逐模型检测协议…';
    await show(active); if (active) poll = setInterval(() => show(active).catch(error), 1000);
  } catch (e) { busy(false); error(e); }
  finally { if (body) body.api_key = ''; }
});
$('stop').addEventListener('click', async () => {
  if (!active) return;
  const id = active;
  try { await api(`runs/${id}/stop`, {method:'POST'}); await show(id); } catch (e) { error(e); }
});
(async () => {
  metadata = await api('meta'); channels = (await Workbench.api('/api/registry/channels')).channels.filter(channel => channel.enabled);
  for (const channel of channels) $('channel').append(new Option(Workbench.label(channel), channel.id));
  const query = new URLSearchParams(location.search);
  if (channels.some(channel => String(channel.id) === query.get('channel'))) {
    $('channel').value = query.get('channel'); $('temporary').hidden = true; await resetConnection();
  }
  if (query.get('model')) { available.add(query.get('model')); selected.add(query.get('model')); }
  renderModels(); await history(); $('message').textContent = '先获取或输入模型，再预览并开始检测。';
})().catch(error);
