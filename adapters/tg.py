"""TG 适配器 —— 通过 Telegram 与内核交互。

架构：独立进程 + polling 模式。
内核作为一个对象被导入，TG 消息走内核的 process()。
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse

# 确保可以 import 上级目录的内核
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kernel import Kernel

BOT_TOKEN = "8769651388:AAHb5c4YClHM6WOr04EFUw0PLMnFwTJZpnM"
CHAT_ID = "8391869847"
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

kernel = None  # 全局内核实例


def tg_send(text: str, chat_id: str = CHAT_ID) -> bool:
    """发送消息到 TG"""
    if not text:
        return True

    # 限制消息长度
    if len(text) > 4000:
        text = text[:3997] + "..."

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }).encode()

    try:
        req = urllib.request.Request(f"{API_BASE}/sendMessage", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        print(f"[TG send error] {e}")
        return False


def tg_send_md(text: str, chat_id: str = CHAT_ID) -> bool:
    """用 MarkdownV2 格式发送"""
    if not text:
        return True
    if len(text) > 4000:
        text = text[:3997] + "..."

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
    }).encode()

    try:
        req = urllib.request.Request(f"{API_BASE}/sendMessage", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        print(f"[TG send error] {e}")
        return False


def get_updates(offset: int = 0) -> list:
    """获取未处理的消息"""
    url = f"{API_BASE}/getUpdates?timeout=30&offset={offset}"
    try:
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=35)
        data = json.loads(resp.read())
        return data.get("result", [])
    except Exception as e:
        print(f"[TG poll error] {e}")
        return []


def handle_message(text: str) -> str:
    """处理一条消息，返回回复"""
    global kernel

    if kernel is None:
        kernel = Kernel()

    if text.startswith("/"):
        return kernel.system_command(text)

    return kernel.process(text)


def run_polling():
    """启动 TG polling 循环"""
    global kernel
    kernel = Kernel()

    update_offset = 0
    tg_send("🧬 niannian-meta 内核已启动 v0.1.0")

    print("[TG adapter] 开始 polling...")

    while True:
        try:
            updates = get_updates(update_offset)

            for update in updates:
                update_offset = update["update_id"] + 1

                if "message" not in update:
                    continue

                msg = update["message"]
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()

                if not text:
                    continue

                # 只处理来自主人的消息
                if chat_id != CHAT_ID:
                    print(f"[忽略] 来自 {chat_id} 的消息")
                    continue

                print(f"[TG] << {text[:80]}")
                response = handle_message(text)
                print(f"[TG] >> {response[:80]}")

                tg_send(response, chat_id)

        except KeyboardInterrupt:
            print("\n[TG adapter] 停止")
            break
        except Exception as e:
            print(f"[TG adapter] 循环错误: {e}")
            time.sleep(5)


# ─── 独立运行 ─────────────────────────────────────────

if __name__ == "__main__":
    print("niannian-meta TG adapter")
    print("按 Ctrl+C 停止")
    run_polling()
