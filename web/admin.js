let profiles = [];

const profileName = (code) => (profiles.find((item) => item.code === code) || {}).name || code;

function renderProfiles(bindings) {
  const options = profiles.map((item) => `<option value="${item.code}">${esc(item.name)}</option>`).join('');
  $('#primary-profile').innerHTML = options;
  $('#secondary-profiles').innerHTML = options;
  $('#usage-list').innerHTML = bindings.length ? `<table><thead><tr><th>对象</th><th>匿名指纹</th><th>主用途</th><th>次用途</th><th>备注</th><th></th></tr></thead><tbody>${bindings.map((row) => `<tr><td>${esc(row.subject_label || row.subject_type)}<br><small>${esc(row.subject_type)}</small></td><td><code>${esc(row.subject_hash.slice(0, 15))}…</code></td><td>${esc(profileName(row.primary_profile))}</td><td>${row.secondary_profiles.map(profileName).map(esc).join('、') || '-'}</td><td>${esc(row.note || '-')}</td><td><button data-delete-binding="${row.id}">删除</button></td></tr>`).join('')}</tbody></table>` : '<div class="empty">尚无用途绑定，未标注请求统一按未知/通用处理。</div>';
}

function renderPolicy(policy) {
  $('#egress-policy').innerHTML = `<p>${esc(policy.default)}</p><div class="metrics"><div class="metric"><div class="k">重定向上限</div><div class="v">${policy.max_redirects}</div></div><div class="metric"><div class="k">响应上限</div><div class="v">${(policy.max_response_bytes / 1048576).toFixed(0)} MiB</div></div></div><p class="help">部署白名单：${policy.deployment_allowlist.map(esc).join('、') || '无'}。${esc(policy.management)}</p>`;
}

function renderBackup(status) {
  const runs = status.latest_runs || [];
  $('#backup-status').innerHTML = `<div class="metrics" style="margin-top:12px">${status.databases.map((row) => `<div class="metric"><div class="k">${esc(row.database)} SQLite</div><div class="v">${(row.bytes / 1048576).toFixed(1)} MiB</div><small>${row.migration_required ? '已达到 PostgreSQL 迁移阈值' : `阈值 ${(row.threshold_bytes / 1073741824).toFixed(1)} GiB`}</small></div>`).join('')}</div><p class="help">备份目录：${esc(status.backup_dir)} · ${status.offsite_configured ? '已配置服务器外路径' : '当前是本机路径，生产部署需外挂私有目录'}</p>${runs.length ? `<table><thead><tr><th>类型</th><th>状态</th><th>完成时间</th><th>文件</th></tr></thead><tbody>${runs.map((row) => `<tr><td>${esc(row.kind)}</td><td>${esc(row.status)}</td><td>${fmtTime(row.finished_at)}</td><td><small>${esc(row.backup_path || '-')}</small></td></tr>`).join('')}</tbody></table>` : '<div class="empty">尚无备份记录。</div>'}`;
}

function renderAlerts(rows) {
  $('#alert-list').innerHTML = rows.length ? rows.map((row) => `<div class="card between" style="margin-top:10px"><span><b>${esc(row.title)}</b><small class="muted">${esc(row.kind)} · ${fmtTime(row.last_seen_at)} · ${row.occurrence_count} 次</small><small>${esc(row.detail)}</small></span><button data-resolve-alert="${row.id}">确认并关闭</button></div>`).join('') : '<div class="empty">没有未处理的后台告警。</div>';
}

function renderAudit(rows) {
  $('#audit-list').innerHTML = rows.length ? `<table><thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>结果</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${fmtTime(row.created_at)}</td><td>${esc(row.actor)}</td><td>${esc(row.action)}</td><td>${esc(row.object_type)} ${esc(row.object_id)}</td><td>${esc(row.result)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">没有匹配的审计记录。</div>';
}

async function loadAudit() {
  const params = new URLSearchParams({ limit: '200' });
  if ($('#audit-actor').value.trim()) params.set('actor', $('#audit-actor').value.trim());
  if ($('#audit-action').value.trim()) params.set('action', $('#audit-action').value.trim());
  if ($('#audit-object').value.trim()) params.set('object_type', $('#audit-object').value.trim());
  if ($('#audit-result').value) params.set('result', $('#audit-result').value);
  renderAudit(await get(`/api/audit-events?${params}`));
}

async function load() {
  clearError();
  try {
    const [loadedProfiles, bindings, policy, alerts, backup] = await Promise.all([get('/api/insights/usage-profiles'), get('/api/insights/usage-bindings'), get('/api/security/egress-policy'), get('/api/system-alerts'), get('/api/backups/status')]);
    profiles = loadedProfiles; renderProfiles(bindings); renderPolicy(policy); renderAlerts(alerts); renderBackup(backup); await loadAudit();
  } catch (error) { showError(error.message); }
}

$('#usage-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await post('/api/insights/usage-bindings', { subject_type: $('#subject-type').value, subject_id: $('#subject-id').value, subject_label: $('#subject-label').value.trim(), primary_profile: $('#primary-profile').value, secondary_profiles: [...$('#secondary-profiles').selectedOptions].map((item) => item.value), note: $('#usage-note').value.trim() });
    event.target.reset(); await load();
  } catch (error) { showError(error.message); }
});
$('#usage-list').addEventListener('click', async (event) => { const button = event.target.closest('[data-delete-binding]'); if (!button) return; try { await del(`/api/insights/usage-bindings/${button.dataset.deleteBinding}`); await load(); } catch (error) { showError(error.message); } });
$('#alert-list').addEventListener('click', async (event) => { const button = event.target.closest('[data-resolve-alert]'); if (!button) return; try { await post(`/api/system-alerts/${button.dataset.resolveAlert}/resolve`); await load(); } catch (error) { showError(error.message); } });
$('#refresh-alerts').addEventListener('click', load);
$('#run-backup').addEventListener('click', async () => { try { await post('/api/backups/run'); await load(); } catch (error) { showError(error.message); } });
$('#run-restore-drill').addEventListener('click', async () => { try { await post('/api/backups/restore-drill'); await load(); } catch (error) { showError(error.message); } });
$('#audit-filter').addEventListener('submit', async (event) => { event.preventDefault(); try { await loadAudit(); } catch (error) { showError(error.message); } });
load();
