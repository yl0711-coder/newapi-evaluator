(() => {
  const $ = id => document.getElementById(id);
  const api = (path, body) => Workbench.api('/api/token-usage' + path, body === undefined ? {} : {method:'POST',body:JSON.stringify(body)});
  const message = value => { $('message').textContent = value; };
  let models = [], channels = [], polling = false, renderedResults = '', refreshing = false, lastAttempt = 0, lastSuccess = 0, activeEnabled = false;
  const selected = () => ({channel_id:Number($('candidate').value),model:$('model').value,protocol:$('protocol').value});
  const node = (tag, text) => Workbench.node(tag, text);
  const channelName = id => channels.find(c => c.id === id)?.name || '#' + id;
  const statuses = {queued:'排队中',running:'执行中',completed:'已完成',cancelled:'已取消'};
  const protocols = {openai:'Chat Completions',responses:'Responses',anthropic:'Claude Messages'};
  const identity = (display, endpoint) => `${display?.name || '未记录渠道名称'} · #${endpoint.registry_channel_id} · ${endpoint.canonical_model || endpoint.model} · ${protocols[endpoint.protocol] || endpoint.protocol}`;
  async function refresh() {
    if (refreshing) return;
    refreshing = true; lastAttempt = Date.now();
    try {
    const [bindings, configs, results, status] = await Promise.all([api('/bindings'),api('/configs'),api('/results'),api('/status')]);
    activeEnabled = status.active_enabled;
    $('executor').textContent = `消费者${status.running ? '运行中' : '未启动'}；主动测试${status.active_enabled ? '已启用' : '未启用'}；启用监测 ${status.scheduled_enabled} 个`;
    $('manualcheck').disabled = !activeEnabled;
    $('active-help').textContent = activeEnabled ? '主动测试已启用；确认后可发起固定双端测试。' : '主动执行未启用，立即测试暂不可用。半小时监测独立运行，可正常启用。';
    $('bindings').replaceChildren(...bindings.bindings.map(b => node('article', `${b.model} · ${b.protocol} · 可信参照 ${channelName(b.snapshot.registry_channel_id)} · 小偏差 ${b.thresholds.minor_ratio} 倍且差值 ${b.thresholds.minor_difference} Token`)));
    $('configs').replaceChildren(...configs.configs.map(config => {
      const record = node('article', `候选 ${identity(config.display_identity?.candidate, config.candidate)}；可信参照 ${identity(config.display_identity?.reference, config.reference)}；${config.enabled ? '每半小时监测' : '已暂停'}`);
      const toggle = node('button', config.enabled ? '暂停监测' : '启用监测');
      toggle.addEventListener('click', () => action(async () => {
        if (!config.enabled && !$('confirm').checked) throw new Error('请确认固定双端测试请求');
        await api('/configs', {channel_id:config.candidate.registry_channel_id,model:config.model,protocol:config.protocol,config_id:config.id,enabled:!config.enabled,confirm_live:$('confirm').checked});
        message('监测配置已保存'); await refresh();
      }));
      record.append(toggle); return record;
    }));
    const snapshot=JSON.stringify([results.jobs,activeEnabled]);
    const openEvidence=new Set([...$('results').querySelectorAll('article')].filter(row=>row.querySelector('details')?.open).map(row=>row.dataset.jobId));
    if(snapshot !== renderedResults) $('results').replaceChildren(...results.jobs.map(job => {
      const record = document.createElement('article'); record.dataset.jobId = job.job_id;
      const r = job.result;
      const terminal = ['completed','cancelled'].includes(job.status);
      const label = terminal ? (r?.label || '输入 Token 用量无法判定') : r?.severity === 'high_risk' ? `${r.label}；复核中` : '等待本轮完成，尚未形成最终结论';
      record.append(node('strong', label));
      record.append(node('p', `候选 ${identity(job.display_identity?.candidate, job.target)}；可信参照 ${identity(job.display_identity?.reference, job.reference)}`));
      let state = `任务${statuses[job.status] || '状态未知'}`;
      if (job.status === 'queued') state += !activeEnabled && job.config_id == null ? '；主动执行未启用，等待启用后执行' : '；等待实际执行';
      record.append(node('p', `${state}；实际计费待核实`));
      const details = document.createElement('details'); details.append(node('summary', '技术证据'), node('pre', JSON.stringify(job, null, 2))); record.append(details);
      details.open=openEvidence.has(job.job_id);
      if (['queued','running'].includes(job.status)) {
        const cancel = node('button',job.status === 'queued' ? '取消此排队任务' : '停止此任务'); cancel.addEventListener('click', () => action(async () => {await api('/results/'+job.job_id+'/cancel',{});await refresh();})); record.append(cancel);
      }
      return record;
    }));
    renderedResults=snapshot;
    polling = results.jobs.some(job => ['queued','running'].includes(job.status));
    if(!polling && $('message').textContent === '测试已入队，等待实际执行') message('测试已结束，请查看结论。');
    lastSuccess = Date.now();
    $('updated').textContent = `最近更新：${new Date(lastSuccess).toLocaleTimeString('zh-CN')}；空闲时每 15 秒自动刷新`;
    } catch(error) {
      $('updated').textContent = `刷新失败：${error.message}；${lastSuccess ? '上次成功更新 '+new Date(lastSuccess).toLocaleTimeString('zh-CN') : '尚无成功更新'}`;
      throw error;
    } finally { refreshing = false; }
  }
  async function action(fn) { try { await fn(); } catch(error) { message(error.message); } }
  async function loadModels(preserve=false) {
    const meta=await api('/meta');models=meta.models;channels=meta.channels;
    const current=$('model').value;
    $('model').replaceChildren(...models.map(m=>new Option(m.model,m.model)));
    if(preserve) $('model').value=current;
    else $('protocol').value=models[0]?.protocol || 'openai';
    for(const id of ['reference','candidate']) {
      const chosen=$(id).value;
      $(id).replaceChildren(new Option('请选择渠道',''),...channels.filter(c=>c.enabled && c.credential_status==='available').map(c=>new Option(c.name+' · #'+c.id,c.id)));
      if(preserve) $(id).value=chosen;
    }
  }
  $('refresh-models').addEventListener('click',()=>action(async()=>{await loadModels(true);message('共用模型选项已更新，当前选择已保留。');}));
  $('model').addEventListener('change', () => { $('protocol').value = models.find(m => m.model === $('model').value)?.protocol || 'openai'; $('trusted').checked = false; });
  $('save-binding').addEventListener('click', () => action(async () => {
    await api('/bindings',{model:$('model').value,protocol:$('protocol').value,reference_channel_id:Number($('reference').value),confirm_trusted:$('trusted').checked,threshold_config:{minor_ratio:Number($('minor-ratio').value),minor_difference:Number($('minor-difference').value)}});
    $('trusted').checked=false; message('可信参照已保存，未发送测试请求'); await refresh();
  }));
  $('preview').addEventListener('click', () => action(async () => { const result=await api('/preview',selected());$('preview-result').textContent=`候选 ${identity(result.display_identity?.candidate, result.target)}；可信参照 ${identity(result.display_identity?.reference, result.reference)}；同条件首轮 ${result.initial_requests} 请求；最多 ${result.max_requests}；零重试；每 ${result.interval_seconds / 60} 分钟`; }));
  $('save-config').addEventListener('click', () => action(async () => { await api('/configs',{...selected(),enabled:$('enabled').checked,confirm_live:$('confirm').checked});$('confirm').checked=false;message('监测配置已保存');await refresh(); }));
  $('manualcheck').addEventListener('click', () => action(async () => { if(!activeEnabled) throw new Error('主动执行未启用，请先启用后再发起测试');await api('/manualcheck',{...selected(),confirm_live:$('confirm').checked,idempotency_key:Workbench.requestId()});$('confirm').checked=false;message('测试已入队，等待实际执行');await refresh(); }));
  $('refresh').addEventListener('click', () => action(refresh));
  action(async () => {
    await loadModels();
    await refresh();
  });
  const timer=setInterval(() => { if(document.visibilityState === 'visible' && Date.now()-lastAttempt >= (polling ? 1000 : 15000)) action(refresh); },1000);
  document.addEventListener('visibilitychange', () => { if(document.visibilityState === 'visible') action(refresh); });
  addEventListener('pagehide', () => clearInterval(timer));
})();
