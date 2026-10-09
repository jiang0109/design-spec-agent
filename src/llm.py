"""模型客户端：全项目唯一直接接触 API 的地方。

设计意图（面试可直接讲）：
  - 上层（agent.py）不应该知道用的是 DeepSeek 还是 OpenAI，只依赖这里的返回值结构
  - 顺手把 token 用量、耗时、finish_reason 收上来，第 4 周的评测报告要用
  - API key 只从环境变量读，绝不写进代码 —— 这是最基本的安全习惯

关于 API key：见项目根目录的 .env（不提交到 git），模板在 .env.example
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

_client: OpenAI | None = None


class MissingAPIKeyError(RuntimeError):
    """没有配 API key 时给出明确指引，而不是抛一个看不懂的 OpenAI 异常。"""


def get_client() -> OpenAI:
    global _client
    if _client is not None:
        return _client

    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise MissingAPIKeyError(
            "没有找到 API key。请在项目根目录创建 .env 文件并写入：\n"
            "    LLM_API_KEY=sk-你的密钥\n"
            "可以参考 .env.example。"
        )

    _client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
        timeout=float(os.getenv("LLM_TIMEOUT", "120")),
        max_retries=2,
    )
    return _client


def model_name() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


@dataclass
class LLMResult:
    """一次调用的完整结果，方便上层记账。"""

    message: Any
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_sec: float = 0.0
    finish_reason: str | None = None
    raw: Any = field(default=None, repr=False)


def ask_llm(
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> LLMResult:
    """调用一次模型。注意：它只调用"一次"，循环逻辑在 agent.py 里。"""
    kwargs: dict[str, Any] = {
        "model": model_name(),
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
    if json_mode:
        # 让模型直接返回 JSON 对象。真正的字段校验仍然由 Pydantic 负责。
        kwargs["response_format"] = {"type": "json_object"}

    started = time.perf_counter()
    resp = get_client().chat.completions.create(**kwargs)
    elapsed = time.perf_counter() - started

    usage = getattr(resp, "usage", None)
    return LLMResult(
        message=resp.choices[0].message,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        elapsed_sec=elapsed,
        finish_reason=getattr(resp.choices[0], "finish_reason", None),
        raw=resp,
    )


def ping() -> str:
    """连通性自检：python -c "from src.llm import ping; print(ping())" """
    r = ask_llm([{"role": "user", "content": "只回复两个字：连通"}], temperature=0)
    return f"{r.message.content}（model={model_name()}, {r.elapsed_sec:.2f}s）"
