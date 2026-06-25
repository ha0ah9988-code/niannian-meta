"""核心对话循环 —— 从 Hermes 提取成熟度，去 Nous 依赖版

包含：重试策略、错误分类、迭代预算控制。
"""

import json
import time
import sys
import threading
from typing import Optional

from core.providers.base import BaseProvider, ProviderError
from core.tools import ToolRegistry
from evolution.memory import Memory


# ── 错误分类 ─────────────────────────────────────────

class ErrorCategory:
    """错误分类 —— 决定恢复策略"""
    RETRYABLE = "retryable"           # 临时故障（超时/503/429）
    RATE_LIMITED = "rate_limited"     # 频率限制（需退避）
    AUTH = "auth"                     # 认证错误（需换 key）
    FORMAT = "format"                 # 请求格式错误（不重试）
    CONTEXT_OVERFLOW = "overflow"     # 上下文超长（需压缩）
    UNKNOWN = "unknown"               # 未知错误


def classify_error(error: Exception, status_code: int = None) -> tuple:
    """分类错误，返回 (category, should_retry, should_backoff)

    Args:
        error: 异常对象
        status_code: HTTP 状态码（如果有）

    Returns:
        (category, should_retry, should_backoff)
    """
    msg = str(error).lower()
    sc = status_code

    # 认证错误
    if sc in (401, 403) or any(x in msg for x in ["unauthorized", "forbidden",
                                                    "invalid_api_key",
                                                    "authentication"]):
        return (ErrorCategory.AUTH, False, False)

    # 频率限制
    if sc == 429 or "rate_limit" in msg or "too many requests" in msg:
        return (ErrorCategory.RATE_LIMITED, True, True)

    # 上下文超长
    if sc == 413 or any(x in msg for x in ["context_length", "context_overflow",
                                            "too many tokens",
                                            "maximum context"]):
        return (ErrorCategory.CONTEXT_OVERFLOW, False, False)

    # 请求格式错误
    if sc == 400 or "bad request" in msg or "invalid_request" in msg:
        # 少数 400 可以通过重试恢复
        if "reasoning_content" in msg or "must be passed back" in msg:
            return (ErrorCategory.FORMAT, False, False)
        return (ErrorCategory.FORMAT, False, False)

    # 服务端错误（可重试）
    if sc in (500, 502, 503, 529) or any(x in msg for x in ["server error",
                                                              "overloaded",
                                                              "temporarily"]):
        return (ErrorCategory.RETRYABLE, True, True)

    # 超时
    if any(x in msg for x in ["timeout", "timed out", "deadline"]):
        return (ErrorCategory.RETRYABLE, True, True)

    # DNS/连接错误
    if any(x in msg for x in ["name or service not known", "connection",
                               "connect error", "eof"]):
        return (ErrorCategory.RETRYABLE, True, True)

    return (ErrorCategory.UNKNOWN, True, False)


# ── 迭代预算 ─────────────────────────────────────────

class IterationBudget:
    """线程安全的迭代预算控制"""

    def __init__(self, max_total: int = 90):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self):
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)

    @property
    def used(self) -> int:
        with self._lock:
            return self._used


# ── 核心 Agent ───────────────────────────────────────

class Agent:
    """niannian-meta 核心 Agent

    一次对话 = 一个 Agent 实例。
    可通过适配器接管 on_ask / on_tool_progress 回调。
    """

    def __init__(self, provider: BaseProvider, tools: ToolRegistry,
                 memory: Memory, identity_prompt: str):
        self.provider = provider
        self.tools = tools
        self.memory = memory
        self.identity_prompt = identity_prompt

        # 状态
        self.history = memory.load_history()
        self._turn_count = 0
        self._llm_calls_this_turn = 0
        self._budget = IterationBudget(max_total=90)

        # 回调（适配器可接管）
        self.on_tool_progress = lambda name, args: None
        self.on_ask = lambda q: input(f"\n[确认] {q}\n> ").strip()

        # 身份（惰性加载）
        self._rules = None
        self._agents = None

    # ── 身份 consult ────────────────────────────────

    @property
    def rules(self) -> str:
        if self._rules is None:
            from core import load_rules
            self._rules = load_rules()
        return self._rules

    @property
    def agents(self) -> str:
        if self._agents is None:
            from core import load_agents
            self._agents = load_agents()
        return self._agents

    # ── 核心循环 ────────────────────────────────────

    def process(self, user_input: str, context: str = "") -> str:
        """处理一轮用户输入"""
        self._turn_count += 1
        self._llm_calls_this_turn = 0

        if not self._budget.consume():
            return "迭代预算已耗尽，请开始新会话 (/clear)"

        # 构造消息
        messages = self._build_messages(user_input, context)

        # LLM 循环
        final_content = ""
        tool_rounds = 0
        max_tool_rounds = 20

        while tool_rounds < max_tool_rounds:
            tool_rounds += 1

            response = self._llm_call_with_retry(messages)
            if response is None:
                final_content += "\n[LLM 调用失败，请重试]"
                break

            content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            if content:
                final_content += content + "\n"

            if not tool_calls:
                break

            # 执行工具
            for tc in tool_calls:
                result = self._execute_tool(tc["name"], tc.get("arguments", {}))

                # 注入工具结果
                asst = {"role": "assistant",
                       "content": content if content else None}
                if response.get("reasoning_content"):
                    asst["reasoning_content"] = response["reasoning_content"]
                asst["tool_calls"] = [
                    {"id": tc.get("id", ""), "type": "function",
                     "function": {"name": tc["name"],
                                  "arguments": json.dumps(tc["arguments"])}}
                ]
                messages.append(asst)
                messages.append({"role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "content": str(result)})

        # 持久化
        self.history.append({"role": "user", "content": user_input})
        self.history.append({"role": "assistant", "content": final_content})
        self.memory.save_history(self.history)

        # 自动结晶
        if self._turn_count % 5 == 0:
            self._auto_crystallize()

        return final_content.strip()

    # ── LLM 调用（带重试） ──────────────────────────

    def _llm_call_with_retry(self, messages: list) -> Optional[dict]:
        """调用 LLM，带分类重试和退避

        重试策略：
        - 认证错误：直接失败
        - 格式错误：去掉工具再试一次
        - 速率限制：退避后重试
        - 服务端错误：退避后重试（最多 3 次）
        - 超时/连接错误：退避后重试（最多 2 次）
        """
        max_attempts = 3
        last_error = None

        for attempt in range(max_attempts):
            try:
                self._llm_calls_this_turn += 1
                if not self._budget.consume():
                    return None

                return self.provider.chat(
                    messages=messages,
                    tools=self.tools.to_openai_tools(),
                )

            except Exception as e:
                last_error = e
                category, should_retry, should_backoff = classify_error(e)

                # 格式错误：去掉工具再试
                if category == ErrorCategory.FORMAT and attempt == 0:
                    try:
                        return self.provider.chat(messages=messages)
                    except Exception as e2:
                        last_error = e2
                        return None

                # 认证错误：不重试
                if category == ErrorCategory.AUTH:
                    return None

                # 上下文超长：不重试，报给用户
                if category == ErrorCategory.CONTEXT_OVERFLOW:
                    return {"content": "上下文超长，请 /clear 后重试"}

                # 退避
                if should_backoff:
                    wait = min(2 ** attempt * 2, 16)
                    time.sleep(wait)

                if not should_retry or attempt >= max_attempts - 1:
                    return None

        return None

    # ── 工具执行 ────────────────────────────────────

    def _execute_tool(self, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"未知工具: {name}"

        self.on_tool_progress(name, args)

        if name == "ask":
            return self.on_ask(args.get("question", "需要确认"))

        try:
            result = tool(**args)
            return str(result) if result is not None else "(空)"
        except Exception as e:
            return f"工具执行错误: {e}"

    # ── System Prompt ──────────────────────────────

    def _build_messages(self, user_input: str, context: str = "") -> list:
        """构建消息列表"""
        parts = [self.identity_prompt, "\n## 可用工具\n"]
        for t in self.tools.list_tools():
            parts.append(f"- {t.name}: {t.description}")
            if t.parameters:
                parts.append(f"  参数: {', '.join(t.parameters.keys())}")

        parts.append("\n## 记忆\n")
        parts.append(self.memory.get_l1())
        l2_short = "\n".join(self.memory.get_l2().split("\n")[:15])
        parts.append(f"\n{l2_short}")

        if context:
            parts.append(f"\n## 上下文\n{context}")

        parts.append(f"\nTurn: {self._turn_count}")
        system = "\n".join(parts)

        messages = [{"role": "system", "content": system}]
        for msg in self.history[-30:]:
            messages.append(msg)
        messages.append({"role": "user", "content": user_input})
        return messages

    # ── 结晶 ───────────────────────────────────────

    def _auto_crystallize(self):
        summary = f"Turn {self._turn_count}: {len(self.history)} msg"
        self.memory.crystallize(task_summary=summary)
        rpt = self.memory.maintain()
        if "超限" in rpt:
            pass  # 未来触发 cleanup

    # ── 系统命令 ───────────────────────────────────

    def system_command(self, cmd: str) -> str:
        c = cmd.strip().lower()
        if c == "/status":
            return (f"niannian-meta v0.2.0\n"
                    f"Turns: {self._turn_count}\n"
                    f"LLM calls in last turn: {self._llm_calls_this_turn}\n"
                    f"Budget: {self._budget.used}/{self._budget.max_total}\n"
                    f"Tools: {len(self.tools.list_tools())}\n"
                    f"History: {len(self.history)} msgs\n"
                    f"{self.memory.maintain()}")
        if c == "/rules":
            return self.rules
        if c == "/soul":
            return self.identity_prompt
        if c == "/clear":
            self.history.clear()
            self._turn_count = 0
            return "会话已清空"
        if c.startswith("/agent"):
            return self.agents
        if c.startswith("/memory"):
            parts = cmd.split()
            if len(parts) > 1:
                if parts[1] == "l1":
                    return self.memory.get_l1()
                if parts[1] == "l2":
                    return self.memory.get_l2()
                if parts[1] == "ls":
                    return "\n".join(self.memory.list_l3())
            return self.memory.get_l1()
        return f"未知: {cmd}"
