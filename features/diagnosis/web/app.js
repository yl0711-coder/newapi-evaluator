(() => {
  const $ = id => document.getElementById(id), node = Workbench.node;
  const api = (path, options) => Workbench.api(`/diagnosis/api${path}`, options);
  const post = (path, body) => api(path, {method:'POST', body:JSON.stringify(body)});
  const names = {baseline:'基准组',smaller_input:'输入减半',smaller_output:'输出上限减半',toggle_stream:'切换流式'};
  const states = {running:'运行中',completed:'已结束',stopped:'已停止',interrupted:'已中断'};
  const outcomes = {pending:'待执行',not_sent:'未发送',running:'测量中',completed:'正常结束',output_limit:'达到输出上限',cancelled:'已取消',unknown:'结果未确认',incomplete_response:'响应未完整结束',rate_limited:'限流',first_content_timeout:'首段超时',idle_timeout:'文本空闲超时',total_timeout:'总超时',stream_disconnect:'连接中断',refused:'拒绝',empty_response:'空响应',invalid_response:'响应格式错误',authentication_error:'鉴权失败',permission_error:'无权限',upstream_http_error:'上游 HTTP 错误',request_rejected:'请求被拒',invalid_content_type:'流类型错误',connection_error:'连接失败'};
  let cases = [], preview = null, current = '', poll = null;
  const status = (message, error=false) => { $('status').textContent = message; $('status').classList.toggle('error',error); };
  const guard = fn => async event => { if(event) event.preventDefault(); try { await fn(event); } catch(error) { status(error.message,true); } };
  const value = id => $(id).value === '' ? null : Number($(id).value);
  const fmt = n => n == null ? '—' : Number(n).toLocaleString('zh-CN',{maximumFractionDigits:1});
  const ratio = n => n == null ? '—' : Number(n).toLocaleString('zh-CN',{maximumSignificantDigits:3})+'×';
  function invalidate() { preview=null; $('preview-panel').hidden=true; $('live-confirm').checked=false; }
  async function loadCases(selected) {
    cases = await api('/cases'); $('case-select').replaceChildren(new Option('请选择案例',''));
    for(const c of cases) $('case-select').append(new Option(`${c.stream?'流式':'非流式'} · ${fmt(c.total_tokens)} 总 Token · HTTP ${c.status_code??'未知'} · ${new Date(c.created_at*1000).toLocaleTimeString()}`,c.id));
    $('case-select').value=selected || ''; chooseCase();
  }
  function chooseCase() {
    invalidate(); const c=cases.find(c=>c.id===$('case-select').value);
    $('case-summary').textContent=c?`历史输入 ${fmt(c.input_tokens)} / 输出 ${fmt(c.output_tokens)}；耗时 ${fmt(c.latency_ms)} ms（${{unknown:'口径未知',total:'总耗时',first_content:'首段文本'}[c.latency_kind]}）。`:'先保存或选择一个案例。';
    if(c) { $('input-tokens').value=c.input_tokens>=32&&c.input_tokens<=65536?c.input_tokens:1024; $('output-tokens').value=c.output_tokens>=2&&c.output_tokens<=8192?c.output_tokens:256; }
  }
  $('case-select').addEventListener('change',chooseCase);
  $('case-form').addEventListener('submit',guard(async()=>{ const body={input_tokens:value('case-input'),output_tokens:value('case-output'),total_tokens:value('case-total'),latency_ms:value('case-latency'),latency_kind:$('case-kind').value,status_code:value('case-status'),stream:$('case-stream').checked}; const rows=await post('/cases',{cases:[body]}); await loadCases(rows[0].id); status('案例已保存。请核对测试分项。'); }));
  $('sample').addEventListener('click',guard(async()=>{ const rows=await post('/cases',{cases:[{total_tokens:1500,latency_ms:8000,latency_kind:'total',status_code:200,stream:true}]}); await loadCases(rows[0].id); status('已添加合成演示案例，不代表真实历史请求。'); }));
  $('case-file').addEventListener('change',guard(async()=>{ const file=$('case-file').files[0]; if(!file)return; if(file.size>262144)throw Error('文件不能超过 256 KiB'); let data; const text=await file.text(); try {data=JSON.parse(text);} catch {try{data=text.split(/\r?\n/).filter(x=>x.trim()).map(x=>JSON.parse(x));}catch{throw Error('文件不是有效的指标 JSON / JSONL');}} const rows=await post('/cases',Array.isArray(data)?{cases:data}:data); await loadCases(rows[0].id); $('case-file').value=''; status(`已导入 ${rows.length} 条案例。`); }));
  $('delete-case').addEventListener('click',guard(async()=>{const id=$('case-select').value;if(!id)return; await api(`/cases/${id}`,{method:'DELETE'}); await loadCases();status('案例已删除；已有运行快照保留。');}));
  $('plan-form').addEventListener('input',invalidate);
  $('mode').addEventListener('change',()=>{const live=$('mode').value==='live'; $('channel-field').hidden=!live; $('mock-field').hidden=live; $('channel').required=live; $('model').value=live?'':'diagnosis-mock';});
  $('plan-form').addEventListener('submit',guard(async()=>{
    invalidate(); const live=$('mode').value==='live'; const target={mode:$('mode').value,protocol:$('protocol').value,model:$('model').value,mock_scenario:live?'healthy':$('scenario').value,channel_id:live?Number($('channel').value):null};
    preview=await post('/preview',{case_id:$('case-select').value,target,input_tokens:value('input-tokens'),output_tokens:value('output-tokens'),repetitions:value('repetitions'),variants:[...document.querySelectorAll('[name=variant]:checked')].map(x=>x.value),timeout_seconds:value('timeout'),first_content_timeout_seconds:value('first-timeout'),idle_timeout_seconds:value('idle-timeout'),max_duration_seconds:value('duration'),max_estimated_tokens:value('budget')});
    const p=preview.plan; $('preview-summary').replaceChildren(node('p',`${p.target.alias} · ${p.target.protocol} · ${p.target.model}`),node('h3',`${p.request_count} 次串行请求 · 合计估算 ${fmt(p.estimated_tokens)} Token`),node('p',`基准：输入估算 ${p.config.input_tokens} / 输出上限 ${p.config.output_tokens} · ${p.case.stream?'流式':'非流式'}；预览 5 分钟有效。`));
    $('assumptions').replaceChildren(...p.assumptions.map(x=>node('li',x))); $('live-confirm-field').hidden=!live; $('start').textContent=live?'确认并开始真实对照':'开始本地 Mock 对照'; $('preview-panel').hidden=false; status('计划已生成，请核对后开始。');
  }));
  async function loadRuns() {const runs=await api('/runs');$('run-select').replaceChildren(new Option('选择历史运行',''));for(const r of runs)$('run-select').append(new Option(`${new Date(r.created_at*1000).toLocaleString()} · ${states[r.state]||r.state}`,r.id));$('run-select').value=current;}
  function render(value) {
    const r=value.run; $('run-state').textContent=`${states[r.state]||r.state} · ${r.results.filter(x=>!['pending','running'].includes(x.outcome)).length}/${r.results.length} 项已归档${r.stop_reason?' · '+r.stop_reason:''}`; $('stop').disabled=r.state!=='running';
    $('results').replaceChildren(...r.results.map(row=>{const tr=node('tr');for(const text of [`${row.ordinal} / ${names[row.variant]}`,outcomes[row.outcome]||row.outcome,row.http_status??'—',fmt(row.ttft_ms),fmt(row.latency_ms),`${fmt(row.usage_input_tokens)} / ${fmt(row.usage_output_tokens)}`])tr.append(node('td',text));return tr;}));
    $('group-summary').replaceChildren(...value.groups.map(g=>{const box=node('div','','metric');box.append(node('span',names[g.variant]),node('strong',`${g.completed} / ${g.attempted}`),node('p','正常结束 / 已尝试'),node('p',`成功总耗时中位数 ${fmt(g.median_success_latency_ms)} ms`),node('p',`与历史耗时比值 ${ratio(g.historical_latency_ratio)}`));return box;}));
    $('conclusion').textContent=value.conclusion; $('evidence').textContent=JSON.stringify({target:r.plan.target,results:r.results,groups:value.groups,metric_notes:value.metric_notes},null,2); $('evidence-detail').hidden=false;
    $('exports').hidden=false; $('export-json').href=`/diagnosis/api/runs/${r.id}/export/json`;$('export-md').href=`/diagnosis/api/runs/${r.id}/export/md`;$('delete-run').disabled=r.state==='running';
    return r.state;
  }
  async function refreshRun(){clearTimeout(poll);if(!current)return;const id=current;const report=await api(`/runs/${id}`);if(id!==current)return;const state=render(report);if(state==='running')poll=setTimeout(()=>refreshRun().catch(e=>status(e.message,true)),700);else await loadRuns();}
  $('start').addEventListener('click',guard(async()=>{if(!preview)throw Error('请重新预览');$('start').disabled=true;try{const r=await post('/runs',{preview_id:preview.preview_id,confirm_live:$('live-confirm').checked});current=r.id;invalidate();await loadRuns();await refreshRun();status('对照已启动。');}finally{$('start').disabled=false;}}));
  $('stop').addEventListener('click',guard(async()=>{await post(`/runs/${current}/stop`,{});await refreshRun();status('已停止；未发送项不会继续，已到上游的请求可能仍被计费。');}));
  $('run-select').addEventListener('change',guard(async()=>{current=$('run-select').value;await refreshRun();}));
  $('delete-run').addEventListener('click',guard(async()=>{await api(`/runs/${current}`,{method:'DELETE'});current='';clearTimeout(poll);await loadRuns();$('results').replaceChildren();$('group-summary').replaceChildren();$('evidence-detail').hidden=true;$('exports').hidden=true;$('run-state').textContent='运行记录已删除。';}));
  guard(async()=>{await Workbench.ready;const config=await api('/config');if(config.live_enabled){$('mode').options[1].disabled=false;$('mode').options[1].textContent='真实公共渠道';}for(const c of config.channels.filter(c=>c.enabled))$('channel').append(new Option(`${c.alias} · v${c.version}`,c.id));await loadCases();await loadRuns();status('准备就绪。先录入历史指标，或添加演示案例体验流程。');})();
})();
