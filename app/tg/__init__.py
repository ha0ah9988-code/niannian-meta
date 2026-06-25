"""TG Gateway —— 适配器（接入 state.db + 压缩引擎）

职责：
  1. 接收消息（poller）
  2. 分派消息到 agent（agent 通过 state.db 自行持久化）
  3. 发送回复（sender）
"""

import logging
import signal
import sys
from typing import Callable

from .api import send_message, send_typing, get_updates, CHAT_ID
from .poller import Poller
from .sender import send, send_progress
from . import commands as cmd_handler

logger = logging.getLogger("tg")


class TGAdapter:
    """TG Gateway 适配器

    Agent 使用 state.db 管理会话持久化 + 上下文压缩，
    适配器只负责消息转发和回调适配。
    """

    def __init__(self, agent_factory: Callable, chat_id: str = CHAT_ID):
        self.chat_id = chat_id
        self.agent = agent_factory()
        self.agent.state_source = "tg"  # 标记来源为 TG
        self.poller = Poller(offset_file="data/tg_offset.txt")

        # 接管 agent 回调
        self.agent.on_ask = self._on_ask
        self.agent.on_tool_progress = self._on_tool_progress
        self.agent.on_compression_status = self._on_compression_notify

        # 注册消息处理器
        self.poller.on_message(self._on_message)

        # 注册优雅关闭
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

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

        # 处理命令或消息（agent 通过 state.db 自动持久化）
        if text.startswith("/"):
            response = cmd_handler.handle(text, self.agent)
        else:
            response = self.agent.process(text)

        # 发送回复
        if response:
            send(response, chat_id)
            logger.info(f">> {response[:80]}")

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

    def _on_compression_notify(self, msg: str):
        """压缩/归档状态通知 → TG 进度消息"""
        send(f"🔄 {msg}", self.chat_id)

    # ── 生命周期 ────────────────────────────────────

    def run(self):
        """启动 gateway"""
        send_message("🧬 niannian-meta v0.2.0 gateway 已启动", self.chat_id)
        logger.info("gateway 启动")
        try:
            self.poller.run()
        finally:
            self._do_shutdown()

    def stop(self):
        """停止 gateway"""
        self.poller.stop()
        self._do_shutdown()

    def _shutdown(self, signum=None, frame=None):
        """signal handler 优雅关闭"""
        logger.info("收到关闭信号")
        self.poller.stop()

    def _do_shutdown(self):
        """关闭时的持久化操作"""
        try:
            # 归档 + 结束会话
            result = self.agent.state.archive_history(
                keep=50,
                source=getattr(self.agent, 'state_source', 'tg'),
            )
            if result["archived"] > 0:
                logger.info(f"已归档 {result['archived']} 条到 sessions.db")
            self.agent.state.end_session("gateway_stop")
            logger.info("state 会话已结束")
        except Exception as e:
            logger.warning(f"关闭异常: {e}")
