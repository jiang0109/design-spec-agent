"""命令行入口 —— 第 1-3 天的可交付物。

用法：
    python cli.py samples/sample_doc.md              # 审查一份文档
    python cli.py samples/sample_doc.md -v           # 打印每一步中间过程
    python cli.py samples/sample_doc.md --max-steps 8
    python cli.py --ping                             # 只测 API 连通性
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ----------------------------------------------------------------------
# Windows 中文控制台默认编码是 GBK，报告里的 ✅ / ❌ / ❓ 会触发：
#     UnicodeEncodeError: 'gbk' codec can't encode character '\u274c'
# 导致打印报告时直接崩溃（文件已写好，但终端看不到内容）。
# 这里把标准流强制为 UTF-8 兜住。这是 Windows 写中文 CLI 的通用坑，
# 与 agent 逻辑无关 —— 但它会伪装成"agent 跑挂了"，值得单独记住。
# ----------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 非标准流（重定向时的旧式流）
        pass

from src.agent import run_agent, render_markdown

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="设计规范审查 agent（Week 1：手写 ReAct 循环）")
    parser.add_argument("doc", nargs="?", help="要审查的文档路径（.md / .txt）")
    parser.add_argument("-o", "--out", default="out", help="输出目录，默认 out/")
    parser.add_argument("--max-steps", type=int, default=6, help="工具循环最大轮次，默认 6")
    parser.add_argument(
        "--tool-calls",
        type=int,
        default=12,
        help="工具调用总预算，默认 12（比 --max-steps 更真实的成本护栏）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每一步中间过程")
    parser.add_argument(
        "--loose-prompt",
        action="store_true",
        help="对照实验用：使用修复前的宽松溯源表述（默认使用加严版本）",
    )
    parser.add_argument("--ping", action="store_true", help="只测试 API 连通性后退出")
    args = parser.parse_args()

    if args.ping:
        try:
            from src.llm import ping

            print("连通性 OK：", ping())
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"连通性失败：{type(e).__name__}: {e}", file=sys.stderr)
            return 1

    if not args.doc:
        parser.print_help()
        return 2

    doc_path = Path(args.doc)
    if not doc_path.is_absolute():
        doc_path = ROOT / doc_path
    if not doc_path.exists():
        print(f"找不到文档：{doc_path}", file=sys.stderr)
        return 2

    doc_text = doc_path.read_text(encoding="utf-8")
    print(f"审查文档：{doc_path.name}（{len(doc_text)} 字符）")
    print("-" * 56)

    try:
        ar = run_agent(
            doc_text,
            doc_name=doc_path.name,
            max_steps=args.max_steps,
            max_tool_calls=args.tool_calls,
            loose_instruction=args.loose_prompt,
            verbose=args.verbose,
        )
    except Exception as e:  # noqa: BLE001
        print(f"\n运行失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = doc_path.stem
    (out_dir / f"{stem}.report.json").write_text(
        ar.model_dump_json(indent=2), encoding="utf-8"
    )
    (out_dir / f"{stem}.report.md").write_text(render_markdown(ar), encoding="utf-8")

    print("-" * 56)
    print(render_markdown(ar))
    print("-" * 56)
    print(f"已写出：{out_dir / f'{stem}.report.json'}")
    print(f"已写出：{out_dir / f'{stem}.report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
