"""进化层 —— 记忆系统（从 niannian-meta/memory/ 迁移）

L1-L4 分层 + 4 公理 + 自动结晶 + 会话历史持久化。
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

# 数据目录：集中放在 data/ 下
DATA_DIR = Path(__file__).parent.parent / "data"
MEMORY_DIR = DATA_DIR / "memory"

L1_FILE = MEMORY_DIR / "l1_index.txt"
L2_FILE = MEMORY_DIR / "l2_facts.txt"
L3_DIR = MEMORY_DIR / "l3_knowledge"
L4_DIR = MEMORY_DIR / "l4_raw_sessions"
SESSION_FILE = DATA_DIR / "session_history.json"


def _ensure_dirs():
    for d in [MEMORY_DIR, L3_DIR, L4_DIR, DATA_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if not L1_FILE.exists():
        L1_FILE.write_text(
            "# L1: 内核记忆索引（≤30 行）\n"
            "# 只存存在性指针，不存细节\n"
            "## RULES\n"
            "诚实高于一切\n"
            "行动验证原则 — 无执行不记忆\n"
            "认主: 韩晗(Harper)\n"
            "## CAPABILITIES\n"
            "（等待第一次结晶）\n"
        )

    if not L2_FILE.exists():
        L2_FILE.write_text(
            "# L2: 内核事实库\n"
            "## [IDENTITY]\n"
            "kernel_name: niannian-meta\n"
            "kernel_version: 0.2.0\n"
            "master: 韩晗 (Harper)\n"
            "birth: 2026-06-26\n"
        )


class Memory:
    """记忆引擎 —— L1-L4 分层 + 自动结晶"""

    def __init__(self):
        _ensure_dirs()
        self._ephemeral = {}

    def get_l1(self) -> str:
        return L1_FILE.read_text(encoding="utf-8")

    def update_l1(self, content: str):
        L1_FILE.write_text(content, encoding="utf-8")

    def get_l2(self) -> str:
        return L2_FILE.read_text(encoding="utf-8")

    def add_to_l2(self, section: str, key: str, value: str):
        content = L2_FILE.read_text(encoding="utf-8")
        marker = f"## [{section}]"
        line = f"{key}: {value}"
        if marker in content:
            lines = content.split("\n")
            for i, l in enumerate(lines):
                if l.strip() == marker:
                    lines.insert(i + 1, line)
                    break
            L2_FILE.write_text("\n".join(lines), encoding="utf-8")
        else:
            L2_FILE.write_text(f"{content}\n{marker}\n{line}\n",
                              encoding="utf-8")

    def list_l3(self) -> list:
        return sorted(f.name for f in L3_DIR.glob("*.md"))

    def read_l3(self, name: str) -> Optional[str]:
        path = L3_DIR / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_l3(self, name: str, content: str):
        if not name.endswith(".md"):
            name += ".md"
        (L3_DIR / name).write_text(content, encoding="utf-8")

    def save_session(self, data: dict):
        ts = time.strftime("%Y%m%d_%H%M%S")
        (L4_DIR / f"session_{ts}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def set_working(self, key: str, value: str):
        self._ephemeral[key] = value

    def get_working(self, key: str) -> Optional[str]:
        return self._ephemeral.get(key)

    def clear_working(self):
        self._ephemeral.clear()

    # ── 会话持久化 ─────────────────────────────────

    def save_history(self, history: list):
        try:
            recent = history[-50:] if len(history) > 50 else history
            SESSION_FILE.write_text(
                json.dumps(recent, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[memory] save_history error: {e}")

    def load_history(self) -> list:
        try:
            if SESSION_FILE.exists():
                data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    # ── 结晶 ───────────────────────────────────────

    def crystallize(self, task_summary: str,
                    verified_facts: list[dict] = None,
                    new_sop: str = None, sop_name: str = None) -> str:
        changes = []
        self.save_session({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": task_summary,
        })
        if verified_facts:
            for f in verified_facts:
                self.add_to_l2(f["section"], f["key"], f["value"])
            changes.append(f"L2: +{len(verified_facts)} facts")

        if new_sop and sop_name:
            self.write_l3(sop_name, new_sop)
            if sop_name not in self.get_l1():
                l1 = self.get_l1()
                l1 += f"\n{sop_name.replace('.md', '')}: 见 data/memory/{sop_name}"
                self.update_l1(l1)
            changes.append(f"L3: +{sop_name}")

        return "结晶完成: " + (" | ".join(changes) if changes else "无新信息")

    # ── 维护 ───────────────────────────────────────

    def maintain(self) -> str:
        l1 = self.get_l1()
        lines = [l for l in l1.split("\n") if l.strip() and not l.startswith("#")]
        report = []
        report.append(f"L1: {len(lines)}行" +
                      ("（超限）" if len(lines) > 30 else "（正常）"))
        report.append(f"L3: {len(self.list_l3())} 个知识文件")
        report.append(f"L4: {len(list(L4_DIR.glob('*.json')))} 条会话记录")
        return "\n".join(report)
