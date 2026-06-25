"""消息发送器 —— 分片 + 格式化 + 重试 + 编辑

支持 HTML/Markdown 格式，自动分片，失败重试。
"""

import html
import re
from typing import Optional

from .api import send_message as _api_send


def send(text: str, chat_id: str = None, parse_mode: str = "HTML") -> bool:
    """发送消息（自动分片 + sanitize）

    Args:
        text: 消息文本
        chat_id: 目标（默认主控）
        parse_mode: HTML 或 MarkdownV2

    Returns:
        是否成功
    """
    if not text:
        return True

    # sanitize HTML
    if parse_mode == "HTML":
        text = _sanitize_html(text)

    # 分片
    chunks = _split(text, max_len=4000)

    for chunk in chunks:
        try:
            _api_send(chunk, chat_id=chat_id, parse_mode=parse_mode)
        except Exception as e:
            # 如果 HTML 格式失败，降级为纯文本重试
            if parse_mode == "HTML" and "can't parse" in str(e).lower():
                plain = re.sub(r"<[^>]+>", "", chunk)
                try:
                    _api_send(plain, chat_id=chat_id, parse_mode="")
                except:
                    return False
            else:
                return False
    return True


def send_progress(tool_name: str, preview: str = "",
                  chat_id: str = None) -> bool:
    """发送工具进度通知（紧凑格式，不刷屏）"""
    brief = f"🛠️ {tool_name}"
    if preview:
        brief += f": {preview}"
    return send(brief, chat_id=chat_id)


def send_error(msg: str, chat_id: str = None) -> bool:
    """发送错误通知"""
    return send(f"❌ {msg}", chat_id=chat_id)


def _sanitize_html(text: str) -> str:
    """清理 HTML，移除不支持的标签"""
    # 移除 think 块
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # 只保留白名单标签
    allowed = {"b", "i", "code", "pre", "a", "strong", "em", "s", "u"}
    text = re.sub(r"<(/?(?:" + "|".join(allowed) + r")[^>]*)>",
                  lambda m: m.group(0), text)
    # 其他标签转义
    text = re.sub(r"<(?!\/?(" + "|".join(allowed) + r")\b)[^>]*>",
                  "", text)

    # 转义裸露的 & < >
    text = text.replace("&", "&amp;")
    text = re.sub(r"&amp;(#\d+|[a-zA-Z]+);", lambda m: m.group(0)
                  .replace("&amp;", "&"), text)
    text = re.sub(r"<(/?(" + "|".join(allowed) + r"))",
                  lambda m: m.group(0).replace("&amp;", "&"), text)

    return text


def _split(text: str, max_len: int = 4000) -> list:
    """将长消息按最大长度分片（在换行处切）"""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # 在 max_len 之前的最后一个换行处切
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = text.rfind("。", 0, max_len)
        if cut < max_len // 2:
            cut = text.rfind(" ", 0, max_len)
        if cut < max_len // 2:
            cut = max_len

        chunks.append(text[:cut].strip())
        text = text[cut:].strip()

    return chunks
