"""LangGraph 版控制流：与 `src/agent.py` 的手写 ReAct 循环**结构等价**。

迁移纪律
--------
只搬「编排」，不碰「判定」。`SYSTEM_PROMPT`、`REPORT_INSTRUCTION`、工具分发、
溯源校验全部**从 agent.py 直接导入**，而不是复制粘贴 —— 否则基线对比测到的
就不是「控制流是否等价」，而是「prompt 有没有被改坏」两件事混在一起。

图结构（对应 agent.py 里的两层 for 循环）
----------------------------------------
    START
      │
      ▼
    agent ──(无 tool_calls)──────────────────────────────┐
      │                                                  │
      │(有 tool_calls)                                   │
      ▼                                                  │
    tools ──(step >= max_steps)──────────────────────────┤
      │                                                  │
      │(step < max_steps)                                │
      └──────────────► agent                             │
                                                         ▼
                                                   report_prep
                                                         │
                                                         ▼
                                      ┌──────────────── report ◄─┐
                                      │(校验通过)           │    │(校验失败且未超次)
                                      ▼                     └────┘
                                   finalize
                                      │
                                      ▼
                                     END

与手写版的三处一一对应：
  外层 `for step in range(max_steps)`  → agent 与 tools 之间的**回边**
  内层 `for tc in msg.tool_calls`     → tools 单个节点内部循环
  `build_report` 的 `for attempt`     → report 节点的**自环**，把隐式重试变成可见的边

为什么值得换成图
----------------
1. **断点续跑**：`checkpointer` 把粒度从「整篇文档」降到「每个节点之后」。
   手写循环里做不到 —— 评测层看不见 agent 内部走到第几步。
2. **人工介入**：`interrupt_before=["report"]` 可让判 fail 的高风险条目先过人眼。
3. **可观测**：预算耗尽、重试这类隐式分支变成图里可见的边。

用法
----
    from src.agent_graph import run_agent_graph
    ar = run_agent_graph(doc)                    # 与 src.agent.run_agent 同签名同返回

    # 断点续跑 + 人工介入
    app = build_agent_graph(checkpointer=saver, interrupt_before=["report"])
    cfg = {"configurable": {"thread_id": "doc-42"}, "recursion_limit": 100}
    app.invoke(init_state(doc), cfg)   # 停在 report 之前
    app.invoke(None, cfg)              # 人确认后继续
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

# 全部从手写版导入，保证 prompt / 工具契约 / 溯源校验逐字相同。
from .agent import (
    MAX_REPORT_ATTEMPTS,
    MAX_STEPS,
    MAX_TOOL_CALLS,
    REPORT_INSTRUCTION,
    REPORT_INSTRUCTION_LOOSE,
    SYSTEM_PROMPT,
    _provenance_drift,
    _strip_code_fence,
    _usage_add,
)
from .llm import ask_llm
from .schemas import AgentReport, SpecReport
from .tools import TOOL_IMPLS, VERIFY_ENABLED, active_schema, dispatch_tool

# LangGraph 默认 recursion_limit=25，而一个 6 步循环 + 3 次报告重试约需 18 步，
# 余量太小。显式放宽，避免长文档触发 GraphRecursionError 这种「跑一半才炸」的故障。
RECURSION_LIMIT = 100


class AgentState(TypedDict, total=False):
    """图状态。刻意用普通类型（无 reducer）：默认 LastValue 覆盖语义，与手写版的可变变量一一对应。

    `messages` 与 `report_messages` 必须分开
    ----------------------------------------
    原版 `build_report` 里 `working = list(messages)` 是**局部副本**，报告重试期间
    追加的消息不会污染 `messages`。而 `run_agent` 用
    `steps = len([m for m in messages if role == "assistant"])` 计轮次 ——
    如果两者共用一个列表，报告重试的 assistant 消息会被算进 steps，指标就不等价了。
    """

    # 入参
    doc: str
    doc_name: str
    llm_verify: bool
    max_steps: int
    max_tool_calls: int
    max_report_attempts: int
    loose_instruction: bool
    verbose: bool

    # 工具循环阶段
    messages: list[dict]
    usage: dict
    step: int
    budget_exhausted: bool
    final_text: str

    # 报告阶段
    report_messages: list[dict]
    report_attempts: int
    report: dict | None
    last_report_error: str

    # 收尾
    started: float
    final_report: dict | None


# ----------------------------------------------------------------------
# 节点
# ----------------------------------------------------------------------
def _node_agent(state: AgentState) -> dict:
    """一次 LLM 调用 + 原样回填助手消息（等价于手写版外层循环体前半）。"""
    result = ask_llm(state["messages"], tools=active_schema(state["llm_verify"]), temperature=0.2)
    usage = dict(state["usage"])
    _usage_add(usage, result)

    msg = result.message
    messages = list(state["messages"])
    if msg.tool_calls:
        # tool_calls 字段不能丢：只有 role="tool" 的消息能对应上 tool_call_id。
        messages.append({
            "role": "assistant",
            "content": msg.content or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })
        final_text = state.get("final_text", "")
    else:
        # 没有工具调用 = 模型认为可以收尾了
        messages.append({"role": "assistant", "content": msg.content or ""})
        final_text = msg.content or ""

    step = state.get("step", 0) + 1
    if state.get("verbose"):
        tail = "结束工具循环" if not msg.tool_calls else f"{len(msg.tool_calls)} 个工具调用"
        print(f"[step {step}] {tail}")
    return {"messages": messages, "usage": usage, "step": step, "final_text": final_text}


def _node_tools(state: AgentState) -> dict:
    """执行一步里的全部工具调用（等价于手写版内层 for，含预算拒绝）。"""
    messages = list(state["messages"])
    usage = dict(state["usage"])
    # 显式复制列表：否则 setdefault(...).append 会改到别的检查点共享的同一个 list
    usage["queries"] = list(usage.get("queries", []))
    usage["reject_reasons"] = list(usage.get("reject_reasons", []))
    budget_exhausted = bool(state.get("budget_exhausted", False))
    use_verify = state["llm_verify"]
    max_tool_calls = state["max_tool_calls"]

    for tc in messages[-1].get("tool_calls") or []:
        name = tc["function"]["name"]
        try:
            args: dict[str, Any] = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}

        if usage["tool_calls"] >= max_tool_calls:
            budget_exhausted = True
            payload = (
                f"已达工具调用总预算（{max_tool_calls} 次），本次调用被拒绝。"
                "请立即停止检索，仅基于已获得的依据完成判定；"
                "仍无依据的点判 uncertain 并把 evidence 留空。"
            )
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": payload})
            continue

        try:
            if name != "search_spec_verified" and name not in TOOL_IMPLS:
                payload = f"不存在名为 {name} 的工具。"
            else:
                out = dispatch_tool(name, args, verify=use_verify)
                payload = out if out is not None else f"不存在名为 {name} 的工具。"
        except TypeError as e:
            payload = f"参数不符合工具签名：{e}"
        except Exception as e:  # noqa: BLE001 —— 故意兜住所有工具异常，避免整程序崩溃
            payload = f"工具执行失败：{type(e).__name__}: {e}"

        usage["tool_calls"] += 1

        # 记录查询语句：适用性判定高度依赖查询措辞，必须可观测
        if isinstance(args, dict) and args.get("query"):
            usage["queries"].append(str(args["query"]))

        # 记录拒绝原因：让「检索层为什么拒答」成为可观测产物
        if isinstance(payload, str):
            try:
                _d = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                _d = None
            if isinstance(_d, dict) and _d.get("reject_reason"):
                _q = args.get("query") if isinstance(args, dict) else ""
                usage["reject_reasons"].append(f"{_d['reject_reason']}（{_q}）")

        if state.get("verbose"):
            print(f"[step {state.get('step', 0)}] 调用 {name}({args.get('query') if isinstance(args, dict) else args!r})")

        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(payload)})

    return {"messages": messages, "usage": usage, "budget_exhausted": budget_exhausted}


def _node_report_prep(state: AgentState) -> dict:
    """把手写版里 `working = list(messages)` 的那个「局部副本」显式化。"""
    template = REPORT_INSTRUCTION_LOOSE if state["loose_instruction"] else REPORT_INSTRUCTION
    schema_text = json.dumps(SpecReport.model_json_schema(), ensure_ascii=False, indent=2)
    report_messages = list(state["messages"])
    report_messages.append({"role": "user", "content": template.format(schema=schema_text)})
    return {"report_messages": report_messages, "report_attempts": 0, "report": None, "last_report_error": ""}


def _node_report(state: AgentState) -> dict:
    """一次结构化输出尝试。失败时把校验错误与自己的错误输出一起回喂。"""
    working = list(state["report_messages"])
    usage = dict(state["usage"])
    attempts = state.get("report_attempts", 0) + 1
    max_attempts = state["max_report_attempts"]

    result = ask_llm(working, temperature=0.1, json_mode=True)
    _usage_add(usage, result)
    raw = _strip_code_fence(result.message.content or "")

    try:
        report = SpecReport.model_validate_json(raw)
    except (ValidationError, ValueError) as e:
        last_error = str(e)
        if state.get("verbose"):
            print(f"[report {attempts}/{max_attempts}] 校验失败，回喂错误重试")
            print("    " + last_error.splitlines()[0][:160])
        working.append({"role": "assistant", "content": raw})
        working.append({
            "role": "user",
            "content": (
                "上面的输出不符合契约，校验器报错如下：\n\n"
                f"{last_error}\n\n"
                "请修正后重新输出完整 JSON。只输出 JSON。"
            ),
        })
        return {
            "report_messages": working,
            "usage": usage,
            "report_attempts": attempts,
            "last_report_error": last_error,
        }

    if state.get("verbose"):
        print(f"[report {attempts}/{max_attempts}] 校验通过，共 {len(report.issues)} 条判定")
    return {"report": report.model_dump(), "usage": usage, "report_attempts": attempts, "last_report_error": ""}


def _node_abort(state: AgentState) -> dict:
    """与手写版同样抛 RuntimeError —— 重试耗尽就是失败，不静默降级。"""
    raise RuntimeError(
        f"结构化输出在 {state['max_report_attempts']} 次尝试后仍不合法：\n{state['last_report_error']}"
    )


def _node_finalize(state: AgentState) -> dict:
    """组装 AgentReport，字段与手写版逐一对应。"""
    report = SpecReport.model_validate(state["report"])
    drift, drift_checked = _provenance_drift(state["messages"], report)
    usage = state["usage"]
    ar = AgentReport(
        doc=state["doc_name"],
        report=report,
        steps=len([m for m in state["messages"] if m.get("role") == "assistant"]),
        tool_calls=usage["tool_calls"],
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        elapsed_sec=round(time.perf_counter() - state["started"], 2),
        provenance_drift=drift,
        provenance_checked=drift_checked,
        budget_exhausted=bool(state.get("budget_exhausted", False)),
        reject_reasons=list(usage.get("reject_reasons", [])),
        retrieval_queries=list(usage.get("queries", [])),
    )
    return {"final_report": ar.model_dump()}


# ----------------------------------------------------------------------
# 条件边（手写版里的 if / 循环退出条件）
# ----------------------------------------------------------------------
def _after_agent(state: AgentState) -> str:
    return "tools" if state["messages"][-1].get("tool_calls") else "report_prep"


def _after_tools(state: AgentState) -> str:
    # 注意：最后一步的工具调用**照常执行**后才进报告阶段，与手写版顺序一致
    return "report_prep" if state["step"] >= state["max_steps"] else "agent"


def _after_report(state: AgentState) -> str:
    if state.get("report"):
        return "finalize"
    if state.get("report_attempts", 0) >= state["max_report_attempts"]:
        return "abort"
    return "report"


# ----------------------------------------------------------------------
# 构图
# ----------------------------------------------------------------------
def build_agent_graph(
    *,
    checkpointer: Any = None,
    interrupt_before: list[str] | None = None,
):
    """构建并编译状态图。

    checkpointer 传给 compile 后，每个节点结束都会落一份检查点；
    中断/崩溃后可用同一个 checkpointer + thread_id 从断点恢复，
    不必重跑前面的检索（这是手写 for 循环给不了的能力）。
    """
    g = StateGraph(AgentState)
    g.add_node("agent", _node_agent)
    g.add_node("tools", _node_tools)
    g.add_node("report_prep", _node_report_prep)
    g.add_node("report", _node_report)
    g.add_node("abort", _node_abort)
    g.add_node("finalize", _node_finalize)

    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", _after_agent, {"tools": "tools", "report_prep": "report_prep"})
    g.add_conditional_edges("tools", _after_tools, {"agent": "agent", "report_prep": "report_prep"})
    g.add_edge("report_prep", "report")
    g.add_conditional_edges(
        "report", _after_report, {"report": "report", "finalize": "finalize", "abort": "abort"}
    )
    g.add_edge("finalize", END)
    g.add_edge("abort", END)

    return g.compile(checkpointer=checkpointer, interrupt_before=interrupt_before or None)


def init_state(
    doc: str,
    doc_name: str = "document.md",
    *,
    max_steps: int = MAX_STEPS,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_report_attempts: int = MAX_REPORT_ATTEMPTS,
    loose_instruction: bool = False,
    llm_verify: bool | None = None,
    verbose: bool = False,
) -> dict:
    """初始状态。与手写版 run_tool_loop 开头的初始化逐项对应。"""
    use_verify = VERIFY_ENABLED if llm_verify is None else llm_verify
    return {
        "doc": doc,
        "doc_name": doc_name,
        "llm_verify": use_verify,
        "max_steps": max_steps,
        "max_tool_calls": max_tool_calls,
        "max_report_attempts": max_report_attempts,
        "loose_instruction": loose_instruction,
        "verbose": verbose,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请审查下面这份设计文档：\n\n<document>\n{doc}\n</document>"},
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "tool_calls": 0},
        "step": 0,
        "budget_exhausted": False,
        "final_text": "",
        "started": time.perf_counter(),
    }


def run_agent_graph(
    doc: str,
    doc_name: str = "document.md",
    *,
    max_steps: int = MAX_STEPS,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_report_attempts: int = MAX_REPORT_ATTEMPTS,
    loose_instruction: bool = False,
    llm_verify: bool | None = None,
    verbose: bool = False,
    checkpointer: Any = None,
    thread_id: str | None = None,
    interrupt_before: list[str] | None = None,
) -> AgentReport:
    """与 `src.agent.run_agent` 同签名、同返回类型的替代实现。

    签名里 `doc_name` 之后的参数是 keyword-only —— 与手写版的位置参数版本略有差别，
    是刻意的：避免调用方靠位置传参，迁移后静默接到错误的参数上。
    """
    app = build_agent_graph(
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
        interrupt_before=interrupt_before,
    )
    state = init_state(
        doc,
        doc_name,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        max_report_attempts=max_report_attempts,
        loose_instruction=loose_instruction,
        llm_verify=llm_verify,
        verbose=verbose,
    )
    cfg = {
        "configurable": {"thread_id": thread_id or f"run-{uuid.uuid4().hex[:8]}"},
        "recursion_limit": RECURSION_LIMIT,
    }
    out = app.invoke(state, cfg)
    if not out.get("final_report"):
        raise RuntimeError(
            "图未产出最终报告 —— 通常是 interrupt_before 让流程停在中断点。"
            f"当前停在 {list(out.keys())[:6]}…，请用同一个 checkpointer + thread_id 调用 app.invoke(None, cfg) 继续。"
        )
    return AgentReport.model_validate(out["final_report"])


def mermaid() -> str:
    """导出图结构的 mermaid 文本（写文档/面试讲解用）。"""
    return build_agent_graph().get_graph().draw_mermaid()
