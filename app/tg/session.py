"""会话管理 —— 创建/持久化/恢复/超时清理

每个用户对话是一个 Session，含消息历史、上下文、创建时间。
"""

import json
import time
import os
import threading
from pathlib import Path

SESSION_DIR = Path(__file__).parent.parent.parent / "data" / "sessions"


class Session:
    """一次对话会话"""

    def __init__(self, session_id: str, chat_id: str):
        self.id = session_id
        self.chat_id = chat_id
        self.messages = []       # [{"role": ..., "content": ...}]
        self.created_at = time.time()
        self.last_active = time.time()
        self.metadata = {}       # 额外元数据

    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        self.last_active = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "messages": self.messages[-100:],  # 只保留最近 100 条
            "created_at": self.created_at,
            "last_active": self.last_active,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        s = cls(data["id"], data["chat_id"])
        s.messages = data.get("messages", [])
        s.created_at = data.get("created_at", time.time())
        s.last_active = data.get("last_active", time.time())
        s.metadata = data.get("metadata", {})
        return s


class SessionManager:
    """会话管理器 —— 持久化 + 超时清理 + 线程安全"""

    def __init__(self, timeout: int = 3600, cleanup_interval: int = 300):
        """
        Args:
            timeout: 会话超时秒数（默认 1 小时无活动）
            cleanup_interval: 清理间隔秒数
        """
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._sessions = {}       # sid → Session
        self._lock = threading.Lock()
        self._timeout = timeout
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()

    def get_or_create(self, chat_id: str) -> Session:
        """获取或创建会话"""
        # chat_id 作为 session_id（单用户模式简化版）
        sid = f"tg_{chat_id}"

        with self._lock:
            if sid in self._sessions:
                session = self._sessions[sid]
                session.last_active = time.time()
                return session

            # 尝试从文件恢复
            session = self._load(sid)
            if session:
                self._sessions[sid] = session
                return session

            # 新建
            session = Session(sid, chat_id)
            self._sessions[sid] = session
            return session

    def save(self, session: Session):
        """持久化会话到文件"""
        path = SESSION_DIR / f"{session.id}.json"
        try:
            path.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[session] save error: {e}")

    def _load(self, sid: str) -> Session:
        """从文件加载会话"""
        path = SESSION_DIR / f"{sid}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return Session.from_dict(data)
            except Exception:
                pass
        return None

    def cleanup(self):
        """清理超时会话"""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now

        with self._lock:
            expired = [
                sid for sid, s in self._sessions.items()
                if now - s.last_active > self._timeout
            ]
            for sid in expired:
                self.save(self._sessions[sid])  # 保存后释放
                del self._sessions[sid]

            if expired:
                print(f"[session] 清理 {len(expired)} 个超时会话")

    def save_all(self):
        """保存所有活跃会话"""
        with self._lock:
            for session in self._sessions.values():
                self.save(session)
