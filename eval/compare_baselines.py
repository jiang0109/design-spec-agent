"""对比两份冻结基线，输出逐点差异 —— 迁移/重构的回归判定器。

为什么要它
----------
只看「97% → 97%」是不够的：总数相同可能是 3 个变好 + 3 个变坏互相抵消。
控制流重构（如迁移到 LangGraph）的验收标准应该是**判定行为逐点等价**，
所以必须能回答：哪个判定点的判定变了、变成什么、是变好还是变坏。

用法
----
    python -m eval.compare_baselines eval/baselines/pre-langgraph.json eval/baselines/post-langgraph.json

退出码
------
    0  无「正确→错误」回归
    1  存在正确→错误回归（可用作 CI 门禁）
    2  输入不可比（判定点集合不同，说明数据或匹配口径变了）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent

METRIC_ROWS = [
    ("判定点总数", "total_points", "int"),
    ("覆盖率", "coverage", "pct"),
    ("准确率", "accuracy", "pct"),
    ("误报 FP", "fp", "int"),
    ("漏报 FN", "fn", "int"),
    ("过度拒答", "over_reject", "int"),
    ("过度自信", "over_confidence", "int"),
    ("未命中", "unmatched", "int"),
    ("引擎占比", "engine_ratio", "pct"),
]


def _load(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return json.loads(p.read_text(encoding="utf-8"))


def _keyed(points: list[dict]) -> dict[tuple, dict]:
    """判定点稳定键：同一文档内 (rule, keyword) 唯一；重复时加序号。"""
    out: dict[tuple, dict] = {}
    seen: dict[tuple, int] = {}
    for p in points:
        base = (p.get("id"), p.get("rule"), p.get("keyword"))
        seen[base] = seen.get(base, 0) + 1
        key = base if seen[base] == 1 else (*base, seen[base])
        out[key] = p
    return out


def _fmt(v, kind: str) -> str:
    if v is None:
        return "-"
    if kind == "pct":
        return f"{v * 100:.0f}%"
    return str(v)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    a, b = _load(argv[1]), _load(argv[2])

    print("=" * 92)
    print(f"A = {a['tag']}  ({a.get('created_at', '?')}, commit {a['git']['commit'][:7]})")
    print(f"B = {b['tag']}  ({b.get('created_at', '?')}, commit {b['git']['commit'][:7]})")
    print("=" * 92)

    # -------- 输入指纹：先确认比的是同一套东西 --------
    ia = a["fingerprint"]["inputs"]
    ib = b["fingerprint"]["inputs"]
    changed = sorted(k for k in set(ia) | set(ib) if ia.get(k) != ib.get(k))
    if changed:
        print(f"\n输入文件差异（{len(changed)} 个）：")
        for k in changed:
            state = "新增" if k not in ia else "删除" if k not in ib else "改动"
            print(f"  [{state}] {k}")
    else:
        print("\n输入文件完全一致（kb / src / eval 脚本 / 评测集逐文件 SHA256 相同）")

    # -------- 指标 --------
    ma, mb = a["metrics"], b["metrics"]
    print()
    print(f"{'指标':<14}{'A':>10}{'B':>10}{'差':>10}")
    print("-" * 92)
    for label, key, kind in METRIC_ROWS:
        va, vb = ma.get(key), mb.get(key)
        if kind == "pct":
            diff = f"{(vb - va) * 100:+.0f}pp" if va is not None and vb is not None else "-"
        else:
            diff = f"{vb - va:+d}" if va is not None and vb is not None else "-"
        print(f"{label:<14}{_fmt(va, kind):>10}{_fmt(vb, kind):>10}{diff:>10}")

    # -------- 判定点集合必须一致，否则不可比 --------
    ka, kb = _keyed(a["points"]), _keyed(b["points"])
    if set(ka) != set(kb):
        only_a, only_b = sorted(set(ka) - set(kb)), sorted(set(kb) - set(ka))
        print(f"\n✗ 判定点集合不同，不可比：A 独有 {len(only_a)} 个，B 独有 {len(only_b)} 个")
        for k in (only_a[:5] + only_b[:5]):
            print(f"    {k}")
        return 2

    # -------- 逐点差异 --------
    changed_pts = []
    better = worse = 0
    for k in sorted(ka, key=lambda x: (str(x[0]), str(x[1]), str(x[2]))):
        pa, pb = ka[k], kb[k]
        fields = [f for f in ("expect", "actual", "correct", "error", "source", "attribute")
                  if pa.get(f) != pb.get(f)]
        if not fields:
            continue
        if not pa["correct"] and pb["correct"]:
            verdict, better = "改善", better + 1
        elif pa["correct"] and not pb["correct"]:
            verdict, worse = "回归", worse + 1
        else:
            verdict = "变化"
        changed_pts.append((k, pa, pb, fields, verdict))

    print()
    print("=" * 92)
    print(f"逐点差异：{len(changed_pts)} 处（改善 {better} / 回归 {worse}）")
    print("=" * 92)
    if not changed_pts:
        print("  所有判定点的 expect / actual / correct / source 完全一致 —— 行为等价")
    for k, pa, pb, fields, verdict in changed_pts[:60]:
        print(f"  [{verdict}] {k[0]} · {k[1]} · {str(k[2])[:24]}")
        for f in fields:
            print(f"      {f:<11} {pa.get(f)!r} → {pb.get(f)!r}")
    if len(changed_pts) > 60:
        print(f"  …… 其余 {len(changed_pts) - 60} 处省略")

    print()
    if worse:
        print(f"✗ 存在 {worse} 处「正确→错误」回归")
        return 1
    print("✓ 无「正确→错误」回归")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
