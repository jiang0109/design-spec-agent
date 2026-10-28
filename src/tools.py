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
# 真实检索接入（BM25 + 可选相关性闸门）
#
# 为什么需要闸门
# --------------
# BM25 只看词面重合。"数据表格的行内边距"含"内边距"，会命中"卡片内边距"规范，
# 分数很高但**适用对象不同**。实测（eval/retrieval_report.md）：
#   无覆盖题的 BM25 top1 分数（34.5）比可答题的最小值（13.9）还高 ——
#   信号方向是反的，阈值路线根本走不通。
#
# 而 LLM 相关性判定（0-3 分）能做到完全分离（可答最小 2 vs 无覆盖最大 1），
# 因此接入后 agent 才真正具备拒答能力。
#
# 两级检索（成本控制）
# --------------------
# 相关性判定每题 k 次 API 调用，是 BM25 的数十倍成本。
# 所以先用 BM25 粗排（funnel=10），只对最靠前的 top_k 个做相关性判定。
# ------------------------------------------------------------------
_INDEX_CACHE = None


def _get_index():
    """惰性构建 BM25 索引。知识库是静态文件，进程内构建一次即可。"""
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        from .chunking import build_chunks, load_sections
        from .retrieve import BM25

        sections = load_sections()
        # 用 heading_split 策略：评测显示它在等预算口径下最优（100%），
        # 且每个子块保留标题头，出处可溯源。
        chunks = build_chunks("heading_split", sections, max_chars=300)
        _INDEX_CACHE = BM25(chunks)
    return _INDEX_CACHE


def search_spec_bm25(query: str, top_k: int = 2) -> str:
    """直接用 BM25 检索（无相关性闸门）。保留作为对照基线。"""
    index = _get_index()
    hits = index.search(query, top_k=top_k)
    if not hits:
        return json.dumps(
            {"found": False, "message": "知识库中没有找到相关规范，请判为 uncertain 并说明缺少依据。"},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "found": True,
            "verified": False,
            "hits": [
                {
                    "section_id": h.chunk.section_id,
                    "title": h.chunk.section_title,
                    "content": h.chunk.content,
                    "score": round(h.score, 2),
                }
                for h in hits
            ],
        },
        ensure_ascii=False,
    )


def search_spec_verified(
    query: str,
    top_k: int = 2,
    min_relevance: int = 2,
    funnel: int = 10,
    max_judge: int = 10,
) -> str:
    """BM25 粗排 + LLM 相关性闸门 + 拒绝原因分类。

    返回值有三种形态，**生成层必须区别对待**：
      1. found=true                → 有适用规范，正常判定
      2. found=false + excluded    → 规则明确排除该对象 → **该对象不受约束**。
                                     这是有结论的，应按"无限制/合规"处理
      3. found=false + no_coverage → 知识库确实无法判定 → 才判 uncertain
                     / not_applicable

    ⚠️ `max_judge` 的历史教训（不要改小）
    --------------------------------------
    最初只对 top-2 做相关性判定以省成本，结果「通用规则 + 特定子对象」的查询
    被系统性误拒。实测（eval/probe_general_rule.py）：

        查询「主按钮文字色是否必须用语义令牌」
          rank 1  CASE-005  → relevance 1（案例讲背景色）
          rank 2  NAME-004  → relevance 1（只讲令牌命名）
          rank 3  COLOR-003 → relevance 3 ← 判对了，但根本没被送去判定

    提到 5 之后又有三例因 gold 落在第 6/8/10 名而误拒（eval/generation_set_expanded）：

        文章正文字号   → TYPO-001 排第 10
        placeholder 命名 → COMP-002 排第 8
        分隔线颜色令牌  → COLOR-003 排第 6

    所以放宽到 10（funnel 也是 10，即全部候选都判）。**真正的瓶颈是 BM25 排序质量**：
    通用规则在长查询下常被具体条目挤到第 6-10 名，判定范围只能跟着放宽。

    代价控制靠**早期退出**：一旦拿到 3 分命中（明确适用），或已累计 2 个命中就停。
    简单查询仍然只判 1-2 次。

    ⚠️ 扩大判定范围的已知风险：无覆盖的查询也会多看几个候选，
    更容易撞上一个"看起来沾边"的条目而造成过度自信（幻觉）。
    每次调整后都必须同时验证 `no_coverage` 一组没有退化 —— 见
    eval/generation_report.md 的分层指标。
    """
    from .relevance import classify_rejection, score_relevance

    index = _get_index()
    # 阶段 1：BM25 粗排（便宜），多取一些给闸门筛选
    candidates = index.search(query, top_k=funnel)
    if not candidates:
        return json.dumps(
            {
                "found": False,
                "reject_reason": "no_coverage",
                "message": "知识库中完全找不到相关内容。请判 uncertain 并把 evidence 留空。",
            },
            ensure_ascii=False,
        )

    # 阶段 2：按 BM25 顺序判定，带早期退出
    verified = []
    passed = []
    judged = 0
    for c in candidates[:max_judge]:
        score, reason = score_relevance(query, c.chunk)
        judged += 1
        entry = {
            "section_id": c.chunk.section_id,
            "title": c.chunk.section_title,
            "content": c.chunk.content,
            "relevance": score,
            "relevance_reason": reason,
            "bm25_score": round(c.score, 2),
            "_chunk": c.chunk,
        }
        verified.append(entry)
        if score >= min_relevance:
            passed.append(entry)
            # 早期退出：3 分是明确适用，1 个就够；否则拿到 2 个即可
            if score >= 3 or len(passed) >= 2:
                break

    if passed:
        hits = passed[:top_k]
        for v in hits:
            v.pop("_chunk", None)
        return json.dumps(
            {"found": True, "verified": True, "judged": judged, "hits": hits},
            ensure_ascii=False,
        )

    # 阶段 3：全被拒 —— 判定**为什么**被拒。这决定生成层该怎么做。
    # 以 BM25 排名第一的候选作代表（它是最可能"本该适用"的那条）
    lead_chunk = verified[0]["_chunk"]
    reject_reason, detail = classify_rejection(query, lead_chunk)

    if reject_reason == "excluded":
        message = (
            "检索到的规范**明确把该对象列为例外**，说明它不受这条规则约束。"
            "这是一个有效结论：请判 pass，并在 reason 里说明「该对象被 XX 规范明确排除」。"
            "不要判 uncertain。"
        )
    else:
        message = (
            "知识库没有能判定该对象的规范（检索到的内容只是主题相关，并非适用规则）。"
            "请判 uncertain，把 evidence 留空，并说明知识库缺少对应规范。"
        )

    return json.dumps(
        {
            "found": False,
            "verified": True,
            "reject_reason": reject_reason,
            "reject_detail": detail,
            "leading_candidate": {
                "section_id": lead_chunk.section_id,
                "title": lead_chunk.section_title,
                "relevance": verified[0]["relevance"],
                "relevance_reason": verified[0]["relevance_reason"],
            },
            "message": message,
        },
        ensure_ascii=False,
    )


# ------------------------------------------------------------------
# 工具注册表：函数实现与 JSON Schema 一一对应
# ------------------------------------------------------------------
TOOL_IMPLS = {
    "search_spec": search_spec,
    "search_spec_verified": search_spec_verified,
}

# 相关性闸门的默认开关。由 run_agent(llm_verify=...) 覆盖，
# 便于用同一份代码跑"接入前 / 接入后"的对照实验。
#
# 默认开启的依据（不是拍脑袋）：
#   对照实验（python -m eval.run_e2e --docs e2e_confusable.md）显示
#     闸门关闭（纯 BM25）：表格行高倍数 → fail   ← 误判
#     闸门开启（含拒绝原因）：表格行高 1.6 倍 → pass  ← 正确
#   原因：TYPO-002 的例外写着"表格单元格不参与行高倍数校验"，
#   即该对象不受约束 —— 这是有结论的，不该判违规。
#   纯 BM25 会把这条例外规则当成适用依据，反而得出错误结论。
#
# 代价：每题额外 2 次（相关性）~3 次（含拒绝原因）LLM 调用，
# 端到端实测耗时约增加 2.9s/份（5.5s → 9.1s）。
VERIFY_ENABLED = True


def dispatch_tool(name: str, args: dict, verify: bool | None = None) -> str | None:
    """按开关执行工具调用。返回 None 表示工具不存在。

    verify 显式传入优先；为 None 时回退到模块默认 VERIFY_ENABLED。
    为什么用显式参数而不是改模块全局：早期版本直接写 `_tools.VERIFY_ENABLED = llm_verify`，
    结果跑完一次对照实验后全局状态就被永久改成那次的值 ——
    后续调用的行为取决于"上一次实验跑的是哪一组"，属于隐藏状态污染。
    """
    if name == "search_spec_verified":
        use_verify = VERIFY_ENABLED if verify is None else verify
        if use_verify:
            return search_spec_verified(**args)
        return search_spec_bm25(**args)
    fn = TOOL_IMPLS.get(name)
    return fn(**args) if fn else None


TOOLS_SCHEMA_VERIFIED = [
    {
        "type": "function",
        "function": {
            "name": "search_spec_verified",
            "description": (
                "检索设计规范知识库，并验证结果是否**真的适用于**你要查的对象。"
                "任何关于间距、颜色、字号、组件属性、命名的判定，都必须先调用本工具。\n\n"
                "返回结果有三种情况，处理方式完全不同：\n"
                "1) found=true：有适用规范，据此判定。\n"
                "2) found=false 且 reject_reason=excluded："
                "检索到的规范**明确把该对象列为例外**，即该对象不受这条规则约束。"
                "这是一个有效结论，应判 pass 并说明被哪条规范排除，**不要判 uncertain**。\n"
                "3) found=false 且 reject_reason=no_coverage/not_applicable："
                "知识库无法判定该对象，应判 uncertain 并把 evidence 留空。"
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

# 对照组：同样的工具名，但描述里不提"验证覆盖"，且执行时走纯 BM25。
# 保持工具名一致，确保两组唯一差异是"有无相关性闸门"。
TOOLS_SCHEMA_NO_VERIFY = [
    {
        "type": "function",
        "function": {
            "name": "search_spec_verified",
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


def active_schema(verify: bool) -> list[dict]:
    return TOOLS_SCHEMA_VERIFIED if verify else TOOLS_SCHEMA_NO_VERIFY
