"""niannian-meta 核心 —— 从身份文件加载身份与规则"""

import os
from pathlib import Path

IDENTITY_DIR = Path(__file__).parent.parent / "identity"


def load_soul() -> str:
    path = IDENTITY_DIR / "soul.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "# soul.md not found"


def load_user() -> str:
    path = IDENTITY_DIR / "user.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "# user.md not found"


def load_rules() -> str:
    path = IDENTITY_DIR / "rules.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "# rules.md not found"


def load_agents() -> str:
    path = IDENTITY_DIR / "agents.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "# agents.md not found"


def build_identity_prompt() -> str:
    soul = load_soul()
    user = load_user()
    return f"{soul}\n\n{user}"


def setup_providers():
    """配置默认的 LLM 提供商注册表

    读取环境变量，注册可用提供商。
    如果配置了多个，自动启用 fallback 链。
    """
    from core.providers import ProviderRegistry
    from core.providers.openai import OpenAIProvider

    registry = ProviderRegistry()

    # 默认 OpenAI 兼容（从 env 读取）
    api_key = (os.environ.get("LLM_API_KEY")
               or os.environ.get("OPENAI_API_KEY")
               or "")
    base_url = (os.environ.get("LLM_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://opencode.ai/zen/go/v1")
    model = (os.environ.get("LLM_MODEL")
             or "deepseek-v4-flash")

    if api_key:
        registry.register("default", OpenAIProvider(
            api_key=api_key, base_url=base_url, model=model,
            extra_body={"reasoning_effort": "max"} if "deepseek" in model else {},
        ), set_default=True)

    # 如果注册了多个，启用 fallback 链
    if len(registry.list()) > 1:
        from core.providers.fallback import FallbackChain
        chain = FallbackChain(registry)
        registry.register("fallback", chain)

    return registry
