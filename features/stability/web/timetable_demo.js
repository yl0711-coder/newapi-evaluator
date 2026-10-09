// Fixed presentation examples only: no fetch, storage, plans or scoring calls.
window.timetableDemoReport = function () {
  const date = "2026-10-08", timezone = "Asia/Shanghai";
  const channels = [
    {id: 901, name: "演示渠道 A", baseline: 190, scores: [186, null, 150, 187, 188, null]},
    {id: 902, name: "演示渠道 B", baseline: 182, scores: [180, null, 179, 181, 92, null]},
    {id: 903, name: "演示渠道 C", baseline: null, scores: [174, null, 178, 172, 175, null]},
  ];
  const times = ["09:00", "09:10", "10:00", "11:00", "12:00", "13:00"];
  const rows = [];
  for (const channel of channels) for (const [index, clock] of times.entries()) {
    const scheduled_at = `${date}T${clock}:00+08:00`, due = Date.parse(scheduled_at) / 1000;
    const waiting = index === 5, missing = channel.id === 902 && index === 4;
    const degraded = channel.id === 901 && index === 2, mismatch = channel.id === 902 && [1, 2].includes(index);
    const score = channel.scores[index], oldMultiplier = channel.id === 901 ? 2 : channel.id === 902 ? 1.5 : 3;
    const multiplier = channel.id === 901 && index >= 3 ? 2.5 : oldMultiplier;
    const canary = score == null ? {label: "未安排", state: "not_scheduled", planned: 0, score: {}} : {
      label: missing ? "未完成 / 未测 · 已尝试 96/192（含 unknown）" : `${score}/192 · ${degraded ? "疑似能力下降" : channel.baseline == null ? "参照不足" : "未检出下降"}`,
      state: missing ? "incomplete" : degraded ? "degraded" : channel.baseline == null ? "reference_missing" : "observed",
      status: missing ? "incomplete" : "completed", planned: 192, attempted: missing ? 96 : 192,
      unknown: missing ? 1 : 0, not_run: missing ? 96 : 0, request_errors: 0,
      baseline_id: channel.baseline == null ? null : `demo-baseline-${channel.id}`,
      score: {correct: score, status: missing ? "incomplete" : degraded ? "degraded" : channel.baseline == null ? "current_only" : "no_detected_degradation",
        ...(channel.baseline != null && !missing ? {comparison: {overall: {baseline_accuracy: channel.baseline / 192, accuracy_loss: (channel.baseline - score) / 192}}} : {})},
    };
    const modeltrace = {label: waiting ? "待采样" : mismatch ? "指纹差异，需复核" : "与 Astra 参考相近（未校准）",
      state: waiting ? "waiting" : mismatch ? "review" : "observed", status: waiting ? "pending" : "completed",
      planned: 3, attempted: waiting ? 0 : 3, unknown: 0, not_run: waiting ? 3 : 0,
      score: {status: waiting ? "not_run" : "scored", source_verdict: mismatch ? "MISMATCH" : "SAME"}};
    const state = missing ? "incomplete" : degraded ? "degraded" : mismatch ? "review" : waiting ? "waiting" : canary.state === "reference_missing" ? "reference_missing" : "observed";
    const labels = {incomplete: "未完成 / 未测", degraded: "疑似能力下降", review: "需复核", reference_missing: "参照不足", observed: "正常观测", waiting: "待采样"};
    const method_multipliers = {modeltrace: multiplier};
    if (score != null) method_multipliers.canary = channel.id === 901 && index === 3 ? 2 : multiplier;
    const start = waiting ? null : due + 4, end = waiting ? null : due + (score == null ? 36 : 420);
    rows.push({run_id: "日计划", date, timezone, scheduled_at, scheduled_at_utc: due,
      registry_channel_id: channel.id, channel_name: channel.name, channel_multiplier: multiplier, method_multipliers,
      sampling_period: waiting ? "未采样" : `${new Date(start * 1000 + 8 * 3600000).toISOString().replace("Z", "+08:00")} – ${new Date(end * 1000 + 8 * 3600000).toISOString().replace("Z", "+08:00")}`,
      sampling_started_at: start, sampling_finished_at: end, canary, modeltrace, state, status: labels[state],
      anomaly: !["observed", "waiting", "sampling"].includes(state), calibration_status: "unvalidated"});
  }
  return {synthetic: true, rows, total: rows.length, displayed: rows.length, counts: {}, empty_plans: [], truncated: false};
};
