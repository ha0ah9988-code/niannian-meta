"""技能系统 —— SKILL.md 创建/管理/自动提取

技能格式（相同 SKILL.md）：
---
name: skill-name
description: 一句话描述（≤60字）
version: 0.1.0
---

## 使用场景
...

## 操作步骤
...

## 注意事项
...
"""

import os
from pathlib import Path
from typing import Optional

SKILLS_DIR = Path(__file__).parent.parent / "data" / "skills"


def ensure_dirs():
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def list_skills() -> list[dict]:
    """列出所有技能"""
    ensure_dirs()
    skills = []
    for f in sorted(SKILLS_DIR.glob("*.md")):
        name = f.stem
        desc = ""
        content = f.read_text(encoding="utf-8")
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("description:"):
                desc = line[len("description:"):].strip().strip('"')
                break
        skills.append({"name": name, "description": desc, "path": str(f)})
    return skills


def read_skill(name: str) -> Optional[str]:
    """读取技能内容"""
    path = SKILLS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def extract_from_conversation(conversation: list[dict], agent) -> Optional[str]:
    """从对话中提取技能 —— 核心学习能力

    分析最近的对话，判断是否有可复用的经验，
    如果有则生成 SKILL.md 并保存。
    """
    ensure_dirs()
    from core.providers.base import BaseProvider

    if not conversation:
        return None

    # 用 LLM 分析对话，提取技能
    messages = conversation[-20:]  # 最近 20 条

    # 让 LLM 自己判断：这段对话是否值得提取为技能
    system = """你是一个技能提取器。分析最近的对话，判断是否存在以下内容：

1. 一个可复用的操作流程（比如"每次做 X 都要先 Y 再 Z"）
2. 一个值得记住的配置/路径/命令
3. 一个反复踩坑的排错经验

如果有，请按以下格式输出（markdown代码块内）：

```skill
---
name: <小写连字符>
description: <一句话描述，≤60字>
---

## 使用场景
...

## 操作步骤
...

## 注意事项
...
```

如果没有值得提取为技能的内容，只输出：SKIP
"""

    try:
        provider = getattr(agent, 'provider', None)
        if provider is None:
            return None

        resp = provider.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": f"分析以下对话，提取技能：\n{_format_conversation(messages)}"},
        ])

        content = resp.get("content", "").strip()

        if "SKIP" in content and len(content) < 10:
            return None

        # 解析技能内容
        if "```skill" in content:
            skill_text = content.split("```skill")[1].split("```")[0].strip()
        elif "```yaml" in content:
            skill_text = content.split("```yaml")[1].split("```")[0].strip()
        else:
            return None

        # 提取 name 作为文件名
        name = ""
        for line in skill_text.split("\n"):
            line = line.strip()
            if line.startswith("name:"):
                name = line[len("name:"):].strip().strip('"')
                break

        if not name:
            return None

        # 保存技能
        path = SKILLS_DIR / f"{name}.md"
        path.write_text(skill_text, encoding="utf-8")

        # 更新 L1 索引
        agent.memory.add_to_l2("SKILLS", name,
                               f"见 data/skills/{name}.md")

        return name

    except Exception as e:
        print(f"[learn] 提取失败: {e}")
        return None


def _format_conversation(messages: list) -> str:
    """格式化对话供 LLM 分析"""
    lines = []
    for msg in messages[-10:]:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if content:
            lines.append(f"[{role}]: {content[:200]}")
    return "\n".join(lines)


def learn_from_turn(agent, user_input: str, response: str):
    """每轮对话后自动判断是否需要学习

    由 Agent 在 process() 完成后调用。
    """
    # 简单的启发式判断：如果用户说了"记住""记下""以后用"等词
    trigger_words = ["记住", "记下", "以后用", "学一下", "把这个记下来",
                     "存成技能", "save this", "remember"]
    should_learn = any(w in user_input.lower() for w in trigger_words)

    if should_learn:
        recent = agent.history[-10:]
        name = extract_from_conversation(recent, agent)
        if name:
            print(f"[learn] 已提取技能: {name}")
