"""相关性判定：对「问题 → chunk」做语义级打分，用于 rerank 与拒答。

要解决的问题（来自 retrieval_report.md 三、）
-------------------------------------------
检索层的拒答检验**全部配置都不可分**：
无覆盖题（如"表格行内边距"）召回的 top1 分数比可答题的均值还高。

根因是 BM25 只看词面重合。"表格行内边距"里含"内边距"，
于是命中了"卡片内边距"规范 —— **词面对上了，但语义上答不了这个问题**。

所以拒答无法靠调整 BM25 阈值实现，必须引入**判断"这段内容是否真的回答了问题"**
的能力。这就是相关性判定，它同时也就是 rerank 的基础。

设计取舍
--------
逐 chunk 单独调用（而非一次给多个 chunk 打分），理由：
  - 单次判定只依赖一个 chunk，结论稳定、不受列表顺序影响
  - 便于按需只为 top-k 调用，控制成本
代价是 API 调用次数 = 查询数 × k。本实验在 35 题 × top-5 的量级，成本可控。

打分档位只用 4 档（0-3），不用 0-100：档位越少，模型输出越稳定，
且下游只需要一个"够不够回答问题"的判据，不需要细粒度排序精度。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .chunking import Chunk
from .llm import ask_llm

# 相关性档位定义。措辞经过刻意设计：
# 关键是让模型把"提到了同一概念"与"能回答这个问题"区分开 ——
# 这正是 BM25 做不到、而拒答必须依赖的区分。
RELEVANCE_SYSTEM = """你是检索结果的相关性评审员。判断给定资料能否回答用户的问题。

按 0-3 打分，只输出 JSON：
{"score": <整数>, "reason": "<20字以内>"}

评分标准：
3 = 资料直接给出了问题的答案（含明确取值、规则或结论）
2 = 资料给出了相关规则，需要简单推导才能回答问题
1 = 资料提到了同一主题/术语，但没有回答该问题
0 = 资料与问题无关

关键要求：不要因为"提到了同一个词"就给高分。
如果资料讲的是**另一个对象或场景**的规则（例如问题问表格行内边距，
资料讲的是卡片内边距），即使术语相同，也应判 1 而不是 3。
只有在资料的规则**确实覆盖了问题所指的对象**时，才给 3。"""


@dataclass
class Relevance:
    chunk: Chunk
    score: int
    reason: str
    bm25_rank: int


def score_relevance(question: str, chunk: Chunk, temperature: float = 0.0) -> tuple[int, str]:
    """对单个 (问题, chunk) 打分，返回 (0-3, 理由)。"""
    user = (
        f"【问题】\n{question}\n\n"
        f"【资料出处】{chunk.section_title}（{chunk.section_id}）\n"
        f"【资料内容】\n{chunk.content}"
    )
    result = ask_llm(
        [
            {"role": "system", "content": RELEVANCE_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        json_mode=True,
    )
    raw = (result.message.content or "").strip()
    # 容错：模型偶尔仍会包 code fence
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        data = json.loads(raw)
        score = int(data.get("score", 0))
        reason = str(data.get("reason", ""))[:60]
    except (json.JSONDecodeError, ValueError, TypeError):
        return -1, f"解析失败: {raw[:50]}"
    return max(0, min(3, score)), reason


def rerank(
    question: str,
    candidates: list,
    top_k: int = 5,
    min_score: int = 2,
    verbose: bool = False,
) -> list[Relevance]:
    """对候选 chunk 做相关性打分并重排。

    返回按 (相关性 desc, BM25 排名 asc) 排序的结果 —— 平手时保留 BM25 的次序，
    这样在相关性判定失效时能自然退化为原始排序，不会比基线更差。
    """
    scored: list[Relevance] = []
    for c in candidates[:top_k]:
        score, reason = score_relevance(question, c.chunk)
        if verbose:
            print(f"    [{score}] {c.chunk.section_id}  {reason}")
        if score < 0:  # 解析失败：保留 BM25 次序，给中性分
            score = 1
        scored.append(Relevance(chunk=c.chunk, score=score, reason=reason, bm25_rank=c.rank))

    scored.sort(key=lambda r: (-r.score, r.bm25_rank))
    return scored
