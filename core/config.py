from __future__ import annotations

from typing import Any

from .text import clean_text

TOOL_RESPONSE_MODES = {
    "silent",
    "warn_only",
    "warn_once_then_report",
    "report_then_silent",
    "report_then_inform",
}
OWNER_REPORT_STYLES = {"structured", "persona_natural"}
CARD_THEMES = {"aurora", "midnight", "paper"}
SEVERITIES = {"low", "medium", "high"}
URGENCIES = {"low", "medium", "high"}
FEEDBACK_CATEGORIES = {"bug", "feature_request", "complaint", "question", "other"}


class Settings:
    """插件配置的统一读取入口。

    所有取值都做了类型收敛和范围兜底，调用方不需要再判空。
    """

    def __init__(self, config: Any) -> None:
        self._config = config

    @property
    def raw(self) -> Any:
        return self._config

    @property
    def available(self) -> bool:
        return self._config is not None

    def get(self, key: str, default: Any = None) -> Any:
        if self._config is None:
            return default
        try:
            return self._config.get(key, default)
        except Exception:
            return default

    def set(self, key: str, value: Any) -> bool:
        """写入并持久化单个配置项，成功返回 True。"""
        if self._config is None:
            return False
        try:
            self._config[key] = value
            self._config.save_config()
            return True
        except Exception:
            return False

    def save(self) -> bool:
        if self._config is None:
            return False
        try:
            self._config.save_config()
            return True
        except Exception:
            return False

    def _bool(self, key: str, default: bool) -> bool:
        return bool(self.get(key, default))

    def _int(self, key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        try:
            value = int(self.get(key, default))
        except Exception:
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _choice(self, key: str, default: str, allowed: set[str]) -> str:
        value = clean_text(self.get(key, default), default)
        return value if value in allowed else default

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._bool("enable", True)

    @property
    def report_session_id(self) -> str:
        return clean_text(self.get("report_session_id", ""))

    @property
    def receiver_name(self) -> str:
        return clean_text(self.get("report_receiver_name", ""), "主人")

    @property
    def bot_self_name(self) -> str:
        """Bot 的自称，用于让人格改写时知道自己是谁；留空则不注入。"""
        return clean_text(self.get("bot_self_name", ""))

    @property
    def max_excerpt_length(self) -> int:
        return self._int("max_excerpt_length", 300, 50, 4000)

    @property
    def debug_log(self) -> bool:
        return self._bool("debug_log", False)

    @property
    def force_plain_text(self) -> bool:
        """发给主人的消息强制走纯文本，不被全局「文本转图片」吃掉。

        上报里常有会话 ID、群号、工单号这类需要复制的内容，转成图片就没法用了。
        """
        return self._bool("force_plain_text", True)

    # ------------------------------------------------------------------
    # 骚扰上报
    # ------------------------------------------------------------------
    @property
    def report_cooldown_seconds(self) -> int:
        return self._int("report_cooldown_seconds", 300, 0, 86400)

    @property
    def report_hourly_limit(self) -> int:
        return self._int("report_hourly_limit", 20, 0, 500)

    @property
    def inject_usage_prompt(self) -> bool:
        return self._bool("inject_usage_prompt", True)

    @property
    def tool_response_mode(self) -> str:
        return self._choice("tool_response_mode", "silent", TOOL_RESPONSE_MODES)

    @property
    def natural_warn_reply(self) -> bool:
        return self._bool("natural_language_warn_reply", True)

    @property
    def warn_template(self) -> str:
        return clean_text(
            self.get("warn_message_template", ""),
            "请立刻停止这种行为。你再这样，我就要告诉{receiver_name}你在骚扰我了。",
        )

    @property
    def warn_memory_seconds(self) -> int:
        return self._int("warn_once_memory_seconds", 1800, 0, 604800)

    @property
    def warn_once_inform_after_report(self) -> bool:
        return self._bool("warn_once_inform_after_report", False)

    @property
    def natural_report_reply(self) -> bool:
        return self._bool("natural_language_report_reply", True)

    @property
    def report_inform_template(self) -> str:
        return clean_text(
            self.get("report_inform_template", ""),
            "我已经把你刚才的行为报告给{receiver_name}了。",
        )

    @property
    def include_message_text(self) -> bool:
        return self._bool("include_message_text", True)

    @property
    def recent_summary_enabled(self) -> bool:
        return self._bool("attach_recent_context_summary", True)

    @property
    def recent_summary_lines(self) -> int:
        return self._int("recent_context_summary_lines", 6, 1, 40)

    @property
    def recent_summary_max_chars(self) -> int:
        return self._int("recent_context_summary_max_chars", 600, 80, 4000)

    @property
    def owner_report_style(self) -> str:
        return self._choice("owner_report_style", "structured", OWNER_REPORT_STYLES)

    @property
    def owner_report_natural(self) -> bool:
        return self._bool("natural_language_report_to_owner", False)

    # ------------------------------------------------------------------
    # 观察名单
    # ------------------------------------------------------------------
    @property
    def watchlist_auto_add(self) -> bool:
        return self._bool("auto_add_sender_to_watchlist", True)

    @property
    def watchlist_max_entries(self) -> int:
        return self._int("watchlist_max_entries", 500, 1, 5000)

    # ------------------------------------------------------------------
    # 反馈窗口
    # ------------------------------------------------------------------
    @property
    def feedback_enabled(self) -> bool:
        return self._bool("enable_feedback_relay", True)

    @property
    def feedback_session_id(self) -> str:
        """反馈专用接收会话，留空表示复用骚扰上报会话。"""
        return clean_text(self.get("feedback_session_id", "")) or self.report_session_id

    @property
    def feedback_proactive_ask(self) -> bool:
        return self._bool("feedback_proactive_ask", True)

    @property
    def feedback_allow_anyone(self) -> bool:
        return self._bool("feedback_allow_anyone", True)

    @property
    def feedback_cooldown_seconds(self) -> int:
        return self._int("feedback_cooldown_seconds", 60, 0, 86400)

    @property
    def feedback_hourly_limit(self) -> int:
        return self._int("feedback_hourly_limit", 30, 0, 500)

    @property
    def feedback_history_lines(self) -> int:
        return self._int("feedback_history_lines", 12, 1, 60)

    @property
    def feedback_history_default(self) -> bool:
        return self._bool("feedback_include_history_default", True)

    @property
    def feedback_persona_rewrite(self) -> bool:
        return self._bool("feedback_persona_rewrite", True)

    @property
    def feedback_reply_persona_rewrite(self) -> bool:
        return self._bool("feedback_reply_persona_rewrite", True)

    @property
    def ticket_max_entries(self) -> int:
        return self._int("feedback_ticket_max_entries", 100, 10, 1000)

    # ------------------------------------------------------------------
    # 群事件通知
    # ------------------------------------------------------------------
    @property
    def notice_enabled(self) -> bool:
        return self._bool("enable_notice", True)

    @property
    def notice_session_id(self) -> str:
        return clean_text(self.get("notice_session_id", "")) or self.report_session_id

    @property
    def notice_ban(self) -> bool:
        return self._bool("notice_ban", True)

    @property
    def notice_admin(self) -> bool:
        return self._bool("notice_admin", True)

    @property
    def notice_member_change(self) -> bool:
        return self._bool("notice_member_change", True)

    @property
    def notice_poke(self) -> bool:
        return self._bool("notice_poke", False)

    @property
    def notice_persona_rewrite(self) -> bool:
        return self._bool("notice_persona_rewrite", True)

    @property
    def notice_snapshot(self) -> bool:
        return self._bool("notice_snapshot", True)

    @property
    def notice_snapshot_count(self) -> int:
        return self._int("notice_snapshot_count", 20, 1, 100)

    @property
    def notice_hourly_limit(self) -> int:
        """群事件小报告每小时上限，与骚扰上报的额度互相独立。

        群里一连串禁言/踢人不会把骚扰上报的配额吃掉。0 表示不限制。
        """
        return self._int("notice_hourly_limit", 30, 0, 500)

    @property
    def notice_group_whitelist(self) -> list[str]:
        raw = self.get("notice_group_whitelist", [])
        if not isinstance(raw, list):
            return []
        return [clean_text(item) for item in raw if clean_text(item)]

    # ------------------------------------------------------------------
    # 卡片
    # ------------------------------------------------------------------
    @property
    def card_enabled(self) -> bool:
        return self._bool("enable_card_report", True)

    @property
    def card_for_harassment(self) -> bool:
        return self._bool("card_for_harassment", True)

    @property
    def card_for_feedback(self) -> bool:
        return self._bool("card_for_feedback", True)

    @property
    def card_for_notice(self) -> bool:
        return self._bool("card_for_notice", True)

    @property
    def card_keep_text(self) -> bool:
        return self._bool("card_keep_text", True)

    @property
    def card_theme(self) -> str:
        return self._choice("card_theme", "aurora", CARD_THEMES)

    @property
    def card_max_messages(self) -> int:
        return self._int("card_max_messages", 14, 1, 60)

    @property
    def card_width(self) -> int:
        return self._int("card_width", 720, 480, 1200)
