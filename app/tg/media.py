"""媒体处理 —— 下载、格式判断、清理

支持：图片、文件、语音消息的下载和缓存。
"""

import os
import time
import urllib.request
from pathlib import Path

from .api import _request, BOT_TOKEN, API_BASE

MEDIA_DIR = Path(__file__).parent.parent.parent / "data" / "media"


def ensure_dirs():
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def get_file_path(file_id: str) -> str:
    """获取文件在 TG 服务器上的路径"""
    result = _request("getFile", {"file_id": file_id})
    return result.get("result", {}).get("file_path", "")


def download_file(file_id: str, suffix: str = "") -> str:
    """下载文件到本地缓存

    Args:
        file_id: TG file_id
        suffix: 文件后缀（.jpg .ogg 等）

    Returns:
        本地文件路径
    """
    ensure_dirs()
    file_path = get_file_path(file_id)
    if not file_path:
        return ""

    local_name = f"{file_id}{suffix}" if suffix else os.path.basename(file_path)
    local_path = MEDIA_DIR / local_name

    if local_path.exists():
        return str(local_path)

    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    try:
        urllib.request.urlretrieve(url, local_path)
        return str(local_path)
    except Exception as e:
        print(f"[media] 下载失败: {e}")
        return ""


def download_voice(file_id: str) -> str:
    """下载语音消息（转 OGG）"""
    return download_file(file_id, suffix=".ogg")


def download_photo(file_id: str) -> str:
    """下载图片（最大分辨率）"""
    return download_file(file_id, suffix=".jpg")


def download_document(file_id: str, file_name: str = "") -> str:
    """下载文件"""
    suffix = ""
    if file_name:
        _, ext = os.path.splitext(file_name)
        suffix = ext
    return download_file(file_id, suffix=suffix)


def cleanup_old(max_age: int = 3600):
    """清理超过 max_age 秒的缓存文件"""
    ensure_dirs()
    now = time.time()
    for f in MEDIA_DIR.iterdir():
        if f.is_file() and now - f.stat().st_mtime > max_age:
            f.unlink()
