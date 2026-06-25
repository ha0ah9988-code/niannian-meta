"""长轮询消息获取器 —— 断线重连 + 偏移管理"""

import time
import logging
from typing import Callable

from .api import get_updates

logger = logging.getLogger("tg.poller")


class Poller:
    """TG 长轮询消息获取器

    特性：
    - update_id 偏移持久化（启动时从上次断开处继续）
    - 断线自动重连（指数退避）
    - 消息去重
    """

    def __init__(self, offset_file: str = None):
        self._offset = 0
        self._offset_file = offset_file
        self._backoff = 1
        self._running = False
        self._handler = None  # Callable[[dict], None]

        # 从文件恢复偏移
        if offset_file:
            try:
                with open(offset_file) as f:
                    self._offset = int(f.read().strip())
                logger.info(f"恢复偏移: {self._offset}")
            except (FileNotFoundError, ValueError):
                pass

    def on_message(self, handler: Callable[[dict], None]):
        """注册消息处理器"""
        self._handler = handler

    def run(self):
        """启动轮询（阻塞）"""
        self._running = True
        logger.info("poller 开始")

        while self._running:
            try:
                updates = get_updates(offset=self._offset)

                if updates:
                    self._backoff = 1  # 成功后重置退避
                    self._handle_updates(updates)
                else:
                    # 空响应 → 短睡眠后继续
                    time.sleep(0.5)

            except Exception as e:
                logger.error(f"poller 错误: {e}")
                wait = min(self._backoff, 30)
                time.sleep(wait)
                self._backoff = min(self._backoff * 2, 60)

        logger.info("poller 停止")

    def stop(self):
        """停止轮询"""
        self._running = False

    def _handle_updates(self, updates: list):
        """处理一批更新"""
        for update in updates:
            self._offset = update["update_id"] + 1

            if "message" not in update:
                continue

            if self._handler:
                try:
                    self._handler(update)
                except Exception as e:
                    logger.error(f"handler 错误: {e}")

        # 持久化偏移
        self._save_offset()

    def _save_offset(self):
        if self._offset_file:
            try:
                with open(self._offset_file, "w") as f:
                    f.write(str(self._offset))
            except Exception as e:
                logger.error(f"保存偏移失败: {e}")
