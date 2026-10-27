"""冒烟测试：一条命令验证全部离线不变量，零 API 调用。

为什么需要它
------------
本项目在开发中反复出现"改动 A 破坏了 B"：
  - 改分块策略后要手动重跑 `run_eval`
  - 改判定器后要手动重跑 `probe_*`
  - 改 schema 后要手动确认输出仍能被解析

每次都靠人记得去查，迟早会漏。所以把**不依赖 LLM 的不变量**全部收敛成一条命令：

    python -m eval.test_smoke

任何一项失败即返回非零退出码，可挂到提交前检查。

覆盖的不变量
------------
  1  所有模块可导入、可编译
  2  知识库结构合法（id 唯一、字段齐全、正文分节与 title 对齐、rule_ref 有效）
  3  front-matter 解析器行为正确（含跨行值与嵌套子键）
  4  评测集自检：gold 存在、不可答题无 gold、覆盖全部 23 条规则
  5  四种分块策略均可构建，chunk 非空且 section_id 合法
  6  结构化字段（适用/例外）在每个规则子块中都可见
  7  检索基线不退化（等预算命中率不低于阈值）
  8  字段加权在 weight=0 时与原始 BM25 完全一致（实现等价性）

注意：本测试**不覆盖**任何需要 LLM 的行为（判定质量、拒答分类）。
那些必须用 `probe_*.py` 真实调用，成本更高，不适合每次提交都跑。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        PASS.append((name, detail))
        print(f"  ✅ {name}" + (f"  —— {detail}" if detail else ""))
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"  ❌ {name}  —— {e}")
    except Exception as e:  # noqa: BLE001 —— 冒烟测试要报出任何异常
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  ❌ {name}  —— {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 1
def t_imports() -> str:
    mods = [
        "src.schemas", "src.llm", "src.tools", "src.agent",
        "src.chunking", "src.retrieve", "src.relevance",
        "eval.validate_kb", "eval.run_eval", "eval.run_e2e",
    ]
    for m in mods:
        importlib.import_module(m)
    return f"{len(mods)} 个模块"


# ---------------------------------------------------------------- 2
def t_kb() -> str:
    from eval import validate_kb as V

    files = sorted(p for p in (ROOT / "kb").glob("*.md") if not p.name.startswith("_"))
    assert files, "kb/ 下没有知识库文件"
    errs: list[str] = []
    ids: list[tuple[str, str]] = []
    for p in files:
        errs += V.validate_file(p)
        fm, _ = V._parse_front_matter(p.read_text(encoding="utf-8"))
        key = "cases" if "cases:" in fm else "rules"
        for it in V._items(fm, key):
            rid = (it.get("id") or "").strip()
            if rid:
                ids.append((rid, p.name))
    seen: dict[str, str] = {}
    for rid, fn in ids:
        if rid in seen and seen[rid] != fn:
            errs.append(f"id {rid} 在 {seen[rid]} 与 {fn} 重复")
        seen[rid] = fn
    assert not errs, f"{len(errs)} 个问题：{errs[:3]}"
    return f"{len(files)} 文件 / {len(ids)} 条目"


# ---------------------------------------------------------------- 3
def t_parser() -> str:
    from eval.test_parser import main as parser_main

    rc = parser_main()
    assert rc == 0, "解析器自测未通过"
    return "跨行值 + 嵌套子键均可解析"


# ---------------------------------------------------------------- 4
def t_dataset() -> str:
    from src.chunking import load_sections

    qs = json.loads((ROOT / "eval" / "retrieval_set.json").read_text(encoding="utf-8"))["questions"]
    known = {s.section_id for s in load_sections()}
    rules = {x for x in known if not x.startswith("CASE")}
    errs = []
    for q in qs:
        if q.get("answerable", True):
            if q.get("gold_section") not in known:
                errs.append(f"{q['id']} gold 不存在")
        elif q.get("gold_section") is not None:
            errs.append(f"{q['id']} 不可答却给了 gold")
    assert not errs, str(errs[:3])
    covered = {q["gold_section"] for q in qs if q.get("answerable", True)}
    missing = rules - covered
    assert not missing, f"未覆盖规则：{sorted(missing)}"
    return f"{len(qs)} 题 / 覆盖 {len(rules)} 条规则"


# ---------------------------------------------------------------- 5
def t_chunking() -> str:
    from src.chunking import STRATEGIES, build_chunks, load_sections

    secs = load_sections()
    counts = {}
    for name in STRATEGIES:
        cs = build_chunks(name, secs)
        assert cs, f"{name} 未产出任何 chunk"
        for c in cs:
            assert c.content.strip(), f"{name} 出现空 chunk {c.chunk_id}"
            assert c.section_id, f"{name} chunk 缺 section_id"
            assert c.chars > 0, f"{name} chunk 长度为 0"
        counts[name] = len(cs)
    return "，".join(f"{k}={v}" for k, v in counts.items())


# ---------------------------------------------------------------- 6
def t_structured_fields() -> str:
    from src.chunking import build_chunks, compact_params, load_sections

    secs = [s for s in load_sections() if not s.section_id.startswith("CASE")]
    chunks = build_chunks("heading_split", secs, max_chars=300)
    missing = [c.chunk_id for c in chunks if "例外:" not in c.content]
    assert not missing, f"{len(missing)} 个 chunk 缺例外字段，例如 {missing[:2]}"
    # compact_params 必须真的压缩
    s = secs[0]
    compact = compact_params(s.fm_block)
    assert len(compact) < len(s.fm_block), "compact_params 未起到压缩作用"
    assert "params:" in compact, "compact_params 丢失了 params"
    return f"{len(chunks)} 个规则 chunk 全部含例外；压缩率 " \
           f"{100 - len(compact) * 100 // len(s.fm_block)}%"


# ---------------------------------------------------------------- 7
def t_retrieval_baseline() -> str:
    from eval.run_eval import evaluate
    from src.chunking import build_chunks, load_sections

    qs = json.loads((ROOT / "eval" / "retrieval_set.json").read_text(encoding="utf-8"))["questions"]
    secs = load_sections()
    out = []
    for name, kw in (("fixed", {}), ("heading_split", {"max_chars": 300}), ("semantic", {})):
        r = evaluate(build_chunks(name, secs, **kw), qs, 2000)
        assert r["budget"] >= 0.85, f"{name} 等预算命中率跌到 {r['budget']*100:.0f}%（阈值 85%）"
        out.append(f"{name}={r['budget']*100:.0f}%")
    return "，".join(out) + "（阈值 85%）"


# ---------------------------------------------------------------- 8
def t_field_weight_equivalence() -> str:
    from src.chunking import build_chunks, load_sections
    from src.retrieve import BM25

    qs = json.loads((ROOT / "eval" / "retrieval_set.json").read_text(encoding="utf-8"))["questions"]
    chunks = build_chunks("heading_split", load_sections(), max_chars=300)
    idx = BM25(chunks)
    for q in qs[:12]:
        base = [r.chunk.section_id for r in idx.search(q["q"], top_k=5)]
        zero = [
            r.chunk.section_id
            for r in idx.search_field_weighted(q["q"], title_weight=0.0, top_k=5)
        ]
        assert base == zero, f"{q['id']}: weight=0 与原始 BM25 不一致"
    return f"{min(12, len(qs))} 题逐题一致"


def main() -> int:
    print("=" * 74)
    print("冒烟测试（离线不变量，零 API 调用）")
    print("=" * 74)

    check("1 模块可导入", t_imports)
    check("2 知识库结构合法", t_kb)
    check("3 front-matter 解析器", t_parser)
    check("4 评测集自检", t_dataset)
    check("5 分块策略可构建", t_chunking)
    check("6 结构化字段可见", t_structured_fields)
    check("7 检索基线未退化", t_retrieval_baseline)
    check("8 字段加权等价性", t_field_weight_equivalence)

    print()
    print("=" * 74)
    if FAIL:
        print(f"❌ {len(FAIL)}/{len(PASS)+len(FAIL)} 项失败：")
        for name, msg in FAIL:
            print(f"   - {name}: {msg}")
        return 1
    print(f"✅ 全部 {len(PASS)} 项通过")
    print("（LLM 相关的判定质量与拒答分类不在本测试范围，见 eval/probe_*.py）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
