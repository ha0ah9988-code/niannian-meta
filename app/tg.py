"""TG 适配器 —— 通过 Telegram 与 agent 交互。

支持 typing 状态提示、工具进度通知、ask 走 TG 不弹终端。
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse

BOT_TOKEN = "8769651388:AAHb5c4YClHM6WOr04EFUw0PLMnFwTJZpnM"
CHAT_ID = "8391869847"
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg_send(text: str, chat_id: str = CHAT_ID) -> bool:
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
        return json.loads(urllib.request.urlopen(req, timeout=15).read()).get("ok", False)
    except Exception as e:
        print(f"[TG send] {e}")
        return False


def tg_typing(chat_id: str = CHAT_ID):
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "action": "typing"}).encode()
        req = urllib.request.Request(f"{API_BASE}/sendChatAction", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        urllib.request.urlopen(req, timeout=5)
    except:
        pass


def get_updates(offset: int = 0, timeout: int = 30) -> list:
    url = f"{API_BASE}/getUpdates?timeout={timeout}&offset={offset}"
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url), timeout=timeout+5).read()).get("result", [])
    except Exception as e:
        print(f"[TG poll] {e}")
        return []


class TGAdapter:
    """TG 适配器 —— 持有 agent 实例并处理消息循环"""

    def __init__(self, agent_factory):
        from core.loop import Agent
        self.agent = agent_factory()

        # 接管回调
        self.agent.on_ask = self._tg_ask
        self.agent.on_tool_progress = self._tg_tool_progress
        self.update_offset = 0
        self._asking = False
        self._last_tool_time = 0

    def _tg_ask(self, question: str) -> str:
        tg_send(f"❓ {question}\n\n（在 TG 回复即可）")
        self._asking = True
        print(f"[TG ask] 等待回答...")

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
                    return text
            time.sleep(0.5)
        return ""

    def _tg_tool_progress(self, tool_name: str, args: dict):
        now = time.time()
        if now - self._last_tool_time < 1.0:
            return
        self._last_tool_time = now

        preview = ""
        if tool_name == "terminal":
            preview = args.get("command", "")[:60]
        elif tool_name == "web_scan":
            preview = args.get("url", "")[:60]
        elif tool_name == "niannian_edit":
            preview = f"{args.get('mode','')} {args.get('file','')}"[:60]

        brief = f"🛠️ {tool_name}"
        if preview:
            brief += f": {preview}"
        tg_send(brief)

    def run(self):
        tg_send("🧬 niannian-meta v0.2.0 已启动")
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
                    if not text or chat_id != CHAT_ID:
                        continue

                    print(f"[TG] << {text[:80]}")
                    tg_typing(chat_id)

                    if text.startswith("/"):
                        response = self.agent.system_command(text)
                    else:
                        response = self.agent.process(text)

                    print(f"[TG] >> {response[:80]}")
                    tg_send(response, chat_id)

            except KeyboardInterrupt:
                print("\n[TG adapter] 停止")
                break
            except Exception as e:
                print(f"[TG adapter] 错误: {e}")
                time.sleep(5)


def run_polling(agent_factory=None):
    """启动 TG polling

    Args:
        agent_factory: 创建 agent 实例的工厂函数
    """
    if agent_factory is None:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from run import create_agent
        agent_factory = create_agent
    TGAdapter(agent_factory).run()
