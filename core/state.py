"""state.db — niannian-meta 持久化存储层

从 Hermes state.db 移植，简化版。

表结构：
  - sessions: 会话元数据（source, started_at, message_count）
  - messages: 消息记录（session_id, role, content, timestamp, active）
  - state_meta: 键值对（K-V 存储）

设计原则：
  - WAL 模式，读写不互锁
  - 单 session 模式（启动时自动创建/恢复当前会话）
  - 消息批量写入
  - 压缩时标记 active=0 而非删除
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

BJ_TZ = timezone(timedelta(hours=8))

DATA_DIR = Path(__file__).parent.parent / "data"
STATE_DB = DATA_DIR / "state.db"
SESSIONS_DB = DATA_DIR / "sessions.db"

# ── Schema ──────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'standalone',
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    title TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    timestamp REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    compressed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_active
    ON messages(session_id, active, timestamp);
"""


# ── StateStore ─────────────────────────────────────


class StateStore:
    """niannian-meta state.db 持久化存储

    单例模式：应用只有一个 state.db 实例。
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: str = None):
        """初始化 state.db

        Args:
            db_path: 数据库路径，默认 data/state.db
        """
        if hasattr(self, '_initialized'):
            return

        self.db_path = str(db_path or STATE_DB)
        self._local = threading.local()
        self._write_lock = threading.Lock()

        # 确保目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # 初始化 schema
        self._init_schema()

        # 当前会话 ID（惰性创建）
        self._current_session_id: Optional[str] = None

        self._initialized = True

    @property
    def _conn(self) -> sqlite3.Connection:
        """获取当前线程的连接（自动创建）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self):
        """初始化数据库 schema"""
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    # ── 会话管理 ───────────────────────────────────

    def get_or_create_session(self, source: str = "standalone") -> str:
        """获取或创建当前会话

        优先恢复上一个未结束的活跃会话，否则创建新的。
        """
        if self._current_session_id:
            return self._current_session_id

        # 尝试恢复最近一个 ended_at IS NULL 的会话
        row = self._conn.execute(
            "SELECT id FROM sessions WHERE ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

        if row:
            self._current_session_id = row["id"]
            return self._current_session_id

        # 创建新会话
        session_id = str(uuid.uuid4())
        now = time.time()
        self._conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            (session_id, source, now),
        )
        self._conn.commit()
        self._current_session_id = session_id
        return session_id

    def end_session(self, end_reason: str = "ended"):
        """结束当前会话"""
        if not self._current_session_id:
            return

        # 统计消息数
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE session_id = ? AND active = 1",
            (self._current_session_id,),
        ).fetchone()
        msg_count = row["cnt"] if row else 0

        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, message_count = ? "
            "WHERE id = ?",
            (time.time(), msg_count, self._current_session_id),
        )
        self._conn.commit()
        self._current_session_id = None

    def close(self):
        """关闭所有连接"""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    def close_all(self):
        """强制关闭所有线程的连接"""
        if hasattr(self, '_local'):
            self.close()

    # ── 消息存储 ───────────────────────────────────

    def save_history(self, messages: List[Dict[str, Any]]):
        """全量保存历史消息

        策略：清空当前会话活跃消息，批量插入新列表。
        适合 Agent 每轮结束后调用。
        """
        session_id = self.get_or_create_session()
        now = time.time()

        with self._write_lock:
            conn = self._conn
            try:
                # 软删除旧活跃消息
                conn.execute(
                    "UPDATE messages SET active = 0 "
                    "WHERE session_id = ? AND active = 1",
                    (session_id,),
                )

                # 批量插入
                rows = []
                for msg in messages:
                    content = msg.get("content")
                    if isinstance(content, (dict, list)):
                        content = json.dumps(content, ensure_ascii=False)
                    elif content is not None:
                        content = str(content)

                    tool_calls = msg.get("tool_calls")
                    if tool_calls is not None:
                        tool_calls = json.dumps(tool_calls, ensure_ascii=False)

                    rows.append((
                        session_id,
                        msg.get("role", "user"),
                        content,
                        msg.get("tool_call_id"),
                        tool_calls,
                        now,
                        1,  # active
                    ))

                conn.executemany(
                    "INSERT INTO messages "
                    "(session_id, role, content, tool_call_id, tool_calls, "
                    " timestamp, active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def load_history(self) -> List[Dict[str, Any]]:
        """加载当前会话活跃消息"""
        session_id = self.get_or_create_session()
        rows = self._conn.execute(
            "SELECT role, content, tool_call_id, tool_calls "
            "FROM messages WHERE session_id = ? AND active = 1 "
            "ORDER BY id ASC",
            (session_id,),
        ).fetchall()

        messages = []
        for row in rows:
            msg = {"role": row["role"]}
            content = row["content"]
            if content is not None:
                msg["content"] = content
            else:
                msg["content"] = None

            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass

            messages.append(msg)

        return messages

    def clear_history(self):
        """清空当前会话历史"""
        if not self._current_session_id:
            return
        with self._write_lock:
            self._conn.execute(
                "UPDATE messages SET active = 0 "
                "WHERE session_id = ? AND active = 1",
                (self._current_session_id,),
            )
            self._conn.commit()

    # ── 压缩标记 ───────────────────────────────────

    def mark_compressed(self, session_id: str = None):
        """标记当前会话已压缩"""
        sid = session_id or self._current_session_id
        if not sid:
            return
        with self._write_lock:
            # 将旧的活跃消息标记为 compressed=1
            self._conn.execute(
                "UPDATE messages SET active = 0, compressed = 1 "
                "WHERE session_id = ? AND active = 1",
                (sid,),
            )
            self._conn.commit()

    # ── K-V 存储 ────────────────────────────────────

    def set_meta(self, key: str, value: str):
        """设置元数据"""
        self._conn.execute(
            "INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str, default: str = None) -> Optional[str]:
        """读取元数据"""
        row = self._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def delete_meta(self, key: str):
        """删除元数据"""
        self._conn.execute(
            "DELETE FROM state_meta WHERE key = ?", (key,)
        )
        self._conn.commit()

    # ── 状态查询 ───────────────────────────────────

    def get_session_info(self) -> Dict[str, Any]:
        """获取当前会话信息"""
        sid = self._current_session_id
        if not sid:
            return {}

        row = self._conn.execute(
            "SELECT id, source, started_at, ended_at, message_count, title "
            "FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        if not row:
            return {}

        active_count = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE session_id = ? AND active = 1",
            (sid,),
        ).fetchone()["cnt"]

        total_count = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE session_id = ?",
            (sid,),
        ).fetchone()["cnt"]

        return {
            "id": row["id"][:8],
            "source": row["source"],
            "started_at": row["started_at"],
            "message_count": row["message_count"] or 0,
            "active_messages": active_count,
            "total_messages": total_count,
        }

    def get_stats(self) -> Dict[str, Any]:
        """获取全量统计"""
        total_sessions = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM sessions"
        ).fetchone()["cnt"]

        total_messages = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages"
        ).fetchone()["cnt"]

        total_compressed = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE compressed = 1"
        ).fetchone()["cnt"]

        # 归档统计
        try:
            archive = SessionsArchive()
            astats = archive.get_stats()
        except Exception:
            astats = {"sessions": 0, "messages": 0}

        return {
            "sessions": total_sessions,
            "messages": total_messages,
            "compressed": total_compressed,
            "archived_sessions": astats["sessions"],
            "archived_messages": astats["messages"],
        }

    # ── Usage 追踪 ────────────────────────────────

    def update_session_usage(self, session_id: str = None, usage: dict = None):
        """累加 token 用量到 state_meta

        session-trim-v2.py 从 sessions 表读 input_tokens/output_tokens 等字段。
        这里用 state_meta 存，key=f"usage:{sid}"。
        """
        if not usage or not session_id:
            return
        key = f"usage:{session_id}"
        current = self.get_meta(key, "{}")
        try:
            cur = json.loads(current)
        except (json.JSONDecodeError, TypeError):
            cur = {}

        # 兼容各种 key 命名
        fields = {
            "input_tokens": ("prompt_tokens", "input_tokens", "input_token_count"),
            "output_tokens": ("completion_tokens", "output_tokens", "output_token_count"),
            "cache_read_tokens": ("cache_read_tokens",),
            "cache_write_tokens": ("cache_write_tokens",),
            "reasoning_tokens": ("reasoning_tokens",),
        }
        for target, sources in fields.items():
            val = 0
            for src in sources:
                v = usage.get(src) or 0
                if v:
                    val = v
                    break
            if val:
                cur[target] = cur.get(target, 0) + val

        # 存回 JSON
        self.set_meta(key, json.dumps(cur))

    def get_session_usage(self, session_id: str = None) -> dict:
        """读取 session 累计用量"""
        if not session_id:
            return {}
        raw = self.get_meta(f"usage:{session_id}", "{}")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    # ── 归档到 sessions.db ──────────────────────────

    def archive_history(self, keep: int = 50, source: str = "standalone") -> dict:
        """将 state.db 中的旧消息归档到 sessions.db

        保留最近 keep 条活跃消息，其余归档。
        兼容 Hermes session-trim-v2.py 的 schema。

        Returns:
            {"archived": int, "remaining": int}
        """
        sid = self._current_session_id
        if not sid:
            return {"archived": 0, "remaining": 0}

        # 读取所有活跃消息
        rows = self._conn.execute(
            "SELECT id, role, content, tool_call_id, tool_calls, timestamp "
            "FROM messages WHERE session_id = ? AND active = 1 "
            "ORDER BY id ASC",
            (sid,),
        ).fetchall()

        if len(rows) <= keep:
            return {"archived": 0, "remaining": len(rows)}

        # 分出要归档的（前 N 条）和保留的（后 keep 条）
        archive_rows = rows[:-keep]
        keep_rows = rows[-keep:]

        # 写入 sessions.db 归档
        archive_db = SessionsArchive()
        archived_count = archive_db.archive_messages(
            session_id=sid,
            messages=[dict(r) for r in archive_rows],
            source=source,
        )
        archive_db.close()

        # 从 state.db 软删除已归档的消息
        archive_ids = [r["id"] for r in archive_rows]
        with self._write_lock:
            placeholders = ",".join("?" for _ in archive_ids)
            self._conn.execute(
                f"UPDATE messages SET active = 0, compressed = 1 "
                f"WHERE id IN ({placeholders})",
                archive_ids,
            )
            self._conn.commit()

        return {"archived": archived_count, "remaining": len(keep_rows)}


# ══════════════════════════════════════════════════════
# SessionsArchive — 归档数据库（与 session-trim-v2.py 兼容）
# ══════════════════════════════════════════════════════

class SessionsArchive:
    """sessions.db 归档存储

    与 Hermes session-trim-v2.py 写入的 sessions.db 兼容。
    session_archive + FTS5 全文搜索。

    路径：data/sessions.db
    """

    def __init__(self, db_path: str = None):
        self.db_path = str(db_path or SESSIONS_DB)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _ensure_schema(self):
        """创建与 session-trim-v2.py 兼容的表结构

        两个 FTS5 索引：
        - session_archive_fts: unicode61（兼容 session-trim-v2.py 的拉丁搜索）
        - session_archive_fts_trigram: trigram（双十二补充，支持 CJK + Latin n-gram）
        """
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                archived_at TEXT,
                msg_index INTEGER,
                role TEXT,
                content TEXT,
                msg_timestamp REAL,
                topic TEXT DEFAULT ''
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS session_archive_fts
                USING fts5(content, tokenize=unicode61);
            CREATE VIRTUAL TABLE IF NOT EXISTS session_archive_fts_trigram
                USING fts5(content, tokenize=trigram);
        """)
        self._conn.commit()

    def archive_messages(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        source: str = "standalone",
        topic: str = "",
    ) -> int:
        """归档一批消息到 sessions.db

        Args:
            session_id: 会话 ID
            messages: 消息列表（每项有 role, content, timestamp 等）
            source: 来源标识
            topic: 主题（可选，用第一条 user 消息自动提取）

        Returns:
            归档消息数
        """
        if not messages:
            return 0

        archived_at = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
        count = 0

        with self._write_lock:
            conn = self._conn
            try:
                for idx, msg in enumerate(messages):
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")
                    if isinstance(content, (dict, list)):
                        content = json.dumps(content, ensure_ascii=False)
                    elif content is None:
                        content = ""
                    else:
                        content = str(content)

                    ts_orig = msg.get("timestamp", 0.0)

                    # 自动提取 topic
                    msg_topic = topic
                    if not msg_topic and role == "user" and len(content) > 5:
                        msg_topic = content.strip()[:80]

                    conn.execute(
                        "INSERT INTO session_archive "
                        "(session_id, archived_at, msg_index, role, content, "
                        " msg_timestamp, topic) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (session_id, archived_at, idx, role, content,
                         ts_orig, msg_topic),
                    )
                    last_id = conn.execute(
                        "SELECT last_insert_rowid()"
                    ).fetchone()[0]

                    # FTS5 — unicode61（兼容 session-trim）
                    ft_text = content[:5000]
                    conn.execute(
                        "INSERT INTO session_archive_fts (rowid, content) "
                        "VALUES (?, ?)",
                        (last_id, ft_text),
                    )
                    # FTS5 — trigram（CJK + Latin n-gram）
                    conn.execute(
                        "INSERT INTO session_archive_fts_trigram (rowid, content) "
                        "VALUES (?, ?)",
                        (last_id, ft_text),
                    )
                    count += 1

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return count

    def search(
        self,
        query: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """FTS5 搜索归档内容（先试 trigram，失败回退 unicode61）

        Args:
            query: 搜索关键词
            limit: 最大结果数

        Returns:
            [{"session_id", "role", "content", "topic", "archived_at", "rank"}, ...]
        """
        # 先试 trigram（CJK + Latin 都支持）
        fts_tables = [
            ("session_archive_fts_trigram", "trigram"),
            ("session_archive_fts", "unicode61"),
        ]
        for fts_table, tokenizer in fts_tables:
            try:
                rows = self._conn.execute(
                    f"SELECT a.session_id, a.role, a.content, a.topic, "
                    f"       a.archived_at, a.msg_timestamp, "
                    f"       rank "
                    f"FROM {fts_table} f "
                    f"JOIN session_archive a ON f.rowid = a.id "
                    f"WHERE {fts_table} MATCH ? "
                    f"ORDER BY rank "
                    f"LIMIT ?",
                    (query, limit),
                ).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except Exception as e:
                # FTS5 语法错误等，尝试下一个
                continue

        # 双保险：LIKE 查询
        try:
            like_q = f"%{query}%"
            rows = self._conn.execute(
                "SELECT session_id, role, content, topic, archived_at, "
                "       msg_timestamp, 0.0 as rank "
                "FROM session_archive "
                "WHERE content LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                (like_q, limit),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except Exception:
            pass

        return []

    def get_stats(self) -> Dict[str, Any]:
        """统计信息"""
        try:
            session_count = self._conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS cnt FROM session_archive"
            ).fetchone()["cnt"]
            msg_count = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM session_archive"
            ).fetchone()["cnt"]
        except Exception:
            session_count = 0
            msg_count = 0

        return {
            "sessions": session_count,
            "messages": msg_count,
        }

    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
