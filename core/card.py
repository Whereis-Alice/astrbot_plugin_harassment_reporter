from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .text import clean_text, now_text, truncate

LOG_PREFIX = "[HarassmentReporter]"
TEMPLATE_FILE = Path(__file__).resolve().parent.parent / "templates" / "report_card.html"

# 卡片渲染依赖 AstrBot 的文转图服务（走网络）。连续失败时先熄火一段时间，
# 避免每次上报都白等一轮网络超时。
FAILURE_THRESHOLD = 3
COOLDOWN_AFTER_FAILURE = 600


def _plain(value: Any, limit: int = 400) -> str:
    """去掉可能破坏卡片排版的尖括号，并限制长度。"""
    text = clean_text(value).replace("<", "＜").replace(">", "＞")
    return truncate(text, limit)


class CardRenderer:
    """把上报内容渲染成一张聊天记录卡片。

    渲染永远是"锦上添花"：失败时返回 None，调用方继续用纯文本发送。
    """

    def __init__(self, star: Any, settings: Any) -> None:
        self._star = star
        self._settings = settings
        self._template: str | None = None
        self._failures = 0
        self._muted_until = 0.0

    def _load_template(self) -> str:
        if self._template is None:
            self._template = TEMPLATE_FILE.read_text(encoding="utf-8")
        return self._template

    @property
    def muted(self) -> bool:
        return time.time() < self._muted_until

    def enabled_for(self, kind: str) -> bool:
        settings = self._settings
        if not settings.card_enabled:
            return False
        if kind == "harassment":
            return settings.card_for_harassment
        if kind == "feedback":
            return settings.card_for_feedback
        if kind == "notice":
            return settings.card_for_notice
        return True

    def build_messages(
        self,
        lines: list[dict[str, Any]],
        *,
        limit: int,
        text_limit: int = 320,
    ) -> list[dict[str, str]]:
        """把标准化聊天行转换成卡片气泡数据。

        role 为 assistant（Bot 自己说的话）时靠右显示。
        """
        from .text import format_ts

        messages: list[dict[str, str]] = []
        for line in lines[-max(1, limit) :]:
            role = clean_text(line.get("role")) or ("assistant" if line.get("is_self") else "user")
            timestamp = line.get("timestamp")
            messages.append(
                {
                    "role": role,
                    "side": "right" if role == "assistant" else "left",
                    "name": _plain(line.get("name") or line.get("sender_name") or "未知用户", 40),
                    "time": clean_text(line.get("time"))
                    or (format_ts(timestamp, "%m-%d %H:%M") if timestamp else ""),
                    "text": _plain(line.get("text"), text_limit) or "[空消息]",
                }
            )
        return messages

    async def render(
        self,
        *,
        kind: str,
        title: str,
        subtitle: str = "",
        icon: str = "🦊",
        badge: str = "",
        badge_level: str = "info",
        summary: str = "",
        summary_title: str = "情况说明",
        chat_title: str = "聊天记录",
        meta: list[dict[str, str]] | None = None,
        messages: list[dict[str, str]] | None = None,
        footer: str = "",
        show_empty_chat: bool = False,
    ) -> str | None:
        """渲染卡片并返回本地图片路径；不可用或失败时返回 None。"""
        if not self.enabled_for(kind) or self.muted:
            return None

        data = {
            "theme": self._settings.card_theme,
            "card_width": self._settings.card_width,
            "icon": icon or "🦊",
            "title": _plain(title, 40) or "消息卡片",
            "subtitle": _plain(subtitle, 90),
            "badge": _plain(badge, 24),
            "badge_level": badge_level if badge_level in {"high", "medium", "low", "info"} else "info",
            "summary": _plain(summary, 1200),
            "summary_title": _plain(summary_title, 20),
            "chat_title": _plain(chat_title, 20),
            "meta": [
                {"label": _plain(item.get("label"), 16), "value": _plain(item.get("value"), 120)}
                for item in (meta or [])
                if clean_text(item.get("value"))
            ],
            "messages": messages or [],
            "footer": _plain(footer, 90) or "由 骚扰上报器 / 反馈窗口 生成",
            "generated_at": now_text(),
            "show_empty_chat": bool(show_empty_chat),
        }

        try:
            path = await self._star.html_render(
                self._load_template(),
                data,
                return_url=False,
                options={"full_page": True, "type": "jpeg", "quality": 92},
            )
        except Exception as exc:
            self._failures += 1
            logger.warning(
                "%s 卡片渲染失败（第 %s 次），本次改用纯文本：%s",
                LOG_PREFIX,
                self._failures,
                exc,
            )
            if self._failures >= FAILURE_THRESHOLD:
                self._muted_until = time.time() + COOLDOWN_AFTER_FAILURE
                self._failures = 0
                logger.warning(
                    "%s 卡片渲染连续失败，暂停 %s 秒后再尝试。",
                    LOG_PREFIX,
                    COOLDOWN_AFTER_FAILURE,
                )
            return None

        self._failures = 0
        path = clean_text(path)
        if not path or not Path(path).exists():
            return None
        return path
