from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .text import clean_text, normalize_context_line, pack_lines, split_context_line

LOG_PREFIX = "[HarassmentReporter]"


def to_chronological(lines: list[str]) -> list[str]:
    """把 AstrBot 的可读上下文还原成时间正序。

    AstrBot 返回的列表是「最新一轮排在最前面」，每一轮以助手发言结尾。
    这里按轮切分再整体反转，得到从旧到新的顺序，方便直接拼摘要或画卡片。
    """
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        role, _content = split_context_line(line)
        if role == "assistant":
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    groups.reverse()
    return [line for group in groups for line in group]


class HistoryReader:
    """读取当前会话的对话历史，供上报摘要和聊天卡片复用。"""

    def __init__(self, context: Any) -> None:
        self.context = context

    async def read_lines(self, umo: str, limit: int) -> list[str]:
        """取最近 limit 行上下文（时间正序）。取不到时返回空列表。"""
        umo = clean_text(umo)
        if not umo or limit <= 0:
            return []
        try:
            manager = self.context.conversation_manager
            conversation_id = await manager.get_curr_conversation_id(umo)
            if not conversation_id:
                return []
            raw, _pages = await manager.get_human_readable_context(
                umo,
                conversation_id,
                page=1,
                page_size=max(1, limit),
            )
        except Exception as exc:
            logger.debug("%s 读取会话历史失败：%s", LOG_PREFIX, exc)
            return []

        lines = [clean_text(item) for item in (raw or []) if clean_text(item)]
        return to_chronological(lines)

    async def summary(self, umo: str, *, lines: int, max_chars: int) -> str:
        """生成「最近几轮聊天摘要」纯文本块。"""
        history = await self.read_lines(umo, lines)
        if not history:
            return ""
        return pack_lines(
            [normalize_context_line(item) for item in history],
            max_chars=max_chars,
        )

    async def chat_rows(
        self,
        umo: str,
        *,
        limit: int,
        user_name: str = "用户",
        assistant_name: str = "",
    ) -> list[dict[str, Any]]:
        """转成聊天卡片需要的气泡数据。"""
        history = await self.read_lines(umo, limit)
        rows: list[dict[str, Any]] = []
        for item in history:
            role, content = split_context_line(item)
            if not content:
                continue
            if role == "assistant":
                name = assistant_name or "我"
            elif role == "user":
                name = user_name or "用户"
            else:
                name = "系统"
            rows.append({"role": role, "name": name, "text": content})
        return rows
