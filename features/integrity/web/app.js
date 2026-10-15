(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const api = (url, options) => Workbench.api('/api/integrity' + url, options);
  const post = (url, body) => api(url, {method:'POST', body:JSON.stringify(body)});
  let metadata = {channels:[], references:[]}, activeTask = '', refreshing = false;
  let submissionKey = Workbench.requestId();
  let unifiedKey = Workbench.requestId(), unifiedActive = '';
  let canaryKey = Workbench.requestId(), canaryBaselines = [];
  const query = new URLSearchParams(location.search);
  $('review-source').value = query.get('source_ref') || 'manual:' + Workbench.requestId();
  $('review-incident').value = $('review-source').value;
  if (['hlwy','kbf'].includes(query.get('strategy'))) $('review-strategy').value = query.get('strategy');
  function error(value) { $('error').textContent = value?.message || String(value); $('error').hidden = false; }
  function clearError() { $('error').hidden = true; $('error').textContent = ''; }
  function text(tag, value, cls) { return Workbench.node(tag, value, cls); }
  function statusLabel(status, cancelling = false) {
    if (cancelling && ['queued', 'running'].includes(status)) return '停止中';
    return ({queued:'排队中', running:'检测中', completed:'已完成', partially_completed:'部分完成',
      cancelled:'已停止', failed:'执行失败', rejected:'条件已失效', unknown:'结果未知',
      skipped:'未执行', not_run:'尚未执行', incomplete:'未完成', unsupported:'当前方法不支持'})[status] || '待确认';
  }
  function channelLine(task) {
    const target = task.target_snapshot || {};
    const protocol = ({responses:'Responses', openai:'Chat Completions', anthropic:'Messages'})[target.protocol] || '未记录协议';
    return `${task.display_identity?.channel_name || '未记录渠道名称'} · 渠道 #${target.registry_channel_id ?? '?'} · ${target.canonical_model || target.model || '未记录模型'} · ${protocol}`;
  }
  function verdictLabel(score) {
    const verdict = score?.source_verdict || score?.status;
    return ({SAME:'未检测到显著差异', DIFF:'检测到行为差异', UNKNOWN:'缺少参考或有效证据',
      UNDETERMINED:'无法判定', current_only:'仅当前成绩，未选择可信参照', degraded:'检测到能力下降',
      normal:'本次未检测到明显下降', unknown:'无法判定', unlisted:'无法判定，超出已登记模型范围'})[verdict]
      || '探索性行为线索，普通 API 尚未校准';
  }
  function methodCard(report) {
    const card = text('section', '', 'method-result'); card.dataset.method = report.method;
    card.append(text('h4', report.label), text('p', statusLabel(report.status)), text('p', verdictLabel(report.score)),
      text('p', `有效样本 ${report.valid}/${report.planned} · 结果未知 ${report.unknown} · 尚未执行 ${report.not_run}`),
      text('p', report.limitation));
    if (report.method === 'canary') card.append(text('p', `当前成绩 ${report.score.correct ?? 0}/${report.planned}`));
    return card;
  }
  function technicalEvidence(task) {
    const details = text('details', '');
    details.append(text('summary', '技术证据（JSON）'), text('p', budgetLine(task)), text('pre', JSON.stringify(task, null, 2)));
    return details;
  }
  function renderDetail(container, task, label) {
    container.hidden = false;
    container.replaceChildren(text('h3', `${label} · 任务 ${task.task_id}`), text('p', channelLine(task)),
      text('p', statusLabel(task.status, task.cancel_requested)));
    if (['queued','running'].includes(task.status)) container.append(text('p', '检测尚未完成，以下为当前部分结果。'));
    if (task.next_step) container.append(text('p', task.next_step));
    if (task.reports) {
      const results = text('div', '', 'method-results');
      for (const report of task.reports) results.append(methodCard(report));
      container.append(results);
    } else container.append(text('p', reportLine(task.report)));
    container.append(technicalEvidence(task));
  }
  function exportName(task, kind) {
    const name = (task.display_identity?.channel_name || '未记录渠道名称').replace(/[\x00-\x1f<>:"/\\|?*]/g, '_').slice(0, 80);
    return `${name}-${kind}-${task.task_id}.json`;
  }
  async function fileJson(id) {
    const file = $(id).files[0];
    if (!file || file.size > 2000000) throw new Error('请选择不超过 2 MB 的 JSON 文件');
    try { return JSON.parse(await file.text()); } catch { throw new Error('JSON 文件格式无效'); }
  }
  function channelModels() {
    const channel = metadata.channels.find(c => String(c.id) === $('review-channel').value);
    const previous = $('review-model').value;
    $('review-model').replaceChildren(new Option('请选择模型',''));
    for (const m of channel?.models || []) $('review-model').append(new Option(m.model,m.model));
    if (previous) $('review-model').value = previous;
    modelProtocol();
  }
  function modelProtocol() {
    const channel = metadata.channels.find(c => String(c.id) === $('review-channel').value);
    const model = channel?.models.find(m => m.model === $('review-model').value);
    if (model) $('review-protocol').value = model.protocol;
  }
  function referenceOptions() {
    const selected = $('review-reference').value;
    $('review-reference').replaceChildren(new Option('请选择匹配参考',''));
    for (const ref of metadata.references.filter(r => r.strategy_id === $('review-strategy').value)) {
      $('review-reference').append(new Option(`${ref.reference_model} · ${ref.sample_count} 样本 · ${ref.reference_hash.slice(0,12)}${ref.strategy_id==='kbf'&&!ref.has_self_test?' · 缺 self-test，UNKNOWN':''}`,ref.reference_hash));
    }
    if (selected) $('review-reference').value = selected;
    referenceConditions();
  }
  function referenceConditions() {
    const ref = metadata.references.find(r => r.reference_hash === $('review-reference').value);
    $('review-conditions').value = ref ? JSON.stringify(ref.conditions,null,2) : '';
    const b = ref?.conditions?.budget;
    for (const [id,key] of [['review-requests','max_requests'],['review-input','max_input_tokens'],['review-output','max_output_tokens'],['review-seconds','total_timeout_seconds']]) $(id).value = b?.[key] || '';
  }
  async function loadMeta() {
    metadata = await api('/meta');
    $('executor-status').textContent = metadata.executor.running ? '主动消费者正在运行；只有明确创建的任务会执行。' : '主动消费者默认关闭，任务将排队。管理员需显式配置 EVAL_INTEGRITY_EXECUTOR=live 并启动受锁保护的工作台消费者。';
    const selected = $('review-channel').value || query.get('registry_channel_id');
    $('review-channel').replaceChildren(new Option('请选择渠道',''));
    for (const channel of metadata.channels) {
      const option = new Option(`${channel.name} · #${channel.id}${channel.available?'': ' · '+channel.reason}`,String(channel.id));
      option.disabled = !channel.available; $('review-channel').append(option);
    }
    if (selected) $('review-channel').value = selected;
    channelModels();
    if (query.get('model')) { $('review-model').value = query.get('model'); modelProtocol(); }
    referenceOptions();
    const chosen = $('unified-channel').value;
    $('unified-channel').replaceChildren(new Option('请选择渠道',''));
    for (const channel of metadata.unified.channels) {
      const option = new Option(`${channel.name} · #${channel.id}${channel.available?'':' · 不可执行'}`,String(channel.id));
      option.disabled=!channel.available; $('unified-channel').append(option);
    }
    if(chosen) $('unified-channel').value=chosen;
    unifiedConditions();
    const canaryChosen = $('canary-channel').value;
    $('canary-channel').replaceChildren(new Option('请选择渠道',''));
    for (const channel of metadata.unified.channels) {
      const option = new Option(`${channel.name} · #${channel.id}`, String(channel.id));
      option.disabled = !channel.models.some(m => m.model === 'gpt-6-astra' && m.eligible);
      $('canary-channel').append(option);
    }
    if (canaryChosen) $('canary-channel').value = canaryChosen;
    canaryBaselines = (await api('/canary/baselines')).baselines;
    canaryBaselineOptions();
  }
  function canaryBaselineOptions() {
    const chosen = $('canary-baseline').value;
    const prefix = 'registry:' + $('canary-channel').value + ':';
    $('canary-baseline').replaceChildren(new Option('未选择，仅当前成绩',''));
    for (const b of canaryBaselines.filter(b => b.score?.conditions?.provider?.startsWith(prefix) && b.score?.conditions?.budget?.window === 'canary-active-v1')) {
      $('canary-baseline').append(new Option(`#${b.id} ${b.label}`, String(b.id)));
    }
    if (chosen) $('canary-baseline').value = chosen;
  }
  function unifiedConditions() {
    const channel=metadata.unified?.channels.find(c=>String(c.id)===$('unified-channel').value);
    const model=channel?.models.find(m=>m.model===$('unified-model').value);
    $('unified-condition').textContent=model ? `Responses · low · 映射 ${model.upstream_model} · ${model.eligible?'可执行':model.reason}` : '请选择已启用且具有有效地址、凭据及模型/协议映射的公共渠道；已记录渠道也可手动测试。';
    $('unified-submit').disabled=!model?.eligible;
  }
  async function unifiedDetail(id) {
    unifiedActive=id; const task=await api('/tests/'+id);
    renderDetail($('unified-detail'), task, '主动测试报告');
  }
  async function refreshUnified() {
    const {tasks}=await api('/tests');$('unified-tasks').replaceChildren();
    if(!tasks.length)$('unified-tasks').append(text('p','暂无三项测试历史'));
    for(const task of tasks) {
      const row=text('article','', 'task');row.dataset.unifiedId=task.task_id;
      row.append(text('h3',`${task.task_version === 'canary-active-v1' ? 'Canary' : '三项测试'} · ${statusLabel(task.status, task.cancel_requested)}`),
        text('p', channelLine(task)), text('p', `任务 ${task.task_id}`));
      if (['queued','running'].includes(task.status)) row.append(text('p', '检测尚未完成，以下为当前部分结果。'));
      const results=text('div','', 'method-results');
      for(const report of task.reports) {
        results.append(methodCard(report));
      }
      row.append(results);
      const canary = task.reports.find(r => r.method === 'canary');
      if (canary) {
        if (task.status === 'completed' && canary.status === 'completed' && !canary.request_errors && !canary.unknown && !canary.not_run) {
          const lock = text('button', '锁定为可信 Canary 参照'); lock.type='button';
          lock.onclick=async()=>{try {
            const label=prompt('为完整同条件结果填写可信参照名称', 'Canary ' + task.task_id); if(!label)return;
            await post('/tests/'+task.task_id+'/baseline', {label});
            canaryBaselines=(await api('/canary/baselines')).baselines; canaryBaselineOptions();
          }catch(e){error(e);}}; row.append(lock);
        }
      }
      const detail=text('button','查看报告');detail.type='button';detail.onclick=()=>unifiedDetail(task.task_id).catch(error);row.append(detail);
      if(['queued','running'].includes(task.status)) {const b=text('button','取消任务');b.type='button';b.onclick=()=>post('/tests/'+task.task_id+'/cancel',{}).then(refreshUnified).catch(error);row.append(b);}
      if(['cancelled','failed','partially_completed'].includes(task.status)) {const b=text('button','安全恢复任务');b.type='button';b.onclick=()=>post('/tests/'+task.task_id+'/resume',{}).then(refreshUnified).catch(error);row.append(b);}
      const link=text('a','导出 JSON','export');link.href='/api/integrity/tests/'+task.task_id+'/export';link.download=exportName(task, '完整性测试');row.append(link);$('unified-tasks').append(row);
    }
    if(unifiedActive)await unifiedDetail(unifiedActive);
  }
  function budgetLine(task) {
    const c = task.consumed || {}, l = task.limits || {}, f = task.fees || {};
    return `尝试 ${c.requests ?? 0}/${l.max_requests ?? '?'} · input 记账预估 ${c.input_tokens_reserved ?? 0} · output 记账预估 ${c.output_tokens_reserved ?? 0} · 未知请求 ${c.unknown_requests ?? 0} · 费用估算 USD ${f.estimated_usd ?? 'unknown'}（费用未知请求 ${f.unknown_requests ?? 0}），费用及 token 预估不作停止条件`;
  }
  function reportLine(report) {
    const verdict = report?.source_verdict || report?.status || '等待结果';
    const meaning = verdict === 'SAME' ? '未发现显著差异，不证明相同或等效' : verdict === 'DIFF' ? '检测到行为差异，不认证身份' : verdict === 'UNKNOWN' ? '缺少参考 self-test 或证据' : verdict === 'UNDETERMINED' ? '覆盖不足或证据不足' : '探索性行为比较，未校准';
    return `${verdict} · ${meaning} · 目标 ${report?.target_valid ?? 0}/${report?.target_total ?? '?'} · 参考 ${report?.reference_valid ?? '?'}/${report?.reference_total ?? '?'}`;
  }
  function render(tasks) {
    $('tasks').replaceChildren();
    if (!tasks.length) $('tasks').append(text('p','暂无主动复核任务'));
    for (const task of tasks) {
      const row = text('div','', 'task'); row.dataset.taskId = task.task_id;
      row.append(text('div',`${task.strategy_id.toUpperCase()} · ${statusLabel(task.status, task.cancel_requested)}`, 'task-title'),
        text('p', channelLine(task)), text('p', `任务 ${task.task_id} · 来源 ${task.source_ref}`), text('p',reportLine(task.report)));
      const detail = text('button','查看报告'); detail.type='button'; detail.onclick=()=>showDetail(task.task_id).catch(error); row.append(detail);
      if (['queued','running'].includes(task.status)) {
        const cancel=text('button','取消任务'); cancel.type='button'; cancel.onclick=()=>post('/reviews/'+task.task_id+'/cancel',{}).then(refresh).catch(error);row.append(cancel);
      }
      if (['cancelled','failed','partially_completed'].includes(task.status)) {
        const resume=text('button','安全恢复'); resume.type='button'; resume.onclick=()=>post('/reviews/'+task.task_id+'/resume',{}).then(refresh).catch(error);row.append(resume);
      }
      const link = text('a','导出 JSON','export'); link.href='/api/integrity/reviews/'+task.task_id+'/export'; link.download=exportName(task, '完整性复核'); row.append(link);
      $('tasks').append(row);
    }
  }
  async function showDetail(id) {
    activeTask = id; const task=await api('/reviews/'+id);
    renderDetail($('task-detail'), task, '主动复核报告');
  }
  async function refreshEvidence() {
    const {tasks}=await api('/evidence'); $('evidence-tasks').replaceChildren();
    for (const task of tasks) {
      const id=task.task_id || task.job_id;
      const row=text('div','', 'task'); row.dataset.evidenceId=id;
      row.append(text('p',`官方账号离线分析 · ${task.status} · ${task.account_alias || task.scope || ''}`),text('pre',JSON.stringify(task.report || task.result || task,null,2)));
      if (['queued','running','cancelled','failed','partially_completed'].includes(task.status)) {
        const operation=['queued','running'].includes(task.status)?'cancel':'resume';
        const action=text('button',operation==='cancel'?'取消分析':'安全恢复分析');action.type='button';
        action.onclick=()=>post('/evidence/'+id+'/'+operation,{}).then(refreshEvidence).catch(error);row.append(action);
      }
      const link=text('a','导出官方账号分析','export');link.href='/api/integrity/evidence/'+id+'/export';link.download='official-account-analysis.json';row.append(link);$('evidence-tasks').append(row);
    }
  }
  async function refresh() {
    if (refreshing) return; refreshing=true;
    try { const data=await api('/reviews');render(data.tasks);if(activeTask) await showDetail(activeTask);await refreshEvidence();await refreshUnified(); }
    finally { refreshing=false; }
  }
  $('review-channel').onchange=()=>{channelModels();submissionKey=Workbench.requestId();};
  $('review-model').onchange=()=>{modelProtocol();submissionKey=Workbench.requestId();};
  $('review-strategy').onchange=()=>{referenceOptions();submissionKey=Workbench.requestId();};
  $('review-reference').onchange=()=>{referenceConditions();submissionKey=Workbench.requestId();};
  $('reference-form').onsubmit=async event=>{event.preventDefault();clearError();try {
    const value=await post('/references',{package:await fileJson('reference-file'),expected_sha256:$('reference-sha').value,confirm_authorized:$('reference-authorized').checked});
    $('reference-status').textContent='已验证并导入 '+value.reference_hash.slice(0,12); await loadMeta();$('review-strategy').value=value.strategy_id;referenceOptions();$('review-reference').value=value.reference_hash;referenceConditions();
  } catch(e){error(e);}};
  $('review-form').onsubmit=async event=>{event.preventDefault();clearError();$('review-submit').disabled=true;try {
    const task=await post('/reviews',{registry_channel_id:Number($('review-channel').value),model:$('review-model').value,protocol:$('review-protocol').value,strategy_id:$('review-strategy').value,
      reference_hash:$('review-reference').value,source_ref:$('review-source').value,incident_id:$('review-incident').value,idempotency_key:submissionKey,
      conditions:JSON.parse($('review-conditions').value),limits:{max_requests:Number($('review-requests').value),max_input_tokens:Number($('review-input').value),max_output_tokens:Number($('review-output').value)},budget_seconds:Number($('review-seconds').value),confirm_live:$('review-confirm').checked});
    $('review-status').textContent='已受理 '+task.task_id;await refresh();await showDetail(task.task_id);
  }catch(e){error(e);}finally{$('review-submit').disabled=false;}};
  $('evidence-form').onsubmit=async event=>{event.preventDefault();clearError();try {
    const task=await post('/evidence',{evidence:await fileJson('evidence-file'),idempotency_key:'evidence:'+Workbench.requestId(),confirm_authorized:$('evidence-authorized').checked});
    $('evidence-status').textContent='已受理 '+(task.task_id || task.job_id);await refreshEvidence();
  }catch(e){error(e);}};
  $('refresh-models').onclick=async()=>{
    const button=$('refresh-models');button.disabled=true;clearError();
    const ids=['review-channel','review-model','review-protocol','review-reference','unified-channel','unified-model','canary-channel','canary-baseline'];
    const chosen=ids.map(id=>[id,$(id).value]);
    try {await loadMeta();for(const [id,value] of chosen) $(id).value=value;unifiedConditions();}
    catch(e){error(e);}finally{button.disabled=false;}
  };
  $('refresh').onclick=()=>{clearError();refresh().catch(error);};
  $('unified-channel').onchange=$('unified-model').onchange=()=>{unifiedKey=Workbench.requestId();unifiedConditions();};
  $('unified-new').onclick=()=>{unifiedKey=Workbench.requestId();$('unified-status').textContent='已准备新一轮；点击开始执行。';$('unified-confirm').checked=false;};
  $('unified-form').onsubmit=async event=>{event.preventDefault();clearError();$('unified-submit').disabled=true;try {
    const task=await post('/tests',{registry_channel_id:Number($('unified-channel').value),model:$('unified-model').value,protocol:'responses',idempotency_key:unifiedKey,confirm_live:$('unified-confirm').checked});
    $('unified-status').textContent='已受理统一任务 '+task.task_id;await refreshUnified();await unifiedDetail(task.task_id);
  }catch(e){error(e);}finally{unifiedConditions();}};
  $('canary-channel').onchange=()=>{canaryKey=Workbench.requestId();canaryBaselineOptions();$('canary-confirm').checked=false;};
  $('canary-baseline').onchange=()=>{canaryKey=Workbench.requestId();$('canary-confirm').checked=false;};
  $('canary-new').onclick=()=>{canaryKey=Workbench.requestId();$('canary-confirm').checked=false;$('canary-status').textContent='已准备新一轮 Canary。';};
  $('canary-form').onsubmit=async event=>{event.preventDefault();clearError();$('canary-submit').disabled=true;try {
    const task=await post('/canary',{registry_channel_id:Number($('canary-channel').value), model:'gpt-6-astra',protocol:'responses',
      idempotency_key:canaryKey,baseline_id:Number($('canary-baseline').value)||null,confirm_live:$('canary-confirm').checked});
    $('canary-status').textContent='已受理 Canary '+task.task_id;await refreshUnified();await unifiedDetail(task.task_id);
  }catch(e){error(e);}finally{$('canary-submit').disabled=false;}};
  loadMeta().then(refresh).catch(error);
  setInterval(()=>refresh().catch(error),1500);
})();
