"""TG 底层 API 封装 —— 所有直接调 Telegram Bot API 的代码统一在这里。

职责：
- HTTP 请求封装（urllib，无第三方依赖）
- 统一错误处理 + 重试（429 退避）
- Token/chat_id 配置
"""

import json
import time
import urllib.request
import urllib.parse
from typing import Optional

BOT_TOKEN = "8769651388:AAHb5c4YClHM6WOr04EFUw0PLMnFwTJZpnM"
CHAT_ID = "8391869847"
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


class APIError(Exception):
    """TG API 调用错误"""
    pass


def _request(method: str, params: dict = None,
             timeout: int = 30) -> dict:
    """通用 TG API 请求（带 429 退避重试）"""
    url = f"{API_BASE}/{method}"
    if params:
        data = urllib.parse.urlencode(params).encode()
    else:
        data = None

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = urllib.request.urlopen(req, timeout=timeout)
            result = json.loads(resp.read())
            if not result.get("ok"):
                raise APIError(f"API error: {result}")
            return result
        except urllib.request.HTTPError as e:
            if e.code == 429 and attempt < 2:
                retry_after = int(e.headers.get("Retry-After", 5))
                time.sleep(retry_after)
                continue
            raise APIError(f"HTTP {e.code}: {e.read().decode()[:200]}")
        except urllib.request.URLError as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise APIError(f"Network error: {e.reason}")


def send_message(text: str, chat_id: str = CHAT_ID,
                 parse_mode: str = "HTML") -> dict:
    """发送消息（自动分片）"""
    if not text:
        return {"ok": True}

    if len(text) > 4000:
        text = text[:3997] + "..."

    return _request("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    })


def send_typing(chat_id: str = CHAT_ID):
    """显示 typing 状态"""
    try:
        _request("sendChatAction", {
            "chat_id": chat_id,
            "action": "typing",
        }, timeout=5)
    except APIError:
        pass


def get_updates(offset: int = 0, timeout: int = 30) -> list:
    """长轮询获取未处理消息"""
    result = _request("getUpdates", {
        "offset": offset,
        "timeout": timeout,
    }, timeout=timeout + 5)
    return result.get("result", [])


def get_me() -> dict:
    """验证 bot token"""
    return _request("getMe")


def delete_message(chat_id: str, message_id: int):
    """删除消息"""
    try:
        _request("deleteMessage", {
            "chat_id": chat_id,
            "message_id": message_id,
        })
    except APIError:
        pass
