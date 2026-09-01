const presetTarget = Number(new URLSearchParams(location.search).get('target') || 0);
let localRunners = [];

async function init() {
  try {
    const [targets, estimate, runners] = await Promise.all([
      get('/api/targets'), loadEstimate(), get('/api/local-runners'),
    ]);
    localRunners = runners;
    const online = runners.filter((runner) => runner.status === 'online' && !runner.update_available);
    $('#f-runner').innerHTML = online.length ? online.map((runner) => `<option value="${runner.id}">${esc(runner.name)} · v${esc(runner.version)}</option>`).join('') : '<option value="">没有可用的在线执行器</option>';
    $('#runner-status').textContent = online.length ? `${online.length} 台在线；任务凭据只会加密给选中的电脑。` : '请先启动并配对本地执行器；版本过旧或离线时不能提交压力测试。';
    if (!targets.length) {
      $('#notice').classList.remove('hide');
      $('#notice').textContent = '还没有已通过准入的渠道。';
      $('#btn-submit').disabled = true;
      return;
    }
    if (!online.length) $('#btn-submit').disabled = true;
    $('#f-target').innerHTML = targets.map((target) =>
      `<option value="${target.id}" ${target.id === presetTarget ? 'selected' : ''}>${
        esc(target.name)} · ${esc(target.model)}</option>`).join('');
    renderEstimate(estimate);
  } catch (error) { showError(error.message); }
}

async function loadEstimate() {
  const params = new URLSearchParams({
    kind: 'load', load_levels: $('#f-levels').value,
    load_requests_per_level: $('#f-requests').value,
  });
  return get(`/api/estimate?${params}`);
}

function renderEstimate(estimate) {
  $('#est').innerHTML = [
    ['本次请求数', estimate.requests], ['本次最大档位', estimate.concurrency],
    ['预计 tokens', estimate.tokens.toLocaleString()],
    ['预计费用', `¥${estimate.cost.toFixed(4)}`],
  ].map(([key, value]) => `<div class="metric"><div class="k">${key}</div>
    <div class="v">${value}</div></div>`).join('');
  $('#f-limit').placeholder = `默认 ${Math.max(estimate.cost * 4, 0.5).toFixed(2)}`;
}

['#f-levels', '#f-requests'].forEach((selector) => $(selector).addEventListener('change', async () => {
  try { renderEstimate(await loadEstimate()); } catch (error) { showError(error.message); }
}));

$('#btn-submit').addEventListener('click', async () => {
  clearError();
  const levels = $('#f-levels').value.split(',').map((value) => Number(value.trim()))
    .filter((value) => Number.isInteger(value) && value > 0);
  if (!levels.length) return showError('至少填写一个有效并发档位');
  const body = {
    kind: 'load', target_id: Number($('#f-target').value),
    local_runner_id: Number($('#f-runner').value),
    load_levels: levels,
    load_requests_per_level: Number($('#f-requests').value),
    load_cooldown_seconds: Number($('#f-cooldown').value),
    load_stream: $('#f-stream').checked,
    load_mode: $('#f-mode').value,
    load_prompt_profile: $('#f-profile').value,
    load_interval_seconds: Number($('#f-interval').value),
    load_burst_period_seconds: Number($('#f-burst').value),
    load_max_in_flight: Number($('#f-max-inflight').value),
  };
  const maxTokens = Number($('#f-max-tokens').value);
  if (maxTokens > 0) body.load_max_tokens = maxTokens;
  const limit = Number($('#f-limit').value);
  if (limit > 0) body.cost_limit = limit;
  if (!body.local_runner_id) return showError('请先选择一台在线且版本合格的本地执行器');
  try {
    $('#btn-submit').disabled = true;
    const result = await post('/api/tasks', body);
    location.href = `task.html?id=${result.task_id}`;
  } catch (error) {
    $('#btn-submit').disabled = false;
    showError(error.message);
  }
});

$('#create-pairing-code').onclick = async () => {
  const name = $('#runner-name').value.trim();
  if (!name) return showError('请填写执行器名称');
  try {
    const result = await post('/api/local-runners/pairing-codes', { name });
    $('#pairing-result').innerHTML = `一次性配对码：<code>${esc(result.pairing_code)}</code> · ${fmtTime(result.expires_at)} 前有效`;
  } catch (error) { showError(error.message); }
};

init();
