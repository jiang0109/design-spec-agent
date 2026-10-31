"""混合判定流程：抽取 → 路由 → 规则引擎优先，未知/不可执行时回落。

设计原则
--------
    ｜能确定性判的，绝不交给模型｜

    抽取（LLM）→ 属性在闭集内？
                    是 → 规则引擎判定（确定性、可复现、可单测）
                    否 → 回落：标记为需要 LLM / 检索路径

为什么不做"引擎判定 + LLM 复核"
------------------------------
复核会重新引入不确定性 —— 而这一整轮改动的目的就是消除它。
对确定性判定，复核只应发生在"引擎说 claim 形状不匹配"这种**结构性失败**上，
不应该对引擎给出的结论做二次语义判断。

当前实现里"回落"只有一种情况：属性不在闭集（文档讲了知识库未覆盖的对象，
如圆角、投影、动效）。这类本来就该判 uncertain，不需要再调 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .claim_extractor import UNKNOWN, extract, route
from .rule_engine import RuleEngine


@dataclass
class Judgment:
    statement: str
    attribute: str
    rule_id: str
    verdict: str            # pass / fail / uncertain
    detail: str
    source: str             # engine / llm / fallback
    field_name: str = ""
    expected: str = ""
    actual: str = ""
    evidence: bool = False      # LLM 路径是否引用了规范原文
    evidence_text: str = ""     # 规则原文（引擎路径可直接给出，可溯源）
    suggestion: str = ""        # 可执行的修改建议


# 字段名的中文标签：建议文案里不该出现 `allowed`、`required_pattern` 这类内部字段名。
_FIELD_LABEL = {
    "allowed": "取值",
    "step": "取值粒度",
    "min": "下限",
    "min_touch_target": "最小可点击高度",
    "max": "上限",
    "max_content_width": "最大宽度",
    "range": "取值范围",
    "multiplier": "行高倍数",
    "constraint": "间距关系",
    "pattern": "命名格式",
    "required": "必填属性",
    "defaults": "默认值",
    "default": "默认值",
    "required_pattern": "颜色引用方式",
    "forbidden_pattern": "颜色引用方式",
}


def _enrich(rule, res) -> tuple[str, str]:
    """给引擎判定补上「规范原文」与「修改建议」。

    为什么必须补：README 承诺输出「结构化报告 + 引用出处 + 修改建议」，
    而规则引擎的原始输出只有 field/expected/actual。直接切默认会让报告失去
    这两个字段 —— 判定更准了，但产物更差，等于用可复现性换了可用性。

    好在这两件事都能**确定性生成**，不需要再调模型：
      引用出处 = 规则原文（引擎本来就加载了 rule.statement）
      修改建议 = 由 field / expected 拼模板，字段名转成中文标签
    """
    if rule is None:
        return "", ""
    evidence_text = (rule.statement or "").strip()[:260]
    if res.verdict != "fail":
        return evidence_text, ""
    label = _FIELD_LABEL.get(res.field_name, res.field_name or "取值")
    actual = res.actual if res.actual else "未给出"
    sug = f"{label}应为 {res.expected}，当前为 {actual}。"
    if rule.exceptions:
        exc = "；".join(e.strip() for e in rule.exceptions[:2] if e.strip() and e.strip() != "无")
        if exc:
            sug += f"（例外：{exc}）"
    return evidence_text, sug


@dataclass
class HybridReport:
    judgments: list[Judgment] = field(default_factory=list)
    extract_calls: int = 0
    llm_runs: int = 0

    # ---- 统计 ----
    @property
    def total(self) -> int:
        return len(self.judgments)

    @property
    def by_engine(self) -> int:
        return sum(1 for j in self.judgments if j.source == "engine")

    @property
    def fallback(self) -> int:
        return sum(1 for j in self.judgments if j.source == "fallback")

    @property
    def by_llm(self) -> int:
        return sum(1 for j in self.judgments if j.source == "llm")

    def counts(self) -> dict[str, int]:
        out = {"pass": 0, "fail": 0, "uncertain": 0}
        for j in self.judgments:
            out[j.verdict] = out.get(j.verdict, 0) + 1
        return out


def judge_claim(claim, engine: RuleEngine) -> Judgment:
    """单条 claim → 判定。"""
    rule_id, routed, note = route(claim.attribute, claim.claim)

    if rule_id is None:
        return Judgment(claim.statement, claim.attribute, "", "uncertain", note, "fallback")

    if not engine.supports(rule_id):
        # 规则存在但引擎无法确定性执行（如 COLOR-005 需要语义）
        return Judgment(
            claim.statement, claim.attribute, rule_id, "uncertain",
            f"{rule_id} 的约束需要语义判断，引擎不支持，需 LLM 复核", "fallback",
        )

    # ⚠️ 例外粗筛：必须在引擎判定之前。
    # 否则「全屏沉浸式视图内的卡片内边距 8px」会被判 fail，
    # 而正确答案是 pass（被 SPACING-001 的例外排除）。
    # 实测漏掉这一步会让「排除型」一组从 10/10 掉到 2/10。
    exc = engine.exception_hit(rule_id, claim.statement)
    if exc:
        return Judgment(
            claim.statement, claim.attribute, rule_id, "uncertain",
            f"陈述可能落在例外条款内（{exc[:24]}…），交 LLM 判断", "fallback",
        )

    r = engine.evaluate(rule_id, routed)
    if r.verdict == "uncertain":
        # 结构性失败：claim 形状与规则约束不匹配（抽取没给全字段）
        return Judgment(claim.statement, claim.attribute, rule_id, "uncertain",
                        f"{note}；但 {r.detail}", "fallback")
    ev_text, sug = _enrich(engine.rules.get(rule_id), r)
    return Judgment(claim.statement, claim.attribute, rule_id, r.verdict, r.detail,
                    "engine", r.field_name, r.expected, r.actual,
                    evidence=bool(ev_text), evidence_text=ev_text, suggestion=sug)


def to_agent_report(doc_name: str, rep: HybridReport) -> "AgentReport":
    """把 HybridReport 适配成与 LLM 路径相同的 AgentReport，供 CLI/下游复用。

    为什么要适配而不是新造一种输出：两条路径的报告格式必须一致，
    否则每换一次引擎，下游（CLI 渲染、评测匹配、未来前端）都要改。
    """
    from .schemas import AgentReport, Issue, SpecReport

    c = rep.counts()
    # 同一句陈述可能命中多条规则（例如「variant 未设默认值」同时违反
    # COMP-001 必填声明与 COMP-004 默认值要求）。这时如果不加标识，
    # 报告里会出现两行完全相同的 target，看起来像重复输出。
    seen: dict[str, int] = {}
    for j in rep.judgments:
        seen[j.statement] = seen.get(j.statement, 0) + 1

    issues = []
    for j in rep.judgments:
        base = (j.statement or j.attribute or "未命名项")[:40]
        target = f"{base} ｜{j.rule_id}" if seen.get(j.statement, 0) > 1 and j.rule_id else base
        issues.append(
            Issue(
                target=target,
                verdict=j.verdict if j.verdict in ("pass", "fail", "uncertain") else "uncertain",
                reason=j.detail,
                evidence=j.evidence_text,
                suggestion=j.suggestion,
            )
        )
    sections = sorted({j.rule_id for j in rep.judgments if j.rule_id})
    summary = (
        f"共判定 {rep.total} 项：{c.get('pass', 0)} 项合规、{c.get('fail', 0)} 项不合规、"
        f"{c.get('uncertain', 0)} 项无依据。"
        f"其中 {rep.by_engine} 项由规则引擎确定性判定，"
        f"{rep.by_llm + rep.fallback} 项走语义路径。"
    )
    return AgentReport(
        doc=doc_name,
        report=SpecReport(summary=summary, issues=issues, evidence_sections=sections),
        steps=rep.extract_calls,
        tool_calls=rep.llm_runs,
    )


def run_hybrid_agent(
    doc: str, doc_name: str = "document.md", *, agent_runner=None
) -> "AgentReport":
    """双路并行的端到端入口，返回与 LLM 路径同构的报告。"""
    return to_agent_report(doc_name, evaluate_document_dual(doc, agent_runner=agent_runner))


def evaluate_document(doc: str, engine: RuleEngine | None = None) -> HybridReport:
    """抽取 + 逐条判定（**仅引擎路径**，未知属性标 uncertain，不调 LLM）。"""
    engine = engine or RuleEngine()
    claims = extract(doc)
    rep = HybridReport(extract_calls=1)
    for c in claims:
        rep.judgments.append(judge_claim(c, engine))
    return rep


# ======================================================================
# 双路并行：引擎路径 + LLM 路径
#
# 为什么必须双路而不是单路替换
# ----------------------------
# 引擎只认闭集属性。而知识库里有一类规则是「明确排除某对象」
# （TYPO-002 排除表格单元格、SPACING-001 排除全屏卡片…）。
# 判断"这个陈述是否落在例外里"需要读例外条款的语义 —— 引擎做不到。
# 这类案例现状是 LLM 路径做对的（120 点里 excluded 10/10）。
#
# 所以正确的形态是**分工**：
#   闭集属性            → 引擎（确定性、可复现）
#   未知属性 / 需读例外 → LLM 路径
#
# 成本控制：LLM 路径只跑"未知陈述"拼成的迷你文档，
# 不重新处理引擎已经判过的部分。
# ======================================================================
def _mini_doc(statements: list[str]) -> str:
    lines = [f"- {s}。" if not s.endswith(("。", "！", "？")) else f"- {s}" for s in statements]
    return "# 待复核项\n\n" + "\n".join(lines) + "\n"


def _match_issue(statement: str, issues: list) -> object | None:
    """把一条陈述对齐到 LLM 给出的某个判定。

    优先按顺序对齐（迷你文档每行一条陈述，通常一一对应）；
    数量不一致时按子串包含关系兜底。
    """
    for i in issues:
        t = getattr(i, "target", "") or ""
        if t and (t in statement or statement in t):
            return i
    return None


def evaluate_document_dual(
    doc: str,
    engine: RuleEngine | None = None,
    llm_fallback: bool = True,
    agent_runner=None,
) -> HybridReport:
    """双路：引擎判定闭集属性，其余交给 LLM 路径。

    agent_runner
        LLM 路径的控制流实现，默认 `src.agent.run_agent`（手写 ReAct 循环）。
        显式传入 `src.agent_graph.run_agent_graph` 即可用**同一套基线**对比
        状态图实现 —— 迁移验收靠它。

        为什么用参数注入而不是环境变量/模块全局：项目原有纪律是"不改写模块全局，
        避免隐藏状态污染"（见 agent.py 里 llm_verify 的处理）。评测脚本若靠
        环境变量切换实现，两次运行的区别就不在运行记录里了。
    """
    engine = engine or RuleEngine()
    claims = extract(doc)
    rep = HybridReport(extract_calls=1)

    need_llm = []
    for c in claims:
        j = judge_claim(c, engine)
        if j.source == "engine":
            rep.judgments.append(j)
        else:
            need_llm.append(c)

    if not need_llm:
        return rep

    if not llm_fallback:
        for c in need_llm:
            rep.judgments.append(
                Judgment(c.statement, c.attribute, "", "uncertain",
                         "关闭 LLM 兜底：该属性不在闭集内", "fallback")
            )
        return rep

    # 只把未知陈述交给 LLM 路径
    if agent_runner is None:
        from .agent import run_agent as agent_runner

    ar = agent_runner(_mini_doc([c.statement for c in need_llm]),
                      doc_name="hybrid_fallback", verbose=False)
    rep.llm_runs = 1
    issues = list(ar.report.issues)

    # 顺序对齐优先（一一对应时最可靠），否则退化为子串匹配
    ordered = len(issues) == len(need_llm)
    for idx, c in enumerate(need_llm):
        issue = issues[idx] if ordered else _match_issue(c.statement, issues)
        if issue is None:
            rep.judgments.append(
                Judgment(c.statement, c.attribute, "", "uncertain",
                         "LLM 路径未就该点给出判定", "llm")
            )
            continue
        rep.judgments.append(
            Judgment(
                c.statement, c.attribute, "",
                getattr(issue, "verdict", "uncertain"),
                (getattr(issue, "reason", "") or "")[:60],
                "llm",
                evidence=bool(getattr(issue, "evidence", "")),
            )
        )
    return rep
