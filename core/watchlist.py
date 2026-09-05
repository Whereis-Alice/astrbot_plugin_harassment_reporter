from __future__ import annotations

import time
from typing import Any

from .text import clean_text, format_ts, truncate

TEMPLATE_KEY = "watch_target"
NOTE_LIMIT = 200


class Watchlist:
    """观察名单管理。

    采用双存储：
    - 可编辑字段（备注、是否启用等）写进插件配置，方便在 WebUI 里直接改；
    - 统计字段（上报次数、最后时间等）写进插件键值库，避免污染配置界面。

    关于「启用」开关：取消勾选后条目会被保留，但不再自动更新统计，
    也不会在名单满时被自动清理掉——它代表「这条是我手动管理的，别动」。
    """

    def __init__(self, settings: Any, store: Any) -> None:
        self._settings = settings
        self._store = store

    # ------------------------------------------------------------------
    # 配置侧
    # ------------------------------------------------------------------
    def _config_rows(self) -> list[dict[str, Any]]:
        raw = self._settings.get("watchlist_entries", [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
        template_key = clean_text(row.get("__template_key"))
        if template_key and template_key != TEMPLATE_KEY:
            return None

        sender_id = clean_text(row.get("sender_id"))
        platform_id = clean_text(row.get("platform_id"))
        key = clean_text(row.get("key"))

        if not key and sender_id:
            key = f"{platform_id or 'default'}:{sender_id}"
        if not key:
            return None
        if not sender_id and ":" in key:
            sender_id = key.split(":", 1)[1]
        if not platform_id and ":" in key:
            platform_id = key.split(":", 1)[0]

        return {
            "key": key,
            "sender_id": sender_id or "unknown",
            "sender_name": clean_text(row.get("sender_name"), "未知用户"),
            "platform_id": platform_id or "unknown",
            "note": truncate(clean_text(row.get("note")), NOTE_LIMIT),
            "enabled": bool(row.get("enabled", True)),
        }

    def _config_to_dict(self) -> dict[str, dict[str, Any]]:
        """读出配置里的全部条目。

        注意这里必须保留 enabled=False 的行：所有写回操作都是整份覆写，
        一旦在读取阶段过滤掉停用条目，管理员在 WebUI 取消勾选的行会在
        下一次上报时被永久抹掉。
        """
        result: dict[str, dict[str, Any]] = {}
        for raw_row in self._config_rows():
            row = self._normalize_row(raw_row)
            if row is None:
                continue
            key = row.pop("key")
            result[key] = row
        return result

    def _save_config(self, watchlist: dict[str, dict[str, Any]]) -> None:
        rows = []
        ordered = sorted(
            watchlist.items(),
            key=lambda item: (clean_text(item[1].get("sender_name")), item[0]),
        )
        for key, entry in ordered:
            rows.append(
                {
                    "__template_key": TEMPLATE_KEY,
                    "key": key,
                    "sender_id": clean_text(entry.get("sender_id"), "unknown"),
                    "sender_name": clean_text(entry.get("sender_name"), "未知用户"),
                    "platform_id": clean_text(entry.get("platform_id"), "unknown"),
                    "note": truncate(clean_text(entry.get("note")), NOTE_LIMIT),
                    "enabled": bool(entry.get("enabled", True)),
                }
            )
        self._settings.set("watchlist_entries", rows)

    # ------------------------------------------------------------------
    # 启动迁移
    # ------------------------------------------------------------------
    async def migrate(self) -> None:
        """把旧版本只存在键值库里的名单搬到配置里，只需要执行一次。"""
        if not self._settings.available:
            return

        rows = self._config_rows()
        if rows:
            normalized = {}
            for raw_row in rows:
                row = self._normalize_row(raw_row)
                if row is None:
                    continue
                key = row.pop("key")
                normalized[key] = row
            if normalized:
                self._save_config(normalized)
            self._clear_legacy_snapshot()
            return

        legacy = await self._store.get_legacy_watchlist()
        if not legacy:
            self._clear_legacy_snapshot()
            return

        migrated: dict[str, dict[str, Any]] = {}
        for key, entry in legacy.items():
            if not isinstance(entry, dict):
                continue
            migrated[str(key)] = {
                "sender_id": clean_text(entry.get("sender_id"), "unknown"),
                "sender_name": clean_text(entry.get("sender_name"), "未知用户"),
                "platform_id": clean_text(entry.get("platform_id"), "unknown"),
                "note": truncate(clean_text(entry.get("last_reason")), NOTE_LIMIT),
                "enabled": True,
            }
        if migrated:
            self._save_config(migrated)
        self._clear_legacy_snapshot()

    def _clear_legacy_snapshot(self) -> None:
        if self._settings.get("watchlist_snapshot") not in (None, ""):
            self._settings.set("watchlist_snapshot", "")

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    async def all(self) -> dict[str, dict[str, Any]]:
        base = self._config_to_dict()
        meta_all = await self._store.get_watchlist_meta()
        merged: dict[str, dict[str, Any]] = {}
        for key, row in base.items():
            meta = meta_all.get(key) if isinstance(meta_all.get(key), dict) else {}
            merged[key] = {
                **row,
                "enabled": bool(row.get("enabled", True)),
                "group_id": clean_text(meta.get("group_id")),
                "last_session_id": clean_text(meta.get("last_session_id")),
                "first_reported_at": _as_float(meta.get("first_reported_at")),
                "last_reported_at": _as_float(meta.get("last_reported_at")),
                "report_count": _as_int(meta.get("report_count")),
                "last_reason": clean_text(meta.get("last_reason")) or row.get("note", ""),
                "last_severity": clean_text(meta.get("last_severity"), "unknown"),
            }
        return merged

    async def save(self, watchlist: dict[str, dict[str, Any]]) -> None:
        editable: dict[str, dict[str, Any]] = {}
        meta: dict[str, dict[str, Any]] = {}
        for key, raw_entry in watchlist.items():
            entry = raw_entry if isinstance(raw_entry, dict) else {}
            editable[key] = {
                "sender_id": clean_text(entry.get("sender_id"), "unknown"),
                "sender_name": clean_text(entry.get("sender_name"), "未知用户"),
                "platform_id": clean_text(entry.get("platform_id"), "unknown"),
                "note": truncate(
                    clean_text(entry.get("note")) or clean_text(entry.get("last_reason")),
                    NOTE_LIMIT,
                ),
                "enabled": bool(entry.get("enabled", True)),
            }
            meta[key] = {
                "group_id": clean_text(entry.get("group_id")),
                "last_session_id": clean_text(entry.get("last_session_id")),
                "first_reported_at": _as_float(entry.get("first_reported_at")),
                "last_reported_at": _as_float(entry.get("last_reported_at")),
                "report_count": _as_int(entry.get("report_count")),
                "last_reason": truncate(clean_text(entry.get("last_reason")), NOTE_LIMIT),
                "last_severity": clean_text(entry.get("last_severity"), "unknown"),
            }
        self._save_config(editable)
        await self._store.put_watchlist_meta(meta)

    async def get(self, key: str) -> dict[str, Any] | None:
        return (await self.all()).get(key)

    async def add_report(
        self,
        *,
        key: str,
        sender_id: str,
        sender_name: str,
        platform_id: str,
        group_id: str,
        session_id: str,
        reason: str,
        severity: str,
    ) -> None:
        if not self._settings.watchlist_auto_add:
            return

        watchlist = await self.all()
        now_ts = time.time()
        existing = watchlist.get(key, {})
        if existing and not existing.get("enabled", True):
            # 管理员手动停用了这条，保留原样，不再刷新统计。
            return
        watchlist[key] = {
            **existing,
            "sender_id": sender_id or "unknown",
            "sender_name": sender_name or "未知用户",
            "platform_id": platform_id or "unknown",
            "group_id": group_id,
            "last_session_id": session_id,
            "first_reported_at": _as_float(existing.get("first_reported_at")) or now_ts,
            "last_reported_at": now_ts,
            "report_count": _as_int(existing.get("report_count")) + 1,
            "last_reason": truncate(reason, NOTE_LIMIT),
            "last_severity": severity,
            "enabled": True,
        }

        max_entries = self._settings.watchlist_max_entries
        if len(watchlist) > max_entries:
            kept = sorted(
                watchlist.items(),
                key=lambda item: self.trim_key(item[1]),
                reverse=True,
            )[:max_entries]
            watchlist = dict(kept)
        await self.save(watchlist)

    async def remove(self, key: str) -> bool:
        watchlist = await self.all()
        if key not in watchlist:
            return False
        watchlist.pop(key, None)
        await self.save(watchlist)
        return True

    async def clear(self) -> None:
        await self.save({})

    async def resolve_key(self, target: str) -> str:
        """支持直接给完整键，也支持只给用户 ID。"""
        target = clean_text(target)
        if not target:
            return ""
        watchlist = await self.all()
        if target in watchlist:
            return target
        matched = [
            key
            for key, entry in watchlist.items()
            if clean_text(entry.get("sender_id")) == target or key.endswith(f":{target}")
        ]
        if len(matched) == 1:
            return matched[0]
        return target

    # ------------------------------------------------------------------
    # 排序
    # ------------------------------------------------------------------
    @staticmethod
    def sort_key(entry: dict[str, Any]) -> tuple[float, int]:
        """展示排序：最近上报的在前，时间相同时次数多的在前。"""
        return (_as_float(entry.get("last_reported_at")), _as_int(entry.get("report_count")))

    @staticmethod
    def trim_key(entry: dict[str, Any]) -> tuple[int, float, int]:
        """名单超上限时的保留优先级：手动停用的条目永不被自动清理。"""
        manual = 0 if entry.get("enabled", True) else 1
        return (manual, _as_float(entry.get("last_reported_at")), _as_int(entry.get("report_count")))

    @staticmethod
    def format_entry(key: str, entry: dict[str, Any]) -> str:
        note = truncate(clean_text(entry.get("note")), 80)
        state = "" if entry.get("enabled", True) else "  ⏸ 已停用（保留记录，不再自动更新）\n"
        return (
            f"- {key}\n"
            f"{state}"
            f"  用户：{clean_text(entry.get('sender_name'), '未知用户')} "
            f"({clean_text(entry.get('sender_id'), 'unknown')}) | "
            f"平台：{clean_text(entry.get('platform_id'), 'unknown')}\n"
            f"  次数：{_as_int(entry.get('report_count'))} | "
            f"最后严重度：{clean_text(entry.get('last_severity'), 'unknown')} | "
            f"最后时间：{format_ts(entry.get('last_reported_at'))}\n"
            f"  最后原因：{truncate(clean_text(entry.get('last_reason')), 80)}\n"
            f"  备注：{note or '-'}"
        )


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
