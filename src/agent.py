"""手写 ReAct 循环 —— Week 1 的核心，也是本阶段唯一不该被框架替代的部分。

一个 agent 的全部真相就四件事：
    LLM + 工具调用 + 记忆（messages 列表） + 循环终止条件

这里刻意不用 LangGraph / LangChain，目的是把每一行都握在手里。
第 3 周会用 LangGraph 重写一遍，那时你才有资格对比"框架替你做了什么"。

两个阶段：
  阶段 A  工具循环：模型自主决定查什么 → 真实检索 → 结果回填 → 直到它给出结论
  阶段 B  结构化输出：把结论压成 Pydantic 契约，校验失败就把错误原样回喂重试
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from pydantic import ValidationError

from .llm import ask_llm
from .schemas import AgentReport, SpecReport
from .tools import TOOL_IMPLS, TOOLS_SCHEMA

# 防死循环 + 防烧钱：这两个上限是"可控"的最低要求
MAX_STEPS = 6
MAX_REPORT_ATTEMPTS = 3

SYSTEM_PROMPT = """你是设计规范审查助手，负责把设计文档和设计规范知识库做逐条比对。

工作流程：
1. 读用户给出的设计文档内容。
2. 对每一个需要判定合规性的点，必须先调用 search_spec 工具检索规范原文。
3. 只有在检索到明确依据时，才能判 pass 或 fail；检索不到依据时判 uncertain，绝不凭记忆猜测。
4. 全部判定完成后，直接给出简短的总结性结论，结束本轮。

判定纪律：
- evidence 字段必须引用检索结果里的规范原文，不得改写、不得编造。
- fail 必须给出可执行的 suggestion，且建议要落在规范允许的取值范围内。
- 宁判 uncertain，不判错。误报和漏报都是质量问题。"""


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _strip_code_fence(text: str) -> str:
    """模型经常把 JSON 包在 ```json ... ``` 里，剥掉它。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    return m.group(1).strip() if m else t


def _usage_add(acc: dict, r) -> None:
    acc["prompt_tokens"] += r.prompt_tokens
    acc["completion_tokens"] += r.completion_tokens
    acc["llm_calls"] += 1


# ----------------------------------------------------------------------
# 阶段 A：工具调用循环
# ----------------------------------------------------------------------
def run_tool_loop(doc: str, max_steps: int = MAX_STEPS, verbose: bool = True) -> tuple[str, list[dict], dict]:
    """返回 (模型最终文本, 完整消息历史, 用量统计)。"""
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"请审查下面这份设计文档：\n\n<document>\n{doc}\n</document>"},
    ]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "tool_calls": 0}
    steps = 0

    for step in range(1, max_steps + 1):
        steps = step
        result = ask_llm(messages, tools=TOOLS_SCHEMA, temperature=0.2)
        _usage_add(usage, result)
        msg = result.message

        # 关键点 1：助手消息必须原样回填。
        # 只有 role="tool" 的消息能对应上 tool_call_id，所以 tool_calls 字段不能丢。
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in (msg.tool_calls or [])
                ],
            }
            if msg.tool_calls
            else {"role": "assistant", "content": msg.content or ""}
        )

        # 没有工具调用 = 模型认为可以收尾了
        if not msg.tool_calls:
            if verbose:
                print(f"[step {step}] 模型结束工具循环，进入结构化输出")
            return msg.content or "", messages, usage

        # 执行每个工具调用
        for tc in msg.tool_calls:
            usage["tool_calls"] += 1
            name = tc.function.name
            try:
                args: dict[str, Any] = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                # 关键点 3：错误不冒泡，变成工具结果回填
                payload = f"参数不是合法 JSON：{e}。请重新给出合法参数。"
            else:
                fn = TOOL_IMPLS.get(name)
                if fn is None:
                    payload = f"不存在名为 {name} 的工具。可用工具：{list(TOOL_IMPLS)}"
                else:
                    try:
                        payload = fn(**args)
                    except TypeError as e:
                        payload = f"参数不符合工具签名：{e}"
                    except Exception as e:  # noqa: BLE001 —— 故意兜住所有工具异常
                        payload = f"工具执行失败：{type(e).__name__}: {e}"

            if verbose:
                q = args.get("query") if isinstance(args, dict) else args
                print(f"[step {step}] 调用 {name}({q!r})")

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(payload)})

    # 走到这里说明达到了步数上限
    if verbose:
        print(f"[warn] 达到 max_steps={max_steps}，强制终止工具循环")
    return "达到最大步数上限，未能得出完整结论。", messages, usage


# ----------------------------------------------------------------------
# 阶段 B：结构化输出 + 校验重试
# ----------------------------------------------------------------------
REPORT_INSTRUCTION = """现在把上面的审查结论整理成严格符合下面 JSON Schema 的对象。

要求：
- 只输出 JSON，不要输出任何解释文字，不要用 markdown 代码块包裹。
- evidence 必须来自 search_spec 返回的规范原文。
- 没有依据的点一律判 uncertain，并把 evidence 留空。

JSON Schema：
{schema}
"""


def build_report(messages: list[dict], usage: dict, verbose: bool = True) -> tuple[SpecReport, dict]:
    """让模型产出符合 SpecReport 契约的数据；失败则把校验错误回喂重试。"""
    schema_text = json.dumps(SpecReport.model_json_schema(), ensure_ascii=False, indent=2)
    working = list(messages)
    working.append({"role": "user", "content": REPORT_INSTRUCTION.format(schema=schema_text)})

    last_error = ""
    for attempt in range(1, MAX_REPORT_ATTEMPTS + 1):
        result = ask_llm(working, temperature=0.1, json_mode=True)
        _usage_add(usage, result)
        raw = _strip_code_fence(result.message.content or "")

        try:
            report = SpecReport.model_validate_json(raw)
        except (ValidationError, ValueError) as e:
            last_error = str(e)
            if verbose:
                print(f"[report {attempt}/{MAX_REPORT_ATTEMPTS}] 校验失败，回喂错误重试")
                print("    " + last_error.splitlines()[0][:160])
            # 关键动作：把错误原文和它自己的错误输出一起回喂
            working.append({"role": "assistant", "content": raw})
            working.append(
                {
                    "role": "user",
                    "content": (
                        "上面的输出不符合契约，校验器报错如下：\n\n"
                        f"{last_error}\n\n"
                        "请修正后重新输出完整 JSON。只输出 JSON。"
                    ),
                }
            )
            continue

        if verbose:
            print(f"[report {attempt}/{MAX_REPORT_ATTEMPTS}] 校验通过，共 {len(report.issues)} 条判定")
        return report, usage

    raise RuntimeError(f"结构化输出在 {MAX_REPORT_ATTEMPTS} 次尝试后仍不合法：\n{last_error}")


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------
def run_agent(doc: str, doc_name: str = "document.md", max_steps: int = MAX_STEPS, verbose: bool = True) -> AgentReport:
    started = time.perf_counter()
    _, messages, usage = run_tool_loop(doc, max_steps=max_steps, verbose=verbose)
    report, usage = build_report(messages, usage, verbose=verbose)
    elapsed = time.perf_counter() - started

    return AgentReport(
        doc=doc_name,
        report=report,
        steps=len([m for m in messages if m.get("role") == "assistant"]),
        tool_calls=usage["tool_calls"],
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        elapsed_sec=round(elapsed, 2),
    )


# ----------------------------------------------------------------------
# 渲染：把结构化结果变成人看的 markdown（纯代码生成，不让模型碰格式）
# ----------------------------------------------------------------------
_VERDICT_LABEL = {"pass": "✅ 符合", "fail": "❌ 违反", "uncertain": "❓ 无依据"}


def render_markdown(ar: AgentReport) -> str:
    lines = [
        f"# 设计规范审查报告：{ar.doc}",
        "",
        f"> 步数 {ar.steps}｜工具调用 {ar.tool_calls} 次｜"
        f"token {ar.prompt_tokens}+{ar.completion_tokens}｜耗时 {ar.elapsed_sec}s",
        "",
        "## 结论",
        "",
        ar.report.summary,
        "",
        "## 逐条判定",
        "",
    ]
    for i, issue in enumerate(ar.report.issues, 1):
        lines += [
            f"### {i}. {issue.target} —— {_VERDICT_LABEL.get(issue.verdict, issue.verdict)}",
            "",
            f"- **理由**：{issue.reason}",
        ]
        if issue.evidence:
            lines.append(f"- **规范依据**：{issue.evidence}")
        if issue.suggestion:
            lines.append(f"- **修改建议**：{issue.suggestion}")
        lines.append("")

    if ar.report.evidence_sections:
        lines += ["## 引用出处", ""]
        lines += [f"- {s}" for s in ar.report.evidence_sections]
        lines.append("")
    return "\n".join(lines)
