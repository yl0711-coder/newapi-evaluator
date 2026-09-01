let incidentState = { incidents: [], sources: [], locations: [], selected: null };
const causeLabels = { upstream_global: '上游整体异常', single_channel: '单个渠道异常', production_network: '生产网络异常', relay_internal: '中转站自身异常', user_quota: '用户级限额', user_safety: '用户安全限制', permission: 'API Key 或模型权限问题', upstream_rate_limit: '上游 429 限流', user_network: '用户侧网络不稳', insufficient: '证据不足' };

function renderList() {
  $('#incident-count').textContent = `${incidentState.incidents.length} 条`;
  $('#incident-list').innerHTML = incidentState.incidents.length ? incidentState.incidents.map((item) => `<article class="incident-row ${item.id === incidentState.selected ? 'on' : ''}" data-incident="${item.id}"><span><strong>${esc(item.channel)}${item.model ? ` · ${esc(item.model)}` : ''}</strong><small>${fmtTime(item.first_seen_at)} · ${item.alert_count} 条告警 · 影响 ${item.affected_users} 个匿名用户</small></span><span class="severity-${item.severity}">${esc(item.attribution.cause_name || '收集证据中')}</span></article>`).join('') : '<div class="empty">尚未收到监测告警。</div>';
}

function renderDetail(item) {
  const attribution = item.attribution || {};
  const support = (attribution.supporting_evidence || []).map((text) => `<li>${esc(text)}</li>`).join('') || '<li>暂无足够支持证据</li>';
  const contradictions = (attribution.contradictions || []).map((text) => `<li>${esc(text)}</li>`).join('') || '<li>暂无明确反证</li>';
  $('#incident-detail').innerHTML = `<div class="section-head"><div><h2>#${item.id} · ${esc(item.channel)}</h2><p>${esc(item.platform_group)} · ${esc(item.model)} · ${esc(item.severity)}</p></div><button id="recollect">重新收集证据</button></div><div class="attribution-summary"><span class="confidence">置信度 ${Math.round((attribution.confidence || 0) * 100)}%</span><strong>${esc(attribution.cause_name || '收集证据中')}</strong><p>${esc(attribution.recommended_action || '等待证据收集完成')}</p><small>${esc(attribution.automation_boundary || '')}</small></div><div class="detail-grid"><section><h3>支持证据</h3><ul>${support}</ul></section><section><h3>反证与缺口</h3><ul>${contradictions}</ul></section></div><h3>证据时间线</h3><div class="evidence-list">${item.evidence.map((row) => `<div class="evidence-item"><span><b>${esc(row.source)}</b> · ${esc(row.evidence_type)}</span><strong>${esc(row.summary)}</strong><small>${fmtTime(row.collected_at)}</small></div>`).join('') || '<div class="empty">证据收集中</div>'}</div>`;
  $('#recollect').onclick = async () => { try { const detail = await post(`/api/incidents/${item.id}/collect`); renderDetail(detail); await load(false); } catch (error) { showError(error.message); } };
}

function renderConfig() {
  $('#source-list').innerHTML = incidentState.sources.map((source) => `<div class="config-row"><span><strong>${esc(source.name)}</strong><small>${source.enabled ? '启用' : '已停用'} · 最近告警 ${fmtTime(source.last_alert_at)}</small></span><code>${esc(window.location.origin + source.webhook_path)}</code><button data-delete-source="${source.id}">${source.enabled ? '停用' : '已归档'}</button></div>`).join('') || '<div class="empty">尚未配置告警源。</div>';
  $('#probe-list').innerHTML = incidentState.locations.map((probe) => `<div class="config-row"><span><strong>${esc(probe.name)}</strong><small>${probe.location_type === 'production' ? '生产位置' : '独立位置'} · 每小时 ${probe.max_requests_per_hour} 次</small></span><code>${esc(probe.endpoint)}</code><button data-delete-location="${probe.id}">停用</button></div>`).join('') || '<div class="empty">尚未配置探针位置；归因置信度会受限。</div>';
}

async function load(keepSelection = true) {
  try {
    const [incidents, sources, locations] = await Promise.all([get('/api/incidents'), get('/api/incidents/sources'), get('/api/incidents/probe-locations')]);
    incidentState = { incidents, sources, locations, selected: keepSelection ? incidentState.selected : null };
    renderList(); renderConfig();
    if (incidentState.selected) renderDetail(await get(`/api/incidents/${incidentState.selected}`));
  } catch (error) { showError(error.message); }
}

$('#incident-list').onclick = async (event) => { const row = event.target.closest('[data-incident]'); if (!row) return; incidentState.selected = Number(row.dataset.incident); renderList(); try { renderDetail(await get(`/api/incidents/${incidentState.selected}`)); } catch (error) { showError(error.message); } };
$('#source-form').onsubmit = async (event) => { event.preventDefault(); try { await post('/api/incidents/sources', { name: $('#source-name').value, secret: $('#source-secret').value, enabled: true }); event.target.reset(); await load(); } catch (error) { showError(error.message); } };
$('#probe-form').onsubmit = async (event) => { event.preventDefault(); try { await post('/api/incidents/probe-locations', { name: $('#probe-name').value, location_type: $('#probe-type').value, endpoint: $('#probe-endpoint').value, token: $('#probe-token').value, enabled: true, max_requests_per_hour: Number($('#probe-limit').value) }); event.target.reset(); await load(); } catch (error) { showError(error.message); } };
$('#source-list').onclick = async (event) => { const button = event.target.closest('[data-delete-source]'); if (!button) return; try { await del(`/api/incidents/sources/${button.dataset.deleteSource}`); await load(); } catch (error) { showError(error.message); } };
$('#probe-list').onclick = async (event) => { const button = event.target.closest('[data-delete-location]'); if (!button) return; try { await del(`/api/incidents/probe-locations/${button.dataset.deleteLocation}`); await load(); } catch (error) { showError(error.message); } };
$('#reload').onclick = () => load();
load();
