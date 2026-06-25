"""原子工具系统 —— 内核的工具注册表和基础工具。

设计原则：
- 最少原子工具：内核只装能独立存活的最小工具集
- 工具即能力种子：不预装"功能"，只装可以长出功能的工具
- 可热插拔：工具可以在运行时注册/注销
"""

import os
import subprocess
import json
from typing import Any, Callable, Optional


class Tool:
    """单个工具的抽象"""
    def __init__(self, name: str, description: str,
                 handler: Callable, parameters: dict):
        self.name = name
        self.description = description
        self.handler = handler
        self.parameters = parameters

    def to_openai_tool(self) -> dict:
        """转为 OpenAI 工具格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": self.parameters},
            },
        }

    def __call__(self, **kwargs) -> Any:
        return self.handler(**kwargs)


def tool(name: str, description: str, parameters: dict):
    """装饰器：注册一个工具 handler"""
    def decorator(func):
        func._tool_meta = {"name": name, "description": description,
                          "parameters": parameters}
        return func
    return decorator


class ToolRegistry:
    """工具注册表 —— 内核通过它发现和调用工具"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def register_func(self, name: str, description: str,
                      parameters: dict, handler: Callable):
        self._tools[name] = Tool(name, description, handler, parameters)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def to_openai_tools(self) -> list[dict]:
        return [t.to_openai_tool() for t in self._tools.values()]


# ─── 原子工具实现 ──────────────────────────────────────────


def _niannian_edit(mode: str = "read", file: str = "",
                   pattern: str = "", content: str = "",
                   old: str = "", new: str = "",
                   offset: int = 1, limit: int = 200,
                   **kwargs) -> str:
    """全能文件工具：读/写/改/搜/备份"""
    # 转成 niannian_edit 的命令行调用
    cmd = f'niannian_edit --mode {mode} --file "{file}"'
    if pattern:
        cmd += f' --pattern "{pattern}"'
    if content:
        cmd += f' --content "{content}"'
    if old and new:
        cmd += f' --old "{old}" --new "{new}"'
    if offset != 1:
        cmd += f" --offset {offset}"
    if limit != 200:
        cmd += f" --limit {limit}"

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return f"错误: {result.stderr.strip() or result.stdout.strip()}"
        return result.stdout.strip() or "(空输出)"
    except subprocess.TimeoutExpired:
        return "错误: 操作超时"
    except Exception as e:
        return f"错误: {e}"


def _terminal(command: str, timeout: int = 30) -> str:
    """在 shell 中执行命令"""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(空输出)"
    except subprocess.TimeoutExpired:
        return "错误: 命令执行超时"
    except Exception as e:
        return f"错误: {e}"


def _web_scan(url: str, selector: str = "") -> str:
    """获取网页的简化文本内容"""
    import requests
    from bs4 import BeautifulSoup

    try:
        resp = requests.get(url, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 移除脚本和样式
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        # 限制输出长度
        if len(text) > 3000:
            text = text[:3000] + "\n\n...(截断)"
        return text
    except Exception as e:
        return f"错误: {e}"


def _ask(question: str) -> str:
    """向用户提问 —— 返回用户的回答"""
    print(f"\n[内核需要确认] {question}")
    return input("> ").strip()


def register_atomic_tools(registry: ToolRegistry):
    """注册内核的原子工具集"""
    tools = [
        Tool(
            name="niannian_edit",
            description="全能文件工具：读、写、搜索、替换、备份文件。"
                        "mode 支持 read/write/search/replace/append/backup",
            handler=_niannian_edit,
            parameters={
                "mode": {"type": "string", "description": "操作模式: read/write/search/replace/append"},
                "file": {"type": "string", "description": "文件路径"},
                "pattern": {"type": "string", "description": "搜索模式（search 模式用）"},
                "content": {"type": "string", "description": "写入内容（write 模式用）"},
                "old": {"type": "string", "description": "被替换的原文（replace 模式用）"},
                "new": {"type": "string", "description": "替换后的文本（replace 模式用）"},
                "offset": {"type": "integer", "description": "读取起始行"},
                "limit": {"type": "integer", "description": "最大行数"},
            },
        ),
        Tool(
            name="terminal",
            description="执行 shell 命令。优先用 niannian_edit 操作文件，"
                        "只在需要运行脚本、安装软件、git 操作时用此工具",
            handler=_terminal,
            parameters={
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数（默认 30）"},
            },
        ),
        Tool(
            name="web_scan",
            description="获取网页文本内容。用于读文章、看文档、简单的数据提取。"
                        "不支持复杂的交互式页面",
            handler=_web_scan,
            parameters={
                "url": {"type": "string", "description": "网页 URL"},
                "selector": {"type": "string", "description": "CSS 选择器（可选）"},
            },
        ),
        Tool(
            name="ask",
            description="当遇到不确定、需要授权、或需要用户决策时，向用户提问。"
                        "敏感操作（删除/安装/付费等）必须先问",
            handler=_ask,
            parameters={
                "question": {"type": "string", "description": "要向用户提出的问题"},
            },
        ),
    ]
    for t in tools:
        registry.register(t)
