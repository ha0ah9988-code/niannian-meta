"""Fallback 链 —— 自动切换提供商

配置多个 provider，当前一个失败时自动切换下一个。
"""

import time
from typing import Optional

from .base import BaseProvider, ProviderError
from . import ProviderRegistry


class FallbackChain(BaseProvider):
    """Fallback 链 —— 当一个 provider 失败时尝试下一个

    用法：
        chain = FallbackChain(registry)
        chain.add_filter(...)  # 可选：过滤可 fallback 的错误类型
        result = chain.chat(messages)
    """

    name = "fallback"

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry
        self._retryable_categories = {
            "rate_limit", "timeout", "server", "unknown",
        }
        self._max_attempts_per_provider = 2
        self._backoff_base = 2

    def chat(self, messages: list, tools: list = None,
             max_tokens: int = 4096) -> dict:
        """按 provider 顺序尝试，失败则 fallback"""
        providers = self.registry.list()
        last_error = None

        for provider_name in providers:
            provider = self.registry.get(provider_name)
            if provider is None:
                continue

            for attempt in range(self._max_attempts_per_provider):
                try:
                    return provider.chat(messages, tools, max_tokens)
                except ProviderError as e:
                    last_error = e

                    # 不可重试 → 立即 fallback 到下一个 provider
                    if not e.retryable and e.category not in self._retryable_categories:
                        break

                    # 可重试 → 退避后重试
                    if attempt < self._max_attempts_per_provider - 1:
                        wait = self._backoff_base ** attempt
                        time.sleep(wait)
                    # 最后一次尝试失败 → fallback 到下一个 provider

        # 所有 provider 都失败
        raise last_error or ProviderError("所有 provider 都不可用", retryable=False)

    def chat_stream(self, messages: list, tools: list = None,
                    max_tokens: int = 4096):
        """流式版本 —— 同样 fallback 逻辑"""
        providers = self.registry.list()
        last_error = None

        for provider_name in providers:
            provider = self.registry.get(provider_name)
            if provider is None or not provider.supports_streaming:
                continue

            for attempt in range(self._max_attempts_per_provider):
                try:
                    yield from provider.chat_stream(messages, tools, max_tokens)
                    return
                except ProviderError as e:
                    last_error = e
                    if not e.retryable:
                        break
                    if attempt < self._max_attempts_per_provider - 1:
                        time.sleep(self._backoff_base ** attempt)

        raise last_error or ProviderError("所有 provider stream 都不可用", retryable=False)
