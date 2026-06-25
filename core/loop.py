"""核心对话循环 —— 从 Hermes 提取成熟度，去 Nous 依赖版

包含：重试策略、错误分类、迭代预算控制、上下文自动压缩。

基于 Hermes 架构：
  - ContextEngine + ContextCompressor 处理自动压缩
  - 每次 LLM 响应后跟踪 token 用量
  - 达到阈值时自动触发压缩
"""

import json
import time
import sys
import threading
import logging
from typing import Optional

from core.providers.base import BaseProvider, ProviderError
from core.tools import ToolRegistry
from evolution.memory import Memory
from evolution.learn import learn_from_turn
from core.compressor import (
    ContextEngine,
    ContextCompressor,
    estimate_messages_tokens,
    estimate_content_tokens,
)
from core.state import StateStore

logger = logging.getLogger(__name__)

# ── 工具输出截断 ─────────────────────────────────────
MAX_TOOL_RESULT_CHARS = 15000
_TOOL_TRUNC_WARNING = (
    "\n... (truncated {excess} chars, original was {total})"
)


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

    架构来源（Hermes conversation_loop.py）：
      - provider 调用带重试与错误分类
      - 每次 LLM 响应后通过 context_engine 跟踪 token
      - 自动压缩检查并触发 ContextCompressor
    """

    def __init__(
        self,
        provider: BaseProvider,
        tools: ToolRegistry,
        memory: Memory,
        identity_prompt: str,
        context_engine: ContextEngine = None,
        config: dict = None,
    ):
        self.provider = provider
        self.tools = tools
        self.memory = memory
        self.identity_prompt = identity_prompt

        # 配置
        config = config or {}
        kernel_cfg = config.get("kernel", {})
        compression_cfg = config.get("compression", {})

        # state.db 持久化
        state_cfg = config.get("state", {})
        self.state = StateStore(db_path=state_cfg.get("db_path"))
        self.state_source = state_cfg.get("source", "standalone")

        # 状态 — 从 state.db 加载历史
        self.history = self.state.load_history()
        self._turn_count = 0
        self._llm_calls_this_turn = 0
        self._budget = IterationBudget(max_total=kernel_cfg.get("max_total", 90))
        self._history_size = kernel_cfg.get("history_size", 20)

        # 回调（适配器可接管）
        self.on_tool_progress = lambda name, args: None
        self.on_ask = lambda q: input(f"\n[确认] {q}\n> ").strip()
        self.on_stream = None  # Callable[[str], None], TG 模式时设置
        self.on_compression_status = None  # Callable[[str], None]

        # 上下文引擎 —— 自动压缩
        if context_engine:
            self.context_engine = context_engine
        elif compression_cfg.get("enabled", True):
            # 从 provider 构建 LLM 函数
            llm_func = self._make_compression_llm_func()
            self.context_engine = ContextCompressor(
                llm_func=llm_func,
                threshold_percent=compression_cfg.get("threshold_percent", 0.75),
                protect_first_n=compression_cfg.get("protect_first_n", 3),
                protect_last_n=compression_cfg.get("protect_last_n", 8),
                context_length=compression_cfg.get("context_length", 128000),
                summary_max_tokens=compression_cfg.get("summary_max_tokens", 8000),
            )
        else:
            self.context_engine = None  # 无压缩

        # 身份（惰性加载）
        self._rules = None
        self._agents = None

    def _make_compression_llm_func(self):
        """创建用于压缩的 LLM 调用函数

        使用主 provider 的聊天接口，包装为压缩器可用的签名。
        """
        def _compress_llm(messages: list, max_tokens: int = 2000,
                          temperature: float = 0.3) -> str:
            try:
                result = self.provider.chat(
                    messages=messages,
                    max_tokens=max_tokens,
                )
                return result.get("content", "")
            except Exception as e:
                logger.warning("Compression LLM call failed: %s", e)
                return ""

        return _compress_llm

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

        messages = self._build_messages(user_input, context)

        # 尝试流式输出（如果提供了 on_stream 回调）
        final_content = ""
        tool_rounds = 0
        max_tool_rounds = 20
        use_stream = hasattr(self, 'on_stream') and self.on_stream is not None

        if use_stream:
            return self._process_stream(messages, user_input, max_tool_rounds)

        # 非流式路径
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

            for tc in tool_calls:
                result = self._execute_tool(tc["name"], tc.get("arguments", {}))
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

        self.history.append({"role": "user", "content": user_input})
        self.history.append({"role": "assistant", "content": final_content})
        self.state.save_history(self.history)

        # ── 自动压缩检查（替代旧的 _compress_history） ──
        self._auto_compress_check(messages)

        if self._turn_count % 5 == 0:
            self._auto_crystallize()

        # 自动学习检测
        learn_from_turn(self, user_input, final_content)

        return final_content.strip()

    def _process_stream(self, messages: list, user_input: str,
                        max_rounds: int = 20) -> str:
        """流式处理路径 —— 事件驱动（provider yield 事件 dict）"""
        full_content = ""
        tool_rounds = 0

        while tool_rounds < max_rounds:
            tool_rounds += 1

            accumulated = ""
            tool_calls = None

            try:
                for event in self.provider.chat_stream(
                    messages=messages,
                    tools=self.tools.to_openai_tools(),
                ):
                    etype = event.get("type", "")
                    if etype == "content":
                        chunk = event.get("data", "")
                        accumulated += chunk
                        if self.on_stream:
                            self.on_stream(chunk)
                    elif etype == "tool_calls":
                        tool_calls = event.get("data", [])
                    elif etype == "usage":
                        # Track usage from stream
                        if self.context_engine:
                            self.context_engine.update_from_response(event.get("data", {}))
            except Exception as e:
                if self.on_stream:
                    self.on_stream(f"\n\n[错误: {e}]")
                break

            full_content += accumulated

            if not tool_calls:
                break  # Done — no tools to execute

            if tool_rounds >= max_rounds:
                break

            # 执行工具（交错模式：流式文本 + 工具结果）
            for tc in tool_calls:
                name = tc.get("function", {}).get("name", "")
                args_raw = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
                tc_id = tc.get("id", "")

                result = self._execute_tool(name, args)
                asst = {"role": "assistant",
                       "content": accumulated if accumulated else None}
                asst["tool_calls"] = [
                    {"id": tc_id, "type": "function",
                     "function": {"name": name,
                                  "arguments": args_raw}}
                ]
                messages.append(asst)
                messages.append({"role": "tool",
                                "tool_call_id": tc_id,
                                "content": str(result)})
                if self.on_stream:
                    self.on_stream(f"\n🛠️ {name}: {str(result)[:100]}")
                accumulated = ""  # Reset for next round

        self.history.append({"role": "user", "content": user_input})
        self.history.append({"role": "assistant", "content": full_content})
        self.state.save_history(self.history)

        # ── 自动压缩检查 ──
        self._auto_compress_check(messages)

        if self._turn_count % 5 == 0:
            self._auto_crystallize()

        return full_content.strip()

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

                response = self.provider.chat(
                    messages=messages,
                    tools=self.tools.to_openai_tools(),
                )

                # ── 跟踪 token 用量 ──
                self._update_compression_tracking(response)

                return response

            except Exception as e:
                last_error = e
                category, should_retry, should_backoff = classify_error(e)

                # 格式错误：去掉工具再试
                if category == ErrorCategory.FORMAT and attempt == 0:
                    try:
                        response = self.provider.chat(messages=messages)
                        self._update_compression_tracking(response)
                        return response
                    except Exception as e2:
                        last_error = e2
                        return None

                # 认证错误：不重试
                if category == ErrorCategory.AUTH:
                    return None

                # 上下文超长：触发紧急压缩
                if category == ErrorCategory.CONTEXT_OVERFLOW:
                    if self.context_engine:
                        # 强制压缩
                        compressed = self._force_compress()
                        if compressed:
                            messages[:] = compressed
                            continue  # 重试
                    return {"content": "上下文超长，已尝试压缩。请 /clear 后重试。"}

                # 退避
                if should_backoff:
                    wait = min(2 ** attempt * 2, 16)
                    time.sleep(wait)

                if not should_retry or attempt >= max_attempts - 1:
                    return None

        return None

    def _update_compression_tracking(self, response: dict):
        """从 LLM 响应中提取 token 用量并更新 context_engine + state.db"""
        if not self.context_engine:
            return

        # 尝试从响应中提取 usage
        usage = response.get("usage")
        if usage:
            self.context_engine.update_from_response(usage)
            # 也写 state.db
            sid = self.state._current_session_id
            if sid:
                self.state.update_session_usage(session_id=sid, usage=usage)
            return

        # fallback：粗略估算
        total_input = estimate_messages_tokens(
            response.get("_input_messages", [])
        ) if "_input_messages" in response else 0
        output_text = response.get("content", "") or ""
        output_tokens = estimate_content_tokens(output_text)
        usage_fb = {
            "prompt_tokens": total_input,
            "completion_tokens": output_tokens,
        }

        self.context_engine.update_from_response(usage_fb)
        sid = self.state._current_session_id
        if sid:
            self.state.update_session_usage(session_id=sid, usage=usage_fb)

    def _auto_compress_check(self, messages: list):
        """每轮结束后检查是否需要自动压缩"""
        if not self.context_engine:
            return

        # 如果 context_engine 没有真实 token 数据，
        # 用消息数量做粗略触发（至少 history_size+10 条才考虑）
        if self.context_engine.last_prompt_tokens <= 0:
            if len(self.history) < self._history_size + 10:
                return

        if self.context_engine.should_compress():
            self._emit_status("🗜️ 上下文自动压缩中…")
            # 先归档旧消息到 sessions.db
            archive_result = self.state.archive_history(
                keep=50, source=self.state_source
            )
            if archive_result["archived"] > 0:
                self._emit_status(
                    f"📦 已归档 {archive_result['archived']} 条到 sessions.db"
                )
            compressed = self.context_engine.compress(self.history)
            if compressed and len(compressed) < len(self.history):
                self.history = compressed
                self.state.save_history(self.history)
                self.state.mark_compressed()
                self._emit_status(
                    f"✅ 压缩完成: {len(self.history)} 条消息保留"
                )

    def _force_compress(self) -> Optional[list]:
        """紧急强制压缩（上下文超时时触发）"""
        if not self.context_engine:
            return None
        self._emit_status("⚠️ 紧急压缩…")
        try:
            compressed = self.context_engine.compress(self.history, force=True)
            if compressed and len(compressed) < len(self.history):
                self.history = compressed
                self.state.save_history(self.history)
                self.state.mark_compressed()
                self._emit_status(f"✅ 紧急压缩: {len(self.history)} 条保留")
                return compressed
        except Exception:
            pass
        return None

    # ── 工具执行 ────────────────────────────────────

    def _trim_tool_result(self, result: str) -> str:
        """截断过长工具输出"""
        if isinstance(result, str) and len(result) > MAX_TOOL_RESULT_CHARS:
            excess = len(result) - MAX_TOOL_RESULT_CHARS
            return (result[:MAX_TOOL_RESULT_CHARS] +
                    _TOOL_TRUNC_WARNING.format(excess=excess, total=len(result)))
        return result

    def _execute_tool(self, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"未知工具: {name}"

        self.on_tool_progress(name, args)

        if name == "ask":
            return self.on_ask(args.get("question", "需要确认"))

        try:
            result = tool(**args)
            return self._trim_tool_result(str(result) if result is not None else "(空)")
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

        # 压缩状态信息
        if self.context_engine:
            status = self.context_engine.get_status()
            tp = getattr(self.context_engine, 'threshold_percent', 0.75)
            cl = status.get('context_length') or getattr(self.context_engine, 'context_length', 128000)
            parts.append(
                f"\n## 上下文状态\n"
                f"压缩触发线: {status['threshold_tokens']:,} tokens "
                f"({int(tp * 100)}% of {cl:,})\n"
                f"当前用量: {status['usage_percent']:.0f}% "
                f"(已压缩 {status['compression_count']} 次)"
            )

        parts.append(f"\nTurn: {self._turn_count}")
        system = "\n".join(parts)

        messages = [{"role": "system", "content": system}]
        for msg in self.history[-self._history_size:]:
            messages.append(msg)
        messages.append({"role": "user", "content": user_input})
        return messages

    # ── 压缩状态通知 ────────────────────────────────

    def _emit_status(self, msg: str):
        """发送压缩状态通知"""
        if self.on_compression_status:
            try:
                self.on_compression_status(msg)
            except Exception:
                pass

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
            parts = [
                f"niannian-meta v0.2.0",
                f"Turns: {self._turn_count}",
                f"LLM calls in last turn: {self._llm_calls_this_turn}",
                f"Budget: {self._budget.used}/{self._budget.max_total}",
                f"Tools: {len(self.tools.list_tools())}",
                f"History: {len(self.history)} msgs",
            ]
            if self.context_engine:
                status = self.context_engine.get_status()
                parts.append(
                    f"Compression: {status['compression_count']}x "
                    f"({status['usage_percent']:.0f}% used"
                    f"/{status['threshold_tokens']:,} threshold)"
                )
            # state.db 状态
            sinfo = self.state.get_session_info()
            if sinfo:
                parts.append(
                    f"Session: {sinfo['id']} ({sinfo['source']}) "
                    f"[active={sinfo['active_messages']}, "
                    f"total={sinfo['total_messages']}]"
                )
            stats = self.state.get_stats()
            if stats.get("archived_messages", 0) > 0:
                parts.append(
                    f"Archive: {stats['archived_sessions']} sessions / "
                    f"{stats['archived_messages']} msgs"
                )
            parts.append(self.memory.maintain())
            return "\n".join(parts)

        if c == "/rules":
            return self.rules
        if c == "/soul":
            return self.identity_prompt
        if c == "/clear":
            self.state.clear_history()
            self.history.clear()
            self._turn_count = 0
            if self.context_engine:
                self.context_engine.on_session_reset()
            return "会话已清空"

        if c == "/compress" or c.startswith("/compress "):
            return self._manual_compress(c)

        if c == "/learn":
            from evolution.learn import extract_from_conversation, list_skills
            name = extract_from_conversation(self.history[-20:], self)
            if name:
                return f"✅ 已提取技能: {name}\n可用: data/skills/{name}.md"
            return "暂无值得提取的技能"

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
        if c.startswith("/compression"):
            return self._compression_status()
        if c == "/usage":
            return self._usage_info()
        if c == "/state":
            return self._state_status()
        return f"未知: {cmd}"

    def _manual_compress(self, cmd: str) -> str:
        """手动触发压缩 (使用 session-trim-v2.py 或内置引擎)"""
        parts = cmd.split()
        focus = " ".join(parts[1:]) if len(parts) > 1 else ""

        # 先尝试用 session-trim 脚本（如果可用）
        import os
        trim_script = os.path.expanduser(
            "~/niannian/repo/shared/tools/session-trim-v2.py"
        )
        if os.path.exists(trim_script):
            import subprocess
            try:
                result = subprocess.run(
                    ["python3", trim_script, "50"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    return f"✅ session-trim 压缩完成:\n{result.stdout.strip()}"
            except Exception as e:
                pass

        # fallback: 使用内置引擎
        if not self.context_engine:
            return "上下文压缩未启用"

        # 先归档旧消息到 sessions.db
        archive_result = self.state.archive_history(
            keep=50, source=self.state_source
        )
        result_parts = []
        if archive_result["archived"] > 0:
            result_parts.append(
                f"📦 已归档 {archive_result['archived']} 条到 sessions.db"
            )

        compressed = self.context_engine.compress(self.history)
        if compressed and len(compressed) < len(self.history):
            old_len = len(self.history)
            self.history = compressed
            self.state.save_history(self.history)
            self.state.mark_compressed()
            result_parts.append(
                f"✅ 内置压缩: {old_len} → {len(compressed)} 条 "
                f"(累计 {self.context_engine.compression_count} 次)"
            )
        else:
            result_parts.append("当前消息太少或无必要压缩")

        return "\n".join(result_parts)

    def _compression_status(self) -> str:
        """显示压缩引擎 + state 状态"""
        lines = []
        if self.context_engine:
            s = self.context_engine.get_status()
            lines = [
                f"引擎: {self.context_engine.name}",
                f"压缩次数: {s['compression_count']}",
                f"当前用量: {s['usage_percent']:.0f}%",
                f"触发阈值: {s['threshold_tokens']:,} tokens "
                f"(={int(self.context_engine.threshold_percent * 100)}% "
                f"of {s['context_length']:,})",
                f"保护首条: {self.context_engine.protect_first_n}",
                f"保护尾条: {self.context_engine.protect_last_n}",
            ]
            s_e = self.context_engine
            if hasattr(s_e, '_last_compress_aborted') and s_e._last_compress_aborted:
                lines.append(f"末次失败: {s_e._last_summary_error}")
        else:
            lines = ["压缩引擎未启用"]

        # DB 统计
        stats = self.state.get_stats()
        lines.extend([
            "",
            "** state.db **",
            f"总会话: {stats['sessions']}",
            f"总消息: {stats['messages']}",
            f"已压缩: {stats['compressed']}",
        ])
        return "\n".join(lines)

    def _state_status(self) -> str:
        """显示 state.db + sessions.db 状态"""
        sinfo = self.state.get_session_info()
        stats = self.state.get_stats()
        lines = [
            "--- state.db ---",
            f"DB 路径: {self.state.db_path}",
        ]
        if sinfo:
            lines.append(f"当前会话: {sinfo['id']}")
            lines.append(f"来源: {sinfo['source']}")
            lines.append(f"活跃消息: {sinfo['active_messages']}")
            lines.append(f"总消息: {sinfo['total_messages']}")
        lines.extend([
            "",
            "--- sessions.db（归档） ---",
            f"已归档会话: {stats.get('archived_sessions', 0)}",
            f"已归档消息: {stats.get('archived_messages', 0)}",
            "",
            "--- 全局统计 ---",
            f"总会话: {stats['sessions']}",
            f"总消息: {stats['messages']}",
            f"已压缩: {stats['compressed']}",
        ])
        return "\n".join(lines)

    def _usage_info(self) -> str:
        """显示 token 用量"""
        sid = self.state._current_session_id
        if not sid:
            return "无活跃会话"
        usage = self.state.get_session_usage(session_id=sid)
        if not usage:
            return "尚无 token 数据"
        lines = [f"会话: {sid[:8]}"]
        for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "reasoning_tokens"):
            v = usage.get(k, 0)
            if v:
                lines.append(f"  {k}: {v:,}")
        total = sum(
            v for k, v in usage.items()
            if k in ("input_tokens", "output_tokens",
                     "cache_read_tokens", "cache_write_tokens",
                     "reasoning_tokens")
        )
        lines.append(f"  total: {total:,}")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════
# run.py 辅助 — 优雅关闭
# ══════════════════════════════════════════════════════

def setup_graceful_shutdown(agent):
    """注册 signal handler 实现优雅关闭"""
    import signal

    def _shutdown(signum, frame):
        print(f"\n🛑 收到信号 {signum}，正在关闭...")
        try:
            # 归档活跃消息
            result = agent.state.archive_history(
                keep=50, source=getattr(agent, 'state_source', 'standalone')
            )
            if result["archived"] > 0:
                print(f"📦 已归档 {result['archived']} 条到 sessions.db")
            # 结束会话
            agent.state.end_session("interrupted")
            print("✅ 会话已结束")
        except Exception as e:
            print(f"⚠️ 关闭异常: {e}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
