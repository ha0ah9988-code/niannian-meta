"""内核记忆系统 —— L1-L4 分层 + 自动结晶 + 4 公理。

设计来源：GA 的记忆分层 + Hermes 的记忆管理。
记忆属于内核自身，无论内核接入什么框架都随身携带。
"""

import os
import re
import json
import time
from pathlib import Path
from typing import Optional


MEMORY_DIR = Path(__file__).parent

L1_FILE = MEMORY_DIR / "l1_index.txt"          # ≤30 行精简索引
L2_FILE = MEMORY_DIR / "l2_facts.txt"          # 已验证的事实库
L3_DIR = MEMORY_DIR / "l3_knowledge"            # SOP / 规则 / 技能
L4_DIR = MEMORY_DIR / "l4_raw_sessions"         # 原始会话历史


def _ensure_dirs():
    """确保记忆目录和初始文件存在"""
    for d in [L3_DIR, L4_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if not L1_FILE.exists():
        L1_FILE.write_text("# L1: 内核记忆索引（≤30 行）\n"
                          "# 只存存在性指针，不存细节\n"
                          "# 格式: 场景关键词 → 文件名\n\n"
                          "## RULES\n"
                          "诚实高于一切 — 不确定就说不确定\n"
                          "行动验证原则 — 无执行不记忆\n"
                          "认主: 韩晗(Harper)\n\n"
                          "## CAPABILITIES\n"
                          "（等待第一次结晶）\n")

    if not L2_FILE.exists():
        L2_FILE.write_text("# L2: 内核事实库\n"
                          "## [IDENTITY]\n"
                          "kernel_name: niannian-meta\n"
                          "kernel_version: 0.1.0\n"
                          "master: 韩晗 (Harper)\n"
                          "birth: 2026-06-26\n"
                          "location: VPS (5.15.0-181-generic)\n\n"
                          "## [ENV]\n"
                          "home: /root/niannian-meta/\n"
                          "timezone: UTC+8\n")


class Memory:
    """内核记忆引擎 —— 结晶、分层、自维护"""

    def __init__(self):
        _ensure_dirs()
        self._ephemeral = {}  # 本轮的工作记忆（不持久化）

    # ─── L1: 精简索引 ──────────────────────────────────

    def get_l1(self) -> str:
        """读取 L1 索引"""
        return L1_FILE.read_text(encoding="utf-8")

    def update_l1(self, new_content: str):
        """更新 L1（追加式，不 overwrite）"""
        L1_FILE.write_text(new_content, encoding="utf-8")

    # ─── L2: 事实库 ───────────────────────────────────

    def get_l2(self) -> str:
        return L2_FILE.read_text(encoding="utf-8")

    def add_to_l2(self, section: str, key: str, value: str):
        """向 L2 指定 section 添加一条事实"""
        content = L2_FILE.read_text(encoding="utf-8")
        marker = f"## [{section}]"
        new_line = f"{key}: {value}"

        if marker in content:
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if line.strip() == marker:
                    # 在 section 标题后插入
                    lines.insert(i + 1, new_line)
                    break
            L2_FILE.write_text("\n".join(lines), encoding="utf-8")
        else:
            L2_FILE.write_text(f"{content}\n{marker}\n{new_line}\n",
                              encoding="utf-8")

    # ─── L3: 知识库 ───────────────────────────────────

    def list_l3(self) -> list[str]:
        """列出 L3 知识文件"""
        return sorted(f.name for f in L3_DIR.glob("*.md"))

    def read_l3(self, name: str) -> Optional[str]:
        """读取 L3 文件"""
        path = L3_DIR / name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def write_l3(self, name: str, content: str):
        """写入 L3 SOP/规则"""
        if not name.endswith(".md"):
            name += ".md"
        (L3_DIR / name).write_text(content, encoding="utf-8")

    # ─── L4: 会话历史 ─────────────────────────────────

    def save_session(self, session_data: dict):
        """保存一次会话到 L4"""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        path = L4_DIR / f"session_{timestamp}.json"
        path.write_text(json.dumps(session_data, ensure_ascii=False, indent=2),
                       encoding="utf-8")

    # ─── 工作记忆（本轮，不持久化） ──────────────────

    def set_working(self, key: str, value: str):
        self._ephemeral[key] = value

    def get_working(self, key: str) -> Optional[str]:
        return self._ephemeral.get(key)

    def clear_working(self):
        self._ephemeral.clear()

    # ─── 结晶机制 ─────────────────────────────────────

    def crystallize(self, task_summary: str, verified_facts: list[dict] = None,
                    new_sop: str = None, sop_name: str = None) -> str:
        """从任务中结晶记忆。

        Args:
            task_summary: 任务摘要（写入 L4 并可能影响 L1）
            verified_facts: [{"section": "...", "key": "...", "value": "..."}]
            new_sop: 新的 SOP 内容
            sop_name: SOP 文件名

        Returns:
            结晶结果摘要
        """
        changes = []

        # 1. 保存会话摘要到 L4
        self.save_session({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": task_summary,
        })

        # 2. 写入 L2 事实
        if verified_facts:
            for fact in verified_facts:
                self.add_to_l2(fact["section"], fact["key"], fact["value"])
            changes.append(f"L2: +{len(verified_facts)} facts")

        # 3. 写入 L3 SOP
        if new_sop and sop_name:
            self.write_l3(sop_name, new_sop)
            changes.append(f"L3: +{sop_name}")

        # 4. 同步 L1 索引（如果有新能力）
        if sop_name and not self._l1_has(sop_name):
            current_l1 = self.get_l1()
            # 在 CAPABILITIES 下加一行
            current_l1 += f"\n{sop_name.replace('.md', '')}: 见 L3/{sop_name}"
            self.update_l1(current_l1)
            changes.append("L1: 索引更新")

        result = "结晶完成: " + (" | ".join(changes) if changes else "无可结晶的新信息")
        return result

    def _l1_has(self, name: str) -> bool:
        return name in L1_FILE.read_text(encoding="utf-8")

    # ─── 记忆维护 ─────────────────────────────────────

    def maintain(self) -> str:
        """执行记忆维护（压缩、层级迁移、清理）"""
        l1 = self.get_l1()
        lines = l1.strip().split("\n")
        report = []

        # 检查 L1 行数
        content_lines = [l for l in lines if l.strip() and not l.startswith("#")]
        if len(content_lines) > 30:
            report.append(f"L1 超限: {len(content_lines)}行 > 30行（需要压缩）")
        else:
            report.append(f"L1: {len(content_lines)}行（正常）")

        # 检查 L3 文件数
        l3_files = self.list_l3()
        report.append(f"L3: {len(l3_files)} 个知识文件")

        # 检查 L4 会话数
        l4_files = list(L4_DIR.glob("*.json"))
        report.append(f"L4: {len(l4_files)} 条会话记录")

        return "\n".join(report)
