(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const api = (url, options) => Workbench.api('/api/integrity' + url, options);
  const post = (url, body) => api(url, {method:'POST', body:JSON.stringify(body)});
  let metadata = {channels:[], references:[]}, activeTask = '', refreshing = false;
  let submissionKey = crypto.randomUUID();
  let unifiedKey = crypto.randomUUID(), unifiedActive = '';
  const query = new URLSearchParams(location.search);
  $('review-source').value = query.get('source_ref') || 'manual:' + crypto.randomUUID();
  $('review-incident').value = $('review-source').value;
  if (['hlwy','kbf'].includes(query.get('strategy'))) $('review-strategy').value = query.get('strategy');
  function error(value) { $('error').textContent = value?.message || String(value); $('error').hidden = false; }
  function clearError() { $('error').hidden = true; $('error').textContent = ''; }
  function text(tag, value, cls) { return Workbench.node(tag, value, cls); }
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
  }
  function unifiedConditions() {
    const channel=metadata.unified?.channels.find(c=>String(c.id)===$('unified-channel').value);
    const model=channel?.models.find(m=>m.model===$('unified-model').value);
    $('unified-condition').textContent=model ? `Responses · low · 映射 ${model.upstream_model} · ${model.eligible?'可执行':model.reason}` : '请选择已启用且具有有效地址、凭据及模型/协议映射的公共渠道；已记录渠道也可手动测试。';
    $('unified-submit').disabled=!model?.eligible;
  }
  async function unifiedDetail(id) {
    unifiedActive=id; const task=await api('/tests/'+id);
    $('unified-detail').hidden=false; $('unified-detail').replaceChildren(text('h3','统一报告 '+id),text('pre',JSON.stringify(task,null,2)));
  }
  async function refreshUnified() {
    const {tasks}=await api('/tests');$('unified-tasks').replaceChildren();
    if(!tasks.length)$('unified-tasks').append(text('p','暂无三项测试历史'));
    for(const task of tasks) {
      const row=text('article','', 'task');row.dataset.unifiedId=task.task_id;
      row.append(text('h3',`${task.status} · ${task.target_snapshot.model} · #${task.target_snapshot.registry_channel_id}`),text('p',budgetLine(task)));
      const results=text('div','', 'method-results');
      for(const report of task.reports) {
        const card=text('section','', 'method-result');card.dataset.method=report.method;
        card.append(text('h4',report.label),text('p',`${report.status} · ${report.score.source_verdict || report.score.status} · ${report.reason}`),
          text('p',`有效 ${report.valid} / 尝试 ${report.attempted} / 计划 ${report.planned} · invalid ${report.invalid} · unknown ${report.unknown} · 未运行 ${report.not_run}`),
          text('p',`耗时 ${Math.round(report.duration_ms)} ms · input ${report.usage.input_tokens_reported ?? 'unknown'} / output ${report.usage.output_tokens_reported ?? 'unknown'} / reasoning ${report.usage.reasoning_tokens_reported ?? 'unknown'}`),
          text('p',`版本 ${report.version} · ${report.conditions.protocol} / ${report.conditions.effort} · calibration_status=unvalidated · metadata_status=unavailable`),text('p',report.limitation));results.append(card);
      }
      row.append(results);
      const detail=text('button','查看三项报告');detail.type='button';detail.onclick=()=>unifiedDetail(task.task_id).catch(error);row.append(detail);
      if(['queued','running'].includes(task.status)) {const b=text('button','取消三项任务');b.type='button';b.onclick=()=>post('/tests/'+task.task_id+'/cancel',{}).then(refreshUnified).catch(error);row.append(b);}
      if(['cancelled','failed','partially_completed'].includes(task.status)) {const b=text('button','安全恢复三项任务');b.type='button';b.onclick=()=>post('/tests/'+task.task_id+'/resume',{}).then(refreshUnified).catch(error);row.append(b);}
      const link=text('a','导出三项 JSON','export');link.href='/api/integrity/tests/'+task.task_id+'/export';link.download='three-method-api.json';row.append(link);$('unified-tasks').append(row);
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
      row.append(text('div',`${task.strategy_id.toUpperCase()} · ${task.status} · ${task.source_ref}`, 'task-title'),text('p',budgetLine(task)),text('p',reportLine(task.report)));
      const detail = text('button','查看报告'); detail.type='button'; detail.onclick=()=>showDetail(task.task_id).catch(error); row.append(detail);
      if (['queued','running'].includes(task.status)) {
        const cancel=text('button','取消任务'); cancel.type='button'; cancel.onclick=()=>post('/reviews/'+task.task_id+'/cancel',{}).then(refresh).catch(error);row.append(cancel);
      }
      if (['cancelled','failed','partially_completed'].includes(task.status)) {
        const resume=text('button','安全恢复'); resume.type='button'; resume.onclick=()=>post('/reviews/'+task.task_id+'/resume',{}).then(refresh).catch(error);row.append(resume);
      }
      const link = text('a','导出 JSON','export'); link.href='/api/integrity/reviews/'+task.task_id+'/export'; link.download='integrity-review.json'; row.append(link);
      $('tasks').append(row);
    }
  }
  async function showDetail(id) {
    activeTask = id; const task=await api('/reviews/'+id);
    $('task-detail').hidden=false; $('task-detail').replaceChildren(text('h3',`报告 ${id}`),text('p',task.next_step),text('pre',JSON.stringify(task,null,2)));
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
  $('review-channel').onchange=()=>{channelModels();submissionKey=crypto.randomUUID();};
  $('review-model').onchange=()=>{modelProtocol();submissionKey=crypto.randomUUID();};
  $('review-strategy').onchange=()=>{referenceOptions();submissionKey=crypto.randomUUID();};
  $('review-reference').onchange=()=>{referenceConditions();submissionKey=crypto.randomUUID();};
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
    const task=await post('/evidence',{evidence:await fileJson('evidence-file'),idempotency_key:'evidence:'+crypto.randomUUID(),confirm_authorized:$('evidence-authorized').checked});
    $('evidence-status').textContent='已受理 '+(task.task_id || task.job_id);await refreshEvidence();
  }catch(e){error(e);}};
  $('refresh').onclick=()=>{clearError();refresh().catch(error);};
  $('unified-channel').onchange=$('unified-model').onchange=()=>{unifiedKey=crypto.randomUUID();unifiedConditions();};
  $('unified-new').onclick=()=>{unifiedKey=crypto.randomUUID();$('unified-status').textContent='已准备新一轮；点击开始执行。';$('unified-confirm').checked=false;};
  $('unified-form').onsubmit=async event=>{event.preventDefault();clearError();$('unified-submit').disabled=true;try {
    const task=await post('/tests',{registry_channel_id:Number($('unified-channel').value),model:$('unified-model').value,protocol:'responses',idempotency_key:unifiedKey,confirm_live:$('unified-confirm').checked});
    $('unified-status').textContent='已受理统一任务 '+task.task_id;await refreshUnified();await unifiedDetail(task.task_id);
  }catch(e){error(e);}finally{unifiedConditions();}};
  loadMeta().then(refresh).catch(error);
  setInterval(()=>refresh().catch(error),1500);
})();
