#!/usr/bin/env python3
"""niannian-meta 入口

自动加载 env.sh，只需一个命令启动。
用法：
  python3 run.py             独立模式（stdin/stdout）
  python3 run.py tg          TG 模式
"""

import os
import sys

# 自动加载 env.sh
env_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.sh")
if os.path.exists(env_sh):
    with open(env_sh) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                parts = line[7:].split("=", 1)
                if len(parts) == 2:
                    k, v = parts[0], parts[1].strip('"').strip("'")
                    if not os.environ.get(k):
                        os.environ[k] = v

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def create_agent():
    """创建并返回一个配置完成的 Agent 实例"""
    from core import setup_providers
    from core.tools import ToolRegistry, register_atomic_tools
    from core.loop import Agent
    from core import build_identity_prompt
    from evolution.memory import Memory

    registry = setup_providers()
    provider = registry.default
    tools = ToolRegistry()
    register_atomic_tools(tools)
    memory = Memory()
    identity = build_identity_prompt()

    return Agent(provider=provider, tools=tools, memory=memory,
                 identity_prompt=identity)


def run_standalone():
    agent = create_agent()
    print("\n" + "=" * 50)
    print("niannian-meta v0.2.0")
    print("=" * 50)
    print("/status /rules /soul /clear /memory")
    print()

    while True:
        try:
            user_input = input("🧬 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break
        if user_input.startswith("/"):
            response = agent.system_command(user_input)
        else:
            response = agent.process(user_input)
        print(f"\n📥 {response}\n")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "tg":
        from app.tg import TGAdapter
        adapter = TGAdapter(create_agent)
        adapter.run()
    else:
        run_standalone()


if __name__ == "__main__":
    main()
