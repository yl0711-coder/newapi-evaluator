let state = {
  schedules: [], legacySchedules: [], options: { primary_models: [], groups: [] },
  feishu: [], emails: [], families: [], editing: null,
};

function checkedValues(selector) {
  return $$(selector).filter((input) => input.checked).map((input) => Number(input.value));
}

function choices(items, name, selected = []) {
  if (!items.length) return '<span class="small muted">暂无可选项</span>';
  return items.map((item) => `<label class="choice"><input type="checkbox" name="${name}" value="${item.id}" ${selected.includes(item.id) ? 'checked' : ''}> ${esc(item.name || item.address)}</label>`).join('');
}

function selectedConfigurationIds() {
  return $$('[data-configuration-target]').filter((input) => input.checked).map((input) => Number(input.value));
}

function renderPrimaryModels() {
  const models = state.options.primary_models || [];
  $('#primary-models').innerHTML = models.filter((model) => model.enabled).map((model) =>
    `<span class="tag ok">${esc(model.display_name)}<small> · ${esc(model.canonical_model)}</small></span>`,
  ).join('') || '<span class="muted">尚未配置主测模型</span>';
  $('#primary-rule-version').textContent = '10 次 / 模型 / 批次';
  const rules = state.schedules.find((schedule) => schedule.current_version)?.current_version?.measurement_rules
    || state.options.measurement_rules || {};
  const thresholds = rules.monitoring_thresholds;
  $('#primary-thresholds').textContent = thresholds
    ? `统一阈值：成功率下降 ≥ ${thresholds.success_rate_drop * 100} 个百分点；上游错误率上升 ≥ ${thresholds.upstream_error_rate_rise * 100} 个百分点；速度同时相对恶化 ≥ ${thresholds.relative_speed_worsening * 100}% 且 TTFT +${thresholds.ttft_absolute_seconds}s、总耗时 P95 +${thresholds.duration_absolute_seconds}s。`
    : '统一阈值会随计划版本冻结；暂无已创建的精确连接配置定时计划。';
}

function targetChoice(configuration, selected) {
  const missing = configuration.missing_primary_models || [];
  const disabled = missing.length ? 'disabled' : '';
  const help = missing.length
    ? `<small class="warn-text">缺少主测映射：${esc(missing.join('、'))}</small>`
    : `<small>${esc((configuration.supported_primary_models || []).join('、'))} · ${esc(configuration.fingerprint.slice(-12))}</small>`;
  return `<label class="configuration-target"><input data-configuration-target type="checkbox" value="${configuration.id}" ${selected.includes(configuration.id) ? 'checked' : ''} ${disabled}><b>${esc(configuration.channel_name)} · ${esc(configuration.display_name)}</b><small>${esc(configuration.family_name)} · ${Number(configuration.upstream_multiplier)}×</small>${help}</label>`;
}

function renderTargets(selected = []) {
  const groups = state.options.groups || [];
  $('#configuration-target-groups').innerHTML = groups.length ? groups.map((group) => `<section class="configuration-target-group"><h4>${esc(group.label)}</h4><div class="configuration-target-list">${group.configurations.map((configuration) => targetChoice(configuration, selected)).join('')}</div></section>`).join('') : '<div class="empty">目前没有可选的已上线精确连接配置。</div>';
  updateTargetCount();
}

function updateTargetCount() {
  $('#target-count').textContent = `${selectedConfigurationIds().length} 份配置`;
}

function renderForm(schedule = null) {
  state.editing = schedule?.id || null;
  $('#form-title').textContent = schedule ? `编辑计划：${schedule.name}` : '新建定时计划';
  $('#schedule-name').value = schedule?.name || '';
  $('#report-time').value = schedule?.report_time || '09:00';
  $('#schedule-enabled').checked = schedule?.enabled ?? true;
  $('#feishu-options').innerHTML = choices(state.feishu, 'feishu', schedule?.feishu_webhook_ids || []);
  $('#email-options').innerHTML = choices(state.emails, 'emails', schedule?.email_recipient_ids || []);
  renderTargets((schedule?.configuration_targets || []).map((target) => target.id));
  $('#cancel-edit').classList.toggle('hide', !schedule);
}

function scheduleCard(schedule) {
  const version = schedule.current_version;
  const status = schedule.paused_at ? '<span class="tag bad">已暂停</span>'
    : schedule.enabled ? '<span class="tag ok">已启用</span>' : '<span class="tag">已停用</span>';
  const targets = (schedule.configuration_targets || []).map((target) =>
    `<span class="tag">${esc(target.channel_name)} · ${esc(target.display_name)} · ${Number(target.upstream_multiplier)}×</span>`,
  ).join('');
  const baseline = version?.baseline_status === 'ready' ? '基线已固定' : '基线建立中（需 3 份合格封存报告）';
  const reports = (schedule.current_report_tasks || []).map((report) => `<a class="sm" href="task.html?id=${report.task_id}">查看当前报告</a>`).join('');
  const monitoring = { connectivity_anomaly: '连接异常', persistent: '持续异常', advisory: '异常提示', recovered: '已恢复', normal: '正常' }[schedule.monitoring_state] || '正常';
  const affected = schedule.affected_model_count ? ` · ${schedule.affected_model_count} 个模型受影响` : '';
  return `<article class="card configuration-schedule-card" data-schedule-id="${schedule.id}"><div class="between"><div><h3>${esc(schedule.name)} ${status}</h3><p class="help">${esc(schedule.report_time)} 报告发送 · 版本 v${version?.version || '—'} · ${esc(baseline)} · 监测：${esc(monitoring)}${esc(affected)}</p>${schedule.pause_reason ? `<div class="notice warn">暂停原因：${esc(schedule.pause_reason)}；错过 ${schedule.missed_runs || 0} 次，不会补跑。</div>` : ''}</div><div class="actions"><button type="button" class="sm" data-edit-schedule="${schedule.id}">编辑</button><button type="button" class="sm" data-rebuild-baseline="${schedule.id}">重建基线</button></div></div><div class="schedule-targets">${targets || '<span class="muted">尚未选择目标</span>'}</div><div class="schedule-version"><span>主测模型：${esc((version?.primary_models || []).join('、') || '—')}</span><span>测量规则：${esc(version?.measurement_rules?.version || '—')}</span><span>阈值：${esc(version?.threshold_version || '—')}</span></div>${reports ? `<div class="actions">${reports}</div>` : ''}</article>`;
}

function legacyCard(schedule) {
  return `<article class="card"><b>${esc(schedule.name)}</b><span class="tag">旧计划</span><p class="help">${esc(schedule.report_time)} 报告发送；原有目标和历史均保持不变。</p></article>`;
}

function renderDestinations() {
  $('#feishu-list').innerHTML = state.feishu.map((item) => `<div class="between small"><span>${esc(item.name)} · ${esc(item.webhook_masked)}</span><span><button class="sm" data-test-feishu="${item.id}">测试</button> <button class="sm danger" data-delete-feishu="${item.id}">删除</button></span></div>`).join('');
  $('#email-list').innerHTML = state.emails.map((item) => `<div class="between small"><span>${esc(item.name)} · ${esc(item.address)}</span><span><button class="sm" data-test-email="${item.id}">测试</button> <button class="sm danger" data-delete-email="${item.id}">删除</button></span></div>`).join('');
}

function render() {
  renderPrimaryModels();
  renderForm(state.editing ? state.schedules.find((schedule) => schedule.id === state.editing) : null);
  $('#schedule-list').innerHTML = state.schedules.length ? state.schedules.map(scheduleCard).join('') : '<div class="card empty">还没有精确连接配置定时计划。</div>';
  $('#legacy-schedules').classList.toggle('hide', !state.legacySchedules.length);
  $('#legacy-schedule-list').innerHTML = state.legacySchedules.map(legacyCard).join('');
  renderDestinations();
}

async function load() {
  clearError();
  try {
    const [schedules, options, legacySchedules, feishu, emails, smtp, families] = await Promise.all([
      get('/api/configuration-scheduled-tests'), get('/api/scheduled-configuration-options'),
      get('/api/scheduled-tests'), get('/api/notifications/feishu'),
      get('/api/notifications/email-recipients'), get('/api/notifications/smtp'), get('/api/model-families'),
    ]);
    const managedIds = new Set(schedules.map((schedule) => schedule.id));
    state = { ...state, schedules, options, legacySchedules: legacySchedules.filter((schedule) => !managedIds.has(schedule.id)), feishu, emails, families };
    if (smtp) {
      $('#smtp-host').value = smtp.host; $('#smtp-port').value = smtp.port; $('#smtp-tls').value = smtp.tls_mode;
      $('#smtp-user').value = smtp.username; $('#smtp-from').value = smtp.from_address; $('#smtp-from-name').value = smtp.from_name;
    }
    render();
  } catch (error) { showError(error.message); }
}

function openPrimaryModels() {
  const selected = new Set((state.options.primary_models || []).filter((model) => model.enabled).map((model) => model.canonical_model));
  const models = (state.families || []).flatMap((family) => (family.models || []).filter((model) => !model.archived_at && model.enabled).map((model) => ({ ...model, family_name: family.name })));
  $('#primary-model-options').innerHTML = models.map((model) => `<label class="choice"><input type="checkbox" value="${esc(model.model)}" ${selected.has(model.model) ? 'checked' : ''}> ${esc(model.display_name || model.model)} <small>${esc(model.family_name)}</small></label>`).join('') || '<span class="muted">没有可启用的家族模型。</span>';
  $('#primary-model-reason').value = '';
  $('#primary-model-dialog').showModal();
}

async function savePrimaryModels(event) {
  event.preventDefault();
  const models = $$('#primary-model-options input:checked').map((input) => input.value);
  const reason = $('#primary-model-reason').value.trim();
  if (!models.length || !reason) return showError('请选择至少一个主测模型并说明调整原因。');
  try {
    await api('/api/scheduled-primary-models', { method: 'PUT', body: JSON.stringify({ models, reason }) });
    $('#primary-model-dialog').close(); await load();
  } catch (error) { showError(error.message); }
}

function scheduleBody() {
  return {
    name: $('#schedule-name').value.trim(), report_time: $('#report-time').value,
    configuration_ids: selectedConfigurationIds(), feishu_webhook_ids: checkedValues('[name="feishu"]'),
    email_recipient_ids: checkedValues('[name="emails"]'), enabled: $('#schedule-enabled').checked,
  };
}

async function saveSchedule() {
  try {
    const body = scheduleBody();
    if (!body.name || !body.report_time) throw new Error('请填写计划名称和报告发送时间。');
    if (!body.configuration_ids.length) throw new Error('请至少选择一份已上线精确连接配置。');
    if (state.editing) await api(`/api/configuration-scheduled-tests/${state.editing}`, { method: 'PUT', body: JSON.stringify(body) });
    else await post('/api/configuration-scheduled-tests', body);
    state.editing = null; await load();
  } catch (error) { showError(error.message); }
}

async function rebuildBaseline(scheduleId) {
  const schedule = state.schedules.find((item) => item.id === scheduleId);
  const message = `将结束当前版本 v${schedule?.current_version?.version || '—'}，旧报告与旧基线仅保留历史；新版本需要重新积累 3 份合格报告。`;
  const reason = window.prompt(`${message}\n\n请输入重建原因：`);
  if (reason === null) return;
  if (!reason.trim()) return showError('重建基线必须填写原因。');
  if (!window.confirm('确认重建基线？正在运行的批次会按旧版本自然完成，不会计入新基线。')) return;
  try {
    await api(`/api/configuration-scheduled-tests/${scheduleId}/rebuild-baseline`, {
      method: 'POST', body: JSON.stringify({ reason: reason.trim() }),
    });
    await load();
  } catch (error) { showError(error.message); }
}

async function saveSmtp() {
  try {
    await api('/api/notifications/smtp', { method: 'PUT', body: JSON.stringify({
      host: $('#smtp-host').value.trim(), port: Number($('#smtp-port').value), tls_mode: $('#smtp-tls').value,
      username: $('#smtp-user').value.trim(), password: $('#smtp-password').value,
      from_name: $('#smtp-from-name').value.trim(), from_address: $('#smtp-from').value.trim(),
    }) }); $('#smtp-password').value = '';
  } catch (error) { showError(error.message); }
}

function bind() {
  $('#edit-primary-models').addEventListener('click', openPrimaryModels);
  $('#primary-model-form').addEventListener('submit', savePrimaryModels);
  $('#configuration-target-groups').addEventListener('change', updateTargetCount);
  $('#save-schedule').addEventListener('click', saveSchedule);
  $('#cancel-edit').addEventListener('click', () => { state.editing = null; renderForm(); });
  $('#schedule-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-edit-schedule]');
    if (button) {
      state.editing = Number(button.dataset.editSchedule); renderForm(state.schedules.find((item) => item.id === state.editing));
      return scrollTo({ top: 0, behavior: 'smooth' });
    }
    const rebuild = event.target.closest('[data-rebuild-baseline]');
    if (rebuild) rebuildBaseline(Number(rebuild.dataset.rebuildBaseline));
  });
  $('#save-smtp').addEventListener('click', saveSmtp);
  $('#add-feishu').addEventListener('click', async () => { try { await post('/api/notifications/feishu', { name: $('#feishu-name').value.trim(), webhook: $('#feishu-webhook').value.trim(), secret: $('#feishu-secret').value }); await load(); } catch (error) { showError(error.message); } });
  $('#add-email').addEventListener('click', async () => { try { await post('/api/notifications/email-recipients', { name: $('#email-name').value.trim(), address: $('#email-address').value.trim() }); await load(); } catch (error) { showError(error.message); } });
  $('.notification-section').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-test-feishu],[data-delete-feishu],[data-test-email],[data-delete-email]');
    if (!button) return;
    try {
      if (button.dataset.testFeishu) await post(`/api/notifications/feishu/${button.dataset.testFeishu}/test`);
      if (button.dataset.deleteFeishu) await del(`/api/notifications/feishu/${button.dataset.deleteFeishu}`);
      if (button.dataset.testEmail) await post(`/api/notifications/email-recipients/${button.dataset.testEmail}/test`);
      if (button.dataset.deleteEmail) await del(`/api/notifications/email-recipients/${button.dataset.deleteEmail}`);
      await load();
    } catch (error) { showError(error.message); }
  });
}

bind();
load();
