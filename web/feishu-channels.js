(() => {
  const element = id => document.getElementById('feishu-' + id);
  let settings = null, busy = false, dirty = false;
  function controls() {
    element('sync').disabled = busy || dirty || !settings?.configured || settings.running;
    element('save').disabled = busy;
    element('refresh').disabled = busy;
    for (const field of element('form').querySelectorAll('input')) field.disabled = busy;
  }
  function show(value) {
    settings = value;
    element('source').hidden = !value.table_url;
    if (value.table_url) element('source').href = value.table_url;
    element('last-success').textContent = value.last_success ? `上次成功同步：${new Date(value.last_success * 1000).toLocaleString()}` : '尚未成功同步。';
    element('issues').replaceChildren();
    const result = value.last_result;
    if (result) {
      for (const issue of result.issues) element('issues').append(Workbench.node('p', `${issue.record_id}：${issue.reason}`));
      if (result.pending > result.issues.length) element('issues').append(Workbench.node('p', `共 ${result.pending} 条待处理，显示前 ${result.issues.length} 条。`));
      if (result.missing) element('issues').append(Workbench.node('p', `${result.missing} 条之前同步的记录本次未读到，已保留原状。`));
    }
    element('status').textContent = value.running ? '同步正在进行，请稍后刷新同步状态。' : value.last_error ? `本次同步失败：${value.last_error}。保留上次结果。` : result ?
      `读取 ${result.total} 条；新增 ${result.added}、更新 ${result.updated}、未变化 ${result.unchanged}、停用 ${result.disabled}、跳过 ${result.skipped}、待补或冲突 ${result.pending}。` :
      value.configured ? '连接设置已保存，点击“手动同步”读取表格。' : '请先填写飞书连接设置。';
    controls();
  }
  async function load(fill = false) {
    const value = await Workbench.api('/api/registry/feishu/settings');
    if (fill) {
      element('url').value = value.table_url;
      element('app-id').value = value.app_id;
      element('config').open = !value.configured;
    }
    show(value);
  }
  element('form').addEventListener('input', () => { dirty = true; controls(); element('status').textContent = '连接设置已修改，请保存后同步。'; });
  element('form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !settings) return;
    const body = {app_id:element('app-id').value.trim(), app_secret:element('secret').value,
      table_url:element('url').value.trim(), version:settings.version};
    element('secret').value = ''; busy = true; controls();
    try {
      const value = await Workbench.api('/api/registry/feishu/settings', {method:'PUT', body:JSON.stringify(body)});
      dirty = false; show(value); element('url').value = value.table_url; element('config').open = false;
      element('status').textContent = '连接设置已保存，点击“手动同步”读取表格。';
    } catch (error) { element('status').textContent = error.message; }
    finally { body.app_secret = ''; busy = false; controls(); }
  });
  element('sync').addEventListener('click', async () => {
    if (busy || dirty || !settings?.configured || settings.running) return;
    busy = true; controls(); element('status').textContent = '正在读取飞书表格并同步渠道…';
    try {
      await Workbench.api('/api/registry/feishu/sync', {method:'POST', body:JSON.stringify({confirm_read:true})});
      window.dispatchEvent(new Event('registry-channels-updated'));
      await load();
    } catch (error) { element('status').textContent = error.message; }
    finally { busy = false; controls(); }
  });
  element('refresh').addEventListener('click', async () => {
    busy = true; controls();
    try { await load(); } catch (error) { element('status').textContent = error.message; }
    finally { busy = false; controls(); }
  });
  window.addEventListener('pagehide', () => { element('secret').value = ''; });
  load(true).catch(error => { element('status').textContent = error.message; });
})();
