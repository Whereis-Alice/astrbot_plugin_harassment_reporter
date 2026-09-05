from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

from .text import clean_text, convert_duration

LOG_PREFIX = "[HarassmentReporter]"

# deliver() 可能返回的状态，调用方据此决定要不要让模型说「已经上报」。
STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_UNCONFIGURED = "unconfigured"
STATUS_COOLDOWN = "cooldown"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_SEND_FAILED = "send_failed"


@dataclass
class Delivery:
    """一次投递的结果。"""

    status: str
    detail: str = ""
    remaining: int = 0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


class Outbox:
    """所有发给主人的消息都从这里出去。

    冷却、每小时限流、卡片附图、失败降级都收在一处，
    这样骚扰上报、反馈转达、群事件通知三条链路的行为完全一致。
    """

    def __init__(self, context: Any, store: Any, settings: Any, card: Any) -> None:
        self.context = context
        self.store = store
        self.settings = settings
        self.card = card

    async def deliver(
        self,
        *,
        channel: str,
        target_session_id: str,
        text: str,
        image_path: str | None = None,
        source_session_id: str = "",
        cooldown: int = 0,
        hourly_limit: int = 0,
        ignore_limits: bool = False,
    ) -> Delivery:
        """把一条消息投递到目标会话。

        Args:
            channel: 逻辑通道名（harassment / feedback / notice / ticket_reply），
                冷却与限流按通道独立计数。
            source_session_id: 触发来源会话，冷却按它计数；留空则按目标会话计数。
            ignore_limits: 管理员手动测试时跳过冷却与限流。
        """
        target = clean_text(target_session_id)
        body = clean_text(text)

        if not target:
            return Delivery(
                STATUS_UNCONFIGURED,
                "还没有绑定接收会话。请在目标会话里执行 /hr_bind，或把 /hr_sid 显示的 ID 填进插件配置。",
            )
        if not body and not image_path:
            return Delivery(STATUS_SEND_FAILED, "没有可发送的内容。")

        cooldown_key = clean_text(source_session_id) or target

        if not ignore_limits and cooldown > 0:
            remaining = await self.store.cooldown_remaining(channel, cooldown_key, cooldown)
            if remaining > 0:
                return Delivery(
                    STATUS_COOLDOWN,
                    f"这个会话还在冷却中，剩余约 {convert_duration(remaining)}，本次没有发送。",
                    remaining,
                )

        if not ignore_limits and hourly_limit > 0:
            limited, used = await self.store.rate_limited(channel, hourly_limit)
            if limited:
                return Delivery(
                    STATUS_RATE_LIMITED,
                    f"最近一小时已经发了 {used} 条，达到上限 {hourly_limit} 条，本次没有发送。",
                )

        chain = MessageChain()
        has_image = False
        if image_path:
            try:
                chain.file_image(str(image_path))
                has_image = True
            except Exception as exc:
                logger.warning("%s 卡片附图失败，本次改用纯文本：%s", LOG_PREFIX, exc)
                has_image = False

        if body and (not has_image or self.settings.card_keep_text):
            chain.message(body)
        if not chain.chain:
            chain.message(body or "（空消息）")
        if self.settings.force_plain_text:
            # 上报里常有会话 ID、群号这类需要复制的内容，默认不走全局文转图。
            chain.use_t2i(False)

        try:
            delivered = await self.context.send_message(target, chain)
        except Exception as exc:
            logger.error("%s 投递消息失败：%s", LOG_PREFIX, exc)
            return Delivery(STATUS_SEND_FAILED, f"发送失败：{exc}")

        if not delivered:
            return Delivery(
                STATUS_SEND_FAILED,
                "发送失败：AstrBot 没能把消息投递出去。请确认接收会话 ID 是否正确、对应平台适配器是否在线。",
            )

        await self.store.mark_cooldown(channel, cooldown_key)
        if hourly_limit > 0:
            await self.store.mark_rate(channel)
        return Delivery(STATUS_OK, "已送达。")
