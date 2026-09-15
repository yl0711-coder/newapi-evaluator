const $ = id => document.getElementById(id);
const names = {candidate:'候选端', reference:'参照端'};
const statusNames = {completed:'正常返回',truncated:'输出截断',unrecognized:'流格式不兼容',empty:'空响应',error:'请求异常',missing:'缺少数据',upstream_error:'上游生成失败',protocol_error:'流协议异常',incomplete_stream:'流未完整结束',incomplete_response:'回答未完成',refused:'上游拒答'};
const reportStatus = {completed:'已完成',canceled:'已停止',failed:'未完成'};
let metadata, references = [], controller = null, report = null;
const liveRows = new Map();
const duration = value => Number.isFinite(value) ? `${(value / 1000).toFixed(2)} 秒` : '不可用';
const signedDuration = value => Number.isFinite(value) ? `${value > 0 ? '+' : ''}${(value / 1000).toFixed(2)} 秒` : '不可比较';
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));

async function loadReferences() {
  references = await Workbench.picker($('reference-id'), $('reference-id').value);
  showReference();
}

function showReference() {
  const item = references.find(channel => channel.id === Number($('reference-id').value));
  $('reference-url').value = item?.base_url || '';
}

function applyPreset() {
  const preset = metadata.presets.find(item => item.id === $('preset').value);
  if (!preset) return;
  for (const side of Object.keys(names)) {
    $(`${side}-model`).value = preset.model;
    $(`${side}-protocol`).value = preset.protocol;
  }
  syncModelPolicy();
}

function syncModelPolicy() {
  let fixed = false;
  for (const side of Object.keys(names)) {
    const model = $(`${side}-model`).value.trim();
    const required = metadata.presets.find(item => item.model === model)?.required_protocol;
    const selector = $(`${side}-protocol`);
    selector.disabled = Boolean(required);
    if (required) { selector.value = required; fixed = true; }
  }
  $('model-policy').hidden = !fixed;
  const policy = metadata.presets.find(item => item.required_protocol);
  $('model-policy').textContent = policy ? `${policy.label} 固定使用 Responses，推理强度 ${policy.reasoning_effort}，每题最多 ${policy.max_output_tokens} 个输出 Token（含推理）。双端使用相同设置。` : '';
}

function responsesMetrics(value) {
  if (value.request_protocol !== 'responses') return [];
  return [['请求协议','Responses'],['最大输出 Token（含推理）',value.max_output_tokens ?? '未提供'],['推理强度',value.reasoning_effort || '上游默认'],['输入 Token',value.input_tokens ?? '未提供'],['推理 Token',value.reasoning_tokens ?? '未提供'],['响应状态',value.response_status || '未收到']];
}

function metric(label, value) {
  const item = Workbench.node('div');
  item.append(Workbench.node('small', label), Workbench.node('strong', value));
  return item;
}

function questionFor(questionId) {
  return report?.questions?.find(item => item.id === questionId) || metadata.questions.find(item => item.id === questionId) || {id:questionId,title:questionId,prompt:''};
}

function createLiveQuestion(event) {
  const question = questionFor(event.question_id);
  const card = Workbench.node('details','','panel evidence-card');
  card.open = true;
  const role = event.round_role === 'warmup' ? '预热，不计入结论' : '有效评测';
  card.append(Workbench.node('summary',`第 ${event.round} 轮 · ${question.title} · ${role}`));
  const body = Workbench.node('div','','evidence-body');
  const promptBox = Workbench.node('details','','evidence-block');
  promptBox.append(Workbench.node('summary','查看题目'),Workbench.node('pre',event.prompt || question.prompt || '题目内容不可用'));
  const sides = Workbench.node('div','','two');
  for (const side of Object.keys(names)) {
    const block = Workbench.node('section','','endpoint-evidence');
    const status = Workbench.node('p','等待响应','hint endpoint-status');
    const metrics = Workbench.node('div','','metric-grid');
    const answer = Workbench.node('pre','');
    const reasoningBox = Workbench.node('details','','evidence-block');
    const reasoning = Workbench.node('pre','上游未提供独立推理字段','empty-reasoning');
    reasoningBox.append(Workbench.node('summary','上游返回的推理内容'),reasoning);
    block.append(Workbench.node('h3',names[side]),status,metrics,answer,reasoningBox);
    sides.append(block);
    const response = {round:event.round,question_id:event.question_id,side,pair_id:event.pair_id,round_role:event.round_role,variant_id:event.variant_id || 'original',prompt:event.prompt || question.prompt || '',content:'',reasoning:''};
    report.responses.push(response);
    liveRows.set(`${event.round}:${event.question_id}:${side}`,{status,metrics,answer,reasoning,response});
  }
  body.append(promptBox,sides); card.append(body); $('results').append(card);
}

function handleEvent(event) {
  if (event.type === 'run_started') {
    report.run_id = event.run_id;
    report.warmup_rounds = event.warmup_rounds;
    for (const side of Object.keys(names)) if (event.protocols?.[side]) report[side].protocol = event.protocols[side];
  }
  if (event.type === 'question_started') {
    createLiveQuestion(event);
    const role = event.round_role === 'warmup' ? '预热轮' : '有效评测轮';
    $('run-status').textContent = `第 ${event.round} 轮（${role}），正在测试第 ${event.index} 题。`;
  }
  const row = liveRows.get(`${event.round}:${event.question_id}:${event.side}`);
  if (event.type === 'chunk' && row) {
    row.response.content += event.content || '';
    row.response.reasoning += event.reasoning || '';
    row.answer.textContent = row.response.content;
    if (row.response.reasoning) {
      row.reasoning.textContent = row.response.reasoning;
      row.reasoning.classList.remove('empty-reasoning');
    }
  }
  if (event.type === 'side_finished' && row) {
    report.measurements.push(event);
    row.status.textContent = event.ok ? '正常返回' : `${statusNames[event.status] || event.status} · ${event.error || '请检查返回内容'}`;
    row.status.classList.toggle('error',!event.ok);
    row.metrics.replaceChildren(
      metric('首次可见输出',duration(event.ttft_ms)),metric('首次答案正文',duration(event.first_answer_ms)),
      metric('完整响应',duration(event.total_ms)),metric('Token/s',event.speed_data_valid ? event.tokens_per_second : '不可用'),
      metric('输出 Token',Number.isFinite(event.output_tokens) ? event.output_tokens : '未提供'),metric('最大输出停顿',duration(event.maximum_output_pause_ms)),metric('结束标记',event.finish_reason || '未提供'), ...responsesMetrics(event).map(([label,value]) => metric(label,String(value)))
    );
  }
  if (event.type === 'run_finished') { report.status = 'completed'; $('run-status').textContent = '测试完成，正在生成成对报告。'; }
}

async function consume(response) {
  if (!response.ok) {
    const data = await response.json();
    throw new Error(Array.isArray(data.detail) ? data.detail.map(item => item.msg).join('；') : data.detail || '测试失败');
  }
  const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
  while (true) {
    const {value,done} = await reader.read(); if (done) break;
    buffer += decoder.decode(value,{stream:true}); const lines = buffer.split('\n'); buffer = lines.pop();
    for (const line of lines) if (line.trim()) handleEvent(JSON.parse(line));
  }
  buffer += decoder.decode(); if (buffer.trim()) handleEvent(JSON.parse(buffer));
  if (report.status !== 'completed') throw new Error('测试连接中断，当前保留的是部分结果');
}

function summaryCard(label,value) {
  const card = Workbench.node('div','','summary-card');
  card.append(Workbench.node('small',label),Workbench.node('strong',value));
  return card;
}

function renderOverview() {
  const summary = report.summary || {}, evidence = summary.evidence || {}, counts = summary.counts || {};
  $('headline').textContent = summary.overall?.label || '证据不足，建议人工复核';
  $('headline').dataset.status = summary.overall?.status || 'insufficient';
  $('summary-cards').replaceChildren(
    summaryCard('能力',summary.ability?.label || '查看逐题回答后人工判断'),
    summaryCard('速度',summary.speed?.label || '速度证据不足'),
    summaryCard('稳定性',summary.stability?.label || '稳定性证据不足'),
    summaryCard('有效题对',`${evidence.valid_pairs || 0} / ${evidence.evaluated_pairs || 0}`),
    summaryCard('候选端独有异常',String(counts.candidate_only || 0)),
    summaryCard('双端共同异常',String(counts.shared || 0)),
    summaryCard('测试轮次',`${evidence.test_rounds || report.rounds || 0} 轮（${evidence.warmup_rounds || 0} 轮预热）`),
    summaryCard('证据充分度',evidence.confidence || '低')
  );
  $('explanations').replaceChildren(...(summary.explanations || []).map(text => Workbench.node('li',text)));
  const identity = summary.model_identity || {};
  $('identity-summary').replaceChildren(...Object.keys(names).map(side => {
    const item = identity[side] || {}, card = Workbench.node('div','','identity-card');
    const actual = item.actual_models?.length ? item.actual_models.join('、') : '上游未提供';
    card.append(Workbench.node('h3',`${names[side]} · ${item.label || '身份未验证'}`),Workbench.node('p',`请求模型：${item.requested_model || report[side]?.model || '-'}`),Workbench.node('p',`响应模型：${actual}`));
    return card;
  }));
}

function renderPairs() {
  const pairs = (report.pairs || []).filter(pair => pair.round_role === 'evaluated');
  $('pair-summary').replaceChildren(...pairs.map(pair => {
    const row = Workbench.node('tr','','pair-row');
    const difference = pair.attribution !== 'normal' || !['耗时接近','不可比较'].includes(pair.speed_label);
    row.dataset.attribution = pair.attribution; row.dataset.speed = pair.speed_label; row.dataset.difference = String(difference);
    const both = pair.candidate_ok && pair.reference_ok ? '双端正常' : `候选：${statusNames[pair.candidate_status] || pair.candidate_status}；参照：${statusNames[pair.reference_status] || pair.reference_status}`;
    const judgement = pair.attribution === 'normal' ? pair.speed_label : pair.attribution_label;
    const ability = `人工查看（候选 ${pair.candidate_answer_chars} 字 / 参照 ${pair.reference_answer_chars} 字）`;
    for (const value of [`第 ${pair.round} 轮 · ${pair.title}`,both,ability,signedDuration(pair.first_answer_difference_ms),signedDuration(pair.total_difference_ms),pair.attribution_label,judgement]) {
      const cell = Workbench.node('td',value); if (value === judgement) cell.className = 'pair-judgement'; row.append(cell);
    }
    return row;
  }));
  applyPairFilter();
}

function applyPairFilter() {
  const filter = $('pair-filter').value;
  for (const row of $('pair-summary').querySelectorAll('tr')) {
    const visible = filter === 'all' || (filter === 'difference' && row.dataset.difference === 'true') ||
      (filter === 'candidate' && row.dataset.attribution === 'candidate') ||
      (filter === 'shared' && row.dataset.attribution === 'shared') ||
      (filter === 'speed' && !['耗时接近','不可比较'].includes(row.dataset.speed));
    row.dataset.hidden = String(!visible);
  }
}

function renderTechnicalEvidence() {
  const responses = new Map((report.responses || []).map(item => [`${item.round}:${item.question_id}:${item.side}`,item]));
  const measurements = new Map((report.measurements || []).map(item => [`${item.round}:${item.question_id}:${item.side}`,item]));
  const pairs = report.pairs || [];
  $('results').replaceChildren(...pairs.map(pair => {
    const card = Workbench.node('details','','panel evidence-card');
    card.open = pair.round_role === 'evaluated' && pair.attribution !== 'normal';
    const role = pair.round_role === 'warmup' ? '预热，不计入结论' : '有效评测';
    card.append(Workbench.node('summary',`第 ${pair.round} 轮 · ${pair.title} · ${role} · ${pair.attribution_label}`));
    const body = Workbench.node('div','','evidence-body'), question = questionFor(pair.question_id);
    const selectedPrompt = responses.get(`${pair.round}:${pair.question_id}:candidate`)?.prompt || question.prompt;
    const promptBox = Workbench.node('details','','evidence-block');
    promptBox.append(Workbench.node('summary','查看题目'),Workbench.node('pre',selectedPrompt || '题目内容不可用'));
    const sides = Workbench.node('div','','two');
    for (const side of Object.keys(names)) {
      const key = `${pair.round}:${pair.question_id}:${side}`, response = responses.get(key) || {}, measurement = measurements.get(key) || {};
      const block = Workbench.node('section','','endpoint-evidence');
      const metrics = Workbench.node('details','','evidence-block');
      const grid = Workbench.node('div','','metric-grid');
      grid.append(metric('首次可见输出',duration(measurement.ttft_ms)),metric('首次答案正文',duration(measurement.first_answer_ms)),metric('完整响应',duration(measurement.total_ms)),metric('Token/s',measurement.speed_data_valid ? measurement.tokens_per_second : '不可用'),metric('速度数据',measurement.speed_data_valid ? '有效' : measurement.speed_invalid_reason || '不可用'),metric('输出 Token',Number.isFinite(measurement.output_tokens) ? measurement.output_tokens : '未提供'),metric('最大输出停顿',duration(measurement.maximum_output_pause_ms)),metric('启动偏差',duration(pair.start_skew_ms)),metric('异常分类',measurement.error_category || '无'),metric('响应模型',measurement.actual_model || '未提供'),metric('上游请求 ID',measurement.upstream_request_id || '未提供'),metric('系统指纹',measurement.system_fingerprint || '未提供'),metric('响应格式',measurement.response_format || '未识别'), ...responsesMetrics(measurement).map(([label,value]) => metric(label,String(value))));
      metrics.append(Workbench.node('summary','技术指标'),grid);
      const answerBox = Workbench.node('details','','evidence-block'); answerBox.append(Workbench.node('summary','最终回答原文'),Workbench.node('pre',response.content || '上游未提供答案正文'));
      const reasoningBox = Workbench.node('details','','evidence-block'); const reasoning = Workbench.node('pre',response.reasoning || '上游未提供独立推理字段',response.reasoning ? '' : 'empty-reasoning'); reasoningBox.append(Workbench.node('summary','上游返回的推理内容'),reasoning);
      block.append(Workbench.node('h3',names[side]),Workbench.node('p',measurement.ok ? '正常返回' : `${statusNames[measurement.status] || measurement.status || '缺少数据'}${measurement.error ? ` · ${measurement.error}` : ''}`,'hint endpoint-status'),metrics,answerBox,reasoningBox); sides.append(block);
    }
    body.append(promptBox,sides); card.append(body); return card;
  }));
}

function renderReport() {
  $('report-panel').hidden = false;
  renderOverview(); renderPairs(); renderTechnicalEvidence();
  for (const id of ['export-html','export-technical-html','export-json']) $(id).disabled = false;
}

async function loadReportHistory() {
  const history = (await Workbench.api('./api/reports')).reports, root = $('report-history');
  $('report-history-count').textContent = history.length ? `已保存 ${history.length} 条 · 最多保留最近 30 条` : '暂无记录 · 最多保留最近 30 条';
  if (!history.length) { root.replaceChildren(Workbench.node('p','还没有准入报告记录。','empty')); return; }
  root.replaceChildren(...history.map(item => {
    const card = Workbench.node('article','','run-card'), title = Workbench.node('h3',`${item.candidate_model} 对比 ${item.reference_model}`);
    const when = new Date(item.reported_at || item.created_at * 1000).toLocaleString('zh-CN',{hour12:false});
    const facts = `${when} · ${reportStatus[item.status] || item.status}\n${item.overall_label || '旧版报告'}\n候选端独有异常：${item.candidate_only ?? '-'} · 共同异常：${item.shared ?? '-'} · 有效题对：${item.valid_pairs ?? '-'}`;
    const actions = Workbench.node('div','','actions'), view = Workbench.node('button','查看报告','secondary'), download = Workbench.node('button','下载 JSON','secondary'), remove = Workbench.node('button','删除','secondary');
    for (const button of [view,download,remove]) button.type = 'button';
    view.addEventListener('click',async()=>{ try { report = await Workbench.api(`./api/reports/${item.id}`); renderReport(); $('report-panel').scrollIntoView({behavior:'smooth'}); } catch (error) { $('error').textContent = error.message; } });
    download.addEventListener('click',async()=>{ try { Workbench.download(await Workbench.api(`./api/reports/${item.id}`),`admission-saved-${item.id}.json`); } catch (error) { $('error').textContent = error.message; } });
    remove.addEventListener('click',async()=>{ if (!confirm(`删除准入报告 #${item.id}？`)) return; try { await Workbench.api(`./api/reports/${item.id}`,{method:'DELETE'}); await loadReportHistory(); } catch (error) { $('error').textContent = error.message; } });
    actions.append(view,download,remove); card.append(title,Workbench.node('p',facts,'hint history-facts'),actions); return card;
  }));
}

async function persistReport() {
  if (!report || report.status === 'running') return;
  const {id:_id,...payload} = report;
  report = await Workbench.api('./api/reports',{method:'POST',body:JSON.stringify(payload)});
  renderReport(); await loadReportHistory();
}

function ordinaryPayload(source) {
  const endpoint = value => Object.fromEntries(['name','model','protocol'].filter(key=>value?.[key]!==undefined).map(key=>[key,value[key]]));
  return {version:source.version,created_at:source.created_at,status:source.status,rounds:source.rounds,warmup_rounds:source.warmup_rounds,candidate:endpoint(source.candidate),reference:endpoint(source.reference),summary:source.summary,pairs:(source.pairs || []).filter(pair => pair.round_role === 'evaluated').map(({pair_id,...pair}) => pair)};
}

function reportHtml(source, technical) {
  const summary = source.summary || {}, evidence = summary.evidence || {}, counts = summary.counts || {};
  const cards = [['能力',summary.ability?.label],['速度',summary.speed?.label],['稳定性',summary.stability?.label],['有效题对',`${evidence.valid_pairs || 0} / ${evidence.evaluated_pairs || 0}`],['候选端独有异常',counts.candidate_only || 0],['共同异常',counts.shared || 0]].map(([label,value])=>`<div class="card"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value ?? '-')}</strong></div>`).join('');
  const pairRows = (source.pairs || []).filter(pair=>pair.round_role==='evaluated').map(pair=>`<tr><td>${escapeHtml(`第 ${pair.round} 轮 · ${pair.title}`)}</td><td>${escapeHtml(pair.attribution_label)}</td><td>${escapeHtml(pair.speed_label)}</td><td>${escapeHtml('人工查看双方回答')}</td></tr>`).join('');
  let details = '';
  if (technical) {
    const responses = new Map((source.responses || []).map(item=>[`${item.round}:${item.question_id}:${item.side}`,item]));
    const measurements = new Map((source.measurements || []).map(item=>[`${item.round}:${item.question_id}:${item.side}`,item]));
    details = (source.pairs || []).map(pair=>`<details><summary>${escapeHtml(`第 ${pair.round} 轮 · ${pair.title} · ${pair.round_role === 'warmup' ? '预热' : '有效评测'}`)}</summary><div class="two">${Object.keys(names).map(side=>{
      const key = `${pair.round}:${pair.question_id}:${side}`, response = responses.get(key) || {}, measurement = measurements.get(key) || {};
      const speed = measurement.speed_data_valid ? measurement.tokens_per_second : `不可用${measurement.speed_invalid_reason ? `（${measurement.speed_invalid_reason}）` : ''}`;
      return `<section><h3>${names[side]}</h3><p>状态：${escapeHtml(measurement.ok ? '正常返回' : statusNames[measurement.status] || measurement.status || '缺少数据')}</p><p>首次可见输出：${escapeHtml(duration(measurement.ttft_ms))}；首次答案正文：${escapeHtml(duration(measurement.first_answer_ms))}；完整响应：${escapeHtml(duration(measurement.total_ms))}；Token/s：${escapeHtml(speed)}</p>${(measurement.request_protocol === 'responses' ? [['实际输出 Token',measurement.output_tokens ?? '未提供'], ...responsesMetrics(measurement)] : []).map(([label,value]) => `<p>${escapeHtml(label)}：${escapeHtml(value)}</p>`).join('')}<h4>最终回答原文</h4><pre>${escapeHtml(response.content || '上游未提供答案正文')}</pre><h4>上游返回的推理内容</h4><pre>${escapeHtml(response.reasoning || '上游未提供独立推理字段')}</pre></section>`;
    }).join('')}</div></details>`).join('');
  }
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>准入测试报告</title><style>body{max-width:1100px;margin:32px auto;padding:20px;font:16px/1.6 sans-serif;color:#172c29}.headline{padding:18px;background:#e5eee7;border-left:5px solid #0a675d}.cards,.two{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.cards{grid-template-columns:repeat(3,1fr);margin:18px 0}.card,details{border:1px solid #d7ded5;border-radius:8px;padding:14px;margin:12px 0}.card small,.card strong{display:block}table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #d7ded5;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5ef;padding:12px}@media(max-width:700px){.cards,.two{grid-template-columns:1fr}}</style><h1>准入测试报告</h1><p>${escapeHtml(source.created_at)} · ${escapeHtml(reportStatus[source.status] || source.status)}</p><div class="headline"><h2>${escapeHtml(summary.overall?.label || '证据不足')}</h2><p>能力由人员阅读逐题回答后判断，系统不自动给出准入决定。</p></div><div class="cards">${cards}</div><ul>${(summary.explanations || []).map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ul><h2>逐题摘要</h2><table><thead><tr><th>题目</th><th>异常归因</th><th>速度</th><th>能力</th></tr></thead><tbody>${pairRows}</tbody></table>${details}</html>`;
}

$('compare-form').addEventListener('submit',async event=>{
  event.preventDefault(); if (controller) return;
  syncModelPolicy();
  const payload = {candidate:{base_url:$('candidate-url').value.trim(),api_key:$('candidate-key').value.trim(),model:$('candidate-model').value.trim(),protocol:$('candidate-protocol').value},reference:{channel_id:Number($('reference-id').value),model:$('reference-model').value.trim(),protocol:$('reference-protocol').value},rounds:Number($('rounds').value)};
  if (payload.candidate.model !== payload.reference.model) { $('error').textContent='成对测试要求候选端与参照端使用相同的请求模型名。'; return; }
  const reference = references.find(channel=>channel.id===payload.reference.channel_id);
  report = {version:2,run_id:'',created_at:new Date().toISOString(),status:'running',rounds:payload.rounds,warmup_rounds:payload.rounds>=2?1:0,candidate:{base_url:payload.candidate.base_url,model:payload.candidate.model,protocol:payload.candidate.protocol},reference:{...payload.reference,name:reference?.name,base_url:reference?.base_url,multiplier:reference?.multiplier},questions:metadata.questions,measurements:[],responses:[],pairs:[],summary:{}};
  liveRows.clear(); $('results').replaceChildren(); $('pair-summary').replaceChildren(); $('report-panel').hidden=false; $('headline').textContent='测试进行中，完成后生成综合报告。'; $('summary-cards').replaceChildren(); $('explanations').replaceChildren(); $('identity-summary').replaceChildren(); $('error').textContent='';
  controller = new AbortController(); $('setup').disabled=true; $('start').disabled=true; $('stop').hidden=false; for (const id of ['export-html','export-technical-html','export-json']) $(id).disabled=true;
  try { await consume(await fetch('./api/compare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:controller.signal})); report.status='completed'; }
  catch (error) { report.status=error.name==='AbortError'?'canceled':'failed'; $('run-status').textContent=error.name==='AbortError'?'已停止，正在保存部分结果。':'本轮未完成，正在保存已有证据。'; if(error.name!=='AbortError') $('error').textContent=error.message; }
  finally { payload.candidate.api_key=''; try { await persistReport(); $('run-status').textContent=report.status==='completed'?'本轮测试完成，报告已生成。':`${reportStatus[report.status]}，部分报告已保存。`; $('report-panel').scrollIntoView({behavior:'smooth',block:'start'}); } catch(error) { $('error').textContent=`${$('error').textContent?`${$('error').textContent}；`:''}报告历史保存失败：${error.message}`; } controller=null; $('setup').disabled=false; $('start').disabled=false; $('stop').hidden=true; }
});

$('extract').addEventListener('click',async()=>{ const text=$('candidate-import').value.trim(); if(!text)return; $('extract').disabled=true; try{const data=await Workbench.api('./api/extract-channel',{method:'POST',body:JSON.stringify({text})}); if(data.base_url)$('candidate-url').value=data.base_url;if(data.api_key)$('candidate-key').value=data.api_key;$('candidate-import').value='';$('extract-status').textContent=data.has_url&&data.has_key?'已提取，未保存到渠道库。':'信息不完整，请补充地址或密钥。';}catch(error){$('extract-status').textContent=error.message;}finally{$('extract').disabled=false;}});
for (const side of Object.keys(names)) $(`${side}-model`).addEventListener('input',syncModelPolicy);
$('stop').addEventListener('click',()=>controller?.abort()); $('preset').addEventListener('change',applyPreset); $('reference-id').addEventListener('change',showReference); $('pair-filter').addEventListener('change',applyPairFilter);
$('refresh-reference').addEventListener('click',()=>loadReferences().catch(error=>$('error').textContent=error.message)); $('refresh-reports').addEventListener('click',()=>loadReportHistory().catch(error=>$('error').textContent=error.message));
$('export-json').addEventListener('click',()=>report&&Workbench.download(report,`admission-technical-${Date.now()}.json`));
$('export-html').addEventListener('click',async()=>{ if(!report)return; try{const source=report.id?await Workbench.api(`./api/reports/${report.id}/ordinary`):ordinaryPayload(report);Workbench.download(reportHtml(source,false),`admission-summary-${Date.now()}.html`,'text/html');}catch(error){$('error').textContent=error.message;} });
$('export-technical-html').addEventListener('click',()=>report&&Workbench.download(reportHtml(report,true),`admission-technical-${Date.now()}.html`,'text/html'));

(async()=>{ metadata=await Workbench.api('./api/meta'); $('preset').replaceChildren(new Option('手动填写模型','')); for(const preset of metadata.presets)$('preset').append(new Option(`${preset.provider} · ${preset.label}`,preset.id)); await Promise.all([loadReferences(),loadReportHistory()]); })().catch(error=>{$('error').textContent=error.message;});
