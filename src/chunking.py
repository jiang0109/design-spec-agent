"""分块策略：把知识库切成可检索的 chunk。

为什么需要三种策略
------------------
分块是 RAG 里影响效果最大的一步，比换模型重要得多。但"哪个策略更好"不能凭
直觉回答 —— 必须实测。三种策略代表三种不同的假设：

  A `fixed`    固定长度 + 重叠。假设"位置无关，切匀就行"。
               优点：实现简单、chunk 大小可控。
               风险：会把一个规则从中间砍断，且切点与语义边界无关。

  B `heading`  按 `##` 二级标题切。假设"一个标题下就是一个完整语义单元"。
               优点：chunk 边界与文档结构对齐，天然带出处。
               风险：文档结构不规范时失效；短节会产生大量碎片。

  C `semantic` 按段落聚合成接近目标大小的块，遇标题强制断开。
               优点：兼顾语义完整与 chunk 大小。
               风险：段落过长时仍会切分；实现复杂度最高。

本模块只负责"怎么切"，不含检索与打分 —— 这样才能客观比较策略本身。

关键设计：chunk 是**自包含**的
-----------------------------
每个 chunk 都带上「文档 → 规则 id → 规则 title」的头部。原因是 Week 1 已经
踩过"模型改写出处标签"的坑：如果 chunk 里没有显式的出处，模型只能靠上下文
猜，溯源漂移率就会上去。把出处焊进 chunk 内容，是让引用可验证的结构性手段。
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
class Section:
    """知识库里的一个语义单元 = 一条规则（或一条案例）。

    prose 是"叙事通道"的正文；fm_block 是该条规则在 front-matter 中的
    结构化片段（YAML 文本），一并保留以便检索到精确取值。
    """

    doc_id: str
    doc_title: str
    section_id: str
    section_title: str
    prose: str
    fm_block: str = ""

    def header(self) -> str:
        return f"【{self.doc_title} / {self.section_title} / {self.section_id}】"


@dataclass
class Chunk:
    """可检索的片段。"""

    chunk_id: str
    doc_id: str
    section_id: str
    section_title: str
    content: str
    strategy: str
    chars: int = field(init=False)

    def __post_init__(self) -> None:
        self.chars = len(self.content)


# ======================================================================
# 加载：把 kb/*.md 解析成 Section 列表
# ======================================================================
def _split_front_matter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1], parts[2]


def _fm_top_scalar(fm: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.+)$", fm, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _fm_blocks(fm: str, kind: str) -> list[tuple[str, str, str]]:
    """把 front-matter 里的 rules/cases 切成 (id, title, 原始YAML文本块)。

    用缩进切分：一个条目从 `  - id: XXX` 开始，到下一个同级 `- id:` 或段落结束。
    """
    lines = fm.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{kind}:\s*$", line):
            start = i + 1
            break
    if start is None:
        return []

    blocks: list[tuple[str, str, str]] = []
    cur: list[str] = []
    cur_id = ""
    cur_title = ""

    def flush() -> None:
        if cur_id:
            blocks.append((cur_id, cur_title, "\n".join(cur).strip()))

    for line in lines[start:]:
        if line and not line.startswith((" ", "\t", "-")):
            break
        m = re.match(r"^\s*-\s+id:\s*(\S+)\s*$", line)
        if m:
            flush()
            cur_id, cur_title = m.group(1), ""
            cur = [line.strip()]
            continue
        if cur_id:
            mt = re.match(r"^\s+title:\s*(.+)$", line)
            if mt and not cur_title:
                cur_title = mt.group(1).strip()
            cur.append(line)
    flush()
    return blocks


def load_sections(kb_dir: Path = KB_DIR, include_format: bool = False) -> list[Section]:
    """加载全部知识库文件为 Section 列表。

    默认排除 `_format.md`：它是写给作者看的格式契约，不是规范知识本身，
    混进检索语料会污染评测（它包含大量示例文本，容易被误召回）。
    """
    sections: list[Section] = []

    for path in sorted(kb_dir.glob("*.md")):
        if path.name.startswith("_") and not include_format:
            continue
        text = path.read_text(encoding="utf-8")
        fm, body = _split_front_matter(text)
        doc_id = _fm_top_scalar(fm, "doc_id") or path.stem
        doc_title = _fm_top_scalar(fm, "title") or path.stem
        kind = "cases" if re.search(r"^cases:\s*$", fm, re.MULTILINE) else "rules"

        blocks = {bid: (btitle, btext) for bid, btitle, btext in _fm_blocks(fm, kind)}

        # 按 `## ` 切正文
        parts = re.split(r"^##\s+(.+)$", body, flags=re.MULTILINE)
        # parts = [前言, 标题1, 正文1, 标题2, 正文2, ...]
        for i in range(1, len(parts) - 1, 2):
            sec_title = parts[i].strip()
            prose = parts[i + 1].strip()
            # 用 title 反查 section_id
            sec_id = ""
            fm_block = ""
            for bid, (btitle, btext) in blocks.items():
                if btitle == sec_title:
                    sec_id, fm_block = bid, btext
                    break
            sections.append(
                Section(
                    doc_id=doc_id,
                    doc_title=doc_title,
                    section_id=sec_id or f"{doc_id}#{sec_title}",
                    section_title=sec_title,
                    prose=prose,
                    fm_block=fm_block,
                )
            )
    return sections


# ======================================================================
# 策略 A：固定长度 + 重叠
# ======================================================================
def chunk_fixed(
    sections: list[Section], size: int = 400, overlap: int = 60
) -> list[Chunk]:
    """把所有正文拼成一条流，按固定字符数切分。

    这是**基线策略**：它完全无视文档结构。
    保留它不是为了用，而是为了证明"结构信息有价值" ——
    如果按标题切分的效果并不比它好，那说明知识库的结构本身没被利用起来。
    """
    chunks: list[Chunk] = []
    step = max(1, size - overlap)

    for sec in sections:
        text = f"{sec.header()}\n{sec.prose}"
        if len(text) <= size:
            chunks.append(
                Chunk(
                    chunk_id=f"fixed:{sec.section_id}:0",
                    doc_id=sec.doc_id,
                    section_id=sec.section_id,
                    section_title=sec.section_title,
                    content=text,
                    strategy="fixed",
                )
            )
            continue
        for j, start in enumerate(range(0, len(text), step)):
            piece = text[start : start + size]
            if not piece.strip():
                break
            chunks.append(
                Chunk(
                    chunk_id=f"fixed:{sec.section_id}:{j}",
                    doc_id=sec.doc_id,
                    section_id=sec.section_id,
                    section_title=sec.section_title,
                    content=piece,
                    strategy="fixed",
                )
            )
            if start + size >= len(text):
                break
    return chunks


# ======================================================================
# 策略 B：按标题层级切分（当前基线首选）
# ======================================================================
def chunk_heading(sections: list[Section]) -> list[Chunk]:
    """一个 `##` 节 = 一个 chunk。

    自包含性最好：规则说明、为什么、正反例都在同一个 chunk 里，
    模型拿到就能判定，不需要跨 chunk 拼接。代价是 chunk 大小不均。
    """
    chunks: list[Chunk] = []
    for sec in sections:
        content = f"{sec.header()}\n{sec.prose}"
        if sec.fm_block:
            # 结构化通道附在后面：精确取值（allowed/pattern/mapping）靠它
            content += f"\n\n--- 结构化参数 ---\n{sec.fm_block}"
        chunks.append(
            Chunk(
                chunk_id=f"heading:{sec.section_id}",
                doc_id=sec.doc_id,
                section_id=sec.section_id,
                section_title=sec.section_title,
                content=content,
                strategy="heading",
            )
        )
    return chunks


# ======================================================================
# 策略 C：段落语义聚合
# ======================================================================
def chunk_semantic(
    sections: list[Section], target: int = 600, max_chars: int = 1200
) -> list[Chunk]:
    """按段落聚合成接近 target 大小的块，超出 max_chars 时强制切分。

    与 A 的区别：切点只落在段落边界上，不会把一个句子砍断。
    与 B 的区别：允许把同一节内的多个短段落合并，避免碎片。
    节与节之间不合并 —— 跨规则合并会让 chunk 语义混杂，反而降低精度。
    """
    chunks: list[Chunk] = []

    for sec in sections:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", sec.prose) if p.strip()]
        if not paragraphs:
            continue

        buf: list[str] = []
        buf_len = 0
        part = 0

        def emit() -> None:
            nonlocal part
            if not buf:
                return
            body = "\n\n".join(buf)
            chunks.append(
                Chunk(
                    chunk_id=f"semantic:{sec.section_id}:{part}",
                    doc_id=sec.doc_id,
                    section_id=sec.section_id,
                    section_title=sec.section_title,
                    content=f"{sec.header()}\n{body}",
                    strategy="semantic",
                )
            )
            part += 1

        for para in paragraphs:
            # 单个段落就超过上限：只能硬切，但按句号优先切
            if len(para) > max_chars:
                emit()
                buf, buf_len = [], 0
                sentences = re.split(r"(?<=[。；！？])", para)
                piece = ""
                for s in sentences:
                    if len(piece) + len(s) > max_chars and piece:
                        chunks.append(
                            Chunk(
                                chunk_id=f"semantic:{sec.section_id}:{part}",
                                doc_id=sec.doc_id,
                                section_id=sec.section_id,
                                section_title=sec.section_title,
                                content=f"{sec.header()}\n{piece}",
                                strategy="semantic",
                            )
                        )
                        part += 1
                        piece = ""
                    piece += s
                if piece.strip():
                    buf = [piece]
                    buf_len = len(piece)
                continue

            if buf_len + len(para) > target and buf:
                emit()
                buf, buf_len = [], 0
            buf.append(para)
            buf_len += len(para)

        emit()

    return chunks


def _fm_list_items(block: str, field: str) -> list[str]:
    """从一个 YAML 片段里提取某字段下的 `- value` 列表项。

    纯行扫描：只在「字段名所在行」之后、遇到同级新字段之前收集 `- ` 行。
    不依赖 YAML 库（项目环境无 PyYAML，且这三个字段结构固定）。
    """
    lines = block.splitlines()
    out: list[str] = []
    start = None
    base_indent = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)" + re.escape(field) + r":\s*$", line)
        if m:
            start = i + 1
            base_indent = len(m.group(1))
            break
    if start is None:
        return []
    for line in lines[start:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        # 同级或更浅的非列表行 = 字段结束
        if indent <= base_indent and not line.lstrip().startswith("-"):
            break
        m_item = re.match(r"^\s*-\s*(.+?)\s*$", line)
        if m_item:
            out.append(m_item.group(1))
    return out


def compact_params(fm_block: str) -> str:
    """把结构化参数压成精简形式。

    为什么需要它（本轮实测数据）
    ---------------------------
    原始 front-matter 块平均 **598 字符**，而正文平均只有 **395 字符**。
    38 节的 heading chunk 总字符拆解：结构化参数 **58%**、正文 39%、标题头 3%。

    也就是说：**chunk 之所以"太大"，主因不是正文长，而是结构化参数块过大。**
    进一步看，这个块里 statement / rationale / positive_case / negative_case
    四项在正文里**已经完整重复了一遍**（正文本来就是按这四项行文的）。

    所以只保留正文没有的信息：params（精确取值）、applies_when（适用条件）、
    exceptions（例外）。这三项正是"正文讲不清、但判定必须精确"的部分。
    """
    parts: list[str] = []

    # params：逐行收集（含子键），它是精确校验的唯一来源
    lines = fm_block.splitlines()
    start = None
    base_indent = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)params:\s*$", line)
        if m:
            start = i + 1
            base_indent = len(m.group(1))
            break
    if start is not None:
        body: list[str] = []
        for line in lines[start:]:
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent and not line.lstrip().startswith("-"):
                break
            body.append(line.strip())
        if body:
            parts.append("params: " + " | ".join(body))

    for field, label in (("applies_when", "适用"), ("exceptions", "例外")):
        items = _fm_list_items(fm_block, field)
        if items:
            parts.append(f"{label}: " + "; ".join(items))

    return "\n".join(parts)


def _split_by_label(text: str) -> list[str]:
    """按 `**粗体标签。**` 切成语义单元；没有标签时退回按空行切。"""
    if "**" in text:
        pieces = re.split(r"(?=\*\*[^*]+\*\*)", text)
        return [p.strip() for p in pieces if p.strip()]
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


# ======================================================================
# 策略 D：标题切分 + 超长节二次切分（针对 heading 粒度过粗的问题）
# ======================================================================
def chunk_heading_split(
    sections: list[Section],
    max_chars: int = 500,
    compact: bool = True,
    repeat_params: bool = True,
) -> list[Chunk]:
    """在 `##` 切分基础上，对超出 max_chars 的节按粗体标签/段落二次切分。

    设计要点
    --------
    1. **沿语义边界切**，不按字符数硬切。锚点是 `**为什么这么规定。**`
       `**正例**` `**反例**` `**经验。**` 这类粗体标签 —— 它们是作者标注的语义分段点。
    2. **每个子块重复标题头**（`【文档 / 规则名 / ID】`），保证任何子块单独被检索到
       时，出处依然明确、可溯源。
    3. `repeat_params=True`（默认）时**每个子块都带结构化参数**。
       早期版本为了省字符只挂在第一个子块上，结果是：
       72 个 chunk 里 49 个没有「例外」字段 —— 而判定器只看到被召回的 chunk，
       于是**例外条款在多数情况下根本不可见**。
       实测后果：TYPO-002 的例外明确写着"表格单元格不参与行高倍数校验"，
       但判定器看不到它，只能凭正文措辞猜，把"表格行高"误判为不适用，
       造成端到端功能回归（见 eval/e2e_report.md）。
       **省下的那点字符，代价是关键约束不可见 —— 不划算。**
    4. 整体不超过 max_chars 的节不切分。

    compact=True 用 `compact_params()` 精简参数；False 则附完整 front-matter 块。
    """
    chunks: list[Chunk] = []

    for sec in sections:
        params_text = ""
        if sec.fm_block:
            params_text = (
                compact_params(sec.fm_block) if compact else sec.fm_block
            )

        units = _split_by_label(sec.prose)
        groups: list[list[str]] = []
        buf: list[str] = []
        buf_len = 0

        for unit in units:
            # 单个单元就超限：自成一组
            if len(unit) > max_chars:
                if buf:
                    groups.append(buf)
                    buf, buf_len = [], 0
                groups.append([unit])
                continue
            if buf_len + len(unit) > max_chars and buf:
                groups.append(buf)
                buf, buf_len = [], 0
            buf.append(unit)
            buf_len += len(unit) + 1
        if buf:
            groups.append(buf)

        if not groups:
            continue

        for gi, group in enumerate(groups):
            content = f"{sec.header()}\n" + "\n\n".join(group)
            # 结构字段：默认每个子块都带（保证关键约束始终可见）
            if params_text and (repeat_params or gi == 0):
                content += f"\n\n--- 结构化参数 ---\n{params_text}"
            chunks.append(
                Chunk(
                    chunk_id=f"heading_split:{sec.section_id}:{gi}",
                    doc_id=sec.doc_id,
                    section_id=sec.section_id,
                    section_title=sec.section_title,
                    content=content,
                    strategy="heading_split",
                )
            )

    return chunks


STRATEGIES = {
    "fixed": chunk_fixed,
    "heading": chunk_heading,
    "heading_split": chunk_heading_split,
    "semantic": chunk_semantic,
}


def build_chunks(strategy: str, sections: list[Section] | None = None, **kw) -> list[Chunk]:
    if strategy not in STRATEGIES:
        raise ValueError(f"未知策略 {strategy}，可选：{list(STRATEGIES)}")
    secs = sections if sections is not None else load_sections()
    return STRATEGIES[strategy](secs, **kw)


# ======================================================================
# 自检入口
# ======================================================================
def main() -> int:
    import sys

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    sections = load_sections()
    print(f"知识库：{len(sections)} 个语义单元（Section）\n")

    missing_id = [s.section_title for s in sections if "#" in s.section_id]
    if missing_id:
        print(f"⚠️ 以下分节未能对上规则 id（标题与 front-matter title 不一致）：")
        for t in missing_id:
            print(f"    - {t}")
        print()

    print(f"{'策略':<10} {'chunk 数':>8} {'平均字符':>9} {'最小':>6} {'最大':>6}")
    print("-" * 46)
    for name in STRATEGIES:
        chunks = build_chunks(name, sections)
        sizes = [c.chars for c in chunks]
        print(
            f"{name:<10} {len(chunks):>8} {sum(sizes)//len(sizes):>9} "
            f"{min(sizes):>6} {max(sizes):>6}"
        )

    print("\n各策略首个 chunk 预览（heading）：")
    first = build_chunks("heading", sections)[0]
    print(f"  id={first.chunk_id}  chars={first.chars}")
    print("  " + first.content[:200].replace("\n", "\n  "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
