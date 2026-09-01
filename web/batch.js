const taskIds = (new URLSearchParams(location.search).get('tasks') || '')
  .split(',').map(Number).filter(Number.isInteger);

async function load() {
  if (!taskIds.length) {
    $('#summary').textContent = '没有指定任务。';
    return;
  }
  try {
    const tasks = await Promise.all(taskIds.map((id) => get(`/api/tasks/${id}`)));
    const active = tasks.filter((task) => ['queued', 'running'].includes(task.status));
    $('#summary').textContent = active.length
      ? `${tasks.length} 个模型已提交，${active.length} 个仍在排队或执行。页面会自动刷新。`
      : `${tasks.length} 个模型均已完成。`;
    $('#tasks').innerHTML = `<table><thead><tr><th>模型</th><th>任务</th><th>状态</th><th>智力题</th><th>稳定率</th><th>首字 P50 / P95</th><th>总耗时 P50 / P95</th><th>结论</th></tr></thead><tbody>${tasks.map((task) => {
      const progress = task.progress || {};
      const metrics = (task.report || {}).metrics || {};
      const admission = metrics.admission || {};
      const speed = metrics.fixed_speed || {};
      return `<tr><td>${esc((task.snapshot || {}).model || task.target_name)}</td>
        <td><a href="task.html?id=${task.id}">#${task.id}</a></td>
        <td>${statusTag(task.status)}</td>
        <td>${admission.passed_items != null ? `${admission.passed_items}/${admission.expected_items}` : (admission.valid_items != null ? `${admission.valid_items}/${admission.expected_items} 有效` : (progress.total ? `${progress.done || 0}/${progress.total}` : '-'))}</td>
        <td>${admission.stability_rate == null ? '-' : `${(admission.stability_rate * 100).toFixed(0)}%`}</td>
        <td>${speed.p50_ttft == null ? '-' : `${speed.p50_ttft}s / ${speed.p95_ttft}s`}</td>
        <td>${speed.p50_latency == null ? '-' : `${speed.p50_latency}s / ${speed.p95_latency}s`}</td>
        <td>${esc(task.verdict || '-')}</td></tr>`;
    }).join('')}</tbody></table>`;
    if (active.length) setTimeout(load, 2500);
  } catch (error) { showError(error.message); }
}

load();
