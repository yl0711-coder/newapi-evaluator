const $ = id => document.getElementById(id);
const api = (path, options) => Workbench.api('/api/channel-monitor' + path, options);
let state, timer, busy = false, generation = 0;
const names = {healthy:'本轮正常',transport_recovered:'请求已恢复',transport_failure:'五次请求均失败',quality_failure:'糖果答错且 MT 明确偏离',mt_mismatch:'糖果答对但 MT 明确偏离',inconclusive:'无法判定'};
const events = {minimum_channels:'保底保护：保留渠道并告警',channel_disabled:'已停用渠道',channel_enabled:'已恢复渠道',management_action_unconfirmed:'管理动作未确认，渠道已锁定，请人工核对',binding_unavailable:'目标不可用、配置变化或测试窗口已结束',externally_enabled:'渠道已被外部启用，解除自动恢复所有权',external_status:'外部停用，保持现状',round_interrupted:'本轮未完成，未确认的请求不会计为五次失败'};
Object.assign(events,{online_inventory_unavailable:'飞书完整上线名单读取失败，本轮不自动恢复',recovery_unavailable:'恢复资格或动作未确认，本轮暂停后续停用',circuit_opened:'多渠道请求故障，已熔断全部自动停用',circuit_closed:'后续定时轮次全部未锁定映射正常，已解除熔断',circuit_reset:'已人工解除熔断，计划保持暂停',disable_suspended:'本轮自动停用已暂停',floor_connectivity_recheck:'四条保底前的延迟连通性复测已完成'});
const time = value => value == null ? '—' : new Date(value * 1000).toLocaleString();
function message(text){$('message').textContent = text;}
function button(label, action){const b=Workbench.node('button',label,'secondary');b.type='button';b.addEventListener('click',()=>operate(action));return b;}
function options(id, rows, value, label){const selected=$(id).value;$(id).replaceChildren(...rows.map(r=>new Option(label(r),value(r))));if([...$(id).options].some(o=>o.value===selected))$(id).value=selected;}
async function refresh(initial=false){
  const ticket=++generation;
  const [meta,history]=await Promise.all([api('/meta'),api('/history')]);
  if(ticket!==generation)return;
  state=meta;
  const thresholds=meta.quality_rounds;
  $('quality-rules').textContent=`糖果每次完成后均检测 3 个 MT 探针。糖果答错且 MT 明确偏离，连续 ${thresholds.quality_failure} 个半小时轮次停用；糖果答对但 MT 明确偏离，连续 ${thresholds.mt_mismatch} 个半小时轮次停用。两条路径分别计数，无法判定、匹配、漏轮及手动测试会中断连续计数。`;
  $('plan-status').textContent=meta.executor.plan.enabled ? (meta.executor.running?'监控中 · 本轮运行中':'监控已启用') : '监控已暂停';
  $('schedule').textContent=`下次计划开始：${time(meta.executor.plan.next_due)}；按开始到开始计时，错过不补测。`;
  $('instance-status').textContent=meta.instance?'管理令牌已加密保存，页面不回显。':'尚未配置管理连接。';
  if(initial&&meta.instance){$('base-url').value=meta.instance.base_url;$('user-id').value=meta.instance.user_id||'';}
  options('registry-id',meta.channels.filter(c=>c.enabled&&c.credential_status==='available'),r=>String(r.id),r=>'公共渠道 #'+r.id);
  options('model',meta.models,r=>r.model,r=>r.model);
  options('expected',meta.expected_models,r=>r,r=>r);
  if(initial){if(meta.models.some(m=>m.model==='gpt-6-astra'))$('model').value='gpt-6-astra';$('expected').value='gpt-6-astra';}
  const enabled=meta.executor.plan.enabled;
  $('circuit-status').textContent=meta.executor.circuit.opened?'集中故障熔断中：自动停用暂停，检测和合格恢复继续。':'集中故障熔断未触发。';
  $('reset-circuit').disabled=enabled||!meta.executor.circuit.opened;
  for(const id of ['instance-form','binding-form'])for(const c of $(id).elements)c.disabled=enabled;
  $('run').disabled=!enabled||meta.executor.running;$('pause').disabled=!enabled;$('enable').disabled=enabled;
  $('bindings').replaceChildren();
  if(!meta.bindings.length)$('bindings').append(Workbench.node('p','尚未绑定渠道。'));
  for(const row of meta.bindings){
    const card=Workbench.node('article','','binding');card.dataset.binding=String(row.id);
    card.append(Workbench.node('strong',`New API #${row.remote_id} → 公共渠道 #${row.registry_id} · ${row.model}`));
    card.append(Workbench.node('p',`${row.locked?'已锁定':'自动管理'} · ${row.owned_reason?'自动停用，等待飞书上线资格、糖果答对且 MT 匹配':'当前无自动停用所有权'}`));
    const outcome=row.last_result?.outcome;
    card.append(Workbench.node('p',`糖果错且 MT 偏离 ${outcome==='quality_failure'?row.quality_streak:0}/${thresholds.quality_failure} · 糖果对且 MT 偏离 ${outcome==='mt_mismatch'?row.quality_streak:0}/${thresholds.mt_mismatch}`));
    if(row.last_result){card.append(Workbench.node('p',names[row.last_result.outcome]||row.last_result.outcome));if(row.last_result.modeltrace)card.append(Workbench.node('p','MT 预测：'+(row.last_result.modeltrace.prediction||'无法判定')));}
    const actions=Workbench.node('div','','actions');
    actions.append(button(row.locked?'解除锁定':'人工锁定',()=>api(`/bindings/${row.id}/lock`,{method:'PUT',body:JSON.stringify({locked:!row.locked})})));
    if(row.owned_reason)actions.append(button('人工接管并锁定',()=>api(`/bindings/${row.id}/lock`,{method:'PUT',body:JSON.stringify({locked:true,takeover:true})})));
    if(!enabled&&!row.owned_reason)actions.append(button('移除映射',()=>api(`/bindings/${row.id}`,{method:'DELETE'})));
    card.append(actions);$('bindings').append(card);
  }
  $('events').replaceChildren();
  for(const e of history.events){const node=Workbench.node('div',`${time(e.created)} · ${e.binding_id==null?'本轮':'映射 #'+e.binding_id} · ${events[e.kind]||e.kind}`,'event');if(e.kind==='minimum_channels')for(const d of e.detail.deficient)node.append(Workbench.node('p',`${d.group} / ${d.model}：禁用后剩 ${d.remaining} 条，至少需要 4 条`));$('events').append(node);}
  if(!history.events.length)$('events').append(Workbench.node('p','暂无告警或动作。'));
  $('actions').replaceChildren();
  for(const action of history.actions){const target=action.target;const item=Workbench.node('div',`#${action.id} · ${time(action.started)} · New API #${target.remote_id} · ${action.desired===2?'停用':'启用'} · ${action.status}`,'event');item.append(Workbench.node('p',`公共渠道 #${target.registry_id} · ${target.model} · ${target.protocol}`));if(target.trigger_outcome)item.append(Workbench.node('p',(names[target.trigger_outcome]||target.trigger_outcome)+(target.threshold?` · 连续 ${target.streak}/${target.threshold} 轮`:'')));const evidence=Workbench.node('details');evidence.append(Workbench.node('summary','冻结目标与操作依据'),Workbench.node('pre',JSON.stringify(target,null,2)));item.append(evidence);$('actions').append(item);}
  if(!history.actions.length)$('actions').append(Workbench.node('p','暂无管理动作。'));
  $('rounds').replaceChildren();
  for(const round of history.rounds)$('rounds').append(button(`#${round.id} · ${time(round.due)} · ${round.status}`,async()=>{$('attempts').textContent=JSON.stringify(await api(`/rounds/${round.id}/attempts`),null,2);}));
}
async function operate(action, success='已保存。'){
  if(busy)return;busy=true;generation++;
  try{await action();await refresh();message(success);}catch(error){message(error.message);}finally{busy=false;}
}
$('instance-form').addEventListener('submit',event=>{event.preventDefault();const token=$('token').value;$('token').value='';operate(()=>api('/instance',{method:'PUT',body:JSON.stringify({base_url:$('base-url').value.trim(),token,user_id:$('user-id').value?Number($('user-id').value):null})}),'管理连接已保存，未发送测试请求。');});
$('binding-form').addEventListener('submit',event=>{event.preventDefault();const body={remote_id:Number($('remote-id').value),registry_id:Number($('registry-id').value),model:$('model').value,protocol:$('protocol').value,expected:$('expected').value,confirm_live:$('confirm-read').checked,confirm_same_credential:$('confirm-key').checked};$('confirm-read').checked=false;$('confirm-key').checked=false;operate(()=>api('/bindings',{method:'POST',body:JSON.stringify(body)}),'已核对并保存映射，尚未启用测试。');});
$('enable').addEventListener('click',()=>{const body={enabled:true,confirm_live:$('confirm-live').checked,confirm_manage:$('confirm-manage').checked,confirm_inventory:$('confirm-inventory').checked};for(const id of ['confirm-live','confirm-manage','confirm-inventory'])$(id).checked=false;operate(()=>api('/plan',{method:'PUT',body:JSON.stringify(body)}),'监控已启用，将从下一个半小时时隙开始。');});
$('reset-circuit').addEventListener('click',()=>{const body={revision:state.executor.circuit.revision,confirm_reset:$('confirm-reset').checked};$('confirm-reset').checked=false;operate(()=>api('/circuit/reset',{method:'POST',body:JSON.stringify(body)}),'熔断已解除，计划保持暂停。');});
$('pause').addEventListener('click',()=>operate(()=>api('/plan',{method:'PUT',body:JSON.stringify({enabled:false})}),'监控已暂停；已发出的管理动作请查看账本。'));
$('run').addEventListener('click',()=>operate(()=>api('/run',{method:'POST'}),'已提交本轮测试，后台继续运行。'));
$('refresh').addEventListener('click',()=>operate(()=>Promise.resolve(),'已刷新。'));
$('model').addEventListener('change',()=>{const m=state.models.find(m=>m.model===$('model').value);if(m)$('protocol').value=m.protocol;});
async function poll(initial=false){try{if(!busy)await refresh(initial);}catch(error){message(error.message);}finally{timer=setTimeout(()=>poll(),5000);}}
window.addEventListener('pagehide',()=>{clearTimeout(timer);$('token').value='';});poll(true);
