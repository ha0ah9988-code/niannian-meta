"""TG 适配器 —— 通过 Telegram 与内核交互。

支持：消息收发、ask 工具走 TG 不弹终端。
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kernel import Kernel

BOT_TOKEN = "8769651388:AAHb5c4YClHM6WOr04EFUw0PLMnFwTJZpnM"
CHAT_ID = "8391869847"
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg_send(text: str, chat_id: str = CHAT_ID) -> bool:
    """发送消息到 TG"""
    if not text:
        return True
    if len(text) > 4000:
        text = text[:3997] + "..."

    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(f"{API_BASE}/sendMessage", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        print(f"[TG send error] {e}")
        return False


def tg_typing(chat_id: str = CHAT_ID):
    """发送 typing 状态，让用户知道 bot 正在处理"""
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "action": "typing"}).encode()
        req = urllib.request.Request(f"{API_BASE}/sendChatAction", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        urllib.request.urlopen(req, timeout=5)
    except:
        pass


def get_updates(offset: int = 0, timeout: int = 30) -> list:
    """获取未处理的消息"""
    url = f"{API_BASE}/getUpdates?timeout={timeout}&offset={offset}"
    try:
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=timeout + 5)
        return json.loads(resp.read()).get("result", [])
    except Exception as e:
        print(f"[TG poll error] {e}")
        return []


class TGAdapter:
    """TG 适配器 —— 持有内核实例并处理消息循环"""

    def __init__(self):
        self.kernel = Kernel()
        self.update_offset = 0

        # 接管内核的 ask 工具，走 TG 不弹终端
        self.kernel._on_ask = self._tg_ask
        self._asking = False

    def _tg_ask(self, question: str) -> str:
        """TG 版 ask —— 发问题到主人对话，等待回复"""
        tg_typing()  # 先取消 typing
        tg_send(f"❓ {question}\n\n（在 TG 回复即可）")
        self._asking = True
        print(f"[TG ask] 等待回答: {question[:60]}")

        # 短轮询等待主人回复
        while self._asking:
            updates = get_updates(self.update_offset, timeout=10)
            for update in updates:
                self.update_offset = update["update_id"] + 1
                if "message" not in update:
                    continue
                msg = update["message"]
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()
                if chat_id == CHAT_ID and text:
                    self._asking = False
                    print(f"[TG ask] 收到回复: {text[:60]}")
                    return text
            time.sleep(0.5)

        return ""

    def run(self):
        """主 polling 循环"""
        tg_send("🧬 niannian-meta 内核已启动 v0.1.0")
        print("[TG adapter] 开始 polling...")

        while True:
            try:
                updates = get_updates(self.update_offset)

                for update in updates:
                    self.update_offset = update["update_id"] + 1
                    if "message" not in update:
                        continue

                    msg = update["message"]
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "").strip()

                    if not text:
                        continue
                    if chat_id != CHAT_ID:
                        print(f"[忽略] 来自 {chat_id} 的消息")
                        continue

                    print(f"[TG] << {text[:80]}")
                    tg_typing(chat_id)  # 显示 typing... 状态
                    if text.startswith("/"):
                        response = self.kernel.system_command(text)
                    else:
                        response = self.kernel.process(text)
                    print(f"[TG] >> {response[:80]}")
                    tg_send(response, chat_id)

            except KeyboardInterrupt:
                print("\n[TG adapter] 停止")
                break
            except Exception as e:
                print(f"[TG adapter] 循环错误: {e}")
                time.sleep(5)


def run_polling():
    TGAdapter().run()


if __name__ == "__main__":
    print("niannian-meta TG adapter")
    print("按 Ctrl+C 停止")
    run_polling()
