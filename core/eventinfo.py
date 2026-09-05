from __future__ import annotations

from typing import Any

from .text import clean_text


def session_id(event: Any) -> str:
    return clean_text(getattr(event, "unified_msg_origin", ""), "unknown")


def platform_id(event: Any) -> str:
    """平台实例 ID（同一平台可能配置了多个实例）。"""
    try:
        return clean_text(event.get_platform_id(), "unknown")
    except Exception:
        return "unknown"


def platform_name(event: Any) -> str:
    try:
        return clean_text(event.get_platform_name(), "unknown")
    except Exception:
        return "unknown"


def group_id(event: Any) -> str:
    try:
        return clean_text(event.get_group_id())
    except Exception:
        return ""


def self_id(event: Any) -> str:
    try:
        return clean_text(event.get_self_id())
    except Exception:
        return ""


def sender_id(event: Any) -> str:
    try:
        return clean_text(event.get_sender_id(), "unknown")
    except Exception:
        return "unknown"


def sender_name(event: Any) -> str:
    try:
        return clean_text(event.get_sender_name(), "未知用户")
    except Exception:
        return "未知用户"


def message_text(event: Any, fallback: str = "") -> str:
    return clean_text(getattr(event, "message_str", ""), fallback)


def watch_key(event: Any) -> str:
    """观察名单里一个人的唯一键：平台实例 + 用户 ID。"""
    return f"{platform_id(event)}:{sender_id(event)}"


def warn_cache_key(event: Any) -> str:
    """「先警告再上报」的记忆键：同一个人在不同会话里分别计数。"""
    return f"{session_id(event)}|{watch_key(event)}"


def origin_label(event: Any) -> str:
    """给人看的来源描述，例如「群 123456」或「私聊」。"""
    gid = group_id(event)
    if gid:
        return f"群 {gid}"
    return "私聊"
