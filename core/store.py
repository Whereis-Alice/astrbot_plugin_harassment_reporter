from __future__ import annotations

import time
from typing import Any, Protocol

from .text import clean_text, truncate

# 观察名单的统计字段（上报次数、最后时间等）。
# 1.x 曾把整份名单存在 watchlist_v1 里，2.0 起可编辑字段搬进了插件配置，
# 但统计字段一直沿用同一个 key —— 于是「旧名单」和「新统计」变得无法区分，
# 名单在 WebUI 里被删空后又会被统计数据重新灌回配置（2.1.1 修复）。
# 现在统计单独存一个 key，watchlist_v1 只作为一次性的旧数据来源读取。
WATCHLIST_META_KEY = "watchlist_meta_v1"
WATCHLIST_LEGACY_KEY = "watchlist_v1"
WATCHLIST_MIGRATED_KEY = "watchlist_migrated_v1"
WARN_CACHE_STORAGE_KEY = "warned_sessions_v1"
COOLDOWN_STORAGE_KEY = "report_cooldown_v1"
RATE_STORAGE_KEY = "rate_counter_v1"
CONTACT_STORAGE_KEY = "feedback_contacts_v1"


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

    async def _get_list(self, key: str) -> list[dict[str, Any]]:
        try:
            data = await self._host.get_kv_data(key, [])
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [dict(item) for item in data if isinstance(item, dict)]

    async def _put(self, key: str, value: Any) -> None:
        try:
            await self._host.put_kv_data(key, value)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 观察名单（可编辑字段存在插件配置里，统计字段存在这里）
    # ------------------------------------------------------------------
    async def get_watchlist_meta(self) -> dict[str, Any]:
        return await self._get_dict(WATCHLIST_META_KEY)

    async def put_watchlist_meta(self, meta: dict[str, Any]) -> None:
        await self._put(WATCHLIST_META_KEY, meta)

    async def get_legacy_watchlist(self) -> dict[str, Any]:
        """读 1.x 遗留的整份名单。只在迁移标记还没置位时读一次。"""
        return await self._get_dict(WATCHLIST_LEGACY_KEY)

    async def drop_legacy_watchlist(self) -> None:
        """迁移完成后删掉旧 key，免得它以后又被当成名单读回来。"""
        try:
            await self._host.delete_kv_data(WATCHLIST_LEGACY_KEY)
        except Exception:
            pass

    async def watchlist_migrated(self) -> bool:
        """名单是否已经完成迁移。置位之后，插件配置就是唯一真源。"""
        try:
            return bool(await self._host.get_kv_data(WATCHLIST_MIGRATED_KEY, False))
        except Exception:
            return False

    async def mark_watchlist_migrated(self) -> None:
        await self._put(WATCHLIST_MIGRATED_KEY, True)

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
    # 最近联系记录
    # ------------------------------------------------------------------
    # 这里不做工单系统，只是一本「最近谁通过我找过你」的簿子：
    # 列表按时间倒序，第 1 条就是最近一次，主人用 /hr_back 回话时按序号取。
    async def get_contacts(self) -> list[dict[str, Any]]:
        return await self._get_list(CONTACT_STORAGE_KEY)

    async def put_contacts(self, contacts: list[dict[str, Any]]) -> None:
        await self._put(CONTACT_STORAGE_KEY, contacts)

    async def add_contact(self, payload: dict[str, Any], max_entries: int) -> None:
        contacts = await self.get_contacts()
        contacts.insert(0, payload)
        await self.put_contacts(contacts[: max(1, max_entries)])

    async def mark_contact_replied(self, index: int, reply: str) -> bool:
        contacts = await self.get_contacts()
        if index < 0 or index >= len(contacts):
            return False
        contacts[index]["reply"] = truncate(clean_text(reply), 500)
        contacts[index]["replied_at"] = time.time()
        await self.put_contacts(contacts)
        return True

    async def clear_contacts(self) -> None:
        await self.put_contacts([])
