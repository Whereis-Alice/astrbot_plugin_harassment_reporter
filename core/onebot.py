from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .text import clean_text, truncate

LOG_PREFIX = "[HarassmentReporter]"

ONEBOT_PLATFORM_NAMES = {"aiocqhttp"}

# 各实现对合并转发 node 的字段命名不一致：
# go-cqhttp / SnowLuma 习惯 uin + name，NapCat / LLBot 兼容 user_id + nickname。
# 同时写入两套键在实测中兼容性最好。
FORWARD_NODE_ALIASES = True

# 一个接口连续失败多少次后，才认定「这个协议端真的不支持」并在本次运行里不再尝试。
# 用连续失败计数而不是一次失败就下结论：网络抖动、协议端重启、临时风控都会
# 造成偶发失败，一次就把能力标成不支持，整个运行周期都会白白降级。
FAILURE_LIMIT = 3

# 兼容旧名字：合并转发原本单独有一个阈值，现在和其它能力共用同一个。
FORWARD_FAILURE_LIMIT = FAILURE_LIMIT


@dataclass
class ChatLine:
    """一条被标准化后的群聊记录。"""

    sender_name: str = "未知用户"
    sender_id: str = ""
    text: str = ""
    timestamp: float = 0.0
    is_self: bool = False
    avatar: str = ""
    # 这条消息里图片的 http 地址。卡片用它画缩略图，转发时也能直接重发。
    images: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sender_name": self.sender_name,
            "sender_id": self.sender_id,
            "text": self.text,
            "timestamp": self.timestamp,
            "is_self": self.is_self,
            "avatar": self.avatar,
            "images": list(self.images),
        }


@dataclass
class OneBotCapability:
    """记录本次运行时探测到的实现能力，避免反复调用失败的接口。"""

    history_supports_count: bool = True
    private_history_supported: bool = True
    private_history_supports_count: bool = True
    forward_supported: bool = True
    forward_failures: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    strikes: dict[str, int] = field(default_factory=dict)

    def note_failure(self, action: str) -> None:
        self.failures[action] = self.failures.get(action, 0) + 1

    def strike(self, name: str) -> bool:
        """给某个能力记一次失败，连续失败到阈值时返回 True。

        返回 True 才代表可以下「这个协议端不支持」的结论；在那之前每次都还会
        再试一遍，所以一次偶发失败不会让能力被永久关掉，网络恢复后自动痊愈。
        """
        self.strikes[name] = self.strikes.get(name, 0) + 1
        return self.strikes[name] >= FAILURE_LIMIT

    def clear_strike(self, name: str) -> None:
        """调用成功了，把这个能力的连续失败计数归零。"""
        self.strikes.pop(name, None)

    def note_forward_result(self, ok: bool) -> None:
        """合并转发的失败判定。

        一次失败更可能是网络抖动或临时风控，连续失败到阈值才认定这个实现
        不支持合并转发，避免偶发错误让整个运行周期都退化成纯文本。
        """
        if ok:
            self.forward_failures = 0
            self.clear_strike("forward")
            return
        self.forward_failures += 1
        if self.strike("forward"):
            self.forward_supported = False


# QQ 的公开头像地址，直接拼 QQ 号 / 群号就能取到，不需要登录态。
# 卡片是交给文转图服务渲染的，由那一端去拉这两个地址。
QQ_USER_AVATAR = "https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640"
QQ_GROUP_AVATAR = "https://p.qlogo.cn/gh/{gid}/{gid}/640/"


def qq_user_avatar(uin: Any) -> str:
    """用户头像地址。只认纯数字 QQ 号，其余一律返回空串走首字块兜底。"""
    value = clean_text(uin)
    return QQ_USER_AVATAR.format(uin=value) if value.isdigit() else ""


def qq_group_avatar(gid: Any) -> str:
    """群头像地址。同样只认纯数字群号。"""
    value = clean_text(gid)
    return QQ_GROUP_AVATAR.format(gid=value) if value.isdigit() else ""


def is_onebot_event(event: Any) -> bool:
    """判断事件是否来自 OneBot v11 适配器（NapCat / LLBot / SnowLuma 等）。"""
    try:
        return clean_text(event.get_platform_name()) in ONEBOT_PLATFORM_NAMES
    except Exception:
        return False


def get_client(event: Any) -> Any | None:
    """取出 aiocqhttp 客户端，非 OneBot 平台返回 None。"""
    if not is_onebot_event(event):
        return None
    client = getattr(event, "bot", None)
    return client if client is not None else None


def get_raw_notice(event: Any) -> dict[str, Any] | None:
    """如果这是一个 OneBot 通知事件，返回它的原始字典。"""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if not isinstance(raw, dict):
        return None
    if clean_text(raw.get("post_type")) != "notice":
        return None
    return raw


def segments_to_text(message: Any, *, limit: int = 200) -> str:
    """把 OneBot 消息段转成一行可读文本，尽量保留非文字内容的占位符。"""
    if isinstance(message, str):
        return truncate(clean_text(message), limit)
    if not isinstance(message, list):
        return ""

    parts: list[str] = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        seg_type = clean_text(seg.get("type"))
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type == "text":
            parts.append(clean_text(data.get("text")))
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type == "at":
            target = clean_text(data.get("qq"))
            parts.append("[@全体]" if target == "all" else f"[@{target}]")
        elif seg_type == "reply":
            parts.append("[回复]")
        elif seg_type in {"record", "video"}:
            parts.append("[语音]" if seg_type == "record" else "[视频]")
        elif seg_type == "file":
            parts.append(f"[文件 {clean_text(data.get('file'), '未知')}]")
        elif seg_type in {"json", "xml"}:
            parts.append("[卡片消息]")
        elif seg_type == "forward":
            parts.append("[合并转发]")
        elif seg_type == "poke":
            parts.append("[戳一戳]")
        else:
            parts.append(f"[{seg_type or '未知内容'}]")
    return truncate(clean_text(" ".join(p for p in parts if p)), limit)


def collect_segment_images(message: Any, *, limit: int = 4) -> list[str]:
    """从 OneBot 消息段里挑出图片的 http 地址。

    协议端在 image 段里给的 url 一般是可以直接访问的 http 地址；个别实现只填
    file，那就看 file 是不是也是个地址。两个都不是 http（比如只给了一个本地
    缓存文件名）就跳过 —— 我们这边拿不到那个文件。
    """
    if not isinstance(message, list):
        return []
    found: list[str] = []
    for seg in message:
        if not isinstance(seg, dict) or clean_text(seg.get("type")) != "image":
            continue
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        for key in ("url", "file"):
            value = clean_text(data.get(key))
            if value.startswith(("http://", "https://")):
                if value not in found:
                    found.append(value)
                break
        if len(found) >= max(1, limit):
            break
    return found


class OneBotBridge:
    """OneBot v11 动作的容错封装。

    不同实现（NapCat、LLBot / LLOneBot、SnowLuma、Lagrange 等）对同一个动作的
    参数和返回结构支持程度不同，所有调用都在这里做降级，绝不向上抛异常。
    """

    def __init__(self, *, debug: bool = False) -> None:
        self.capability = OneBotCapability()
        self.debug = debug

    def _log(self, action: str, exc: Exception) -> None:
        self.capability.note_failure(action)
        if self.debug:
            logger.warning("%s OneBot 动作 %s 调用失败：%s", LOG_PREFIX, action, exc)
        else:
            logger.debug("%s OneBot 动作 %s 调用失败：%s", LOG_PREFIX, action, exc)

    async def call(self, client: Any, action: str, **params: Any) -> Any | None:
        if client is None:
            return None
        try:
            return await client.call_action(action, **params)
        except Exception as exc:
            self._log(action, exc)
            return None

    async def fetch_group_name(self, client: Any, group_id: Any) -> str:
        gid = clean_text(group_id)
        if not gid:
            return ""
        result = await self.call(client, "get_group_info", group_id=int(gid) if gid.isdigit() else gid)
        if isinstance(result, dict):
            name = clean_text(result.get("group_name"))
            if name:
                return name
        return ""

    async def fetch_member_name(self, client: Any, group_id: Any, user_id: Any) -> str:
        uid = clean_text(user_id)
        if not uid:
            return ""
        gid = clean_text(group_id)
        if gid:
            result = await self.call(
                client,
                "get_group_member_info",
                group_id=int(gid) if gid.isdigit() else gid,
                user_id=int(uid) if uid.isdigit() else uid,
                no_cache=False,
            )
            if isinstance(result, dict):
                name = clean_text(result.get("card")) or clean_text(result.get("nickname"))
                if name:
                    return name
        stranger = await self.call(
            client,
            "get_stranger_info",
            user_id=int(uid) if uid.isdigit() else uid,
        )
        if isinstance(stranger, dict):
            name = clean_text(stranger.get("nickname"))
            if name:
                return name
        return ""

    @staticmethod
    def _normalize_history(
        messages: list[dict[str, Any]],
        *,
        count: int,
        self_id: str = "",
        text_limit: int = 200,
    ) -> list[ChatLine]:
        """把 OneBot 返回的原始消息列表标准化成 ChatLine。

        群聊和私聊两个接口的返回结构一致，所以共用这一段。
        """
        mine = clean_text(self_id)
        lines: list[ChatLine] = []
        for item in messages[-max(1, count) :]:
            sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
            sender_id = clean_text(sender.get("user_id")) or clean_text(item.get("user_id"))
            sender_name = (
                clean_text(sender.get("card"))
                or clean_text(sender.get("nickname"))
                or clean_text(item.get("nickname"))
                or (sender_id or "未知用户")
            )
            text = segments_to_text(item.get("message"), limit=text_limit)
            if not text:
                text = truncate(clean_text(item.get("raw_message")), text_limit)
            try:
                timestamp = float(item.get("time") or 0)
            except Exception:
                timestamp = 0.0
            lines.append(
                ChatLine(
                    sender_name=sender_name,
                    sender_id=sender_id,
                    text=text or "[空消息]",
                    timestamp=timestamp or time.time(),
                    is_self=bool(mine) and sender_id == mine,
                    images=collect_segment_images(item.get("message")),
                )
            )
        return lines

    @staticmethod
    def _extract_history_messages(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, dict):
            for key in ("messages", "data", "message"):
                value = result.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return []

    async def fetch_group_history(
        self,
        client: Any,
        group_id: Any,
        *,
        count: int = 20,
        self_id: str = "",
        text_limit: int = 200,
    ) -> list[ChatLine]:
        """拉取群历史消息并标准化。任何实现不支持时返回空列表。"""
        gid = clean_text(group_id)
        if not gid or client is None:
            return []
        group_arg: Any = int(gid) if gid.isdigit() else gid

        result = None
        if self.capability.history_supports_count:
            result = await self.call(
                client,
                "get_group_msg_history",
                group_id=group_arg,
                count=max(1, min(100, count)),
            )
            if result is None:
                # 有的实现不认 count 参数，退回不带参数的调用。
                # 连续失败到阈值才真的放弃 count —— 不然一次网络抖动之后，
                # 这个运行周期里就只能拿协议端默认的那几条了。
                if self.capability.strike("group_history_count"):
                    self.capability.history_supports_count = False
            else:
                self.capability.clear_strike("group_history_count")
        if result is None:
            result = await self.call(client, "get_group_msg_history", group_id=group_arg)
        if result is None:
            return []

        messages = self._extract_history_messages(result)
        if not messages:
            return []
        return self._normalize_history(
            messages,
            count=count,
            self_id=self_id,
            text_limit=text_limit,
        )

    async def fetch_private_history(
        self,
        client: Any,
        user_id: Any,
        *,
        count: int = 20,
        self_id: str = "",
        text_limit: int = 200,
    ) -> list[ChatLine]:
        """拉取私聊历史消息。

        get_friend_msg_history 不在 OneBot v11 标准里，只有 NapCat、LLOneBot 等
        扩展实现提供。连续失败到阈值才认定当前协议端没有这个接口并停止重试，
        这样偶发的一次失败不会让整个运行周期都拿不到私聊记录。
        """
        uid = clean_text(user_id)
        if not uid or client is None or not self.capability.private_history_supported:
            return []
        user_arg: Any = int(uid) if uid.isdigit() else uid

        result = None
        if self.capability.private_history_supports_count:
            result = await self.call(
                client,
                "get_friend_msg_history",
                user_id=user_arg,
                count=max(1, min(100, count)),
            )
            if result is None:
                if self.capability.strike("private_history_count"):
                    self.capability.private_history_supports_count = False
            else:
                self.capability.clear_strike("private_history_count")
        if result is None:
            result = await self.call(client, "get_friend_msg_history", user_id=user_arg)
        if result is None:
            if self.capability.strike("private_history"):
                self.capability.private_history_supported = False
            return []
        self.capability.clear_strike("private_history")

        messages = self._extract_history_messages(result)
        if not messages:
            return []
        return self._normalize_history(
            messages,
            count=count,
            self_id=self_id,
            text_limit=text_limit,
        )

    def build_forward_node(self, *, name: str, uin: str, text: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "content": [{"type": "text", "data": {"text": text}}],
        }
        data["user_id"] = clean_text(uin, "0")
        data["nickname"] = clean_text(name, "未知用户")
        if FORWARD_NODE_ALIASES:
            data["uin"] = data["user_id"]
            data["name"] = data["nickname"]
        return {"type": "node", "data": data}

    async def send_forward(
        self,
        client: Any,
        *,
        nodes: list[dict[str, Any]],
        group_id: str = "",
        user_id: str = "",
    ) -> bool:
        """发送合并转发消息，成功返回 True。不支持时返回 False 由调用方降级。"""
        if client is None or not nodes or not self.capability.forward_supported:
            return False
        gid = clean_text(group_id)
        uid = clean_text(user_id)
        if gid:
            result = await self.call(
                client,
                "send_group_forward_msg",
                group_id=int(gid) if gid.isdigit() else gid,
                messages=nodes,
            )
        elif uid:
            result = await self.call(
                client,
                "send_private_forward_msg",
                user_id=int(uid) if uid.isdigit() else uid,
                messages=nodes,
            )
        else:
            return False
        self.capability.note_forward_result(result is not None)
        return result is not None
