"""最近聊天记录的收集器。

爱丽丝去找狐狸的时候会附一张卡片，卡片上画的应该是「群里刚刚发生了什么」，
而不是她和某个用户之间的模型上下文。两者的区别很关键：

- OneBot 的 get_group_msg_history 拿到的是群里所有人的真实发言，
  每条都有独立的昵称、QQ 号和时间戳，看起来就像一段聊天截图；
- AstrBot 的会话上下文是喂给模型的原始材料，群聊里多人共享同一个会话，
  所有人的发言都被标成 "User:"，还混着系统提示和图片本地路径。

所以这里优先走 OneBot 接口，只在拿不到时才退回会话上下文（并做清洗兜底）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .eventinfo import group_id, message_text, platform_id, self_id, sender_id, sender_name
from .onebot import ChatLine, get_client, is_onebot_event, qq_group_avatar, qq_user_avatar
from .text import clean_text, relative_time, truncate

LOG_PREFIX = "[HarassmentReporter]"

# 单条消息在卡片上的最大长度。超过这个长度的发言在聊天记录里本来就看不清，
# 截断反而更接近真实的聊天截图观感。
LINE_TEXT_LIMIT = 220

SOURCE_GROUP = "group_history"
SOURCE_PRIVATE = "private_history"
SOURCE_CONTEXT = "conversation"
SOURCE_CURRENT = "current_only"
SOURCE_EMPTY = "empty"


@dataclass
class ChatLog:
    """一段可以直接拿去画卡片的聊天记录。"""

    lines: list[ChatLine] = field(default_factory=list)
    title: str = ""
    subtitle: str = ""
    group_id: str = ""
    group_name: str = ""
    group_avatar: str = ""
    source: str = SOURCE_EMPTY

    @property
    def empty(self) -> bool:
        return not self.lines

    @property
    def real(self) -> bool:
        """是不是真的从平台接口拉到的聊天记录。"""
        return self.source in {SOURCE_GROUP, SOURCE_PRIVATE}

    def rows(self) -> list[dict[str, Any]]:
        return [line.as_dict() for line in self.lines]

    def as_text(self, *, max_lines: int = 8, line_limit: int = 80) -> str:
        """卡片渲染失败时的纯文本兜底，让狐狸至少还能看到几句原文。"""
        if not self.lines:
            return ""
        picked = self.lines[-max(1, max_lines) :]
        return "\n".join(
            f"{truncate(line.sender_name, 12)}：{truncate(line.text, line_limit)}" for line in picked
        )


class ChatLogCollector:
    """按「平台接口优先」的顺序收集最近聊天记录。"""

    def __init__(self, *, settings: Any, bridge: Any, history: Any) -> None:
        self.settings = settings
        self.bridge = bridge
        self.history = history
        # 群名要额外发一次请求，同一个群没必要每次上报都问一遍。
        self._group_names: dict[str, str] = {}

    async def group_name(self, event: Any, gid: str = "") -> str:
        """取群名称，取不到就返回空串。结果按平台实例缓存。"""
        gid = clean_text(gid) or group_id(event)
        if not gid or not is_onebot_event(event):
            return ""
        cache_key = f"{platform_id(event)}:{gid}"
        if cache_key in self._group_names:
            return self._group_names[cache_key]
        name = ""
        try:
            name = await self.bridge.fetch_group_name(get_client(event), gid)
        except Exception:
            name = ""
        self._group_names[cache_key] = name
        return name

    async def collect(
        self,
        event: Any,
        *,
        count: int,
        include_current: bool = True,
    ) -> ChatLog:
        """收集最近 count 条聊天记录。任何环节失败都只是内容变少，不会抛异常。"""
        gid = group_id(event)
        name = await self.group_name(event, gid)
        log = ChatLog(group_id=gid, group_name=name)

        if count > 0:
            log.lines, log.source = await self._fetch(event, gid, count)

        if include_current:
            self._append_current(event, log)

        self._attach_avatars(event, log)
        self._label(event, log)
        return log

    # ------------------------------------------------------------------
    # 数据源
    # ------------------------------------------------------------------
    async def _fetch(self, event: Any, gid: str, count: int) -> tuple[list[ChatLine], str]:
        if is_onebot_event(event):
            client = get_client(event)
            if client is not None:
                try:
                    if gid:
                        lines = await self.bridge.fetch_group_history(
                            client,
                            gid,
                            count=count,
                            self_id=self_id(event),
                            text_limit=LINE_TEXT_LIMIT,
                        )
                        if lines:
                            return lines, SOURCE_GROUP
                    else:
                        lines = await self.bridge.fetch_private_history(
                            client,
                            sender_id(event),
                            count=count,
                            self_id=self_id(event),
                            text_limit=LINE_TEXT_LIMIT,
                        )
                        if lines:
                            return lines, SOURCE_PRIVATE
                except Exception:
                    pass

        lines = await self._from_context(event, count)
        return lines, SOURCE_CONTEXT if lines else SOURCE_EMPTY

    async def _from_context(self, event: Any, count: int) -> list[ChatLine]:
        """退路：把当前会话的模型上下文当成聊天记录。

        上下文里分不出群里的具体是谁在说话，只能标成「发送者」和 Bot 自己，
        所以这只是兜底，观感一定不如真实群聊记录。
        """
        try:
            rows = await self.history.chat_rows(
                clean_text(getattr(event, "unified_msg_origin", "")),
                limit=count,
                user_name=sender_name(event),
                assistant_name=self.settings.bot_self_name or "我",
            )
        except Exception:
            return []

        me = self_id(event)
        who = sender_id(event)
        lines: list[ChatLine] = []
        for row in rows:
            text = clean_text(row.get("text"))
            if not text:
                continue
            is_bot = clean_text(row.get("role")) == "assistant"
            lines.append(
                ChatLine(
                    sender_name=clean_text(row.get("name"), "未知用户"),
                    sender_id=me if is_bot else who,
                    text=truncate(text, LINE_TEXT_LIMIT),
                    timestamp=0.0,
                    is_self=is_bot,
                )
            )
        return lines

    @staticmethod
    def _append_current(event: Any, log: ChatLog) -> None:
        """把用户此刻这句话补进去。

        群历史接口通常已经包含它了，所以先查重；同一个人说了同样的话就不重复添加。
        """
        current = clean_text(message_text(event))
        if not current:
            return
        who = sender_id(event)
        for line in log.lines[-3:]:
            if clean_text(line.text) == truncate(current, LINE_TEXT_LIMIT) or (
                line.sender_id == who and clean_text(line.text) == current
            ):
                return
        log.lines.append(
            ChatLine(
                sender_name=sender_name(event),
                sender_id=who,
                text=truncate(current, LINE_TEXT_LIMIT),
                timestamp=time.time(),
                is_self=False,
            )
        )
        if log.source == SOURCE_EMPTY:
            log.source = SOURCE_CURRENT

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 头像
    # ------------------------------------------------------------------
    def _attach_avatars(self, event: Any, log: ChatLog) -> None:
        """给每条发言和卡片头部挂上真实 QQ 头像地址。

        只在 OneBot 平台上做，因为只有 QQ 有公开的头像地址可以直接拼。
        拼不出来的（非数字 ID、其它平台）留空，卡片会自动画彩色首字块。
        """
        if not self.settings.card_use_real_avatar or not is_onebot_event(event):
            return
        for line in log.lines:
            if not line.avatar:
                line.avatar = qq_user_avatar(line.sender_id)
        log.group_avatar = (
            qq_group_avatar(log.group_id) if log.group_id else qq_user_avatar(sender_id(event))
        )

    # 卡片头部文案
    # ------------------------------------------------------------------
    @staticmethod
    def _label(event: Any, log: ChatLog) -> None:
        if log.group_id:
            log.title = log.group_name or f"群 {log.group_id}"
        else:
            log.title = f"和 {sender_name(event)} 的私聊"

        parts: list[str] = []
        if log.group_id:
            parts.append(f"群号 {log.group_id}")
        latest = max((line.timestamp for line in log.lines), default=0.0)
        moment = relative_time(latest)
        if moment:
            parts.append(moment)
        log.subtitle = " · ".join(parts)
