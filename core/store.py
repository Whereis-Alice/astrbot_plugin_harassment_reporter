from __future__ import annotations

import time
from typing import Any, Protocol

from .text import clean_text, truncate

WATCHLIST_STORAGE_KEY = "watchlist_v1"
WARN_CACHE_STORAGE_KEY = "warned_sessions_v1"
COOLDOWN_STORAGE_KEY = "report_cooldown_v1"
RATE_STORAGE_KEY = "rate_counter_v1"
TICKET_STORAGE_KEY = "feedback_tickets_v1"


class KVHost(Protocol):
    """AstrBot Star 自带的键值存储接口。"""

    async def get_kv_data(self, key: str, default: Any = None) -> Any: ...

    async def put_kv_data(self, key: str, value: Any) -> Any: ...

    async def delete_kv_data(self, key: str) -> Any: ...


class Store:
    """插件所有持久化数据的读写入口。

    这里统一封装 AstrBot 的插件级键值库，避免各功能模块各写一套存取逻辑。
    """

    def __init__(self, host: KVHost) -> None:
        self._host = host

    async def _get_dict(self, key: str) -> dict[str, Any]:
        try:
            data = await self._host.get_kv_data(key, {})
        except Exception:
            return {}
        return dict(data) if isinstance(data, dict) else {}

    async def _put(self, key: str, value: Any) -> None:
        try:
            await self._host.put_kv_data(key, value)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 观察名单元数据（可编辑字段存在配置里，统计字段存在这里）
    # ------------------------------------------------------------------
    async def get_watchlist_meta(self) -> dict[str, Any]:
        return await self._get_dict(WATCHLIST_STORAGE_KEY)

    async def put_watchlist_meta(self, meta: dict[str, Any]) -> None:
        await self._put(WATCHLIST_STORAGE_KEY, meta)

    async def get_legacy_watchlist(self) -> dict[str, Any]:
        return await self._get_dict(WATCHLIST_STORAGE_KEY)

    # ------------------------------------------------------------------
    # 已警告记录（先警告再上报策略用）
    # ------------------------------------------------------------------
    async def get_warned(self) -> dict[str, float]:
        raw = await self._get_dict(WARN_CACHE_STORAGE_KEY)
        result: dict[str, float] = {}
        for key, value in raw.items():
            try:
                result[str(key)] = float(value)
            except Exception:
                continue
        return result

    async def put_warned(self, warned: dict[str, float]) -> None:
        await self._put(WARN_CACHE_STORAGE_KEY, warned)

    async def prune_warned(self, memory_seconds: int) -> dict[str, float]:
        warned = await self.get_warned()
        if memory_seconds <= 0:
            if warned:
                await self.put_warned({})
            return {}
        cutoff = time.time() - memory_seconds
        pruned = {key: ts for key, ts in warned.items() if ts >= cutoff}
        if len(pruned) != len(warned):
            await self.put_warned(pruned)
        return pruned

    async def was_warned(self, key: str, memory_seconds: int) -> bool:
        return key in await self.prune_warned(memory_seconds)

    async def mark_warned(self, key: str, memory_seconds: int) -> None:
        warned = await self.prune_warned(memory_seconds)
        warned[key] = time.time()
        await self.put_warned(warned)

    async def clear_warned(self, key: str, memory_seconds: int) -> None:
        warned = await self.prune_warned(memory_seconds)
        if key in warned:
            warned.pop(key, None)
            await self.put_warned(warned)

    # ------------------------------------------------------------------
    # 冷却（持久化，重启不丢）
    # ------------------------------------------------------------------
    async def cooldown_remaining(self, channel: str, session_id: str, cooldown: int) -> int:
        if cooldown <= 0:
            return 0
        data = await self._get_dict(COOLDOWN_STORAGE_KEY)
        try:
            last_at = float(data.get(f"{channel}|{session_id}", 0) or 0)
        except Exception:
            last_at = 0.0
        remaining = int(cooldown - (time.time() - last_at))
        return remaining if remaining > 0 else 0

    async def mark_cooldown(self, channel: str, session_id: str) -> None:
        data = await self._get_dict(COOLDOWN_STORAGE_KEY)
        now = time.time()
        data[f"{channel}|{session_id}"] = now
        if len(data) > 400:
            kept = sorted(data.items(), key=lambda item: float(item[1] or 0), reverse=True)[:200]
            data = dict(kept)
        await self._put(COOLDOWN_STORAGE_KEY, data)

    # ------------------------------------------------------------------
    # 每小时发送上限（防止把主人淹掉）
    # ------------------------------------------------------------------
    async def rate_limited(self, channel: str, limit: int) -> tuple[bool, int]:
        """返回 (是否超限, 最近一小时已发送条数)。"""
        if limit <= 0:
            return False, 0
        data = await self._get_dict(RATE_STORAGE_KEY)
        cutoff = time.time() - 3600
        stamps = [float(x) for x in data.get(channel, []) if isinstance(x, (int, float))]
        stamps = [ts for ts in stamps if ts >= cutoff]
        return len(stamps) >= limit, len(stamps)

    async def mark_rate(self, channel: str) -> None:
        data = await self._get_dict(RATE_STORAGE_KEY)
        cutoff = time.time() - 3600
        stamps = [float(x) for x in data.get(channel, []) if isinstance(x, (int, float))]
        stamps = [ts for ts in stamps if ts >= cutoff]
        stamps.append(time.time())
        data[channel] = stamps[-500:]
        await self._put(RATE_STORAGE_KEY, data)

    # ------------------------------------------------------------------
    # 反馈工单
    # ------------------------------------------------------------------
    async def get_tickets(self) -> dict[str, dict[str, Any]]:
        raw = await self._get_dict(TICKET_STORAGE_KEY)
        return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}

    async def put_tickets(self, tickets: dict[str, dict[str, Any]]) -> None:
        await self._put(TICKET_STORAGE_KEY, tickets)

    async def save_ticket(self, ticket_id: str, payload: dict[str, Any], max_entries: int) -> None:
        tickets = await self.get_tickets()
        tickets[ticket_id] = payload
        if len(tickets) > max_entries:
            kept = sorted(
                tickets.items(),
                key=lambda item: float(item[1].get("created_at", 0) or 0),
                reverse=True,
            )[:max_entries]
            tickets = dict(kept)
        await self.put_tickets(tickets)

    async def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        tickets = await self.get_tickets()
        wanted = clean_text(ticket_id).upper()
        if wanted in tickets:
            return tickets[wanted]
        # 允许只输入后 4 位短码
        matched = [key for key in tickets if key.upper().endswith(wanted)]
        if len(matched) == 1:
            return tickets[matched[0]]
        return None

    async def resolve_ticket_id(self, ticket_id: str) -> str:
        tickets = await self.get_tickets()
        wanted = clean_text(ticket_id).upper()
        if wanted in tickets:
            return wanted
        matched = [key for key in tickets if key.upper().endswith(wanted)]
        if len(matched) == 1:
            return matched[0]
        return ""

    async def update_ticket(self, ticket_id: str, **changes: Any) -> bool:
        tickets = await self.get_tickets()
        if ticket_id not in tickets:
            return False
        entry = tickets[ticket_id]
        for key, value in changes.items():
            if key == "reply":
                entry[key] = truncate(clean_text(value), 500)
            else:
                entry[key] = value
        tickets[ticket_id] = entry
        await self.put_tickets(tickets)
        return True

    async def clear_tickets(self) -> None:
        await self.put_tickets({})
