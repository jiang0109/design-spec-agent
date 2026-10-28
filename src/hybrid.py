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
    source: str             # engine / fallback
    field_name: str = ""
    expected: str = ""
    actual: str = ""


@dataclass
class HybridReport:
    judgments: list[Judgment] = field(default_factory=list)
    extract_calls: int = 0

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

    r = engine.evaluate(rule_id, routed)
    if r.verdict == "uncertain":
        # 结构性失败：claim 形状与规则约束不匹配（抽取没给全字段）
        return Judgment(claim.statement, claim.attribute, rule_id, "uncertain",
                        f"{note}；但 {r.detail}", "fallback")
    return Judgment(claim.statement, claim.attribute, rule_id, r.verdict, r.detail,
                    "engine", r.field_name, r.expected, r.actual)


def evaluate_document(doc: str, engine: RuleEngine | None = None) -> HybridReport:
    """抽取 + 逐条判定。"""
    engine = engine or RuleEngine()
    claims = extract(doc)
    rep = HybridReport(extract_calls=1)
    for c in claims:
        rep.judgments.append(judge_claim(c, engine))
    return rep
