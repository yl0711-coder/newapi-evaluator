const CONFIGURATION_STATUS = {
  pending_test: ['待测试', 'warn'],
  offline: ['未上线', ''],
  online: ['已上线', 'ok'],
  disabled: ['已停用', 'bad'],
  testing: ['测试中', 'run'],
};

const CONFIGURATION_NEXT_ACTION = {
  create_fidelity: '开始保真测试',
  review_fidelity: '查看保真证据',
  create_admission_batch: '发起准入评测',
  mark_online: '标记已上线',
  view_batch: '查看准入批次',
  none: '查看详情',
};

let configurationState = {
  configurations: [],
  families: [],
  filter: 'all',
  query: '',
  detail: null,
};

function configurationStatus(value) {
  const [label, className] = CONFIGURATION_STATUS[value] || [value || '未知', ''];
  return `<span class="tag ${className}">${esc(label)}</span>`;
}

function configurationModels(configuration) {
  return (configuration.model_mappings || []).map((mapping) =>
    `${mapping.canonical_model}${mapping.request_model === mapping.canonical_model ? '' : ` → ${mapping.request_model}`}`,
  );
}

function configurationMatches(configuration) {
  const query = configurationState.query.trim().toLowerCase();
  if (configurationState.filter === 'pending'
      && !['pending_test', 'testing'].includes(configuration.business_status)) return false;
  if (configurationState.filter === 'online' && configuration.business_status !== 'online') return false;
  if (configurationState.filter === 'attention'
      && !configuration.attention_count && !configuration.active_check) return false;
  if (!query) return true;
  const source = [
    configuration.channel_name,
    configuration.display_name,
    configuration.family_name,
    configuration.base_url_masked,
    configuration.upstream_multiplier,
    ...configurationModels(configuration),
  ].join(' ').toLowerCase();
  return source.includes(query);
}

function configurationAction(configuration) {
  const action = configuration.next_action || 'none';
  const disabled = configuration.permissions?.[action] === false ? 'disabled' : '';
  const label = CONFIGURATION_NEXT_ACTION[action] || CONFIGURATION_NEXT_ACTION.none;
  return `<button type="button" class="sm ${action === 'mark_online' ? 'primary' : ''}" data-configuration-action="${esc(action)}" data-configuration-id="${configuration.id}" ${disabled}>${esc(label)}</button>`;
}

function configurationGroupCard(channel) {
  const familyRows = channel.families.map((family) => `<section class="configuration-family">
    <header><div><b>${esc(family.family_name)}</b><span>${Number(family.upstream_multiplier).toLocaleString('zh-CN')}×</span></div><small>${family.configurations.length} 份精确连接配置</small></header>
    <div class="configuration-rate-list">${family.configurations.map(configurationRow).join('')}</div>
  </section>`).join('');
  return `<article class="card configuration-channel-card">
    <header class="configuration-channel-header"><div><h2>${esc(channel.channel_name)}</h2><p>${channel.families.length} 个模型家族 · ${channel.configuration_count} 份精确连接配置</p></div><span class="tag ${channel.attention_count ? 'warn' : 'ok'}">${channel.attention_count ? `${channel.attention_count} 项需处理` : '状态正常'}</span></header>
    ${familyRows}
  </article>`;
}

function configurationRow(configuration) {
  const models = configurationModels(configuration);
  const result = configuration.current_results?.admission;
  const activity = configuration.active_check
    ? `<span class="small run-text">上线检查：${esc(configuration.active_check.progress_label || '执行中')}</span>`
    : configuration.latest_connectivity_check
      ? `<span class="small ${configuration.latest_connectivity_check.status === 'passed' ? 'ok-text' : 'warn-text'}">最近连接检查：${esc(configuration.latest_connectivity_check.summary)}</span>`
      : '<span class="small muted">尚未执行连接检查</span>';
  return `<div class="configuration-rate-row" data-configuration-row="${configuration.id}">
    <div class="configuration-row-main"><div><b>${esc(configuration.display_name)}</b>${configurationStatus(configuration.business_status)}<small>${esc(models.join('、') || '尚未配置模型映射')}</small></div><div class="configuration-row-meta"><span>${esc(configuration.protocol)}</span><span>${esc(configuration.fingerprint_short || '待保存')}</span></div></div>
    <div class="configuration-result"><span>${result ? `当前准入：${esc(result.status_label)}` : '暂无当前准入结果'}</span>${activity}</div>
    <div class="configuration-row-actions"><button type="button" class="sm" data-configuration-detail="${configuration.id}">详情</button>${configurationAction(configuration)}</div>
  </div>`;
}

function renderOverview() {
  const stats = configurationState.summary || {};
  $('#stat-channels').textContent = stats.channels || 0;
  $('#stat-configurations').textContent = stats.configurations || 0;
  $('#stat-pending').textContent = stats.pending_test || 0;
  $('#stat-online').textContent = stats.online || 0;
  $('#stat-attention').textContent = stats.attention || 0;
}

function groupConfigurations(configurations) {
  const channels = new Map();
  for (const configuration of configurations) {
    const channelKey = configuration.channel_name || '未命名渠道';
    if (!channels.has(channelKey)) {
      channels.set(channelKey, {
        channel_name: channelKey, families: new Map(), configuration_count: 0, attention_count: 0,
      });
    }
    const channel = channels.get(channelKey);
    const familyKey = `${configuration.family_name}|${configuration.upstream_multiplier}`;
    if (!channel.families.has(familyKey)) {
      channel.families.set(familyKey, {
        family_name: configuration.family_name || '未分配家族',
        upstream_multiplier: configuration.upstream_multiplier || 0,
        configurations: [],
      });
    }
    channel.families.get(familyKey).configurations.push(configuration);
    channel.configuration_count += 1;
    channel.attention_count += Number(configuration.attention_count || 0);
  }
  return [...channels.values()].map((channel) => ({
    ...channel, families: [...channel.families.values()].sort((left, right) =>
      left.family_name.localeCompare(right.family_name, 'zh-CN')
      || Number(left.upstream_multiplier) - Number(right.upstream_multiplier)),
  })).sort((left, right) => left.channel_name.localeCompare(right.channel_name, 'zh-CN'));
}

function renderConfigurationList() {
  const configurations = configurationState.configurations.filter(configurationMatches);
  const channels = groupConfigurations(configurations);
  $('#configuration-list').innerHTML = channels.length
    ? channels.map(configurationGroupCard).join('')
    : '<div class="card empty">没有符合筛选条件的精确连接配置。可以添加一份新的配置开始。</div>';
}

function renderPage() {
  renderOverview();
  renderConfigurationList();
}

async function loadConfigurations() {
  clearError();
  try {
    const [data, families] = await Promise.all([
      get('/api/channel-configurations'),
      get('/api/model-families'),
    ]);
    configurationState = {
      ...configurationState,
      configurations: data.configurations || [],
      summary: data.summary || {},
      families,
    };
    renderPage();
  } catch (error) {
    showError(error.message);
  }
}

function familyById(value) {
  return configurationState.families.find((family) => family.id === Number(value)) || null;
}

function mappingRow(mapping = {}) {
  const options = configurationState.families.flatMap((family) => (family.models || [])
    .filter((model) => model.enabled)
    .map((model) => `<option value="${esc(model.model)}" ${mapping.canonical_model === model.model ? 'selected' : ''}>${esc(family.name)} · ${esc(model.display_name)} · ${esc(model.model)}</option>`),
  ).join('');
  return `<div class="model-mapping-row"><div class="field"><label>规范模型</label><select data-mapping-canonical required><option value="">请选择</option>${options}</select></div><div class="field"><label>实际请求模型</label><input data-mapping-request maxlength="160" value="${esc(mapping.request_model || '')}" placeholder="上游模型 ID" required></div><button type="button" class="icon-button" data-remove-mapping aria-label="删除模型映射">×</button></div>`;
}

function renderMappingRows(mappings = []) {
  const resolved = mappings.length ? mappings : [{}];
  $('#model-mappings').innerHTML = resolved.map(mappingRow).join('');
}

function resetConfigurationForm() {
  $('#configuration-form').reset();
  $('#configuration-id').value = '';
  $('#configuration-dialog-title').textContent = '添加精确连接配置';
  const families = configurationState.families.filter((family) => !family.archived_at);
  $('#configuration-family').innerHTML = families.map((family) =>
    `<option value="${family.id}">${esc(family.name)}</option>`,
  ).join('') || '<option value="">请先维护模型家族</option>';
  renderMappingRows();
}

function openConfigurationForm(configuration = null) {
  resetConfigurationForm();
  if (configuration) {
    $('#configuration-dialog-title').textContent = '复制为新的精确连接配置';
    $('#configuration-id').value = configuration.id;
    $('#configuration-family').value = String(configuration.family_id);
    $('#configuration-multiplier').value = configuration.upstream_multiplier;
    $('#configuration-channel').value = configuration.channel_name;
    $('#configuration-name').value = `${configuration.display_name}（新配置）`;
    $('#configuration-protocol').value = configuration.protocol;
    $('#configuration-base-url').value = configuration.base_url || '';
    $('#configuration-route').value = configuration.route || '';
    $('#configuration-group').value = configuration.group_name || '';
    $('#configuration-note').value = configuration.note || '';
    renderMappingRows(configuration.model_mappings || []);
  }
  $('#configuration-dialog').showModal();
}

function formMappings() {
  return $$('.model-mapping-row', $('#model-mappings')).map((row) => ({
    canonical_model: $('[data-mapping-canonical]', row).value,
    request_model: $('[data-mapping-request]', row).value.trim(),
  })).filter((mapping) => mapping.canonical_model || mapping.request_model);
}

async function saveConfiguration(event) {
  event.preventDefault();
  const sourceConfigurationId = Number($('#configuration-id').value) || null;
  const modelMappings = formMappings();
  if (!modelMappings.length || modelMappings.some((mapping) => !mapping.canonical_model || !mapping.request_model)) {
    return showError('请完整填写至少一条模型映射。');
  }
  const apiKey = $('#configuration-api-key').value;
  if (!sourceConfigurationId && !apiKey) return showError('新建精确连接配置必须填写凭据。');
  const body = {
    source_configuration_id: sourceConfigurationId,
    family_id: Number($('#configuration-family').value),
    channel_name: $('#configuration-channel').value.trim(),
    display_name: $('#configuration-name').value.trim(),
    protocol: $('#configuration-protocol').value,
    base_url: $('#configuration-base-url').value.trim(),
    api_key: apiKey || null,
    upstream_multiplier: Number($('#configuration-multiplier').value),
    route: $('#configuration-route').value.trim(),
    group_name: $('#configuration-group').value.trim(),
    model_mappings: modelMappings,
    note: $('#configuration-note').value.trim(),
  };
  const button = $('#save-configuration');
  button.disabled = true;
  try {
    await post('/api/channel-configurations', body);
    $('#configuration-dialog').close();
    await loadConfigurations();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

function detailTab(tab) {
  $$('[data-detail-tab]').forEach((button) => button.classList.toggle('on', button.dataset.detailTab === tab));
  $$('.detail-panel').forEach((panel) => panel.classList.toggle('hide', panel.id !== `detail-${tab}`));
}

function reportHistoryRow(entry) {
  const current = entry.current ? '<span class="tag ok">当前结果</span>' : '';
  return `<li><div><b>${esc(entry.title)}</b>${current}<small>${esc(entry.status_label || entry.status)} · ${fmtTime(entry.completed_at || entry.created_at)}</small></div>${entry.href ? `<a class="sm" href="${esc(entry.href)}">查看报告</a>` : ''}</li>`;
}

function renderDetail(configuration) {
  configurationState.detail = configuration;
  $('#detail-title').textContent = `${configuration.channel_name} · ${configuration.display_name}`;
  const currentAdmission = configuration.current_results?.admission;
  const currentComparison = configuration.current_results?.comparison;
  const check = configuration.latest_connectivity_check;
  const onlineAction = configuration.business_status !== 'online' && configuration.business_status !== 'disabled'
    && configuration.permissions?.mark_online
    ? '<button type="button" class="primary" data-configuration-action="mark_online">标记已上线</button>' : '';
  const presentationAction = configuration.permissions?.edit_presentation
    ? '<button type="button" data-detail-action="presentation">编辑显示信息</button>' : '';
  const statusActions = configuration.permissions?.mark_online
    ? `${configuration.business_status !== 'pending_test' ? '<button type="button" data-detail-action="status" data-status="pending_test">标记待测试</button>' : ''}${configuration.business_status !== 'offline' ? '<button type="button" data-detail-action="status" data-status="offline">标记未上线</button>' : ''}${configuration.business_status !== 'disabled' ? '<button type="button" class="danger" data-detail-action="status" data-status="disabled">标记已停用</button>' : ''}`
    : '';
  $('#detail-overview').innerHTML = `<section class="detail-summary"><div><span>业务状态</span>${configurationStatus(configuration.business_status)}</div><div><span>采购倍率</span><b>${Number(configuration.upstream_multiplier)}×</b></div><div><span>保真</span><b>${esc(configuration.fidelity_status_label || '需要保真')}</b></div><div><span>当前准入</span><b>${esc(currentAdmission?.status_label || '暂无当前结果')}</b></div></section>
    ${configuration.active_check ? `<div class="notice info">上线检查正在后台执行：${esc(configuration.active_check.progress_label || '正在逐模型检查')}。关闭页面不会取消检查。</div>` : ''}
    ${check ? `<div class="notice ${check.status === 'passed' ? 'info' : 'warn'}">最近基础连接检查：${esc(check.summary)}${check.error ? `；${esc(check.error)}` : ''}</div>` : ''}
    <section class="detail-actions"><button type="button" data-detail-action="copy">复制为新配置</button><button type="button" data-detail-action="connectivity">测试连接</button>${presentationAction}${configurationAction(configuration)}${onlineAction}${statusActions}${configuration.active_check ? '<button type="button" class="danger" data-detail-action="cancel-online-check">取消检查</button>' : ''}</section>
    <section class="detail-current-results"><h3>当前结果</h3><div><b>准入评测</b><span>${esc(currentAdmission?.summary || '暂无当前结果')}</span></div><div><b>普通对比</b><span>${esc(currentComparison?.summary || '暂无当前结果')}</span></div></section>`;
  $('#detail-connection').innerHTML = `<dl class="configuration-definition"><dt>地址</dt><dd>${esc(configuration.base_url_masked || '—')}</dd><dt>协议</dt><dd>${esc(configuration.protocol)}</dd><dt>模型映射</dt><dd>${configurationModels(configuration).map(esc).join('<br>') || '—'}</dd><dt>采购倍率</dt><dd>${Number(configuration.upstream_multiplier)}×</dd><dt>路由</dt><dd>${esc(configuration.route || '—')}</dd><dt>分组</dt><dd>${esc(configuration.group_name || '—')}</dd><dt>凭据指纹</dt><dd class="mono">${esc(configuration.credential_fingerprint || '—')}</dd><dt>配置指纹</dt><dd class="mono">${esc(configuration.fingerprint || '—')}</dd></dl>`;
  $('#detail-history').innerHTML = `<p class="help">取消、停止、未完成或证据完整性失败的批次只在这里保留历史，不会覆盖当前结果。</p><ul class="configuration-history">${(configuration.history || []).map(reportHistoryRow).join('') || '<li class="empty">还没有测试历史。</li>'}</ul>`;
  detailTab('overview');
  $('#configuration-detail-dialog').showModal();
}

async function openDetail(configurationId) {
  try {
    const configuration = await get(`/api/channel-configurations/${configurationId}`);
    renderDetail(configuration);
  } catch (error) {
    showError(error.message);
  }
}

async function testConnectivity(configurationId) {
  try {
    const result = await post(`/api/channel-configurations/${configurationId}/connectivity-checks`, {});
    const message = result.summary || '连接检查已完成。';
    if (configurationState.detail?.id === configurationId) await openDetail(configurationId);
    await loadConfigurations();
    showError(message, '#notice');
    $('#notice').className = `notice ${result.status === 'passed' ? 'info' : 'warn'}`;
  } catch (error) {
    showError(error.message);
  }
}

function openStatusDialog(configurationId, toStatus) {
  $('#status-configuration-id').value = configurationId;
  $('#status-form').dataset.toStatus = toStatus;
  const online = toStatus === 'online';
  $('#status-dialog-title').textContent = online ? '标记为已上线' : '调整业务状态';
  $('#status-dialog-help').textContent = online
    ? '系统会顺序检查全部模型映射。全部通过后才会改为“已上线”；失败或取消时保持原状态。'
    : toStatus === 'disabled'
      ? '停用会保留全部历史，但必须填写具体原因。'
      : '业务状态调整会记录操作理由、操作人、时间以及调整前后的状态。';
  $('#status-submit').textContent = online ? '开始上线检查' : '确认调整';
  $('#status-dialog').showModal();
}

async function submitStatus(event) {
  event.preventDefault();
  const configurationId = Number($('#status-configuration-id').value);
  const toStatus = $('#status-form').dataset.toStatus;
  const body = {
    to_status: toStatus,
    reason_code: $('#status-reason').value,
    note: $('#status-note').value.trim(),
  };
  const button = $('#status-submit');
  button.disabled = true;
  try {
    await post(`/api/channel-configurations/${configurationId}/business-status`, body);
    $('#status-dialog').close();
    if (configurationState.detail?.id === configurationId) await openDetail(configurationId);
    await loadConfigurations();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

function openPresentationDialog(configuration) {
  $('#presentation-configuration-id').value = configuration.id;
  $('#presentation-name').value = configuration.display_name || '';
  $('#presentation-note').value = configuration.note || '';
  $('#presentation-dialog').showModal();
}

async function savePresentation(event) {
  event.preventDefault();
  const configurationId = Number($('#presentation-configuration-id').value);
  try {
    const updated = await api(`/api/channel-configurations/${configurationId}`, {
      method: 'PATCH', body: JSON.stringify({
        display_name: $('#presentation-name').value.trim(), note: $('#presentation-note').value.trim(),
      }),
    });
    $('#presentation-dialog').close();
    renderDetail(updated);
    await loadConfigurations();
  } catch (error) { showError(error.message); }
}

async function startConfigurationAction(configurationId, action) {
  if (action === 'none') return openDetail(configurationId);
  if (action === 'mark_online') return openStatusDialog(configurationId, 'online');
  if (action === 'create_admission_batch') {
    location.href = `compare.html?configuration=${configurationId}`;
    return;
  }
  if (action === 'view_batch' || action === 'review_fidelity') {
    location.href = `compare.html?configuration=${configurationId}`;
    return;
  }
  if (action === 'create_fidelity') {
    try {
      const result = await post(`/api/channel-configurations/${configurationId}/fidelity-tasks`, {});
      location.href = `compare.html?configuration=${configurationId}&task=${result.task_id}`;
    } catch (error) {
      showError(error.message);
    }
  }
}

async function cancelOnlineCheck(configurationId) {
  try {
    await post(`/api/channel-configurations/${configurationId}/online-checks/cancel`, {});
    await openDetail(configurationId);
    await loadConfigurations();
  } catch (error) {
    showError(error.message);
  }
}

function bindPage() {
  $('#add-configuration').addEventListener('click', () => openConfigurationForm());
  $('#configuration-family').addEventListener('change', () => renderMappingRows(formMappings()));
  $('#add-model-mapping').addEventListener('click', () => {
    $('#model-mappings').insertAdjacentHTML('beforeend', mappingRow());
  });
  $('#model-mappings').addEventListener('click', (event) => {
    const remove = event.target.closest('[data-remove-mapping]');
    if (remove && $$('.model-mapping-row').length > 1) remove.closest('.model-mapping-row').remove();
  });
  $('#configuration-form').addEventListener('submit', saveConfiguration);
  $('#configuration-search').addEventListener('input', (event) => {
    configurationState.query = event.target.value;
    renderConfigurationList();
  });
  $('#configuration-filter').addEventListener('click', (event) => {
    const button = event.target.closest('[data-filter]');
    if (!button) return;
    configurationState.filter = button.dataset.filter;
    $$('[data-filter]').forEach((entry) => entry.classList.toggle('on', entry === button));
    renderConfigurationList();
  });
  $('#configuration-list').addEventListener('click', (event) => {
    const detail = event.target.closest('[data-configuration-detail]');
    if (detail) return openDetail(Number(detail.dataset.configurationDetail));
    const action = event.target.closest('[data-configuration-action]');
    if (action) return startConfigurationAction(Number(action.dataset.configurationId), action.dataset.configurationAction);
  });
  $('#configuration-detail-dialog').addEventListener('click', (event) => {
    const tab = event.target.closest('[data-detail-tab]');
    if (tab) return detailTab(tab.dataset.detailTab);
    const configurationAction = event.target.closest('[data-configuration-action]');
    if (configurationAction && configurationState.detail) {
      return startConfigurationAction(
        configurationState.detail.id, configurationAction.dataset.configurationAction,
      );
    }
    const action = event.target.closest('[data-detail-action]');
    if (!action || !configurationState.detail) return;
    const id = configurationState.detail.id;
    if (action.dataset.detailAction === 'copy') openConfigurationForm(configurationState.detail);
    if (action.dataset.detailAction === 'connectivity') testConnectivity(id);
    if (action.dataset.detailAction === 'cancel-online-check') cancelOnlineCheck(id);
    if (action.dataset.detailAction === 'presentation') openPresentationDialog(configurationState.detail);
    if (action.dataset.detailAction === 'status') openStatusDialog(id, action.dataset.status);
  });
  $('#status-form').addEventListener('submit', submitStatus);
  $('#presentation-form').addEventListener('submit', savePresentation);
}

bindPage();
loadConfigurations();
