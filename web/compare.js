const BATCH_STATUS = {
  pending_test: ['待测试', 'warn'], offline: ['未上线', ''], online: ['已上线', 'ok'], disabled: ['已停用', ''],
  draft: ['等待启动', ''],
  awaiting_fidelity_truth: ['等待保真判断', 'warn'],
  testing: ['双端测试中', 'run'],
  pending_retest: ['待补测', 'warn'],
  awaiting_conclusion: ['等待整体结论', 'ok'],
  concluded: ['已结束', ''],
};

const BATCH_TASK_STATUS = {
  queued: '排队中', fidelity: '保真中', awaiting_fidelity_truth: '等待保真判断',
  warmup: '预热中', identity: '身份确认中', speed_round_1: '速度测试（第一轮）',
  ability_base: '能力测试', speed_round_2: '速度测试（第二轮）',
  ability_retest: '能力复测', finalizing: '封存报告中', completed: '已完成',
  completed_with_insufficient_metrics: '已完成，部分指标数据不足', stopped: '已停止', canceled: '已取消',
};

let batchState = {
  configurations: [], preview: null, batch: null, fidelityTask: null,
  configurationId: null, refreshTimer: null, correctingConclusion: false,
};

function parameterId(name) {
  const value = Number(new URLSearchParams(location.search).get(name));
  return Number.isInteger(value) && value > 0 ? value : null;
}

function batchStatusTag(status, id = '') {
  const [label, className] = BATCH_STATUS[status] || [status || '未知', ''];
  return `<span${id ? ` id="${esc(id)}"` : ''} class="tag ${className}">${esc(label)}</span>`;
}

function updateProgress(step) {
  const order = ['fidelity', 'benchmarks', 'conclusion'];
  const activeIndex = order.indexOf(step);
  $$('.batch-progress li').forEach((entry) => {
    const index = order.indexOf(entry.dataset.progress);
    entry.classList.toggle('active', index === activeIndex);
    entry.classList.toggle('complete', activeIndex > index);
  });
}

function configurationLabel(configuration) {
  const models = (configuration.model_mappings || []).map((mapping) => mapping.canonical_model).join('、');
  return `${configuration.channel_name} · ${configuration.display_name} · ${configuration.family_name} ${Number(configuration.upstream_multiplier)}× · ${models}`;
}

function setConfigurationId(id) {
  batchState.configurationId = id || null;
  const url = new URL(location.href);
  if (id) url.searchParams.set('configuration', String(id));
  else url.searchParams.delete('configuration');
  url.searchParams.delete('batch');
  url.searchParams.delete('task');
  history.replaceState(null, '', url);
}

function setBatchId(id) {
  const url = new URL(location.href);
  if (id) url.searchParams.set('batch', String(id));
  else url.searchParams.delete('batch');
  history.replaceState(null, '', url);
}

function renderConfigurationSelector() {
  const select = $('#batch-configuration');
  const preferred = batchState.configurationId || Number(select.value);
  select.innerHTML = batchState.configurations.length
    ? batchState.configurations.map((configuration) => `<option value="${configuration.id}">${esc(configurationLabel(configuration))}</option>`).join('')
    : '<option value="">没有可用精确连接配置</option>';
  if (batchState.configurations.some((configuration) => configuration.id === preferred)) {
    select.value = String(preferred);
  }
}

function metric(label, value, hint = '') {
  return `<div class="metric"><div class="k">${esc(label)}</div><div class="v">${esc(value)}</div>${hint ? `<div class="help">${esc(hint)}</div>` : ''}</div>`;
}

function renderPreview() {
  const preview = batchState.preview;
  if (!preview) {
    $('#batch-configuration-summary').innerHTML = '';
    return;
  }
  const configuration = preview.configuration;
  const fidelity = preview.fidelity || {};
  $('#batch-configuration-state').outerHTML = batchStatusTag(preview.batch?.status || fidelity.state || configuration.business_status, 'batch-configuration-state');
  $('#batch-configuration-summary').innerHTML = `<div class="metrics">
    ${metric('业务状态', configuration.business_status_label)}
    ${metric('保真条件', fidelity.label || '待确认')}
    ${metric('待准入模型', String((preview.models || []).length))}
    ${metric('采购倍率', `${Number(configuration.upstream_multiplier)}×`)}
  </div>`;
  const guidance = $('#batch-fidelity-guidance');
  if (!preview.eligible) {
    guidance.classList.remove('hide');
    guidance.innerHTML = fidelityGuidance(preview)
      || esc(preview.blocking_reason || '该配置尚不满足启动双端准入评测的保真条件。请先按提示完成保真。');
  } else {
    guidance.classList.add('hide');
    guidance.textContent = '';
  }
}

function fidelityGuidance(preview) {
  const task = batchState.fidelityTask;
  if (preview.fidelity?.code !== 'awaiting_human_truth') return '';
  if (!task) return '自动保真证据已提交，正在读取人工判断所需信息。';
  const checks = (task.fidelity_checks || []).map((check) => {
    const label = check.item_id || check.id || '保真项';
    const matched = check.matched === true ? '符合机械预期' : check.matched === false ? '需要人工复核' : '已完成';
    return `<li><b>${esc(label)}</b>：${esc(matched)}</li>`;
  }).join('');
  const canDecide = task.permissions?.can_decide_fidelity;
  const action = canDecide
    ? '<button type="button" class="sm primary" id="open-fidelity-decision">记录人工判断</button>'
    : '<span class="muted">当前账号没有保真审核权限。</span>';
  return `<div class="fidelity-guidance"><b>自动保真已完成，等待人工判断</b><p>任务 #${task.task?.id || task.paired?.task_id} 的连接已关闭；请选择“真”或“不真”，不要将等待状态记录成第三种结论。</p>${checks ? `<ul>${checks}</ul>` : ''}<div class="actions">${action}</div></div>`;
}

function benchmarkOption(option) {
  const model = option.request_model === option.canonical_model ? option.canonical_model
    : `${option.canonical_model} → ${option.request_model}`;
  return `<option value="${option.configuration_id}">${esc(option.channel_name)} · ${esc(option.display_name)} · ${Number(option.upstream_multiplier)}× · ${esc(model)}</option>`;
}

function benchmarkRow(model) {
  const options = model.benchmark_options || [];
  const unavailable = !options.length;
  return `<article class="batch-benchmark-row ${unavailable ? 'unavailable' : ''}">
    <div><b>${esc(model.canonical_model)}</b><small>候选请求模型：${esc(model.request_model)}</small></div>
    <div class="field"><label>已上线标杆</label><select data-benchmark-model="${esc(model.canonical_model)}" ${unavailable ? 'disabled' : 'required'}><option value="">请选择一份精确连接配置</option>${options.map(benchmarkOption).join('')}</select>${unavailable ? '<div class="help warn-text">没有支持相同具体模型或已确认等价别名的已上线标杆。请先补充合格标杆。</div>' : '<div class="help">标杆只用于本模型的一对一相对参照。</div>'}</div>
  </article>`;
}

function scalePreview(preview) {
  const estimate = preview.estimate || {};
  return [
    metric('模型子任务', String((preview.models || []).length)),
    metric('每个子任务基础配对', String(estimate.base_logical_pairs || 16)),
    metric('每个子任务请求上限', String(estimate.maximum_endpoint_requests || '—')),
    metric('固定冷却', `${estimate.fixed_cooldown_seconds || 5} 秒`),
  ].join('');
}

function renderBenchmarkStep() {
  const preview = batchState.preview;
  const activeBatch = batchState.batch || preview?.batch;
  const eligible = Boolean(preview?.eligible);
  const canBuild = eligible && !activeBatch;
  $('#batch-benchmark-step').setAttribute('aria-disabled', String(!eligible));
  $('#batch-benchmark-state').outerHTML = batchStatusTag(activeBatch?.status || (eligible ? 'draft' : 'awaiting_fidelity_truth'), 'batch-benchmark-state');
  $('#batch-benchmark-empty').classList.toggle('hide', canBuild || Boolean(activeBatch));
  $('#batch-start-form').classList.toggle('hide', !canBuild);
  if (!preview) return;
  if (canBuild) {
    $('#batch-benchmark-list').innerHTML = (preview.models || []).map(benchmarkRow).join('');
    $('#batch-scale-preview').innerHTML = scalePreview(preview);
    validateBatchStart();
  }
  if (!canBuild && !activeBatch) {
    $('#batch-benchmark-empty').textContent = preview.blocking_reason || '完成保真条件后即可为每个具体模型选择标杆。';
  }
}

function taskReportLink(modelTask) {
  const report = modelTask.current_report;
  if (!report) return '<span class="muted">尚无成功正式报告</span>';
  return report.href
    ? `<a href="${esc(report.href)}">查看报告 v${report.version}</a>`
    : `<span>报告 v${esc(report.version)}</span>`;
}

function batchModelTaskRow(modelTask) {
  const status = BATCH_TASK_STATUS[modelTask.state] || modelTask.state;
  const active = modelTask.current_report ? '<span class="tag ok">当前依据</span>' : '';
  return `<li><div class="batch-model-task-title"><b>${esc(modelTask.canonical_model)}</b>${active}<small>标杆：${esc(modelTask.benchmark_label || '尚未选择')}</small></div><div class="batch-model-task-state"><span>${esc(status)}</span>${taskReportLink(modelTask)}</div></li>`;
}

function conclusionHistoryRow(item) {
  return `<li><b>${esc(item.verdict_label)}</b><span>v${item.conclusion_version} · ${esc(item.actor)} · ${fmtTime(item.created_at)}</span><small>${esc(item.reason)}</small></li>`;
}

function conclusionForm(batch) {
  if (!batch.permissions?.can_conclude) return '';
  if (batch.status === 'concluded') {
    if (!batch.evidence_window?.valid) return '<p class="help">当前正式报告已超出证据窗口，不能更正这份批次结论；如需重新判断，请创建新的准入批次。</p>';
    if (!batchState.correctingConclusion) return '<div class="actions"><button type="button" id="open-conclusion-correction">更正结论</button></div>';
  }
  if (batch.status !== 'awaiting_conclusion' && !(batch.status === 'concluded' && batchState.correctingConclusion)) {
    const continueButton = batch.status !== 'concluded' && batch.permissions?.can_continue
      ? '<button type="button" id="open-continue" class="primary">继续补测</button>' : '';
    return continueButton ? `<div class="actions">${continueButton}</div>` : '';
  }
  const correction = batchState.correctingConclusion;
  return `<form id="batch-conclusion-form" class="batch-conclusion-form">
    <h3>${correction ? '更正人工准入结论' : '人工准入结论'}</h3><p class="help">系统不会根据速度、稳定性或能力指标自动决定准入。${correction ? '更正会追加新的结论版本，原结论保持历史可查。' : ''}</p>
    <fieldset class="paired-choice-set"><legend>结论</legend><label><input type="radio" name="batch-verdict" value="admit"> 准入</label><label><input type="radio" name="batch-verdict" value="do_not_admit"> 暂不准入</label>${correction ? '' : '<label><input type="radio" name="batch-verdict" value="continue"> 继续补测</label>'}</fieldset>
    <div class="field"><label for="batch-conclusion-reason">理由</label><textarea id="batch-conclusion-reason" maxlength="4000" required placeholder="${correction ? '说明更正依据。' : '“暂不准入”或“继续补测”必须说明依据；选择“准入”可选预设理由并说明证据。'}"></textarea></div>
    <div class="field"><label for="batch-conclusion-refs">报告或证据引用</label><input id="batch-conclusion-refs" placeholder="选择“准入”时至少引用一个模型报告区域或证据"></div>
    <div class="actions"><button type="submit" class="primary">提交人工结论</button></div>
  </form>`;
}

function renderBatchReport() {
  const batch = batchState.batch;
  const panel = $('#batch-report');
  $('#batch-conclusion-step').setAttribute('aria-disabled', String(!batch));
  $('#batch-report-empty').classList.toggle('hide', Boolean(batch));
  panel.classList.toggle('hide', !batch);
  if (!batch) return;
  $('#batch-conclusion-state').outerHTML = batchStatusTag(batch.status, 'batch-conclusion-state');
  const windowText = batch.evidence_window?.valid
    ? `报告证据窗口有效，至 ${fmtTime(batch.evidence_window.expires_at)}`
    : batch.evidence_window?.reason || '等待全部模型形成当前成功报告';
  panel.innerHTML = `<section class="batch-report-summary"><div>${batchStatusTag(batch.status)}<span>${esc(windowText)}</span></div><div class="metrics">${metric('待准入模型', String(batch.model_tasks?.length || 0))}${metric('已有当前报告', String(batch.current_report_count || 0))}${metric('当前结论', batch.current_conclusion?.verdict_label || '尚未提交')}</div></section>
    <section><h3>模型子任务</h3><ul class="batch-model-tasks">${(batch.model_tasks || []).map(batchModelTaskRow).join('')}</ul></section>
    <section class="batch-history"><h3>结论历史</h3><ul>${(batch.conclusion_history || []).map(conclusionHistoryRow).join('') || '<li class="muted">还没有人工结论。</li>'}</ul></section>
    ${conclusionForm(batch)}`;
  const currentProgress = batch.status === 'awaiting_fidelity_truth' ? 'fidelity'
    : ['draft', 'testing', 'pending_retest'].includes(batch.status) ? 'benchmarks' : 'conclusion';
  updateProgress(currentProgress);
}

function validateBatchStart() {
  const hasAll = $$('[data-benchmark-model]').length > 0
    && $$('[data-benchmark-model]').every((select) => Boolean(select.value));
  $('#batch-start').disabled = !hasAll || !$('#batch-scale-confirmed').checked;
}

async function loadPreview() {
  const id = batchState.configurationId;
  batchState.preview = null;
  batchState.batch = null;
  batchState.correctingConclusion = false;
  batchState.fidelityTask = null;
  if (!id) {
    renderPreview(); renderBenchmarkStep(); renderBatchReport();
    return;
  }
  try {
    const preview = await get(`/api/admission-batches/preview?configuration_id=${id}`);
    batchState.preview = preview;
    const fidelityTaskId = preview.fidelity?.task_id || parameterId('task');
    if (fidelityTaskId) {
      batchState.fidelityTask = await get(`/api/paired-admission/tasks/${fidelityTaskId}`);
    }
    if (preview.batch) {
      batchState.batch = preview.batch;
      setBatchId(preview.batch.id);
    }
    renderPreview(); renderBenchmarkStep(); renderBatchReport();
    scheduleRefresh();
  } catch (error) {
    showError(error.message);
  }
}

async function loadBatch(batchId) {
  try {
    batchState.batch = await get(`/api/admission-batches/${batchId}`);
    batchState.correctingConclusion = false;
    batchState.configurationId = batchState.batch.configuration_id;
    setConfigurationId(batchState.configurationId);
    renderPreview(); renderBenchmarkStep(); renderBatchReport();
    scheduleRefresh();
  } catch (error) {
    showError(error.message);
  }
}

function scheduleRefresh() {
  clearTimeout(batchState.refreshTimer);
  const state = batchState.batch?.status;
  const fidelityState = batchState.fidelityTask?.paired?.state;
  if ((state && !['concluded'].includes(state)) || (fidelityState && !['awaiting_fidelity_truth', 'stopped', 'canceled'].includes(fidelityState))) {
    batchState.refreshTimer = setTimeout(async () => {
      if (batchState.batch?.id) await loadBatch(batchState.batch.id);
      else await loadPreview();
    }, 3500);
  }
}

async function startBatch(event) {
  event.preventDefault();
  if (!batchState.preview) return;
  const benchmarkSelections = $$('[data-benchmark-model]').map((select) => ({
    canonical_model: select.dataset.benchmarkModel,
    benchmark_configuration_id: Number(select.value),
  }));
  if (benchmarkSelections.some((item) => !item.benchmark_configuration_id)) return showError('请为每个模型选择一个已上线标杆。');
  const button = $('#batch-start');
  button.disabled = true;
  try {
    const result = await post('/api/admission-batches', {
      configuration_id: batchState.configurationId,
      benchmark_selections: benchmarkSelections,
      scale_confirmed: true,
    });
    setBatchId(result.id);
    await loadBatch(result.id);
  } catch (error) {
    showError(error.message);
    button.disabled = false;
  }
}

function selectedConclusion() {
  return $('input[name="batch-verdict"]:checked')?.value || '';
}

async function submitConclusion(event) {
  event.preventDefault();
  const verdict = selectedConclusion();
  const reason = $('#batch-conclusion-reason').value.trim();
  const refs = $('#batch-conclusion-refs').value.split(',').map((value) => value.trim()).filter(Boolean);
  if (!verdict) return showError('请选择人工结论。');
  if (!reason) return showError('请填写判断理由。');
  if (verdict === 'admit' && !refs.length) return showError('选择“准入”时必须引用模型报告区域或证据。');
  if (verdict === 'continue') return openContinueDialog();
  try {
    await post(`/api/admission-batches/${batchState.batch.id}/conclusion`, {
      verdict, reason, evidence_refs: refs,
      expected_conclusion_version: batchState.batch.current_conclusion_version || 0,
      report_versions: batchState.batch.expected_report_versions || [],
    });
    batchState.correctingConclusion = false;
    await loadBatch(batchState.batch.id);
  } catch (error) {
    showError(error.message);
  }
}

function openContinueDialog() {
  if (!batchState.batch) return;
  $('#continue-models').innerHTML = (batchState.batch.model_tasks || []).map((modelTask) => `<label><input type="checkbox" value="${esc(modelTask.canonical_model)}"> <b>${esc(modelTask.canonical_model)}</b><small>${esc(modelTask.current_report ? '已有当前报告，选择后将失去本批次准入资格' : '尚无成功正式报告')}</small></label>`).join('');
  $('#continue-reason').value = '';
  $('#continue-dialog').showModal();
}

async function submitContinue(event) {
  event.preventDefault();
  const models = $$('#continue-models input:checked').map((input) => input.value);
  const reason = $('#continue-reason').value.trim();
  if (!models.length) return showError('请选择需要补测的具体模型。');
  if (!reason) return showError('请填写补测原因。');
  try {
    await post(`/api/admission-batches/${batchState.batch.id}/continue`, { models, reason });
    $('#continue-dialog').close();
    await loadBatch(batchState.batch.id);
  } catch (error) {
    showError(error.message);
  }
}

function openFidelityDialog() {
  const task = batchState.fidelityTask;
  if (!task) return;
  $('#fidelity-form').reset();
  $('#fidelity-dialog').showModal();
}

async function submitFidelityDecision(event) {
  event.preventDefault();
  const task = batchState.fidelityTask;
  const value = $('input[name="fidelity-value"]:checked')?.value;
  const reason = $('#fidelity-reason').value.trim();
  const evidenceRefs = $('#fidelity-refs').value.split(',').map((entry) => entry.trim()).filter(Boolean);
  if (!task || !value || !reason) return showError('请选择“真”或“不真”，并填写判断依据。');
  try {
    await post(`/api/paired-admission/tasks/${task.task.id}/fidelity-decision`, {
      value, reason, evidence_refs: evidenceRefs,
      expected_state_version: task.paired.state_version,
      idempotency_key: crypto.randomUUID(),
    });
    $('#fidelity-dialog').close();
    await loadPreview();
  } catch (error) {
    showError(error.message);
  }
}

function bindBatchPage() {
  $('#batch-configuration').addEventListener('change', (event) => {
    setConfigurationId(Number(event.target.value));
    loadPreview();
  });
  $('#batch-scale-confirmed').addEventListener('change', validateBatchStart);
  $('#batch-benchmark-list').addEventListener('change', validateBatchStart);
  $('#batch-start-form').addEventListener('submit', startBatch);
  $('#batch-report').addEventListener('submit', (event) => {
    if (event.target.id === 'batch-conclusion-form') submitConclusion(event);
  });
  $('#batch-fidelity-guidance').addEventListener('click', (event) => {
    if (event.target.closest('#open-fidelity-decision')) openFidelityDialog();
  });
  $('#fidelity-form').addEventListener('submit', submitFidelityDecision);
  $('#batch-report').addEventListener('click', (event) => {
    if (event.target.closest('#open-continue')) openContinueDialog();
    if (event.target.closest('#open-conclusion-correction')) {
      batchState.correctingConclusion = true;
      renderBatchReport();
    }
  });
  $('#continue-form').addEventListener('submit', submitContinue);
}

async function initializeBatchPage() {
  bindBatchPage();
  try {
    const configurations = await get('/api/channel-configurations');
    batchState.configurations = (configurations.configurations || [])
      .filter((configuration) => configuration.business_status !== 'disabled');
    batchState.configurationId = parameterId('configuration');
    renderConfigurationSelector();
    const batchId = parameterId('batch');
    if (batchId) {
      await loadBatch(batchId);
    } else if (batchState.configurationId || batchState.configurations.length) {
      batchState.configurationId = batchState.configurationId || batchState.configurations[0].id;
      $('#batch-configuration').value = String(batchState.configurationId);
      await loadPreview();
    } else {
      updateProgress('fidelity');
      renderPreview(); renderBenchmarkStep(); renderBatchReport();
    }
  } catch (error) {
    showError(error.message);
  }
}

initializeBatchPage();
