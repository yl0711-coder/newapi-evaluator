"""报告导出：Markdown / HTML / JSON。全部基于已脱敏的报告字典，不碰原始 Key。"""
import html
import json
from typing import Any

VERDICT_HINT = {
    "recommend": "可以接入",
    "observe": "可以接入，但需要观察",
    "downgrade": "先降级使用，不要放量",
    "reject": "暂时不要接入",
    "manual": "需要人工确认后再决定",
}


def to_json(rep: dict[str, Any]) -> str:
    return json.dumps(rep, ensure_ascii=False, indent=2)


def _fmt_pct(v: float | None) -> str:
    return "未测到" if v is None else f"{v:.0%}"


def _cap_markdown(rep: dict[str, Any]) -> list[str]:
    """能力评测的维度分表格。没跑评测就返回空。"""
    cap = rep["metrics"].get("capability")
    if not cap or not cap.get("dims"):
        return []
    bench = rep.get("benchmark") or {}
    base_dims = bench.get("dims") or {}
    tol = float(bench.get("tolerance") or 0.10)

    lines = ["", "### 能力评测", "",
             f"题库版本：{cap.get('pack_version')}"
             + (f"　标杆：{bench.get('name')}（容差 {tol:.0%}）" if bench else "　尚未设定标杆"),
             "",
             "| 维度 | 权重 | 得分 | 标杆 | 差值 | 判定 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for dim, v in cap["dims"].items():
        want = base_dims.get(dim)
        score = v["score"]
        if score is None:
            note = "本次未测到"
            gap = "-"
        elif want is None:
            note = "无标杆"
            gap = "-"
        else:
            diff = score - want
            gap = f"{diff:+.0%}"
            note = "达标" if diff >= -tol else "不达标"
        lines.append(
            f"| {dim} | {v['weight']:.0%} | "
            f"{_fmt_pct(score)} | {_fmt_pct(want)} | {gap} | {note} |")

    over = cap.get("overall")
    b_over = bench.get("overall")
    lines += ["", f"综合得分：{_fmt_pct(over)}"
              + (f"　标杆 {_fmt_pct(b_over)}" if b_over is not None else "")]
    return lines


def _hard_markdown(rep: dict[str, Any]) -> list[str]:
    """硬题分。没跑硬题就返回空。

    刻意与能力评测分成两节：硬题不进四维能力总分，也不参与任何门槛，
    合成一张表会让人误读成"综合得分被硬题拉低了"。
    """
    h = rep["metrics"].get("hard")
    if not h or not h.get("total"):
        return []
    bench = (rep.get("benchmark") or {}).get("hard") or {}
    stale = bool((rep.get("benchmark") or {}).get("hard_stale"))

    lines = ["", "### 硬题（HardcoreLogic 无解识别）", "",
             f"题库版本：{h.get('hard_version')}"
             + ("　标杆硬题版本不一致，不做对比" if stale else ""),
             "",
             "独立计分，不进四维能力总分，也不参与可用性成功率、超时门槛与延迟分位。",
             "",
             "| 题库 | 正确率 | 对/已判 | 标杆 |", "| --- | --- | --- | --- |"]
    for key, v in (h.get("banks") or {}).items():
        want = None
        if not stale:
            want = ((bench.get("banks") or {}).get(key) or {}).get("rate")
        lines.append(
            f"| {v.get('name') or key} | {_fmt_pct(v.get('rate'))} | "
            f"{v.get('correct')}/{v.get('graded')} | {_fmt_pct(want)} |")

    lines += ["", f"硬题总正确率：{_fmt_pct(h.get('rate'))}"
              f"（{h.get('correct')}/{h.get('graded')}）"
              + (f"　标杆 {_fmt_pct(bench.get('rate'))}"
                 if bench.get("rate") is not None and not stale else "")]
    if h.get("ungraded"):
        lines.append(f"其中 {h['ungraded']} 道因请求失败未判分，按未测到计，不算答错。")

    wrong = [(k, v) for k, v in (h.get("items") or {}).items()
             if v.get("graded") and not v.get("correct")]
    if wrong:
        lines += ["", "答错的题：", "",
                  "| 题目 | 模型答案 | 正确答案 |", "| --- | --- | --- |"]
        lines += [f"| {k} | {v.get('answer') or '（空）'} | {v.get('expected')} |"
                  for k, v in wrong]
    return lines


def to_markdown(rep: dict[str, Any]) -> str:
    c, m, t = rep["conclusion"], rep["metrics"], rep["target"]
    lines = [
        f"# 测试报告 #{rep['task_id']}｜{rep['pack']['name']} {rep['pack']['version']}",
        "",
        f"## 结论：{c['verdict']}",
        f"下一步：{VERDICT_HINT.get(c['code'], '')}",
        "",
        "### 判断依据",
    ]
    lines += [f"- {r}" for r in c["reasons"]]
    lines += ["", "### 行动建议"]
    lines += [f"{i}. {a}" for i, a in enumerate(c["actions"], 1)]
    lines += [
        "", "### 核心指标", "",
        "| 指标 | 数值 |", "| --- | --- |",
        f"| 可用性成功率 | {m['pass_rate']:.0%}（{m['ok']}/{m['total']}） |",
        f"| P50 / P95 延迟 | {m['p50_latency']}s / {m['p95_latency']}s |",
        f"| 平均首 token | {m['avg_first_token']}s |",
        f"| 断流率 | {m['stream_break_rate']:.0%} |",
        f"| tokens（入/出） | {m['tokens_in']} / {m['tokens_out']} |",
        f"| 预估成本 | ¥{m['cost']:.4f} |",
    ]
    if m.get("capability_total"):
        lines.append(f"| 能力题得分 | {m['capability_score']}/{m['capability_total']} |")
    lines += _cap_markdown(rep)
    lines += _hard_markdown(rep)
    lines += [
        "", "### 测试目标", "",
        f"- 名称：{t['name']}",
        f"- 地址：{t['base_url']}",
        f"- 模型：{t['model']}（协议 {t['protocol']}）",
        f"- Key：{t['key_masked']}",
        "", "### 分项结果", "",
        "| 测试项 | 结果 | 失败原因 | 说明 |", "| --- | --- | --- | --- |",
    ]
    for it in rep["items"]:
        mark = "通过" if it["ok"] else "失败"
        lines.append(f"| {it['step']} | {mark} | {it['reason'] or '-'} | {it['detail']} |")
    return "\n".join(lines) + "\n"


_HTML_CSS = """
body{font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
color:#1f2937;background:#f8fafc;margin:0;padding:32px}
.wrap{max-width:880px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;
border-radius:10px;padding:28px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:24px 0 8px}
.sub{color:#6b7280;font-size:13px;margin-bottom:20px}
.verdict{padding:14px 16px;border-radius:8px;font-size:16px;font-weight:600;
border:1px solid;margin-bottom:6px}
.recommend{background:#f0fdf4;border-color:#bbf7d0;color:#15803d}
.observe{background:#fefce8;border-color:#fef08a;color:#a16207}
.manual{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8}
.downgrade{background:#fff7ed;border-color:#fed7aa;color:#c2410c}
.reject{background:#fef2f2;border-color:#fecaca;color:#b91c1c}
table{width:100%;border-collapse:collapse;margin-top:8px}
th,td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left;font-size:13px}
th{background:#f9fafb;font-weight:600}
ul,ol{margin:8px 0;padding-left:22px}li{margin:3px 0}
.ok{color:#15803d}.bad{color:#b91c1c}
.kv{display:grid;grid-template-columns:120px 1fr;gap:6px 12px;font-size:13px}
.kv dt{color:#6b7280}.kv dd{margin:0}
"""


def _cap_html(rep: dict[str, Any]) -> str:
    """能力评测维度表。没跑评测就返回空串。"""
    cap = rep["metrics"].get("capability")
    if not cap or not cap.get("dims"):
        return ""
    e = html.escape
    bench = rep.get("benchmark") or {}
    base_dims = bench.get("dims") or {}
    tol = float(bench.get("tolerance") or 0.10)

    rows = []
    for dim, v in cap["dims"].items():
        want = base_dims.get(dim)
        score = v["score"]
        if score is None:
            gap, note, cls = "-", "本次未测到", ""
        elif want is None:
            gap, note, cls = "-", "无标杆", ""
        else:
            diff = score - want
            gap = f"{diff:+.0%}"
            ok = diff >= -tol
            note, cls = ("达标", "ok") if ok else ("不达标", "bad")
        rows.append(
            f"<tr><td>{e(dim)}</td>"
            f"<td>{v['weight']:.0%}</td><td>{_fmt_pct(score)}</td>"
            f"<td>{_fmt_pct(want)}</td><td>{gap}</td>"
            f'<td class="{cls}">{note}</td></tr>')

    head = (f"标杆：{e(str(bench.get('name')))}（容差 {tol:.0%}）"
            if bench else "尚未设定标杆，本次结果可存为标杆")
    over = cap.get("overall")
    b_over = bench.get("overall")
    tail = f"综合得分 {_fmt_pct(over)}"
    if b_over is not None:
        tail += f"，标杆 {_fmt_pct(b_over)}"

    return f"""<h2>能力评测</h2>
<div class="sub">题库版本 {e(str(cap.get('pack_version')))}　·　{head}</div>
<table><tr><th>维度</th><th>权重</th><th>得分</th><th>标杆</th>
<th>差值</th><th>判定</th></tr>{''.join(rows)}</table>
<div class="sub" style="margin-top:8px">{tail}</div>"""


def _hard_html(rep: dict[str, Any]) -> str:
    """硬题分表。没跑硬题就返回空串。"""
    h = rep["metrics"].get("hard")
    if not h or not h.get("total"):
        return ""
    e = html.escape
    bench = (rep.get("benchmark") or {}).get("hard") or {}
    stale = bool((rep.get("benchmark") or {}).get("hard_stale"))

    rows = []
    for key, v in (h.get("banks") or {}).items():
        want = None
        if not stale:
            want = ((bench.get("banks") or {}).get(key) or {}).get("rate")
        rows.append(
            f"<tr><td>{e(str(v.get('name') or key))}</td>"
            f"<td>{_fmt_pct(v.get('rate'))}</td>"
            f"<td>{v.get('correct')}/{v.get('graded')}</td>"
            f"<td>{_fmt_pct(want)}</td></tr>")

    wrong = [(k, v) for k, v in (h.get("items") or {}).items()
             if v.get("graded") and not v.get("correct")]
    wrong_tbl = ""
    if wrong:
        wrong_rows = "".join(
            f"<tr><td>{e(str(k))}</td><td>{e(str(v.get('answer') or '（空）'))}</td>"
            f"<td>{e(str(v.get('expected')))}</td></tr>" for k, v in wrong)
        wrong_tbl = ("<div class=\"sub\" style=\"margin-top:10px\">答错的题</div>"
                     "<table><tr><th>题目</th><th>模型答案</th><th>正确答案</th></tr>"
                     f"{wrong_rows}</table>")

    tail = (f"硬题总正确率 {_fmt_pct(h.get('rate'))}"
            f"（{h.get('correct')}/{h.get('graded')}）")
    if bench.get("rate") is not None and not stale:
        tail += f"，标杆 {_fmt_pct(bench.get('rate'))}"
    if h.get("ungraded"):
        tail += f"；{h['ungraded']} 道未判分，按未测到计"

    return f"""<h2>硬题（HardcoreLogic 无解识别）</h2>
<div class="sub">题库版本 {e(str(h.get('hard_version')))}　·
独立计分，不进四维能力总分，也不参与可用性成功率、超时门槛与延迟分位
{'　·　标杆硬题版本不一致，不做对比' if stale else ''}</div>
<table><tr><th>题库</th><th>正确率</th><th>对/已判</th><th>标杆</th></tr>
{''.join(rows)}</table>
<div class="sub" style="margin-top:8px">{tail}</div>{wrong_tbl}"""


def to_html(rep: dict[str, Any]) -> str:
    c, m, t = rep["conclusion"], rep["metrics"], rep["target"]
    e = html.escape

    def rows(items: list[str]) -> str:
        return "".join(f"<li>{e(x)}</li>" for x in items)

    cap = ""
    if m.get("capability_total"):
        cap = (f"<tr><th>能力题得分</th><td>{m['capability_score']}"
               f"/{m['capability_total']}</td></tr>")
    cap_block = _cap_html(rep)
    hard_block = _hard_html(rep)

    items_html = "".join(
        f"<tr><td>{e(it['step'])}</td>"
        f"<td class=\"{'ok' if it['ok'] else 'bad'}\">"
        f"{'通过' if it['ok'] else '失败'}</td>"
        f"<td>{e(it['reason'] or '-')}</td><td>{e(it['detail'])}</td></tr>"
        for it in rep["items"]
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>测试报告 #{rep['task_id']}</title><style>{_HTML_CSS}</style></head>
<body><div class="wrap">
<h1>测试报告 #{rep['task_id']}</h1>
<div class="sub">{e(rep['pack']['name'])} {e(rep['pack']['version'])}
　·　{e(t['name'])}　·　{e(t['model'])}</div>

<div class="verdict {c['code']}">{e(c['verdict'])}</div>
<div class="sub">下一步：{e(VERDICT_HINT.get(c['code'], ''))}</div>

<h2>判断依据</h2><ul>{rows(c['reasons'])}</ul>
<h2>行动建议</h2><ol>{rows(c['actions'])}</ol>

<h2>核心指标</h2>
<table>
<tr><th>可用性成功率</th><td>{m['pass_rate']:.0%}（{m['ok']}/{m['total']}）</td></tr>
<tr><th>P50 / P95 延迟</th><td>{m['p50_latency']}s / {m['p95_latency']}s</td></tr>
<tr><th>平均首 token</th><td>{m['avg_first_token']}s</td></tr>
<tr><th>断流率</th><td>{m['stream_break_rate']:.0%}</td></tr>
<tr><th>tokens（入/出）</th><td>{m['tokens_in']} / {m['tokens_out']}</td></tr>
<tr><th>预估成本</th><td>¥{m['cost']:.4f}</td></tr>{cap}
</table>

{cap_block}

{hard_block}

<h2>测试目标</h2>
<dl class="kv">
<dt>地址</dt><dd>{e(t['base_url'])}</dd>
<dt>模型</dt><dd>{e(t['model'])}</dd>
<dt>协议</dt><dd>{e(t['protocol'])}</dd>
<dt>Key</dt><dd>{e(t['key_masked'])}</dd>
</dl>

<h2>分项结果</h2>
<table><tr><th>测试项</th><th>结果</th><th>失败原因</th><th>说明</th></tr>
{items_html}</table>
</div></body></html>
"""
