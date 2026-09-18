const $=id=>document.getElementById(id), api=(path,options)=>Workbench.api('./api/'+path,options);
let metadata, channels=[], preview=null, active=null, poll=null;
const states={passed:'通过',failed:'失败',unconfirmed:'待确认',not_run:'未执行',running:'进行中',completed:'已完成',cancelled:'已停止',interrupted:'已中断'};
function plan(){
  const models=$('models').value.split('\n').map(x=>x.trim()).filter(Boolean).map(line=>{const p=line.split('=');if(p.length>2)throw new Error('模型映射格式应为 标准模型=上游模型');return {model:p[0].trim(),upstream_model:p[1]?.trim()||''};});
  const groups=Object.keys(metadata.templates).filter(id=>$(`use-${id}`).checked).map(id=>({name:$(`group-${id}`).value.trim(),template:id,include_responses:id==='openai_common'&&$('include-responses').checked}));
  if(!models.length||!groups.length)throw new Error('至少填写一个模型并选择一个目标场景。');
  return {channel_id:Number($('channel').value)||null,base_url:$('base-url').value.trim(),profile:ProtocolProfileForm.get('probe'),models,groups,
    total_timeout:Number($('total-timeout').value),first_byte_timeout:Number($('first-timeout').value),idle_timeout:Number($('idle-timeout').value)};
}
function invalidate(){preview=null;$('start').disabled=true;$('preview-list').replaceChildren();}
function running(value){$('setup').disabled=value;$('preview').disabled=value;$('start').disabled=value||!preview;$('stop').hidden=!value;}
function message(error){$('error').textContent=error?.message||'';}
async function history(){const data=await api('runs');$('history').replaceChildren(...data.runs.map(r=>{const b=Workbench.node('button',`${new Date(r.created_at*1000).toLocaleString()} · ${r.mode} · ${states[r.state]||r.state}`,'secondary');b.type='button';b.onclick=()=>show(r.id).catch(message);return b;}));}
async function show(id){
  const report=await api('runs/'+id), c=report.conclusion;
  $('report').hidden=false;
  const types=await ProtocolProfileForm.options;
  $('conclusion').replaceChildren(Workbench.node('p',`${states[report.state]||report.state} · ${report.config.mode==='mock'?'本地 Mock 演示':'候选渠道直测'}`),
    Workbench.node('p',`声明：${types.upstream_types[c.declared_type]}；拟配置：${types.channel_types[c.proposed_type]}；推荐：${types.channel_types[c.recommended_type]}`),
    Workbench.node('p',c.recommendation_basis,'hint'),...c.warnings.map(w=>Workbench.node('p',w,'error')));
  $('group-results').replaceChildren(...c.groups.map(g=>{const card=Workbench.node('article','',`panel ${g.state}`);card.append(Workbench.node('h3',`${g.name}：${g.label}`),...g.reasons.map(r=>Workbench.node('p',r)));return card;}));
  $('results').replaceChildren(...report.probes.map(p=>{const row=document.createElement('tr');for(const text of [`${p.model} · ${p.label}`,states[p.status]||p.status,`${p.http_status??'—'} / ${p.protocol_completed?'已结束':'未确认'}`,`总计 ${p.total_ms??'—'} ms；首字 ${p.ttft_ms??'不适用或未观测'}${p.ttft_ms==null?'':' ms'}`,metadata.errors[p.error_class]||p.error_class||'本项检查通过'])row.append(Workbench.node('td',text));return row;}));
  $('checklist').replaceChildren(...c.checklist.map(x=>Workbench.node('li',x)));
  for(const format of ['json','html'])$(`export-${format}`).href=`./api/runs/${id}/export/${format}`;
  if(active===id&&report.state!=='running'){active=null;clearInterval(poll);poll=null;running(false);invalidate();$('message').textContent='本轮协议探测已结束。';await history();}
  return report;
}
$('probe-form').addEventListener('input',()=>{if(!active)invalidate();});
$('channel').addEventListener('change',()=>{const channel=channels.find(c=>c.id===Number($('channel').value));$('temporary').hidden=Boolean(channel);$('api-key').value='';ProtocolProfileForm.set('probe',channel?.protocol_profile||{});invalidate();});
$('mode').addEventListener('change',()=>{$('live-confirm-label').hidden=$('mode').value!=='live';$('confirm-live').checked=false;invalidate();});
$('preview').addEventListener('click',async()=>{message();try{preview=await api('preview',{method:'POST',body:JSON.stringify(plan())});const list=document.createElement('ul');for(const p of preview.probes)list.append(Workbench.node('li',`${p.model}${p.upstream_model!==p.model?' → '+p.upstream_model:''} · ${p.label}`));$('preview-list').replaceChildren(Workbench.node('p',`共 ${preview.request_count} 次请求，每项尝试 1 次，最长 ${preview.maximum_seconds} 秒。网关内部重试另行核对。`),list);$('start').disabled=!preview.can_execute;if(!preview.can_execute)throw new Error('OAuth 直连需要后续网关验证，第一阶段使用供应商 API Key 接口。');}catch(e){message(e);}});
$('probe-form').addEventListener('submit',async event=>{event.preventDefault();message();if(!preview)return;let body;try{body={...plan(),api_key:$('api-key').value,mode:$('mode').value,confirm_live:$('confirm-live').checked,preview_fingerprint:preview.fingerprint};running(true);const r=await api('runs',{method:'POST',body:JSON.stringify(body)});$('api-key').value='';active=r.id;$('message').textContent='正在逐项探测。';await show(active);if(active)poll=setInterval(()=>show(active).catch(message),1000);}catch(e){running(false);message(e);}finally{if(body)body.api_key='';}});
$('stop').addEventListener('click',async()=>{if(active){const id=active;try{await api(`runs/${id}/stop`,{method:'POST'});await show(id);}catch(e){message(e);}}});
(async()=>{metadata=await api('meta');await ProtocolProfileForm.mount($('profile'),'probe');ProtocolProfileForm.set('probe');channels=(await Workbench.api('/api/registry/channels')).channels.filter(c=>c.enabled);for(const c of channels)$('channel').append(new Option(Workbench.label(c),c.id));const requested=new URLSearchParams(location.search).get('channel');if(channels.some(c=>String(c.id)===requested)){$('channel').value=requested;$('channel').dispatchEvent(new Event('change'));}for(const [id,value]of Object.entries(metadata.templates)){const box=Workbench.node('div','','group'),label=Workbench.node('label',value.name,'check'),check=document.createElement('input'),name=document.createElement('input');check.type='checkbox';check.id=`use-${id}`;label.prepend(check);name.id=`group-${id}`;name.value=id;name.maxLength=80;name.setAttribute('aria-label',value.name+'目标分组');box.append(label,name);$('groups').append(box);}await history();$('message').textContent='准备就绪。先预览请求数量，再开始探测。';})().catch(message);
