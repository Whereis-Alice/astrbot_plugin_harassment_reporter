from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.persona_error_reply import resolve_event_conversation_persona_id

WATCHLIST_STORAGE_KEY = "watchlist_v1"
WARN_CACHE_STORAGE_KEY = "warned_sessions_v1"

TOOL_RESPONSE_MODES = {
    "silent",
    "warn_only",
    "warn_once_then_report",
    "report_then_silent",
    "report_then_inform",
}
OWNER_REPORT_STYLES = {
    "structured",
    "persona_natural",
}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_ts(timestamp: float | int | None) -> str:
    if not timestamp:
        return "-"
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def _clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _normalize_context_line(line: str) -> str:
    text = _clean_text(line)
    if text.startswith("User: "):
        return "用户: " + text[6:]
    if text.startswith("Assistant: "):
        return "助手: " + text[11:]
    return text


def _message_chain_to_text(chain: MessageChain | None) -> str:
    if chain is None:
        return ""
    try:
        return chain.get_plain_text().strip()
    except Exception:
        return ""


@dataclass
class HarassmentReportTool(FunctionTool[AstrAgentContext]):
    name: str = "report_harassment"
    description: str = (
        "当你在和用户聊天时感觉自己正在被骚扰、辱骂、挑衅、恶意消耗，"
        "或者对方持续让你明显不舒服时，使用这个工具上报给主人。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "简短说明为什么你觉得自己正在被骚扰。",
                },
                "severity": {
                    "type": "string",
                    "description": "严重程度。",
                    "enum": ["low", "medium", "high"],
                },
                "evidence": {
                    "type": "string",
                    "description": "可选。摘录关键内容或补充说明。",
                },
                "expected_help": {
                    "type": "string",
                    "description": "可选。希望主人如何介入。",
                },
            },
            "required": ["reason", "severity"],
        }
    )
    plugin: Any = Field(default=None)

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        if self.plugin is None:
            return "上报失败：插件实例未初始化。"

        event = context.context.event
        return await self.plugin.handle_tool_report(
            event=event,
            reason=_clean_text(kwargs.get("reason"), "模型认为当前对话存在骚扰风险"),
            severity=_clean_text(kwargs.get("severity"), "medium").lower(),
            evidence=_clean_text(kwargs.get("evidence")),
            expected_help=_clean_text(kwargs.get("expected_help")),
        )


@star.register(
    "astrbot_plugin_harassment_reporter",
    "Huli3",
    "让 LLM 在被骚扰时主动上报到指定会话，并支持附带近期摘要与观察名单",
    "1.2.4",
    "https://github.com/Whereis-Alice/astrbot_plugin_harassment_reporter",
)
class HarassmentReporterPlugin(star.Star):
    """给 LLM 提供一个可主动求助的骚扰上报工具。"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config
        self._tool = HarassmentReportTool(plugin=self)
        self.context.add_llm_tools(self._tool)
        self._last_report_at: dict[str, float] = {}

    async def initialize(self) -> None:
        await self._migrate_watchlist_to_config()
        await self._ensure_watchlist_template_keys()

    def _cfg(self, key: str, default: Any = None) -> Any:
        if self.config is None:
            return default
        return self.config.get(key, default)

    def _is_enabled(self) -> bool:
        return bool(self._cfg("enable", True))

    def _cooldown_seconds(self) -> int:
        try:
            return max(0, int(self._cfg("report_cooldown_seconds", 300)))
        except Exception:
            return 300

    def _report_target(self) -> str:
        return _clean_text(self._cfg("report_session_id", ""))

    def _is_report_receiver_session(self, event: AstrMessageEvent) -> bool:
        target = self._report_target()
        if not target:
            return False
        return _clean_text(event.unified_msg_origin, "unknown") == target

    def _can_view_watchlist(self, event: AstrMessageEvent) -> bool:
        return event.is_admin() or self._is_report_receiver_session(event)

    def _report_receiver_name(self) -> str:
        return _clean_text(self._cfg("report_receiver_name", ""), "主人")

    def _include_message_text(self) -> bool:
        return bool(self._cfg("include_message_text", True))

    def _max_excerpt_length(self) -> int:
        try:
            return max(50, int(self._cfg("max_excerpt_length", 300)))
        except Exception:
            return 300

    def _prompt_enabled(self) -> bool:
        return bool(self._cfg("inject_usage_prompt", True))

    def _recent_summary_enabled(self) -> bool:
        return bool(self._cfg("attach_recent_context_summary", True))

    def _recent_summary_lines(self) -> int:
        try:
            return max(1, int(self._cfg("recent_context_summary_lines", 6)))
        except Exception:
            return 6

    def _recent_summary_max_chars(self) -> int:
        try:
            return max(80, int(self._cfg("recent_context_summary_max_chars", 600)))
        except Exception:
            return 600

    def _watchlist_enabled(self) -> bool:
        return bool(self._cfg("auto_add_sender_to_watchlist", True))

    def _watchlist_max_entries(self) -> int:
        try:
            return max(1, int(self._cfg("watchlist_max_entries", 500)))
        except Exception:
            return 500

    def _get_config_watchlist_rows(self) -> list[dict[str, Any]]:
        raw = self._cfg("watchlist_entries", [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _normalize_watchlist_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        template_key = _clean_text(row.get("__template_key"))
        if template_key and template_key != "watch_target":
            return None

        sender_id = _clean_text(row.get("sender_id"))
        platform_id = _clean_text(row.get("platform_id"))
        key = _clean_text(row.get("key"))

        if not key and sender_id:
            key = f"{platform_id or 'default'}:{sender_id}"
        if not key:
            return None

        if not sender_id and ":" in key:
            sender_id = key.split(":", 1)[1]
        if not platform_id and ":" in key:
            platform_id = key.split(":", 1)[0]

        normalized = {
            "key": key,
            "sender_id": sender_id or "unknown",
            "sender_name": _clean_text(row.get("sender_name"), "未知用户"),
            "platform_id": platform_id or "unknown",
            "note": _truncate(_clean_text(row.get("note")), 200),
            "enabled": bool(row.get("enabled", True)),
        }
        return normalized

    def _config_watchlist_to_dict(self) -> dict[str, dict[str, Any]]:
        watchlist: dict[str, dict[str, Any]] = {}
        for raw_row in self._get_config_watchlist_rows():
            row = self._normalize_watchlist_row(raw_row)
            if row is None:
                continue
            key = row.pop("key")
            if not row.get("enabled", True):
                continue
            watchlist[key] = row
        return watchlist

    async def _save_config_watchlist(self, watchlist: dict[str, dict[str, Any]]) -> None:
        if self.config is None:
            return

        sorted_items = sorted(
            watchlist.items(),
            key=lambda item: (
                _clean_text(item[1].get("sender_name")),
                item[0],
            ),
        )
        rows = []
        for key, entry in sorted_items:
            rows.append(
                {
                    "__template_key": "watch_target",
                    "key": key,
                    "sender_id": _clean_text(entry.get("sender_id"), "unknown"),
                    "sender_name": _clean_text(entry.get("sender_name"), "未知用户"),
                    "platform_id": _clean_text(entry.get("platform_id"), "unknown"),
                    "note": _truncate(_clean_text(entry.get("note")), 200),
                    "enabled": bool(entry.get("enabled", True)),
                }
            )

        if self.config.get("watchlist_entries", []) == rows:
            return

        self.config["watchlist_entries"] = rows
        self.config.save_config()

    async def _migrate_watchlist_to_config(self) -> None:
        if self.config is None:
            return

        existing_rows = self._get_config_watchlist_rows()
        if existing_rows:
            normalized_dict = self._config_watchlist_to_dict()
            await self._save_config_watchlist(normalized_dict)
            return

        legacy_watchlist = await self.get_kv_data(WATCHLIST_STORAGE_KEY, {})
        if not isinstance(legacy_watchlist, dict) or not legacy_watchlist:
            if self.config.get("watchlist_snapshot") is not None:
                self.config["watchlist_snapshot"] = ""
                self.config.save_config()
            return

        migrated: dict[str, dict[str, Any]] = {}
        for key, entry in legacy_watchlist.items():
            if not isinstance(entry, dict):
                continue
            migrated[str(key)] = {
                "sender_id": _clean_text(entry.get("sender_id"), "unknown"),
                "sender_name": _clean_text(entry.get("sender_name"), "未知用户"),
                "platform_id": _clean_text(entry.get("platform_id"), "unknown"),
                "note": _truncate(_clean_text(entry.get("last_reason")), 200),
                "enabled": True,
            }
        await self._save_config_watchlist(migrated)

        if self.config.get("watchlist_snapshot") is not None:
            self.config["watchlist_snapshot"] = ""
            self.config.save_config()

    async def _ensure_watchlist_template_keys(self) -> None:
        if self.config is None:
            return

        rows = self._get_config_watchlist_rows()
        if not rows:
            return

        changed = False
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if _clean_text(item.get("__template_key")) != "watch_target":
                item["__template_key"] = "watch_target"
                changed = True
            normalized_rows.append(item)

        if changed:
            self.config["watchlist_entries"] = normalized_rows
            self.config.save_config()

    def _tool_response_mode(self) -> str:
        mode = _clean_text(self._cfg("tool_response_mode", "silent"), "silent")
        return mode if mode in TOOL_RESPONSE_MODES else "silent"

    def _natural_warn_enabled(self) -> bool:
        return bool(self._cfg("natural_language_warn_reply", True))

    def _natural_inform_enabled(self) -> bool:
        return bool(self._cfg("natural_language_report_reply", True))

    def _warn_once_inform_after_report(self) -> bool:
        return bool(self._cfg("warn_once_inform_after_report", False))

    def _owner_report_style(self) -> str:
        style = _clean_text(
            self._cfg("owner_report_style", "structured"),
            "structured",
        )
        return style if style in OWNER_REPORT_STYLES else "structured"

    def _owner_report_natural_enabled(self) -> bool:
        return bool(self._cfg("natural_language_report_to_owner", False))

    def _warn_memory_seconds(self) -> int:
        try:
            return max(0, int(self._cfg("warn_once_memory_seconds", 1800)))
        except Exception:
            return 1800

    def _severity_text(self, severity: str) -> str:
        mapping = {
            "low": "低",
            "medium": "中",
            "high": "高",
        }
        return mapping.get(severity, severity or "未知")

    def _platform_id(self, event: AstrMessageEvent) -> str:
        return _clean_text(getattr(event, "get_platform_id", lambda: "")(), "unknown")

    def _group_id(self, event: AstrMessageEvent) -> str:
        return _clean_text(getattr(event, "get_group_id", lambda: "")())

    def _watch_key(self, event: AstrMessageEvent) -> str:
        return f"{self._platform_id(event)}:{_clean_text(event.get_sender_id(), 'unknown')}"

    def _warn_cache_key(self, event: AstrMessageEvent) -> str:
        session_id = _clean_text(event.unified_msg_origin, "unknown")
        return f"{session_id}|{self._watch_key(event)}"

    def _build_template_values(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> dict[str, str]:
        sender_name = _clean_text(event.get_sender_name(), "对方")
        sender_id = _clean_text(event.get_sender_id(), "unknown")
        receiver_name = self._report_receiver_name()
        message_text = _clean_text(event.message_str, "")
        return {
            "receiver_name": receiver_name,
            "reason": reason,
            "severity": severity,
            "severity_text": self._severity_text(severity),
            "sender_name": sender_name,
            "sender_id": sender_id,
            "message_text": _truncate(message_text, self._max_excerpt_length()),
            "evidence": _truncate(evidence, self._max_excerpt_length()),
            "expected_help": _truncate(expected_help, 120),
        }

    def _render_template(self, template: str, values: dict[str, str]) -> str:
        try:
            return template.format(**values)
        except Exception:
            return template

    def _warning_template(self) -> str:
        custom = _clean_text(self._cfg("warn_message_template", ""))
        if custom:
            return custom
        return "请立刻停止这种行为。你再这样，我就要告诉{receiver_name}你在骚扰我了。"

    def _report_inform_template(self) -> str:
        custom = _clean_text(self._cfg("report_inform_template", ""))
        if custom:
            return custom
        return "我已经把你刚才的行为报告给{receiver_name}了。"

    def _tool_warning_instruction(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> str:
        values = self._build_template_values(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
        )
        template = self._warning_template()
        rendered = self._render_template(template, values)
        if self._natural_warn_enabled():
            return (
                "不要提到工具调用，也不要说你收到了系统提示。"
                "请基于你当前的人设，用自然语言向对方发出明确警告，"
                f"核心意思要包含：{rendered}"
            )
        return rendered

    def _tool_report_inform_instruction(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> str:
        values = self._build_template_values(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
        )
        template = self._report_inform_template()
        rendered = self._render_template(template, values)
        if self._natural_inform_enabled():
            return (
                "不要提到工具调用。"
                "请基于你当前的人设，用自然语言向对方表达你已经进行了上报，"
                f"核心意思要包含：{rendered}"
            )
        return rendered

    def _silent_instruction(self) -> str:
        return (
            "上报已处理完毕。不要向当前用户透露你已经上报，"
            "不要提到工具调用，也不要输出机械性的确认语。"
            "你可以继续正常回复，或者在合适时自然结束当前对话。"
        )

    def _build_tool_result_text(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
        report_attempted: bool,
        report_succeeded: bool,
        was_warn_only: bool = False,
        warning_reason: str = "",
        error_message: str = "",
    ) -> str:
        if error_message:
            return error_message

        if was_warn_only:
            return self._tool_warning_instruction(
                event=event,
                reason=warning_reason or reason,
                severity=severity,
                evidence=evidence,
                expected_help=expected_help,
            )

        if not report_attempted:
            return self._silent_instruction()

        if not report_succeeded:
            return (
                "你刚刚尝试上报，但这次没有成功。"
                "不要编造自己已经通知过任何人，也不要提到工具调用失败。"
                "你可以根据当前人设自然地设定边界、拒绝继续被骚扰，或简短结束对话。"
            )

        mode = self._tool_response_mode()
        if mode == "report_then_inform" or (
            mode == "warn_once_then_report" and self._warn_once_inform_after_report()
        ):
            return self._tool_report_inform_instruction(
                event=event,
                reason=reason,
                severity=severity,
                evidence=evidence,
                expected_help=expected_help,
            )
        if mode == "warn_only":
            return self._tool_warning_instruction(
                event=event,
                reason=reason,
                severity=severity,
                evidence=evidence,
                expected_help=expected_help,
            )
        return self._silent_instruction()

    def _should_skip_by_cooldown(self, source_session_id: str) -> tuple[bool, int]:
        cooldown = self._cooldown_seconds()
        if cooldown <= 0:
            return False, 0

        last_at = self._last_report_at.get(source_session_id, 0.0)
        remaining = int(cooldown - (time.time() - last_at))
        if remaining > 0:
            return True, remaining
        return False, 0

    def _mark_reported(self, source_session_id: str) -> None:
        self._last_report_at[source_session_id] = time.time()

    async def _get_watchlist(self) -> dict[str, dict[str, Any]]:
        config_watchlist = self._config_watchlist_to_dict()
        metadata = await self.get_kv_data(WATCHLIST_STORAGE_KEY, {})
        metadata = metadata if isinstance(metadata, dict) else {}

        merged: dict[str, dict[str, Any]] = {}
        for key, row in config_watchlist.items():
            meta = metadata.get(key, {})
            meta = meta if isinstance(meta, dict) else {}
            merged[key] = {
                "sender_id": row.get("sender_id", "unknown"),
                "sender_name": row.get("sender_name", "未知用户"),
                "platform_id": row.get("platform_id", "unknown"),
                "note": row.get("note", ""),
                "enabled": bool(row.get("enabled", True)),
                "group_id": _clean_text(meta.get("group_id")),
                "last_session_id": _clean_text(meta.get("last_session_id")),
                "first_reported_at": float(meta.get("first_reported_at", 0) or 0),
                "last_reported_at": float(meta.get("last_reported_at", 0) or 0),
                "report_count": int(meta.get("report_count", 0) or 0),
                "last_reason": _clean_text(meta.get("last_reason")) or row.get("note", ""),
                "last_severity": _clean_text(meta.get("last_severity"), "unknown"),
            }
        return merged

    async def _save_watchlist(self, watchlist: dict[str, dict[str, Any]]) -> None:
        normalized: dict[str, dict[str, Any]] = {}
        metadata: dict[str, dict[str, Any]] = {}

        for key, raw_entry in watchlist.items():
            entry = raw_entry if isinstance(raw_entry, dict) else {}
            normalized[key] = {
                "sender_id": _clean_text(entry.get("sender_id"), "unknown"),
                "sender_name": _clean_text(entry.get("sender_name"), "未知用户"),
                "platform_id": _clean_text(entry.get("platform_id"), "unknown"),
                "note": _truncate(
                    _clean_text(entry.get("note")) or _clean_text(entry.get("last_reason")),
                    200,
                ),
                "enabled": bool(entry.get("enabled", True)),
            }
            metadata[key] = {
                "group_id": _clean_text(entry.get("group_id")),
                "last_session_id": _clean_text(entry.get("last_session_id")),
                "first_reported_at": float(entry.get("first_reported_at", 0) or 0),
                "last_reported_at": float(entry.get("last_reported_at", 0) or 0),
                "report_count": int(entry.get("report_count", 0) or 0),
                "last_reason": _truncate(_clean_text(entry.get("last_reason")), 200),
                "last_severity": _clean_text(entry.get("last_severity"), "unknown"),
            }

        await self._save_config_watchlist(normalized)
        await self.put_kv_data(WATCHLIST_STORAGE_KEY, metadata)

    async def _get_warn_cache(self) -> dict[str, float]:
        data = await self.get_kv_data(WARN_CACHE_STORAGE_KEY, {})
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items()}
        return {}

    async def _save_warn_cache(self, warned: dict[str, float]) -> None:
        await self.put_kv_data(WARN_CACHE_STORAGE_KEY, warned)

    async def _cleanup_warn_cache(self, warned: dict[str, float]) -> dict[str, float]:
        memory_seconds = self._warn_memory_seconds()
        if memory_seconds <= 0:
            return {}
        cutoff = time.time() - memory_seconds
        return {
            key: ts
            for key, ts in warned.items()
            if float(ts) >= cutoff
        }

    async def _was_warned_before(self, event: AstrMessageEvent) -> bool:
        warned = await self._cleanup_warn_cache(await self._get_warn_cache())
        await self._save_warn_cache(warned)
        return self._warn_cache_key(event) in warned

    async def _mark_warned(self, event: AstrMessageEvent) -> None:
        warned = await self._cleanup_warn_cache(await self._get_warn_cache())
        warned[self._warn_cache_key(event)] = time.time()
        await self._save_warn_cache(warned)

    async def _clear_warned(self, event: AstrMessageEvent) -> None:
        warned = await self._cleanup_warn_cache(await self._get_warn_cache())
        key = self._warn_cache_key(event)
        if key in warned:
            del warned[key]
            await self._save_warn_cache(warned)

    async def _get_watch_entry(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        watchlist = await self._get_watchlist()
        return watchlist.get(self._watch_key(event))

    async def _add_sender_to_watchlist(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
    ) -> None:
        if not self._watchlist_enabled():
            return

        watchlist = await self._get_watchlist()
        key = self._watch_key(event)
        now_ts = time.time()
        entry = watchlist.get(key, {})

        report_count = int(entry.get("report_count", 0)) + 1
        watchlist[key] = {
            "sender_id": _clean_text(event.get_sender_id(), "unknown"),
            "sender_name": _clean_text(event.get_sender_name(), "未知用户"),
            "platform_id": self._platform_id(event),
            "group_id": self._group_id(event),
            "last_session_id": _clean_text(event.unified_msg_origin, "unknown"),
            "first_reported_at": float(entry.get("first_reported_at", now_ts)),
            "last_reported_at": now_ts,
            "report_count": report_count,
            "last_reason": _truncate(reason, 200),
            "last_severity": severity,
        }

        max_entries = self._watchlist_max_entries()
        if len(watchlist) > max_entries:
            sorted_items = sorted(
                watchlist.items(),
                key=lambda item: float(item[1].get("last_reported_at", 0)),
                reverse=True,
            )
            watchlist = dict(sorted_items[:max_entries])

        await self._save_watchlist(watchlist)

    async def _remove_watchlist_entry(self, key: str) -> bool:
        watchlist = await self._get_watchlist()
        if key not in watchlist:
            return False
        del watchlist[key]
        await self._save_watchlist(watchlist)
        return True

    async def _resolve_watchlist_key(self, value: str) -> str:
        target = _clean_text(value)
        if not target:
            return ""

        watchlist = await self._get_watchlist()
        if target in watchlist:
            return target

        matched_keys = [
            key
            for key, entry in watchlist.items()
            if _clean_text(entry.get("sender_id")) == target
        ]
        if len(matched_keys) == 1:
            return matched_keys[0]
        return target

    async def _clear_watchlist(self) -> None:
        await self._save_watchlist({})

    async def _build_recent_context_summary(self, event: AstrMessageEvent) -> str:
        if not self._recent_summary_enabled():
            return ""

        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is None:
                return ""

            cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
            if not cid:
                return ""

            lines, _ = await conv_mgr.get_human_readable_context(
                event.unified_msg_origin,
                cid,
                page=1,
                page_size=self._recent_summary_lines(),
            )
            if not lines:
                return ""

            max_chars = self._recent_summary_max_chars()
            normalized = [_normalize_context_line(line) for line in reversed(lines)]

            output_lines: list[str] = []
            used_chars = 0
            per_line_limit = max(40, min(180, max_chars // max(1, len(normalized))))
            for line in normalized:
                clipped = _truncate(line, per_line_limit)
                candidate = f"- {clipped}"
                if used_chars + len(candidate) + 1 > max_chars and output_lines:
                    break
                output_lines.append(candidate)
                used_chars += len(candidate) + 1

            return "\n".join(output_lines)
        except Exception as exc:
            logger.warning("[HarassmentReporter] failed to build recent context summary: %s", exc)
            return ""

    async def _resolve_active_persona_prompt(self, event: AstrMessageEvent) -> str:
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin).get(
                "provider_settings",
                {},
            )
            conversation_persona_id = await resolve_event_conversation_persona_id(
                event,
                self.context.conversation_manager,
            )
            _, persona, _, use_webchat_special_default = (
                await self.context.persona_manager.resolve_selected_persona(
                    umo=event.unified_msg_origin,
                    conversation_persona_id=conversation_persona_id,
                    platform_name=event.get_platform_name(),
                    provider_settings=cfg,
                )
            )
            if use_webchat_special_default:
                return ""
            if persona and persona.get("prompt"):
                return str(persona["prompt"]).strip()
        except Exception as exc:
            logger.warning("[HarassmentReporter] failed to resolve persona prompt: %s", exc)
        return ""

    async def _build_structured_owner_report_text(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> str:
        sender_name = _clean_text(event.get_sender_name(), "未知用户")
        sender_id = _clean_text(event.get_sender_id(), "unknown")
        session_id = _clean_text(event.unified_msg_origin, "unknown")
        platform_id = self._platform_id(event)
        message_text = _clean_text(event.message_str, "[无可读文本]")
        existing_watch = await self._get_watch_entry(event)
        recent_summary = await self._build_recent_context_summary(event)

        lines = [
            "【LLM 骚扰预警】",
            f"时间：{_now_text()}",
            f"严重程度：{self._severity_text(severity)} ({severity})",
            f"来源平台：{platform_id}",
            f"来源会话：{session_id}",
            f"发送者：{sender_name} ({sender_id})",
        ]

        group_id = self._group_id(event)
        if group_id:
            lines.append(f"群组 ID：{group_id}")

        lines.append(f"上报原因：{reason}")

        if existing_watch:
            lines.append(
                "观察名单：已存在该用户，"
                f"累计上报 {int(existing_watch.get('report_count', 0))} 次"
            )

        if evidence:
            lines.append(f"补充证据：{_truncate(evidence, self._max_excerpt_length())}")

        if expected_help:
            lines.append(f"期望处理：{_truncate(expected_help, 120)}")

        if self._include_message_text():
            lines.append(f"当前消息：{_truncate(message_text, self._max_excerpt_length())}")

        if recent_summary:
            lines.append("")
            lines.append("最近几轮聊天摘要：")
            lines.append(recent_summary)

        lines.append("")
        lines.append("说明：这是模型在对话中主动调用 `report_harassment` 工具发出的提醒。")
        return "\n".join(lines)

    async def _try_build_persona_owner_report_text(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> str:
        try:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
            if provider is None:
                return ""

            persona_prompt = await self._resolve_active_persona_prompt(event)
            structured_text = await self._build_structured_owner_report_text(
                event=event,
                reason=reason,
                severity=severity,
                evidence=evidence,
                expected_help=expected_help,
            )
            receiver_name = self._report_receiver_name()

            system_prompt = (
                "你要把一份骚扰上报整理成发给主人看的自然语言消息。"
                "保留事实准确，不要编造不存在的细节，不要改变严重程度，"
                "不要省略重要身份信息和来源信息。"
                "输出一段完整中文消息即可，不要加解释，不要提到你是工具。"
            )
            if persona_prompt:
                system_prompt += (
                    "\n\n# 当前人设口吻参考\n"
                    f"{persona_prompt}"
                )

            prompt = (
                f"请把下面这份结构化骚扰上报，改写成一段发给“{receiver_name}”看的自然语言消息。"
                "风格可以带有人设感，但仍然要清楚、可靠、便于主人理解情况。"
                "要明确说明是谁、在哪个会话、因为什么、严重程度如何，"
                "并在有证据、摘要、期望处理时自然带上。\n\n"
                f"{structured_text}"
            )
            response = await self.context.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=prompt,
                system_prompt=system_prompt,
                tools=None,
            )
            return _message_chain_to_text(response.result_chain) or _clean_text(
                response.completion_text
            )
        except Exception as exc:
            logger.warning(
                "[HarassmentReporter] failed to build persona-style owner report: %s",
                exc,
            )
            return ""

    async def _build_owner_report_text(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> str:
        style = self._owner_report_style()
        if style == "persona_natural" and self._owner_report_natural_enabled():
            natural_text = await self._try_build_persona_owner_report_text(
                event=event,
                reason=reason,
                severity=severity,
                evidence=evidence,
                expected_help=expected_help,
            )
            if natural_text:
                return natural_text

        return await self._build_structured_owner_report_text(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
        )

    async def _build_report_chain(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> MessageChain:
        report_text = await self._build_owner_report_text(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
        )
        return MessageChain().message(report_text)

    async def _send_report(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
        ignore_cooldown: bool = False,
    ) -> tuple[str, str]:
        if not self._is_enabled():
            return "disabled", "上报未执行：插件当前已禁用。"

        target_session_id = self._report_target()
        if not target_session_id:
            return "unconfigured", "上报未执行：还没有配置接收上报的会话 ID。"

        source_session_id = _clean_text(event.unified_msg_origin, "unknown")
        if not ignore_cooldown:
            skipped, remaining = self._should_skip_by_cooldown(source_session_id)
            if skipped:
                return "cooldown", f"上报已跳过：当前会话仍在冷却中，还需等待约 {remaining} 秒。"

        chain = await self._build_report_chain(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
        )

        ok = await self.context.send_message(target_session_id, chain)
        if not ok:
            return (
                "send_failed",
                "上报失败：AstrBot 没有找到这个会话对应的平台实例。"
                "请确认 `report_session_id` 是否正确。",
            )

        self._mark_reported(source_session_id)
        await self._add_sender_to_watchlist(
            event=event,
            reason=reason,
            severity=severity,
        )
        await self._clear_warned(event)
        logger.info(
            "[HarassmentReporter] report sent | source=%s target=%s severity=%s",
            source_session_id,
            target_session_id,
            severity,
        )
        return "ok", "上报已发送。"

    async def handle_tool_report(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> str:
        severity = severity if severity in {"low", "medium", "high"} else "medium"
        mode = self._tool_response_mode()

        if mode == "warn_only":
            await self._mark_warned(event)
            return self._build_tool_result_text(
                event=event,
                reason=reason,
                severity=severity,
                evidence=evidence,
                expected_help=expected_help,
                report_attempted=False,
                report_succeeded=False,
                was_warn_only=True,
            )

        if mode == "warn_once_then_report":
            if not await self._was_warned_before(event):
                await self._mark_warned(event)
                return self._build_tool_result_text(
                    event=event,
                    reason=reason,
                    severity=severity,
                    evidence=evidence,
                    expected_help=expected_help,
                    report_attempted=False,
                    report_succeeded=False,
                    was_warn_only=True,
                    warning_reason=reason,
                )

        status, result = await self._send_report(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
        )
        if status == "cooldown":
            if mode == "report_then_inform" or (
                mode == "warn_once_then_report" and self._warn_once_inform_after_report()
            ):
                return self._tool_report_inform_instruction(
                    event=event,
                    reason=reason,
                    severity=severity,
                    evidence=evidence,
                    expected_help=expected_help,
                )
            return self._silent_instruction()

        if status in {"disabled", "unconfigured", "send_failed"}:
            return (
                "你刚刚尝试求助，但这次没有成功。"
                "不要提到工具调用，也不要假装已经通知了任何人。"
                "请根据当前人设自然地设定边界、拒绝继续被骚扰，或简短结束对话。"
            )

        return self._build_tool_result_text(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
            report_attempted=True,
            report_succeeded=status == "ok",
        )

    def _format_watchlist_entry(self, key: str, entry: dict[str, Any]) -> str:
        sender_name = _clean_text(entry.get("sender_name"), "未知用户")
        sender_id = _clean_text(entry.get("sender_id"), "unknown")
        platform_id = _clean_text(entry.get("platform_id"), "unknown")
        report_count = int(entry.get("report_count", 0))
        severity = _clean_text(entry.get("last_severity"), "unknown")
        reason = _truncate(_clean_text(entry.get("last_reason")), 80)
        updated_at = _format_ts(entry.get("last_reported_at"))
        note = _truncate(_clean_text(entry.get("note")), 80)
        return (
            f"- {key}\n"
            f"  用户：{sender_name} ({sender_id}) | 平台：{platform_id}\n"
            f"  次数：{report_count} | 最后严重度：{severity} | 最后时间：{updated_at}\n"
            f"  最后原因：{reason}\n"
            f"  备注：{note or '-'}"
        )

    @filter.on_llm_request(priority=-5)
    async def inject_tool_usage_hint(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._is_enabled() or not self._prompt_enabled():
            return

        if not self._report_target():
            return

        receiver_name = self._report_receiver_name()
        mode = self._tool_response_mode()
        mode_hint = {
            "silent": "上报成功后，默认不要让对方知道你已经上报。",
            "warn_only": f"调用后优先只发出警告，可以自然提到你会告诉{receiver_name}。",
            "warn_once_then_report": (
                f"第一次调用时先警告；如果对方继续骚扰，再次调用时再正式上报给{receiver_name}。"
                f"{'上报后会自然告诉对方。' if self._warn_once_inform_after_report() else '上报后默认保持静默。'}"
            ),
            "report_then_silent": "调用后先完成上报，但不要向对方透露你已经上报。",
            "report_then_inform": f"调用后先完成上报，再自然告诉对方你已经报告给{receiver_name}。",
        }.get(mode, "")

        hint = TextPart(
            text=(
                "[可用安全工具提示]\n"
                "如果你在当前对话里感觉自己正在被骚扰、辱骂、挑衅、恶意消耗，"
                "或者对方持续让你明显不舒服，你可以主动调用 `report_harassment` 工具。\n"
                "调用时请简洁填写：\n"
                "- `reason`: 为什么你觉得这是骚扰\n"
                "- `severity`: low / medium / high\n"
                "- `evidence`: 可选，摘录关键内容\n"
                "- `expected_help`: 可选，希望主人如何介入\n"
                f"{mode_hint}\n"
                "只有在你真觉得需要提醒主人时才调用，不要因为普通分歧或正常玩笑滥用。"
            )
        ).mark_as_temp()
        req.extra_user_content_parts.append(hint)

    @filter.command("harassment_sid")
    async def harassment_sid(self, event: AstrMessageEvent) -> None:
        yield event.plain_result(
            "当前会话 ID：\n"
            f"{event.unified_msg_origin}\n\n"
            "把这个值填到插件配置 `report_session_id`，"
            "或者直接用 `/harassment_bind_here` 绑定当前会话。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_bind_here")
    async def harassment_bind_here(self, event: AstrMessageEvent) -> None:
        if self.config is None:
            yield event.plain_result("绑定失败：插件配置对象不可用。")
            return

        self.config["report_session_id"] = event.unified_msg_origin
        self.config.save_config()
        yield event.plain_result(
            "已将当前会话绑定为骚扰上报接收会话：\n"
            f"{event.unified_msg_origin}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_unbind")
    async def harassment_unbind(self, event: AstrMessageEvent) -> None:
        if self.config is None:
            yield event.plain_result("解绑失败：插件配置对象不可用。")
            return

        self.config["report_session_id"] = ""
        self.config.save_config()
        yield event.plain_result("已清空骚扰上报接收会话。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_status")
    async def harassment_status(self, event: AstrMessageEvent) -> None:
        target = self._report_target() or "未配置"
        watchlist = await self._get_watchlist()
        yield event.plain_result(
            "骚扰上报插件状态：\n"
            f"- 启用：{self._is_enabled()}\n"
            f"- 接收会话：{target}\n"
            f"- 接收人称呼：{self._report_receiver_name()}\n"
            f"- 冷却秒数：{self._cooldown_seconds()}\n"
            f"- 工具回应策略：{self._tool_response_mode()}\n"
            f"- 先警告再上报后告知对方：{self._warn_once_inform_after_report()}\n"
            f"- 警告使用自然语言：{self._natural_warn_enabled()}\n"
            f"- 告知已上报使用自然语言：{self._natural_inform_enabled()}\n"
            f"- 给主人的上报风格：{self._owner_report_style()}\n"
            f"- 注入提示：{self._prompt_enabled()}\n"
            f"- 包含原消息：{self._include_message_text()}\n"
            f"- 附带最近摘要：{self._recent_summary_enabled()}\n"
            f"- 摘要轮数：{self._recent_summary_lines()}\n"
            f"- 自动加入观察名单：{self._watchlist_enabled()}\n"
            f"- 观察名单人数：{len(watchlist)}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_test")
    async def harassment_test(self, event: AstrMessageEvent, note: str | None = None) -> None:
        status, result = await self._send_report(
            event=event,
            reason="管理员主动测试骚扰上报链路",
            severity="low",
            evidence=_clean_text(note, "这是一条测试消息。"),
            expected_help="无需处理，确认你能收到即可。",
            ignore_cooldown=True,
        )
        if status == "ok":
            yield event.plain_result("测试上报已发送。")
            return
        yield event.plain_result(result)

    @filter.command("harassment_watchlist")
    async def harassment_watchlist(self, event: AstrMessageEvent) -> None:
        if not self._can_view_watchlist(event):
            yield event.plain_result(
                "你目前不能查看观察名单。\n"
                "请使用管理员账号，或在已绑定的骚扰上报接收会话里执行 `/harassment_watchlist`。"
            )
            return

        watchlist = await self._get_watchlist()
        if not watchlist:
            yield event.plain_result("观察名单为空。")
            return

        sorted_items = sorted(
            watchlist.items(),
            key=lambda item: float(item[1].get("last_reported_at", 0)),
            reverse=True,
        )
        preview = sorted_items[:20]
        lines = [f"观察名单（共 {len(watchlist)} 人，最多展示 20 人）："]
        for key, entry in preview:
            lines.append(self._format_watchlist_entry(key, entry))

        if len(watchlist) > len(preview):
            lines.append(f"\n其余 {len(watchlist) - len(preview)} 人未展示。")

        yield event.plain_result("\n".join(lines))

    @filter.command("harassment_watch_remove")
    async def harassment_watch_remove(self, event: AstrMessageEvent, key: str | None = None) -> None:
        if not self._can_view_watchlist(event):
            yield event.plain_result(
                "你目前不能修改观察名单。\n"
                "请使用管理员账号，或在已绑定的骚扰上报接收会话里执行这个命令。"
            )
            return

        target_key = await self._resolve_watchlist_key(_clean_text(key))
        if not target_key:
            yield event.plain_result(
                "请提供要移除的观察名单键或用户 ID，例如：\n"
                "/harassment_watch_remove default:2127074778\n"
                "/harassment_watch_remove 2127074778"
            )
            return

        removed = await self._remove_watchlist_entry(target_key)
        if not removed:
            yield event.plain_result(f"观察名单中不存在：{target_key}")
            return

        yield event.plain_result(f"已从观察名单移除：{target_key}")

    @filter.command("harassment_watch_clear")
    async def harassment_watch_clear(self, event: AstrMessageEvent) -> None:
        if not self._can_view_watchlist(event):
            yield event.plain_result(
                "你目前不能清空观察名单。\n"
                "请使用管理员账号，或在已绑定的骚扰上报接收会话里执行这个命令。"
            )
            return

        await self._clear_watchlist()
        yield event.plain_result("已清空观察名单。")
