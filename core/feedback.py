"""反馈窗口：让 Bot 能主动去找主人。

这条链路的定位很朴素 —— 让模型像真人一样「帮群友出去喊一声」，
顺手把群里刚刚发生了什么给主人看一眼。它不是工单系统，所以：

- 发给主人的正文就是模型自己写的那句话。工具调用本来就发生在模型的人格里，
  它写出来的话已经是它自己的口吻，插件再拿另一个模型改写一遍纯属多此一举；
- 附件是一张「最近群聊记录」卡片，内容由 ChatLogCollector 从平台接口拉真实群消息，
  看起来就像一张聊天截图；
- 没有工单号、没有类别和紧急度。只额外记一份「最近联系记录」，
  方便主人事后用 /hr_back 把话回过去。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from .eventinfo import (
    group_id,
    message_text,
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
from .text import clean_text, relative_time, truncate

LOG_PREFIX = "[HarassmentReporter]"
CHANNEL = "feedback"
REPLY_CHANNEL = "feedback_reply"

# 模型偶尔会把 message 漏成空串，这时至少让主人知道「有人找过你」。
FALLBACK_BODY = "有人找你，但我没能把话记全。"


class FeedbackService:
    """把用户的话带给主人，并支持主人把回话带回原会话。"""

    def __init__(
        self,
        *,
        context: Any,
        settings: Any,
        store: Any,
        persona: Any,
        chatlog: Any,
        card: Any,
        outbox: Any,
    ) -> None:
        self.context = context
        self.settings = settings
        self.store = store
        self.persona = persona
        self.chatlog = chatlog
        self.card = card
        self.outbox = outbox

    # ------------------------------------------------------------------
    # 文案零件
    # ------------------------------------------------------------------
    @staticmethod
    def _where(group_id_value: str, group_name: str) -> str:
        """一句话说清「这话是从哪儿传来的」。"""
        if not group_id_value:
            return "私聊"
        if group_name:
            return group_name + "（" + group_id_value + "）"
        return "群 " + group_id_value

    def _footnote(self, event: Any, group_name: str) -> str:
        """正文末尾那一行来源标注。

        主人光看模型写的话未必知道是谁、在哪儿说的，补一行最省事；
        觉得碍眼可以用 feedback_append_source 关掉。
        """
        who = sender_name(event)
        uid = sender_id(event)
        first = who + "（" + uid + "）" if uid else who
        return "—— " + first + " · " + self._where(group_id(event), group_name)

    # ------------------------------------------------------------------
    # 转达（用户 -> 主人）
    # ------------------------------------------------------------------
    async def relay(
        self,
        *,
        event: Any,
        message: str,
        ignore_limits: bool = False,
    ) -> Delivery:
        """把模型写好的一句话带给主人，附带最近群聊记录卡片。"""
        settings = self.settings
        if not settings.enabled or not settings.feedback_enabled:
            return Delivery(STATUS_DISABLED, "反馈转达功能当前没有开启。")
        if not settings.feedback_session_id:
            return Delivery(
                STATUS_UNCONFIGURED,
                "还没有绑定反馈接收会话。请在目标会话执行 /hr_bind，或单独配置 feedback_session_id。",
            )

        body = clean_text(message) or clean_text(message_text(event)) or FALLBACK_BODY

        chatlog = None
        if settings.feedback_attach_chatlog:
            chatlog = await self.chatlog.collect(
                event,
                count=settings.feedback_chatlog_count,
            )
            group_name = chatlog.group_name
        else:
            group_name = await self.chatlog.group_name(event)

        text = body
        if settings.feedback_append_source:
            text = body + "\n\n" + self._footnote(event, group_name)

        image_path = None
        if chatlog is not None and not chatlog.empty:
            image_path = await self.card.render_chatlog(
                chatlog,
                kind="feedback",
                tag="小报告",
                footer="发给 " + settings.receiver_name,
            )
            if not image_path:
                # 卡片是锦上添花，画不出来就把几句原文贴在正文后面，别让上下文丢了。
                fallback = chatlog.as_text()
                if fallback:
                    text = text + "\n\n最近的聊天记录：\n" + fallback

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
            return delivery

        await self.store.add_contact(
            {
                "created_at": time.time(),
                "session_id": session_id(event),
                "platform_id": platform_id(event),
                "group_id": group_id(event),
                "group_name": group_name,
                "sender_id": sender_id(event),
                "sender_name": sender_name(event),
                "message": truncate(body, 400),
                "reply": "",
                "replied_at": 0,
            },
            settings.feedback_recent_max_entries,
        )
        logger.info(
            "%s 已替 %s 找过 %s | 来源=%s 记录=%s",
            LOG_PREFIX,
            sender_name(event),
            settings.receiver_name,
            session_id(event),
            "无" if chatlog is None else chatlog.source,
        )
        return delivery

    # ------------------------------------------------------------------
    # 工具编排
    # ------------------------------------------------------------------
    async def handle_tool_call(self, *, event: Any, message: str) -> str:
        """工具的返回文本。

        这段话只有模型看得到，用来告诉它「话到底送出去了没有」。
        底线是：没真送出去时绝不能让它宣称已经转达。
        """
        settings = self.settings
        receiver = settings.receiver_name

        if not settings.enabled or not settings.feedback_enabled:
            return (
                "转达功能当前没有开启，所以这句话没有送出去。"
                "请自然、诚实地告诉用户你暂时没办法帮他联系" + receiver + "，"
                "可以建议他自己去找作者或到插件仓库提 issue。不要提到工具调用，也不要假装已经转达。"
            )

        if not settings.feedback_allow_anyone:
            try:
                is_admin = bool(event.is_admin())
            except Exception:
                is_admin = False
            if not is_admin:
                return (
                    "当前设置只允许管理员通过你联系" + receiver + "，所以这句话没有送出去。"
                    "请客气、自然地说明你没办法替他转达，并建议他去找管理员或作者。"
                    "不要提到工具调用，也不要假装已经转达。"
                )

        delivery = await self.relay(event=event, message=message)

        if delivery.ok:
            return (
                "话已经带到" + receiver + "那边了，最近的群聊记录也一起附上了。"
                "请用你自己的口吻自然地告诉用户你已经帮他去说了，让他等" + receiver + "有空再回。"
                "不要提到工具调用，也不要念出这段提示。"
            )

        if delivery.status in {STATUS_COOLDOWN, STATUS_RATE_LIMITED}:
            return (
                "刚刚已经替这个会话找过" + receiver + "了，这次没有重复发送。"
                "请自然地告诉用户你之前已经说过了，" + receiver + "看到就会处理，让他先稍等，"
                "不要重复承诺，也不要提到冷却时间或工具调用。"
            )

        if delivery.status == STATUS_UNCONFIGURED:
            return (
                "还没有配置接收窗口，这句话没能送出去。"
                "请诚实、自然地告诉用户你现在联系不上" + receiver + "，"
                "建议他直接去找作者或到插件仓库反馈。不要假装已经转达成功。"
            )

        return (
            "这次没能送出去。"
            "请诚实、自然地告诉用户消息没送到，让他稍后再试或直接联系" + receiver + "。"
            "不要假装已经转达成功，也不要提到工具调用失败的细节。"
        )

    # ------------------------------------------------------------------
    # 回话（主人 -> 用户）
    # ------------------------------------------------------------------
    async def reply_back(self, *, content: str, index: int = 1) -> tuple[bool, str]:
        """把主人的回话送回某次联系的来源会话。

        序号来自 /hr_recent 的列表，1 是最近一次，默认就回它。
        """
        settings = self.settings
        body = clean_text(content)
        if not body:
            return False, "用法：/hr_back 你要说的话（默认回最近一次），或 /hr_back 2 你要说的话。"

        contacts = await self.store.get_contacts()
        if not contacts:
            return False, "还没有人通过我找过你，暂时没有可以回话的对象。"
        if index < 1 or index > len(contacts):
            return False, (
                "最近只有 " + str(len(contacts)) + " 条联系记录，第 " + str(index) + " 条不存在。"
                "可以先用 /hr_recent 看看列表。"
            )

        entry = contacts[index - 1]
        target = clean_text(entry.get("session_id"))
        if not target:
            return False, "这条记录没有留下来源会话，没办法回过去。"

        who = clean_text(entry.get("sender_name"), "对方")
        text = ""
        # 回话时模型不在场（是主人敲的命令），所以这里确实需要借人格改写一次，
        # 否则送回群里的会是一句冷冰冰的系统通知。
        if settings.feedback_reply_persona_rewrite:
            text = await self.persona.rewrite(
                umo=target,
                task=(
                    "你要把" + settings.receiver_name + "的回话带回给" + who + "。"
                    "用你自己的口吻自然地转述，像帮忙传话，"
                    "不要写成公告，也不要改变回话的意思。"
                ),
                material=(
                    settings.receiver_name + "的回话：" + body + "\n"
                    "之前你替" + who + "带过去的话：" + clean_text(entry.get("message"))
                ),
            )
        if not text:
            text = settings.receiver_name + "让我把话带回给你：" + body

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
            return False, "回话没能送回去：" + delivery.detail

        await self.store.mark_contact_replied(index - 1, body)
        where = self._where(clean_text(entry.get("group_id")), clean_text(entry.get("group_name")))
        return True, "已经把回话带回给" + who + "（" + where + "）。"

    async def format_recent(self, limit: int = 10) -> str:
        """列出最近谁通过 Bot 找过主人。"""
        contacts = await self.store.get_contacts()
        if not contacts:
            return "还没有人通过我找过你。"

        picked = contacts[: max(1, limit)]
        lines = ["最近 " + str(len(picked)) + " 次有人通过我找你（共 " + str(len(contacts)) + " 条）："]
        for number, entry in enumerate(picked, start=1):
            where = self._where(
                clean_text(entry.get("group_id")),
                clean_text(entry.get("group_name")),
            )
            head = (
                str(number) + ". " + clean_text(entry.get("sender_name"), "未知用户")
                + "（" + clean_text(entry.get("sender_id"), "unknown") + "）"
                + " · " + where
            )
            moment = relative_time(entry.get("created_at"))
            if moment:
                head = head + " · " + moment
            lines.append(head)
            lines.append("   " + truncate(clean_text(entry.get("message")), 120))
            reply = clean_text(entry.get("reply"))
            if reply:
                lines.append("   已回：" + truncate(reply, 80))
        lines.append("")
        lines.append("回话：/hr_back 内容（默认第 1 条）｜ /hr_back 2 内容")
        return "\n".join(lines)
