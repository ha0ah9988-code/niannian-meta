"""niannian-meta 核心 —— 从身份文件加载身份与规则"""

import os
from pathlib import Path

IDENTITY_DIR = Path(__file__).parent.parent / "identity"


def load_soul() -> str:
    """加载 soul.md —— 系统提示词的核心身份块"""
    path = IDENTITY_DIR / "soul.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "# soul.md not found"


def load_user() -> str:
    """加载 user.md —— 主人信息"""
    path = IDENTITY_DIR / "user.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "# user.md not found"


def load_rules() -> str:
    """加载 rules.md —— 最高宪法"""
    path = IDENTITY_DIR / "rules.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "# rules.md not found"


def load_agents() -> str:
    """加载 agents.md —— 项目架构索引"""
    path = IDENTITY_DIR / "agents.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "# agents.md not found"


def build_identity_prompt() -> str:
    """构建注入 system prompt 的身份块（soul + user）

    rules.md 和 agents.md 不注入，按需 consult。
    """
    soul = load_soul()
    user = load_user()
    return f"{soul}\n\n{user}"
