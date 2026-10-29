"""规则引擎：把知识库规则里的硬约束编译成可执行的判定。

为什么需要它
------------
`eval/decidability_report.md` 算出：120 个判定点里 **90 个（75%）的判定步骤
可以用代码执行**，而**当前所有错误都落在这 75% 里**。
`eval/query_phrasing_report.md` 进一步查明这些错误的共同点：

    规则已被召回、约束已被读到，但判定器不采用它。

典型证据：E013「内容区最大宽度 1440px」的模型理由里**明确读出了
`max_content_width: 1200`**，却结论"知识库缺少适用规范"。

既然约束本身是确定性的，就没有理由让模型来决定"要不要采用它"。

职责边界（必须说清）
--------------------
    抽取：自然语言文档 → {"attribute": "内边距", "claim": {"value": 32}}
          **仍由 LLM 完成**，本模块不解决抽取
    判定：claim → 合规/违规 + 命中的字段与期望值
          **本模块负责，纯函数、无网络、确定性**
    表达：把判定结果写成理由与建议
          **交给 LLM**

支持的约束类型
--------------
    allowed           取值白名单
    step              取值必须是某数的整数倍
    min / min_touch_target   下限
    max / max_content_width  上限
    range             区间
    multiplier        倍数（行高 = 字号 × 1.5）
    constraint        关系式（between > inside）
    pattern           正则
    required          必须声明的项

用法
----
    engine = RuleEngine()
    r = engine.evaluate("SPACING-001", {"value": 32})
    r.verdict   # 'fail'
    r.detail    # '32 不在允许取值 [12, 16, 24] 内'
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

KB_DIR = Path(__file__).resolve().parent.parent / "kb"


# ======================================================================
# 数据结构
# ======================================================================
@dataclass
class Rule:
    id: str
    title: str
    statement: str
    params: dict
    exceptions: list[str] = field(default_factory=list)
    applies_when: list[str] = field(default_factory=list)


@dataclass
class Result:
    verdict: str          # pass / fail / uncertain
    rule_id: str
    detail: str
    field_name: str = ""
    expected: str = ""
    actual: str = ""


# ======================================================================
# 最小 YAML 解析：只解析 kb/ 里实际出现的形状
# ======================================================================
def _scalar(v: str):
    raw = v.strip()
    # YAML 双引号字符串里的 \\ 会反转义成单个 \。
    # 不处理这个会导致正则错位：kb 里写 "(\/[A-Za-z0-9]+)" 读出来会变成
    # 要求「反斜杠 + 斜杠」，于是合规的 Button/Primary 被判不合规。
    quoted = len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"'
    v = raw.strip('"').strip("'")
    if quoted:
        v = v.replace("\\\\", "\\").replace('\\"', '"')
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    m = re.fullmatch(r"\[(.*)\]", v)
    if m:
        items = [x.strip().strip('"').strip("'") for x in m.group(1).split(",")]
        return [_scalar(x) for x in items if x != ""]
    return v


def _claim_get(claim: dict, name: str):
    """按名字取 claim 里的值，容忍常见命名差异。

    规则里的变量名是规范术语（between_groups / inside_group），
    而 claim 是抽取层给的字段（between / inside）。不做别名解析的话，
    关系式会因为取不到值而静默跳过，然后被别的约束抢先判定。
    """
    for cand in (name, name.replace("_groups", ""), name.replace("_group", "")):
        if cand in claim:
            return claim[cand]
    return None


def parse_params(block: str) -> dict:
    """从 rule 的 YAML 片段里提取 params 下的键值。

    只处理扁平的 `key: value` 与内联列表；嵌套 mapping 保留为文本
    （当前判定不需要展开 mapping —— 它用于给人看的场景映射）。
    """
    lines = block.splitlines()
    out: dict = {}
    start = None
    base = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)params:\s*$", line)
        if m:
            start = i + 1
            base = len(m.group(1))
            break
    if start is None:
        return out

    for line in lines[start:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base and not line.lstrip().startswith("-"):
            break
        m = re.match(r"^\s*([A-Za-z_][\w]*):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2)
            if val.strip():
                out[key] = _scalar(val)
            else:
                out.setdefault(key, {})   # 嵌套块，占位
    return out


def _list_field(block: str, field_name: str) -> list[str]:
    lines = block.splitlines()
    out: list[str] = []
    start = None
    base = 0
    for i, line in enumerate(lines):
        m = re.match(rf"^(\s*){field_name}:\s*$", line)
        if m:
            start = i + 1
            base = len(m.group(1))
            break
    if start is None:
        return out
    for line in lines[start:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base and not line.lstrip().startswith("-"):
            break
        m = re.match(r"^\s*-\s*(.+?)\s*$", line)
        if m:
            out.append(m.group(1))
    return out


def _statement(block: str) -> str:
    m = re.search(r"statement:\s*[>|]\s*\n((?:\s{6,}.+\n)+)", block)
    if m:
        return " ".join(x.strip() for x in m.group(1).splitlines() if x.strip())
    m = re.search(r"statement:\s*(.+)", block)
    return m.group(1).strip() if m else ""


# ======================================================================
# 引擎
# ======================================================================
NUMERIC_KEYS = (
    "value", "gap", "height", "size", "line_height",
    "max_width", "width", "inside", "between",
    "font_size", "font_weight",
)


def _to_number(v):
    """把抽取层可能给出的数值型字符串转成数字（"12px" → 12）。

    为什么需要：LLM 抽取出来的 claim 经常带单位（"12px"、"1.5 倍"），
    而引擎做的是数值比较。不归一化就会抛
    `TypeError: '>' not supported between instances of 'int' and 'str'` ——
    实测在 115 份文档的全量评测里真的崩了。
    **抽取层的输出格式不由引擎控制，所以引擎必须容错。**
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        # 只在整个字符串以数字开头、后面是可选的单位时转换。
        # 不能从任意位置抠数字：那会把 "gray-300" 的 300、"#F5F7FA" 的 5
        # 抠出来，把颜色字符串变成数字（实测让 COLOR-003 断言直接失败）。
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[^\d\s]*\s*", v)
        if m:
            s = m.group(1)
            return float(s) if "." in s else int(s)
    return v


def _normalize_claim(claim: dict) -> dict:
    out = {}
    for k, v in claim.items():
        out[k] = _to_number(v) if k in NUMERIC_KEYS else v
    return out


def _chars(s: str) -> str:
    """只保留中文字符。

    为什么不保留拉丁字母/数字：例外条款里的 `size`、`variant`、`Container`
    在正常陈述里也会出现，参与 n-gram 会制造大量假阳性 ——
    实测「纯布局容器（Container / Divider）不声明 size 与 variant」
    会把「同时声明了 size 与 variant」误判为例外。
    """
    return "".join(re.findall(r"[\u4e00-\u9fff]", s or ""))


def _grams(s: str, n: int = 3) -> set[str]:
    t = _chars(s)
    return {t[i : i + n] for i in range(0, max(0, len(t) - n + 1))}


# ======================================================================
# 例外检测（在引擎判定之前执行）
#
# 为什么必须有这一步
# ------------------
# 全量评测暴露过一个真实事故：引擎**完全忽略 exceptions**，
# 于是「全屏沉浸式视图内的卡片内边距 8px」被直接判 fail（8 不在白名单），
# 而正确答案是 pass（SPACING-001 的例外明确排除这类卡片）。
# 结果「排除型」一组从 LLM 路径的 10/10 掉到 2/10，整体准确率 95% → 87%。
#
# 判定"这个陈述是否落在例外里"需要语义理解，引擎做不了。
# 但**粗筛**是可以做的：例外条款里的关键短语通常会在陈述里复现。
# 用 4-gram 重合度做粗筛，命中就交回 LLM 路径 —— 宁可多交出去，
# 也不能让引擎在例外案例上给出确定性错误。
# ======================================================================
def exception_match(rule_id: str, statement: str, rules: dict, threshold: int = 2) -> str | None:
    rule = rules.get(rule_id)
    if not rule:
        return None
    sg = _grams(statement)
    if not sg:
        return None
    for exc in rule.exceptions:
        e = exc.strip().lstrip("-").strip()
        if not e or e in ("无",):
            continue
        if len(sg & _grams(e)) >= threshold:
            return e
    return None


class RuleEngine:
    """从 kb/ 编译出可执行规则，并按 claim 判定。"""

    def __init__(self, kb_dir: Path = KB_DIR) -> None:
        self.rules: dict[str, Rule] = {}
        self._load(kb_dir)

    # ------------------------------------------------------------------
    def _load(self, kb_dir: Path) -> None:
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.chunking import _fm_blocks, _split_front_matter

        for p in sorted(kb_dir.glob("*.md")):
            if p.name.startswith("_"):
                continue
            fm, _ = _split_front_matter(p.read_text(encoding="utf-8"))
            kind = "cases" if re.search(r"^cases:\s*$", fm, re.MULTILINE) else "rules"
            if kind != "rules":
                continue
            for rid, title, block in _fm_blocks(fm, kind):
                if not rid or rid.startswith("CASE"):
                    continue
                self.rules[rid] = Rule(
                    id=rid,
                    title=title,
                    statement=_statement(block),
                    params=parse_params(block),
                    exceptions=_list_field(block, "exceptions"),
                    applies_when=_list_field(block, "applies_when"),
                )

    # ------------------------------------------------------------------
    def evaluate(self, rule_id: str, claim: dict) -> Result:
        """按 claim 判定。claim 的键由规则的 params 决定：

            {"value": 32}                      单值
            {"size": 20, "line_height": 26}    倍数
            {"inside": 24, "between": 24}      关系式
            {"gap": 8, "height": 40}           多约束
            {"declared": ["size"]}             存在性
            {"name": "Rectangle 45"}           正则
            {"defaults": {"size": 12}}         默认值
        """
        rule = self.rules.get(rule_id)
        if rule is None:
            return Result("uncertain", rule_id, f"未知规则 {rule_id}")
        claim = _normalize_claim(claim)
        p = rule.params

        # 0) 多字段 claim：逐个执行本规则适用的约束，任一不合规即 fail。
        # 为什么需要：SPACING-005 同时约束「间距是 4 的倍数」与「可点击高度 ≥40」，
        # 如果按单约束顺序返回，{"gap": 8, "height": 36} 会在 step 检查通过后
        # 直接返回 pass，把高度违规漏掉。
        multi: list[Result] = []
        if "step" in p and isinstance(claim.get("gap"), (int, float)):
            st, g = p["step"], claim["gap"]
            ok = g % st == 0
            multi.append(Result("pass" if ok else "fail", rule_id,
                                f"间距 {g} {'是' if ok else '不是'} {st} 的整数倍",
                                "step", f"×{st}", str(g)))
        if "min_touch_target" in p and isinstance(claim.get("height"), (int, float)):
            mt, h = p["min_touch_target"], claim["height"]
            ok = h >= mt
            multi.append(Result("pass" if ok else "fail", rule_id,
                                f"可点击高度 {h} {'≥' if ok else '低于'} 下限 {mt}",
                                "min_touch_target", f"≥{mt}", str(h)))
        if multi:
            bad = [c for c in multi if c.verdict == "fail"]
            return bad[0] if bad else multi[0]


        # 1) 白名单
        if "allowed" in p and isinstance(p["allowed"], list) and "value" in claim:
            v = claim["value"]
            allowed = p["allowed"]
            if v in allowed:
                return Result("pass", rule_id, f"{v} 在允许取值 {allowed} 内",
                              "allowed", str(allowed), str(v))
            return Result("fail", rule_id, f"{v} 不在允许取值 {allowed} 内",
                          "allowed", str(allowed), str(v))

        # 2) 关系式（between > inside）—— 必须排在通用 min/max 之前，
        #    否则 SPACING-003 的 min_between_groups 会先命中 "between" 的值，
        #    把「组内 24 = 组间 24」判成 pass，漏掉"必须严格大于"这个关系约束。
        if "constraint" in p and isinstance(p["constraint"], str):
            expr = p["constraint"]
            m = re.match(r"(\w+)\s*>\s*(\w+)", expr.replace(" ", ""))
            if m:
                a_k, b_k = m.group(1), m.group(2)
                a, b = _claim_get(claim, a_k), _claim_get(claim, b_k)
                lo = p.get("min_between_groups")
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    ok = a > b and (lo is None or a >= lo)
                    why = f"{a_k}={a} 必须严格大于 {b_k}={b}" + (f" 且 ≥{lo}" if lo else "")
                    return Result("pass" if ok else "fail", rule_id,
                                  f"{why} —— {'满足' if ok else '不满足'}",
                                  "constraint", expr, f"{a_k}={a},{b_k}={b}")

        # 3) 步长（必须是某数的整数倍）
        if "step" in p and isinstance(p["step"], (int, float)) and "value" in claim:
            v, st = claim["value"], p["step"]
            if isinstance(v, (int, float)) and v % st == 0:
                return Result("pass", rule_id, f"{v} 是 {st} 的整数倍", "step", f"×{st}", str(v))
            return Result("fail", rule_id, f"{v} 不是 {st} 的整数倍", "step", f"×{st}", str(v))

        # 4) 下限（min_touch_target 优先匹配 height，否则 min 匹配 value）
        for key in ("min_touch_target", "min"):
            if key in p and isinstance(p[key], (int, float)):
                actual = _to_number(claim.get("height", claim.get("value")))
                if isinstance(actual, (int, float)):
                    if actual >= p[key]:
                        return Result("pass", rule_id, f"{actual} ≥ 下限 {p[key]}",
                                      key, f"≥{p[key]}", str(actual))
                    return Result("fail", rule_id, f"{actual} 低于下限 {p[key]}",
                                  key, f"≥{p[key]}", str(actual))

        # 5) 上限（只吃显式宽度字段；不能用通用 value，
        #    否则 SPACING-004 会把「页面外边距 12」当成「内容宽度 12」而判 pass）
        for key in ("max_content_width", "max"):
            if key in p and isinstance(p[key], (int, float)):
                v = _to_number(claim.get("max_width", claim.get("width")))
                if v is None and "range" not in p:
                    v = _to_number(claim.get("value"))
                if isinstance(v, (int, float)):
                    if v <= p[key]:
                        return Result("pass", rule_id, f"{v} ≤ 上限 {p[key]}", key, f"≤{p[key]}", str(v))
                    return Result("fail", rule_id, f"{v} 超过上限 {p[key]}", key, f"≤{p[key]}", str(v))

        # 6) 区间
        if "range" in p and isinstance(p["range"], str) and "value" in claim:
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", p["range"])
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                v = claim["value"]
                if isinstance(v, (int, float)) and lo <= v <= hi:
                    return Result("pass", rule_id, f"{v} 在区间 {lo}-{hi} 内",
                                  "range", f"{lo}-{hi}", str(v))
                return Result("fail", rule_id, f"{v} 不在区间 {lo}-{hi} 内",
                              "range", f"{lo}-{hi}", str(v))

        # 7) 倍数（行高 = 字号 × multiplier）
        if "multiplier" in p and {"size", "line_height"} <= claim.keys():
            size = _to_number(claim["size"])
            lh = _to_number(claim["line_height"])
            k = p["multiplier"]
            if not (isinstance(size, (int, float)) and isinstance(lh, (int, float)) and size):
                return Result("uncertain", rule_id,
                              f"行高判定需要数值型 size 与 line_height，实际 {claim['size']!r}/{claim['line_height']!r}",
                              "multiplier", f"×{k}", "")
            ratio = lh / size
            if abs(ratio - k) < 1e-6:
                return Result("pass", rule_id, f"{lh}/{size} = {ratio:g}，等于 {k}",
                              "multiplier", f"×{k}", f"{ratio:g}")
            return Result("fail", rule_id, f"{lh}/{size} = {ratio:g}，不等于 {k}",
                          "multiplier", f"×{k}", f"{ratio:g}")

        # 8) 正则（单 pattern）
        #    方向要区分：多数规则里的 pattern 是**要求的形式**（匹配=合规），
        #    但 NAME-003 的 pattern 描述的是**禁止的形式**（匹配=违规）。
        #    用 `forbidden_examples` 字段作为方向信号 —— 它是"这类写法不允许"的明示。
        if "pattern" in p and isinstance(p["pattern"], str):
            text = claim.get("name", claim.get("value"))
            # 抽取值是 "unknown" 时不要判定：那不是"不合规"，是"没抽到"。
            # 实测漏掉这个守卫会把「品牌 Logo 位图内部使用多种原始色值」
            # 判成 fail（拿字符串 "unknown" 去匹配正则），而它本该回落。
            if isinstance(text, str) and text.strip() and text.strip().lower() != "unknown":
                forbidden_form = "forbidden_examples" in p
                try:
                    hit = bool(re.search(p["pattern"], text))
                except re.error:
                    hit = False
                if forbidden_form:
                    if hit:
                        return Result("fail", rule_id,
                                      f"「{text}」命中禁止的形式 {p['pattern']}",
                                      "pattern", f"不匹配 {p['pattern']}", text)
                    return Result("pass", rule_id,
                                  f"「{text}」不是被禁止的默认名称形式",
                                  "pattern", f"不匹配 {p['pattern']}", text)
                if hit:
                    return Result("pass", rule_id, f"「{text}」符合 {p['pattern']}",
                                  "pattern", p["pattern"], text)
                return Result("fail", rule_id, f"「{text}」不符合 {p['pattern']}",
                              "pattern", p["pattern"], text)

        # 8b) 正则（必须匹配 + 禁止匹配，如 COLOR-003 的语义 token）
        if ("required_pattern" in p or "forbidden_pattern" in p):
            text = claim.get("name", claim.get("value"))
            if isinstance(text, str) and text.strip() and text.strip().lower() != "unknown":
                req = p.get("required_pattern")
                forb = p.get("forbidden_pattern")
                if isinstance(req, str):
                    try:
                        if not re.search(req, text):
                            return Result("fail", rule_id,
                                          f"「{text}」不匹配必需的语义 token 形式 {req}",
                                          "required_pattern", req, text)
                    except re.error:
                        pass
                if isinstance(forb, str):
                    try:
                        if re.search(forb, text):
                            return Result("fail", rule_id,
                                          f"「{text}」命中禁止形式 {forb}（原始色值/色阶变量）",
                                          "forbidden_pattern", f"不匹配 {forb}", text)
                    except re.error:
                        pass
                return Result("pass", rule_id, f"「{text}」使用语义 token，合规",
                              "required_pattern", str(req), text)

        # 8c) 默认值必须等于规定值（如 TYPO-004 正文默认字号 = 14）
        if "default" in p and isinstance(p["default"], (int, float)) and "value" in claim:
            v = claim["value"]
            if v == p["default"]:
                return Result("pass", rule_id, f"默认值 {v} 等于规定值 {p['default']}",
                              "default", str(p["default"]), str(v))
            return Result("fail", rule_id, f"默认值 {v} 不等于规定值 {p['default']}",
                          "default", str(p["default"]), str(v))

        # 9) 必填项（存在性）
        if "required" in p and isinstance(p["required"], list) and "declared" in claim:
            need = set(p["required"])
            have = set(claim["declared"])
            missing = need - have
            if missing:
                return Result("fail", rule_id, f"缺少必填项 {sorted(missing)}",
                              "required", str(sorted(need)), str(sorted(have)))
            return Result("pass", rule_id, f"必填项齐全 {sorted(need)}",
                          "required", str(sorted(need)), str(sorted(have)))

        # 10) 默认值（size 与 variant 都必须有默认值）
        if rule_id in self._default_value_rules() and "defaults" in claim:
            need = {"size", "variant"}
            have = set(claim["defaults"].keys())
            missing = need - have
            if missing:
                return Result("fail", rule_id, f"缺少默认值 {sorted(missing)}",
                              "defaults", "size 与 variant 均有默认值", str(sorted(have)))
            return Result("pass", rule_id, "size 与 variant 均有默认值",
                          "defaults", "size 与 variant 均有默认值", str(sorted(have)))

        return Result("uncertain", rule_id,
                      "该 claim 形状与本规则的约束不匹配（需要更具体的字段）")

    # ------------------------------------------------------------------
    @staticmethod
    def _default_value_rules() -> set[str]:
        """哪些规则属于"必须有默认值"这类存在性约束。"""
        return {"COMP-004"}

    def exception_hit(self, rule_id: str, statement: str) -> str | None:
        """该陈述是否可能落在规则的例外条款内（粗筛）。"""
        return exception_match(rule_id, statement, self.rules)

    def supports(self, rule_id: str) -> bool:
        """该规则是否可由引擎确定性判定（params 里有可执行约束）。"""
        r = self.rules.get(rule_id)
        if not r:
            return False
        executable = {"allowed", "step", "min", "min_touch_target", "max",
                      "max_content_width", "range", "multiplier", "constraint",
                      "pattern", "required", "default",
                      "required_pattern", "forbidden_pattern"}
        if executable & set(r.params):
            return True
        return rule_id in self._default_value_rules()

    def coverage(self) -> tuple[int, int]:
        n = len(self.rules)
        k = sum(1 for rid in self.rules if self.supports(rid))
        return k, n
