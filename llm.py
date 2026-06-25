"""LLM 抽象层 —— 内核与 LLM 之间的薄桥接。

设计原则：
- 不绑定任何特定 provider（OpenAI / Anthropic / 本地都兼容）
- 用 adapter 模式切换后端
- 内核不直接 import 任何 LLM SDK
"""

import json
import os
from typing import Optional


class LLMError(Exception):
    """LLM 调用失败"""
    pass


class BaseLLM:
    """LLM 基类 —— 内核只依赖这个接口"""

    def chat(self, messages: list, tools: Optional[list] = None,
             max_tokens: int = 4096) -> dict:
        """发送对话，返回 {"content": str, "tool_calls": [...]}"""
        raise NotImplementedError

    def chat_stream(self, messages: list, tools: Optional[list] = None):
        """流式对话，yield 文本块"""
        raise NotImplementedError


class OpenAICompatibleLLM(BaseLLM):
    """兼容 OpenAI API 格式的 LLM

    配置优先级：构造函数参数 > 环境变量 > 默认值
    """

    def __init__(self, api_key: str = None, base_url: str = None,
                 model: str = None, extra_body: dict = None):
        # 尝试从多个来源获取配置
        self.api_key = (api_key
                        or os.environ.get("LLM_API_KEY")
                        or os.environ.get("OPENAI_API_KEY")
                        or "")
        self.base_url = (base_url
                         or os.environ.get("LLM_BASE_URL")
                         or os.environ.get("OPENAI_BASE_URL")
                         or "https://opencode.ai/zen/go/v1")
        self.model = (model
                      or os.environ.get("LLM_MODEL")
                      or "deepseek-v4-flash")
        # 额外的请求参数（reasoning_effort 等）
        self.extra_body = extra_body or {}

    def chat(self, messages: list, tools: Optional[list] = None,
             max_tokens: int = 4096) -> dict:
        """调用 LLM 并返回响应"""
        import httpx

        # httpx 不自动读取 HTTP_PROXY 环境变量，需要手动配置
        proxy_url = (os.environ.get("HTTPS_PROXY")
                     or os.environ.get("HTTP_PROXY")
                     or os.environ.get("https_proxy")
                     or os.environ.get("http_proxy")
                     or None)
        client_kwargs = {"timeout": 120}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            **self.extra_body,
        }
        if tools:
            body["tools"] = tools

        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                if resp.status_code != 200:
                    detail = resp.text[:500]
                    raise LLMError(f"HTTP {resp.status_code}: {detail}")
                data = resp.json()
        except Exception as e:
            raise LLMError(f"LLM call failed: {e}")

        choice = data["choices"][0]
        msg = choice["message"]

        result = {"content": msg.get("content", "")}

        if msg.get("tool_calls"):
            result["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "name": tc["function"]["name"],
                    "arguments": json.loads(tc["function"]["arguments"]),
                }
                for tc in msg["tool_calls"]
            ]

        return result


def create_llm(provider: str = "openai", **kwargs) -> BaseLLM:
    """工厂方法：创建 LLM 实例"""
    if provider == "openai":
        return OpenAICompatibleLLM(**kwargs)
    raise ValueError(f"Unknown LLM provider: {provider}")
