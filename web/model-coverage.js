window.ModelCoverage = (() => {
  const $ = id => document.getElementById(id), node = (...args) => Workbench.node(...args);
  const api = (path = '', body, method = 'POST') => Workbench.api('/api/model-coverage' + path, body ? {method, body:JSON.stringify(body)} : {});
  const selected = new Map(), expanded = new Set();
  let data = {channels:[], models:[], schedules:[], rules:{}}, channels = [], fetchIds = [], mapping, enrollmentItems = [], verificationItems = [], preview;
  let previewGeneration = 0, refreshing = false, protocolAvailable = false;
  const availability = {listed:'已列出', not_listed:'未列出', unknown:'尚未获取', fetch_failed:'获取失败', connection_changed:'连接已变更'};
  const enrollment = {scheduled:'已加入计划', missing:'未接入', unscheduled:'待排期', paused:'已暂停'};
  const health = {stable:'稳定', observing:'可用，待观察', unstable:'不稳定', untested:'未测试', stale:'结果已过期', connection_changed:'连接变更，待重测'};
  const speed = {normal:'速度正常', slow:'速度缓慢', collecting:'速度基线采样中', disabled:'未评估速度', unknown:'速度数据不足', unavailable:'速度数据不足'};
  const time = value => value ? new Date(value * 1000).toLocaleString() : '尚无记录';
  const key = item => `${item.channel_id}:${item.id}`;
  const selections = items => items.map(item => ({channel_id:item.channel_id, model_id:item.id}));
  const channelData = id => data.channels.find(c => c.channel_id === id);
  function matches(item) {
    const query = $('model-search').value.trim().toLowerCase(), filter = $('coverage-filter').value;
    if (query && !`${item.label} ${item.model} ${item.upstream_model}`.toLowerCase().includes(query)) return false;
    if (filter === 'listed') return item.availability === 'listed';
    if (filter === 'unmeasured') return item.availability === 'listed' && ['untested','connection_changed','stale'].includes(item.measurement.status);
    if (filter === 'stable-missing') return item.measurement.status === 'stable' && item.enrollment !== 'scheduled';
    return filter !== 'unstable' || item.measurement.status === 'unstable';
  }
  function counts() {
    $('selection-count').textContent = `已选择 ${selected.size} 个渠道模型`;
    $('verify-models').disabled = $('enroll-models').disabled = !selected.size || !data.stability_available;
    $('coverage-mode').textContent = data.stability_available ? '实测状态：最近 7 天内最多 5 批；至少 3 批均达标才显示稳定，48 小时未测标为过期。列表没有自动获取任务。' : '当前为独立工具模式；获取与查看可用，验证和排期请使用完整工作台或定时测试模式。';
  }
  async function reload() { await view.onRefresh(); }
  function action(label, callback) {
    const button = node('button', label, 'secondary'); button.type = 'button';
    button.addEventListener('click', () => Promise.resolve(callback()).catch(error => $('coverage-message').textContent = error.message));
    return button;
  }
  function badge(value, style = '') { return node('span', value, 'coverage-state ' + style); }
  function openCatalog(model = '') {
    $('catalog-form').reset(); $('catalog-error').textContent = '';
    $('catalog-model').value = $('catalog-label').value = model;
    $('catalog-family').value = model.startsWith('claude-') ? 'Claude' : 'GPT';
    $('catalog-protocol').value = model === 'gpt-6-astra' ? 'responses' : model.startsWith('claude-') ? 'anthropic' : 'openai';
    $('catalog-dialog').showModal();
  }
  function openFetch(ids) {
    fetchIds = ids; $('fetch-form').reset(); $('fetch-error').textContent = '';
    if (ids.length === 1) $('fetch-auth').value = channelData(ids[0])?.discovery?.auth_kind || 'openai';
    $('fetch-description').textContent = `手动获取 ${ids.length} 个渠道的模型列表，使用各自已保存的凭据。`;
    $('fetch-dialog').showModal();
  }
  function openMapping(item) {
    mapping = item; $('mapping-error').textContent = '';
    $('mapping-description').textContent = item.label;
    $('mapping-model').value = item.upstream_model; $('mapping-protocol').value = item.protocol;
    $('discovered-models').replaceChildren(...(channelData(item.channel_id)?.discovered_models || []).map(id => new Option(id, id)));
    $('mapping-dialog').showModal();
  }
  function openVerify(items) {
    if (!items.length) return;
    verificationItems = items; $('verify-form').reset(); $('verify-error').textContent = '';
    $('verify-description').textContent = `${items.length} 个渠道模型，每个执行 1 轮 ${data.rules.requests_per_round} 个请求，共 ${items.length * data.rules.requests_per_round} 个请求。结果进入观察记录；本次不发送通知。`;
    $('verify-dialog').showModal();
  }
  async function updatePreview() {
    const generation = ++previewGeneration;
    preview = null; $('enroll-submit').disabled = true; $('enroll-confirm').checked = false;
    $('enroll-preview').replaceChildren(); $('enroll-error').textContent = '';
    const id = Number($('enroll-schedule').value); if (!id) return;
    try {
      const result = await api('/enrollment/preview', {schedule_id:id, items:selections(enrollmentItems)});
      if (generation !== previewGeneration) return;
      preview = result;
      $('enroll-preview').append(node('p', `每天 ${result.daily_times}；每批新增 ${result.added_requests_per_run} 个请求，每天新增 ${result.added_requests_per_day} 个请求。`));
      const labels = {skip:'已在计划中，跳过', create:'创建目标并加入', attach:'复用目标并加入', enable:'恢复目标并加入'};
      for (const item of result.items) $('enroll-preview').append(node('p', `渠道 #${item.channel_id} · ${item.model} · ${labels[item.action]}`));
      $('enroll-submit').disabled = false;
    } catch (error) { if (generation === previewGeneration) $('enroll-error').textContent = error.message; }
  }
  const view = {
    onChange() {}, onRefresh:async () => {},
    async load() {
      data = await api();
      const valid = new Map(data.channels.flatMap(c => c.models).map(item => [key(item), item]));
      for (const [id] of selected) { if (valid.has(id)) selected.set(id, valid.get(id)); else selected.delete(id); }
      counts();
    },
    setProtocolAvailable(value) { protocolAvailable = value; },
    setChannels(value) { channels = value; $('fetch-all').disabled = !channels.some(c => c.enabled); },
    matchesChannel(id) { return (channelData(id)?.models || []).some(matches) || (!data.models.length); },
    render(channel) {
      const info = channelData(channel.id), details = node('details', '', 'model-coverage');
      if (!info) return details;
      details.dataset.channel = channel.id;
      const listed = info.models.filter(m => m.availability === 'listed').length;
      const scheduled = info.models.filter(m => m.enrollment === 'scheduled').length;
      details.append(node('summary', `常用模型 ${info.models.length} 个 · 上游已列出 ${listed} · 已排期 ${scheduled}`));
      details.open = expanded.has(channel.id) || Boolean($('coverage-filter').value || $('model-search').value.trim());
      details.addEventListener('toggle', () => details.open ? expanded.add(channel.id) : expanded.delete(channel.id));
      const fetch = action('获取模型', () => openFetch([channel.id])); fetch.disabled = !channel.enabled;
      details.append(fetch, node('p', `上次成功获取：${time(info.discovery?.succeeded_at)}${info.discovery?.error ? ' · 最近获取失败或未完成，保留上次清单' : ''}`, 'hint'));
      const scroll = node('div', '', 'coverage-scroll'), table = node('table', '', 'coverage-table');
      const head = node('thead'), header = node('tr');
      for (const title of ['选择','模型','上游提供','定时测试','实测状态','操作']) header.append(node('th', title));
      head.append(header); table.append(head);
      const body = node('tbody');
      for (const item of info.models.filter(matches)) {
        const row = node('tr'); row.dataset.model = item.model;
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.has(key(item)); checkbox.disabled = !channel.enabled;
        checkbox.setAttribute('aria-label', `选择 ${channel.name} ${item.label}`);
        checkbox.addEventListener('change', () => { checkbox.checked ? selected.set(key(item), item) : selected.delete(key(item)); counts(); });
        const selectCell = node('td'); selectCell.append(checkbox);
        const label = node('td', item.label); label.append(node('small', `${item.upstream_model} · ${item.protocol}`, 'hint'));
        const upstream = node('td'); upstream.append(badge(availability[item.availability], item.availability === 'listed' ? 'good' : ''));
        if (item.availability === 'fetch_failed' && item.previously_listed) upstream.append(node('small', '上次清单曾列出', 'hint'));
        const plan = node('td'); plan.append(badge(enrollment[item.enrollment], item.enrollment === 'scheduled' ? 'good' : ''));
        if (item.schedules.length) plan.append(node('small', item.schedules.map(s => s.name + (s.enabled ? '' : '（暂停）')).join('、'), 'hint'));
        const measurement = node('td'), status = item.measurement.status;
        measurement.append(badge(health[status], status === 'stable' ? 'good' : status === 'unstable' ? 'bad' : 'wait'));
        measurement.append(node('small', `最近测试：${time(item.measurement.tested_at)}`, 'hint'));
        if (item.measurement.samples) measurement.append(node('small', `${item.measurement.batches} 批 · 成功 ${item.measurement.successes}/${item.measurement.samples} · ${speed[item.measurement.speed] || '速度数据不足'}`, 'hint'));
        const failures = Object.entries(item.measurement.failures || {});
        if (failures.length) measurement.append(node('small', failures.map(([name,count]) => `${name} × ${count}`).join('、'), 'hint'));
        if (item.measurement.report_id && data.stability_available) {
          const link = node('a', '查看报告'); link.href = `/stability/?run=${item.measurement.report_id}`; measurement.append(link);
        }
        const actions = node('td'); actions.append(action('映射', () => openMapping(item)));
        if (protocolAvailable && channel.enabled) {
          const link = node('a', '检测协议');
          link.href = '/admission/protocol/?' + new URLSearchParams({channel:channel.id, model:item.upstream_model}); actions.append(link);
        }
        const verify = action('验证', () => openVerify([item])); verify.disabled = !channel.enabled || !data.stability_available; actions.append(verify);
        row.append(selectCell, label, upstream, plan, measurement, actions); body.append(row);
      }
      table.append(body); scroll.append(table); details.append(scroll);
      if (info.new_models.length) {
        const discovered = node('details', '', 'coverage-discovered'); discovered.append(node('summary', `其他已发现模型 ${info.new_models.length} 个`));
        for (const model of info.new_models) discovered.append(action(model, () => openCatalog(model)));
        details.append(discovered);
      }
      return details;
    },
  };
  for (const id of ['model-search','coverage-filter']) $(id).addEventListener(id === 'model-search' ? 'input' : 'change', () => view.onChange());
  document.querySelectorAll('[data-coverage-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.coverageClose).close()));
  $('fetch-all').addEventListener('click', () => openFetch(channels.filter(c => c.enabled).map(c => c.id)));
  $('catalog-add').addEventListener('click', () => openCatalog());
  $('select-models').addEventListener('click', () => {
    const shown = new Set([...document.querySelectorAll('.model-coverage')].map(element => Number(element.dataset.channel)));
    for (const channel of channels.filter(c => c.enabled && shown.has(c.id))) for (const item of (channelData(channel.id)?.models || []).filter(matches)) {
      if (selected.size >= 100 && !selected.has(key(item))) { $('coverage-message').textContent = '每批最多选择 100 个渠道模型。'; break; }
      selected.set(key(item), item);
    }
    counts(); view.onChange();
  });
  $('clear-models').addEventListener('click', () => { selected.clear(); counts(); view.onChange(); });
  $('verify-models').addEventListener('click', () => openVerify([...selected.values()]));
  $('enroll-models').addEventListener('click', async () => {
    enrollmentItems = [...selected.values()];
    $('enroll-form').reset(); $('enroll-schedule').replaceChildren(new Option('请选择计划',''), ...data.schedules.filter(s => s.enabled).map(s => new Option(s.name,s.id)));
    $('enroll-preview').replaceChildren(); $('enroll-error').textContent = ''; $('enroll-submit').disabled = true;
    $('enroll-dialog').showModal();
  });
  $('enroll-schedule').addEventListener('change', updatePreview);
  async function submit(id, operation) {
    $(id + '-submit').disabled = true; $(id + '-error').textContent = '';
    try { await operation(); $(id + '-dialog').close(); await reload(); }
    catch (error) { $(id + '-error').textContent = error.message; }
    finally { $(id + '-submit').disabled = false; }
  }
  $('catalog-form').addEventListener('submit', event => {
    event.preventDefault(); submit('catalog', () => api('/models', {model:$('catalog-model').value, label:$('catalog-label').value, family:$('catalog-family').value, protocol:$('catalog-protocol').value}));
  });
  $('mapping-form').addEventListener('submit', event => {
    event.preventDefault(); submit('mapping', () => api(`/channels/${mapping.channel_id}/models/${mapping.id}`, {upstream_model:$('mapping-model').value, protocol:$('mapping-protocol').value}, 'PUT'));
  });
  $('fetch-form').addEventListener('submit', event => {
    event.preventDefault(); if (!$('fetch-confirm').checked) return;
    const ids = [...fetchIds], auth_kind = $('fetch-auth').value;
    submit('fetch', async () => {
      let failed = 0;
      for (let offset = 0; offset < ids.length; offset += 3) {
        const batch = await Promise.allSettled(ids.slice(offset,offset+3).map(id => api(`/channels/${id}/fetch`, {auth_kind, confirm_live:true})));
        failed += batch.filter(result => result.status === 'rejected' || !result.value.ok).length;
        $('fetch-description').textContent = `已完成 ${Math.min(offset+3,ids.length)}/${ids.length}，失败 ${failed}。`;
      }
      $('coverage-message').textContent = `模型清单获取完成：成功 ${ids.length-failed}，失败 ${failed}。`;
    });
  });
  $('enroll-form').addEventListener('submit', event => {
    event.preventDefault(); if (!preview || !$('enroll-confirm').checked) return;
    const current = preview;
    submit('enroll', async () => {
      const result = await api('/enrollment', {schedule_id:current.schedule_id, items:selections(enrollmentItems), preview_token:current.preview_token, confirm_live:true});
      $('coverage-message').textContent = `已加入 ${result.added} 个目标，跳过 ${result.skipped} 个重复目标。`;
    });
  });
  $('verify-form').addEventListener('submit', event => {
    event.preventDefault(); if (!$('verify-confirm').checked) return;
    submit('verify', async () => {
      const result = await api('/verify', {items:selections(verificationItems), confirm_live:true});
      $('coverage-message').textContent = `验证任务 #${result.run_id} 已排队，实测状态会自动更新。`;
    });
  });
  setInterval(async () => {
    if (document.hidden || document.querySelector('dialog[open]') || refreshing) return;
    refreshing = true;
    try { await reload(); } catch (error) { $('coverage-message').textContent = error.message; } finally { refreshing = false; }
  }, 15000);
  return view;
})();
