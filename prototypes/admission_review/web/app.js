const $ = id => document.getElementById(id);
let currentRun = null;

function node(tag, text = '', className = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {'Content-Type': 'application/json', ...(options.headers || {})},
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '操作失败');
  return data;
}

function show(message, error = false) {
  $('message').textContent = message;
  $('message').className = error ? 'error' : 'ok';
}

function renderRun() {
  if (!currentRun) return;
  $('test-card').hidden = false;
  $('run-summary').textContent = `#${currentRun.id} · ${currentRun.channel_name} · ${currentRun.model} · ${currentRun.status}`;
  const testing = currentRun.status === 'testing';
  const reviewed = currentRun.status === 'reviewed';
  $('test-complete').disabled = !testing;
  $('test-failed').disabled = !testing;
  $('review-card').hidden = !reviewed && currentRun.status !== 'awaiting_review';
  $('reviewer').disabled = reviewed;
  $('review-note').disabled = reviewed;
  document.querySelectorAll('[name="decision"]').forEach(button => {
    button.disabled = reviewed;
  });
  $('review-result').hidden = !reviewed;
  if (reviewed) {
    const decision = currentRun.review_decision === 'qualified' ? '合格' : '不合格';
    $('review-result').textContent = `已由 ${currentRun.reviewer} 人工确认：${decision}`;
  }
}

async function loadOutbox() {
  const data = await api('/api/outbox');
  const items = data.jobs.map(job => {
    const card = node('article', '', 'outbox-item');
    card.append(
      node('strong', `任务 #${job.id} · 准入 #${job.run_id}`),
      node('span', `状态：${job.status} · 字段数：${Object.keys(job.payload.fields).length}`),
    );
    return card;
  });
  $('outbox').replaceChildren(...(items.length ? items : [node('p', '尚无待同步记录。', 'hint')]));
}

$('start-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    currentRun = await api('/api/runs', {method: 'POST', body: JSON.stringify({
      channel_name: $('channel-name').value,
      base_url: $('base-url').value,
      api_key: $('api-key').value,
      model: $('model').value,
      protocol: $('protocol').value,
    })});
    $('api-key').value = '';
    $('base-url').value = currentRun.channel_url;
    $('reviewer').value = '';
    $('review-note').value = '';
    renderRun();
    show('准入测试记录已创建，临时凭据未保存。');
  } catch (error) { show(error.message, true); }
});

async function finish(outcome) {
  try {
    currentRun = await api(`/api/runs/${currentRun.id}/test-result`, {
      method: 'POST',
      body: JSON.stringify({outcome, summary: {framework_demo: true}}),
    });
    renderRun();
    show('测试结果已记录，等待人工确认。');
  } catch (error) { show(error.message, true); }
}

$('test-complete').addEventListener('click', () => finish('completed'));
$('test-failed').addEventListener('click', () => finish('failed'));

$('review-form').addEventListener('submit', async event => {
  event.preventDefault();
  const decision = event.submitter?.value;
  if (!decision) return show('请选择人工判定结果。', true);
  try {
    currentRun = await api(`/api/runs/${currentRun.id}/review`, {
      method: 'POST',
      body: JSON.stringify({
        decision,
        reviewer: $('reviewer').value,
        note: $('review-note').value,
      }),
    });
    renderRun();
    await loadOutbox();
    show('人工判定已记录，飞书任务正在等待字段映射。');
  } catch (error) { show(error.message, true); }
});

loadOutbox().catch(error => show(error.message, true));
