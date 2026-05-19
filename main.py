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

WATCHLIST_STORAGE_KEY = "watchlist_v1"


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
    "1.1.0",
    "https://github.com/Huli3/astrbot_plugin_harassment_reporter",
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

    def _echo_enabled(self) -> bool:
        return bool(self._cfg("echo_result_to_current_session", True))

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
        data = await self.get_kv_data(WATCHLIST_STORAGE_KEY, {})
        if isinstance(data, dict):
            return data
        return {}

    async def _save_watchlist(self, watchlist: dict[str, dict[str, Any]]) -> None:
        await self.put_kv_data(WATCHLIST_STORAGE_KEY, watchlist)

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

    async def _build_report_chain(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> MessageChain:
        sender_name = _clean_text(event.get_sender_name(), "未知用户")
        sender_id = _clean_text(event.get_sender_id(), "unknown")
        session_id = _clean_text(event.unified_msg_origin, "unknown")
        platform_id = self._platform_id(event)
        message_text = _clean_text(event.message_str, "[无可读文本]")
        existing_watch = await self._get_watch_entry(event)
        recent_summary = await self._build_recent_context_summary(event)

        chain = MessageChain()
        chain.message("【LLM 骚扰预警】\n")
        chain.message(f"时间：{_now_text()}\n")
        chain.message(f"严重程度：{self._severity_text(severity)} ({severity})\n")
        chain.message(f"来源平台：{platform_id}\n")
        chain.message(f"来源会话：{session_id}\n")
        chain.message(f"发送者：{sender_name} ({sender_id})\n")

        group_id = self._group_id(event)
        if group_id:
            chain.message(f"群组 ID：{group_id}\n")

        chain.message(f"上报原因：{reason}\n")

        if existing_watch:
            chain.message(
                "观察名单：已存在该用户，"
                f"累计上报 {int(existing_watch.get('report_count', 0))} 次\n"
            )

        if evidence:
            chain.message(f"补充证据：{_truncate(evidence, self._max_excerpt_length())}\n")

        if expected_help:
            chain.message(f"期望处理：{_truncate(expected_help, 120)}\n")

        if self._include_message_text():
            chain.message(
                f"当前消息：{_truncate(message_text, self._max_excerpt_length())}\n"
            )

        if recent_summary:
            chain.message("\n最近几轮聊天摘要：\n")
            chain.message(recent_summary)
            chain.message("\n")

        chain.message(
            "\n说明：这是模型在对话中主动调用 `report_harassment` 工具发出的提醒。"
        )
        return chain

    async def _echo_result_to_source(
        self,
        *,
        event: AstrMessageEvent,
        result: str,
    ) -> None:
        if not self._echo_enabled():
            return

        source_session_id = _clean_text(event.unified_msg_origin)
        target_session_id = self._report_target()
        if not source_session_id or source_session_id == target_session_id:
            return

        await self.context.send_message(
            source_session_id,
            MessageChain().message(result),
        )

    async def _send_report(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
        ignore_cooldown: bool = False,
        echo_to_source: bool = False,
    ) -> str:
        if not self._is_enabled():
            result = "上报未执行：插件当前已禁用。"
            if echo_to_source:
                await self._echo_result_to_source(event=event, result=result)
            return result

        target_session_id = self._report_target()
        if not target_session_id:
            result = "上报未执行：还没有配置接收上报的会话 ID。"
            if echo_to_source:
                await self._echo_result_to_source(event=event, result=result)
            return result

        source_session_id = _clean_text(event.unified_msg_origin, "unknown")
        if not ignore_cooldown:
            skipped, remaining = self._should_skip_by_cooldown(source_session_id)
            if skipped:
                result = f"上报已跳过：当前会话仍在冷却中，还需等待约 {remaining} 秒。"
                if echo_to_source:
                    await self._echo_result_to_source(event=event, result=result)
                return result

        chain = await self._build_report_chain(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
        )

        ok = await self.context.send_message(target_session_id, chain)
        if not ok:
            result = (
                "上报失败：AstrBot 没有找到这个会话对应的平台实例。"
                "请确认 `report_session_id` 是否正确。"
            )
            if echo_to_source:
                await self._echo_result_to_source(event=event, result=result)
            return result

        self._mark_reported(source_session_id)
        await self._add_sender_to_watchlist(
            event=event,
            reason=reason,
            severity=severity,
        )
        logger.info(
            "[HarassmentReporter] report sent | source=%s target=%s severity=%s",
            source_session_id,
            target_session_id,
            severity,
        )
        result = "上报已发送给主人。"
        if echo_to_source:
            await self._echo_result_to_source(event=event, result=result)
        return result

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
        return await self._send_report(
            event=event,
            reason=reason,
            severity=severity,
            evidence=evidence,
            expected_help=expected_help,
            echo_to_source=True,
        )

    def _format_watchlist_entry(self, key: str, entry: dict[str, Any]) -> str:
        sender_name = _clean_text(entry.get("sender_name"), "未知用户")
        sender_id = _clean_text(entry.get("sender_id"), "unknown")
        platform_id = _clean_text(entry.get("platform_id"), "unknown")
        report_count = int(entry.get("report_count", 0))
        severity = _clean_text(entry.get("last_severity"), "unknown")
        reason = _truncate(_clean_text(entry.get("last_reason")), 80)
        updated_at = _format_ts(entry.get("last_reported_at"))
        return (
            f"- {key}\n"
            f"  用户：{sender_name} ({sender_id}) | 平台：{platform_id}\n"
            f"  次数：{report_count} | 最后严重度：{severity} | 最后时间：{updated_at}\n"
            f"  最后原因：{reason}"
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
            f"- 冷却秒数：{self._cooldown_seconds()}\n"
            f"- 回显结果：{self._echo_enabled()}\n"
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
        result = await self._send_report(
            event=event,
            reason="管理员主动测试骚扰上报链路",
            severity="low",
            evidence=_clean_text(note, "这是一条测试消息。"),
            expected_help="无需处理，确认你能收到即可。",
            ignore_cooldown=True,
        )
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_watchlist")
    async def harassment_watchlist(self, event: AstrMessageEvent) -> None:
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_watch_remove")
    async def harassment_watch_remove(self, event: AstrMessageEvent, key: str | None = None) -> None:
        target_key = _clean_text(key)
        if not target_key:
            yield event.plain_result(
                "请提供要移除的观察名单键，例如：\n"
                "/harassment_watch_remove aiocqhttp:123456"
            )
            return

        removed = await self._remove_watchlist_entry(target_key)
        if not removed:
            yield event.plain_result(f"观察名单中不存在：{target_key}")
            return

        yield event.plain_result(f"已从观察名单移除：{target_key}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_watch_clear")
    async def harassment_watch_clear(self, event: AstrMessageEvent) -> None:
        await self._clear_watchlist()
        yield event.plain_result("已清空观察名单。")
