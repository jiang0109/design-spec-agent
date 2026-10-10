"""知识库结构校验器（零第三方依赖）。

为什么需要它
------------
`kb/` 下是手写/生成的 YAML front-matter + Markdown 正文。没有校验的话，
一个缩进错误或重复 id 会以极隐蔽的方式传导到下游：
  - 评测集按 id 定位 ground truth，id 重复 → 评测结果静默错位
  - 分块实验对齐 `## 节标题` 与规则 title，标题不一致 → 召回统计失真

所以**先校验结构，再谈效果**。这和 Week 1 的教训是同一条：
先验证数据，再相信基于数据的结论。

实现说明
--------
这里刻意不依赖 PyYAML，用一个小型缩进解析器只提取需要的字段。
原因：项目运行环境可能没有 PyYAML，而校验器必须在任何环境下都能跑。
它是一个**校验工具**，不该成为新的依赖风险。

用法
----
    python -m eval.validate_kb
    python -m eval.validate_kb -v      # 打印每条规则摘要
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "kb"

# 条目字段的显式白名单。
# 必须显式列出，不能靠缩进猜测：条目字段的嵌套子键（params 的 allowed 等）
# 与字段本身缩进相同，只有"这个键名是不是条目字段"才能区分层级。
RULE_FIELDS = {
    "id",
    "title",
    "statement",
    "params",
    "applies_when",
    "exceptions",
    "positive_case",
    "negative_case",
    "rationale",
}
CASE_FIELDS = {"id", "title", "rule_ref", "violation", "impact", "fix", "lesson"}

# 规则文件必须存在的字段
RULE_REQUIRED = list(RULE_FIELDS)
CASE_REQUIRED = list(CASE_FIELDS)

# 模糊词黑名单：出现即说明该条不可判定
VAGUE_WORDS = ["合理", "适当", "尽量", "注意", "友好", "美观", "差不多", "大概", "酌情", "灵活处理"]

ID_RE = re.compile(r"^[A-Z]+-\d{3}$")


# ----------------------------------------------------------------------
# 最小 YAML 解析：只提取我们需要的字段与结构
# ----------------------------------------------------------------------
def _parse_front_matter(text: str) -> tuple[dict, str]:
    """返回 (front-matter 文本行, 正文)。front-matter 以 --- 包裹。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    return parts[1], parts[2]


def _top_level_scalars(fm: str) -> dict[str, str]:
    """提取 doc_id / title / version / scope 等顶层标量。"""
    out: dict[str, str] = {}
    for line in fm.splitlines():
        if not line or line.startswith((" ", "\t", "-", "#")):
            continue
        m = re.match(r"^([A-Za-z_][\w]*):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _items(fm: str, key: str) -> list[dict]:
    """提取 rules: / cases: 下的条目，返回 [{字段: "值拼接"}]。

    解析策略：先用**显式字段名集合**判断哪一行是条目的顶层字段，
    再把该字段之后、下一个字段之前的所有行都算作它的值。

    为什么不用纯缩进判断：条目字段的嵌套子键（如 params 的 allowed）
    与字段本身缩进相同，纯缩进无法区分。显式字段名集合是唯一可靠的办法。

    踩过的坑：最初用 `setdefault(k, v)` 只取字段同一行的值，导致
    `negative_case:` 这类「键在一行、值在下一行」的字段被误判为空，
    校验器对所有条目报了假阳性。**校验器自己也需要被验证。**
    """
    lines = fm.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{key}:\s*$", line):
            start = i + 1
            break
    if start is None:
        return []

    block: list[str] = []
    for line in lines[start:]:
        if line and not line.startswith((" ", "\t", "-")):
            break
        block.append(line)

    known = CASE_FIELDS if key == "cases" else RULE_FIELDS
    items: list[dict] = []
    cur: dict | None = None
    field: str | None = None
    buf: list[str] = []
    entry_indent: int | None = None

    def flush() -> None:
        if cur is not None and field is not None:
            joined = "\n".join(buf).strip()
            if joined in (">-", ">", "|", "|-"):
                joined = ""
            cur[field] = joined

    for line in block:
        if not line.strip():
            continue
        m_new = re.match(r"^(\s*)-\s+([A-Za-z_][\w]*):\s*(.*)$", line)
        if m_new and m_new.group(2) in known:
            flush()
            entry_indent = len(m_new.group(1).replace("\t", "  "))
            cur = {m_new.group(2): m_new.group(3).strip()}
            items.append(cur)
            field = m_new.group(2)
            buf = [m_new.group(3)]  # 必须带上首行的值，否则 id 会被冲掉
            continue
        if cur is None or entry_indent is None:
            continue
        # 关键：条目字段必须比「- 」这一行**更深的缩进**。
        # 注意 entry_indent 取的是短横线所在缩进（如 2），而字段在其下一层（如 4），
        # 所以必须用 >= entry_indent + 2，不能用 == entry_indent
        # （最初写成相等判断，导致所有字段都没被识别，整个条目被当成 id 的值）。
        # 更深层的子键（如 params 下的 allowed）虽然缩进也满足条件，
        # 但它们的键名不在 known 白名单里，因此不会误判。
        cur_indent = len(line) - len(line.lstrip())
        m_field = re.match(r"^([A-Za-z_][\w]*):\s*(.*)$", line.strip())
        if m_field and cur_indent >= entry_indent + 2 and m_field.group(1) in known:
            flush()
            field = m_field.group(1)
            cur.setdefault(field, "")
            buf = [m_field.group(2)]
            continue
        buf.append(line.strip())
    flush()
    return items


def _prose_sections(body: str) -> list[str]:
    """提取正文里 `## ` 二级标题。"""
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+)$", body, re.MULTILINE)]


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
def validate_file(path: Path, verbose: bool = False) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_front_matter(text)

    if not fm:
        return [f"{path.name}: 缺少 YAML front-matter（文件需以 --- 开头）"]

    meta = _top_level_scalars(fm)
    for key in ("doc_id", "title", "version", "scope"):
        if key not in meta:
            errors.append(f"{path.name}: front-matter 缺少顶层字段 `{key}`")

    is_case_file = "cases:" in fm
    items = _items(fm, "cases" if is_case_file else "rules")
    required = CASE_REQUIRED if is_case_file else RULE_REQUIRED
    kind = "案例" if is_case_file else "规则"

    if not items:
        errors.append(f"{path.name}: 未解析到任何{kind}条目")

    ids: list[str] = []
    for it in items:
        rid = (it.get("id") or "").strip().strip('"').strip("'")
        if not rid:
            errors.append(f"{path.name}: 有{kind}条目缺少 id")
            continue
        ids.append(rid)
        if not ID_RE.match(rid):
            errors.append(f"{path.name}: id `{rid}` 不符合 <CATEGORY>-<三位序号> 格式")

        for field in required:
            if field not in it:
                errors.append(f"{path.name} [{rid}]: 缺少字段 `{field}`")

        statement = it.get("statement", "")
        if not is_case_file and statement:
            for word in VAGUE_WORDS:
                if word in statement:
                    errors.append(
                        f"{path.name} [{rid}]: statement 含模糊词「{word}」，不可判定"
                    )

        # 反例必须给出具体内容
        neg = it.get("negative_case") or it.get("violation") or ""
        if neg in ("", ">-", ">", "|", "|-"):
            errors.append(f"{path.name} [{rid}]: 反例/违规描述为空")

        if verbose:
            print(f"    {rid}  {it.get('title', '(无标题)')}")

    # 重复 id（文件内）
    dupes = {i for i in ids if ids.count(i) > 1}
    for d in sorted(dupes):
        errors.append(f"{path.name}: id 重复 `{d}`")

    # 正文分节与 title 对齐
    sections = _prose_sections(body)
    if not sections:
        errors.append(f"{path.name}: 正文没有任何 `## ` 二级标题")
    else:
        titles = {it.get("title", "").strip() for it in items}
        unmatched = [s for s in sections if s not in titles]
        if unmatched:
            errors.append(
                f"{path.name}: 正文分节标题与规则 title 不一致 → {unmatched}"
                "（会导致分块后无法与规则 id 对齐）"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库结构校验")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条规则摘要")
    args = parser.parse_args()

    files = sorted(p for p in KB.glob("*.md") if not p.name.startswith("_"))
    if not files:
        print(f"kb/ 下没有找到知识库文件：{KB}", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    all_ids: list[tuple[str, str]] = []  # (id, 文件名)

    print(f"校验 {len(files)} 个文件：{KB}\n")
    for path in files:
        if args.verbose:
            print(f"  {path.name}")
        errs = validate_file(path, verbose=args.verbose)
        all_errors.extend(errs)
        # 收集全局 id
        fm, _ = _parse_front_matter(path.read_text(encoding="utf-8"))
        key = "cases" if "cases:" in fm else "rules"
        for it in _items(fm, key):
            rid = (it.get("id") or "").strip()
            if rid:
                all_ids.append((rid, path.name))

    # 跨文件重复 id
    seen: dict[str, str] = {}
    for rid, fname in all_ids:
        if rid in seen and seen[rid] != fname:
            all_errors.append(f"id `{rid}` 在 {seen[rid]} 与 {fname} 中重复")
        seen[rid] = fname

    # cases 的 rule_ref 必须指向真实存在的规则 id
    rule_ids = {
        rid for rid, fname in all_ids if not fname.startswith("cases")
    }
    for path in files:
        if not path.name.startswith("cases"):
            continue
        fm, _ = _parse_front_matter(path.read_text(encoding="utf-8"))
        for it in _items(fm, "cases"):
            ref = (it.get("rule_ref") or "").strip()
            if ref and ref not in rule_ids:
                all_errors.append(
                    f"{path.name} [{it.get('id')}]: rule_ref `{ref}` 不存在于任何规则文件"
                )

    print(f"共 {len(all_ids)} 个条目（规则 + 案例）\n")

    if all_errors:
        print(f"❌ 发现 {len(all_errors)} 个问题：\n")
        for e in all_errors:
            print(f"  - {e}")
        return 1

    print("✅ 结构校验全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
