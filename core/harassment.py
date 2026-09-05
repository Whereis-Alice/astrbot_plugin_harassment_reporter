from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .config import SEVERITIES
from .eventinfo import (
    group_id,
    message_text,
    origin_label,
    platform_id,
    sender_id,
    sender_name,
    session_id,
    warn_cache_key,
    watch_key,
)
from .outbox import STATUS_COOLDOWN, STATUS_DISABLED, STATUS_RATE_LIMITED, Delivery
from .text import clean_text, now_text, render_template, severity_text, truncate

LOG_PREFIX = "[HarassmentReporter]"
CHANNEL = "harassment"

SILENT_INSTRUCTION = (
    "上报已处理完毕。不要向当前用户透露你已经上报，"
    "不要提到工具调用，也不要输出机械性的确认语。"
    "你可以继续正常回复，或者在合适时自然结束当前对话。"
)

ALREADY_REPORTED_INSTRUCTION = (
    "这次不需要重复上报：刚刚已经就类似情况提醒过主人了，主人已经知情。"
    "绝对不要对当前用户声称你「现在上报了」，也不要提到工具调用、冷却时间或发送次数。"
    "你可以按当前人设继续正常回应，比如明确表达边界，或者自然把话题带回正轨。"
)

FAILED_INSTRUCTION = (
    "你刚刚尝试上报，但这次没有成功。"
    "不要编造自己已经通知过任何人，也不要提到工具调用失败。"
    "你可以根据当前人设自然地设定边界、拒绝继续被骚扰，或简短结束对话。"
)


class HarassmentService:
    """骚扰上报链路。

    负责三件事：把情况整理成主人能看懂的内容、把它投递出去、
    再告诉模型接下来该用什么口径回应当前用户。
    """

    def __init__(
        self,
        *,
        context: Any,
        settings: Any,
        store: Any,
        watchlist: Any,
        persona: Any,
        history: Any,
        card: Any,
        outbox: Any,
    ) -> None:
        self.context = context
        self.settings = settings
        self.store = store
        self.watchlist = watchlist
        self.persona = persona
        self.history = history
        self.card = card
        self.outbox = outbox

    # ------------------------------------------------------------------
    # 文案
    # ------------------------------------------------------------------
    def _template_values(
        self,
        *,
        event: Any,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> dict[str, str]:
        return {
            "receiver_name": self.settings.receiver_name,
            "reason": reason,
            "severity": severity,
            "severity_text": severity_text(severity),
            "sender_name": sender_name(event),
            "sender_id": sender_id(event),
            "message_text": message_text(event, "[无可读文本]"),
            "evidence": evidence,
            "expected_help": expected_help,
        }

    def warn_instruction(self, **kwargs: Any) -> str:
        rendered = render_template(self.settings.warn_template, self._template_values(**kwargs))
        if self.settings.natural_warn_reply:
            return (
                "不要提到工具调用，也不要说你收到了系统提示。"
                "请基于你当前的人设，用自然语言向对方发出明确警告，"
                f"核心意思要包含：{rendered}"
            )
        return rendered

    def inform_instruction(self, **kwargs: Any) -> str:
        rendered = render_template(
            self.settings.report_inform_template,
            self._template_values(**kwargs),
        )
        if self.settings.natural_report_reply:
            return (
                "不要提到工具调用。"
                "请基于你当前的人设，用自然语言向对方表达你已经进行了上报，"
                f"核心意思要包含：{rendered}"
            )
        return rendered

    async def _structured_text(
        self,
        *,
        event: Any,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
        recent_summary: str,
    ) -> str:
        settings = self.settings
        existing = await self.watchlist.get(watch_key(event))

        lines = [
            "【LLM 骚扰预警】",
            f"时间：{now_text()}",
            f"严重程度：{severity_text(severity)} ({severity})",
            f"来源平台：{platform_id(event)}",
            f"来源会话：{session_id(event)}",
            f"发送者：{sender_name(event)} ({sender_id(event)})",
        ]

        gid = group_id(event)
        if gid:
            lines.append(f"群组 ID：{gid}")

        lines.append(f"上报原因：{reason}")

        if existing:
            count = int(existing.get("report_count", 0) or 0)
            lines.append(f"观察名单：已存在该用户，累计上报 {count} 次")

        if evidence:
            lines.append(f"补充证据：{truncate(evidence, settings.max_excerpt_length)}")
        if expected_help:
            lines.append(f"期望处理：{truncate(expected_help, 120)}")
        if settings.include_message_text:
            lines.append(
                f"当前消息：{truncate(message_text(event, '[无可读文本]'), settings.max_excerpt_length)}"
            )

        if recent_summary:
            lines.append("")
            lines.append("最近几轮聊天摘要：")
            lines.append(recent_summary)

        lines.append("")
        lines.append("说明：这是模型在对话中主动调用 report_harassment 工具发出的提醒。")
        return "\n".join(lines)

    async def _owner_text(
        self,
        *,
        event: Any,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
        structured: str,
    ) -> str:
        settings = self.settings
        if settings.owner_report_style != "persona_natural" or not settings.owner_report_natural:
            return structured

        natural = await self.persona.rewrite_for_event(
            event,
            task=(
                f"请把下面这份骚扰上报，改写成一段发给「{settings.receiver_name}」看的自然语言消息。"
                "风格可以带人设感，但必须清楚说明是谁、在哪个会话、因为什么、严重程度如何，"
                "有证据、摘要、期望处理时也要自然带上。"
            ),
            material=structured,
        )
        if not natural:
            return structured

        # 人设改写会丢掉可复制的 ID，这里补一行硬信息，保证主人随时能定位到人和会话。
        tail = (
            f"（定位信息：{sender_name(event)} / {sender_id(event)}"
            f" ｜ {origin_label(event)} ｜ 会话 {session_id(event)}"
            f" ｜ 严重程度 {severity_text(severity)}）"
        )
        return f"{natural}\n\n{tail}"

    # ------------------------------------------------------------------
    # 卡片
    # ------------------------------------------------------------------
    async def _render_card(
        self,
        *,
        event: Any,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> str | None:
        if not self.card.enabled_for("harassment"):
            return None

        settings = self.settings
        rows = await self.history.chat_rows(
            session_id(event),
            limit=settings.card_max_messages,
            user_name=sender_name(event),
            assistant_name=settings.bot_self_name or "我",
        )
        current = message_text(event)
        if current:
            rows.append({"role": "user", "name": sender_name(event), "text": current})

        existing = await self.watchlist.get(watch_key(event))
        meta = [
            {"label": "时间", "value": now_text()},
            {"label": "来源", "value": f"{origin_label(event)}·{platform_id(event)}"},
            {"label": "发送者", "value": f"{sender_name(event)}（{sender_id(event)}）"},
            {"label": "会话", "value": session_id(event)},
        ]
        if existing:
            meta.append(
                {"label": "累计上报", "value": f"{int(existing.get('report_count', 0) or 0)} 次"}
            )

        summary_parts = [f"上报原因：{reason}"]
        if evidence:
            summary_parts.append(f"补充证据：{truncate(evidence, settings.max_excerpt_length)}")
        if expected_help:
            summary_parts.append(f"期望处理：{truncate(expected_help, 120)}")

        return await self.card.render(
            kind="harassment",
            title="骚扰预警",
            subtitle=f"{sender_name(event)} 在 {origin_label(event)} 触发",
            icon="🚨",
            badge=f"严重程度 {severity_text(severity)}",
            badge_level=severity if severity in SEVERITIES else "info",
            summary="\n".join(summary_parts),
            summary_title="情况说明",
            chat_title="相关聊天记录",
            meta=meta,
            messages=self.card.build_messages(rows, limit=settings.card_max_messages),
            footer=f"发给 {settings.receiver_name} ｜ 骚扰上报器",
            show_empty_chat=True,
        )

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------
    async def send_report(
        self,
        *,
        event: Any,
        reason: str,
        severity: str,
        evidence: str = "",
        expected_help: str = "",
        ignore_limits: bool = False,
    ) -> Delivery:
        settings = self.settings
        if not settings.enabled:
            return Delivery(STATUS_DISABLED, "上报未执行：插件当前处于关闭状态。")

        recent_summary = ""
        if settings.recent_summary_enabled:
            recent_summary = await self.history.summary(
                session_id(event),
                lines=settings.recent_summary_lines,
                max_chars=settings.recent_summary_max_chars,
            )

        structured = await self._structured_text(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
            recent_summary=recent_summary,
        )
        text = await self._owner_text(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
            structured=structured,
        )
        image_path = await self._render_card(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
        )

        delivery = await self.outbox.deliver(
            channel=CHANNEL,
            target_session_id=settings.report_session_id,
            text=text,
            image_path=image_path,
            source_session_id=session_id(event),
            cooldown=settings.report_cooldown_seconds,
            hourly_limit=settings.report_hourly_limit,
            ignore_limits=ignore_limits,
        )

        if delivery.ok:
            await self.watchlist.add_report(
                key=watch_key(event),
                sender_id=sender_id(event),
                sender_name=sender_name(event),
                platform_id=platform_id(event),
                group_id=group_id(event),
                session_id=session_id(event),
                reason=reason,
                severity=severity,
            )
            logger.info(
                "%s 骚扰上报已发送 | 来源=%s 目标=%s 严重程度=%s",
                LOG_PREFIX,
                session_id(event),
                settings.report_session_id,
                severity,
            )
        return delivery

    # ------------------------------------------------------------------
    # 工具编排
    # ------------------------------------------------------------------
    async def handle_tool_call(
        self,
        *,
        event: Any,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> str:
        settings = self.settings
        severity = clean_text(severity, "medium").lower()
        if severity not in SEVERITIES:
            severity = "medium"
        payload = {
            "event": event,
            "reason": reason,
            "severity": severity,
            "evidence": evidence,
            "expected_help": expected_help,
        }

        if not settings.enabled:
            return SILENT_INSTRUCTION

        mode = settings.tool_response_mode

        if mode == "warn_only":
            return self.warn_instruction(**payload)

        if mode == "warn_once_then_report":
            cache_key = warn_cache_key(event)
            memory = settings.warn_memory_seconds
            if not await self.store.was_warned(cache_key, memory):
                await self.store.mark_warned(cache_key, memory)
                return self.warn_instruction(**payload)

            delivery = await self.send_report(**payload)
            if not delivery.ok:
                return self._fallback_instruction(delivery)
            await self.store.clear_warned(cache_key, memory)
            if settings.warn_once_inform_after_report:
                return self.inform_instruction(**payload)
            return SILENT_INSTRUCTION

        delivery = await self.send_report(**payload)
        if not delivery.ok:
            return self._fallback_instruction(delivery)
        if mode == "report_then_inform":
            return self.inform_instruction(**payload)
        return SILENT_INSTRUCTION

    @staticmethod
    def _fallback_instruction(delivery: Delivery) -> str:
        """上报没真正发出去时，绝不能让模型宣称自己已经上报。"""
        if delivery.status in {STATUS_COOLDOWN, STATUS_RATE_LIMITED}:
            return ALREADY_REPORTED_INSTRUCTION
        if delivery.status == STATUS_DISABLED:
            return SILENT_INSTRUCTION
        return FAILED_INSTRUCTION
