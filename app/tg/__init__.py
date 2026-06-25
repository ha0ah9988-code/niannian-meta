"""TG Gateway —— 完整版

组装 poller / session / sender / media / commands 为一套完整的
消息处理管道，对外暴露 TGAdapter 类。
"""

import logging
from typing import Callable

from .api import send_message, send_typing, get_updates, CHAT_ID
from .poller import Poller
from .session import SessionManager
from .sender import send, send_progress
from . import commands as cmd_handler

logger = logging.getLogger("tg")


class TGAdapter:
    """TG Gateway 适配器

    职责：
    1. 接收消息（poller）
    2. 管理会话（session manager）
    3. 分派消息到 agent
    4. 发送回复（sender）
    """

    def __init__(self, agent_factory: Callable, chat_id: str = CHAT_ID):
        self.chat_id = chat_id
        self.agent = agent_factory()
        self.sessions = SessionManager()
        self.poller = Poller(offset_file="data/tg_offset.txt")

        # 接管 agent 回调
        self.agent.on_ask = self._on_ask
        self.agent.on_tool_progress = self._on_tool_progress

        # 注册消息处理器
        self.poller.on_message(self._on_message)

    # ── 消息处理管道 ────────────────────────────────

    def _on_message(self, update: dict):
        """消息处理管道入口"""
        msg = update["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()

        # 只处理目标对话
        if chat_id != self.chat_id:
            return

        logger.info(f"<< {text[:80]}")

        # 显示 typing
        send_typing(chat_id)

        # 获取/创建会话
        session = self.sessions.get_or_create(chat_id)

        # 处理命令或消息
        if text.startswith("/"):
            response = cmd_handler.handle(text, self.agent)
        else:
            response = self.agent.process(text)

        # 发送回复
        if response:
            send(response, chat_id)
            logger.info(f">> {response[:80]}")

        # 持久化会话
        self.sessions.save(session)

    # ── 回调 ────────────────────────────────────────

    def _on_ask(self, question: str) -> str:
        """TG 版 ask —— 发问题等回复（独立短轮询）"""
        send(f"❓ {question}\n\n（在 TG 回复即可）", self.chat_id)
        import time as _time

        while True:
            updates = get_updates(offset=self.poller._offset, timeout=10)
            for update in updates:
                self.poller._offset = update["update_id"] + 1
                if "message" not in update:
                    continue
                msg = update["message"]
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()
                if chat_id == self.chat_id and text:
                    return text
            _time.sleep(0.5)

        return ""

    def _on_tool_progress(self, tool_name: str, args: dict):
        """TG 版工具进度回调"""
        import time as _time
        if not hasattr(self, "_last_tool_time"):
            self._last_tool_time = 0

        now = _time.time()
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

        send_progress(tool_name, preview, self.chat_id)

    # ── 生命周期 ────────────────────────────────────

    def run(self):
        """启动 gateway"""
        send_message("🧬 niannian-meta v0.2.0 gateway 已启动", self.chat_id)
        logger.info("gateway 启动")
        try:
            self.poller.run()
        finally:
            self.sessions.save_all()

    def stop(self):
        """停止 gateway"""
        self.poller.stop()
        self.sessions.save_all()
