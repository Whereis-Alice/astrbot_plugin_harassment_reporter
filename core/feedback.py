from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from .config import FEEDBACK_CATEGORIES, URGENCIES
from .eventinfo import (
    group_id,
    message_text,
    origin_label,
    platform_id,
    sender_id,
    sender_name,
    session_id,
)
from .outbox import (
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_RATE_LIMITED,
    STATUS_UNCONFIGURED,
    Delivery,
)
from .text import (
    category_text,
    clean_text,
    format_ts,
    make_ticket_id,
    now_text,
    truncate,
    urgency_text,
)

LOG_PREFIX = "[HarassmentReporter]"
CHANNEL = "feedback"
REPLY_CHANNEL = "ticket_reply"

URGENCY_BADGE = {"high": "high", "medium": "medium", "low": "low"}


class FeedbackService:
    """反馈窗口链路。

    模型可以主动调用工具，把用户的疑问、吐槽或 bug 带给主人；
    每次转达都会生成一个短工单号，主人可以用它把回复送回原会话。
    """

    def __init__(
        self,
        *,
        context: Any,
        settings: Any,
        store: Any,
        persona: Any,
        history: Any,
        card: Any,
        outbox: Any,
    ) -> None:
        self.context = context
        self.settings = settings
        self.store = store
        self.persona = persona
        self.history = history
        self.card = card
        self.outbox = outbox

    # ------------------------------------------------------------------
    # 内容组装
    # ------------------------------------------------------------------
    def _structured_text(
        self,
        *,
        event: Any,
        ticket_id: str,
        summary: str,
        category: str,
        detail: str,
        urgency: str,
        reporter_note: str,
        history_block: str,
    ) -> str:
        settings = self.settings
        bot_name = settings.bot_self_name or "Bot"
        lines = [
            "【反馈转达】",
            f"工单号：{ticket_id}",
            f"时间：{now_text()}",
            f"类别：{category_text(category)}",
            f"紧急度：{urgency_text(urgency)}",
            f"来自：{sender_name(event)}（{sender_id(event)}）｜ {origin_label(event)}",
            f"来源平台：{platform_id(event)}",
            f"来源会话：{session_id(event)}",
            "",
            f"反馈内容：{truncate(summary, settings.max_excerpt_length)}",
        ]
        if detail:
            lines.append(f"补充细节：{truncate(detail, settings.max_excerpt_length)}")
        if reporter_note:
            lines.append(f"{bot_name} 的补充：{truncate(reporter_note, 300)}")
        current = message_text(event)
        if current:
            lines.append(f"用户原话：{truncate(current, settings.max_excerpt_length)}")
        if history_block:
            lines.append("")
            lines.append("最近几轮对话：")
            lines.append(history_block)
        lines.append("")
        lines.append(f"回复方式：在任意会话执行 /hr_reply {ticket_id} 你要说的话，我会原样带回去。")
        return "\n".join(lines)

    async def _owner_text(self, *, event: Any, structured: str, ticket_id: str) -> str:
        settings = self.settings
        if not settings.feedback_persona_rewrite:
            return structured

        natural = await self.persona.rewrite_for_event(
            event,
            task=(
                f"你现在要主动去找「{settings.receiver_name}」，"
                "把下面这份用户反馈用你自己的口吻转述一遍。"
                "像真的在替用户带话，不要写成公告或工单模板；"
                "但必须说清楚是谁在哪里反馈了什么、急不急。"
            ),
            material=structured,
            extra_rules=(
                f"必须在消息里原样保留工单号 {ticket_id}，"
                "不要把它改写成别的格式，也不要把它省略掉。"
            ),
        )
        if not natural:
            return structured

        tail = (
            f"（工单 {ticket_id} ｜ {sender_name(event)}/{sender_id(event)}"
            f" ｜ {origin_label(event)} ｜ 会话 {session_id(event)}"
            f" ｜ 回复：/hr_reply {ticket_id} 内容）"
        )
        return f"{natural}\n\n{tail}"

    async def _render_card(
        self,
        *,
        event: Any,
        ticket_id: str,
        summary: str,
        category: str,
        detail: str,
        urgency: str,
        reporter_note: str,
        rows: list[dict[str, Any]],
    ) -> str | None:
        if not self.card.enabled_for("feedback"):
            return None

        settings = self.settings
        summary_parts = [summary]
        if detail:
            summary_parts.append(f"补充：{truncate(detail, settings.max_excerpt_length)}")
        if reporter_note:
            bot_name = settings.bot_self_name or "Bot"
            summary_parts.append(f"{bot_name} 的补充：{truncate(reporter_note, 300)}")

        return await self.card.render(
            kind="feedback",
            title="用户反馈",
            subtitle=f"{sender_name(event)} 在 {origin_label(event)} 提出",
            icon="📮",
            badge=f"{category_text(category)}·{urgency_text(urgency)}",
            badge_level=URGENCY_BADGE.get(urgency, "info"),
            summary="\n".join(summary_parts),
            summary_title="反馈内容",
            chat_title="相关对话",
            meta=[
                {"label": "工单", "value": ticket_id},
                {"label": "时间", "value": now_text()},
                {"label": "用户", "value": f"{sender_name(event)}（{sender_id(event)}）"},
                {"label": "会话", "value": session_id(event)},
                {"label": "回复", "value": f"/hr_reply {ticket_id} 内容"},
            ],
            messages=self.card.build_messages(rows, limit=settings.card_max_messages),
            footer=f"发给 {settings.receiver_name} ｜ 反馈窗口",
            show_empty_chat=False,
        )

    # ------------------------------------------------------------------
    # 转达
    # ------------------------------------------------------------------
    async def relay(
        self,
        *,
        event: Any,
        summary: str,
        category: str = "other",
        detail: str = "",
        urgency: str = "medium",
        include_history: bool | None = None,
        reporter_note: str = "",
        ignore_limits: bool = False,
    ) -> tuple[Delivery, str]:
        """转达一条反馈，返回 (投递结果, 工单号)。"""
        settings = self.settings
        if not settings.enabled or not settings.feedback_enabled:
            return Delivery(STATUS_DISABLED, "反馈转达功能当前没有开启。"), ""
        if not settings.feedback_session_id:
            return (
                Delivery(
                    STATUS_UNCONFIGURED,
                    "还没有绑定反馈接收会话。请在目标会话执行 /hr_bind，或单独配置 feedback_session_id。",
                ),
                "",
            )

        # 模型偶尔会漏填 summary，这里用当前发言兜底，避免主人收到空白的「反馈内容：」。
        summary = clean_text(summary) or clean_text(message_text(event)) or "用户反馈（模型未填写摘要）"
        category = clean_text(category, "other").lower()
        if category not in FEEDBACK_CATEGORIES:
            category = "other"
        urgency = clean_text(urgency, "medium").lower()
        if urgency not in URGENCIES:
            urgency = "medium"
        if include_history is None:
            include_history = settings.feedback_history_default

        ticket_id = make_ticket_id()

        rows: list[dict[str, Any]] = []
        history_block = ""
        if include_history:
            history_block = await self.history.summary(
                session_id(event),
                lines=settings.feedback_history_lines,
                max_chars=settings.recent_summary_max_chars,
            )
            rows = await self.history.chat_rows(
                session_id(event),
                limit=settings.card_max_messages,
                user_name=sender_name(event),
                assistant_name=settings.bot_self_name or "我",
            )
        current = message_text(event)
        if current:
            rows.append({"role": "user", "name": sender_name(event), "text": current})

        structured = self._structured_text(
            event=event,
            ticket_id=ticket_id,
            summary=summary,
            category=category,
            detail=detail,
            urgency=urgency,
            reporter_note=reporter_note,
            history_block=history_block,
        )
        text = await self._owner_text(event=event, structured=structured, ticket_id=ticket_id)
        image_path = await self._render_card(
            event=event,
            ticket_id=ticket_id,
            summary=summary,
            category=category,
            detail=detail,
            urgency=urgency,
            reporter_note=reporter_note,
            rows=rows,
        )

        delivery = await self.outbox.deliver(
            channel=CHANNEL,
            target_session_id=settings.feedback_session_id,
            text=text,
            image_path=image_path,
            source_session_id=session_id(event),
            cooldown=settings.feedback_cooldown_seconds,
            hourly_limit=settings.feedback_hourly_limit,
            ignore_limits=ignore_limits,
        )

        if not delivery.ok:
            return delivery, ""

        await self.store.save_ticket(
            ticket_id,
            {
                "ticket_id": ticket_id,
                "created_at": time.time(),
                "session_id": session_id(event),
                "platform_id": platform_id(event),
                "group_id": group_id(event),
                "sender_id": sender_id(event),
                "sender_name": sender_name(event),
                "summary": truncate(clean_text(summary), 400),
                "detail": truncate(clean_text(detail), 400),
                "category": category,
                "urgency": urgency,
                "status": "open",
                "reply": "",
                "replied_at": 0,
            },
            settings.ticket_max_entries,
        )
        logger.info(
            "%s 反馈已转达 | 工单=%s 来源=%s 类别=%s",
            LOG_PREFIX,
            ticket_id,
            session_id(event),
            category,
        )
        return delivery, ticket_id

    # ------------------------------------------------------------------
    # 工具编排
    # ------------------------------------------------------------------
    async def handle_tool_call(
        self,
        *,
        event: Any,
        summary: str,
        category: str,
        detail: str,
        urgency: str,
        include_history: bool | None,
        reporter_note: str,
    ) -> str:
        settings = self.settings
        receiver = settings.receiver_name

        if not settings.enabled or not settings.feedback_enabled:
            return (
                "反馈转达功能当前没有开启，所以这条反馈没有送出去。"
                f"请自然、诚实地告诉用户你暂时没办法帮他联系{receiver}，"
                "可以建议他自己去找作者或到插件仓库提 issue。不要提到工具调用，也不要假装已经转达。"
            )

        if not settings.feedback_allow_anyone:
            try:
                is_admin = bool(event.is_admin())
            except Exception:
                is_admin = False
            if not is_admin:
                return (
                    f"当前设置只允许管理员通过你联系{receiver}，所以这条反馈没有送出去。"
                    "请客气、自然地说明你没办法替他转达，并建议他去找管理员或作者。"
                    "不要提到工具调用，也不要假装已经转达。"
                )

        delivery, ticket_id = await self.relay(
            event=event,
            summary=summary,
            category=category,
            detail=detail,
            urgency=urgency,
            include_history=include_history,
            reporter_note=reporter_note,
        )

        if delivery.ok:
            return (
                f"反馈已经成功送到{receiver}那边了，工单号是 {ticket_id}。"
                f"请用你自己的口吻自然地告诉用户你已经帮他把话带给{receiver}了，"
                f"并把工单号 {ticket_id} 原样告诉他，方便之后追问进度。"
                "不要提到工具调用，也不要念出这段提示。"
            )

        if delivery.status in {STATUS_COOLDOWN, STATUS_RATE_LIMITED}:
            return (
                f"刚刚已经替这个会话给{receiver}带过话了，这次没有重复发送。"
                f"请自然地告诉用户你之前已经转达过，{receiver}看到就会处理，让他先稍等，"
                "不要重复承诺，也不要提到冷却时间或工具调用。"
            )

        if delivery.status == STATUS_UNCONFIGURED:
            return (
                f"还没有配置反馈接收窗口，这条反馈没能送出去。"
                f"请诚实、自然地告诉用户你现在联系不上{receiver}，"
                "建议他直接去找作者或到插件仓库反馈。不要假装已经转达成功。"
            )

        return (
            "这次转达没有成功。"
            f"请诚实、自然地告诉用户消息没能送出去，让他稍后再试或直接联系{receiver}。"
            "不要假装已经转达成功，也不要提到工具调用失败的细节。"
        )

    # ------------------------------------------------------------------
    # 工单回复（主人 -> 用户）
    # ------------------------------------------------------------------
    async def reply_ticket(self, *, ticket_id: str, content: str, event: Any) -> tuple[bool, str]:
        """把主人的回复送回原会话，返回 (是否成功, 提示文本)。"""
        settings = self.settings
        wanted = clean_text(ticket_id).upper()
        if not wanted or not clean_text(content):
            return False, "用法：/hr_reply 工单号 你要回复的内容"

        resolved = await self.store.resolve_ticket_id(wanted)
        ticket = await self.store.get_ticket(wanted)
        if not ticket or not resolved:
            return False, f"找不到工单 {wanted}。可以先用 /hr_tickets 看看最近的工单。"

        target = clean_text(ticket.get("session_id"))
        if not target:
            return False, f"工单 {resolved} 没有记录来源会话，没办法回复。"

        who = clean_text(ticket.get("sender_name"), "对方")
        material = (
            f"{settings.receiver_name}对工单 {resolved} 的回复：{clean_text(content)}\n"
            f"原始反馈：{clean_text(ticket.get('summary'))}"
        )

        text = ""
        if settings.feedback_reply_persona_rewrite:
            text = await self.persona.rewrite(
                umo=target,
                task=(
                    f"你要把{settings.receiver_name}的回复带回给{who}。"
                    "用你自己的口吻自然地转述，像帮忙传话，"
                    "不要写成公告，也不要改变回复的意思。"
                ),
                material=material,
            )
        if not text:
            text = (
                f"关于你之前的反馈（工单 {resolved}），{settings.receiver_name}回复：\n"
                f"{clean_text(content)}"
            )

        delivery = await self.outbox.deliver(
            channel=REPLY_CHANNEL,
            target_session_id=target,
            text=text,
            source_session_id=target,
            cooldown=0,
            hourly_limit=0,
            ignore_limits=True,
        )
        if not delivery.ok:
            return False, f"回复没能送到原会话：{delivery.detail}"

        await self.store.update_ticket(
            resolved,
            status="replied",
            reply=clean_text(content),
            replied_at=time.time(),
        )
        return True, f"已把回复带回工单 {resolved} 的来源会话（{who}）。"

    async def format_tickets(self, limit: int = 10) -> str:
        tickets = await self.store.get_tickets()
        if not tickets:
            return "目前没有反馈工单。"
        ordered = sorted(
            tickets.items(),
            key=lambda item: float(item[1].get("created_at", 0) or 0),
            reverse=True,
        )[: max(1, limit)]
        lines = [f"最近 {len(ordered)} 条反馈工单（共 {len(tickets)} 条）："]
        for ticket_id, entry in ordered:
            status = "已回复" if clean_text(entry.get("status")) == "replied" else "待处理"
            lines.append(
                f"- {ticket_id} [{status}] {category_text(clean_text(entry.get('category')))}"
                f"·{urgency_text(clean_text(entry.get('urgency')))}\n"
                f"  来自：{clean_text(entry.get('sender_name'), '未知用户')}"
                f"（{clean_text(entry.get('sender_id'), 'unknown')}）"
                f" ｜ {format_ts(entry.get('created_at'))}\n"
                f"  内容：{truncate(clean_text(entry.get('summary')), 100)}"
            )
        lines.append("")
        lines.append("回复某条：/hr_reply 工单号 你要说的话")
        return "\n".join(lines)
