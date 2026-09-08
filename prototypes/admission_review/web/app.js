const $ = id => document.getElementById(id);
let metadata = null;

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

function statusText(item) {
  const labels = {
    synced: '已写入飞书',
    sending: '正在写入',
    pending: '等待写入',
    failed: '写入失败，可重试',
    not_configured: '飞书尚未配置',
  };
  return labels[item.sync_status] || item.sync_status;
}

function updateGroupHint() {
  const selected = metadata?.models.find(item => item.id === $('model').value);
  $('group-preview').textContent = selected ? `将记录到分组：${selected.group}` : '';
}

async function retry(id) {
  try {
    const item = await api(`/api/participations/${id}/retry`, {method: 'POST'});
    await loadParticipations();
    show(statusText(item));
  } catch (error) {
    show(error.message, true);
  }
}

async function loadParticipations() {
  const data = await api('/api/participations');
  const cards = data.participations.map(item => {
    const card = node('article', '', 'record-item');
    card.append(
      node('strong', item.channel_name),
      node('span', `测试分组：${item.test_group}`),
      node('span', `同步状态：${statusText(item)}`, `sync-${item.sync_status}`),
    );
    if (metadata.feishu.configured && ['failed', 'not_configured'].includes(item.sync_status)) {
      const button = node('button', '重试写入', 'secondary');
      button.type = 'button';
      button.addEventListener('click', () => retry(item.id));
      card.append(button);
    }
    return card;
  });
  $('records').replaceChildren(
    ...(cards.length ? cards : [node('p', '尚无准入参与记录。', 'hint')]),
  );
}

$('record-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('submit').disabled = true;
  try {
    const item = await api('/api/participations', {
      method: 'POST',
      body: JSON.stringify({
        channel: $('channel').value,
        model: $('model').value,
      }),
    });
    $('channel').value = '';
    await loadParticipations();
    show(`${item.channel_name} · ${item.test_group}：${statusText(item)}`);
  } catch (error) {
    show(error.message, true);
  } finally {
    $('submit').disabled = false;
  }
});

(async () => {
  metadata = await api('/api/meta');
  $('model').replaceChildren(...metadata.models.map(item =>
    new Option(`${item.group} · ${item.label}`, item.id),
  ));
  $('model').addEventListener('change', updateGroupHint);
  updateGroupHint();
  const fields = metadata.feishu.written_fields.join('、');
  $('feishu-state').textContent = metadata.feishu.configured
    ? `飞书已配置；程序只写入：${fields}。`
    : `飞书尚未配置；当前会保留待写记录。程序只负责：${fields}。`;
  await loadParticipations();
})().catch(error => show(error.message, true));
