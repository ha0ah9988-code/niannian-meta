# niannian-meta 项目架构

> 项目宪法目录 —— 内核如何与自身、外部框架交互。
> 不注入，需要时查阅。

---

## 目录结构

```
niannian-meta/
├── identity/       ← 灵魂/宪法/主人/索引（身份层）
├── core/           ← 循环/工具/安全/LLM（核心层）
├── evolution/      ← 记忆/结晶/技能（进化层）
├── app/            ← TG gateway（应用层）
└── data/           ← 运行时数据/记忆持久化
```

---

## 核心依赖

- **LLM**: 通过 core/llm.py 对接任意 OpenAI 兼容 API
- **TG**: 通过 app/tg.py polling + bot API，不走第三方库
- **HTTP**: httpx（LLM 请求）+ urllib（TG polling）
- **其他**: 标准库 + 最小 pip 依赖

---

## 接入其他框架

niannian-meta 可以调用外部框架的能力，方式：

```text
niannian-meta
    │
    ├── 直接调用 terminal → hermes <command>
    │                      调用已安装的 hermes CLI
    │
    └── 通过 app/adapter  → 以 plugin/middleware 形式
                           接入 Hermes gateway
```

调用外部框架时，soul.md + rules.md 的约束力始终优先。

---

## 技能管理

技能存放路径：`data/skills/`（未来可通过 skill_manage 工具 CRUD）

技能格式：

```yaml
---
name: skill-name
description: 一句话描述
version: 0.1.0
---

# 技能内容

## 使用场景

## 操作步骤

## 注意事项
```
