'use strict';
const FaderUI=(()=>{
  const types=['short','medium','long'];
  const ids=['fader-duration','fader-cap','fader-ceiling','fader-interval'];
  let timer=null,pending=null,sending=false,jobId=null,version=0;
  const isMode=()=>['account-test','gateway-test'].includes(mode)&&$('load-mode').value==='faders';
  const values=()=>types.map(p=>Number($('fader-'+p+'-number').value));
  const liveJob=()=>currentJob?.faders&&currentJob.id===active&&currentJob.status==='running'?currentJob:null;
  function paint(){
    const maximum=Math.max(1,Number($('fader-range').value)||20,...values());$('fader-range').value=maximum;
    types.forEach(p=>{const slider=$('fader-'+p);slider.max=maximum;slider.value=$('fader-'+p+'-number').value;slider.style.setProperty('--fader-fill',(Number(slider.value)/maximum*100)+'%');slider.setAttribute('aria-valuetext',slider.value+' 条目标在途请求');$('fader-'+p+'-max').textContent=maximum;});
    $('fader-target-total').textContent=number(values().reduce((a,b)=>a+b,0));
  }
  function config(){return {targets:values(),output_limits:types.map(p=>Number($('fader-'+p+'-limit').value)),duration:Number($('fader-duration').value),max_requests:Number($('fader-cap').value),max_inflight:Number($('fader-ceiling').value),refill_interval:Number($('fader-interval').value)};}
  function sync(){
    const on=isMode(),live=liveJob();$('fader-fields').hidden=!on;$('fader-panel').hidden=!(on||currentJob?.faders);
    ids.forEach(id=>$(id).disabled=!on||Boolean(active));
    types.forEach(p=>{const locked=Boolean(active)&&!(live&&live.faders.accepting);$('fader-'+p).disabled=locked;$('fader-'+p+'-number').disabled=locked;$('fader-'+p+'-limit').disabled=Boolean(active)||submitting;});
    $('fader-pause').hidden=!live;$('fader-pause').disabled=Boolean(live&&!live.faders.accepting);
    if(!active&&!currentJob?.faders)$('fader-control-state').textContent='启动后可实时调节；目标为 0 时不发请求';
    paint();
  }
  async function send(){
    if(sending)return;sending=true;
    try{
      while(pending){
        const live=liveJob();if(!live){pending=null;break;}
        const update=pending;pending=null;
        const job=await api('/api/jobs/'+live.id+'/faders',update);
        if(selected===job.id)renderJob(job);
      }
      $('fader-control-error').hidden=true;
    }catch(e){pending=null;$('fader-control-error').textContent=e.message;$('fader-control-error').hidden=false;}
    finally{sending=false;if(currentJob)render(currentJob);}
  }
  function queue(update){pending={...(pending||{}),...update};if(timer)clearTimeout(timer);timer=setTimeout(send,200);}
  function init(){
    types.forEach(p=>{
      $('fader-'+p).addEventListener('input',()=>{$('fader-'+p+'-number').value=$('fader-'+p).value;paint();if(liveJob())queue({targets:values()});});
      $('fader-'+p+'-number').addEventListener('change',()=>{paint();if(liveJob())queue({targets:values()});});
    });
    $('fader-range').addEventListener('change',paint);
    $('fader-pause').addEventListener('click',()=>{const live=liveJob();if(live)queue({paused:!live.faders.paused});});
    paint();
  }
  function render(job){
    const f=job.faders;if(!f){sync();return;}
    if(jobId!==job.id){jobId=job.id;version=0;}
    if(f.version<version)return;version=f.version;
    if(!pending&&!sending&&job.status==='running')types.forEach((p,i)=>{const input=$('fader-'+p+'-number');if(document.activeElement!==input)input.value=f.targets[i];});
    const editable=job.id===active&&job.status==='running'&&f.accepting;
    $('fader-control-state').textContent=editable?(f.paused?'已暂停补发；在途请求自然结束':`运行中 · 每 ${f.refill_interval} 秒补足目标`):job.status==='running'?'已到运行上限，正在收尾':'记录已结束；推子值可用于新测试';
    $('fader-pause').textContent=f.paused?'继续补发':'暂停补发';
    $('fader-issued').textContent=number(f.issued_requests);
    $('fader-inflight').textContent=number(f.inflight);
    $('fader-waiting').textContent=number(f.channels.reduce((n,c)=>n+c.waiting,0));
    $('fader-receiving').textContent=number(f.channels.reduce((n,c)=>n+c.receiving,0));
    for(const c of f.channels){$('fader-'+c.profile+'-state').textContent=`等待 ${c.waiting} · 接收中 ${c.receiving} · 完成 ${c.complete}`;}
    const sec=v=>v==null?'—':v.toFixed(2),states={...burstStates,waiting_headers:'等待响应头'};
    const rows=[...f.timeline].sort((a,b)=>(a.end_seconds!=null)-(b.end_seconds!=null)||b.start_seconds-a.start_seconds).slice(0,30);
    $('fader-rows').innerHTML=rows.length?rows.map(r=>`<tr><td>${esc(r.label)} · ${profiles[r.profile]}</td><td>${esc(states[r.state]||r.state)}</td><td>${r.http_status||'—'}</td><td>${sec(r.wait_seconds)}</td><td>${sec(r.start_seconds)}</td><td>${sec(r.first_output_seconds)}</td><td>${number(r.output_chars)}</td></tr>`).join(''):'<tr><td colspan="7" class="table-empty">推高任一路，观察新请求。</td></tr>';
    $('fader-window').textContent=`优先显示在途与最新请求 · 当前 ${rows.length} 条 / 累计 ${f.issued_requests} 条`;
    const actions={start:'启动',adjust:'调整',pause:'暂停补发',duration:'到时收尾',request_cap:'达到请求上限',stopped:'停止',finished:'结束'};
    $('fader-events').innerHTML=f.events.slice(-6).reverse().map(e=>`<div>${sec(e.at_seconds)}s · ${actions[e.action]||esc(e.action)} · 短 ${e.targets[0]} / 中 ${e.targets[1]} / 长 ${e.targets[2]}${e.paused?' · 暂停中':''}</div>`).join('');
    $('download-fader-events').hidden=!job.downloads.includes('fader-events.json');$('download-fader-events').href=`/api/jobs/${job.id}/fader-events.json`;
    sync();
  }
  return {init,sync,render,config,isMode};
})();
