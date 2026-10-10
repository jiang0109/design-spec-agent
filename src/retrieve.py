"""极简检索器：零第三方依赖，用于分块策略的快速对比。

为什么不用向量检索做这一步
--------------------------
1. 38 条规则 / 4 万字符的小规模语料，词汇检索已经能跑满召回 —— 上向量是浪费
2. chromadb 的默认 embedding 需要从 HuggingFace 下载约 80MB，引入网络依赖
3. 更重要的是：**设计规范是高度结构化、术语密集的文本**，
   查询词与规范用词高度重合（"内边距""对比度""camelCase"），
   这正是 BM25 的强项，也是它常常追平甚至超过向量检索的场景

所以这里用 BM25 作基线。等 Week 2 后半段接入向量做混合检索时，
BM25 的成绩就是那个"必须被打败的基线" —— 这比直接上向量更有说服力。

中文分词：零依赖的双粒度方案
----------------------------
中文没有空格，标准做法是分词（需要 jieba 等依赖）。这里改用
**单字 + 相邻双字** 的组合：
    卡片内边距  →  卡 片 内 边 距 | 卡片 片内 内边 边距
单字保证召回（不漏），双字提供精度（词序信息）。
这是 IR 里成熟的 CJK 处理方式，且不引入任何依赖。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .chunking import Chunk

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def tokenize(text: str) -> list[str]:
    """中英混排分词：ASCII 单词保持完整，CJK 取单字 + 相邻双字，数字整体保留。"""
    tokens: list[str] = []
    tokens.extend(w.lower() for w in _ASCII_WORD.findall(text))
    tokens.extend(_NUMBER.findall(text))

    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    for run in cjk_runs:
        tokens.extend(run)  # 单字
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))  # 相邻双字
    return tokens


@dataclass
class Scored:
    chunk: Chunk
    score: float
    rank: int


def split_fields(content: str) -> tuple[str, str, str]:
    """把 chunk 拆成 (标题头, 所有小标题, 正文)。

    chunk 结构是 `【文档 / 规则名 / ID】` + 正文，正文里可能还有 markdown 小标题。
    标题类字段是高质量信号：规则名称本身就是这条规范最精炼的语义摘要，
    "卡片内边距取值" 之于 query "卡片内边距允许哪几档"，匹配度远高于正文里的长句。
    """
    lines = content.splitlines()
    header = lines[0] if lines and lines[0].startswith("【") else ""
    headings: list[str] = []
    body: list[str] = []
    for line in lines[1:]:
        s = line.strip()
        if s.startswith("#"):
            headings.append(s.lstrip("# ").strip())
        else:
            body.append(line)
    return header, " ".join(headings), "\n".join(body)


class BM25:
    """标准 BM25（k1=1.5, b=0.75），在 chunk 语料上建立索引。"""

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b

        self.docs: list[Counter] = []
        self.lengths: list[int] = []
        df: Counter = Counter()

        for c in chunks:
            toks = tokenize(c.content)
            tf = Counter(toks)
            self.docs.append(tf)
            self.lengths.append(len(toks) or 1)
            for t in tf:
                df[t] += 1

        self.n = len(chunks) or 1
        self.avg_len = sum(self.lengths) / self.n
        # BM25 的 idf，加 0.5 平滑避免负值
        self.idf = {
            t: math.log(1 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()
        }

    # ------------------------------------------------------------------
    # 内部打分原语
    # ------------------------------------------------------------------
    def _score(self, q_tokens: list[str], tf: Counter, dl: int) -> float:
        s = 0.0
        for t in q_tokens:
            f = tf.get(t)
            if not f:
                continue
            idf = self.idf.get(t, 0.0)
            s += idf * (f * (self.k1 + 1)) / (
                f + self.k1 * (1 - self.b + self.b * dl / self.avg_len)
            )
        return s

    def search(self, query: str, top_k: int = 5) -> list[Scored]:
        q_tokens = tokenize(query)
        scores: list[tuple[float, int]] = []

        for i, tf in enumerate(self.docs):
            s = self._score(q_tokens, tf, self.lengths[i])
            if s > 0:
                scores.append((s, i))

        scores.sort(key=lambda x: (-x[0], x[1]))
        return [
            Scored(chunk=self.chunks[i], score=s, rank=r)
            for r, (s, i) in enumerate(scores[:top_k], 1)
        ]

    def search_field_weighted(
        self, query: str, title_weight: float = 1.0, top_k: int = 5
    ) -> list[Scored]:
        """双字段加权：正文 BM25 + title_weight × 标题头/小标题 BM25。

        title_weight = 0 时退化为普通 BM25，因此可以直接和 `search` 对比，
        证明增益确实来自标题字段而不是实现差异。
        """
        q_tokens = tokenize(query)
        pad = "。" * 40  # 固定长度填充，让标题字段的 dl 归一化保持稳定

        scored: list[tuple[float, int]] = []
        for i, c in enumerate(self.chunks):
            header, headings, _ = split_fields(c.content)
            title_text = f"{header} {headings}".strip()

            s_body = self._score(q_tokens, self.docs[i], self.lengths[i])
            if title_text:
                t_toks = tokenize(title_text)
                s_title = self._score(q_tokens, Counter(t_toks), len(t_toks) or 1)
            else:
                s_title = 0.0

            s = s_body + title_weight * s_title
            if s > 0:
                scored.append((s, i))

        scored.sort(key=lambda x: (-x[0], x[1]))
        return [
            Scored(chunk=self.chunks[i], score=s, rank=r)
            for r, (s, i) in enumerate(scored[:top_k], 1)
        ]
