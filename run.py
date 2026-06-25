#!/usr/bin/env python3
"""niannian-meta 入口

用法：
  python run.py             独立模式（stdin/stdout）
  python run.py tg          TG 模式（polling）
"""

import sys
import os

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
