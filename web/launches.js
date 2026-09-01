let launchState = { channels: [], sources: [], settings: null, selected: null, launch: null, syncJob: null };
const launchNames = { tested: '测试完成', confirmed: '已确认建议，等待人工上线', verified: '真实上线已核验', synced: '飞书已同步' };

function renderChannels() {
  $('#channel-list').innerHTML = launchState.channels.map((channel) => `<div class="channel-row ${channel.id === launchState.selected ? 'on' : ''}" data-channel="${channel.id}"><strong>${esc(channel.name)}</strong><small>${esc(channel.business_id)} · ${esc(channel.lifecycle_name)} · ${channel.models.length} 个模型</small></div>`).join('') || '<div class="empty">尚无渠道。</div>';
}

function draftText(draft) {
  if (!draft) return '';
  return [`业务 ID：${draft.business_id}`, `名称：${draft.name}`, `协议：${draft.protocol}`, `地址：${draft.base_url}`, `模型：${draft.models.map((item) => item.canonical_model).join('、')}`, '', draft.credential, draft.automation_boundary].join('\n');
}

function diffRows(job) {
  if (!job) return '';
  const changes = [job.diff.channel, ...(job.diff.models || [])].flatMap((item) => (item.changes || []).map((change) => ({ id: item.business_id, ...change })));
  return changes.map((change) => `<div class="diff-row"><b>${esc(change.id)} · ${esc(change.field)}</b><small>${esc(JSON.stringify(change.before ?? '空'))}</small><span>${esc(JSON.stringify(change.after))}</span></div>`).join('') || '<div class="status-line">远端字段已经一致，无需修改。</div>';
}

function renderDetail() {
  const launch = launchState.launch;
  if (!launch) { $('#launch-detail').innerHTML = '<div class="card empty">该渠道尚无完整准入报告，不能进入上线流程。</div>'; return; }
  const sourceOptions = launchState.sources.filter((item) => item.enabled).map((source) => `<option value="${source.id}">${esc(source.name)}</option>`).join('');
  const conflicts = launchState.syncJob?.diff?.conflicts || [];
  $('#launch-detail').innerHTML = `<div class="steps"><section class="card step"><div class="step-head"><span>1</span><h2>测试证据与技术建议</h2></div><div class="status-line">当前：${esc(launchNames[launch.status] || launch.status)} · 业务 ID ${esc(launch.business_id)}</div><pre class="draft">${esc(draftText(launch.config_draft))}</pre>${launch.status === 'tested' ? '<div class="field"><label>负责人或备注</label><input id="owner-note"></div><button class="primary" id="confirm-launch">确认技术接入建议</button>' : `<p class="help">确认人 #${launch.confirmed_by || '-'} · ${fmtTime(launch.confirmed_at)}</p>`}</section><section class="card step"><div class="step-head"><span>2</span><h2>人工上线后只读核验</h2></div><p>请先由内部人员在中转站人工上线，再从独立只读接口核验业务 ID、在线状态和模型映射。</p><div class="step-actions"><select id="verify-source">${sourceOptions || '<option value="">请先配置只读核验源</option>'}</select><button id="verify-online" ${!['confirmed', 'verified', 'synced'].includes(launch.status) || !sourceOptions ? 'disabled' : ''}>核验实际上线</button></div>${launch.verification.verified_at ? `<div class="status-line">已核验：${fmtTime(launch.verification.verified_at)} · ${launch.verification.models.map(esc).join('、')}</div>` : ''}</section><section class="card step"><div class="step-head"><span>3</span><h2>预览并确认飞书同步</h2></div><p>同步“渠道表 + 模型映射表”；重复同步按业务 ID 更新，不增加重复行。</p><div class="step-actions"><button id="preview-sync" ${!['verified', 'synced'].includes(launch.status) || !launchState.settings ? 'disabled' : ''}>读取飞书并预览差异</button>${launchState.syncJob?.status === 'preview' ? '<button class="primary" id="confirm-sync">确认同步</button>' : ''}${['confirmed', 'retry'].includes(launchState.syncJob?.status) ? '<button class="primary" id="run-sync">立即执行</button>' : ''}</div>${conflicts.length ? `<div class="notice err">${conflicts.map((item) => esc(item.message)).join('；')}，不会自动覆盖。</div>` : ''}<div class="sync-diff">${diffRows(launchState.syncJob)}</div>${launchState.syncJob ? `<div class="status-line">同步任务 #${launchState.syncJob.id} · ${esc(launchState.syncJob.status)}${launchState.syncJob.last_error ? ` · ${esc(launchState.syncJob.last_error)}` : ''}</div>` : ''}</section></div>`;
  const confirm = $('#confirm-launch'); if (confirm) confirm.onclick = confirmLaunch;
  $('#verify-online').onclick = verifyOnline;
  $('#preview-sync').onclick = previewSync;
  const confirmSync = $('#confirm-sync'); if (confirmSync) confirmSync.onclick = confirmFeishu;
  const runSync = $('#run-sync'); if (runSync) runSync.onclick = runFeishu;
}

async function selectChannel(id) {
  launchState.selected = id; launchState.syncJob = null; renderChannels();
  try { launchState.launch = await get(`/api/channels/${id}/launch`); } catch { launchState.launch = null; }
  renderDetail();
}

async function load() {
  try {
    const [channels, sources, settings, jobs] = await Promise.all([get('/api/channels'), get('/api/online-verification/sources'), get('/api/external-sync/feishu/settings'), get('/api/external-sync/jobs')]);
    launchState.channels = channels; launchState.sources = sources; launchState.settings = settings;
    if (launchState.selected) { launchState.launch = await get(`/api/channels/${launchState.selected}/launch`); launchState.syncJob = jobs.find((job) => job.channel_id === launchState.selected) || launchState.syncJob; }
    renderChannels(); renderConfig(); renderDetail();
    const requested = Number(new URLSearchParams(location.search).get('channel')); if (requested && !launchState.selected) await selectChannel(requested);
  } catch (error) { showError(error.message); }
}

function renderConfig() {
  $('#verify-sources').innerHTML = launchState.sources.map((source) => `<div class="config-row"><strong>${esc(source.name)}</strong><span>${esc(source.endpoint)} · ${source.enabled ? '启用' : '停用'}</span></div>`).join('') || '<div class="empty">尚未配置。</div>';
  if (launchState.settings) { $('#feishu-app-id').value = launchState.settings.app_id; $('#feishu-base').value = launchState.settings.base_token; $('#feishu-channel-table').value = launchState.settings.channel_table_id; $('#feishu-model-table').value = launchState.settings.model_table_id; }
}

async function confirmLaunch() { try { launchState.launch = await post(`/api/channels/${launchState.selected}/launch/confirm`, { owner_note: $('#owner-note').value }); await load(); } catch (error) { showError(error.message); } }
async function verifyOnline() { try { launchState.launch = await post(`/api/channels/${launchState.selected}/launch/verify?source_id=${$('#verify-source').value}`); await load(); } catch (error) { showError(error.message); } }
async function previewSync() { try { launchState.syncJob = await post(`/api/channels/${launchState.selected}/external-sync/feishu/preview`); renderDetail(); } catch (error) { showError(error.message); } }
async function confirmFeishu() { try { launchState.syncJob = await post(`/api/external-sync/jobs/${launchState.syncJob.id}/confirm`); renderDetail(); } catch (error) { showError(error.message); } }
async function runFeishu() { try { launchState.syncJob = await post(`/api/external-sync/jobs/${launchState.syncJob.id}/run`); await load(); } catch (error) { showError(error.message); } }

$('#channel-list').onclick = (event) => { const row = event.target.closest('[data-channel]'); if (row) selectChannel(Number(row.dataset.channel)); };
$('#verify-source-form').onsubmit = async (event) => { event.preventDefault(); try { await post('/api/online-verification/sources', { name: $('#verify-name').value, endpoint: $('#verify-endpoint').value, token: $('#verify-token').value, protocol_version: '1', enabled: true }); event.target.reset(); await load(); } catch (error) { showError(error.message); } };
$('#feishu-form').onsubmit = async (event) => { event.preventDefault(); try { launchState.settings = await api('/api/external-sync/feishu/settings', { method: 'PUT', body: JSON.stringify({ app_id: $('#feishu-app-id').value, app_secret: $('#feishu-secret').value, base_token: $('#feishu-base').value, channel_table_id: $('#feishu-channel-table').value, model_table_id: $('#feishu-model-table').value, enabled: true }) }); $('#feishu-secret').value = ''; renderDetail(); } catch (error) { showError(error.message); } };
load();
