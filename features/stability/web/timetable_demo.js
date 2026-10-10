// Fixed presentation examples; no requests, storage or scoring.
window.timetableDemoReport = function () {
  const date = '2026-10-08', timezone = 'Asia/Shanghai';
  const channels = [{id:901,name:'演示渠道 A'}, {id:902,name:'演示渠道 B'}, {id:903,name:'演示渠道 C'}];
  const times = ['09:00','09:10','09:20','09:30','09:40','09:50'], rows = [];
  for (const channel of channels) for (const [index, clock] of times.entries()) {
    const waiting = index === 5, uncertain = channel.id === 902 && [1,2,4].includes(index);
    const other = channel.id === 903 && index === 2;
    const label = waiting ? '无法判定：采样尚未完成' : uncertain ? '无法判定：MT 与 Nerfed 结果不一致' : other ? '更接近 gpt-6-sol' : '更接近 Astra';
    const state = waiting ? 'waiting' : uncertain ? 'review' : 'observed';
    const scheduled_at = `${date}T${clock}:00+08:00`, due = Date.parse(scheduled_at) / 1000;
    rows.push({run_id:'演示日计划',date,timezone,scheduled_at,scheduled_at_utc:due,
      registry_channel_id:channel.id,channel_name:channel.name,
      sampling_period:waiting?'未采样':`${scheduled_at} – ${date}T${clock}:30+08:00`,
      conclusion:{label,state,model:waiting||uncertain?null:other?'gpt-6-sol':'gpt-6-astra',identity_authenticated:false,calibration_status:'unvalidated'},
      state,status:label,anomaly:uncertain,calibration_status:'unvalidated'});
  }
  return {synthetic:true,rows,total:rows.length,displayed:rows.length,counts:{},empty_plans:[],truncated:false};
};
