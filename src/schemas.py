"""输出契约（能力 5 的核心）：用 Pydantic 把"自由文本回答"变成"可校验的结构化数据"。

为什么需要它：
  - 模型的自然语言输出无法被程序可靠消费（下游要做统计、要渲染 UI、要入库）
  - 字段带 description，等于把约束写进 schema 一起交给模型，比在 prompt 里用散装文字描述更稳
  - 校验失败时能拿到精确的错误信息，可以原样回喂给模型让它自己修 → 这就是"鲁棒性"

注意：本文件只定义"数据长什么样"，不包含任何业务逻辑。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ============================================================
# 第一层：agent 在循环中调用的工具，其返回结构
# ============================================================


class SpecHit(BaseModel):
    """一条检索命中的规范原文。"""

    section: str = Field(description="出处标识，例如 'spacing.md#卡片间距'")
    title: str = Field(description="命中条目的标题")
    content: str = Field(description="规范原文，判定时必须引用它作为依据")
    score: float = Field(default=0.0, description="检索得分，Week 2 接入真实检索后才有意义")


# ============================================================
# 第二层：agent 的最终产出（结构化审查报告）
# ============================================================


class Issue(BaseModel):
    """对文档中某一个具体点的判定。"""

    target: str = Field(description="被检查的对象，例如 'Card 内边距'、'主按钮文字色'")
    verdict: Literal["pass", "fail", "uncertain"] = Field(
        description="pass=符合规范；fail=违反规范；uncertain=规范里查不到依据，不要猜"
    )
    reason: str = Field(description="判定的理由，一句话说清楚为什么")
    evidence: str = Field(
        default="",
        description="直接引用检索到的规范原文；没有检索到依据时必须留空并判 uncertain",
    )
    suggestion: str = Field(default="", description="verdict=fail 时给出可执行的修改建议")


class SpecReport(BaseModel):
    """审查报告主体。这是模型需要生成的部分。"""

    summary: str = Field(description="整体结论，2-3 句话")
    issues: list[Issue] = Field(default_factory=list, description="逐条判定结果")
    evidence_sections: list[str] = Field(
        default_factory=list,
        description="本次判定实际引用过的规范出处列表，用于溯源",
    )


# ============================================================
# 第三层：包住报告的程序元信息（由代码注入，模型不负责填）
#    把"模型产出"和"运行指标"分开，是第 4 周做评测报告的基础
# ============================================================


class AgentReport(BaseModel):
    """落盘用的完整结果 = 模型产出 + 运行指标。"""

    doc: str = Field(description="被审查文档的文件名")
    report: SpecReport = Field(description="模型生成的结构化报告")
    steps: int = Field(default=0, description="模型轮次数（含工具调用轮与收尾轮）")
    tool_calls: int = Field(default=0, description="工具被调用的总次数")
    prompt_tokens: int = Field(default=0, description="累计输入 token")
    completion_tokens: int = Field(default=0, description="累计输出 token")
    elapsed_sec: float = Field(default=0.0, description="总耗时（秒）")
    provenance_drift: list[str] = Field(
        default_factory=list,
        description=(
            "溯源漂移：evidence_sections 里出现、但本次检索从未返回过的 section。"
            "非空说明模型改写了出处标签，引用链会断。"
        ),
    )
    provenance_checked: bool = Field(
        default=True,
        description=(
            "溯源检测本身是否可信。为 False 时 provenance_drift 不代表真实情况 —— "
            "指标不可信时必须显式暴露，而不是静默返回一个假结果。"
        ),
    )
    budget_exhausted: bool = Field(
        default=False,
        description="是否因为触达工具调用总预算而被迫中断检索",
    )
