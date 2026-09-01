"""Post-admission package catalogs and mock-answer grading self-test."""
import sys

from app import evaluation_packs, grading, packs


failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(("  OK   " if condition else "  FAIL ") + name
          + ("" if condition else f"  <- {detail}"))
    if not condition:
        failures.append(name)


agent = evaluation_packs.agent_items()
development = evaluation_packs.development_items()
check("Agent 题数与切片完整", len(agent) == 15)
check("开发速度固定 3/2/1 负载", [
    sum(item.get("workload") == workload for item in development)
    for workload in ("short", "medium", "long")] == [3, 2, 1])
check("包预估请求数与常驻题库一致",
      packs.PACKS["agent_stability"]["est_requests"] == len(agent)
      and packs.PACKS["development_speed"]["est_requests"] == len(development))
check("正式题只引用注册判分器", all(
    item["grader"]["id"] in grading.registered_graders()
    for item in [*agent, *development]))

grades = []
for items in (agent, development):
    for item in items:
        config = item["grader"]["config"]
        calls = []
        if item["grader"]["id"] == "tool_call_exact":
            calls = [{"name": config["name"], "args": config["arguments"]}]
        grades.append((item["id"], grading.grade(item, {
            "completion_status": "completed", "text": item["mock_answer"],
            "tool_calls": calls, "finish_reason": "stop",
        })))
failed_grades = [(item_id, grade) for item_id, grade in grades
                 if grade["status"] != "passed"]
check("所有标准答案通过自身判分器", not failed_grades, failed_grades)

del agent, development, grades
corpus, facts = evaluation_packs._corpus()
check("长上下文基础语料达到 8K 字符档", len(corpus) >= 8_000, len(corpus))
original_corpus = evaluation_packs._corpus
evaluation_packs._corpus = lambda: ("short test corpus", facts)
long_context = evaluation_packs.long_context_items()
evaluation_packs._corpus = original_corpus
check("长上下文与缓存共用语料", len(long_context) == 10)
check("长上下文预估请求数一致",
      packs.PACKS["long_context"]["est_requests"] == len(long_context))
check("长上下文题只引用注册判分器", all(
    item["grader"]["id"] in grading.registered_graders()
    for item in long_context))

print(f"通过 {not failures}，失败项：{failures if failures else '无'}")
sys.exit(1 if failures else 0)
