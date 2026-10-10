"""修复 cases.md：把正文「经验。」段落回填到 front-matter 的 lesson 字段。

背景（真实缺陷，不是假设）
--------------------------
`kb/cases.md` 由生成器产出后，机器校验发现 15 条案例里只有 CASE-001 带
`lesson` 字段，其余 14 条缺失 —— 但生成器汇报的是"全部含 7 字段"。
而正文部分的「**经验。**」段落 15 条俱全。

也就是说：**内容没丢，只是结构化通道没同步。**
这类"人读的通道完整、机读的通道残缺"的问题，肉眼审阅正文时完全看不出来，
只有机器校验能发现。这再次印证了"先验证数据，再相信结论"。

修复策略
--------
正文已有权威内容，所以**回填**而不是重写：让生成器重写整篇会引入新风险
（可能改坏已经正确的部分），而回填是幂等的、可复核的、改动面最小的。

脚本自身也做校验：
  - 正文经验数必须等于案例数，否则拒绝写入（宁可不动，也不要写坏）
  - 已存在 lesson 的条目不覆盖
  - 写入后重新解析，确认 15/15 齐全

用法
----
    python -m eval.fix_cases_lesson --dry-run   # 只看会改什么
    python -m eval.fix_cases_lesson             # 实际写入
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.validate_kb import _items, _parse_front_matter  # noqa: E402

CASES = Path(__file__).resolve().parent.parent / "kb" / "cases.md"


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 cases.md 的 lesson 字段")
    parser.add_argument("--dry-run", action="store_true", help="只显示改动，不写文件")
    args = parser.parse_args()

    text = CASES.read_text(encoding="utf-8")
    lines = text.splitlines()

    # --- 1. 从正文提取每个案例的「经验。」段落 ---
    lessons: dict[str, str] = {}
    current_id: str | None = None
    for line in lines:
        m_case = re.match(r"^>\s*案例编号\s+(CASE-\d{3})", line.strip())
        if m_case:
            current_id = m_case.group(1)
            continue
        m_lesson = re.match(r"^\*\*经验。\*\*\s*(.+)$", line.strip())
        if m_lesson and current_id:
            lessons[current_id] = m_lesson.group(1).strip()

    # --- 2. 解析 front-matter，找出缺 lesson 的条目 ---
    fm, _ = _parse_front_matter(text)
    items = _items(fm, "cases")
    all_ids = [(it.get("id") or "").strip() for it in items]
    missing = [i for i in all_ids if i and not items[all_ids.index(i)].get("lesson")]

    print(f"案例总数        : {len(items)}")
    print(f"正文经验段落    : {len(lessons)}")
    print(f"缺 lesson 的条目: {len(missing)} → {missing if missing else '无'}")

    # --- 3. 完整性校验：宁可不动，也不要写坏 ---
    if len(lessons) != len(items):
        print(
            f"\n❌ 正文经验段落数({len(lessons)}) 与案例数({len(items)}) 不一致，拒绝写入。",
            file=sys.stderr,
        )
        return 1

    uncovered = [i for i in all_ids if i not in lessons]
    if uncovered:
        print(f"\n❌ 以下案例在正文里找不到经验段落：{uncovered}", file=sys.stderr)
        return 1

    if not missing:
        print("\n✅ 无需修复，所有案例均已包含 lesson 字段")
        return 0

    # --- 4. 回填：在每个案例的 fix 行后插入 lesson 行 ---
    # 定位 front-matter 的结束位置，正文不动
    fm_end = 0
    if lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm_end = i
                break

    out: list[str] = []
    cur_case: str | None = None
    inserted = 0
    for idx, line in enumerate(lines):
        m_id = re.match(r"^\s*-\s+id:\s*(CASE-\d{3})\s*$", line)
        if m_id and idx < fm_end:
            cur_case = m_id.group(1)
            out.append(line)
            continue
        out.append(line)
        # 在 fix 行之后插入 lesson（仅 front-matter 区、仅缺失的条目）
        if (
            cur_case
            and idx < fm_end
            and re.match(r"^\s+fix:\s*", line)
            and cur_case in missing
        ):
            indent = re.match(r"^(\s*)", line).group(1)
            lesson = lessons[cur_case]
            # YAML：以引号包裹，避免全角冒号以外的字符引发歧义
            safe = lesson.replace('"', '\\"')
            out.append(f'{indent}lesson: "{safe}"')
            inserted += 1
            cur_case = None  # 每个案例只插一次

    if inserted != len(missing):
        print(
            f"\n❌ 预期插入 {len(missing)} 条，实际 {inserted} 条，拒绝写入。",
            file=sys.stderr,
        )
        return 1

    new_text = "\n".join(out) + "\n"

    # --- 5. 写入前先复核：重新解析新内容，确认 15/15 齐全 ---
    fm2, _ = _parse_front_matter(new_text)
    items2 = _items(fm2, "cases")
    still_missing = [it.get("id") for it in items2 if not it.get("lesson")]
    if still_missing:
        print(f"\n❌ 回填后仍有缺失：{still_missing}，拒绝写入。", file=sys.stderr)
        return 1

    print(f"\n将插入 {inserted} 条 lesson（示例）：")
    for cid in missing[:3]:
        print(f'  {cid}: "{lessons[cid][:60]}..."')

    if args.dry_run:
        print("\n[dry-run] 未写入文件")
        return 0

    CASES.write_text(new_text, encoding="utf-8")
    print(f"\n✅ 已写入 {CASES.name}；回填后 {len(items2)}/{len(items2)} 条含 lesson")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
