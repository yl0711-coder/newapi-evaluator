"""规则自测：降智判定、防作假、错误归类、脱敏。直接调建议引擎，不依赖网络。

用法：python selftest_rules.py
"""
import sys

from app import advice, itembank, packs, probes, report, scoring


def step(name, ok=True, reason="", extra=None, latency=1.0, usage=(10, 20)):
    return probes.result(name, ok, reason=reason, latency=latency,
                         usage={"prompt": usage[0], "completion": usage[1]},
                         extra=extra or {})


def cap(name, scored):
    return step(f"能力·{name}", scored == 1,
                reason="" if scored else probes.MISMATCH,
                extra={"scored": scored, "probe_name": name})


def run(title, kind, steps, baseline=None):
    pack = packs.get_pack(kind)
    m = report.compute_metrics(steps, 2.0, 8.0)
    m["gates"] = scoring.gate_results(
        steps, m, pack.get("required"), method_gate=bool(pack.get("method_ids")))
    c = advice.evaluate(kind, pack, steps, m, baseline)
    print(f"\n--- {title} ---")
    print("  结论:", c["verdict"])
    for r in c["reasons"]:
        print("   · 依据:", r)
    for a in c["actions"]:
        print("   · 建议:", a)
    return c


fails = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        fails.append(name)


def ev(dim, scored, level=2):
    """构造一次传输成功且已形成确定性判分的能力证据。"""
    status = "passed" if scored >= 0.999 else "partial" if scored > 0 else "failed"
    return step(f"{dim}·x", extra={
        "dim": dim,
        "item": f"{dim}.x",
        "scored": scored,
        "graded": True,
        "level": level,
        "score_domain": "ability",
        "grade": {"status": status, "score": scored, "failure_codes": []},
        "eligibility": {"ability": "eligible", "stability": "eligible"},
    })


def ev4(scores):
    """四类能力各构造一条确定性证据。"""
    if isinstance(scores, dict):
        return [ev(d, scores.get(d, 1.0)) for d in itembank.ABILITY_DIM_ORDER]
    return [ev(d, scores) for d in itembank.ABILITY_DIM_ORDER]


def baseline(overall, dims=None, version=None):
    """纵向基线：四类能力分与综合分。"""
    return {
        "pack_version": version or itembank.PACK_VERSION,
        "dims": dims or {d: overall for d in itembank.DIM_ORDER},
        "overall": overall, "p95_latency": 1.0,
    }


BASE_FULL = baseline(1.0)

print("=" * 50)
print("纵向降智判定（跟自己的历史基线比）")

# 综合 100% → 20%，跌 80%，应判确认降智
c = run("基线 100%，本次 20%", "degrade",
        [step("连通确认")] + ev4(0.2), BASE_FULL)
check("大幅下降判建议降级", c["code"] == "downgrade", c["code"])
check("依据里点明确认降智", any("确认降智" in r for r in c["reasons"]), c["reasons"])

# 四类能力均降至 71%，下降未达到 30%，应判疑似波动。
c = run("基线 100%，内容能力本次 71%", "degrade",
        [step("连通确认")] + ev4(0.71), BASE_FULL)
check("小幅下降判建议观察", c["code"] == "observe", c["code"])
check("依据里说疑似波动", any("疑似波动" in r for r in c["reasons"]), c["reasons"])
check("建议再复核一次", any("再跑一次" in a or "复核" in a for a in c["actions"]), c["actions"])

# 偏科下降：只有代码维度掉，依据要点名是哪个维度
c = run("只有代码能力掉", "degrade",
        [step("连通确认")] + ev4({"代码": 0.2}),
        BASE_FULL)
check("点名掉得最多的能力", any("代码掉得最多" in r for r in c["reasons"]),
      c["reasons"])

# 题库版本不同 → 拒绝比分
c = run("基线题库版本不同", "degrade",
        [step("连通确认")] + ev4(1.0), baseline(1.0, version="cap-v0"))
check("版本不同要人工复核", c["code"] == "manual", c["code"])
check("依据说明版本不同", any("版本不同" in r for r in c["reasons"]), c["reasons"])

# 没有基线 → 说明本次将作为基线
c = run("没有历史基线", "degrade", [step("连通确认")] + ev4(1.0), None)
check("无基线时说清楚", any("暂无历史基线" in r for r in c["reasons"]), c["reasons"])

# 100% → 100%，未降智
c = run("基线 100%，本次 100%", "degrade",
        [step("连通确认")] + ev4(1.0), BASE_FULL)
check("持平不判降智", c["code"] == "recommend", c["code"])
check("依据说未见降智", any("未见降智" in r for r in c["reasons"]), c["reasons"])

print("\n" + "=" * 50)
print("防作假")

# 静默兜底：请求不存在的模型却返回成功
c = run("请求假模型仍成功", "admission",
        [step("鉴权与模型清单"), step("基础问答"),
         step("错误处理", False, probes.MISMATCH, {"silent_fallback": True})])
check("静默兜底判暂不准入", c["code"] == "reject", c["code"])
check("依据点出疑似作假", any("作假" in r for r in c["reasons"]), c["reasons"])

# 换模型：上游返回的 model 与申报不一致
c = run("上游返回别的模型名", "admission",
        [step("鉴权与模型清单"),
         step("基础问答", extra={"model_mismatch": True, "actual_model": "gpt-3.5-turbo"})])
check("模型不一致要人工复核", c["code"] == "manual", c["code"])
check("依据点出疑似换模型", any("换模型" in r for r in c["reasons"]), c["reasons"])

# usage 缺失 → 成本核算不完整
c = run("响应缺 usage 字段", "admission",
        [step("鉴权与模型清单"), step("基础问答", extra={"usage_complete": False})])
check("缺 usage 判建议观察", c["code"] == "observe", c["code"])
check("依据点出成本核算不完整",
      any("成本核算" in r for r in c["reasons"]), c["reasons"])

print("\n" + "=" * 50)
print("掉线与稳定性")

# 断流
c = run("流式断开", "admission",
        [step("鉴权与模型清单"), step("基础问答"),
         step("流式输出", False, probes.PROTO, {"stream_break": True})])
check("断流判建议降级", c["code"] == "downgrade", c["code"])
check("建议查 SSE", any("SSE" in a for a in c["actions"]), c["actions"])

# 必过项失败
c = run("鉴权失败", "admission",
        [step("QA-PROTO-01 鉴权与模型可达性", False, probes.AUTH)])
check("必过项失败判暂不准入", c["code"] == "reject", c["code"])
check("依据点明必过项", any("必过项" in r for r in c["reasons"]), c["reasons"])

# 基础测试的速度只做固定题横向比较，不设绝对淘汰线
c = run("P95 延迟 25s", "admission",
        [step("鉴权与模型清单", latency=25.0), step("基础问答", latency=25.0)])
check("基础测试不按绝对延迟淘汰", c["code"] != "downgrade", c["code"])

# 限流
c = run("出现限流", "inspect",
        [step("连通抽检"), step("流式输出", False, probes.LIMIT)])
check("限流建议降并发", any("并发" in a for a in c["actions"]), c["actions"])

print("\n" + "=" * 50)
print("错误归类")
for status, want in ((401, probes.AUTH), (403, probes.AUTH), (404, probes.CFG),
                     (429, probes.LIMIT), (504, probes.TIMEOUT), (502, probes.UPSTREAM)):
    got = probes.classify(status, "")
    check(f"HTTP {status} → {want}", got == want, got)
check("模型不存在文案 → 配置",
      probes.classify(400, "The model does not exist") == probes.CFG)
check("HTTP 200 正文里的鉴权错误仍能识别",
      probes.embedded_error("ANTHROPIC_AUTH_TOKEN 格式不正确")[0] == probes.AUTH)
check("HTTP 200 正文里的配置错误仍能识别",
      probes.embedded_error("missing api key") == (probes.CFG, "missing api key"))
check("正常回答不误判成内嵌错误", probes.embedded_error("答案是北京") is None)

print("\n" + "=" * 50)
print("上游注入检测（我们只发 user 消息，正文里不该有自我介绍）")

INJECTED = ("Acknowledged. I'm Claude Code, Anthropic's official CLI for "
            "software engineering tasks.")

check("识别出 Claude Code 自我介绍", probes.detect_injection(INJECTED) != "")
check("识别出 official CLI",
      probes.detect_injection("I am Claude Code, the official CLI") != "")
check("识别出中文自我介绍",
      probes.detect_injection("我是一个由某公司训练的大语言模型") != "")
check("正常回答不误报", probes.detect_injection("北京") == "")
check("代码回答不误报",
      probes.detect_injection("def add(a, b):\n    return a + b") == "")
check("提到 system prompt 的正常技术回答不误报",
      probes.detect_injection("system prompt 是指系统提示词") == "")
check("空回答不炸", probes.detect_injection("") == "")
check("只看开头，正文深处的巧合不误报",
      probes.detect_injection("答案是 391。" + "填充" * 200 + "official cli") == "")


def ev_reply(dim, text, scored=0.0, injected=""):
    """构造带可审查正文的能力证据。"""
    evidence = ev(dim, scored)
    evidence["extra"].update({
        "reply": text,
        "injected": injected,
    })
    return evidence


def ev_inj(dim, text, scored=0.0):
    """构造回答被注入污染的能力证据。"""
    return ev_reply(dim, text, scored, probes.detect_injection(text))


# 多道题都被注入 → 整轮不可信
c = run("多道题回答被注入污染", "capability",
        [step("连通确认")] + [ev_inj(d, INJECTED) for d in itembank.DIM_ORDER])
check("判需要人工复核", c["code"] == "manual", c["code"])
check("依据点明本轮评分不可信",
      any("本轮评分不可信" in r for r in c["reasons"]), c["reasons"])
check("建议提到 system prompt 注入",
      any("system prompt" in a for a in c["actions"]), c["actions"])

# 不同题拿到同一份回答 → 也不可信
SAME = "这是一段足够长的重复回答，用来触发重复检测规则，长度超过二十个字符。"
c = run("不同题拿到相同回答", "capability",
        [step("连通确认")]
        + [ev_reply(d, SAME) for d in itembank.DIM_ORDER])
check("重复回答也判不可信",
      any("几乎相同的回答" in r for r in c["reasons"]), c["reasons"])

# 正常一轮不该被判不可信
c = run("正常一轮", "capability", [step("连通确认")] + ev4(1.0))
check("正常一轮不误判",
      not any("不可信" in r for r in c["reasons"]), c["reasons"])

# 短回答重复不算异常（"北京"这类本来就会重复）
short_steps = [ev_reply(d, "北京", 1.0) for d in itembank.DIM_ORDER]
c = run("短回答重复", "capability", [step("连通确认")] + short_steps)
check("短回答重复不误报",
      not any("不可信" in r for r in c["reasons"]), c["reasons"])

print("\n" + "=" * 50)
print("错误处理：明确拒绝就算过，不挑状态码")

check("4xx 算通过", probes.classify(404, "") == probes.CFG)
# 503 的判定在 probe_error_handling 里，这里验归类不会把它当成配置问题
check("503 归为上游错误", probes.classify(503, "") == probes.UPSTREAM)

print("\n" + "=" * 50)
print("部分得分的时间线文案")
from app import runner as _runner  # noqa: E402

partial = ev("指令保持", 0.75)
zero = ev("代码", 0.0)
passed = ev("推理", 1.0)
check("0.75 分写成「内容部分通过」", "内容部分通过" in _runner._event_text(partial),
      _runner._event_text(partial))
check("0.75 分不写成「失败」", "失败" not in _runner._event_text(partial))
check("0 分写成「内容未通过」", "内容未通过" in _runner._event_text(zero),
      _runner._event_text(zero))
check("满分写成「通过」", "通过" in _runner._event_text(passed))

print("\n" + "=" * 50)
print("脱敏")
from app.security import mask, scrub  # noqa: E402
check("长 Key 脱敏", mask("sk-abcdefgh12345678") == "sk-abc***5678",
      mask("sk-abcdefgh12345678"))
check("短 Key 也不全显", "***" in mask("sk-123"))
check("错误信息里抹掉 Key",
      "sk-secret-value-999" not in scrub("failed with sk-secret-value-999",
                                        "sk-secret-value-999"))

print("\n" + "=" * 50)
print(f"通过 {len(fails) == 0}，失败项：{fails if fails else '无'}")
sys.exit(1 if fails else 0)
