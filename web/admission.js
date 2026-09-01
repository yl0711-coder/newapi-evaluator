let channels = [];
let platformGroups = [];
let families = [];
let selectedChannel = null;
let importedConfigText = '';
let importedHasKey = false;
let pageReady = false;
const DRAFT_KEY = 'admission-draft-v2';

function selectedGroup() {
  return platformGroups.find((group) => group.id === Number($('#f-platform-group').value));
}

function renderChannels() {
  const requested = Number(new URLSearchParams(location.search).get('channel') || 0);
  $('#f-channel').innerHTML = '<option value="">＋ 新建渠道</option>' + channels.map((channel) => `<option value="${channel.id}" ${channel.id === requested ? 'selected' : ''}>${esc(channel.name)} · ${esc(channel.base_url)}${channel.family_id ? ' · 已绑定模型家族' : ''}</option>`).join('');
  if (requested) selectChannel();
}

function renderGroups(preferred) {
  const requested = preferred || Number(new URLSearchParams(location.search).get('group') || 0);
  $('#f-platform-group').innerHTML = platformGroups.map((group) => `<option value="${group.id}" ${group.id === requested ? 'selected' : ''}>${esc(group.label)}</option>`).join('') || '<option value="">请先新建平台倍率组</option>';
  $('#f-new-family').innerHTML = families.map((family) => `<option value="${family.id}">${esc(family.name)}</option>`).join('');
  renderFamilyPreview();
}

function renderFamilyPreview() {
  const group = selectedGroup();
  if (!group) { $('#family-preview').textContent = '请选择平台倍率组。'; return; }
  const enabled = group.models.filter((model) => model.enabled);
  $('#family-preview').innerHTML = `<b>${esc(group.label)}</b> 是接入意向，将自动测试 ${enabled.length} 个模型：${enabled.map((model) => esc(model.display_name)).join('、')}。系统先比较意向组；未达到时再用同一次同版本结果寻找其他技术组。`;
  if (pageReady) loadEstimate();
}

function saveDraft() {
  const draft = {
    channel_id: Number($('#f-channel').value) || null,
    name: $('#f-name').value, base_url: $('#f-base_url').value,
    protocol: $('#f-protocol').value,
    platform_group_id: Number($('#f-platform-group').value) || null,
    upstream_multiplier: $('#f-upstream-rate').value,
    price_in: $('#f-price_in').value, price_out: $('#f-price_out').value,
    confirm_models: $('#confirm-models').checked,
  };
  localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  $('#draft-state').textContent = '草稿已保存';
}

function restoreDraft() {
  let draft = null;
  try { draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null'); } catch {}
  if (!draft) return;
  if (draft.channel_id && channels.some((channel) => channel.id === draft.channel_id)) {
    $('#f-channel').value = String(draft.channel_id); selectChannel();
  }
  if (!draft.channel_id) {
    $('#f-name').value = draft.name || '';
    $('#f-base_url').value = draft.base_url || '';
    $('#f-protocol').value = draft.protocol || 'openai';
  }
  if (draft.platform_group_id && platformGroups.some((group) => group.id === draft.platform_group_id)) {
    $('#f-platform-group').value = String(draft.platform_group_id);
  }
  $('#f-upstream-rate').value = draft.upstream_multiplier || '';
  $('#f-price_in').value = draft.price_in || '';
  $('#f-price_out').value = draft.price_out || '';
  $('#confirm-models').checked = Boolean(draft.confirm_models);
}

function channelBody() {
  return { name: $('#f-name').value.trim(), base_url: $('#f-base_url').value.trim(), api_key: $('#f-api_key').value, protocol: $('#f-protocol').value, group_name: '', source: 'manual', edited_fields: [] };
}

async function importConfig() {
  const text = $('#f-import-text').value.trim();
  if (!text) throw new Error('请先粘贴连接信息');
  const parsed = await post('/api/import', { text });
  importedConfigText = text;
  importedHasKey = parsed.has_key;
  if (parsed.name) $('#f-name').value = parsed.name;
  if (parsed.base_url) $('#f-base_url').value = parsed.base_url;
  $('#f-protocol').value = parsed.protocol || 'openai';
  $('#f-api_key').value = '';
  $('#f-api_key').placeholder = parsed.has_key ? `已安全识别 ${parsed.key_masked}` : '';
  $('#key-tip').textContent = parsed.has_key
    ? `已识别 Key ${parsed.key_masked}；保存时安全使用，不会回显明文。`
    : '未识别到 API Key，请在此手动填写。';
  const found = [parsed.name && '名称', parsed.base_url && '地址', parsed.has_key && 'Key'].filter(Boolean);
  $('#import-result').textContent = found.length ? `已提取：${found.join('、')}` : '没有识别出连接字段，请检查粘贴内容。';
}

async function saveChannel() {
  const body = channelBody();
  const missing = [['name', '渠道名称'], ['base_url', '上游地址']].filter(([key]) => !body[key]).map(([, label]) => label);
  if (!body.api_key && !importedHasKey) missing.push('API Key');
  if (missing.length) throw new Error(`还缺：${missing.join('、')}`);
  selectedChannel = importedConfigText
    ? await post('/api/channels/import', {
        text: importedConfigText, name: body.name, base_url: body.base_url,
        api_key: body.api_key || null, protocol: body.protocol,
      })
    : await post('/api/channels', body);
  await loadChannels(selectedChannel.id);
  $('#channel-form').classList.add('hide');
  $('#channel-tip').textContent = `正在使用「${selectedChannel.name}」，Key 不会回显。`;
  return selectedChannel;
}

async function loadChannels(selectedId = null) {
  channels = await get('/api/channels');
  renderChannels();
  if (selectedId) { $('#f-channel').value = String(selectedId); selectedChannel = channels.find((channel) => channel.id === selectedId) || selectedChannel; }
}

function selectChannel() {
  const id = Number($('#f-channel').value);
  selectedChannel = channels.find((channel) => channel.id === id) || null;
  $('#channel-form').classList.toggle('hide', Boolean(selectedChannel));
  $('#channel-tip').textContent = selectedChannel ? `正在复用「${selectedChannel.name}」的已保存连接。` : '填写连接信息并保存；同一个 Key 只绑定一个模型家族。';
}

async function loadEstimate() {
  try {
    const group = selectedGroup();
    if (!group) return;
    const params = new URLSearchParams({ platform_group_id: String(group.id) });
    if ($('#f-price_in').value !== '') params.set('price_in', $('#f-price_in').value);
    if ($('#f-price_out').value !== '') params.set('price_out', $('#f-price_out').value);
    const preview = await get(`/api/admission-plan-preview?${params}`);
    const estimate = preview.estimate;
    $('#est').innerHTML = [['模型数', estimate.model_count], ['总请求数', estimate.requests], ['预计时间', `${estimate.estimated_minutes} 分钟`], ['本批 tokens', estimate.tokens.toLocaleString()], ['预计费用', `¥${estimate.cost.toFixed(4)}`]].map(([key, value]) => `<div class="metric"><div class="k">${key}</div><div class="v">${value}</div></div>`).join('');
    $('#est-note').textContent = `每个模型固定 ${estimate.base_requests_per_model} 个请求：1 次鉴权、6 道流式能力题、5 道流式速度题。`;
    $('#plan-versions').textContent = `测试标准 ${estimate.base_version}；所有模型题使用流式，速度只统计 5 道固定速度题。`;
  } catch (error) { showError(error.message); }
}

async function load() {
  try {
    [channels, platformGroups, families] = await Promise.all([get('/api/channels'), get('/api/platform-groups'), get('/api/model-families')]);
    renderChannels(); renderGroups(); restoreDraft(); pageReady = true;
    renderFamilyPreview(); await loadEstimate();
  } catch (error) { showError(error.message); }
}

$('#f-channel').onchange = selectChannel;
$('#btn-import-config').onclick = async () => { clearError(); try { await importConfig(); } catch (error) { showError(error.message); } };
$('#f-import-text').oninput = () => {
  if ($('#f-import-text').value.trim() === importedConfigText) return;
  importedConfigText = ''; importedHasKey = false;
  $('#f-api_key').placeholder = '';
  $('#key-tip').textContent = '粘贴内容已变化，请重新点击“自动提取并填写”，或手动填写 Key。';
  $('#import-result').textContent = '';
};
$('#btn-save-channel').onclick = async () => { clearError(); try { await saveChannel(); } catch (error) { showError(error.message); } };
$('#f-platform-group').onchange = () => {
  renderFamilyPreview();
};
$('#btn-create-platform-group').onclick = async () => {
  clearError();
  try {
    const group = await post('/api/platform-groups', { family_id: Number($('#f-new-family').value), online_multiplier: Number($('#f-new-platform-rate').value) });
    platformGroups = await get('/api/platform-groups'); renderGroups(group.id); $('#f-new-platform-rate').value = '';
  } catch (error) { showError(error.message); }
};
$('#btn-discover-models').onclick = async () => {
  clearError(); const button = $('#btn-discover-models'); button.disabled = true;
  try {
    const channel = selectedChannel || await saveChannel();
    const group = selectedGroup();
    if (!group) throw new Error('请先选择目标平台组');
    const result = await post(`/api/channels/${channel.id}/discover-models?platform_group_id=${group.id}`);
    $('#model-discovery').innerHTML = [
      ...result.matched.map((item) => `<div class="model-match ok"><span><b>${esc(item.canonical)}</b><small>上游返回 ${esc(item.upstream)} · ${item.match_type === 'alias' ? '别名匹配' : '规范 ID 匹配'}</small></span><span>已匹配</span></div>`),
      ...result.fuzzy_candidates.map((item) => `<div class="model-match missing"><span><b>${esc(item.upstream)}</b><small>仅为模糊候选，不能自动采用</small>${item.candidates.map((candidate) => `<button type="button" class="sm" data-confirm-model-alias="${candidate.model_id}" data-alias-family="${group.family_id}" data-upstream-alias="${encodeURIComponent(item.upstream)}">确认为 ${esc(candidate.display_name)}（${Math.round(candidate.score * 100)}%）</button>`).join(' ')}</span><span>需人工确认</span></div>`),
      ...result.missing.map((model) => `<div class="model-match missing"><span><b>${esc(model)}</b><small>上游清单未发现，准入时继续验证</small></span><span>待验证</span></div>`),
      ...result.unmapped.map((model) => `<div class="model-match"><span><b>${esc(model)}</b><small>未找到可信映射，不会自动加入模型包</small></span><span>未映射</span></div>`),
    ].join('') + `<p class="help">${esc(result.identity_notice)}</p>`;
  } catch (error) { showError(error.message); }
  finally { button.disabled = false; }
};
$('#model-discovery').onclick = async (event) => {
  const button = event.target.closest('[data-confirm-model-alias]');
  if (!button) return;
  if (!confirm(`确认把上游模型名“${decodeURIComponent(button.dataset.upstreamAlias)}”作为所选规范模型的别名？`)) return;
  button.disabled = true;
  try {
    await post(`/api/model-families/${button.dataset.aliasFamily}/aliases`, {
      model_id: Number(button.dataset.confirmModelAlias),
      alias: decodeURIComponent(button.dataset.upstreamAlias),
    });
    $('#btn-discover-models').click();
  } catch (error) { button.disabled = false; showError(error.message); }
};
['#f-channel', '#f-name', '#f-base_url', '#f-protocol', '#f-platform-group', '#f-upstream-rate', '#f-price_in', '#f-price_out', '#confirm-models'].forEach((selector) => {
  $(selector).addEventListener('change', saveDraft);
});
['#f-price_in', '#f-price_out'].forEach((selector) => $(selector).addEventListener('change', loadEstimate));
$('#btn-submit').onclick = async () => {
  clearError(); const button = $('#btn-submit'); button.disabled = true; button.textContent = '提交中…';
  try {
    const channel = selectedChannel || await saveChannel();
    const body = { platform_group_id: Number($('#f-platform-group').value), upstream_multiplier: Number($('#f-upstream-rate').value) };
    if (!body.platform_group_id) throw new Error('请选择目标平台组');
    if (!(body.upstream_multiplier > 0 && body.upstream_multiplier <= 100)) throw new Error('请填写有效的上游采购倍率');
    if (!$('#confirm-models').checked) throw new Error('请在第 3 步确认平台模型包');
    const priceIn = Number($('#f-price_in').value); const priceOut = Number($('#f-price_out').value);
    if ($('#f-price_in').value !== '') body.price_in = priceIn;
    if ($('#f-price_out').value !== '') body.price_out = priceOut;
    const result = await post(`/api/channels/${channel.id}/admission-tasks`, body);
    localStorage.removeItem(DRAFT_KEY);
    location.href = `batch.html?tasks=${result.task_ids.join(',')}`;
  } catch (error) { showError(error.message); button.disabled = false; button.textContent = '确认计划并加入测试队列'; }
};

load();
