'use strict';
const $ = (id) => document.getElementById(id);
const modes = {
  'account-test': ['单号质量与极限', 'ACCOUNT BENCHMARK', '先确认账号可用，再观察每一阶并发下的表现。', '单号测试'],
  'pool-test': ['号池容量与扩容效率', 'POOL CAPACITY', '逐级增加虚拟账号，比较健康、单号失效和半数退出后的容量。', '号池容量'],
  'gateway-test': ['网关负载与技术极限', 'GATEWAY CAPACITY', '观察在途请求、吞吐、延迟与停止压力后的恢复情况。', '网关极限'],
  'long-task-test': ['长任务稳定性与恢复', 'LONG TASK RECOVERY', '运行持续流和连续步骤，从安全检查点恢复失败的步骤。', '长任务恢复'],
  'chaos-test': ['故障注入与自动恢复', 'FAULT INJECTION', '验证限流、超时、断流和上游不可用时的响应与恢复。', '故障注入']
};
const statuses = {running:'运行中',stopping:'正在停止',completed:'已完成',interrupted:'已停止',incomplete:'未完成',failed:'运行失败',partial_reconstructed:'部分结果'};
const errorNames = {http_error:'HTTP 错误',connect_timeout:'连接超时',request_timeout:'请求超时',read_timeout:'读取超时',stream_disconnect:'流中断',incomplete_stream:'响应不完整',invalid_response:'响应格式错误',connection_error:'连接错误',connection_pool_exhausted:'连接池耗尽',cancelled:'请求取消',transport_error:'传输错误'};
let mode = 'account-test', token = '', selected = null, active = null, submitting = false, refreshing = false;
let currentJob = null;
let sustainedSupported = false;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = (n) => n == null ? '—' : (n * 100).toFixed(1) + '%';
const number = (n) => n == null ? '—' : Math.round(n).toLocaleString('zh-CN');
const duration = (s) => s < 60 ? Math.round(s) + ' 秒' : Math.floor(s/60) + ' 分 ' + Math.round(s%60) + ' 秒';
const environment = () => document.querySelector('[name="environment"]:checked').value;
const integers = (s) => s.split(/[,，、\s]+/).filter(Boolean).map(Number);

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json','X-Relay-UI':token},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '无法连接本地服务');
  return data;
}
function showError(message) {$('form-error').textContent=message;$('form-error').hidden=!message;}
function setMode(next) {
  if (!modes[next]) return;
  mode = next;
  document.querySelectorAll('.nav-item').forEach(b=>{b.classList.toggle('active',b.dataset.mode===mode);b.removeAttribute('aria-current');if(b.dataset.mode===mode)b.setAttribute('aria-current','page');});
  const [title, eyebrow, description, short] = modes[mode];
  $('page-title').textContent=title;document.querySelector('.eyebrow').textContent=eyebrow;$('page-description').textContent=description;$('breadcrumb').textContent=short;
  const mockOnly = ['pool-test','chaos-test'].includes(mode);
  const loadCapable = ['account-test','gateway-test'].includes(mode);
  $('load-controls').hidden=!loadCapable;
  $('preset').querySelector('[value="sustained"]').disabled=!loadCapable;
  if(!loadCapable){$('load-mode').value='requests';$('workload-profile').value='short';if($('preset').value==='sustained')$('preset').value='custom';}
  document.querySelector('[name="environment"][value="live"]').disabled=mockOnly;
  if(mockOnly)document.querySelector('[name="environment"][value="mock"]').checked=true;
  $('pool-field').hidden=mode!=='pool-test';$('steps-field').hidden=mode!=='long-task-test';
  $('setup-footnote').textContent=mode==='account-test'?'单号测试请让该调用 Key 只路由到目标账号。':mode==='gateway-test'?'真实目标的结果同时受网关与上游影响。':'完成后可下载完整报告和请求指标。';
  syncEnvironment();showError('');
}
function syncEnvironment() {
  const live=environment()==='live';
  $('live-fields').hidden=!live;$('mock-note').hidden=live;$('live-confirm').hidden=!live;
  ['base-url','api-key','model'].forEach(id=>{$(id).required=live;$(id).disabled=!live;});
  $('confirm-live').required=live;$('confirm-live').checked=false;
  $('timeout').value=live?90:mode==='chaos-test'?.15:$('workload-profile').value==='long'?30:3;
  syncLoad();
}
function syncLoad() {
  const capable=['account-test','gateway-test'].includes(mode), timed=capable&&$('load-mode').value==='duration', long=capable&&$('workload-profile').value==='long';
  $('duration-fields').hidden=!timed;$('samples-field').hidden=timed;$('output-fields').hidden=!long;
  $('samples').disabled=timed;['stage-duration','max-stage-requests'].forEach(id=>$(id).disabled=!timed);
  $('output-tokens').disabled=!long;$('limit-field').disabled=!long;
  estimate();
}
function estimate() {
  const stages=integers($('stages').value), samples=Number($('samples').value);
  if(['account-test','gateway-test'].includes(mode)&&$('load-mode').value==='duration'){
    $('request-estimate').textContent=`每阶持续补发 ${$('stage-duration').value} 秒，最多 ${$('max-stage-requests').value} 条。到时停止补发，等待在途请求结束，再串行探测恢复。`;return;
  }
  const requests=stages.reduce((sum,c)=>sum+Math.max(samples,c*2),0);
  $('request-estimate').textContent=['pool-test','chaos-test','long-task-test'].includes(mode)?'每个场景分别计数，结束后自动进行恢复探测。':`预计 ${Number.isFinite(requests)?requests:'—'} 次负载请求，另含恢复探测。`;
}
function busy() {
  const needsUpdate=!sustainedSupported&&['account-test','gateway-test'].includes(mode)&&($('load-mode').value==='duration'||$('workload-profile').value==='long');
  $('start-button').disabled=Boolean(active)||submitting||needsUpdate;
  $('start-button').innerHTML=submitting?'正在启动…':active?'测试进行中…':needsUpdate?'等待新版本地服务…':'开始测试 <span aria-hidden="true">↗</span>';
}
function chart(stages) {
  const values=stages.filter(s=>s.p95_latency_ms!=null);
  if(!values.length){$('chart').innerHTML='<div class="empty-chart"><span class="empty-chart-icon">∿</span><strong>等待有效响应</strong><p>成功请求的 P95 延迟会显示在这里。</p></div>';return;}
  const width=620,height=185,left=54,right=24,top=16,bottom=30,max=Math.max(...values.map(s=>s.p95_latency_ms),1)*1.2;
  const x=(i)=>left+(width-left-right)*(values.length===1?.5:i/(values.length-1));
  const y=(v)=>height-bottom-(height-top-bottom)*v/max;
  let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="各阶段 P95 延迟图">`;
  for(let i=0;i<4;i++){const v=max*i/3, yy=y(v);svg+=`<line x1="${left}" x2="${width-right}" y1="${yy}" y2="${yy}" stroke="#e7edf6" stroke-dasharray="3 4"/><text x="${left-9}" y="${yy+4}" text-anchor="end">${Math.round(v)}</text>`;}
  const line=values.map((s,i)=>`${x(i)},${y(s.p95_latency_ms)}`).join(' ');
  if(values.length>1){svg+=`<polygon points="${x(0)},${height-bottom} ${line} ${x(values.length-1)},${height-bottom}" fill="#edf2ff"/><polyline points="${line}" fill="none" stroke="#315bea" stroke-width="2.5"/>`;}
  values.forEach((s,i)=>{svg+=`<circle cx="${x(i)}" cy="${y(s.p95_latency_ms)}" r="4" fill="#315bea" stroke="white" stroke-width="2"><title>${esc(s.stage)}：${s.p95_latency_ms.toFixed(1)} ms</title></circle>`;if(values.length<13||i%Math.ceil(values.length/10)===0)svg+=`<text x="${x(i)}" y="${height-8}" text-anchor="middle">${s.concurrency}</text>`;});
  $('chart').innerHTML=svg+'</svg>';
}
function occupancyChart(job) {
  const selectedStage=$('occupancy-stage').value, stages=(job.stages||[]).filter(s=>s.occupancy);
  const stageOptions='<option value="">最新阶段</option>'+stages.map(s=>`<option value="${esc(s.stage)}">${esc(s.stage)}</option>`).join('');
  if($('occupancy-stage').innerHTML!==stageOptions)$('occupancy-stage').innerHTML=stageOptions;
  $('occupancy-stage').value=stages.some(s=>s.stage===selectedStage)?selectedStage:'';
  const o=selectedStage&&stages.find(s=>s.stage===selectedStage)?.occupancy||job.occupancy||stages.at(-1)?.occupancy;
  if(!o){['live-inflight','mean-inflight','full-occupancy'].forEach(id=>$(id).textContent='—');$('occupancy-chart').innerHTML='<p class="muted">此记录没有占用时间线</p>';$('occupancy-note').textContent='新测试会记录实际在途曲线；旧报告不推算账号占用。';return;}
  $('live-inflight').textContent=number(o.current_inflight);$('mean-inflight').textContent=o.mean_inflight.toFixed(2);$('full-occupancy').textContent=pct(o.target_occupancy_ratio);
  const points=o.series||[], w=620,h=210,l=42,r=20,t=18,b=32,end=Math.max(points.at(-1)?.seconds||0,.1),max=Math.max(o.target,o.peak_inflight,1)*1.15;
  const x=v=>l+(w-l-r)*v/end,y=v=>h-b-(h-t-b)*v/max;
  let svg=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="客户端在途与接收中请求数随时间变化">`;
  for(let i=0;i<4;i++){const v=max*i/3;svg+=`<line x1="${l}" x2="${w-r}" y1="${y(v)}" y2="${y(v)}" stroke="#e7edf6"/><text x="${l-8}" y="${y(v)+4}" text-anchor="end">${v.toFixed(1)}</text>`;}
  svg+=`<line x1="${l}" x2="${w-r}" y1="${y(o.target)}" y2="${y(o.target)}" stroke="#8c96a8" stroke-dasharray="5 4"/>`;
  if(o.drain_seconds>0&&o.load_seconds<end)svg+=`<rect x="${x(o.load_seconds)}" y="${t}" width="${x(end)-x(o.load_seconds)}" height="${h-t-b}" fill="#fff3df"/><text x="${x(o.load_seconds)+4}" y="${t+12}">收尾</text>`;
  for(const [key,color] of [['inflight','#315bea'],['receiving','#087f69']]){
    const path=points.map((p,i)=>`${i?'H':'M'} ${x(p.seconds)} ${i?'V':''} ${y(p[key])}`).join(' ');
    svg+=`<path d="${path}" stroke="${color}" stroke-width="2" fill="none"/>`;
  }
  for(let i=0;i<=4;i++)svg+=`<text x="${x(end*i/4)}" y="${h-8}" text-anchor="middle">${(end*i/4).toFixed(1)}s</text>`;
  $('occupancy-chart').innerHTML=svg+'</svg>';
  const reasons={duration:'时长到达',request_count:'请求数完成',request_cap:'请求上限提前到达',stopped:'用户停止'};
  $('occupancy-note').textContent=`${o.stage} · 目标 ${o.target} / 峰值在途 ${o.peak_inflight} / 峰值接收中 ${o.peak_receiving}。蓝色为在途，绿色为接收中，虚线为目标。负载 ${o.load_seconds.toFixed(1)} 秒，收尾 ${o.drain_seconds.toFixed(1)} 秒${o.stop_reason?'；'+(reasons[o.stop_reason]||o.stop_reason):''}。真实账号生成占用尚未接入服务端证据。`;
}
function renderJob(job) {
  if(selected!==job.id)$('occupancy-stage').value='';currentJob=job;
  selected=job.id;const running=['running','stopping'].includes(job.status), m=job.metrics, a=job.analysis||{};
  $('result-title').textContent=job.name+' · '+(job.environment==='mock'?'Mock':'真实环境');
  $('result-status').textContent=statuses[job.status]||job.status;$('result-status').className='badge '+(running?'running':job.status==='completed'?'success':job.status==='failed'?'failed':'neutral');
  const phase=job.phase==='recovery'?'串行恢复探测':job.occupancy?.phase==='draining'?'已停止补发，等待在途请求结束':job.phase==='load'?'负载请求进行中':'准备连接目标';
  $('run-context').textContent=running?phase+' · '+(job.occupancy?.stage||job.current_stage||''):job.message||('记录 '+job.id.slice(0,8)+' · '+new Date(job.started_at*1000).toLocaleString('zh-CN'));
  $('elapsed').textContent=duration(job.elapsed_seconds);$('stop-button').hidden=!running;
  $('progress-bar').classList.toggle('working',running);$('progress-bar').style.width=running?'40%':'100%';
  $('metric-success').textContent=pct(m.success_rate);$('metric-success').className=m.success_rate==null?'':m.success_rate>=.99?'good':'bad';
  $('metric-complete').textContent=pct(m.completeness_rate);$('metric-ttft').textContent=m.p50_ttft_ms==null?'—':number(m.p50_ttft_ms)+' ms';
  $('metric-p95').textContent=m.p95_latency_ms==null?'—':number(m.p95_latency_ms)+' ms';$('metric-count').textContent=number(m.requests);
  $('metric-stable').textContent=number(a.max_stable_concurrency);$('stable-caption').textContent=a.range_censored?'测试范围内通过，未测到上限':'客户端请求口径';
  const stages=job.stages||[];$('stage-count').textContent=stages.length?stages.length+' 个已完成阶段':'等待阶段完成';
  $('stage-rows').innerHTML=stages.length?stages.map(s=>`<tr><td>${esc(s.stage)}</td><td>${s.concurrency}</td><td>${s.samples}</td><td>${pct(s.success_rate)}</td><td>${number(s.p95_latency_ms)}</td></tr>`).join(''):'<tr><td colspan="5" class="table-empty">当前阶段完成后会自动更新。</td></tr>';
  chart(stages);
  occupancyChart(job);
  const errors=Object.entries(m.errors);$('error-summary').hidden=!errors.length;$('error-summary').textContent=errors.map(([k,n])=>(errorNames[k]||k)+' × '+n).join(' · ');
  const notes=[];if(job.environment==='mock')notes.push('Mock 结果不代表真实账号或号池能力。');
  if(a.low_confidence)notes.push('样本量不足，当前结论标记为低置信度。');
  if(m.success_rate!=null&&m.success_rate<.99)notes.push('检测到请求失败，请查看阶段明细与报告。');
  if(stages.some(s=>s.occupancy?.stop_reason==='request_cap'))notes.push('请求上限提前到达，未完成设定持续时长。');
  if(a.final_complete!==undefined)notes.push(`长任务：${a.confirmed_steps}/${a.expected_steps} 步完成，恢复 ${a.recovery_count} 次。`);
  if(m.latency_window_samples===10000)notes.push('顶部延迟指标基于最近 10,000 个成功样本。');
  $('result-note').textContent=notes.join(' ')||'成功率按负载请求计算，恢复探测单独记录。';
  $('downloads').hidden=!job.downloads.includes('report.md');
  [['download-report','report.md'],['download-summary','summary.json'],['download-raw','results.jsonl']].forEach(([id,file])=>{$(id).href=`/api/jobs/${job.id}/${file}`;$(id).setAttribute('download',file);});
}
function renderHistory(jobs) {
  const html=jobs.length?jobs.slice(0,9).map(j=>`<button class="history-card ${j.id===selected?'selected':''}" data-id="${j.id}"><span class="history-card-top">${esc(j.name)}<span class="badge ${j.environment==='mock'?'mock-badge':'live-badge'}">${j.environment==='mock'?'Mock':'真实'}</span></span><span class="history-meta"><span>${esc(statuses[j.status]||j.status)} · ${number(j.metrics.requests)} 请求</span><span>${pct(j.metrics.success_rate)}</span></span><span class="history-time">${new Date(j.started_at*1000).toLocaleString('zh-CN')}</span></button>`).join(''):'<p class="muted">还没有测试记录。完成首次测试后会保存在这里。</p>';
  if($('history').innerHTML!==html)$('history').innerHTML=html;
}
async function selectJob(id) {try{renderJob(await api('/api/jobs/'+id));await refresh();}catch(e){showError(e.message);}}
async function refresh() {
  if(refreshing)return;refreshing=true;
  try{const state=await api('/api/state');token=state.csrf;sustainedSupported=state.ui_schema_version>=2;active=state.jobs.find(j=>['running','stopping'].includes(j.status))?.id||null;
    $('connection').textContent='本地服务已连接';$('local-address').textContent=location.host;
    if(!selected&&active)selected=active;if(selected)renderJob(await api('/api/jobs/'+selected));renderHistory(state.jobs);busy();
  }catch(e){$('connection').textContent='本地服务连接中断';}finally{refreshing=false;}
}
$('test-form').addEventListener('submit',async(event)=>{event.preventDefault();showError('');if(active||submitting)return;
  if(!sustainedSupported&&($('load-mode').value==='duration'||$('workload-profile').value==='long')){showError('请重启本地控制台服务后使用持续并发。');return;}
  const live=environment()==='live', stages=integers($('stages').value);
  if(!stages.length||stages.some((c,i)=>!Number.isInteger(c)||c<1||c>1200||(i>0&&c<=stages[i-1]))){showError('并发阶梯需按从小到大填写，例如 1, 2, 3, 5。');return;}
  if(live&&!$('confirm-live').checked){showError('请先确认向该接口发送真实请求。');return;}
  const body={mode,environment:environment(),base_url:$('base-url').value.trim(),model:$('model').value.trim(),api_key:live?$('api-key').value.trim():'',confirm_live:live&&$('confirm-live').checked,
    load_mode:$('load-mode').value,stage_duration:Number($('stage-duration').value),max_stage_requests:Number($('max-stage-requests').value),
    workload_profile:$('workload-profile').value,output_tokens:Number($('output-tokens').value),limit_field:$('limit-field').value,
    samples:Number($('samples').value),timeout:Number($('timeout').value),stages,pool_sizes:integers($('pool-sizes').value),steps:Number($('steps').value)};
  submitting=true;busy();
  try{const job=await api('/api/jobs',body);$('api-key').value='';active=job.id;renderJob(job);await refresh();}catch(e){showError(e.message);}finally{body.api_key='';submitting=false;busy();}
});
$('stop-button').addEventListener('click',async()=>{if(!selected)return;try{renderJob(await api('/api/jobs/'+selected+'/stop',{}));}catch(e){showError(e.message);}});
$('mode-nav').addEventListener('click',e=>{const button=e.target.closest('[data-mode]');if(button)setMode(button.dataset.mode);});
$('history').addEventListener('click',e=>{const button=e.target.closest('[data-id]');if(button)selectJob(button.dataset.id);});
document.querySelectorAll('[name="environment"]').forEach(el=>el.addEventListener('change',syncEnvironment));
$('preset').addEventListener('change',()=>{const preset=$('preset').value;if(preset==='sustained'){$('stages').value='5';$('load-mode').value='duration';$('stage-duration').value=60;$('max-stage-requests').value=1000;$('workload-profile').value='long';$('output-tokens').value=1024;}else if(preset==='quick'||preset==='ladder'){$('load-mode').value='requests';$('workload-profile').value='short';$('stages').value=preset==='quick'?'1':'1, 2, 3, 5';$('samples').value=preset==='quick'?8:30;}syncLoad();});
['load-mode','workload-profile'].forEach(id=>$(id).addEventListener('change',()=>{$('preset').value='custom';syncLoad();}));
['stage-duration','max-stage-requests','output-tokens'].forEach(id=>$(id).addEventListener('input',()=>{$('preset').value='custom';estimate();}));
$('occupancy-stage').addEventListener('change',()=>{if(currentJob)occupancyChart(currentJob);});
['stages','samples'].forEach(id=>$(id).addEventListener('input',()=>{$('preset').value='custom';estimate();}));
$('refresh-history').addEventListener('click',refresh);
setMode(mode);refresh();setInterval(refresh,1500);
