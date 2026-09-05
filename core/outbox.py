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
    # 实际挂上去的转发图片数。回执要照实说带了几张，不能凭「打算带几张」张口就报。
    images_sent: int = 0

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

    async def precheck(
        self,
        *,
        channel: str,
        target_session_id: str,
        source_session_id: str = "",
        cooldown: int = 0,
        hourly_limit: int = 0,
        ignore_limits: bool = False,
    ) -> Delivery | None:
        """先问一句「这条现在发得出去吗」，答案是不行就别再往下折腾。

        三条链路在真正投递之前都要做昂贵的准备工作：调模型改写成人格口吻、
        走 OneBot 接口拉群历史、开无头浏览器截一张卡片。如果这些都干完了才发现
        正处在冷却期，那一整轮算力就白烧了。所以调用方应当先问这里。

        返回 None 表示可以继续；返回 Delivery 时，其状态与 deliver() 在同样
        条件下的返回完全一致，调用方直接 return 即可。deliver() 内部仍会再判一次，
        这里只是提前挡一道，两处结果保持同一份逻辑。
        """
        target = clean_text(target_session_id)
        if not target:
            return Delivery(
                STATUS_UNCONFIGURED,
                "还没有绑定接收会话。请在目标会话里执行 /hr_bind，或把 /hr_sid 显示的 ID 填进插件配置。",
            )
        if ignore_limits:
            return None

        cooldown_key = clean_text(source_session_id) or target

        if cooldown > 0:
            remaining = await self.store.cooldown_remaining(channel, cooldown_key, cooldown)
            if remaining > 0:
                return Delivery(
                    STATUS_COOLDOWN,
                    f"这个会话还在冷却中，剩余约 {convert_duration(remaining)}，本次没有发送。",
                    remaining,
                )

        if hourly_limit > 0:
            limited, used = await self.store.rate_limited(channel, hourly_limit)
            if limited:
                return Delivery(
                    STATUS_RATE_LIMITED,
                    f"最近一小时已经发了 {used} 条，达到上限 {hourly_limit} 条，本次没有发送。",
                )

        return None

    async def deliver(
        self,
        *,
        channel: str,
        target_session_id: str,
        text: str,
        image_path: str | None = None,
        images: list[Any] | None = None,
        source_session_id: str = "",
        cooldown: int = 0,
        hourly_limit: int = 0,
        ignore_limits: bool = False,
    ) -> Delivery:
        """把一条消息投递到目标会话。

        Args:
            channel: 逻辑通道名（harassment / feedback / notice / feedback_reply），
                冷却与限流按通道独立计数。
            image_path: 聊天记录卡片的本地文件路径。
            images: 要一起带过去的原图（images.ImageRef），比如群友说「把这张图给狐狸」。
            source_session_id: 触发来源会话，冷却按它计数；留空则按目标会话计数。
            ignore_limits: 管理员手动测试时跳过冷却与限流。
        """
        target = clean_text(target_session_id)
        body = clean_text(text)

        blocked = await self.precheck(
            channel=channel,
            target_session_id=target,
            source_session_id=source_session_id,
            cooldown=cooldown,
            hourly_limit=hourly_limit,
            ignore_limits=ignore_limits,
        )
        if blocked is not None:
            return blocked
        if not body and not image_path:
            return Delivery(STATUS_SEND_FAILED, "没有可发送的内容。")

        cooldown_key = clean_text(source_session_id) or target

        # 文本才是主角 —— 那是模型自己写的那句话；卡片只是跟在后面的一张截图。
        # 所以顺序固定为「先说话，再放聊天记录卡片，最后才是群友要转的原图」，
        # 主人打开会话时第一眼看到的永远是那句话。
        chain = MessageChain()
        if body:
            chain.message(body)
        if image_path:
            try:
                chain.file_image(str(image_path))
            except Exception as exc:
                logger.warning("%s 卡片附图失败，本次只发文本：%s", LOG_PREFIX, exc)
        images_sent = sum(1 for ref in (images or []) if ref and ref.attach_to(chain))
        if not chain.chain:
            chain.message("（空消息）")
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
        return Delivery(STATUS_OK, "已送达。", images_sent=images_sent)
