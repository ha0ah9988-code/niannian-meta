"""niannian-meta 核心循环 —— 对话 + 工具调度。

从 Hermes 提取精华：重试、错误分类、预算控制。
去掉所有 Nous 依赖、provider 特殊处理。
保持干净 —— 目标 800 行以内。
"""

import json
import time
import sys
from typing import Optional

from core.llm import BaseLLM
from core.tools import ToolRegistry
from evolution.memory import Memory


class Agent:
    """Agent 核心 —— 一次对话 = 一个 Agent 实例"""

    def __init__(self, llm: BaseLLM, tools: ToolRegistry,
                 memory: Memory, identity_prompt: str):
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.identity_prompt = identity_prompt

        # 状态
        self.history = memory.load_history()
        self._turn_count = 0
        self._retry_count = 0
        self._tool_callbacks = []

        # 身份只读引用
        self._rules = None   # 惰性加载
        self._agents = None

    # ── 身份 consult（惰性） ───────────────────────

    @property
    def rules(self) -> str:
        if self._rules is None:
            from core import load_rules
            self._rules = load_rules()
        return self._rules

    @property
    def agents(self) -> str:
        if self._agents is None:
            from core import load_agents
            self._agents = load_agents()
        return self._agents

    # ── 工具进度回调（适配器可接管） ──────────────

    def on_tool_progress(self, tool_name: str, args: dict):
        """工具执行时回调。适配器可覆盖此方法。"""
        pass

    def on_ask(self, question: str) -> str:
        """需要用户确认时回调。适配器可覆盖。"""
        return input(f"\n[确认] {question}\n> ").strip()

    # ── 核心循环 ──────────────────────────────────

    def process(self, user_input: str, context: str = "") -> str:
        """处理一条用户输入 —— 核心循环

        支持多轮工具调用，直到 LLM 不再需要调用工具。
        """
        self._turn_count += 1
        self._retry_count = 0

        # 1. 构建消息
        system_prompt = self._build_system_prompt(context)
        messages = [{"role": "system", "content": system_prompt}]

        # 加载历史（最近 30 轮）
        for msg in self.history[-30:]:
            messages.append(msg)
        messages.append({"role": "user", "content": user_input})

        # 2. LLM 循环
        final_content = ""
        tool_rounds = 0
        max_tool_rounds = 20

        while tool_rounds < max_tool_rounds:
            tool_rounds += 1

            # 调 LLM（带重试）
            response = self._llm_call(messages)

            content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            if content:
                final_content += content + "\n"

            if not tool_calls:
                break

            # 3. 执行工具
            for tc in tool_calls:
                tool_name = tc["name"]
                args = tc.get("arguments", {})

                result = self._execute_tool(tool_name, args)

                # 注入回消息
                asst_msg = {"role": "assistant",
                           "content": content if content else None}
                if response.get("reasoning_content"):
                    asst_msg["reasoning_content"] = response["reasoning_content"]
                asst_msg["tool_calls"] = [
                    {"id": tc.get("id", ""), "type": "function",
                     "function": {"name": tool_name,
                                  "arguments": json.dumps(args)}}
                ]
                messages.append(asst_msg)
                messages.append({"role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "content": str(result)})

        # 4. 持久化
        self.history.append({"role": "user", "content": user_input})
        self.history.append({"role": "assistant", "content": final_content})
        self.memory.save_history(self.history)

        # 5. 自动结晶
        if self._turn_count % 5 == 0:
            self._auto_crystallize()

        return final_content.strip()

    def _llm_call(self, messages: list, max_retries: int = 3) -> dict:
        """调用 LLM（带重试 + 指数退避）"""
        for attempt in range(max_retries):
            try:
                return self.llm.chat(
                    messages=messages,
                    tools=self.tools.to_openai_tools(),
                )
            except Exception as e:
                self._retry_count += 1
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # 退避：1s, 2s, 4s
                    time.sleep(wait)
                else:
                    # 最后一次失败：不带工具重试一次
                    try:
                        return self.llm.chat(messages=messages)
                    except Exception as e2:
                        return {"content": f"LLM 调用失败: {e}"}

        return {"content": "LLM 调用失败"}

    def _execute_tool(self, tool_name: str, args: dict) -> str:
        """执行一个工具调用"""
        tool = self.tools.get(tool_name)
        if not tool:
            return f"未知工具: {tool_name}"

        # 回调通知
        self.on_tool_progress(tool_name, args)

        # ask 工具特殊处理
        if tool_name == "ask":
            return self.on_ask(args.get("question", "需要确认"))

        try:
            result = tool(**args)
            return str(result) if result is not None else "(空)"
        except Exception as e:
            return f"工具执行错误: {e}"

    # ── System Prompt ──────────────────────────────

    def _build_system_prompt(self, extra_context: str = "") -> str:
        """构建 system prompt"""
        parts = [
            self.identity_prompt,
            "\n## 可用工具\n",
        ]
        for t in self.tools.list_tools():
            parts.append(f"- {t.name}: {t.description}")
            if t.parameters:
                parts.append(f"  参数: {', '.join(t.parameters.keys())}")

        # 记忆
        parts.append("\n## 我的记忆")
        parts.append(self.memory.get_l1())
        l2 = self.memory.get_l2()
        l2_short = "\n".join(l2.split("\n")[:20])
        parts.append(f"\n{l2_short}")

        if extra_context:
            parts.append(f"\n## 上下文\n{extra_context}")

        parts.append(f"\nTurn: {self._turn_count}")
        return "\n".join(parts)

    # ── 结晶 ──────────────────────────────────────

    def _auto_crystallize(self):
        """自动结晶检查"""
        summary = f"Turn {self._turn_count}: {len(self.history)} 条消息"
        self.memory.crystallize(task_summary=summary)

        # L1 行数维护
        report = self.memory.maintain()
        if "超限" in report:
            pass  # 未来可以自动触发 cleanup

    # ── 系统命令 ──────────────────────────────────

    def system_command(self, cmd: str) -> str:
        """处理系统命令（不以 LLM 循环处理）"""
        cmd = cmd.strip().lower()

        if cmd == "/status":
            return (f"niannian-meta v0.2.0\n"
                    f"Turns: {self._turn_count}\n"
                    f"Tools: {len(self.tools.list_tools())}\n"
                    f"History: {len(self.history)} msgs\n"
                    f"{self.memory.maintain()}")
        elif cmd == "/rules":
            return self.rules
        elif cmd == "/soul":
            return self.identity_prompt
        elif cmd == "/clear":
            self.history.clear()
            self._turn_count = 0
            return "会话已清空"
        elif cmd.startswith("/agent"):
            return self.agents
        elif cmd.startswith("/memory"):
            parts = cmd.split()
            if len(parts) > 1:
                if parts[1] == "l1":
                    return self.memory.get_l1()
                elif parts[1] == "l2":
                    return self.memory.get_l2()
                elif parts[1] == "ls":
                    return "\n".join(self.memory.list_l3())
            return self.memory.get_l1()

        return f"未知命令: {cmd}"
