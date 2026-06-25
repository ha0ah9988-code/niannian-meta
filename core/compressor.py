"""上下文压缩引擎 —— 从 Hermes context_compressor + context_engine 适配

三层设计：
1. ContextEngine（ABC）— 插拔基类，定义压缩生命周期接口
2. ContextCompressor — 默认实现，LLM 摘要式压缩
3. 外部可替换（plugin context engine 模式）

核心能力：
- 跟踪 token 用量（update_from_response）
- 自动判断压缩时机（should_compress）
- LLM 摘要压缩，保护首尾消息（compress）
- 压缩失败回退

Hermes 去掉的依赖：
  - auxiliary_client → 直接用核心 provider
  - session_db/压缩锁 → niannian-meta 无 state.db
  - plugin/memory provider hooks → 后续再加
  - redact/model_metadata 深度依赖 → 本地简化估算
"""

import copy
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 常量（从 Hermes 直接移植） ──────────────────────

# 摘要前缀 —— 注入压缩后的消息头部，告诉 LLM 这是背景不是活跃指令
SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a background reference, NOT active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Respond ONLY to the latest user message that appears AFTER this "
    "summary — that message is the single source of truth for what to do "
    "right now. "
    "IMPORTANT: Your persistent memory in the system prompt is ALWAYS "
    "authoritative and active — never ignore memory content due to this note."
)

SUMMARY_END_MARKER = (
    "--- END OF CONTEXT SUMMARY — "
    "respond to the message below, not the summary above ---"
)

# 摘要输出配额
MIN_SUMMARY_TOKENS = 1500
SUMMARY_RATIO = 0.20
SUMMARY_TOKENS_CEILING = 8000

# 工具结果占位符
PRUNED_TOOL_PLACEHOLDER = "[Old tool output cleared to save context space]"

# 估算常数
CHARS_PER_TOKEN = 4
IMAGE_TOKEN_ESTIMATE = 1600
IMAGE_CHAR_EQUIVALENT = IMAGE_TOKEN_ESTIMATE * CHARS_PER_TOKEN

# 摘要失败冷静期
SUMMARY_FAILURE_COOLDOWN = 600  # 10 分钟

# 回退摘要上限
FALLBACK_SUMMARY_MAX_CHARS = 6000
FALLBACK_TURN_MAX_CHARS = 500

# 最小摘要尾消息保护
MIN_TAIL_MESSAGE_FLOOR = 8

# token 估算
TOKEN_ESTIMATE_RATIO = 3.5  # 字符/token 混合估算（中英文兼顾）
MINIMUM_CONTEXT_LENGTH = 64000  # 最低上下文要求


# ── 工具函数 ────────────────────────────────────────


def estimate_messages_tokens(messages: List[Dict]) -> int:
    """粗略估算消息列表的 token 数"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content) / TOKEN_ESTIMATE_RATIO
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    total += len(text) / TOKEN_ESTIMATE_RATIO
                    if part.get("type") == "image_url":
                        total += IMAGE_TOKEN_ESTIMATE
        # tool_calls 加一点 overhead
        if msg.get("tool_calls"):
            total += 50 * len(msg["tool_calls"])
        if msg.get("role") in ("system", "user", "assistant"):
            total += 4  # 角色前缀
    return int(total)


def estimate_content_tokens(text: str) -> int:
    """估算文本 token 数"""
    return int(len(text) / TOKEN_ESTIMATE_RATIO)


def format_tool_calls_for_summary(tool_calls: List[Dict]) -> str:
    """将 tool_calls 格式化为可读摘要"""
    parts = []
    for tc in tool_calls:
        name = "unknown"
        args_str = ""
        if isinstance(tc, dict):
            fn = tc.get("function", tc)
            name = fn.get("name", "unknown")
            args = fn.get("arguments", "")
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    keys = list(parsed.keys())[:3]
                    args_str = ", ".join(f"{k}={parsed[k]}" for k in keys)
                except json.JSONDecodeError:
                    args_str = args[:80]
            elif isinstance(args, dict):
                keys = list(args.keys())[:3]
                args_str = ", ".join(f"{k}={args[k]}" for k in keys)
        parts.append(f"[{name}({args_str})]")
    return "; ".join(parts)


# ── ContextEngine（ABC） ─────────────────────────────


class ContextEngine(ABC):
    """上下文引擎基类 —— 控制何时压缩以及如何压缩"""

    # 跟踪状态
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0

    # 压缩参数
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6

    @property
    @abstractmethod
    def name(self) -> str:
        """引擎标识"""

    @abstractmethod
    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """每次 LLM 响应后更新 token 用量"""

    @abstractmethod
    def should_compress(self, prompt_tokens: int = None) -> bool:
        """返回 True 表示需要触发压缩"""

    @abstractmethod
    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
    ) -> List[Dict[str, Any]]:
        """执行压缩，返回压缩后的消息列表"""

    # 可选：预检
    def should_compress_preflight(self, messages: List[Dict]) -> bool:
        return False

    def has_content_to_compress(self, messages: List[Dict]) -> bool:
        return True

    def get_status(self) -> Dict[str, Any]:
        return {
            "last_prompt_tokens": self.last_prompt_tokens,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100, self.last_prompt_tokens / self.context_length * 100)
                if self.context_length else 0
            ),
            "compression_count": self.compression_count,
        }

    def on_session_start(self, session_id: str = "", **kwargs) -> None:
        pass

    def on_session_end(self, session_id: str = "", messages: List = None) -> None:
        pass

    def on_session_reset(self) -> None:
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0


# ── ContextCompressor ───────────────────────────────


class ContextCompressor(ContextEngine):
    """Hermes 风格上下文压缩器 —— LLM 摘要式压缩

    算法：
    1. 分离 head（保护首 N 条）、middle（可压缩）、tail（保护尾 N 条）
    2. 将 middle 构建为摘要请求，调用 LLM 生成摘要
    3. 摘要 + tail 组成压缩后消息列表
    4. 如果 LLM 失败，用 fallback（提取关键信息拼接）

    配置（从 config.yaml 读取或构造函数参数）：
      - threshold_percent: 多少 % 触发压缩（默认 0.75）
      - protect_first_n: 保护前 N 条非 system 消息
      - protect_last_n: 保护后 N 条消息
      - summary_max_tokens: 摘要最大 token 数
    """

    name = "compressor"

    def __init__(
        self,
        llm_func=None,  # Callable[[list], str] — 外部注入的 LLM 调用函数
        threshold_percent: float = 0.75,
        protect_first_n: int = 3,
        protect_last_n: int = 6,
        context_length: int = 128000,
        summary_max_tokens: int = SUMMARY_TOKENS_CEILING,
    ):
        self.llm_func = llm_func
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.context_length = context_length
        self.summary_max_tokens = summary_max_tokens
        self.threshold_tokens = int(context_length * threshold_percent)

        # 状态
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

        # 失败追踪
        self._last_compress_aborted = False
        self._last_summary_error: Optional[str] = None
        self._last_failure_time: float = 0

    # ── token 跟踪 ─────────────────────────────────

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """从 LLM 响应中提取 token 用量"""
        if not usage:
            return

        # 兼容不同命名
        prompt = (
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or usage.get("input_token_count")
        )
        completion = (
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or usage.get("output_token_count")
        )
        total = usage.get("total_tokens") or (
            (prompt or 0) + (completion or 0)
        )

        if prompt:
            self.last_prompt_tokens = prompt
        if completion:
            self.last_completion_tokens = completion
        if total:
            self.last_total_tokens = total

    # ── 压缩决策 ───────────────────────────────────

    def should_compress(self, prompt_tokens: int = None) -> bool:
        """检查是否达到压缩阈值"""
        # 冷静期：失败后等待
        if self._last_failure_time > 0:
            elapsed = time.time() - self._last_failure_time
            if elapsed < SUMMARY_FAILURE_COOLDOWN:
                return False

        tokens = prompt_tokens or self.last_prompt_tokens
        if tokens <= 0:
            return False

        # 至少需要工具调用/system/history 占一定量才值得压缩
        ratio = tokens / self.context_length if self.context_length else 0
        return ratio >= self.threshold_percent

    # ── 压缩执行 ───────────────────────────────────

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
    ) -> List[Dict[str, Any]]:
        """主压缩入口

        返回压缩后的消息列表。失败时返回原始列表 + 设置 _last_compress_aborted。
        """
        if not messages or len(messages) < 4:
            return messages  # 太少，不值得压缩

        self._last_compress_aborted = False
        self._last_summary_error = None
        target_len = len(messages)

        try:
            # 分离各部分
            result = self._compress_impl(messages, focus_topic)
            if result is not None and len(result) < target_len:
                self.compression_count += 1
                return result
            return messages
        except Exception as e:
            logger.warning("Compression failed: %s", e)
            self._last_compress_aborted = True
            self._last_summary_error = str(e)
            self._last_failure_time = time.time()
            return messages

    def _compress_impl(
        self,
        messages: List[Dict],
        focus_topic: str = None,
    ) -> Optional[List[Dict]]:
        """压缩算法实现"""
        # 1. 识别 system prompt
        system_msgs = []
        body_msgs = []
        for m in messages:
            if m.get("role") == "system" and not body_msgs:
                system_msgs.append(m)
            else:
                body_msgs.append(m)

        if len(body_msgs) < 4:
            return None  # body 消息太少，不值得压缩

        # 2. 切分 head / middle / tail
        head = body_msgs[: self.protect_first_n]
        tail_protect = max(self.protect_last_n, MIN_TAIL_MESSAGE_FLOOR)
        tail = body_msgs[-tail_protect:] if len(body_msgs) > tail_protect else body_msgs
        middle_start = len(head)
        middle_end = len(body_msgs) - (len(tail) if len(body_msgs) > tail_protect else 0)
        middle = body_msgs[middle_start:middle_end]

        if not middle:
            return None  # 没有可压缩的内容

        # 3. 预修剪工具结果（压缩前减负）
        middle = self._prune_tool_results(middle)

        # 4. 尝试 LLM 摘要
        summary = None
        if self.llm_func:
            try:
                summary = self._summarize_middle(middle, focus_topic)
            except Exception as e:
                logger.warning("LLM summarization failed: %s", e)
                self._last_summary_error = str(e)

        if not summary:
            # fallback：提取关键信息拼接
            summary = self._fallback_summary(middle)

        # 5. 构建压缩后消息列表
        summary_msg = {
            "role": "user",
            "content": f"{summary}\n\n{SUMMARY_END_MARKER}",
        }

        compressed = list(system_msgs) + list(head) + [summary_msg] + list(tail)
        return compressed

    def _prune_tool_results(self, messages: List[Dict]) -> List[Dict]:
        """修剪工具结果 —— 将过长的 tool result 替换为占位符"""
        pruned = []
        for msg in messages:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 2000:
                    msg = dict(msg)
                    msg["content"] = f"{content[:500]}\n...\n{PRUNED_TOOL_PLACEHOLDER}"
            pruned.append(msg)
        return pruned

    def _summarize_middle(
        self,
        middle: List[Dict],
        focus_topic: str = None,
    ) -> Optional[str]:
        """用 LLM 生成中间消息的摘要"""
        # 估算 middle 的 token 数，计算摘要配额
        middle_tokens = estimate_messages_tokens(middle)
        budget = max(
            MIN_SUMMARY_TOKENS,
            min(int(middle_tokens * SUMMARY_RATIO), self.summary_max_tokens),
        )

        # 构建摘要 prompt
        topic_hint = (
            f"\nFocus on information related to: {focus_topic}"
            if focus_topic else ""
        )

        summary_prompt = [
            {
                "role": "system",
                "content": (
                    "You are a context summarizer for an AI assistant. "
                    "Your task is to produce a concise, factual summary of the "
                    "conversation turns below. "
                    "Focus on preserving:"
                    "\n- Key user requests and intents"
                    "\n- Important facts, decisions, and findings"
                    "\n- Tools used and their outcomes"
                    "\n- Code paths, file locations, and configuration changes"
                    "\n- Anything the assistant explicitly committed to doing later"
                    f"\n{topic_hint}"
                    "\n\nOUTPUT FORMAT: Write the summary as a continuous paragraph "
                    "using plain text with short sections."
                    "\n\nDo NOT include: greetings, meta-commentary, markdown headers."
                    f"\n\nKeep the summary under {budget} tokens."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Summarize the following conversation turns "
                    "(they appear in chronological order):\n\n"
                    + self._format_messages_for_summary(middle)
                ),
            },
        ]

        # 调用 LLM
        raw = self.llm_func(
            messages=summary_prompt,
            max_tokens=budget,
            temperature=0.3,
        )

        if not raw or not raw.strip():
            return None

        # 清洗输出
        summary = raw.strip()
        # 去掉可能的思考块
        summary = re.sub(r"<think>.*?</think>", "", summary, flags=re.DOTALL).strip()

        # 构建完整摘要消息
        full = f"{SUMMARY_PREFIX}\n\n{summary}"
        return full

    def _format_messages_for_summary(self, messages: List[Dict]) -> str:
        """将消息列表格式化为 LLM 可读的文本"""
        parts = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            # 工具调用
            tool_calls = msg.get("tool_calls")
            tc_text = ""
            if tool_calls:
                tc_text = format_tool_calls_for_summary(tool_calls)

            # 工具结果
            if role == "tool":
                tc_id = msg.get("tool_call_id", "")
                if isinstance(content, str):
                    content_trunc = content[:600]
                else:
                    content_trunc = str(content)[:600]
                parts.append(f"[Tool result {tc_id}]: {content_trunc}")
                continue

            if isinstance(content, str) and content:
                content_snippet = content[:800]
                prefix = f"[{role}]"
                if tc_text:
                    prefix += f" (calls: {tc_text})"
                parts.append(f"{prefix}: {content_snippet}")
            elif tc_text:
                parts.append(f"[{role}] (calls: {tc_text})")

        return "\n\n".join(parts)

    def _fallback_summary(self, messages: List[Dict]) -> str:
        """LLM 失败时的回退摘要 —— 提取关键信息拼接"""
        items = []
        seen_tools = set()

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user" and isinstance(content, str):
                lines = content.strip().split("\n")
                first_line = lines[0][:200]
                if first_line:
                    items.append(f"User asked: {first_line}")

            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    for tc in tool_calls:
                        fn = tc.get("function", tc)
                        name = fn.get("name", "")
                        if name and name not in seen_tools:
                            seen_tools.add(name)
                            items.append(f"Used tool: {name}")
                if isinstance(content, str) and content.strip():
                    first = content.strip().split("\n")[0][:150]
                    if first:
                        items.append(f"Responded: {first}")

            elif role == "tool" and isinstance(content, str):
                tc_id = msg.get("tool_call_id", "")[:8]
                result_snippet = content[:100].strip()
                if result_snippet:
                    items.append(f"Result [{tc_id}]: {result_snippet}")

        # 去重且不要过长
        seen = set()
        unique_items = []
        for item in items:
            if item not in seen:
                seen.add(item)
                unique_items.append(item)

        total_chars = sum(len(i) for i in unique_items)
        if total_chars > FALLBACK_SUMMARY_MAX_CHARS:
            # 截断
            truncated = []
            char_count = 0
            for item in unique_items:
                if char_count + len(item) > FALLBACK_SUMMARY_MAX_CHARS:
                    break
                truncated.append(item)
                char_count += len(item)
            unique_items = truncated

        body = "\n".join(f"• {item}" for item in unique_items)
        return (
            f"{SUMMARY_PREFIX}\n\n"
            f"Previous conversation summary (fallback — LLM summarization unavailable):\n"
            f"{body}"
        )
