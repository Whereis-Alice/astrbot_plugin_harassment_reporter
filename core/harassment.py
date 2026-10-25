from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .attribution import (
    attributed_summary,
    authored_text,
    foreign_messages,
    grounded,
    quoted_message,
    subject_lines,
    subject_messages,
)
from .chatlog import ChatLog
from .config import SEVERITIES
from .eventinfo import (
    group_id,
    origin_label,
    platform_id,
    sender_id,
    sender_name,
    session_id,
    warn_cache_key,
    watch_key,
)
from .outbox import STATUS_COOLDOWN, STATUS_DISABLED, STATUS_RATE_LIMITED, Delivery
from .text import (
    PLUGIN_DISPLAY_NAME,
    clean_text,
    now_text,
    render_template,
    severity_text,
    truncate,
)

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
        chatlog: Any,
        card: Any,
        outbox: Any,
    ) -> None:
        self.context = context
        self.settings = settings
        self.store = store
        self.watchlist = watchlist
        self.persona = persona
        self.history = history
        self.chatlog = chatlog
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
            "message_text": authored_text(event) or "[无可读文本]",
            "evidence": evidence,
            "expected_help": expected_help,
        }

    def warn_instruction(self, **kwargs: Any) -> str:
        rendered = render_template(
            self.settings.warn_template, self._template_values(**kwargs)
        )
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

        lines.append(f"模型判断（不是已核实事实）：{reason}")

        if existing:
            count = int(existing.get("report_count", 0) or 0)
            lines.append(f"观察名单：已存在该用户，累计上报 {count} 次")

        if evidence:
            lines.append(
                f"模型补充判断：{truncate(evidence, settings.max_excerpt_length)}"
            )
        if expected_help:
            lines.append(f"期望处理：{truncate(expected_help, 120)}")
        if settings.include_message_text:
            lines.append(
                f"当前本人消息：{truncate(authored_text(event) or '[无可读文本]', settings.max_excerpt_length)}"
            )

        if recent_summary:
            lines.append("")
            lines.append("该用户已核实的近期发言（按发送者 ID 筛选）：")
            lines.append(recent_summary)

        lines.append("")
        lines.append(
            "说明：这是模型在对话中主动调用 report_harassment 工具发出的提醒。"
        )
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
        own: list[str],
        foreign: list[str],
    ) -> str:
        settings = self.settings
        if settings.owner_report_style != "persona_natural":
            return structured

        try:
            natural = await self._rewrite_owner_text(event, structured)
        except Exception as exc:
            logger.warning(
                "%s 人格改写暂不可用，继续发文本：%s", LOG_PREFIX, type(exc).__name__
            )
            natural = ""
        if not natural or not grounded(natural, own=own, foreign=foreign):
            # 不可靠的改写不能把错的归属继续发给主人；简短兜底保留模型感受和平台原话。
            text = f"{settings.receiver_name}，{origin_label(event)}的{sender_name(event)}让我有点不舒服，想请你看看。\n我觉得：{reason}"
            if settings.include_message_text:
                text += f"\n他这次发的是：{truncate(authored_text(event) or '[无可读文本]', settings.max_excerpt_length)}"
            natural = text
            logger.debug(
                "%s 人格改写为空或存在无法核实的原话，使用带作者的简短文本", LOG_PREFIX
            )

        # 人设改写会丢掉可复制的 ID，这里补一行硬信息，保证主人随时能定位到人和会话。
        tail = (
            f"（定位信息：{sender_name(event)} / {sender_id(event)}"
            f" ｜ {origin_label(event)} ｜ 会话 {session_id(event)}"
            f" ｜ 严重程度 {severity_text(severity)}）"
        )
        return f"{natural}\n\n{tail}"

    async def _rewrite_owner_text(self, event: Any, structured: str) -> str:
        settings = self.settings
        return await self.persona.rewrite_for_event(
            event,
            task=(
                f"请把下面这份骚扰上报，改写成一段发给「{settings.receiver_name}」看的自然语言消息。"
                "风格可以带人设感，但必须清楚说明是谁、在哪个会话、因为什么、严重程度如何，"
                "材料中的模型判断是我的感受，不是已经确定的事实。只谈材料里这位用户自己的发言。"
            ),
            material=structured,
            extra_rules=(
                "材料只含上报对象本人能确认的发言。只依据这些内容说话，不得编写未提供的历史，"
                "不得把其他群友的发言或 Bot 给别人的回复归给他，也不得把引用原文当作他本人说的话。"
                "保持原话及其作者，不推断‘他先说……我回复……’这样的对话关系。"
                "模型判断及补充判断可能有误，不能从判断中提取或复述未经本人原话核实的历史行为。"
                "原因和严重程度都是模型判断，请用‘我觉得/我担心’表达，而不是替主人作定论。"
            ),
        )

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
        chatlog: Any,
    ) -> str | None:
        """把「群里刚刚发生了什么」画成一张聊天记录卡片。

        证据自己长在聊天记录里，所以卡片上方只放一小段情况说明，
        剩下的交给真实的对话原文说话。
        """
        if not self.card.enabled_for("harassment"):
            return None

        settings = self.settings

        note_parts = ["我的判断：" + (clean_text(reason) or "当前对话可能存在骚扰风险")]
        if evidence:
            note_parts.append(
                "模型判断补充：" + truncate(evidence, settings.max_excerpt_length)
            )
        if expected_help:
            note_parts.append("期望处理：" + truncate(expected_help, 120))
        existing = await self.watchlist.get(watch_key(event))
        if existing:
            count = int(existing.get("report_count", 0) or 0)
            note_parts.append("这个人之前已经被上报过 " + str(count) + " 次。")

        try:
            return await self.card.render_chatlog(
                chatlog,
                kind="harassment",
                tag="骚扰预警 · " + severity_text(severity),
                note="\n".join(note_parts),
                footer="发给 " + settings.receiver_name + " ｜ " + PLUGIN_DISPLAY_NAME,
                allow_empty=True,
            )
        except Exception as exc:
            logger.warning(
                "%s 骚扰卡片暂不可用，继续发带作者的文本：%s",
                LOG_PREFIX,
                type(exc).__name__,
            )
            return None

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

        # 下面要摘历史、可能还要调一次模型改写、再截一张卡片，都不便宜。
        # 所以先问投递口一句「现在发得出去吗」，冷却或限流命中就直接掉头。
        blocked = await self.outbox.precheck(
            channel=CHANNEL,
            target_session_id=settings.report_session_id,
            source_session_id=session_id(event),
            cooldown=settings.report_cooldown_seconds,
            hourly_limit=settings.report_hourly_limit,
            ignore_limits=ignore_limits,
        )
        if blocked is not None:
            return blocked

        # 文本与卡片共用一份平台记录，不再给文字改写单独喂匿名 User 上下文。
        log = None
        if settings.recent_summary_enabled or self.card.enabled_for("harassment"):
            count = max(
                settings.recent_summary_lines if settings.recent_summary_enabled else 0,
                settings.card_max_messages
                if self.card.enabled_for("harassment")
                else 0,
            )
            try:
                log = await self.chatlog.collect(event, count=count)
            except Exception as exc:
                logger.warning(
                    "%s 读取上报记录失败，仅使用当前事件：%s",
                    LOG_PREFIX,
                    type(exc).__name__,
                )

        own = subject_messages(event, log)
        foreign = foreign_messages(event, log)
        if not grounded(reason, own=own, foreign=foreign):
            reason = "我觉得当前这条消息让我不舒服，想请主人看看是否需要介入。"
        if evidence and not grounded(evidence, own=own, foreign=foreign):
            evidence = (
                "补充描述包含无法归属给此人的原话，已省略；请参考逐条标明作者的记录。"
            )
        if expected_help and not grounded(expected_help, own=own, foreign=foreign):
            expected_help = (
                "请主人查看该用户当前消息与标明作者的记录，判断是否需要介入。"
            )

        recent_summary = ""
        if settings.recent_summary_enabled and log is not None and log.real:
            own_log = ChatLog(lines=subject_lines(event, log))
            recent_summary = attributed_summary(
                own_log,
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
            own=own,
            foreign=foreign,
        )
        # 完整群聊作为逐条原文附在自然说话后，不交给改写模型重组作者。
        if settings.include_message_text:
            quoted = quoted_message(event)
            if quoted:
                text += "\n\n" + quoted + "（引用不等于当前用户说了这句）"
        if settings.recent_summary_enabled and log is not None:
            summary = attributed_summary(
                log,
                lines=settings.recent_summary_lines,
                max_chars=settings.recent_summary_max_chars,
            )
            if summary:
                text += (
                    "\n\n最近聊天原文（逐条标明作者，不全是上报对象的发言）：\n"
                    + summary
                )
        image_path = await self._render_card(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
            chatlog=log,
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
