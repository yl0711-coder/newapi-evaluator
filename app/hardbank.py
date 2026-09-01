"""T62 HardcoreLogic 无解识别题库。

题面与答案保存在 hard_items.json。这套分数独立于四维能力分，
HARD_VERSION 按题面与答案生成；内容变化后必须重新建立硬题标杆。
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .config import HARD_TOKEN_FLOOR

_DATA_PATH = Path(__file__).resolve().parent / "hard_items.json"

# 展示用元信息。题目本身在 JSON 里，这里只放呈现层的名字与说明，改文案不影响版本号。
BANK_META: dict[str, dict[str, str]] = {
    "hardcore_unsolvable": {
        "name": "HardcoreLogic 无解识别",
        "desc": "寻路、密码算式、汉诺塔和二进制四道无解题；严格核对 solvable=false 与 solution=null。",
    },
}


def _load() -> list[dict[str, Any]]:
    if not _DATA_PATH.exists():
        raise FileNotFoundError(
            f"缺少 {_DATA_PATH.name}，先在 test/ 目录下跑："
            f"node scripts/convert_hard_banks.mjs")
    raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    items = raw.get("items") or []
    if not items:
        raise ValueError(f"{_DATA_PATH.name} 里没有题目")
    return items


def _apply_token_floor(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 config.HARD_TOKEN_FLOOR 盖在题库自带的 maxTokens 上，取较大值。

    为什么需要这一层：**思考 token 算在 max_tokens 里**。题库里的 8192 / 4096
    是按「普通模型简短推理几句就给答案」定的，推理模型光 thinking 就能烧穿，
    正文被截成空的 → 抽不到答案 → 记 0 分。而这在报告里跟「真的答错」一模一样，
    从分数上根本看不出来，等于系统性压低所有推理模型。

    不直接改 hard_items.json：预算是「怎么问」而不是「问什么」，属于平台策略，
    不应改变冻结的题面与答案资产。
    也因此 HARD_VERSION 不含预算，调预算不会让已有硬题标杆失效。
    """
    for it in items:
        floor = HARD_TOKEN_FLOOR.get(it["bank"], 0)
        it["max_tokens_bank"] = int(it["max_tokens"])      # 题库原值，留个证据
        it["max_tokens"] = max(int(it["max_tokens"]), floor)
    return items


_ITEMS: list[dict[str, Any]] = _apply_token_floor(_load())


def _digest() -> str:
    """按题面 + 答案算内容哈希。题一变版本就变，旧硬题标杆自动失效。"""
    h = hashlib.sha256()
    for it in _ITEMS:
        h.update(it["id"].encode())
        h.update(it["prompt"].encode())
        h.update("".join(it["expected"]).encode())
    return h.hexdigest()[:8]


HARD_VERSION = f"hard-{_digest()}"


# ---------------- 答案抽取与归一化 ----------------

_SOLUTION_RE = re.compile(r"<solution>(.*?)</solution>", re.S | re.I)

# 上标数字 → 普通数字，模型偶尔用 10⁻³ 这种写法
_SUPERSCRIPT = str.maketrans({
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
    "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁻": "-", "⁺": "+",
})
_SUPER_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+")

# HLE 镜像里的标准答案本身就是 LaTeX 写的（见 hle-physics-16 的 expected），
# 所以两边都要过一遍这套清洗，否则纯文本作答的模型会被判错。
# LaTeX 运算符：必须换成乘号而不是删掉。删掉的话 "1.776 \times 10^{-3}"
# 会变成 "1.77610^-3"，与标准答案对不上 —— 答对的模型被判错。
_LATEX_OPS = {
    r"\times": "*", r"\cdot": "*", r"\ast": "*", r"\div": "/",
}

# 纯装饰，直接删
_LATEX_JUNK = (
    r"\left", r"\right",        # 必须先去这两个，否则 \left( 里的括号被当成 \(
    r"\(", r"\)", r"\[", r"\]",
    r"\,", r"\;", r"\:", r"\!", r"\ ",
    r"\mathrm", r"\text", r"\displaystyle",
)


def _strip_latex(s: str) -> str:
    for op, rep in _LATEX_OPS.items():
        s = s.replace(op, rep)
    for junk in _LATEX_JUNK:
        s = s.replace(junk, "")
    return s.replace("$", "")


def _fix_superscripts(s: str) -> str:
    """上标里隐含一个 ^：10⁻³ 应当变成 10^-3。

    只翻译字符会得到 10-3，那是减法，与 1.776 * 10^-3 对不上，
    答对的模型会被判错。所以补一个 ^ 再翻译。
    """
    s = _SUPER_RE.sub(lambda m: "^" + m.group(0).translate(_SUPERSCRIPT), s)
    return s.replace("^^", "^")


def extract_answer(text: str) -> str:
    """优先取最后一个 <solution> 块；没有就退化成最后一行非空文本。

    取**最后一个**而不是第一个：模型有时会先举例说明格式（"放在 <solution></solution> 里"），
    真答案在末尾。题面也明确要求 solution 块是最后输出的内容。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    blocks = _SOLUTION_RE.findall(raw)
    if blocks:
        return blocks[-1].strip()
    # 没有 solution 块：模型没遵守格式。取最后一行非空文本兜底，
    # 答对了但没套标签仍然算对 —— 判的是能力，不是格式服从度。
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def normalize(ans: str) -> str:
    """归一化：抹掉不影响正确性的写法差异，但**不做任何等价数学换算**。

    顺序有讲究，不能随手调，每一条都是被实际写法坑出来的：
      - markdown 强调要在乘号之前（`**` 是强调、单个 `*` 是乘号，不能一起吃掉）
      - 指数花括号要在去包裹符号之前（否则 `10^{-3}` 的尾括号被当成包裹符号剥掉，
        剩下 `10^{-3` 再也匹配不上）
      - 答案前缀要在去包裹符号之前（否则 `Answer: (B)` 会剩下 `(b`）
      - 科学计数法要在去空格之后（否则 `1.776e-3` 与 `1.776 * 10^-3` 归不到一处）
    """
    s = (ans or "").strip()
    if not s:
        return ""
    # 1. markdown：只吃成对的 ** / __，单个 * 是乘号，必须留着
    s = re.sub(r"\*\*+", "", s)
    s = re.sub(r"__+", "", s)
    s = s.replace("`", "")
    # 2. LaTeX：\times 之类换成乘号，\(...\) 之类直接删
    s = _strip_latex(s)
    # 3. unicode 写法统一（上标要补 ^，见 _fix_superscripts）
    s = _fix_superscripts(s)
    s = (s.replace("×", "*").replace("·", "*")
          .replace("−", "-").replace("–", "-").replace("—", "-")
          .replace("\xa0", " "))
    # 4. 指数花括号：^{2} → ^2。必须早于第 6 步
    s = re.sub(r"\^\{([^}]*)\}", r"^\1", s)
    # 5. 答案前缀：'答案：B' / 'Answer: B'
    s = re.sub(r"^(最终答案|答案|answer|final answer)\s*[:：是]?\s*", "", s, flags=re.I)
    # 6. 包裹符号：'(B)' / 'B)' / '「B」'
    s = s.strip().strip("()[]{}<>「」『』\"'“”‘’").strip()
    # 7. 千分位：只在纯数字上去逗号，别动正常含逗号的文本答案
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    # 8. 尾部标点与大小写
    s = s.rstrip(".。，,;；!！ ").lower()
    # 9. 短答案里空格不承载信息（HLE 短答上限 24 字符，编程硬核都是整数），
    #    去掉后 'd - (d-2k)^2' 与 'd - (d - 2k)^2' 才能归一到一处
    s = re.sub(r"\s+", "", s)
    # 10. 科学计数法统一成 mantissa*10^exp
    s = re.sub(r"(\d)e([+-]?\d+)$", r"\1*10^\2", s)
    return s.replace("x10^", "*10^").replace("10^+", "10^")


def score_exact(item: dict[str, Any], text: str) -> float:
    """1.0 或 0.0。expected 是数组时任一命中即算对（镜像原答案在首位）。"""
    got = normalize(extract_answer(text))
    if not got:
        return 0.0
    for want in item["expected"]:
        if got == normalize(want):
            return 1.0
    return 0.0


def score(item: dict[str, Any], text: str) -> float:
    if item.get("scorer") == "exact":
        return score_exact(item, text)
    if item.get("scorer") == "structured":
        raw = extract_answer(text)
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return 0.0
        try:
            actual = json.loads(raw[start:end + 1])
            expected = json.loads(item["expected"][0])
        except (ValueError, TypeError):
            return 0.0
        return 1.0 if actual == expected else 0.0
    raise ValueError(f"未知判分器 {item.get('scorer')!r}（题 {item['id']}）")


# ---------------- 查询接口 ----------------

def all_items(banks: list[str] | None = None) -> list[dict[str, Any]]:
    """按 JSON 里的顺序返回题目。banks 给定时只返回那几套。"""
    if not banks:
        return list(_ITEMS)
    want = set(banks)
    return [it for it in _ITEMS if it["bank"] in want]


def banks() -> list[str]:
    """出现过的题库，保持 JSON 里的首次出现顺序。"""
    out: list[str] = []
    for it in _ITEMS:
        if it["bank"] not in out:
            out.append(it["bank"])
    return out


def bank_name(bank: str) -> str:
    return BANK_META.get(bank, {}).get("name", bank)


def get(item_id: str) -> dict[str, Any] | None:
    return next((it for it in _ITEMS if it["id"] == item_id), None)


def estimate_tokens(banks: list[str] | None = None) -> int:
    """预估 token 上界。

    按 max_tokens 满打满算，实际通常远低于这个数 —— 模型推理几句就给答案，
    不会把 8192 用满。宁可预估偏高，也不要让用户被账单意外。
    """
    items = all_items(banks)
    return sum(int(it["max_tokens"]) + 220 for it in items)


def count(banks: list[str] | None = None) -> int:
    return len(all_items(banks))
