"""QQ 官方 Bot 的发送和可见消息。OpenID 不等同于 QQ 号。"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.platform.astr_message_event import MessageSesion

from .attribution import authored_text
from .eventinfo import (
    group_id,
    message_id,
    message_timestamp,
    platform_id,
    platform_name,
    sender_id,
    sender_name,
    session_id,
)
from .images import KIND_BASE64, KIND_FILE, KIND_URL, ImageRef, from_event
from .onebot import ChatLine
from .text import clean_text, truncate

OFFICIAL_NAMES = {"qq_official", "qq_official_webhook"}
OPENID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
MAX_SESSIONS = 256
MAX_MESSAGES = 60


def is_official_event(event: Any) -> bool:
    return platform_name(event) in OFFICIAL_NAMES


def field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def receipt_id(value: Any) -> str:
    result = field(value, "id")
    if type(result) not in (str, int):
        return ""
    return clean_text(result) if clean_text(result) != "0" else ""


@dataclass
class OfficialOutcome:
    ok: bool = False
    detail: str = ""
    images_sent: int = 0
    card_sent: bool = False


class QQOfficialBridge:
    def __init__(self, context: Any, settings: Any) -> None:
        self.context, self.settings = context, settings
        self._routes: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._history: dict[str, list[ChatLine]] = {}
        self._lock = asyncio.Lock()
        self._sequence = 0

    def close(self) -> None:
        self._routes.clear()
        self._history.clear()

    def platform(self, target: str) -> Any | None:
        try:
            session = MessageSesion.from_str(target)
            getter = getattr(self.context, "get_platform_inst", None)
            inst = getter(session.platform_name) if callable(getter) else None
            if inst is not None and inst.meta().name in OFFICIAL_NAMES:
                return inst
        except Exception:
            pass
        return None

    def account(self, event: Any) -> str:
        inst = self.platform(session_id(event))
        appid = clean_text(getattr(inst, "appid", ""))
        return f"{platform_id(event)}|official:{appid or platform_name(event)}"

    @staticmethod
    def c2c_event(event: Any) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        uid = clean_text(field(field(raw, "author"), "user_openid"))
        return (
            is_official_event(event)
            and not group_id(event)
            and uid == sender_id(event)
            and bool(OPENID.fullmatch(uid))
        )

    def observe(self, event: Any) -> None:
        if not is_official_event(event):
            return
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        sid = session_id(event)
        if field(raw, "group_openid") and clean_text(
            field(raw, "group_openid")
        ) == group_id(event):
            scene = "group"
        elif self.c2c_event(event):
            scene = "c2c"
        elif field(raw, "guild_id") and not group_id(event):
            scene = "guild_dm"
        elif field(raw, "channel_id") and group_id(event):
            scene = "channel"
        else:
            return
        self._routes[sid] = {
            "scene": scene,
            "msg_id": message_id(event),
            "at": time.time(),
            "guild_id": clean_text(field(raw, "guild_id")),
        }
        self._routes.move_to_end(sid)
        while len(self._routes) > MAX_SESSIONS:
            old, _ = self._routes.popitem(last=False)
            self._history.pop(old, None)
        rows = self._history.setdefault(sid, [])
        mid = message_id(event)
        if mid and any(
            line.message_id == mid and line.sender_id == sender_id(event)
            for line in rows[-3:]
        ):
            return
        rows.append(
            ChatLine(
                sender_name=sender_name(event),
                sender_id=sender_id(event),
                text=truncate(authored_text(event), 220) or "[无可读文本]",
                timestamp=message_timestamp(event),
                message_id=mid,
                images=[ref.url for ref in from_event(event, limit=4) if ref.url],
            )
        )
        del rows[:-MAX_MESSAGES]

    def recent(self, event: Any, count: int) -> list[ChatLine]:
        self.observe(event)
        rows = self._history.get(session_id(event), [])
        cutoff = time.time() - 3600
        return [
            line for line in rows if not line.timestamp or line.timestamp >= cutoff
        ][-count:]

    def _next_seq(self) -> int:
        self._sequence = self._sequence % 10000 + 1
        return self._sequence

    async def _invoke(
        self, api: Any, scene: str, identity: str, route: dict[str, Any], **payload: Any
    ) -> Any:
        if scene == "c2c":
            # C2C 主动发送不借用其他会话的被动回复凭证。
            return await api.post_c2c_message(
                openid=identity, msg_seq=self._next_seq(), **payload
            )
        mid = route.get("msg_id") if time.time() - route.get("at", 0) < 300 else ""
        if mid:
            payload["msg_id"] = mid
        if scene == "group":
            return await api.post_group_message(
                group_openid=identity, msg_seq=self._next_seq(), **payload
            )
        if scene == "guild_dm":
            return await api.post_dms(guild_id=route["guild_id"], **payload)
        return await api.post_message(channel_id=identity, **payload)

    async def _image(
        self, api: Any, scene: str, identity: str, route: dict[str, Any], ref: ImageRef
    ) -> Any:
        if scene in {"channel", "guild_dm"}:
            if ref.kind == KIND_URL:
                return await self._invoke(api, scene, identity, route, image=ref.value)
            data = await self._image_data(ref)
            return await self._invoke(
                api,
                scene,
                identity,
                route,
                file_image=base64.b64decode(data, validate=True),
            )
        from botpy.http import Route

        payload: dict[str, Any] = {"file_type": 1, "srv_send_msg": False}
        if ref.kind == KIND_URL:
            payload["url"] = ref.value
        else:
            payload["file_data"] = await self._image_data(ref)
        if scene == "group":
            endpoint = Route(
                "POST", "/v2/groups/{group_openid}/files", group_openid=identity
            )
        else:
            endpoint = Route("POST", "/v2/users/{openid}/files", openid=identity)
        media = await api._http.request(endpoint, json=payload)
        info = clean_text(field(media, "file_info"))
        if not info:
            raise ValueError("官方图片上传没有返回 file_info")
        return await self._invoke(
            api, scene, identity, route, msg_type=7, media={"file_info": info}
        )

    async def _image_data(self, ref: ImageRef) -> str:
        limit = self.settings.courier_image_max_mb * 1024 * 1024
        if ref.kind == KIND_BASE64:
            if len(ref.value) > (limit + 2) // 3 * 4:
                raise ValueError("图片超过大小限制")
            base64.b64decode(ref.value, validate=True)
            return ref.value
        path = Path(ref.value)
        if path.stat().st_size > limit:
            raise ValueError("图片超过大小限制")
        return base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode(
            "ascii"
        )

    async def send(
        self,
        target: str,
        text: str,
        *,
        card: str | None = None,
        images: list[ImageRef] | None = None,
    ) -> OfficialOutcome:
        inst = self.platform(target)
        if inst is None:
            return OfficialOutcome(detail="找不到对应的 QQ 官方平台。")
        session = MessageSesion.from_str(target)
        identity = session.session_id
        route = self._routes.get(target)
        if route is None:
            if session.message_type.value != "FriendMessage" or not OPENID.fullmatch(
                identity
            ):
                return OfficialOutcome(
                    detail="尚未收到这个官方群或频道的消息。请先在目标会话与 Bot 互动，再绑定或回复。"
                )
            route = {"scene": "c2c", "at": 0}
        scene = route["scene"]
        client = inst.get_client()
        api = getattr(client, "api", None)
        if api is None:
            return OfficialOutcome(detail="QQ 官方平台客户端尚未就绪。")
        async with self._lock:
            try:
                response = await asyncio.wait_for(
                    self._invoke(
                        api,
                        scene,
                        identity,
                        route,
                        content=text,
                        **({"msg_type": 0} if scene in {"group", "c2c"} else {}),
                    ),
                    timeout=self.settings.courier_timeout_seconds,
                )
                if not receipt_id(response):
                    return OfficialOutcome(
                        detail="官方接口没有返回有效发送回执，结果无法确认；不要自动重试。"
                    )
                text_message_id = receipt_id(response)
            except Exception as exc:
                logger.warning(
                    "[HarassmentReporter] QQ 官方文字发送未确认：%s", type(exc).__name__
                )
                return OfficialOutcome(
                    detail="QQ 官方发送未确认。请检查 OpenID、平台在线状态和主动消息权限；群回复也可能已超过有效期。不要自动重试。"
                )
            result = OfficialOutcome(ok=True)
            refs = ([(True, ImageRef(KIND_FILE, card))] if card else []) + [
                (False, ref) for ref in (images or [])
            ]
            for is_card, ref in refs:
                try:
                    response = await asyncio.wait_for(
                        self._image(api, scene, identity, route, ref),
                        timeout=self.settings.courier_timeout_seconds,
                    )
                    if not receipt_id(response):
                        raise ValueError("图片没有有效回执")
                    if is_card:
                        result.card_sent = True
                    else:
                        result.images_sent += 1
                except Exception as exc:
                    logger.warning(
                        "[HarassmentReporter] QQ 官方正文已发，附图未确认：%s",
                        type(exc).__name__,
                    )
                    result.detail = "正文已送达，部分附图未能确认送达。"
                    break
            rows = self._history.get(target)
            if rows is not None:
                rows.append(
                    ChatLine(
                        sender_id="qq_official",
                        sender_name=self.settings.bot_self_name or "我",
                        text=truncate(text, 220),
                        timestamp=time.time(),
                        is_self=True,
                        message_id=text_message_id,
                    )
                )
                del rows[:-MAX_MESSAGES]
            return result
