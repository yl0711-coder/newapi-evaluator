const $ = id => document.getElementById(id);
const names = {candidate:'候选端', reference:'参照端'};
const statusNames = {completed:'正常返回',truncated:'输出截断',unrecognized:'流格式不兼容',empty:'空响应',error:'请求异常',missing:'缺少数据',upstream_error:'上游生成失败',protocol_error:'流协议异常',incomplete_stream:'流未完整结束',incomplete_response:'回答未完成',refused:'上游拒答'};
const reportStatus = {completed:'已完成',canceled:'已停止，未完成快照',failed:'未完成快照',running:'进行中快照'};
statusNames.unknown = '结果未知，未重发';
let metadata, references = [], report = null, selectedJobId = null, queueGeneration = 0;
let renderedReport = '';
const jobStatus = {queued:'等待测试',running:'测试进行中',waiting_credentials:'等待补充临时 Key',succeeded:'已完成',completed:'已完成',failed:'未完成',cancelled:'已停止',canceled:'已停止'};
const duration = value => Number.isFinite(value) ? `${(value / 1000).toFixed(2)} 秒` : '不可用';
const signedDuration = value => Number.isFinite(value) ? `${value > 0 ? '+' : ''}${(value / 1000).toFixed(2)} 秒` : '不可比较';
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const endpointText = value => `${value?.name || '未记录渠道名称'}${value?.channel_id ? ` · 渠道 #${value.channel_id}` : ''} · ${value?.model || '未记录模型'} · ${value?.protocol || '未记录协议'}`;
const filenamePart = value => String(value || '未记录渠道名称').normalize('NFKC').replace(/[^\p{L}\p{N}._-]+/gu,'-').replace(/^\.+/,'').slice(0,48) || 'channel';
const exportFilename = (source, kind, extension) => `admission-${filenamePart(source.candidate?.name)}-vs-${filenamePart(source.reference?.name)}-${kind}-${source.id || Date.now()}.${extension}`;
let precheckGeneration = 0, precheckTimer;

function schedulePrecheck() { clearTimeout(precheckTimer); precheckGeneration++; precheckTimer = setTimeout(() => void loadPrecheck(),200); }
async function loadPrecheck() {
  const generation=precheckGeneration;
  const values={model:$('candidate-model').value.trim(),candidate_protocol:$('candidate-protocol').value,reference_model:$('reference-model').value.trim(),reference_protocol:$('reference-protocol').value,reference_channel_id:$('reference-id').value};
  if(!values.model || !values.reference_model || !values.reference_channel_id){$('token-precheck').textContent='填写双方模型、协议与参照渠道后检查 Token 可信条件。';return;}
  try { const result=await Workbench.api(`./api/token-precheck?${new URLSearchParams(values)}`); if(generation===precheckGeneration)$('token-precheck').textContent=result.label; }
  catch { if(generation===precheckGeneration)$('token-precheck').textContent='Token 可信条件检查暂时不可用；创建后会再次核验，六题测试仍可执行。'; }
}

function jobDescription(job, jobs=[]) {
  const state=job.state || job.status, head=jobs.find(item=>['queued','running','waiting_credentials'].includes(item.state || item.status));
  let text=job.cancel_requested && state==='running' ? '停止中，等待已发送请求关闭并保存结果' : jobStatus[state] || state;
  if(state==='queued')text+=` · 排队第 ${job.queue_position} 位`;
  if(state==='waiting_credentials' && job.queue_position===1)text+=` · 队首暂停，后续 ${jobs.filter(item=>['queued','waiting_credentials'].includes(item.state || item.status) && item.id!==job.id).length} 个任务等待；请补充原 Key 或取消本任务`;
  else if(['queued','waiting_credentials'].includes(state) && head?.state==='waiting_credentials')text+=` · 等待队首 #${head.id}（${head.candidate?.name || '未记录渠道名称'}）补充原 Key`;
  const p=job.progress;
  if(p)text+=`\n已确认 ${p.confirmed || 0} · 结果未知 ${p.unknown || 0}（不会重发） · 无需执行 ${p.not_needed || 0} · 尚未发送 ${p.not_sent || 0} · 发送中 ${p.sending || 0} / 共 ${p.total_steps} 步`;
  if(p?.current_steps?.length)text+=`\n当前${p.phase==='token' ? ' Token 专项' : '题目/轮次'}：${p.current_steps.map(step=>{const [number,question,side]=step.split(':');return number==='token' ? `${names[question]} · 第 ${Number(side)+1} 次对照` : `第 ${number} 轮 · ${question} · ${names[side]}`;}).join('、')}`;
  if(job.error)text+=`\n原因：${job.error}`;
  if(job.report_removed)text+='\n报告正文已删除或超过保留上限。';
  return text;
}

async function loadReferences() {
  references = await Workbench.picker($('reference-id'), $('reference-id').value);
  showReference();
}

function showReference() {
  const item = references.find(channel => channel.id === Number($('reference-id').value));
  $('reference-url').value = item?.base_url || '';
  schedulePrecheck();
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
  schedulePrecheck();
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

function summaryCard(label,value) {
  const card = Workbench.node('div','','summary-card');
  card.append(Workbench.node('small',label),Workbench.node('strong',value));
  return card;
}

function renderOverview() {
  const summary = report.summary || {}, evidence = summary.evidence || {}, counts = summary.counts || {};
  const token = report.token_usage || summary.token_usage;
  $('report-channels').textContent=`候选：${endpointText(report.candidate)}\n参照：${endpointText(report.reference)}`;
  $('overview-title').textContent=evidence.coverage_complete ? '本轮对照结论' : '已完成部分与当前证据';
  $('report-completion').textContent=evidence.coverage_complete ? `已完整覆盖 ${evidence.covered_pairs} / ${evidence.planned_pairs} 个评测题对。` : `当前为未完成快照，已覆盖 ${evidence.covered_pairs || 0} / ${evidence.planned_pairs || 0} 个评测题对；下载内容也仅代表当前进度。`;
  $('token-usage-conclusion').textContent = token?.label || (token ? 'Token 专项证据不足，实际计费待核实。' : '此报告尚无 Token 专项结果。');
  $('token-usage-details').textContent = token ? JSON.stringify(token,null,2) : '尚无检测证据。';
  $('token-usage-evidence').hidden = !token;
  $('headline').textContent = summary.overall?.label || '证据不足，建议人工复核';
  $('headline').dataset.status = summary.overall?.status || 'insufficient';
  $('summary-cards').replaceChildren(
    summaryCard('能力',summary.ability?.label || '查看逐题回答后人工判断'),
    summaryCard('速度',summary.speed?.label || '速度证据不足'),
    summaryCard('稳定性',summary.stability?.label || '稳定性证据不足'),
    summaryCard('有效题对 / 计划',`${evidence.valid_pairs || 0} / ${evidence.planned_pairs || 0}`),
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
    card.append(Workbench.node('h3',`${names[side]} · ${report[side]?.name || '未记录渠道名称'} · ${item.label || '身份未验证'}`),Workbench.node('p',`请求模型：${item.requested_model || report[side]?.model || '-'}`),Workbench.node('p',`响应模型：${actual}`));
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
      block.append(Workbench.node('h3',`${names[side]} · ${endpointText(report[side])}`),Workbench.node('p',measurement.ok ? '正常返回' : `${statusNames[measurement.status] || measurement.status || '缺少数据'}${measurement.error ? ` · ${measurement.error}` : ''}`,'hint endpoint-status'),metrics,answerBox,reasoningBox); sides.append(block);
    }
    body.append(promptBox,sides); card.append(body); return card;
  }));
}

function renderReport() {
  renderedReport = JSON.stringify(report);
  $('report-panel').hidden = false;
  renderOverview(); renderPairs(); renderTechnicalEvidence();
  for (const id of ['export-html','export-technical-html','export-json']) $(id).disabled = !report;
}

async function loadReportHistory() {
  const history = (await Workbench.api('./api/reports')).reports, root = $('report-history');
  $('report-history-count').textContent = history.length ? `已保存 ${history.length} 条 · 最多保留最近 30 条` : '暂无记录 · 最多保留最近 30 条';
  if (!history.length) { root.replaceChildren(Workbench.node('p','还没有准入报告记录。','empty')); return; }
  root.replaceChildren(...history.map(item => {
    const card = Workbench.node('article','','run-card'), title = Workbench.node('h3',`${endpointText(item.candidate || {name:item.candidate_name,model:item.candidate_model})} 对比 ${endpointText(item.reference || {name:item.reference_name,model:item.reference_model})}`);
    const when = new Date(item.reported_at || item.created_at * 1000).toLocaleString('zh-CN',{hour12:false});
    const facts = `${when} · ${reportStatus[item.status] || item.status}\n${item.overall_label || '旧版报告'}\n候选端独有异常：${item.candidate_only ?? '-'} · 共同异常：${item.shared ?? '-'} · 有效题对：${item.valid_pairs ?? '-'}`;
    const actions = Workbench.node('div','','actions'), view = Workbench.node('button','查看报告','secondary'), download = Workbench.node('button','下载 JSON','secondary'), remove = Workbench.node('button','删除','secondary');
    for (const button of [view,download,remove]) button.type = 'button';
    view.addEventListener('click',async()=>{ try { selectedJobId = null; queueGeneration++; const generation=queueGeneration; const saved=await Workbench.api(`./api/reports/${item.id}`); if(generation!==queueGeneration)return; report=saved; $('stop').hidden=true; $('run-status').textContent=`历史报告 #${item.id} · ${endpointText(saved.candidate)} → ${endpointText(saved.reference)}`; renderReport(); $('report-panel').scrollIntoView({behavior:'smooth'}); } catch (error) { $('error').textContent = error.message; } });
    download.addEventListener('click',async()=>{ try { const saved=await Workbench.api(`./api/reports/${item.id}`); Workbench.download(saved,exportFilename(saved,'technical','json')); } catch (error) { $('error').textContent = error.message; } });
    remove.addEventListener('click',async()=>{ if (!confirm(`删除准入报告 #${item.id}？`)) return; try { await Workbench.api(`./api/reports/${item.id}`,{method:'DELETE'}); if(report?.id===item.id){report=null;renderedReport='';$('report-panel').hidden=true;$('run-status').textContent=`报告 #${item.id} 已删除。`;selectedJobId=null;queueGeneration++;} await loadReportHistory(); } catch (error) { $('error').textContent = error.message; } });
    actions.append(view,download,remove); card.append(title,Workbench.node('p',facts,'hint history-facts'),actions); return card;
  }));
}

function ordinaryPayload(source) {
  const endpoint = value => ({name:value?.name || '未记录渠道名称',...Object.fromEntries(['channel_id','model','protocol'].filter(key=>value?.[key]!==undefined).map(key=>[key,value[key]]))});
  const token = source.token_usage ? Object.fromEntries(['status','conclusion','label','severity','billing_status','reason'].filter(key=>source.token_usage[key]!==undefined).map(key=>[key,source.token_usage[key]])) : undefined;
  return {version:source.version,created_at:source.created_at,status:source.status,rounds:source.rounds,warmup_rounds:source.warmup_rounds,candidate:endpoint(source.candidate),reference:endpoint(source.reference),summary:source.summary,token_usage:token,pairs:(source.pairs || []).filter(pair => pair.round_role === 'evaluated').map(({pair_id,...pair}) => pair)};
}

function reportHtml(source, technical) {
  const summary = source.summary || {}, evidence = summary.evidence || {}, counts = summary.counts || {};
  const cards = [['能力',summary.ability?.label],['速度',summary.speed?.label],['稳定性',summary.stability?.label],['有效题对',`${evidence.valid_pairs || 0} / ${evidence.planned_pairs || 0}`],['候选端独有异常',counts.candidate_only || 0],['共同异常',counts.shared || 0]].map(([label,value])=>`<div class="card"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value ?? '-')}</strong></div>`).join('');
  const pairRows = (source.pairs || []).filter(pair=>pair.round_role==='evaluated').map(pair=>`<tr><td>${escapeHtml(`第 ${pair.round} 轮 · ${pair.title}`)}</td><td>${escapeHtml(pair.attribution_label)}</td><td>${escapeHtml(pair.speed_label)}</td><td>${escapeHtml('人工查看双方回答')}</td></tr>`).join('');
  let details = '';
  const token = source.token_usage || summary.token_usage;
  const tokenSection = token ? `<h2>输入 Token 专项结论</h2><p>${escapeHtml(token.label || '证据不足，实际计费待核实。')}</p>${technical ? `<details><summary>Token 检测证据</summary><pre>${escapeHtml(JSON.stringify(token,null,2))}</pre></details>` : ''}` : '';
  if (technical) {
    const responses = new Map((source.responses || []).map(item=>[`${item.round}:${item.question_id}:${item.side}`,item]));
    const measurements = new Map((source.measurements || []).map(item=>[`${item.round}:${item.question_id}:${item.side}`,item]));
    details = (source.pairs || []).map(pair=>`<details><summary>${escapeHtml(`第 ${pair.round} 轮 · ${pair.title} · ${pair.round_role === 'warmup' ? '预热' : '有效评测'}`)}</summary><div class="two">${Object.keys(names).map(side=>{
      const key = `${pair.round}:${pair.question_id}:${side}`, response = responses.get(key) || {}, measurement = measurements.get(key) || {};
      const speed = measurement.speed_data_valid ? measurement.tokens_per_second : `不可用${measurement.speed_invalid_reason ? `（${measurement.speed_invalid_reason}）` : ''}`;
      return `<section><h3>${escapeHtml(names[side] + " · " + endpointText(source[side]))}</h3><p>状态：${escapeHtml(measurement.ok ? '正常返回' : statusNames[measurement.status] || measurement.status || '缺少数据')}</p><p>首次可见输出：${escapeHtml(duration(measurement.ttft_ms))}；首次答案正文：${escapeHtml(duration(measurement.first_answer_ms))}；完整响应：${escapeHtml(duration(measurement.total_ms))}；Token/s：${escapeHtml(speed)}</p>${(measurement.request_protocol === 'responses' ? [['实际输出 Token',measurement.output_tokens ?? '未提供'], ...responsesMetrics(measurement)] : []).map(([label,value]) => `<p>${escapeHtml(label)}：${escapeHtml(value)}</p>`).join('')}<h4>最终回答原文</h4><pre>${escapeHtml(response.content || '上游未提供答案正文')}</pre><h4>上游返回的推理内容</h4><pre>${escapeHtml(response.reasoning || '上游未提供独立推理字段')}</pre></section>`;
    }).join('')}</div></details>`).join('');
  }
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>准入测试报告</title><style>body{max-width:1100px;margin:32px auto;padding:20px;font:16px/1.6 sans-serif;color:#172c29}.headline{padding:18px;background:#e5eee7;border-left:5px solid #0a675d}.cards,.two{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.cards{grid-template-columns:repeat(3,1fr);margin:18px 0}.card,details{border:1px solid #d7ded5;border-radius:8px;padding:14px;margin:12px 0}.card small,.card strong{display:block}table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #d7ded5;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5ef;padding:12px}@media(max-width:700px){.cards,.two{grid-template-columns:1fr}}</style><h1>准入测试报告</h1><p>候选：${escapeHtml(endpointText(source.candidate))}<br>参照：${escapeHtml(endpointText(source.reference))}</p><p>${escapeHtml(evidence.coverage_complete ? "完整报告" : "未完成快照：仅包含当前已记录证据")}</p><p>${escapeHtml(source.created_at)} · ${escapeHtml(reportStatus[source.status] || source.status)}</p><div class="headline"><h2>${escapeHtml(summary.overall?.label || '证据不足')}</h2><p>能力由人员阅读逐题回答后判断，系统不自动给出准入决定。</p></div>${tokenSection}<div class="cards">${cards}</div><ul>${(summary.explanations || []).map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ul><h2>逐题摘要</h2><table><thead><tr><th>题目</th><th>异常归因</th><th>速度</th><th>能力</th></tr></thead><tbody>${pairRows}</tbody></table>${details}</html>`;
}

const queueCards = new Map();
let queuePollBusy = false;
const completedHistory = new Set();

function sizeQueue() {
  if (!$('admission-queue-panel').open) return;
  const root = $('admission-queue'), cards = [...root.children].filter(item => item.matches('article'));
  const scrollTop = root.scrollTop, atEnd = scrollTop > 0 && root.scrollHeight - root.clientHeight - scrollTop <= 2;
  root.style.gridAutoRows = '';
  if (cards.length > 5) {
    const height = Math.ceil(Math.min(...cards.map(card => card.getBoundingClientRect().height)));
    root.style.gridAutoRows = `minmax(${height}px,max-content)`;
    root.style.maxHeight = `${height * 5 + parseFloat(getComputedStyle(root).rowGap) * 4}px`;
  } else root.style.maxHeight = '';
  root.scrollTop = atEnd ? root.scrollHeight : scrollTop;
}

$('admission-queue-panel').addEventListener('toggle', () => requestAnimationFrame(sizeQueue));
window.addEventListener('resize', () => requestAnimationFrame(sizeQueue));

function renderQueue(jobs) {
  const root = $('admission-queue'), seen = new Set();
  if(jobs.length)for(const element of root.querySelectorAll('.empty'))element.remove();
  let positionIndex = 0;
  for (const job of jobs) {
    const id = String(job.id); seen.add(id);
    let item = queueCards.get(id);
    if (!item) {
      const card = Workbench.node('article','','run-card'), title = Workbench.node('h3'), facts = Workbench.node('p','','hint history-facts'), actions = Workbench.node('div','','actions');
      const view = Workbench.node('button','查看进度','secondary'), cancel = Workbench.node('button','取消任务','secondary');
      view.type = cancel.type = 'button';
      view.addEventListener('click',async()=>{ selectedJobId=job.id; queueGeneration++; try { await loadQueue(); } catch(error) { $('error').textContent=error.message; } });
      cancel.dataset.action='cancel';
      cancel.addEventListener('click',async()=>{ cancel.disabled=true; try { await Workbench.api(`./api/jobs/${encodeURIComponent(job.id)}/cancel`,{method:'POST'}); await loadQueue(); } catch(error) { $('error').textContent=error.message; } finally {cancel.disabled=false;} });
      const credentials = Workbench.node('div','','queue-credentials'), label = Workbench.node('label','补充临时 Key'), key = document.createElement('input'), resume = Workbench.node('button','补充并继续','secondary');
      key.type='password'; key.autocomplete='off'; key.spellcheck=false; resume.type='button'; label.append(key);
      resume.addEventListener('click',async()=>{ const value=key.value; if(!value)return; key.value=''; resume.disabled=true; try { await Workbench.api(`./api/jobs/${encodeURIComponent(job.id)}/credentials`,{method:'POST',body:JSON.stringify({api_key:value})}); await loadQueue(); } catch(error) { $('error').textContent=error.message; } finally { resume.disabled=false; } });
      credentials.append(label,resume); actions.append(view,cancel); card.append(title,facts,actions,credentials);
      item={card,title,facts,cancel,credentials}; queueCards.set(id,item);
    }
    const state=job.state || job.status;
    item.title.textContent=`任务 #${job.id} · ${endpointText(job.candidate || {model:job.candidate_model || job.model})} → ${endpointText(job.reference)}`;
    item.facts.textContent=jobDescription(job,jobs);
    item.cancel.textContent=`${state==='running' ? '停止运行' : '取消排队'} #${job.id} · ${job.candidate?.name || '未记录渠道名称'}`;
    item.cancel.hidden=['completed','succeeded','failed','cancelled','canceled'].includes(state);
    item.credentials.hidden=!(job.needs_credentials || state==='waiting_credentials');
    if(root.children[positionIndex]!==item.card)root.insertBefore(item.card,root.children[positionIndex] || null);
    positionIndex++;
  }
  for (const [id,item] of queueCards) if(!seen.has(id)){item.card.remove();queueCards.delete(id);}
  if (!jobs.length) root.replaceChildren(Workbench.node('p','暂无任务。提交后按创建顺序执行。','empty'));
  else for(const element of root.querySelectorAll('.empty'))element.remove();
  const active = jobs.filter(job => ['queued','running','waiting_credentials'].includes(job.state || job.status));
  $('queue-count').textContent = `${jobs.length} 个任务 · ${active.filter(job => job.state === 'running').length} 个运行中 · ${active.filter(job => job.state !== 'running').length} 个等待`;
  requestAnimationFrame(sizeQueue);
}

async function loadSelectedJob(jobs=[]) {
  const id=selectedJobId, generation=queueGeneration;
  if(id===null)return;
  const job=await Workbench.api(`./api/jobs/${encodeURIComponent(id)}`);
  if(generation!==queueGeneration || id!==selectedJobId)return;
  const state=job.state || job.status;
  $('run-status').textContent=`任务 #${id} · ${endpointText(job.candidate)} → ${endpointText(job.reference)}\n${jobDescription(job,jobs)}`;
  $('stop').textContent=`${state==='running' ? '停止运行' : '取消排队'} #${id} · ${job.candidate?.name || '未记录渠道名称'}`;
  $('stop').dataset.jobId=String(id);
  $('stop').hidden=['completed','succeeded','failed','cancelled','canceled'].includes(state);
  if(job.report){report=job.report; if(job.report_id)report.id=job.report_id; if(JSON.stringify(report)!==renderedReport)renderReport();}
  else if(job.report_removed){report=null;renderedReport='';$('report-panel').hidden=true;}
  if(job.report_id && !completedHistory.has(job.report_id)){
    await loadReportHistory();
    completedHistory.add(job.report_id);
  }
}

async function loadQueue() {
  const data=await Workbench.api('./api/jobs'), jobs=data.jobs || [];
  renderQueue(jobs);
  if(selectedJobId===null && !report){
    const job=jobs.find(item=>['running','waiting_credentials','queued'].includes(item.state || item.status)) || jobs[0];
    if(job)selectedJobId=job.id;
  }
  await loadSelectedJob(jobs);
}

async function pollQueue() {
  if(queuePollBusy)return;
  queuePollBusy=true;
  try { await loadQueue(); } catch(error) { $('error').textContent=error.message; } finally { queuePollBusy=false; }
}

$('compare-form').addEventListener('submit',async event=>{
  event.preventDefault(); syncModelPolicy();
  const payload={candidate:{name:$('candidate-name').value.trim(),base_url:$('candidate-url').value.trim(),api_key:$('candidate-key').value.trim(),model:$('candidate-model').value.trim(),protocol:$('candidate-protocol').value},reference:{channel_id:Number($('reference-id').value),model:$('reference-model').value.trim(),protocol:$('reference-protocol').value},rounds:Number($('rounds').value)};
  if(payload.candidate.model!==payload.reference.model){$('error').textContent='成对测试要求候选端与参照端使用相同的请求模型名。';return;}
  $('candidate-key').value=''; $('start').disabled=true; $('error').textContent='';
  $('run-status').textContent='正在创建准入任务。';
  try {
    const job=await Workbench.api('./api/jobs',{method:'POST',headers:{'Idempotency-Key':Workbench.requestId()},body:JSON.stringify(payload)});
    selectedJobId=job.id;queueGeneration++;await loadQueue();
  } catch(error) { $('error').textContent=error.message; } finally { payload.candidate.api_key=''; $('start').disabled=false; }
});

$('extract').addEventListener('click',async()=>{ const text=$('candidate-import').value.trim(); if(!text)return; $('extract').disabled=true; try{const data=await Workbench.api('./api/extract-channel',{method:'POST',body:JSON.stringify({text})}); if(data.base_url)$('candidate-url').value=data.base_url;if(data.api_key)$('candidate-key').value=data.api_key;$('candidate-import').value='';$('extract-status').textContent=data.has_url&&data.has_key?'已提取，未保存到渠道库。':'信息不完整，请补充地址或密钥。';}catch(error){$('extract-status').textContent=error.message;}finally{$('extract').disabled=false;}});
for (const side of Object.keys(names)) $(`${side}-model`).addEventListener('input',syncModelPolicy);
for (const side of Object.keys(names)) $(`${side}-protocol`).addEventListener('change',schedulePrecheck);
$('stop').addEventListener('click',async()=>{ const id=$('stop').dataset.jobId; if(!id || $('stop').hidden)return; try{await Workbench.api(`./api/jobs/${encodeURIComponent(id)}/cancel`,{method:'POST'});await loadQueue();}catch(error){$('error').textContent=error.message;} });
$('refresh-queue').addEventListener('click',()=>pollQueue()); $('preset').addEventListener('change',applyPreset); $('reference-id').addEventListener('change',showReference); $('pair-filter').addEventListener('change',applyPairFilter);
$('refresh-reference').addEventListener('click',()=>loadReferences().catch(error=>$('error').textContent=error.message)); $('refresh-reports').addEventListener('click',()=>loadReportHistory().catch(error=>$('error').textContent=error.message));
$('export-json').addEventListener('click',()=>report&&Workbench.download(report,exportFilename(report,'technical','json')));
$('export-html').addEventListener('click',async()=>{ if(!report)return; const snapshot=report; try{const source=snapshot.id?await Workbench.api(`./api/reports/${snapshot.id}/ordinary`):ordinaryPayload(snapshot);Workbench.download(reportHtml(source,false),exportFilename(snapshot,'summary','html'),'text/html');}catch(error){$('error').textContent=error.message;} });
$('export-technical-html').addEventListener('click',()=>report&&Workbench.download(reportHtml(report,true),exportFilename(report,'technical','html'),'text/html'));

(async()=>{ metadata=await Workbench.api('./api/meta'); $('preset').replaceChildren(new Option('手动填写模型','')); for(const preset of metadata.presets)$('preset').append(new Option(`${preset.provider} · ${preset.label}`,preset.id)); await Promise.all([loadReferences(),loadReportHistory()]); await loadQueue(); setInterval(pollQueue,1500); })().catch(error=>{$('error').textContent=error.message;});
