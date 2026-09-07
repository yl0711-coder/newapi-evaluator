const $ = id => document.getElementById(id);
let channels = [];
async function refresh() { channels = (await Workbench.api('/api/registry/channels')).channels; render(); }
function render() {
  const query = $('search').value.trim().toLowerCase(), state = $('status-filter').value;
  const visible = channels.filter(c => (!state || c.status === state) && `${c.name} ${c.base_url} ${c.scope}`.toLowerCase().includes(query));
  $('count').textContent = `共 ${channels.length} 条连接记录，当前显示 ${visible.length} 条`;
  $('list').replaceChildren(...visible.map(c => {
    const card = Workbench.node('article', '', 'panel record');
    card.append(Workbench.node('h3', c.name), Workbench.node('span', c.enabled ? (c.status === 'online' ? '已上线' : '已记录') : '已停用', 'badge'),
      Workbench.node('p', `${c.scope || '通用'} · ${c.multiplier}x · #${c.id}`), Workbench.node('p', c.base_url, 'muted'), Workbench.node('p', '密钥已保存', 'hint'));
    if (c.note) card.append(Workbench.node('p', c.note, 'hint'));
    const actions = Workbench.node('div', '', 'actions'); const edit = Workbench.node('button', '编辑', 'secondary'); edit.addEventListener('click', () => open(c)); actions.append(edit); card.append(actions); return card;
  }));
  if (!visible.length) $('list').append(Workbench.node('p', '没有匹配的渠道。', 'muted'));
}
function open(c = null) {
  $('channel-form').reset(); $('id').value = c?.id || ''; $('version').value = c?.version || '';
  $('editor-title').textContent = c ? '编辑渠道' : '新增已上线渠道';
  for (const [id, field] of [['url','base_url'],['name','name'],['scope','scope'],['note','note']]) $(id).value = c?.[field] || '';
  $('multiplier').value = c?.multiplier ?? ''; $('state').value = c?.status || 'online'; $('enabled').checked = c ? c.enabled : true;
  $('key').value = ''; $('key').required = !c; $('form-error').textContent = ''; $('editor').showModal();
}
$('add').addEventListener('click', () => open()); $('close').addEventListener('click', () => $('editor').close());
$('editor').addEventListener('close', () => { $('key').value = ''; });
$('search').addEventListener('input', render); $('status-filter').addEventListener('change', render);
$('refresh').addEventListener('click', () => refresh().catch(e => $('message').textContent = e.message));
$('channel-form').addEventListener('submit', async event => {
  event.preventDefault(); $('save').disabled = true; $('form-error').textContent = '';
  const id = $('id').value;
  const body = {name:$('name').value.trim(), base_url:$('url').value.trim(), api_key:$('key').value.trim(),
    multiplier:Number($('multiplier').value), scope:$('scope').value.trim(), note:$('note').value.trim(),
    status:$('state').value, enabled:$('enabled').checked, version:Number($('version').value) || null};
  try { await Workbench.api(`/api/registry/channels${id ? `/${id}` : ''}`, {method:id ? 'PUT' : 'POST', body:JSON.stringify(body)});
    $('key').value = ''; $('editor').close(); await refresh(); $('message').textContent = '渠道已保存，三个工具均可使用。';
  } catch (e) { $('form-error').textContent = e.message; } finally { body.api_key = ''; $('save').disabled = false; }
});
$('import').addEventListener('click', async () => {
  const text = $('import-text').value.trim(); if (!text) return;
  $('import').disabled = true;
  try { const result = await Workbench.api('/api/registry/import', {method:'POST', body:JSON.stringify({text})});
    $('import-text').value = ''; $('import-status').textContent = `已记录 ${result.added} 条，跳过重复 ${result.skipped} 条。`; await refresh();
  } catch (e) { $('import-status').textContent = e.message; } finally { $('import').disabled = false; }
});
refresh().catch(e => { $('message').textContent = e.message; });
