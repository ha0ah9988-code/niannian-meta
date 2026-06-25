"""LLM 提供商抽象基类 —— 所有 provider 继承此接口"""

from typing import Optional


class ProviderError(Exception):
    """提供商调用错误"""
    def __init__(self, message: str, status_code: int = None,
                 retryable: bool = True, category: str = "unknown"):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.category = category  # auth|rate_limit|timeout|format|server|unknown


class BaseProvider:
    """LLM 提供商基类

    每个子类实现 chat() 和 chat_stream() 方法。
    """

    name: str = "base"
    supports_streaming: bool = True
    supports_tools: bool = True

    def chat(self, messages: list, tools: list = None,
             max_tokens: int = 4096) -> dict:
        """非流式对话

        Returns:
            {"content": str, "tool_calls": [...],
             "reasoning_content": str (optional)}
        """
        raise NotImplementedError

    def chat_stream(self, messages: list, tools: list = None,
                    max_tokens: int = 4096):
        """流式对话 —— yield 文本块

        Yields:
            str: 文本块
        """
        raise NotImplementedError

    def count_tokens(self, messages: list) -> int:
        """估算消息 token 数（粗略）"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 2  # 中英文粗略估算
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += len(part.get("text", "")) // 2
        return total
