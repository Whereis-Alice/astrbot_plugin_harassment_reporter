from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .eventinfo import self_id as event_self_id
from .eventinfo import session_id
from .onebot import get_client, get_raw_notice, is_onebot_event
from .outbox import STATUS_DISABLED, Delivery
from .text import clean_text, convert_duration, now_text, pack_lines, truncate

LOG_PREFIX = "[HarassmentReporter]"
CHANNEL = "notice"
DEDUP_CHANNEL = "notice_dedup"

# 一部分 OneBot 实现会把同一个通知推送多次，这里做一个很短的去重窗口。
DEDUP_WINDOW = 15


def _same_id(left: Any, right: Any) -> bool:
    a = clean_text(left)
    b = clean_text(right)
    return bool(a) and a == b


def _parse_onebot_target(umo: str) -> tuple[str, str]:
    """把 aiocqhttp 会话 ID 拆成 (群号, QQ 号)，非 OneBot 会话返回两个空串。"""
    parts = clean_text(umo).split(":")
    if len(parts) < 3 or parts[0] != "aiocqhttp":
        return "", ""
    kind = parts[1].lower()
    target = clean_text(parts[2])
    if "group" in kind:
        return target, ""
    return "", target


class NoticeService:
    """群事件小报告。

    Bot 自己被禁言、被解禁、被设为管理员、被踢出群、被拉进新群时，
    主动用当前人格的口吻告诉主人一声，并可以顺手抽查那个群最近在聊什么。
    """

    def __init__(
        self,
        *,
        context: Any,
        settings: Any,
        store: Any,
        persona: Any,
        card: Any,
        outbox: Any,
        bridge: Any,
    ) -> None:
        self.context = context
        self.settings = settings
        self.store = store
        self.persona = persona
        self.card = card
        self.outbox = outbox
        self.bridge = bridge

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def handle(self, event: Any) -> bool:
        """处理一个可能是通知事件的消息。真的处理了才返回 True。"""
        settings = self.settings
        if not settings.enabled or not settings.notice_enabled:
            return False

        raw = get_raw_notice(event)
        if raw is None:
            return False

        payload = self._classify(event, raw)
        if payload is None:
            return False

        group = payload["group_id"]
        whitelist = settings.notice_group_whitelist
        if group and whitelist and group not in whitelist:
            return False

        dedup_key = payload["kind"] + "|" + (group or clean_text(payload.get("operator_id")))
        if await self.store.cooldown_remaining(DEDUP_CHANNEL, dedup_key, DEDUP_WINDOW) > 0:
            return False
        await self.store.mark_cooldown(DEDUP_CHANNEL, dedup_key)

        try:
            await self._report(event, payload)
        except Exception as exc:
            logger.error("%s 处理群事件通知失败：%s", LOG_PREFIX, exc)
        return True

    # ------------------------------------------------------------------
    # 事件分类
    # ------------------------------------------------------------------
    def _classify(self, event: Any, raw: dict[str, Any]) -> dict[str, Any] | None:
        """把原始通知转成统一的描述结构，不关心的事件返回 None。"""
        settings = self.settings
        me = event_self_id(event)
        notice_type = clean_text(raw.get("notice_type"))
        sub_type = clean_text(raw.get("sub_type"))
        group = clean_text(raw.get("group_id"))
        operator = clean_text(raw.get("operator_id"))
        user = clean_text(raw.get("user_id"))
        target = clean_text(raw.get("target_id"))

        if notice_type == "group_ban" and settings.notice_ban:
            if not _same_id(user, me):
                return None
            try:
                seconds = int(raw.get("duration") or 0)
            except Exception:
                seconds = 0
            if sub_type == "lift_ban" or seconds <= 0:
                return self._payload(
                    kind="ban_lift",
                    title="禁言解除",
                    icon="🔓",
                    badge="已解禁",
                    badge_level="low",
                    headline="我在这个群里的禁言被解除了，又可以说话了。",
                    group_id=group,
                    operator_id=operator,
                    extra_lines=[],
                )
            return self._payload(
                kind="ban",
                title="我被禁言了",
                icon="🔇",
                badge="被禁言",
                badge_level="high",
                headline="我在这个群里被管理员禁言了，暂时说不了话。",
                group_id=group,
                operator_id=operator,
                extra_lines=["禁言时长：" + convert_duration(seconds)],
            )

        if notice_type == "group_admin" and settings.notice_admin:
            if not _same_id(user, me):
                return None
            if sub_type == "unset":
                return self._payload(
                    kind="admin_unset",
                    title="管理员被取消",
                    icon="📉",
                    badge="取消管理",
                    badge_level="medium",
                    headline="我在这个群里的管理员权限被取消了。",
                    group_id=group,
                    operator_id=operator,
                    extra_lines=[],
                )
            return self._payload(
                kind="admin_set",
                title="升职成管理员",
                icon="📈",
                badge="设为管理",
                badge_level="low",
                headline="我在这个群里被设为管理员了。",
                group_id=group,
                operator_id=operator,
                extra_lines=[],
            )

        if notice_type == "group_decrease" and settings.notice_member_change:
            if not _same_id(user, me):
                return None
            if sub_type == "kick_me":
                return self._payload(
                    kind="kicked",
                    title="我被踢出群了",
                    icon="🚪",
                    badge="被移出",
                    badge_level="high",
                    headline="我被人从这个群里踢出去了。",
                    group_id=group,
                    operator_id=operator,
                    extra_lines=[],
                    snapshot=False,
                )
            return self._payload(
                kind="left",
                title="我离开了一个群",
                icon="🚪",
                badge="已退群",
                badge_level="medium",
                headline="我不在这个群里了（可能是主动退出或群被解散）。",
                group_id=group,
                operator_id=operator,
                extra_lines=["事件子类型：" + (sub_type or "未知")],
                snapshot=False,
            )

        if notice_type == "group_increase" and settings.notice_member_change:
            if not _same_id(user, me):
                return None
            how = "被邀请进来的" if sub_type == "invite" else "进群申请通过了"
            return self._payload(
                kind="joined",
                title="我进了一个新群",
                icon="🎉",
                badge="新群",
                badge_level="low",
                headline="我刚刚加入了一个新群，" + how + "。",
                group_id=group,
                operator_id=operator,
                extra_lines=[],
            )

        if notice_type == "friend_add" and settings.notice_member_change:
            return self._payload(
                kind="friend_add",
                title="加了个新好友",
                icon="🤝",
                badge="新好友",
                badge_level="info",
                headline="有人把我加成好友了。",
                group_id="",
                operator_id=user,
                extra_lines=[],
                snapshot=False,
            )

        if notice_type == "notify" and sub_type == "poke" and settings.notice_poke:
            if not _same_id(target, me):
                return None
            return self._payload(
                kind="poke",
                title="有人戳我",
                icon="👆",
                badge="戳一戳",
                badge_level="info",
                headline="有人戳了我一下。",
                group_id=group,
                operator_id=user,
                extra_lines=[],
                snapshot=False,
            )

        return None

    @staticmethod
    def _payload(
        *,
        kind: str,
        title: str,
        icon: str,
        badge: str,
        badge_level: str,
        headline: str,
        group_id: str,
        operator_id: str,
        extra_lines: list[str],
        snapshot: bool = True,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "title": title,
            "icon": icon,
            "badge": badge,
            "badge_level": badge_level,
            "headline": headline,
            "group_id": group_id,
            "operator_id": operator_id,
            "extra_lines": extra_lines,
            "snapshot": snapshot,
        }

    # ------------------------------------------------------------------
    # 上报
    # ------------------------------------------------------------------
    async def _report(self, event: Any, payload: dict[str, Any]) -> Delivery:
        settings = self.settings
        target = settings.notice_session_id
        if not target:
            return Delivery(STATUS_DISABLED, "还没有配置通知接收会话。")

        group = clean_text(payload["group_id"])
        client = get_client(event)
        group_name = await self.bridge.fetch_group_name(client, group) if group else ""
        operator_id = clean_text(payload["operator_id"])
        operator_name = ""
        if operator_id:
            operator_name = await self.bridge.fetch_member_name(client, group, operator_id)

        where = ("群 " + (group_name + "（" + group + "）" if group_name else group)) if group else "私聊"
        who = (operator_name + "（" + operator_id + "）") if operator_name else (operator_id or "未知")

        lines = [
            "【群事件通知】" + payload["title"],
            "时间：" + now_text(),
            "情况：" + payload["headline"],
            "位置：" + where,
        ]
        if operator_id:
            lines.append("操作者：" + who)
        lines.extend(payload["extra_lines"])

        rows: list[dict[str, Any]] = []
        snapshot_block = ""
        want_snapshot = bool(payload.get("snapshot")) and settings.notice_snapshot and group
        if want_snapshot:
            rows, snapshot_block = await self._collect_snapshot(event, group)

        structured = "\n".join(lines)
        if snapshot_block:
            structured += "\n\n这个群最近的消息：\n" + snapshot_block

        text = structured
        if settings.notice_persona_rewrite:
            natural = await self.persona.rewrite_for_event(
                event,
                task=(
                    "你现在要主动去找「" + settings.receiver_name + "」说一件刚发生在你身上的事。"
                    "用你自己的口吻讲清楚发生了什么、在哪个群、是谁做的，"
                    "像真的在跟熟人说话，不要写成系统公告或日志。"
                ),
                material=structured,
                extra_rules="不要复述聊天记录原文，只需要说清楚发生了什么。",
            )
            if natural:
                tail = "（" + payload["title"] + " ｜ " + where
                if operator_id:
                    tail += " ｜ 操作者 " + who
                tail += " ｜ " + now_text() + "）"
                text = natural + "\n\n" + tail

        image_path = await self._render_card(
            payload=payload,
            where=where,
            who=who if operator_id else "",
            rows=rows,
            group_name=group_name,
            group=group,
        )

        if image_path and not settings.card_keep_text and snapshot_block:
            # 卡片里已经有聊天记录了，纯文本里就不再重复一遍。
            text = "\n".join(lines)

        delivery = await self.outbox.deliver(
            channel=CHANNEL,
            target_session_id=target,
            text=text,
            image_path=image_path,
            source_session_id=session_id(event),
            cooldown=0,
            hourly_limit=settings.notice_hourly_limit,
        )

        if delivery.ok and rows and image_path is None:
            # 卡片没画出来，退一步用合并转发把原始记录送过去。
            await self._try_forward(event, target, rows, where)

        if delivery.ok:
            logger.info(
                "%s 群事件通知已发送 | 类型=%s 群=%s",
                LOG_PREFIX,
                payload["kind"],
                group or "-",
            )
        else:
            logger.warning(
                "%s 群事件通知未发送 | 类型=%s 原因=%s",
                LOG_PREFIX,
                payload["kind"],
                delivery.detail,
            )
        return delivery

    # ------------------------------------------------------------------
    # 抽查
    # ------------------------------------------------------------------
    async def _collect_snapshot(
        self,
        event: Any,
        group: str,
        *,
        count: int = 0,
    ) -> tuple[list[dict[str, Any]], str]:
        """拉一段群历史，返回 (卡片气泡数据, 纯文本块)。"""
        client = get_client(event)
        if client is None or not group:
            return [], ""
        limit = count or self.settings.notice_snapshot_count
        lines = await self.bridge.fetch_group_history(
            client,
            group,
            count=limit,
            self_id=event_self_id(event),
            text_limit=min(200, self.settings.max_excerpt_length),
        )
        if not lines:
            return [], ""
        rows = [line.as_dict() for line in lines]
        block = pack_lines(
            [item.sender_name + "：" + item.text for item in lines],
            max_chars=self.settings.recent_summary_max_chars,
        )
        return rows, block

    async def peek(
        self,
        event: Any,
        group: str,
        *,
        count: int = 0,
        target_session_id: str = "",
    ) -> tuple[bool, str]:
        """管理员手动抽查某个群的最近消息，成功返回 (True, 提示文本)。"""
        group = clean_text(group)
        if not group:
            return False, "用法：/hr_peek 群号"
        if not is_onebot_event(event):
            return False, "抽查功能依赖 OneBot v11 接口，当前平台用不了。"

        rows, block = await self._collect_snapshot(event, group, count=count)
        if not rows:
            return False, (
                "没能取到群 " + group + " 的历史消息。\n"
                "可能是 Bot 不在这个群里，或者你用的协议端不支持 get_group_msg_history。"
            )

        client = get_client(event)
        group_name = await self.bridge.fetch_group_name(client, group)
        where = "群 " + (group_name + "（" + group + "）" if group_name else group)
        target = clean_text(target_session_id) or session_id(event)

        image_path = None
        if self.card.enabled_for("notice"):
            image_path = await self.card.render(
                kind="notice",
                title="群消息抽查",
                subtitle=where,
                icon="🔍",
                badge=str(len(rows)) + " 条",
                badge_level="info",
                summary="",
                chat_title="最近消息",
                meta=[
                    {"label": "时间", "value": now_text()},
                    {"label": "群号", "value": group},
                    {"label": "群名", "value": group_name},
                ],
                messages=self.card.build_messages(rows, limit=self.settings.card_max_messages),
                footer="群消息抽查 ｜ 骚扰上报器",
            )

        text = "【群消息抽查】" + where + "\n时间：" + now_text()
        if image_path is None or self.settings.card_keep_text:
            text += "\n\n" + truncate(block, 2000)

        delivery = await self.outbox.deliver(
            channel=CHANNEL,
            target_session_id=target,
            text=text,
            image_path=image_path,
            source_session_id=target,
            ignore_limits=True,
        )
        if not delivery.ok:
            return False, "抽查结果没能发出去：" + delivery.detail
        return True, "已抽查 " + where + "，共 " + str(len(rows)) + " 条消息。"

    # ------------------------------------------------------------------
    # 合并转发降级
    # ------------------------------------------------------------------
    async def _try_forward(
        self,
        event: Any,
        target: str,
        rows: list[dict[str, Any]],
        where: str,
    ) -> bool:
        client = get_client(event)
        if client is None:
            return False
        group, user = _parse_onebot_target(target)
        if not group and not user:
            return False

        nodes = [
            self.bridge.build_forward_node(
                name="抽查结果",
                uin=event_self_id(event),
                text=where + " 最近的消息记录",
            )
        ]
        for row in rows[-self.settings.notice_snapshot_count :]:
            nodes.append(
                self.bridge.build_forward_node(
                    name=clean_text(row.get("sender_name"), "未知用户"),
                    uin=clean_text(row.get("sender_id"), "0"),
                    text=clean_text(row.get("text"), "[空消息]"),
                )
            )
        return await self.bridge.send_forward(client, nodes=nodes, group_id=group, user_id=user)

    # ------------------------------------------------------------------
    # 卡片
    # ------------------------------------------------------------------
    async def _render_card(
        self,
        *,
        payload: dict[str, Any],
        where: str,
        who: str,
        rows: list[dict[str, Any]],
        group_name: str,
        group: str,
    ) -> str | None:
        if not self.card.enabled_for("notice"):
            return None

        meta = [
            {"label": "时间", "value": now_text()},
            {"label": "位置", "value": where},
        ]
        if group:
            meta.append({"label": "群号", "value": group})
        if group_name:
            meta.append({"label": "群名", "value": group_name})
        if who:
            meta.append({"label": "操作者", "value": who})

        summary_parts = [payload["headline"]]
        summary_parts.extend(payload["extra_lines"])

        return await self.card.render(
            kind="notice",
            title=payload["title"],
            subtitle=where,
            icon=payload["icon"],
            badge=payload["badge"],
            badge_level=payload["badge_level"],
            summary="\n".join(part for part in summary_parts if part),
            summary_title="发生了什么",
            chat_title="这个群最近在聊",
            meta=meta,
            messages=self.card.build_messages(rows, limit=self.settings.card_max_messages)
            if rows
            else [],
            footer="发给 " + self.settings.receiver_name + " ｜ 群事件通知",
        )
