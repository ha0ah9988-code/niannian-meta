---
name: restart-tg-bot
description: 重启 TG bot 的标准操作流程
---

## 使用场景
需要重启 Telegram Bot 时使用，确保环境变量正确加载再运行。

## 操作步骤
1. 在项目目录下执行 `source env.sh` 加载环境变量
2. 执行 `python3 run.py tg` 启动 bot

## 注意事项
- 确保当前工作目录是包含 `env.sh` 和 `run.py` 的项目根目录
- 如果 `env.sh` 不存在，需先创建或准备环境变量
- 首次运行或变更环境后建议先测试连接状态