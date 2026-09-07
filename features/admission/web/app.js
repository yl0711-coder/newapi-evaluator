const $ = id => document.getElementById(id);
const names = {candidate:'候选端', reference:'参照端'};
let metadata, references = [], controller = null, report = null;
const rows = new Map();
const reportStatus = {completed:'已完成',canceled:'已停止',failed:'未完成'};
const duration = v => Number.isFinite(v) ? `${(v / 1000).toFixed(2)} s` : '-';
function percentile(values, p) { const sorted = values.filter(Number.isFinite).sort((a,b) => a-b); return sorted.length ? sorted[Math.ceil(sorted.length*p)-1] : null; }
async function loadReferences() {
  references = await Workbench.picker($('reference-id'), $('reference-id').value);
  showReference();
}
function showReference() { const item = references.find(x => x.id === Number($('reference-id').value)); $('reference-url').value = item?.base_url || ''; }
function applyPreset() {
  const preset = metadata.presets.find(x => x.id === $('preset').value);
  if (!preset) return;
  for (const side of Object.keys(names)) { $(`${side}-model`).value = preset.model; $(`${side}-protocol`).value = preset.protocol; }
}
function summary() {
  const summary = {};
  for (const side of Object.keys(names)) {
    const good = report?.measurements.filter(x => x.side === side && x.ok) || [];
    summary[side] = {completed:good.length, expected:report ? metadata.questions.length*report.rounds : 0,
      ttft_p50_ms:percentile(good.map(x=>x.ttft_ms),.5), ttft_p95_ms:percentile(good.map(x=>x.ttft_ms),.95),
      total_p50_ms:percentile(good.map(x=>x.total_ms),.5), total_p95_ms:percentile(good.map(x=>x.total_ms),.95),
      tokens_per_second_p50:percentile(good.map(x=>x.tokens_per_second),.5)};
  }
  return summary;
}
function renderSummary() {
  const data = summary();
  $('summary').replaceChildren(...Object.entries(data).map(([side,s]) => {
    const tr = document.createElement('tr');
    for (const value of [names[side],`${s.completed} / ${s.expected}`,`${duration(s.ttft_p50_ms)} / ${duration(s.ttft_p95_ms)}`,`${duration(s.total_p50_ms)} / ${duration(s.total_p95_ms)}`,s.tokens_per_second_p50 ?? '-']) tr.append(Workbench.node('td',value));
    return tr;
  }));
  if (report) report.summary = data;
}
function createQuestion(event) {
  const question = metadata.questions.find(x => x.id === event.question_id);
  const card = Workbench.node('details','','panel run-card'); card.open = true;
  card.append(Workbench.node('summary',`第 ${event.round} 轮 · ${question.title}`));
  const prompt = Workbench.node('details'); prompt.append(Workbench.node('summary','查看题目'),Workbench.node('pre',question.prompt)); card.append(prompt);
  const sides = Workbench.node('div','','two');
  for (const side of Object.keys(names)) {
    const block = Workbench.node('section'); block.append(Workbench.node('h3',names[side]));
    const status = Workbench.node('p','等待响应','hint');
    const metrics = Workbench.node('div','','metric-grid');
    const answer = Workbench.node('pre','');
    const reasoningBox = Workbench.node('details'); const reasoning = Workbench.node('pre',''); reasoningBox.append(Workbench.node('summary','独立推理文本'),reasoning);
    block.append(status,metrics,answer,reasoningBox); sides.append(block);
    const response = {round:event.round,question_id:event.question_id,side,content:'',reasoning:''};
    report.responses.push(response); rows.set(`${event.round}:${event.question_id}:${side}`,{status,metrics,answer,reasoning,response});
  }
  card.append(sides); $('results').append(card);
}
function handleEvent(event) {
  if (event.type === 'question_started') {
    createQuestion(event); $('run-status').textContent = `第 ${event.round} 轮，正在测试第 ${event.index} 题。`;
  }
  const row = rows.get(`${event.round}:${event.question_id}:${event.side}`);
  if (event.type === 'chunk' && row) {
    row.response.content += event.content || ''; row.response.reasoning += event.reasoning || '';
    row.answer.textContent = row.response.content; row.reasoning.textContent = row.response.reasoning;
  }
  if (event.type === 'side_finished' && row) {
    report.measurements.push(event); row.status.textContent = event.ok ? '响应完成' : `${event.status} · ${event.error || '请检查返回内容'}`;
    row.status.classList.toggle('error',!event.ok);
    for (const [label,value] of [['首包',duration(event.ttft_ms)],['首答',duration(event.first_answer_ms)],['总耗时',duration(event.total_ms)],['Token/s',event.tokens_per_second ?? '-'],['输出 Token',event.output_tokens ?? '-'],['结束标记',event.finish_reason || '-']]) {
      const metric = Workbench.node('div'); metric.append(Workbench.node('small',label),Workbench.node('strong',value)); row.metrics.append(metric);
    }
    renderSummary();
  }
  if (event.type === 'run_finished') { report.status = 'completed'; $('run-status').textContent = '本轮测试完成，请结合速度指标和逐题回答判断。'; }
}
async function consume(response) {
  if (!response.ok) { const data = await response.json(); throw new Error(Array.isArray(data.detail) ? data.detail.map(x => x.msg).join('；') : data.detail || '测试失败'); }
  const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
  while (true) { const {value,done} = await reader.read(); if (done) break;
    buffer += decoder.decode(value,{stream:true}); const lines = buffer.split('\n'); buffer = lines.pop();
    for (const line of lines) if (line.trim()) handleEvent(JSON.parse(line));
  }
  buffer += decoder.decode(); if (buffer.trim()) handleEvent(JSON.parse(buffer));
  if (report.status !== 'completed') throw new Error('测试连接中断，当前保留的是部分结果');
}
async function loadReportHistory() {
  const history = (await Workbench.api('./api/reports')).reports;
  const root = $('report-history');
  if (!history.length) { root.replaceChildren(Workbench.node('p','还没有准入报告记录。','empty')); return; }
  root.replaceChildren(...history.map(item => {
    const card = Workbench.node('article','','run-card');
    const title = Workbench.node('h3',`${item.candidate_model} 对比 ${item.reference_model}`);
    const when = new Date(item.reported_at || item.created_at * 1000).toLocaleString('zh-CN',{hour12:false});
    const detail = Workbench.node('p',`${when} · ${reportStatus[item.status] || item.status}\n候选端：${item.candidate_url}\n参照端：${item.reference_name}`,'hint');
    detail.style.whiteSpace = 'pre-line';
    const actions = Workbench.node('div','','actions');
    const download = Workbench.node('button','下载 JSON','secondary'); download.type = 'button';
    download.addEventListener('click',async()=>{
      try { const saved = await Workbench.api(`./api/reports/${item.id}`); Workbench.download(saved,`admission-saved-${item.id}.json`); }
      catch (e) { $('error').textContent = e.message; }
    });
    const remove = Workbench.node('button','删除','secondary'); remove.type = 'button';
    remove.addEventListener('click',async()=>{
      if (!confirm(`删除准入报告 #${item.id}？`)) return;
      try { await Workbench.api(`./api/reports/${item.id}`,{method:'DELETE'}); await loadReportHistory(); }
      catch (e) { $('error').textContent = e.message; }
    });
    actions.append(download,remove); card.append(title,detail,actions); return card;
  }));
}
async function persistReport() {
  if (!report || report.status === 'running') return;
  const {id: _id, ...payload} = report;
  const saved = await Workbench.api('./api/reports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  report.id = saved.id;
  await loadReportHistory();
}
$('extract').addEventListener('click', async () => {
  const text = $('candidate-import').value.trim(); if (!text) return;
  $('extract').disabled = true;
  try { const data = await Workbench.api('./api/extract-channel',{method:'POST',body:JSON.stringify({text})});
    if (data.base_url) $('candidate-url').value = data.base_url;
    if (data.api_key) $('candidate-key').value = data.api_key;
    $('candidate-import').value = ''; $('extract-status').textContent = data.has_url && data.has_key ? '已提取，未保存到渠道库。' : '信息不完整，请补充地址或密钥。';
  } catch (e) { $('extract-status').textContent = e.message; } finally { $('extract').disabled = false; }
});
$('compare-form').addEventListener('submit', async event => {
  event.preventDefault(); if (controller) return;
  const payload = {candidate:{base_url:$('candidate-url').value.trim(),api_key:$('candidate-key').value.trim(),model:$('candidate-model').value.trim(),protocol:$('candidate-protocol').value},
    reference:{channel_id:Number($('reference-id').value),model:$('reference-model').value.trim(),protocol:$('reference-protocol').value},rounds:Number($('rounds').value)};
  const reference = references.find(c => c.id === payload.reference.channel_id);
  report = {version:1,created_at:new Date().toISOString(),status:'running',rounds:payload.rounds,
    candidate:{base_url:payload.candidate.base_url,model:payload.candidate.model,protocol:payload.candidate.protocol},
    reference:{...payload.reference,name:reference?.name,base_url:reference?.base_url,multiplier:reference?.multiplier},questions:metadata.questions,measurements:[],responses:[]};
  rows.clear(); $('results').replaceChildren(); renderSummary(); $('error').textContent = '';
  controller = new AbortController(); $('setup').disabled = true; $('start').disabled = true; $('stop').hidden = false;
  $('export-json').disabled = true; $('export-html').disabled = true;
  try { await consume(await fetch('./api/compare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:controller.signal})); }
  catch (e) { report.status = e.name === 'AbortError' ? 'canceled' : 'failed'; $('run-status').textContent = e.name === 'AbortError' ? '已停止，当前为部分结果。' : '本轮未完成。'; if (e.name !== 'AbortError') $('error').textContent = e.message; }
  finally { payload.candidate.api_key = '';
    try { await persistReport(); } catch (e) { $('error').textContent = `${$('error').textContent ? `${$('error').textContent}；` : ''}报告历史保存失败：${e.message}`; }
    controller = null; $('setup').disabled = false; $('start').disabled = false; $('stop').hidden = true;
    $('export-json').disabled = false; $('export-html').disabled = false; }
});
$('stop').addEventListener('click',()=>controller?.abort()); $('preset').addEventListener('change',applyPreset);
$('reference-id').addEventListener('change',showReference);
$('refresh-reference').addEventListener('click',()=>loadReferences().catch(e=>$('error').textContent=e.message));
$('refresh-reports').addEventListener('click',()=>loadReportHistory().catch(e=>$('error').textContent=e.message));
$('export-json').addEventListener('click',()=>report && Workbench.download(report,`admission-${Date.now()}.json`));
$('export-html').addEventListener('click',()=>{
  if (!report) return;
  const escape = v => String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const body = report.responses.map(r=>`<section><h2>第 ${r.round} 轮 · ${escape(r.question_id)} · ${names[r.side]}</h2><pre>${escape(r.content)}</pre><details><summary>独立推理</summary><pre>${escape(r.reasoning)}</pre></details></section>`).join('');
  Workbench.download(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>准入测试报告</title><style>body{max-width:1100px;margin:32px auto;padding:20px;font:16px/1.6 sans-serif}pre{white-space:pre-wrap;overflow-wrap:anywhere}section{border-top:1px solid #ccc;margin-top:24px}td,th{padding:12px;text-align:left}</style><h1>准入测试报告</h1><p>${escape(report.created_at)} · ${escape(report.status)}</p><p>候选端：${escape(report.candidate.base_url)} · ${escape(report.candidate.model)}</p><p>参照端：${escape(report.reference.name)} · ${escape(report.reference.model)}</p><table>${$('summary').parentElement.innerHTML}</table>${body}</html>`,`admission-${Date.now()}.html`,'text/html');
});
(async()=>{
  metadata = await Workbench.api('./api/meta'); $('preset').replaceChildren(new Option('手动填写模型',''));
  for (const p of metadata.presets) $('preset').append(new Option(`${p.provider} · ${p.label}`,p.id));
  await Promise.all([loadReferences(),loadReportHistory()]); renderSummary();
})().catch(e=>{$('error').textContent=e.message;});
