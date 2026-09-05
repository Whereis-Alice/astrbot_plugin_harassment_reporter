"""聊天记录卡片。

这张卡片只做一件事：把「最近的聊天记录」画成一张像聊天截图的图片。
它是纯粹的锦上添花 —— 渲染失败一律返回 None，调用方继续用纯文本发送，
绝不因为画图失败而让消息送不出去。
"""

from __future__ import annotations

import os
import time
import zlib
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .text import PLUGIN_DISPLAY_NAME, clean_text, format_ts, now_text, strip_placeholders, truncate

LOG_PREFIX = "[HarassmentReporter]"
TEMPLATE_FILE = Path(__file__).resolve().parent.parent / "templates" / "chat_card.html"

# 卡片的版式宽度（逻辑像素）。它只决定「一行能放多少字」，不决定图片有多大 ——
# 真正的出图宽度是 PAGE_WIDTH × 清晰度倍数（倍数由用户在配置里选）。
# 720 取的是手机聊天截图的观感：气泡不会宽得像一整段文章，读起来最像真的聊天记录。
PAGE_WIDTH = 720

# 卡片渲染依赖 AstrBot 的文转图服务（要起浏览器、可能走网络）。
# 连续失败时先熄火一段时间，避免每次上报都白等一轮超时。
FAILURE_THRESHOLD = 3
COOLDOWN_AFTER_FAILURE = 600

# 单个气泡里最多画几张缩略图。群友一次甩九张图的场面是有的，但卡片上画满九张
# 会把上下文全挤出去；三张足够看出「他发的是什么」，剩下的还有文字占位符提示。
SHOTS_PER_BUBBLE = 3

# 头像兜底底色。拿不到真实 QQ 头像时（其它平台、非数字 ID、图片加载失败）
# 就画一个彩色首字块；同一个人每次都是同一色，一眼能看出谁在说话。
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


def _shots(value: Any) -> list[str]:
    """挑出能直接放进 <img src> 的图片地址。

    只要 http 地址：卡片是丢给远端浏览器渲染的，本地路径和 base64 到了那边
    加载不出来，反而会在卡片上留一块空白。
    """
    if not isinstance(value, (list, tuple)):
        return []
    picked: list[str] = []
    for item in value:
        url = clean_text(item)
        if url.startswith(("http://", "https://")) and url not in picked:
            picked.append(url)
            if len(picked) >= SHOTS_PER_BUBBLE:
                break
    return picked


def _initial(name: str) -> str:
    """取昵称里第一个能显示的字符当头像文字。"""
    for char in clean_text(name):
        if char.strip():
            return char.upper()
    return "?"


def _sniff_image(file: Path) -> str | None:
    """读文件头认一下真实格式：返回 "png" / "jpeg" / "other"，不是图片就返回 None。

    AstrBot 下载渲染结果时不检查响应状态码，也一律用 .jpg 命名保存 ——
    渲染服务返回一段错误信息时，我们手上会是一个「看着像图片」的文件。
    与其把乱码当图片发给主人，不如在这里认出来，当成渲染失败退回纯文本。
    """
    try:
        with file.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF8", b"BM")):
        return "other"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "other"
    return None


def _checked_image_path(path: str) -> str | None:
    """确认渲染结果确实是张图片，并把 PNG 的后缀改回 .png。

    临时文件一律叫 .jpg，PNG 会名不副实；个别客户端按后缀猜类型时会出岔子。
    改名失败不算问题，继续用原路径发送即可。
    """
    if not path:
        return None
    file = Path(path)
    kind = _sniff_image(file)
    if kind is None:
        return None
    if kind == "png" and file.suffix.lower() != ".png":
        target = file.with_suffix(".png")
        try:
            os.replace(file, target)
            file = target
        except OSError:
            pass
    return str(file)


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
    ) -> list[dict[str, Any]]:
        """把标准化聊天行（ChatLine.as_dict()）转成卡片气泡数据。

        Bot 自己说的话靠右显示，其余靠左，和常见聊天软件一致。
        """
        show_time = self._settings.card_show_time
        show_shots = bool(self._settings.card_show_images)
        messages: list[dict[str, Any]] = []
        for line in lines[-max(1, limit) :]:
            is_self = bool(line.get("is_self"))
            name = _plain(line.get("sender_name") or line.get("name") or "未知用户", 24)
            seed = clean_text(line.get("sender_id")) or name
            stamp = line.get("timestamp")
            shots = _shots(line.get("images")) if show_shots else []
            text = _plain(line.get("text"), text_limit)
            # 图片画出来了，就不用再留一句「[图片]」了；文字被占位符占满的气泡
            # 直接留空，只显示缩略图，看起来才像真的聊天截图。
            if shots and not strip_placeholders(text).strip():
                text = ""
            messages.append(
                {
                    "side": "right" if is_self else "left",
                    "name": name,
                    "initial": _initial(name),
                    "color": _avatar_color(seed),
                    "time": (format_ts(stamp, "%H:%M") if (show_time and stamp) else ""),
                    "text": text or ("" if shots else "[空消息]"),
                    "avatar": clean_text(line.get("avatar")),
                    "images": shots,
                }
            )
        return messages

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _screenshot_options(self, *, lossless: bool) -> dict[str, Any]:
        """截图参数。PNG 不能带 quality —— 带了远端浏览器会直接报错。"""
        if lossless:
            return {"full_page": True, "type": "png"}
        return {"full_page": True, "type": "jpeg", "quality": 95}

    async def _shoot(self, data: dict[str, Any]) -> str:
        """去文转图服务截一张图，返回本地临时文件路径。

        整页截图会把横向溢出一起拍下来，所以模板里的 min-width 才是出图宽度的
        真正来源，和渲染服务的视口宽度无关。
        无损模式万一被渲染服务拒绝，自动用高质量 JPEG 再试一次。
        """
        lossless = bool(self._settings.card_lossless)
        try:
            return await self._star.html_render(
                self._load_template(),
                data,
                return_url=False,
                options=self._screenshot_options(lossless=lossless),
            )
        except Exception as exc:
            if not lossless:
                raise
            logger.debug("%s PNG 卡片渲染失败，改用 JPEG 再试一次：%s", LOG_PREFIX, exc)
            return await self._star.html_render(
                self._load_template(),
                data,
                return_url=False,
                options=self._screenshot_options(lossless=False),
            )

    def _note_failure(self, reason: str) -> None:
        """记一次渲染失败；连续失败到阈值就熄火一段时间，别每次上报都白等超时。"""
        self._failures += 1
        logger.warning(
            "%s 卡片%s（第 %s 次），本次改用纯文本。",
            LOG_PREFIX,
            reason,
            self._failures,
        )
        if self._failures >= FAILURE_THRESHOLD:
            self._muted_until = time.time() + COOLDOWN_AFTER_FAILURE
            self._failures = 0
            logger.warning(
                "%s 卡片渲染连续失败，暂停 %s 秒后再尝试。",
                LOG_PREFIX,
                COOLDOWN_AFTER_FAILURE,
            )

    async def render(
        self,
        *,
        kind: str,
        title: str,
        subtitle: str = "",
        icon: str = "",
        avatar: str = "",
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
            "icon": _plain(icon, 2) or _initial(clean_title),
            "avatar": clean_text(avatar),
            "title": clean_title,
            "subtitle": _plain(subtitle, 90),
            "tag": _plain(tag, 16),
            "note": _plain(note, 600),
            "messages": messages or [],
            "footer": _plain(footer, 90) or f"由「{PLUGIN_DISPLAY_NAME}」生成",
            "generated_at": now_text(),
            "empty_hint": _plain(empty_hint, 60),
            "show_avatar": bool(self._settings.card_show_avatar),
            # 版式宽度 + 清晰度倍数：模板用它们算出图有多大，见模板顶部注释。
            "page_width": PAGE_WIDTH,
            "scale": str(self._settings.card_scale),
        }

        try:
            raw = await self._shoot(data)
        except Exception as exc:
            self._note_failure(f"渲染失败：{exc}")
            return None

        path = _checked_image_path(clean_text(raw))
        if path is None:
            self._note_failure("渲染服务没有返回有效图片")
            return None
        self._failures = 0
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
            avatar=getattr(chatlog, "group_avatar", ""),
            tag=tag,
            note=note,
            messages=self.build_messages(
                chatlog.rows(),
                limit=self._settings.card_max_messages,
            ),
            footer=footer,
            empty_hint=empty_hint,
        )
