#!/usr/bin/env python3
"""niannian-meta 入口

自动加载 env.sh（如果存在），用户只需一个命令。
用法：
  python3 run.py             独立模式（stdin/stdout）
  python3 run.py tg          TG 模式（polling）
"""

import os
import sys

# 自动加载 env.sh（如果存在）
env_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.sh")
if os.path.exists(env_sh):
    with open(env_sh) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                parts = line[7:].split("=", 1)
                if len(parts) == 2:
                    key, val = parts[0], parts[1].strip('"').strip("'")
                    if not os.environ.get(key):
                        os.environ[key] = val

# 确保能 import 项目模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "tg":
        from adapters.tg import run_polling
        run_polling()
    else:
        from kernel import run_standalone
        run_standalone()


if __name__ == "__main__":
    main()
