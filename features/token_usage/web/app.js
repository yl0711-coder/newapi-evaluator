(() => {
  const $ = id => document.getElementById(id);
  const api = (path, body) => Workbench.api('/api/token-usage' + path, body === undefined ? {} : {method:'POST',body:JSON.stringify(body)});
  const message = value => { $('message').textContent = value; };
  let models = [], channels = [], polling = false, renderedResults = '';
  const selected = () => ({channel_id:Number($('candidate').value),model:$('model').value,protocol:$('protocol').value});
  const node = (tag, text) => Workbench.node(tag, text);
  const channelName = id => channels.find(c => c.id === id)?.name || '#' + id;
  async function refresh() {
    const [bindings, configs, results, status] = await Promise.all([api('/bindings'),api('/configs'),api('/results'),api('/status')]);
    $('executor').textContent = `消费者${status.running ? '运行中' : '未启动'}；主动测试${status.active_enabled ? '已启用' : '未启用'}；启用监测 ${status.scheduled_enabled} 个`;
    $('bindings').replaceChildren(...bindings.bindings.map(b => node('article', `${b.model} · ${b.protocol} · 可信参照 ${channelName(b.snapshot.registry_channel_id)} · 小偏差 ${b.thresholds.minor_ratio} 倍且差值 ${b.thresholds.minor_difference} Token`)));
    $('configs').replaceChildren(...configs.configs.map(config => {
      const record = node('article', `${config.model} · ${channelName(config.candidate.registry_channel_id)} · ${config.enabled ? '每半小时监测' : '已暂停'}`);
      const toggle = node('button', config.enabled ? '暂停监测' : '启用监测');
      toggle.addEventListener('click', () => action(async () => {
        if (!config.enabled && !$('confirm').checked) throw new Error('请确认固定双端测试请求');
        await api('/configs', {channel_id:config.candidate.registry_channel_id,model:config.model,protocol:config.protocol,config_id:config.id,enabled:!config.enabled,confirm_live:$('confirm').checked});
        message('监测配置已保存'); await refresh();
      }));
      record.append(toggle); return record;
    }));
    const snapshot=JSON.stringify(results.jobs);
    const openEvidence=new Set([...$('results').querySelectorAll('article')].filter(row=>row.querySelector('details')?.open).map(row=>row.dataset.jobId));
    if(snapshot !== renderedResults) $('results').replaceChildren(...results.jobs.map(job => {
      const record = document.createElement('article'); record.dataset.jobId = job.job_id;
      record.append(node('strong', `${job.target.canonical_model} · ${channelName(job.target.registry_channel_id)} · ${job.result?.label || job.status}`));
      const r = job.result;
      record.append(node('p', r ? `任务 ${job.status}；实际计费待核实` : `任务 ${job.status}；已接受入队`));
      const details = document.createElement('details'); details.append(node('summary', '技术证据'), node('pre', JSON.stringify(job, null, 2))); record.append(details);
      details.open=openEvidence.has(job.job_id);
      if (['queued','running'].includes(job.status)) {
        const cancel = node('button','停止'); cancel.addEventListener('click', () => action(async () => {await api('/results/'+job.job_id+'/cancel',{});await refresh();})); record.append(cancel);
      }
      return record;
    }));
    renderedResults=snapshot;
    polling = results.jobs.some(job => ['queued','running'].includes(job.status));
    if(!polling && $('message').textContent === '测试已入队，等待实际执行') message('测试已结束，请查看结论。');
  }
  async function action(fn) { try { await fn(); } catch(error) { message(error.message); } }
  $('model').addEventListener('change', () => { $('protocol').value = models.find(m => m.model === $('model').value)?.protocol || 'openai'; $('trusted').checked = false; });
  $('save-binding').addEventListener('click', () => action(async () => {
    await api('/bindings',{model:$('model').value,protocol:$('protocol').value,reference_channel_id:Number($('reference').value),confirm_trusted:$('trusted').checked,threshold_config:{minor_ratio:Number($('minor-ratio').value),minor_difference:Number($('minor-difference').value)}});
    $('trusted').checked=false; message('可信参照已保存，未发送测试请求'); await refresh();
  }));
  $('preview').addEventListener('click', () => action(async () => { const result=await api('/preview',selected());$('preview-result').textContent=`同条件首轮 ${result.initial_requests} 请求；最多 ${result.max_requests}；零重试；每 ${result.interval_seconds / 60} 分钟；参照 ${channelName(result.reference.registry_channel_id)}`; }));
  $('save-config').addEventListener('click', () => action(async () => { await api('/configs',{...selected(),enabled:$('enabled').checked,confirm_live:$('confirm').checked});$('confirm').checked=false;message('监测配置已保存');await refresh(); }));
  $('manualcheck').addEventListener('click', () => action(async () => { await api('/manualcheck',{...selected(),confirm_live:$('confirm').checked,idempotency_key:crypto.randomUUID()});$('confirm').checked=false;message('测试已入队，等待实际执行');await refresh(); }));
  $('refresh').addEventListener('click', () => action(refresh));
  action(async () => {
    const meta=await api('/meta');models=meta.models;channels=meta.channels;
    $('model').replaceChildren(...models.map(m=>new Option(m.model,m.model)));
    $('protocol').value=models[0]?.protocol || 'openai';
    for(const id of ['reference','candidate']) $(id).replaceChildren(new Option('请选择渠道',''),...channels.filter(c=>c.enabled && c.credential_status==='available').map(c=>new Option(c.name+' · #'+c.id,c.id)));
    await refresh();
  });
  const timer=setInterval(() => { if(polling) action(refresh); },1000);
  addEventListener('pagehide', () => clearInterval(timer));
})();
