(() => {
  'use strict';

  const base = '/volcengine-admission';
  const $ = id => document.getElementById(id);
  const node = (tag, value, className = '') => Workbench.node(tag, value, className);
  const states = {
    queued: '排队中', running: '验收中', completed: '本轮已结束', stopped: '本地已停止',
    interrupted: '服务中断', pending: '等待发送', sending: '提交中', submitted: '视频已提交',
    polling: '等待视频完成', passed: '通过', failed: '失败', unknown: '提交结果未知',
    not_sent: '未发送', inconclusive: '无法判定',
  };
  let catalog = [];
  const controls = new Map();
  let preview = null;
  let previewSnapshot = '';
  let previewRevision = 0;
  let previewExpiry = null;
  let submitting = false;
  let checkingAuth = false;
  let refreshing = false;
  let selectedRun = null;
  let currentRun = null;
  let pollTimer = null;
  let runAction = false;
  let detailRevision = 0;

  function message(value, error = false) {
    $('message').textContent = value;
    $('message').className = error ? 'error' : 'status';
  }

  function mode() {
    return document.querySelector('input[name="mode"]:checked').value;
  }

  function money(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `¥${number.toFixed(5).replace(/0+$/, '').replace(/\.$/, '')}` : '未提供';
  }

  function bytes(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '未提供';
    return `${(Number(value) / (1024 * 1024)).toFixed(2)} MiB`;
  }

  function renderCapacity(capacity) {
    if (!capacity) { $('capacity-status').textContent = '本地样片容量暂不可用'; return; }
    $('capacity-status').textContent = `本地样片：已用 ${bytes(capacity.used_bytes)} / ${bytes(capacity.max_bytes)}，可用 ${bytes(capacity.available_bytes)}，正在预留 ${bytes(capacity.reserved_bytes)}；文件 ${capacity.used_files} / ${capacity.max_files}，另预留 ${capacity.reserved_files} 个。空间不足的项目会在生成前停止发送。`;
  }

  async function refreshCapacity() {
    renderCapacity(await Workbench.api(`${base}/api/capacity`));
  }

  function canCleanup(run) {
    return run && !run.execution_active && !['queued', 'running'].includes(run.status) && (run.items || []).some(item => item.media_id);
  }

  function datetime(value) {
    if (value === null || value === undefined || value === '') return '时间未提供';
    const parsed = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    return Number.isNaN(parsed.getTime()) ? '时间未提供' : parsed.toLocaleString('zh-CN', {hour12: false});
  }

  function specLabel(spec = {}) {
    const values = [];
    if (spec.resolution) values.push(spec.resolution);
    if (spec.duration !== undefined) values.push(`${spec.duration} 秒`);
    if (spec.frames !== undefined) values.push(`${spec.frames} 帧（约 ${(Number(spec.frames) / 24).toFixed(2)} 秒）`);
    if (spec.size) values.push(spec.size, '1 张');
    if (spec.resolution) values.push('1:1');
    return values.join(' · ');
  }

  function notes(target, values) {
    target.replaceChildren(...(values || []).map(value => node('p', value)));
  }

  function selection() {
    const items = [];
    for (const model of catalog) {
      const control = controls.get(model.model_id);
      if (!control.checked.checked) continue;
      const spec = {};
      if (model.kind === 'image') {
        spec.size = control.size.value;
      } else {
        spec.resolution = control.resolution.value;
        if (control.duration.value.startsWith('frames:')) spec.frames = Number(control.duration.value.slice(7));
        else spec.duration = Number(control.duration.value);
      }
      items.push({model_id: model.model_id, spec});
    }
    const result = {mode: mode(), items};
    const prompt = $('prompt').value.trim();
    if (prompt) result.prompt = prompt;
    return result;
  }

  function syncButtons() {
    const live = mode() === 'live';
    $('check-auth').disabled = checkingAuth || (live && (!$('confirm-read').checked || !$('api-key').value.trim()));
    $('confirm-live').disabled = !live;
    $('execute').disabled = submitting || !preview || (live && (!$('confirm-live').checked || !$('api-key').value.trim()));
    $('preview').disabled = submitting || catalog.length === 0 || selection().items.length === 0;
    $('resume-run').disabled = runAction || !currentRun?.can_resume || currentRun.execution_active || currentRun.id !== selectedRun || !$('confirm-resume').checked || (currentRun.mode === 'live' && !$('api-key').value.trim());
    $('stop-run').disabled = runAction || !currentRun || currentRun.id !== selectedRun || !['queued', 'running'].includes(currentRun.status);
    $('cleanup-media').disabled = runAction || !canCleanup(currentRun) || currentRun.id !== selectedRun || !$('confirm-cleanup').checked;
  }

  function updateSelection() {
    let count = 0;
    let legacy = 0;
    for (const model of catalog) {
      const control = controls.get(model.model_id);
      const checked = control.checked.checked;
      control.card.classList.toggle('selected', checked);
      for (const input of control.specs.querySelectorAll('select,input')) input.disabled = !checked;
      if (checked) { count++; if (model.legacy) legacy++; }
    }
    $('selection-summary').textContent = `已选 ${count} 个型号，每个型号生成一次`;
    $('legacy-count').textContent = legacy ? `已选 ${legacy} 款` : '未选择';
    syncButtons();
  }

  function invalidatePreview() {
    previewRevision++;
    preview = null;
    previewSnapshot = '';
    clearTimeout(previewExpiry);
    $('preview-panel').hidden = true;
    $('confirm-live').checked = false;
    $('execute-status').textContent = '';
    updateSelection();
  }

  function selectControl(label, id, values, selected) {
    const wrapper = node('label', label);
    wrapper.htmlFor = id;
    const select = document.createElement('select');
    select.id = id;
    for (const value of values) select.append(new Option(value.label ?? value, value.value ?? value));
    select.value = String(selected);
    wrapper.append(select);
    return {wrapper, select};
  }

  function renderCatalog() {
    $('current-models').replaceChildren();
    $('legacy-model-list').replaceChildren();
    controls.clear();
    catalog.forEach((model, index) => {
      const card = node('article', '', 'model-card');
      card.dataset.modelId = model.model_id;
      const label = node('label', '', 'check model-choice');
      const checked = document.createElement('input');
      checked.type = 'checkbox';
      checked.id = `model-${index}`;
      checked.checked = Boolean(model.default_selected);
      checked.dataset.modelId = model.model_id;
      label.append(checked, node('span', `${model.name} · ${model.kind === 'video' ? '视频' : '图片'}`));
      const specs = node('div', '', 'model-specs');
      const control = {card, checked, specs};
      const defaults = model.default_spec || {};
      if (model.kind === 'image') {
        const size = selectControl('图片尺寸', `size-${index}`, model.sizes || [], defaults.size);
        control.size = size.select;
        specs.append(size.wrapper, node('p', '单张图片，关闭组图', 'hint fixed-spec'));
      } else {
        const resolution = selectControl('视频清晰度', `resolution-${index}`, model.resolutions || [], defaults.resolution);
        const durations = [];
        if (model.supports_frames && Number.isInteger(defaults.frames)) {
          durations.push({label: `${defaults.frames} 帧 · 最小规格`, value: `frames:${defaults.frames}`});
        }
        for (let duration = model.duration_min; duration <= model.duration_max; duration++) {
          durations.push({label: `${duration} 秒`, value: String(duration)});
        }
        const duration = selectControl('视频时长', `duration-${index}`, durations, defaults.frames ? `frames:${defaults.frames}` : defaults.duration);
        control.resolution = resolution.select;
        control.duration = duration.select;
        specs.append(resolution.wrapper, duration.wrapper, node('p', '画面比例 1:1 · 无参考视频', 'hint fixed-spec'));
      }
      card.append(label, node('p', model.model_id, 'model-id'), specs);
      controls.set(model.model_id, control);
      $(model.legacy ? 'legacy-model-list' : 'current-models').append(card);
    });
    $('catalog-status').textContent = `已加载 ${catalog.filter(model => !model.legacy).length} 款默认型号和 ${catalog.filter(model => model.legacy).length} 款存量兼容型号。`;
    const eos = [...new Set(catalog.filter(model => model.legacy && model.eos_at).map(model => model.eos_at.slice(0, 10)))];
    $('legacy-note').textContent = `已停止新购，需账号先前已开通；${eos.length === 1 ? `计划于 ${eos[0]} 下线。` : ''}不会自动切换至新型号。`;
    $('reset-models').disabled = false;
    updateSelection();
  }

  function renderPreview(data) {
    const rows = data.items.map(item => {
      const row = document.createElement('tr');
      const name = node('td', item.name);
      name.append(node('code', item.model_id, 'model-id'));
      row.append(name, node('td', specLabel(item.spec)), node('td', '1 次'), node('td', money(item.estimated_cny)));
      return row;
    });
    $('preview-rows').replaceChildren(...rows);
    $('preview-summary').textContent = `${data.mode === 'mock' ? 'Mock 演示' : '真实生成'} · ${data.items.length} 个型号 / ${data.items.length} 次生成 · 官方刊例价估算 ${money(data.estimated_cny)}`;
    if (data.mode === 'mock') $('preview-summary').textContent += '（本次不产生火山费用）';
    notes($('preview-notes'), [...(data.notes || []), ...(data.capacity_warnings || [])]);
    if (data.capacity) renderCapacity(data.capacity);
    $('preview-panel').hidden = false;
    $('confirm-live').checked = false;
    syncButtons();
  }

  function reportUrl(id) {
    return `${base}/api/runs/${encodeURIComponent(id)}/report`;
  }

  function itemEvidence(item) {
    return {
      item_id: item.item_id, model_id: item.model_id, kind: item.kind, spec: item.spec,
      status: item.status, task_id: item.task_id ?? null, http_status: item.http_status ?? null,
      error_category: item.error_category ?? null, elapsed_ms: item.elapsed_ms ?? null, usage: item.usage || {},
    };
  }

  function renderItem(run, item) {
    const card = node('article', '', 'result-card');
    card.dataset.itemId = item.item_id;
    const heading = node('div', '', 'result-heading');
    heading.append(node('h3', item.name), node('span', states[item.status] || item.status, `badge status-${item.status}`));
    card.append(heading, node('p', item.model_id, 'model-id'), node('p', specLabel(item.spec), 'hint'));
    const metrics = [];
    if (item.elapsed_ms !== null && item.elapsed_ms !== undefined) metrics.push(`耗时 ${(item.elapsed_ms / 1000).toFixed(2)} 秒`);
    if (item.http_status !== null && item.http_status !== undefined) metrics.push(`HTTP ${item.http_status}`);
    if (item.error_category) metrics.push(`分类 ${item.error_category}`);
    if (metrics.length) card.append(node('p', metrics.join(' · '), 'hint'));
    if (item.task_id) card.append(node('p', `任务 ID：${item.task_id}`, 'hint'));
    if (item.status === 'unknown') card.append(node('p', '提交结果不明，未自动重发，以免重复生成和计费。', 'hint'));
    if (item.status === 'inconclusive') card.append(node('p', '本次证据不足，无法判定该型号可用。', 'hint'));
    if (item.error_category === 'media_capacity_unavailable') {
      const hint = item.task_id
        ? '本地样片空间不足，暂时无法继续取得样片。上游任务已提交，可能继续执行并计费。请先清理历史样片，再使用原 Key 只读继续查询。'
        : item.status === 'not_sent'
          ? '本地样片空间不足，本项未发送，未创建生成任务。请先清理历史样片，再重新预览。'
          : '本地样片空间不足，暂时无法取得样片。请先清理历史样片，再核对本项任务状态，勿直接重复生成。';
      card.append(node('p', hint, 'hint'));
    }
    if (item.error_category === 'media_capacity_or_storage_failed') card.append(node('p', '上游结果已返回，但本地样片保存失败。请查看技术指标，勿直接重复生成。', 'hint'));
    if (item.media_status === 'removed') card.append(node('p', '本地样片已清理，检测结论及技术指标保留。', 'hint'));
    if (item.media_id) {
      const url = `${base}/api/runs/${encodeURIComponent(run.id)}/media/${encodeURIComponent(item.media_id)}`;
      const media = document.createElement(item.kind === 'video' ? 'video' : 'img');
      media.src = url;
      if (item.kind === 'video') { media.controls = true; media.preload = 'metadata'; media.playsInline = true; }
      else { media.alt = `${item.name} 本轮生成样片`; media.loading = 'lazy'; }
      card.append(media);
      const actions = node('div', '', 'actions');
      const download = node('a', '下载样片', 'button secondary');
      download.href = `${url}?download=true`;
      download.download = '';
      actions.append(download);
      card.append(actions);
      if (item.kind === 'video') card.append(node('p', '浏览器无法播放时可下载查看；部分清晰度采用 HEVC 编码。', 'hint'));
    }
    const details = document.createElement('details');
    details.append(node('summary', '本型号技术指标'), node('pre', JSON.stringify(itemEvidence(item), null, 2)));
    card.append(details);
    return card;
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (currentRun && (currentRun.execution_active || ['queued', 'running'].includes(currentRun.status))) {
      pollTimer = setTimeout(() => refreshRuns().catch(error => {
        message(`刷新失败：${error.message}，可点击“刷新记录”重试。`, true);
      }), 2000);
    }
  }

  function renderRun(run) {
    const previousId = currentRun?.id;
    currentRun = run;
    selectedRun = run.id;
    $('run-detail').hidden = false;
    $('run-title').textContent = `${run.mode === 'mock' ? 'Mock 演示' : '真实 Key'} · 验收结果`;
    $('run-status').textContent = states[run.status] || run.status;
    $('run-status').className = `badge status-${run.status}`;
    $('run-settling').hidden = !run.execution_active || ['queued', 'running'].includes(run.status);
    const items = run.items || [];
    $('run-summary').textContent = `${items.filter(item => item.status === 'passed').length} / ${items.length} 个型号通过 · 官方刊例价估算 ${money(run.estimated_cny)} · 实际账单未核验`;
    $('run-id').textContent = `记录 ${run.id} · ${datetime(run.created_at)}`;
    $('download-report').href = reportUrl(run.id);
    $('resume-controls').hidden = !run.can_resume || run.execution_active;
    $('cleanup-controls').hidden = !canCleanup(run);
    if (previousId !== run.id) { $('confirm-resume').checked = false; $('confirm-cleanup').checked = false; }
    notes($('run-notes'), run.notes);
    const oldItems = new Map([...$('run-items').children].map(card => [card.dataset.itemId, card]));
    const cards = items.map(item => {
      const old = oldItems.get(item.item_id);
      const fingerprint = JSON.stringify(item);
      if (old?.dataset.fingerprint === fingerprint) return old;
      const card = renderItem(run, item);
      card.dataset.fingerprint = fingerprint;
      return card;
    });
    const container = $('run-items');
    for (const child of [...container.children]) if (!cards.includes(child)) child.remove();
    cards.forEach((card, index) => {
      if (container.children[index] !== card) container.insertBefore(card, container.children[index] || null);
    });
    $('run-technical').textContent = JSON.stringify({
      id: run.id, mode: run.mode, status: run.status, created_at: run.created_at, updated_at: run.updated_at,
      estimated_cny: run.estimated_cny, billing_status: run.billing_status, execution_active: Boolean(run.execution_active),
      can_resume: Boolean(run.can_resume), items: items.map(itemEvidence),
    }, null, 2);
    syncButtons();
    schedulePoll();
  }

  async function loadRun(id) {
    selectedRun = id;
    const revision = ++detailRevision;
    clearTimeout(pollTimer);
    syncButtons();
    const run = await Workbench.api(`${base}/api/runs/${encodeURIComponent(id)}`);
    if (revision === detailRevision && selectedRun === id) renderRun(run);
  }

  async function refreshRuns() {
    if (refreshing) return;
    refreshing = true;
    $('refresh-runs').disabled = true;
    try {
      const data = await Workbench.api(`${base}/api/runs`);
      if (!selectedRun && data.runs.length) selectedRun = data.runs[0].id;
      $('runs').replaceChildren(...data.runs.map(run => {
        const button = node('button', `${run.mode === 'mock' ? 'Mock' : '真实'} · ${datetime(run.created_at)} · ${states[run.status] || run.status} · ${run.id}`, 'secondary');
        button.type = 'button';
        button.setAttribute('aria-current', run.id === selectedRun ? 'true' : 'false');
        button.addEventListener('click', () => loadRun(run.id).then(() => {
          for (const sibling of $('runs').children) sibling.setAttribute('aria-current', sibling.dataset.runId === selectedRun ? 'true' : 'false');
        }).catch(error => message(`读取记录失败：${error.message}`, true)));
        button.dataset.runId = run.id;
        return button;
      }));
      $('history-status').textContent = data.runs.length ? `${data.runs.length} 条脱敏记录` : '尚无验收记录。先选择型号并预览本次测试。';
      if (selectedRun) await loadRun(selectedRun);
      await refreshCapacity();
    } finally {
      refreshing = false;
      $('refresh-runs').disabled = false;
    }
  }

  $('admission-form').addEventListener('input', event => {
    if (event.target.id === 'confirm-live') syncButtons();
    else invalidatePreview();
  });
  $('admission-form').addEventListener('change', event => {
    if (event.target.id === 'confirm-live') syncButtons();
    else invalidatePreview();
  });
  for (const radio of document.querySelectorAll('input[name="mode"]')) radio.addEventListener('change', () => {
    $('confirm-read').checked = false;
    $('auth-status').textContent = '';
    invalidatePreview();
  });
  for (const id of ['api-key', 'confirm-read', 'confirm-resume', 'confirm-cleanup']) $(id).addEventListener('input', syncButtons);

  $('reset-models').addEventListener('click', () => {
    renderCatalog();
    invalidatePreview();
    message('已恢复默认型号和最低价规格，请重新预览。');
  });

  $('check-auth').addEventListener('click', async () => {
    if (checkingAuth) return;
    const body = {mode: mode(), api_key: $('api-key').value.trim(), confirm_read: $('confirm-read').checked};
    if (body.mode === 'live' && (!body.api_key || !body.confirm_read)) return;
    checkingAuth = true;
    $('api-key').value = '';
    $('confirm-read').checked = false;
    $('auth-status').textContent = '正在检查鉴权';
    syncButtons();
    try {
      const data = await Workbench.api(`${base}/api/auth`, {method: 'POST', body: JSON.stringify(body)});
      const label = data.status === 'passed' ? '官方端点鉴权通过' : data.status === 'failed' ? '鉴权检查未通过' : '鉴权结果无法判定';
      const metrics = [data.http_status ? `HTTP ${data.http_status}` : '', data.error_category || '', data.elapsed_ms !== undefined ? `${data.elapsed_ms} ms` : ''].filter(Boolean);
      $('auth-status').textContent = `${body.mode === 'mock' ? 'Mock 演示：' : ''}${label}${metrics.length ? ` · ${metrics.join(' · ')}` : ''}。${data.note || ''}`;
      message('临时 Key 已从输入框清空；真实生成前需重新填写。');
    } catch (error) { $('auth-status').textContent = `鉴权检查失败：${error.message}`; }
    finally { body.api_key = ''; checkingAuth = false; syncButtons(); }
  });

  $('preview').addEventListener('click', async () => {
    const snapshot = selection();
    if (!snapshot.items.length || submitting) return;
    invalidatePreview();
    const revision = previewRevision;
    $('preview').disabled = true;
    message('正在计算本次测试估价');
    try {
      const data = await Workbench.api(`${base}/api/preview`, {method: 'POST', body: JSON.stringify(snapshot)});
      if (revision !== previewRevision) { message('配置已改变，请按当前配置重新预览。'); return; }
      preview = data;
      previewSnapshot = JSON.stringify(snapshot);
      renderPreview(data);
      const expiry = new Date(typeof data.expires_at === 'number' && data.expires_at < 1e12 ? data.expires_at * 1000 : data.expires_at).getTime();
      if (Number.isFinite(expiry)) previewExpiry = setTimeout(() => {
        invalidatePreview();
        message('费用预览已过期，请重新预览本次测试。');
      }, Math.max(0, expiry - Date.now()));
      message('预览完成，请核对本轮型号、规格和费用。');
    } catch (error) { message(`预览失败：${error.message}`, true); }
    finally { syncButtons(); }
  });

  $('admission-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting || !preview) return;
    if (previewSnapshot !== JSON.stringify(selection())) { invalidatePreview(); message('配置已改变，请重新预览。', true); return; }
    const live = mode() === 'live';
    if (live && (!$('api-key').value.trim() || !$('confirm-live').checked)) { message('真实生成需填写临时 Key 并确认本轮费用。', true); return; }
    const body = {
      preview_id: preview.preview_id, fingerprint: preview.fingerprint,
      api_key: $('api-key').value.trim(), confirm_live: $('confirm-live').checked,
      idempotency_key: Workbench.requestId(),
    };
    submitting = true;
    $('api-key').value = '';
    $('confirm-live').checked = false;
    $('confirm-read').checked = false;
    $('execute-status').textContent = '正在提交本轮验收';
    syncButtons();
    try {
      const run = await Workbench.api(`${base}/api/runs`, {method: 'POST', body: JSON.stringify(body)});
      invalidatePreview();
      $('prompt').value = '';
      renderRun(run);
      await refreshRuns();
      message('本轮验收已提交，临时 Key 已清空。关闭页面后后台继续。');
    } catch (error) {
      invalidatePreview();
      message(`提交未能确认：${error.message}。请先刷新历史记录核对本轮状态，勿直接重复生成。`, true);
      await refreshRuns().catch(() => {});
    } finally { body.api_key = ''; submitting = false; syncButtons(); }
  });

  $('stop-run').addEventListener('click', async () => {
    if (runAction || !currentRun || currentRun.id !== selectedRun || !['queued', 'running'].includes(currentRun.status)) return;
    const id = currentRun.id;
    const revision = ++detailRevision;
    clearTimeout(pollTimer);
    runAction = true;
    syncButtons();
    try {
      const run = await Workbench.api(`${base}/api/runs/${encodeURIComponent(id)}/stop`, {method: 'POST', body: '{}'});
      if (revision === detailRevision && selectedRun === id) renderRun(run);
      message('已停止本地发送与等待。上游已提交任务可能继续执行并计费。');
      await refreshRuns();
    } catch (error) { message(`停止失败：${error.message}`, true); }
    finally { runAction = false; syncButtons(); schedulePoll(); }
  });

  $('resume-run').addEventListener('click', async () => {
    if (runAction || !currentRun?.can_resume || currentRun.execution_active || currentRun.id !== selectedRun || !$('confirm-resume').checked) return;
    const body = {api_key: $('api-key').value.trim(), confirm_read: true};
    if (currentRun.mode === 'live' && !body.api_key) return;
    const id = currentRun.id;
    const revision = ++detailRevision;
    clearTimeout(pollTimer);
    runAction = true;
    $('api-key').value = '';
    $('confirm-resume').checked = false;
    syncButtons();
    try {
      const run = await Workbench.api(`${base}/api/runs/${encodeURIComponent(id)}/resume`, {method: 'POST', body: JSON.stringify(body)});
      if (revision === detailRevision && selectedRun === id) renderRun(run);
      message('已继续查询已提交的视频任务，不发送新的生成请求。');
      await refreshRuns();
    } catch (error) { message(`继续查询失败：${error.message}`, true); }
    finally { body.api_key = ''; runAction = false; syncButtons(); schedulePoll(); }
  });

  $('cleanup-media').addEventListener('click', async () => {
    if (runAction || !canCleanup(currentRun) || currentRun.id !== selectedRun || !$('confirm-cleanup').checked) return;
    const id = currentRun.id;
    const revision = ++detailRevision;
    clearTimeout(pollTimer);
    runAction = true;
    $('confirm-cleanup').checked = false;
    syncButtons();
    try {
      const run = await Workbench.api(`${base}/api/runs/${encodeURIComponent(id)}/cleanup`, {
        method: 'POST', body: JSON.stringify({confirm_local_delete: true}),
      });
      invalidatePreview();
      if (revision === detailRevision && selectedRun === id) renderRun(run);
      await refreshRuns();
      message('本记录的本地样片已清理，报告和检测指标保留；未删除上游任务。');
    } catch (error) { message(`清理未完成：${error.message}`, true); }
    finally { runAction = false; syncButtons(); schedulePoll(); }
  });

  $('refresh-runs').addEventListener('click', () => refreshRuns().catch(error => message(`刷新失败：${error.message}`, true)));
  window.addEventListener('pagehide', () => { $('api-key').value = ''; $('prompt').value = ''; clearTimeout(pollTimer); clearTimeout(previewExpiry); });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    $('api-key').value = '';
    invalidatePreview();
    refreshRuns().catch(error => message(`刷新失败：${error.message}`, true));
  });

  async function initialize() {
    const data = await Workbench.api(`${base}/api/meta`);
    catalog = data.catalog;
    $('official-host').textContent = data.official_base_url;
    notes($('catalog-notes'), data.notes);
    renderCatalog();
    await refreshRuns();
  }
  initialize().catch(error => { $('catalog-status').textContent = '页面加载失败'; message(error.message, true); });
})();
