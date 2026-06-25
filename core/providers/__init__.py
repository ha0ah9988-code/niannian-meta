"""提供商注册表 —— 管理和切换 LLM 提供商"""

from typing import Optional
from .base import BaseProvider


class ProviderRegistry:
    """提供商注册表

    管理多个 LLM 提供商，支持按名称查找和 fallback 链。
    """

    def __init__(self):
        self._providers: dict[str, BaseProvider] = {}
        self._default: Optional[str] = None

    def register(self, name: str, provider: BaseProvider, set_default: bool = False):
        """注册一个提供商"""
        self._providers[name] = provider
        if set_default or self._default is None:
            self._default = name

    def get(self, name: str = None) -> Optional[BaseProvider]:
        """获取提供商实例"""
        name = name or self._default
        return self._providers.get(name)

    def list(self) -> list[str]:
        """列出所有已注册的提供商"""
        return list(self._providers.keys())

    @property
    def default(self) -> Optional[BaseProvider]:
        return self._providers.get(self._default)

    @property
    def default_name(self) -> Optional[str]:
        return self._default

    def set_default(self, name: str):
        if name in self._providers:
            self._default = name
