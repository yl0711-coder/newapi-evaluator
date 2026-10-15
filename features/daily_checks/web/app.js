(() => {
  const $ = id => document.getElementById(id);
  const api = (path, options) => Workbench.api('/api/daily-checks' + path, options);
  let meta, preview, selected, generation = 0, timer, busy = false;
  const states = {collecting:'检测中',finalized:'已生成报告',cancelled:'已取消',sent:'已送达',pending:'待发送',sending:'发送中',unknown:'送达未确认',failed:'发送失败',disabled:'未配置机器人'};
  let reasons = {};
  function message(text) { $('message').textContent = text; }
  function timestamp(value) { return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value*1000)); }
  function profileInput() {
    return [['gpt','GPT'],['claude','Claude']].filter(([id]) => $(id+'-model').value).map(([id,family]) => ({family,model:$(id+'-model').value,protocol:$(id+'-protocol').value,reference_channel_id:Number($(id+'-reference').value)}));
  }
  function input(enabled=false) { return {profiles:profileInput(),notify:$('notify-enabled').checked,enabled,confirm_live:$('live-confirm').checked,confirm_notify:$('notify-confirm').checked,confirm_trusted:$('trust-confirm').checked,preview_fingerprint:preview?.fingerprint || null}; }
  function invalidate() { preview = null; $('enable').disabled = true; $('preview-summary').textContent = '配置已变化，请重新预览。'; }
  async function loadMeta(preserveForm=false) {
    meta = await api('/meta');
    reasons = meta.reasons;
    for (const [id,family] of [['gpt','GPT'],['claude','Claude']]) {
      const current = $(id+'-model').value, reference = $(id+'-reference').value;
      $(id+'-model').replaceChildren(new Option('暂不配置',''));
      for (const model of meta.models.filter(m => m.test_families.includes(family))) $(id+'-model').append(new Option(model.model,model.model));
      $(id+'-reference').replaceChildren(new Option('请选择可信参照',''));
      for (const channel of meta.channels.filter(c => c.enabled && c.credential_status==='available')) $(id+'-reference').append(new Option(`${channel.name} · #${channel.id}`,channel.id));
      if (preserveForm) { $(id+'-model').value=current; $(id+'-reference').value=reference; }
      else {
        const profile = meta.plan.profiles.find(p => p.family===family);
        if (profile) { $(id+'-model').value=profile.model; $(id+'-protocol').value=profile.protocol; $(id+'-reference').value=profile.reference_channel_id; }
      }
    }
    if (!preserveForm) $('notify-enabled').checked = meta.plan.notify;
    $('plan-status').textContent = meta.plan.enabled ? '计划已启用；保存之后的下一轮开始执行。' : '计划已暂停，尚未启用每日检测。';
    $('notification-status').textContent = meta.notification_configured ? 'Webhook 已加密保存，不回显。' : '尚未配置有效 Webhook。';
  }
  async function renderDetail(id) {
    const version=++generation; const data=await api('/batches/'+encodeURIComponent(id));
    if (version!==generation) return;
    selected=id; const report=data.report;
    $('detail').hidden=false; $('detail-title').textContent=`${timestamp(report.report_at)} · ${states[report.status] || report.status}`;
    $('coverage').textContent=`覆盖 ${report.coverage.fully_measured_rows}/${report.coverage.rows} 个渠道模型组合；${report.coverage.online_channels} 个已上线渠道。通知：${data.notifications.map(n=>`${n.part}: ${states[n.status] || n.status}`).join('；') || '尚未到报告时间或未启用推送'}。`;
    $('cancel-batch').hidden=report.status!=='collecting';
    $('cancel-batch').dataset.batchId=id; $('retry-notification').dataset.batchId=id;
    $('report-rows').replaceChildren();
    for (const row of report.channels) {
      const card=Workbench.node('article','','report-row');
      card.append(Workbench.node('h3',`${row.channel_name} · ${row.family} · ${row.model || '未配置模型'} · ${row.multiplier}x · #${row.channel_id}`));
      card.append(Workbench.node('p',`ModelTrace：${row.modeltrace.label}。Token：${row.usage.label}。`));
      for (const [key,label] of [['modeltrace','ModelTrace'],['usage_candidate','usage 候选'],['usage_reference','usage 参照']]) {
        const p=row.performance[key];
        card.append(Workbench.node('p',`${label}：完成 ${p.completed}/${p.planned}，未确认 ${p.unknown}，未测 ${p.not_run}；响应中位 ${p.latency_median_ms ?? '未测'} ms，首文本中位 ${p.ttft_median_ms ?? '未测'} ms，输出速度 ${p.tokens_per_second_median ?? '无法测量'} Token/s。${p.stability}`));
      }
      for (const result of [row.modeltrace,row.usage]) if (result.reason) card.append(Workbench.node('p',`原因：${reasons[result.reason] || result.reason}`));
      $('report-rows').append(card);
    }
    $('technical').textContent=JSON.stringify(report,null,2);
  }
  async function refresh() {
    if (busy || document.hidden) return;
    busy=true;
    try {
      const data=await api('/batches'); $('batches').replaceChildren();
      for (const row of data.batches) {
        const button=Workbench.node('button',`${timestamp(row.report_at)} · ${states[row.status] || row.status} · ${row.coverage ? row.coverage.fully_measured_rows+'/'+row.coverage.rows+' 完整测量' : '本轮检测中'}`);
        button.type='button'; button.addEventListener('click',()=>renderDetail(row.id).catch(e=>message(e.message))); $('batches').append(button);
      }
      if (!data.batches.length) $('batches').append(Workbench.node('p','尚无批次报告。启用后从下一轮开始记录。'));
      if (selected) await renderDetail(selected);
      $('refresh-status').textContent='刷新成功：'+new Date().toLocaleTimeString('zh-CN');
    } catch(e) { $('refresh-status').textContent='刷新失败：'+e.message; } finally { busy=false; }
  }
  $('preview').addEventListener('click',async()=>{try {
    preview=await api('/preview',{method:'POST',body:JSON.stringify(input())});
    $('preview-summary').textContent=`已上线 ${preview.online_channels} 个渠道；每轮首次 ${preview.initial_requests_per_batch} 请求，重大用量异常复核后最多 ${preview.max_requests_per_batch}；每日首次 ${preview.initial_requests_per_day}，最多 ${preview.max_requests_per_day}。一小时内完成全部渠道取决于响应速度；到时会报告缺测。`;
    $('preview-table').hidden=false; $('preview-rows').replaceChildren();
    for(const row of preview.channels){const tr=document.createElement('tr');tr.append(Workbench.node('td',`${row.channel_name} · ${row.multiplier}x · #${row.channel_id}`),Workbench.node('td',`${row.family} · ${row.model || '未配置'}`),Workbench.node('td',[row.mt_reason,row.usage_reason].filter(Boolean).map(r=>reasons[r]||r).join('；')||'配置可用'));$('preview-rows').append(tr);}
    $('enable').disabled=false;
  }catch(e){message(e.message);}});
  async function save(enabled) { try { await api('/plan',{method:'PUT',body:JSON.stringify(input(enabled))}); $('live-confirm').checked=$('notify-confirm').checked=$('trust-confirm').checked=false; invalidate(); await loadMeta(); message(enabled ? '计划已启用，从下一轮开始检测。' : '配置已保存，计划保持暂停。'); } catch(e){message(e.message);} }
  $('plan-form').addEventListener('submit',e=>{e.preventDefault();save(true);}); $('save').addEventListener('click',()=>save(false));
  $('pause').addEventListener('click',async()=>{try{await api('/pause',{method:'POST'});await loadMeta();await refresh();message('计划已暂停，后续请求停止。');}catch(e){message(e.message);}});
  for(const id of ['gpt-model','gpt-protocol','gpt-reference','claude-model','claude-protocol','claude-reference','notify-enabled']) $(id).addEventListener('change',()=>{
    if(id.endsWith('-model')){const prefix=id.split('-')[0];const model=meta.models.find(m=>m.model===$(id).value);if(model) $(prefix+'-protocol').value=model.protocol;} invalidate();
  });
  async function webhook(value){$('webhook').value='';try{await api('/notification',{method:'PUT',body:JSON.stringify({webhook:value})});await loadMeta(true);message(value?'Webhook 已保存，未发送通知。':'Webhook 已清除。');}catch(e){message(e.message);}}
  $('notification-form').addEventListener('submit',e=>{e.preventDefault();webhook($('webhook').value.trim());});$('clear-webhook').addEventListener('click',()=>webhook(''));
  $('refresh').addEventListener('click',refresh);
  $('refresh-models').addEventListener('click',async()=>{try{await loadMeta(true);message('共用模型选项已更新，当前配置已保留。');}catch(e){message(e.message);}});
  $('cancel-batch').addEventListener('click',async()=>{const id=$('cancel-batch').dataset.batchId;try{await api('/batches/'+encodeURIComponent(id)+'/cancel',{method:'POST'});await refresh();}catch(e){message(e.message);}});
  for(const [id,format,ext] of [['download-json','json','json'],['download-markdown','markdown','md']]) $(id).addEventListener('click',async()=>{const batch=selected;if(!batch)return;try{const response=await fetch('/api/daily-checks/batches/'+encodeURIComponent(batch)+'/export?format='+format);if(!response.ok)throw new Error('报告下载失败');Workbench.download(await response.text(),batch+'.'+ext,format==='json'?'application/json':'text/markdown');}catch(e){message(e.message);}});
  $('retry-notification').addEventListener('click',async()=>{const id=$('retry-notification').dataset.batchId;try{await api('/batches/'+encodeURIComponent(id)+'/retry-notification',{method:'POST',body:JSON.stringify({confirm_notify:$('retry-confirm').checked})});$('retry-confirm').checked=false;await refresh();}catch(e){message(e.message);}});
  window.addEventListener('pagehide',()=>{clearInterval(timer);$('webhook').value='';generation++;});
  loadMeta().then(refresh).then(()=>{timer=setInterval(refresh,5000);}).catch(e=>message(e.message));
})();
