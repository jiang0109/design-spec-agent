"""Claim 抽取器 + 属性路由：把自然语言陈述转成可被规则引擎执行的 claim。

为什么需要抽取层
----------------
`src/rule_engine.py` 已经能确定性地判定 `{claim}`，但它假定"用哪条规则"
和"claim 长什么样"都已确定。这两件事在自然语言文档里都没有现成答案：

    文档：**内边距**：卡片内边距使用 20px。
    需要：attribute="卡片内边距"，claim={"value": 20}

所以本模块补两件事：
  1. 用 LLM 把文档陈述抽取成受控形态（`extract()`）
  2. 用**受控属性词表**把 attribute 路由到 rule_id（`route()`）

为什么用受控词表而不是自由文本匹配
----------------------------------
前几轮已经证明：让判定器去判断"这条规则是否适用于这个对象"，在措辞变化下
极其不稳（`relevance_report.md` 三版提示词、`applicability_report.md` v3 判据，
都没能稳定解决）。而**如果属性名来自一个闭集**，"属性 → 规则"就是查表，
不需要语义判断。

代价：抽取器必须在闭集里选。选不出来（文档讲的是圆角、投影、动效这类
知识库未覆盖的对象）就输出 `unknown`，由上层回落到检索 + LLM 路径。
**这正是我们想要的行为** —— 未知的交给人/语义层，已知的交给代码。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from .llm import ask_llm


# ======================================================================
# 受控属性词表：属性 → (规则 id, claim 字段要求)
# 这份表是把"设计规范的语义"翻译成"可执行契约"的地方，需要人工维护。
# ======================================================================
@dataclass(frozen=True)
class Attribute:
    key: str                 # 抽取器要输出的属性名（闭集）
    rule_id: str             # 路由到的规则
    fields: str              # claim 需要哪些字段（同时用于提示词）
    hint: str                # 给抽取器的判定提示

    def describe(self) -> str:
        return f"{self.key}｜字段 {self.fields}｜{self.hint}"


ATTRIBUTES: list[Attribute] = [
    Attribute("卡片内边距", "SPACING-001", '{"value": 数值(px)}',
              "卡片的内边距/内容与边框的距离"),
    Attribute("卡片间距", "SPACING-002", '{"value": 数值(px)}',
              "卡片与卡片之间、同组卡片内部的间距"),
    Attribute("分组间距", "SPACING-003", '{"inside": 组内数值, "between": 组间数值}',
              "分组之间的间距，且需与组内间距对比"),
    Attribute("页面外边距", "SPACING-004", '{"value": 数值(px)}',
              "页面左右外边距"),
    Attribute("内容区最大宽度", "SPACING-004", '{"max_width": 数值(px)}',
              "内容区/容器的最大宽度上限"),
    Attribute("元素间距", "SPACING-005", '{"gap": 数值(px)}',
              "组件内部元素之间的距离"),
    Attribute("可点击高度", "SPACING-005", '{"height": 数值(px)}',
              "按钮、可点击元素的高度"),
    Attribute("文字对比度", "COLOR-001", '{"value": 数值(比值), "font_size": 数值(px，可选), "font_weight": 数值(可选)}',
              "文字与背景的对比度。若给出字号≥18 或 14px且字重≥600，属大号文字"),
    Attribute("颜色引用方式", "COLOR-003", '{"value": "色值或令牌字符串"}',
              "颜色用什么方式引用（语义 token / 色阶变量 / 十六进制）"),
    Attribute("语义色类别", "COLOR-004", '{"value": "语义色类别名"}',
              "语义色属于哪一类（primary/secondary/success/warning/danger/info）"),
    Attribute("状态色用途", "COLOR-005", '{"value": "用于什么语义场景"}',
              "状态色（success/warning/danger/info）被用在什么语义场景，是否属于该状态"),
    Attribute("字号", "TYPO-001", '{"value": 数值(px)}', "文字字号"),
    Attribute("行高", "TYPO-002", '{"size": 字号(px), "line_height": 行高(px)}',
              "行高，需要同时给出字号"),
    Attribute("字重", "TYPO-003", '{"value": 数值}', "字重数值"),
    Attribute("正文默认字号", "TYPO-004", '{"value": 数值(px)}', "正文的默认字号"),
    Attribute("属性声明", "COMP-001", '{"declared": ["size","variant"]}',
              "组件声明了哪些必填属性"),
    Attribute("属性命名", "COMP-002", '{"name": "属性名"}', "组件属性名的写法"),
    Attribute("布尔属性命名", "COMP-003", '{"name": "属性名"}', "布尔类型属性的命名"),
    Attribute("属性默认值", "COMP-004", '{"defaults": {"size": ..., "variant": ...}}',
              "组件属性的默认值"),
    Attribute("变体取值", "COMP-005", '{"value": "变体名"}', "variant 的取值"),
    Attribute("图层命名", "NAME-001", '{"name": "图层名"}', "图层命名格式"),
    Attribute("组件命名", "NAME-002", '{"name": "组件名"}', "组件命名格式"),
    Attribute("图层默认名", "NAME-003", '{"name": "图层名"}',
              "图层是否仍是工具自动生成的默认名（Frame 123 这类）"),
    Attribute("令牌命名", "NAME-004", '{"name": "令牌名"}', "设计令牌命名格式"),
]

BY_KEY: dict[str, Attribute] = {a.key: a for a in ATTRIBUTES}
UNKNOWN = "unknown"


# ======================================================================
# 路由
# ======================================================================
def _pick_contrast_rule(claim: dict) -> str:
    """文字对比度按字号/字重分流到 COLOR-001（正文）或 COLOR-002（大号）。"""
    size = claim.get("font_size")
    weight = claim.get("font_weight")
    if isinstance(size, (int, float)):
        if size >= 18:
            return "COLOR-002"
        if size >= 14 and isinstance(weight, (int, float)) and weight >= 600:
            return "COLOR-002"
        return "COLOR-001"
    # 没给字号时保守走正文标准（更严格）
    return "COLOR-001"


def route(attribute: str, claim: dict) -> tuple[str | None, dict, str]:
    """属性 + claim → (rule_id, 规整后的 claim, 说明)。

    返回 rule_id=None 表示无法路由（未知属性），上层应回落到 LLM 路径。
    """
    if attribute == UNKNOWN or attribute not in BY_KEY:
        return None, claim, f"属性「{attribute}」不在受控词表内，交回 LLM 路径"

    attr = BY_KEY[attribute]
    out = dict(claim)

    # 文字对比度需要按字号/字重选规则
    if attribute == "文字对比度":
        rid = _pick_contrast_rule(out)
        out.pop("font_size", None)
        out.pop("font_weight", None)
        return rid, out, f"按字号/字重分流到 {rid}"

    return attr.rule_id, out, f"路由到 {attr.rule_id}"


# ======================================================================
# 抽取
# ======================================================================
EXTRACT_SYSTEM = """你从设计文档里抽取"可被规范校验的陈述"。

输出 JSON：
{"claims": [{"statement": "...", "attribute": "...", "claim": {...}}]}

attribute 必须从下面的闭集里选，**不要自创**；无法归入任何一项时填 "unknown"。

{attributes}

规则：
- 每条陈述独立成一条 claim，statement 用文档原文里的那句话。
- claim 里的数值只填数字（不带单位），字符串原样保留。
- 文档里没有给出具体取值、只是笼统描述时，填 "unknown"。
- 不要遗漏条目，也不要合并两条不同属性的陈述。"""


@dataclass
class Claim:
    statement: str
    attribute: str
    claim: dict = field(default_factory=dict)


def extract(doc: str, temperature: float = 0.0) -> list[Claim]:
    """把文档抽取成 Claim 列表。未知属性用 "unknown" 标记。"""
    attributes_text = "\n".join(f"- {a.describe()}" for a in ATTRIBUTES)
    # 用 replace 而不是 str.format：提示词里含 JSON 示例（{"claims": [...]}），
    # .format() 会把那里的花括号当占位符，抛 KeyError: '"claims"'。
    system = EXTRACT_SYSTEM.replace("{attributes}", attributes_text)
    res = ask_llm(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": f"文档：\n\n{doc}"},
        ],
        temperature=temperature,
        json_mode=True,
    )
    raw = (res.message.content or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        data = json.loads(raw)
        items = data.get("claims") or []
    except (json.JSONDecodeError, ValueError, TypeError):
        return []

    out: list[Claim] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append(
            Claim(
                statement=str(it.get("statement", ""))[:120],
                attribute=str(it.get("attribute", UNKNOWN)),
                claim=it.get("claim") if isinstance(it.get("claim"), dict) else {},
            )
        )
    return out
