"""niannian-meta 内核种子 —— 最小自进化实体。

这是整个项目的种子文件。内核的核心循环、身份、生长机制都在这里。
"""

import os
import json
import sys
from typing import Optional

from meta_rules import META_RULES, get_formatted_rules
from llm import create_llm, BaseLLM
from tools import ToolRegistry, register_atomic_tools
from memory import Memory

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class Kernel:
    """niannian-meta 内核

    可以独立运行，也可以作为其他 agent 的内核。
    独立运行时靠 4 个原子工具 + LLM 存活。
    接入其他框架时，内核的规则覆盖框架规则，记忆随身携带。
    """

    def __init__(self, llm: BaseLLM = None):
        # ── 身份（不可变） ──
        self.rules = get_formatted_rules()

        # ── 组件 ──
        if llm is None:
            llm = create_llm()
        self.llm = llm
        self.tools = ToolRegistry()
        self.memory = Memory()

        # ── 状态 ──
        self.history = []  # 当前会话消息历史
        self._turn_count = 0
        self._task_active = False
        self._tool_results_buffer = []
        self._modules = {}  # 外接模块注册表

        # 注册原子工具
        register_atomic_tools(self.tools)

    # ─── 模块管理（热插拔） ───────────────────────────

    def load_module(self, name: str, module_obj):
        """加载一个外接模块（gateway、搜索、浏览器等）"""
        self._modules[name] = module_obj
        if hasattr(module_obj, "on_load"):
            module_obj.on_load(self)

    def unload_module(self, name: str):
        """卸载模块"""
        if name in self._modules:
            if hasattr(self._modules[name], "on_unload"):
                self._modules[name].on_unload(self)
            del self._modules[name]

    def get_module(self, name: str):
        return self._modules.get(name)

    # ─── 核心循环 ────────────────────────────────────

    def process(self, user_input: str, context: str = "") -> str:
        """处理一轮用户输入 —— 内核核心循环

        这是内核最核心的方法。一次调用 = 一轮完整交互。
        可以递归调用（工具返回后继续）。
        """
        self._turn_count += 1

        # 1. 构建 system prompt（元规则 + 记忆 + 工具定义）
        system_prompt = self._build_system_prompt(context)

        # 2. 构造消息
        messages = [{"role": "system", "content": system_prompt}]

        # 添加历史
        for msg in self.history[-20:]:  # 保留最近 20 轮
            messages.append(msg)

        # 添加本次输入
        messages.append({"role": "user", "content": user_input})

        # 3. LLM 调用 + 工具执行循环（多轮直到 LLM 不再调工具）
        final_content = ""
        tool_rounds = 0
        max_tool_rounds = 20

        while tool_rounds < max_tool_rounds:
            tool_rounds += 1

            response = self.llm.chat(
                messages=messages,
                tools=self.tools.to_openai_tools(),
            )

            content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            # 累积内容
            if content:
                final_content += content + "\n"

            # 没有工具调用 → 结束
            if not tool_calls:
                break

            # 4. 执行工具调用
            for tc in tool_calls:
                tool_name = tc["name"]
                args = tc.get("arguments", {})
                tool = self.tools.get(tool_name)

                if tool:
                    try:
                        result = tool(**args)
                    except Exception as e:
                        result = f"工具执行错误: {e}"
                else:
                    result = f"未知工具: {tool_name}"

                # 工具结果注入回消息
                messages.append({
                    "role": "assistant",
                    "content": content if content else None,
                    "tool_calls": [
                        {"id": tc.get("id", ""),
                         "type": "function",
                         "function": {"name": tool_name,
                                      "arguments": json.dumps(args)}}
                    ],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": str(result),
                })

                self._tool_results_buffer.append({
                    "tool": tool_name,
                    "args": args,
                    "result": str(result)[:500],
                })

        # 5. 记录历史
        self.history.append({"role": "user", "content": user_input})
        self.history.append({"role": "assistant", "content": final_content})

        # 6. 触发结晶检查（每隔几轮评估一次）
        if self._turn_count % 5 == 0:
            self._auto_crystallize()

        return final_content.strip()

    # ─── System Prompt 构造 ───────────────────────────

    def _build_system_prompt(self, extra_context: str = "") -> str:
        """构造完整的 system prompt"""
        parts = [
            "# niannian-meta 内核\n",
            self.rules,
            "\n## 可用工具\n",
            "你有以下工具可用：\n",
        ]

        # 列出工具
        for t in self.tools.list_tools():
            parts.append(f"- {t.name}: {t.description}")
            if t.parameters:
                params = ", ".join(t.parameters.keys())
                parts.append(f"  参数: {params}")

        # 注入记忆
        parts.append("\n## 内核记忆")
        parts.append("\n### L1 索引（精简）")
        parts.append(self.memory.get_l1())
        parts.append("\n### L2 事实库（关键环境事实）")
        l2 = self.memory.get_l2()
        # 只取关键部分，避免太长
        l2_lines = l2.split("\n")[:30]
        parts.append("\n".join(l2_lines))

        # 额外上下文
        if extra_context:
            parts.append(f"\n## 上下文\n{extra_context}")

        # 当前轮信息
        parts.append(f"\n## 当前\nTurn: {self._turn_count}")

        return "\n".join(parts)

    # ─── 结晶机制 ─────────────────────────────────────

    def _auto_crystallize(self):
        """自动结晶检查 —— 从最近的工具执行中提取可结晶的信息"""
        if not self._tool_results_buffer:
            return

        # 通知 LLM 进行结晶（通过 system prompt 注入）
        # 简洁版本：只记录 L4 会话摘要
        recent_tools = [r["tool"] for r in self._tool_results_buffer[-10:]]
        summary = f"Turn {self._turn_count}: 使用了工具 {', '.join(set(recent_tools))}"

        if len(self._tool_results_buffer) > 0:
            # 检查是否有值得结晶的信息
            notable = False
            verified_facts = []

            for record in self._tool_results_buffer:
                t = record["tool"]
                r = record["result"]

                # 检测可能的 L2 事实
                if "error" not in r.lower() and len(r) > 20:
                    # 这是一个潜在的可记事实（简化处理）
                    pass

            self.memory.crystallize(
                task_summary=summary,
                verified_facts=verified_facts if verified_facts else None,
            )

    # ─── 系统命令 ─────────────────────────────────────

    def system_command(self, cmd: str) -> str:
        """处理内核系统命令（不以 agent 循环处理）"""
        cmd = cmd.strip().lower()

        if cmd == "/status":
            return (f"niannian-meta v0.1.0\n"
                    f"Turns: {self._turn_count}\n"
                    f"Tools: {len(self.tools.list_tools())}\n"
                    f"Modules: {list(self._modules.keys())}\n"
                    f"History: {len(self.history)} msgs\n"
                    f"{self.memory.maintain()}")
        elif cmd == "/memory":
            return self.memory.get_l1()
        elif cmd == "/rules":
            return self.rules
        elif cmd == "/clear":
            self.history.clear()
            self._tool_results_buffer.clear()
            self._turn_count = 0
            return "会话已清空"
        elif cmd.startswith("/memory "):
            parts = cmd.split(" ", 2)
            if len(parts) >= 2:
                cmd2 = parts[1]
                if cmd2 == "l2":
                    return self.memory.get_l2()
                elif cmd2 == "ls":
                    return "\n".join(self.memory.list_l3())
        elif cmd.startswith("/module "):
            parts = cmd.split()
            if len(parts) == 2:
                name = parts[1]
                if name in self._modules:
                    self.unload_module(name)
                    return f"模块 {name} 已卸载"
                else:
                    return f"未知模块: {name}"
            elif len(parts) == 1:
                return f"已加载模块: {list(self._modules.keys())}"

        return f"未知命令: {cmd}"


# ─── 独立运行入口 ─────────────────────────────────────

def run_standalone():
    """独立模式：stdin/stdout 交互"""
    kernel = Kernel()

    print("\n" + "=" * 50)
    print("niannian-meta 内核 v0.1.0")
    print("=" * 50)
    print("输入 /status 查看状态, /rules 查看规则, /clear 清空, /help 帮助")
    print("输入 exit 退出")
    print()

    while True:
        try:
            user_input = input("🧬 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            print("内核休眠。")
            break

        if user_input.startswith("/"):
            response = kernel.system_command(user_input)
        else:
            response = kernel.process(user_input)

        print(f"\n📥 {response}\n")


if __name__ == "__main__":
    run_standalone()
