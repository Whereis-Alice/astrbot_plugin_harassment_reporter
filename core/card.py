"""聊天记录卡片。

这张卡片只做一件事：把「最近的聊天记录」画成一张像聊天截图的图片。
它是纯粹的锦上添花 —— 渲染失败一律返回 None，调用方继续用纯文本发送，
绝不因为画图失败而让消息送不出去。
"""

from __future__ import annotations

import time
import zlib
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .text import PLUGIN_DISPLAY_NAME, clean_text, format_ts, now_text, truncate

LOG_PREFIX = "[HarassmentReporter]"
TEMPLATE_FILE = Path(__file__).resolve().parent.parent / "templates" / "chat_card.html"

# 卡片渲染依赖 AstrBot 的文转图服务（要起浏览器、可能走网络）。
# 连续失败时先熄火一段时间，避免每次上报都白等一轮超时。
FAILURE_THRESHOLD = 3
COOLDOWN_AFTER_FAILURE = 600

# 头像底色。按发送者取一个稳定的颜色，同一个人每次都是同一色，
# 这样一眼就能看出「谁在说话」。
AVATAR_COLORS = (
    "#6a5cff",
    "#ff7a59",
    "#2fb583",
    "#3f8cff",
    "#c869d6",
    "#e0873a",
    "#3fb0c9",
    "#d9536f",
    "#7d8bff",
    "#59a14f",
)


def _plain(value: Any, limit: int = 400) -> str:
    """去掉可能破坏卡片排版的尖括号，并限制长度。"""
    text = clean_text(value).replace("<", "＜").replace(">", "＞")
    return truncate(text, limit)


def _avatar_color(seed: str) -> str:
    """按发送者算一个固定的头像底色。

    这里用 crc32 而不是内置 hash()：Python 的字符串 hash 每次启动都会加盐，
    同一个人在不同次运行里会变色。
    """
    key = clean_text(seed) or "unknown"
    return AVATAR_COLORS[zlib.crc32(key.encode("utf-8")) % len(AVATAR_COLORS)]


def _initial(name: str) -> str:
    """取昵称里第一个能显示的字符当头像文字。"""
    for char in clean_text(name):
        if char.strip():
            return char.upper()
    return "?"


class CardRenderer:
    """把聊天记录渲染成一张卡片图。"""

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

    # ------------------------------------------------------------------
    # 气泡数据
    # ------------------------------------------------------------------
    def build_messages(
        self,
        lines: list[dict[str, Any]],
        *,
        limit: int,
        text_limit: int = 320,
    ) -> list[dict[str, str]]:
        """把标准化聊天行（ChatLine.as_dict()）转成卡片气泡数据。

        Bot 自己说的话靠右显示，其余靠左，和常见聊天软件一致。
        """
        show_time = self._settings.card_show_time
        messages: list[dict[str, str]] = []
        for line in lines[-max(1, limit) :]:
            is_self = bool(line.get("is_self"))
            name = _plain(line.get("sender_name") or line.get("name") or "未知用户", 24)
            seed = clean_text(line.get("sender_id")) or name
            stamp = line.get("timestamp")
            messages.append(
                {
                    "side": "right" if is_self else "left",
                    "name": name,
                    "initial": _initial(name),
                    "color": _avatar_color(seed),
                    "time": (format_ts(stamp, "%H:%M") if (show_time and stamp) else ""),
                    "text": _plain(line.get("text"), text_limit) or "[空消息]",
                }
            )
        return messages

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    async def render(
        self,
        *,
        kind: str,
        title: str,
        subtitle: str = "",
        icon: str = "",
        tag: str = "",
        note: str = "",
        messages: list[dict[str, str]] | None = None,
        footer: str = "",
        empty_hint: str = "这次没有可附带的聊天记录",
    ) -> str | None:
        """渲染卡片并返回本地图片路径；功能关闭、熄火中或渲染失败时返回 None。"""
        if not self.enabled_for(kind) or self.muted:
            return None

        clean_title = _plain(title, 40) or "聊天记录"
        data = {
            "theme": self._settings.card_theme,
            "card_width": self._settings.card_width,
            "icon": _plain(icon, 2) or _initial(clean_title),
            "title": clean_title,
            "subtitle": _plain(subtitle, 90),
            "tag": _plain(tag, 16),
            "note": _plain(note, 600),
            "messages": messages or [],
            "footer": _plain(footer, 90) or f"由「{PLUGIN_DISPLAY_NAME}」生成",
            "generated_at": now_text(),
            "empty_hint": _plain(empty_hint, 60),
            "show_avatar": bool(self._settings.card_show_avatar),
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

    # ------------------------------------------------------------------
    # 便捷入口
    # ------------------------------------------------------------------
    async def render_chatlog(
        self,
        chatlog: Any,
        *,
        kind: str,
        tag: str = "",
        note: str = "",
        footer: str = "",
        empty_hint: str = "这次没有可附带的聊天记录",
        allow_empty: bool = False,
    ) -> str | None:
        """直接把一个 ChatLog 画成卡片，三条链路共用。"""
        if chatlog is None:
            return None
        if chatlog.empty and not allow_empty:
            return None
        return await self.render(
            kind=kind,
            title=chatlog.title,
            subtitle=chatlog.subtitle,
            tag=tag,
            note=note,
            messages=self.build_messages(
                chatlog.rows(),
                limit=self._settings.card_max_messages,
            ),
            footer=footer,
            empty_hint=empty_hint,
        )
