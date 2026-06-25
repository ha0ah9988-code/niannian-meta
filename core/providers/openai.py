"""OpenAI 兼容 API 提供商 —— 支持 stream + tools + reasoning

适用于：DeepSeek、MiniMax、Mimo、OpenRouter 等所有
兼容 OpenAI /v1/chat/completions 格式的 API。
"""

import json
import os
import httpx
from typing import Optional

from .base import BaseProvider, ProviderError


class OpenAIProvider(BaseProvider):
    """OpenAI 兼容 API 提供商"""

    name = "openai"

    def __init__(self, api_key: str = None, base_url: str = None,
                 model: str = None, extra_body: dict = None):
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
        self.extra_body = extra_body or {}

        # 代理
        self._proxy = (os.environ.get("HTTPS_PROXY")
                       or os.environ.get("HTTP_PROXY")
                       or os.environ.get("https_proxy")
                       or os.environ.get("http_proxy")
                       or None)

    def _build_client(self) -> httpx.Client:
        kwargs = {"timeout": 120}
        if self._proxy:
            kwargs["proxy"] = self._proxy
        return httpx.Client(**kwargs)

    def chat(self, messages: list, tools: list = None,
             max_tokens: int = 4096) -> dict:
        """非流式对话"""
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            **self.extra_body,
        }
        if tools:
            body["tools"] = tools

        with self._build_client() as client:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            )

            if resp.status_code != 200:
                self._raise_error(resp)

            data = resp.json()
            msg = data["choices"][0]["message"]

            result = {"content": msg.get("content", "")}
            if msg.get("reasoning_content"):
                result["reasoning_content"] = msg["reasoning_content"]
            if msg.get("tool_calls"):
                result["tool_calls"] = [
                    {"id": tc.get("id", ""),
                     "name": tc["function"]["name"],
                     "arguments": json.loads(tc["function"]["arguments"])}
                    for tc in msg["tool_calls"]
                ]

            return result

    def chat_stream(self, messages: list, tools: list = None,
                    max_tokens: int = 4096):
        """流式对话 —— yield 事件 dict

        事件类型：
          {"type": "content", "data": "文本块"}
          {"type": "tool_calls", "data": [tool_call_dict, ...]}
          {"type": "usage", "data": {"prompt_tokens": N, ...}}
          {"type": "done", "data": {}}
        """
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self.extra_body,
        }
        if tools:
            body["tools"] = tools

        with self._build_client() as client:
            with client.stream("POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            ) as resp:
                if resp.status_code != 200:
                    self._raise_error(resp)
                    return

                buffer = {}
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        # Usage info (final chunk)
                        if chunk.get("usage"):
                            yield {"type": "usage", "data": chunk["usage"]}

                        choices = chunk.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})

                        # Content text
                        content = delta.get("content", "")
                        if content:
                            yield {"type": "content", "data": content}

                        # Tool calls (streaming deltas — need assembly)
                        if "tool_calls" in delta:
                            for tc_delta in delta["tool_calls"]:
                                idx = tc_delta.get("index", 0)
                                if idx not in buffer:
                                    buffer[idx] = {
                                        "id": "",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                tcc = buffer[idx]
                                if tc_delta.get("id"):
                                    tcc["id"] = tc_delta["id"]
                                if "function" in tc_delta:
                                    fn = tc_delta["function"]
                                    if fn.get("name"):
                                        tcc["function"]["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        tcc["function"]["arguments"] += fn["arguments"]

                # Assemble complete tool calls
                if buffer:
                    calls = []
                    for idx in sorted(buffer):
                        tc = buffer[idx]
                        args_str = tc["function"]["arguments"]
                        try:
                            args = json.loads(args_str) if args_str else {}
                        except json.JSONDecodeError:
                            args = {"_raw": args_str}
                        calls.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": json.dumps(args),
                            },
                        })
                    yield {"type": "tool_calls", "data": calls}

                yield {"type": "done", "data": {}}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _raise_error(self, resp):
        detail = resp.text[:500]
        status = resp.status_code

        if status in (401, 403):
            raise ProviderError(detail, status, retryable=False, category="auth")
        elif status == 429:
            raise ProviderError(detail, status, retryable=True, category="rate_limit")
        elif status in (500, 502, 503):
            raise ProviderError(detail, status, retryable=True, category="server")
        elif status == 400:
            # DeepSeek reasoning_content 错误不可重试
            if "reasoning_content" in detail:
                raise ProviderError(detail, status, retryable=False, category="format")
            raise ProviderError(detail, status, retryable=False, category="format")
        else:
            raise ProviderError(detail, status, retryable=True, category="unknown")
