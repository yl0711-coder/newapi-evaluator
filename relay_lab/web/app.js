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
let burstSupported = false;
let fadersSupported = false;
Object.assign(errorNames,{total_timeout:'请求总时限到达',first_output_timeout:'首段等待超时',stream_idle_timeout:'流空闲超时',upstream_error:'流内上游错误'});
const burstStates={...errorNames,complete:'完整结束',waiting_first_output:'等待首段',receiving:'接收中'};
const profiles={short:'短',medium:'中',long:'长'};
const burstFields=['burst-short-count','burst-medium-count','burst-long-count','burst-short-limit','burst-medium-limit','burst-long-limit','burst-capacity','burst-first-timeout','burst-idle-timeout','burst-total-timeout'];
const isBurst=()=>['account-test','gateway-test'].includes(mode)&&$('load-mode').value==='mixed_burst';
const burstTotal=()=>['short','medium','long'].reduce((sum,p)=>sum+Number($('burst-'+p+'-count').value),0);
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
  $('preset').querySelector('[value="mixed_burst"]').disabled=!loadCapable;
  $('preset').querySelector('[value="faders"]').disabled=!loadCapable;
  if(!loadCapable){$('load-mode').value='requests';$('workload-profile').value='short';if(['sustained','mixed_burst','faders'].includes($('preset').value))$('preset').value='custom';}
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
  const capable=['account-test','gateway-test'].includes(mode), fader=FaderUI.isMode(), burst=isBurst(), mixed=burst||fader, timed=capable&&$('load-mode').value==='duration', long=capable&&$('workload-profile').value==='long'&&!mixed;
  $('duration-fields').hidden=!timed;$('samples-field').hidden=timed||mixed;$('output-fields').hidden=!(long||mixed);
  $('samples').disabled=timed||mixed;['stage-duration','max-stage-requests'].forEach(id=>$(id).disabled=!timed);
  $('output-tokens').disabled=!long;$('output-tokens').closest('label').hidden=mixed;$('limit-field').disabled=!(long||mixed);
  $('workload-profile').closest('label').hidden=mixed;
  $('burst-fields').hidden=!mixed;burstFields.forEach(id=>$(id).disabled=!mixed);
  $('burst-count-fields').hidden=fader;$('burst-plan-hint').hidden=fader;
  ['short','medium','long'].forEach(p=>['count','limit'].forEach(k=>$('burst-'+p+'-'+k).disabled=!burst));
  $('mock-policy-field').hidden=!mixed||environment()==='live';$('mock-policy').disabled=!mixed||environment()==='live';
  $('timeout-field').hidden=mixed;$('timeout').disabled=mixed;$('stages-field').hidden=mixed;$('stages').disabled=mixed;
  estimate();busy();FaderUI.sync();
}
function estimate() {
  if(FaderUI.isMode()){$('request-estimate').textContent='启动后实时调节右侧推子。到时或达到请求上限停止补发并收尾；不追加恢复探测。';return;}
  const stages=integers($('stages').value), samples=Number($('samples').value);
  if(isBurst()){$('request-estimate').textContent=`同时发出 ${burstTotal()} 条原始请求，不补发、不追加恢复探测。观察短／中请求结束后，其他原始请求开始输出还是报错。`;return;}
  if(['account-test','gateway-test'].includes(mode)&&$('load-mode').value==='duration'){
    $('request-estimate').textContent=`每阶持续补发 ${$('stage-duration').value} 秒，最多 ${$('max-stage-requests').value} 条。到时停止补发，等待在途请求结束，再串行探测恢复。`;return;
  }
  const requests=stages.reduce((sum,c)=>sum+Math.max(samples,c*2),0);
  $('request-estimate').textContent=['pool-test','chaos-test','long-task-test'].includes(mode)?'每个场景分别计数，结束后自动进行恢复探测。':`预计 ${Number.isFinite(requests)?requests:'—'} 次负载请求，另含恢复探测。`;
}
function busy() {
  const needsUpdate=FaderUI.isMode()?!fadersSupported:isBurst()?!burstSupported:!sustainedSupported&&['account-test','gateway-test'].includes(mode)&&($('load-mode').value==='duration'||$('workload-profile').value==='long');
  $('start-button').disabled=Boolean(active)||submitting||needsUpdate;
  $('start-button').innerHTML=submitting?'正在启动…':active?'测试进行中…':needsUpdate?'等待新版本地服务…':(FaderUI.isMode()?'启动推子测试':'开始测试')+' <span aria-hidden="true">↗</span>';
  FaderUI.sync();
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
  const points=o.series||[], w=620,h=210,l=42,r=20,t=18,b=32,end=Math.max(points.at(-1)?.seconds||0,.1),max=Math.max(o.target,o.peak_inflight,...points.map(p=>p.target||0),1)*1.15;
  const x=v=>l+(w-l-r)*v/end,y=v=>h-b-(h-t-b)*v/max;
  let svg=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="客户端在途与接收中请求数随时间变化">`;
  for(let i=0;i<4;i++){const v=max*i/3;svg+=`<line x1="${l}" x2="${w-r}" y1="${y(v)}" y2="${y(v)}" stroke="#e7edf6"/><text x="${l-8}" y="${y(v)+4}" text-anchor="end">${v.toFixed(1)}</text>`;}
  if(!o.dynamic_target)svg+=`<line x1="${l}" x2="${w-r}" y1="${y(o.target)}" y2="${y(o.target)}" stroke="#8c96a8" stroke-dasharray="5 4"/>`;
  if(o.drain_seconds>0&&o.load_seconds<end)svg+=`<rect x="${x(o.load_seconds)}" y="${t}" width="${x(end)-x(o.load_seconds)}" height="${h-t-b}" fill="#fff3df"/><text x="${x(o.load_seconds)+4}" y="${t+12}">收尾</text>`;
  for(const [key,color] of [['inflight','#315bea'],['receiving','#087f69'],...(o.dynamic_target?[['target','#8994a8']]:[])]){
    const path=points.map((p,i)=>`${i?'H':'M'} ${x(p.seconds)} ${i?'V':''} ${y(p[key]||0)}`).join(' ');
    svg+=`<path d="${path}" stroke="${color}" stroke-width="2" fill="none"/>`;
  }
  for(let i=0;i<=4;i++)svg+=`<text x="${x(end*i/4)}" y="${h-8}" text-anchor="middle">${(end*i/4).toFixed(1)}s</text>`;
  $('occupancy-chart').innerHTML=svg+'</svg>';
  const reasons={duration:'时长到达',request_count:'请求数完成',request_cap:'请求上限提前到达',stopped:'用户停止',batch_complete:'固定批次结束'};
  $('occupancy-note').textContent=`${o.stage} · 目标 ${o.target} / 峰值在途 ${o.peak_inflight} / 峰值接收中 ${o.peak_receiving}。蓝色为在途，绿色为接收中，虚线为目标。负载 ${o.load_seconds.toFixed(1)} 秒，收尾 ${o.drain_seconds.toFixed(1)} 秒${o.stop_reason?'；'+(reasons[o.stop_reason]||o.stop_reason):''}。真实账号生成占用尚未接入服务端证据。`;
}
function renderBurst(b) {
  $('burst-panel').hidden=!b;if(!b)return;
  const rows=b.timeline||[], seconds=v=>v==null?'—':v.toFixed(2);
  $('burst-count').textContent=`已发 ${b.issued_requests} / ${b.planned_requests} · 已结束 ${b.finished_requests}`;
  $('burst-overview').textContent=`参考限制 ${b.reference_capacity}；峰值在途 ${b.peak_inflight}，峰值接收中 ${b.peak_receiving}；累计 ${number(b.total_output_chars)} 字符。发起时间跨度 ${seconds(b.launch_spread_ms)} ms。`;
  $('burst-rows').innerHTML=rows.map(r=>`<tr><td>${esc(r.label)} · ${profiles[r.profile]||esc(r.profile)}</td><td>${esc(burstStates[r.state]||r.state)}${r.upstream_error_kind?' · '+esc(r.upstream_error_kind):''}</td><td>${r.http_status||'—'}</td><td>${seconds(r.first_output_seconds)}</td><td>${seconds(r.end_seconds)}</td><td>${number(r.output_chars)}</td><td>${r.after_release?esc(r.after_release)+' 后 '+seconds(r.release_delay_seconds)+'s':'—'}</td></tr>`).join('');
  const width=640,left=62,right=24,top=24,rowHeight=27,height=Math.max(90,rows.length*rowHeight+62),end=Math.max(b.elapsed_seconds,.1),x=s=>left+(width-left-right)*s/end;
  let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="原始批次每条请求等待首段与接收时间线">`;
  for(let i=0;i<=4;i++){const t=end*i/4;svg+=`<line x1="${x(t)}" x2="${x(t)}" y1="${top-8}" y2="${height-28}" stroke="#e7edf6"/><text x="${x(t)}" y="${height-8}" text-anchor="middle">${t.toFixed(1)}s</text>`;}
  rows.forEach((r,i)=>{const y=top+i*rowHeight,stop=r.end_seconds??end,first=r.first_output_seconds;
    svg+=`<text x="${left-8}" y="${y+12}" text-anchor="end">${esc(r.label)} ${profiles[r.profile]||''}</text><rect x="${x(r.start_seconds)}" y="${y}" width="${Math.max(1,x(first??stop)-x(r.start_seconds))}" height="16" rx="3" fill="#dce2ec"><title>等待首段 ${seconds(r.wait_seconds)} 秒</title></rect>`;
    if(first!=null)svg+=`<rect x="${x(first)}" y="${y}" width="${Math.max(1,x(stop)-x(first))}" height="16" rx="3" fill="#0c987e"><title>已收 ${r.output_chars} 字符；最长块间隔 ${seconds(r.max_gap_seconds)} 秒；末段至当前或结束 ${seconds(r.silent_seconds)} 秒</title></rect>`;
    if(r.end_seconds!=null)svg+=`<circle cx="${x(stop)}" cy="${y+8}" r="3" fill="${r.state==='complete'?'#087f69':'#cf5362'}"><title>${esc(burstStates[r.state]||r.state)}</title></circle>`;
  });$('burst-chart').innerHTML=svg+'</svg>';
  const observations=b.release_observations||[];
  $('burst-releases').innerHTML='<strong>短／中请求结束后的原始请求</strong>'+ (observations.length?observations.map(e=>`<p>${esc(e.released_label)} 在 ${seconds(e.at_seconds)}s 完整结束；当时等待：${esc(e.waiting_labels.join('、')||'无')}。${e.later_output.map(p=>esc(p.label)+' 在其后 '+seconds(p.delay_seconds)+'s 开始输出').join('；')||'尚未观察到后续输出'}。${e.ended_without_output.length?'无正文结束：'+esc(e.ended_without_output.join('、'))+'。':''}</p>`).join(''):'<p>等待短／中请求完整结束。</p>')+'<p>时间对应提供排队线索；未接入服务端队列证据。表中“释放后开始”使用 '+seconds(b.release_window_seconds)+' 秒观察窗口。</p>';
}
function renderJob(job) {
  if(selected!==job.id)$('occupancy-stage').value='';currentJob=job;
  selected=job.id;const running=['running','stopping'].includes(job.status), m=job.metrics, a=job.analysis||{};
  $('result-title').textContent=job.name+' · '+(job.environment==='mock'?'Mock':'真实环境');
  $('result-status').textContent=statuses[job.status]||job.status;$('result-status').className='badge '+(running?'running':job.status==='completed'?'success':job.status==='failed'?'failed':'neutral');
  const phase=job.phase==='faders'?'三路推子运行中':job.phase==='draining'?'停止补发，等待在途结束':job.phase==='mixed_burst'?'原始混合批次进行中 · 不补发':job.phase==='recovery'?'串行恢复探测':job.occupancy?.phase==='draining'?'已停止补发，等待在途请求结束':job.phase==='load'?'负载请求进行中':'准备连接目标';
  $('run-context').textContent=running?phase+' · '+(job.occupancy?.stage||job.current_stage||''):job.message||('记录 '+job.id.slice(0,8)+' · '+new Date(job.started_at*1000).toLocaleString('zh-CN'));
  $('elapsed').textContent=duration(job.elapsed_seconds);$('stop-button').hidden=!running;
  $('progress-bar').classList.toggle('working',running);$('progress-bar').style.width=running?'40%':'100%';
  $('metric-success').textContent=pct(m.success_rate);$('metric-success').className=m.success_rate==null?'':m.success_rate>=.99?'good':'bad';
  $('metric-complete').textContent=pct(m.completeness_rate);$('metric-ttft').textContent=m.p50_ttft_ms==null?'—':number(m.p50_ttft_ms)+' ms';
  $('metric-p95').textContent=m.p95_latency_ms==null?'—':number(m.p95_latency_ms)+' ms';$('metric-count').textContent=number(m.requests);
  $('metric-stable').textContent=number(a.max_stable_concurrency);$('stable-caption').textContent=a.range_censored?'测试范围内通过，未测到上限':'客户端请求口径';
  $('metric-stable-label').textContent=job.burst?'峰值接收中':'最高通过的并发阶梯';
  if(job.faders){$('metric-stable-label').textContent='峰值客户端在途';$('metric-stable').textContent=number(job.occupancy?.peak_inflight||job.stages?.at(-1)?.occupancy?.peak_inflight);$('stable-caption').textContent='动态负载，未测定稳定容量';}
  if(job.burst){$('metric-stable').textContent=number(job.burst.peak_receiving);$('stable-caption').textContent='本批次观察值，非稳定上限';}
  const stages=job.stages||[];$('stage-count').textContent=stages.length?stages.length+' 个已完成阶段':'等待阶段完成';
  $('stage-rows').innerHTML=stages.length?stages.map(s=>`<tr><td>${esc(s.stage)}</td><td>${s.concurrency}</td><td>${s.samples}</td><td>${pct(s.success_rate)}</td><td>${number(s.p95_latency_ms)}</td></tr>`).join(''):'<tr><td colspan="5" class="table-empty">当前阶段完成后会自动更新。</td></tr>';
  chart(stages);
  occupancyChart(job);
  renderBurst(job.burst);
  FaderUI.render(job);
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
  try{const state=await api('/api/state');token=state.csrf;sustainedSupported=state.ui_schema_version>=2;burstSupported=state.ui_schema_version>=3;fadersSupported=state.ui_schema_version>=4;active=state.jobs.find(j=>['running','stopping'].includes(j.status))?.id||null;
    $('connection').textContent='本地服务已连接';$('local-address').textContent=location.host;
    if(!selected&&active)selected=active;if(selected)renderJob(await api('/api/jobs/'+selected));renderHistory(state.jobs);busy();
  }catch(e){$('connection').textContent='本地服务连接中断';}finally{refreshing=false;}
}
$('test-form').addEventListener('submit',async(event)=>{event.preventDefault();showError('');if(active||submitting)return;
  if(FaderUI.isMode()&&!fadersSupported){showError('请重启本地服务后使用三路推子。');return;}
  if(isBurst()&&!burstSupported){showError('请重启本地控制台服务后使用固定混合批次。');return;}
  if(!sustainedSupported&&($('load-mode').value==='duration'||$('workload-profile').value==='long')){showError('请重启本地控制台服务后使用持续并发。');return;}
  const live=environment()==='live', stages=FaderUI.isMode()?[1]:isBurst()?[burstTotal()]:integers($('stages').value);
  if(!stages.length||stages.some((c,i)=>!Number.isInteger(c)||c<1||c>1200||(i>0&&c<=stages[i-1]))){showError('并发阶梯需按从小到大填写，例如 1, 2, 3, 5。');return;}
  if(live&&!$('confirm-live').checked){showError('请先确认向该接口发送真实请求。');return;}
  const body={mode,environment:environment(),base_url:$('base-url').value.trim(),model:$('model').value.trim(),api_key:live?$('api-key').value.trim():'',confirm_live:live&&$('confirm-live').checked,
    load_mode:$('load-mode').value,stage_duration:Number($('stage-duration').value),max_stage_requests:Number($('max-stage-requests').value),
    workload_profile:$('workload-profile').value,output_tokens:Number($('output-tokens').value),limit_field:$('limit-field').value,
    samples:Number($('samples').value),timeout:Number($('timeout').value),stages,pool_sizes:integers($('pool-sizes').value),steps:Number($('steps').value),
    faders:FaderUI.config(),mock_admission_policy:$('mock-policy').value,
    mixed_burst:{counts:['short','medium','long'].map(p=>Number($('burst-'+p+'-count').value)),output_limits:['short','medium','long'].map(p=>Number($('burst-'+p+'-limit').value)),expected_capacity:Number($('burst-capacity').value),first_output_timeout:Number($('burst-first-timeout').value),idle_timeout:Number($('burst-idle-timeout').value),total_timeout:Number($('burst-total-timeout').value)}};
  submitting=true;busy();
  try{const job=await api('/api/jobs',body);$('api-key').value='';active=job.id;renderJob(job);await refresh();}catch(e){showError(e.message);}finally{body.api_key='';submitting=false;busy();}
});
$('stop-button').addEventListener('click',async()=>{if(!selected)return;try{renderJob(await api('/api/jobs/'+selected+'/stop',{}));}catch(e){showError(e.message);}});
$('mode-nav').addEventListener('click',e=>{const button=e.target.closest('[data-mode]');if(button)setMode(button.dataset.mode);});
$('history').addEventListener('click',e=>{const button=e.target.closest('[data-id]');if(button)selectJob(button.dataset.id);});
document.querySelectorAll('[name="environment"]').forEach(el=>el.addEventListener('change',syncEnvironment));
$('preset').addEventListener('change',()=>{const preset=$('preset').value;if(preset==='faders'){$('load-mode').value='faders';}else if(preset==='mixed_burst'){$('load-mode').value='mixed_burst';['short','medium','long'].forEach((p,i)=>{$('burst-'+p+'-count').value=[2,2,6][i];$('burst-'+p+'-limit').value=[64,512,4096][i];});}else if(preset==='sustained'){$('stages').value='5';$('load-mode').value='duration';$('stage-duration').value=60;$('max-stage-requests').value=1000;$('workload-profile').value='long';$('output-tokens').value=1024;}else if(preset==='quick'||preset==='ladder'){$('load-mode').value='requests';$('workload-profile').value='short';$('stages').value=preset==='quick'?'1':'1, 2, 3, 5';$('samples').value=preset==='quick'?8:30;}syncLoad();});
burstFields.forEach(id=>$(id).addEventListener('input',()=>{$('preset').value='custom';estimate();}));
['load-mode','workload-profile'].forEach(id=>$(id).addEventListener('change',()=>{$('preset').value='custom';syncLoad();}));
['stage-duration','max-stage-requests','output-tokens'].forEach(id=>$(id).addEventListener('input',()=>{$('preset').value='custom';estimate();}));
$('occupancy-stage').addEventListener('change',()=>{if(currentJob)occupancyChart(currentJob);});
['stages','samples'].forEach(id=>$(id).addEventListener('input',()=>{$('preset').value='custom';estimate();}));
$('refresh-history').addEventListener('click',refresh);
FaderUI.init();setMode(mode);refresh();setInterval(refresh,750);
