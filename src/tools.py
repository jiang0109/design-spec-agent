"""工具层：定义 agent 可以调用的能力，以及每个能力的 JSON Schema。

Week 1 目标：只关心"工具是怎么被描述、被调用、结果怎么回填"这条链路。
所以这里用 LOCAL_KB 假数据占位，Week 2 会把 search_spec 换成真实检索
（分块 → embedding → 混合检索 → rerank），但工具的**契约不会变**。
这种"先定契约、后换实现"的做法，本身就是工程化的关键点。

三个必答问题（面试会问）：
  1. tools schema 怎么让模型知道有哪些工具？
     → 它被序列化进请求体，模型读的是描述文本，输出的只是一段 JSON 字符串。
  2. 为什么结果要以 role="tool" 追加回 messages？
     → 模型没有状态，每一轮都靠完整 messages 重建上下文。
  3. 工具抛异常怎么办？
     → 不要让它冒泡成程序崩溃，要把错误信息当成工具结果回填，让模型自己纠正。
"""

from __future__ import annotations

import json

from .schemas import SpecHit

# ------------------------------------------------------------------
# 占位知识库：每条都带 section（出处），因为"可溯源"是后面评测的硬指标
# ------------------------------------------------------------------
LOCAL_KB: list[dict] = [
    {
        "section": "spacing.md#卡片内边距",
        "title": "卡片内边距",
        "content": "卡片内边距只允许使用 12 / 16 / 24 三档。紧凑型列表卡片用 12，"
        "常规内容卡片用 16，营销位大卡片用 24。不得出现 8、20、32 等档位。",
        "keywords": ["内边距", "padding", "卡片", "card"],
    },
    {
        "section": "spacing.md#卡片间距",
        "title": "卡片间距",
        "content": "卡片之间的间距只允许使用 16 或 24。同一组卡片必须使用同一间距，"
        "禁止混用；不同分组之间使用 24，分组内部使用 16。",
        "keywords": ["间距", "gap", "卡片", "margin", "栅格"],
    },
    {
        "section": "color.md#文字对比度",
        "title": "文字对比度",
        "content": "正文文字与背景的对比度必须 ≥ 4.5:1；大号文字（≥18px 或 14px 粗体）"
        "可放宽至 ≥ 3:1。不满足时必须更换色板中的语义色，不得自行调浅。",
        "keywords": ["对比度", "contrast", "颜色", "文字", "可访问性"],
    },
    {
        "section": "color.md#语义色",
        "title": "语义色的使用",
        "content": "色彩必须使用语义 token（如 color-text-primary），禁止直接引用色值变量"
        "（如 blue-500）。语义色共 6 类：primary / secondary / success / warning / danger / info。",
        "keywords": ["语义色", "token", "颜色", "色值", "primary"],
    },
    {
        "section": "typography.md#字号阶梯",
        "title": "字号阶梯",
        "content": "字号只允许 12 / 14 / 16 / 20 / 24 / 32 六档。正文默认 14，"
        "标题按层级使用 20 和 24，展示型标题用 32。行高统一为字号的 1.5 倍。",
        "keywords": ["字号", "字体", "标题", "字号阶梯", "行高"],
    },
    {
        "section": "component.md#组件属性命名",
        "title": "组件属性命名",
        "content": "组件属性统一使用 camelCase，禁止 kebab-case 和中文属性名。"
        "布尔属性必须以 is / has / can 开头，例如 isLoading、hasIcon。",
        "keywords": ["属性", "props", "命名", "camelCase", "组件"],
    },
    {
        "section": "component.md#必填属性",
        "title": "必填属性约定",
        "content": "每个组件必须显式声明 size 与 variant 两个属性，且必须提供默认值。"
        "缺少默认值的属性视为不合规（会导致设计稿与代码不一致）。",
        "keywords": ["属性", "必填", "默认值", "variant", "size", "组件"],
    },
    {
        "section": "naming.md#图层命名",
        "title": "图层与组件命名",
        "content": "图层名使用「组件名/修饰」格式，例如 Button/Primary/Default。"
        "禁止出现 Frame 123、Rectangle 45 这类自动生成的默认名称。",
        "keywords": ["命名", "图层", "组件名", "frame"],
    },
]


# ------------------------------------------------------------------
# 工具实现（Week 1 用关键词打分，Week 2 整体替换掉函数体）
# ------------------------------------------------------------------
def search_spec(query: str, top_k: int = 3) -> str:
    """按查询串检索设计规范知识库，返回命中的规范原文（含出处）。"""
    q = (query or "").lower()
    tokens = [t for t in q.replace("，", " ").replace(",", " ").split() if t]
    # 中文没有空格，所以额外做"子串命中"判断
    scored: list[tuple[float, dict]] = []
    for item in LOCAL_KB:
        score = 0.0
        for kw in item["keywords"]:
            if kw.lower() in q:
                score += 2.0
        for tk in tokens:
            if tk and tk in f"{item['title']}{item['content']}".lower():
                score += 1.0
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    hits = [
        SpecHit(section=it["section"], title=it["title"], content=it["content"], score=sc)
        for sc, it in scored[:top_k]
    ]

    if not hits:
        return json.dumps(
            {"found": False, "message": "知识库中没有找到相关规范，请判为 uncertain 并说明缺少依据。"},
            ensure_ascii=False,
        )
    return json.dumps(
        {"found": True, "hits": [h.model_dump() for h in hits]},
        ensure_ascii=False,
    )


# ------------------------------------------------------------------
# 工具注册表：函数实现与 JSON Schema 一一对应
# ------------------------------------------------------------------
TOOL_IMPLS = {
    "search_spec": search_spec,
}

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_spec",
            "description": (
                "检索设计规范知识库。任何关于间距、颜色、字号、组件属性、命名的判定，"
                "都必须先调用本工具取得规范原文，不得凭记忆回答。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要查询的规范问题，用自然语言描述，例如 '卡片内边距允许哪几档'",
                    }
                },
                "required": ["query"],
            },
        },
    }
]
