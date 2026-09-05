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

CONFIG_LAYOUT_VERSION = "2"
LAYOUT_KEY = "config_layout_version"

# 配置项 -> 所属分组。2.1.0 起 _conf_schema.json 把配置分成了七组，
# 这张表让 Settings 既能读新的分组结构，也能读老用户残留的平铺键。
KEY_GROUPS: dict[str, str] = {
    # 称呼与身份
    "report_receiver_name": "identity",
    "bot_self_name": "identity",
    # 骚扰上报
    "report_session_id": "harassment",
    "tool_response_mode": "harassment",
    "report_cooldown_seconds": "harassment",
    "report_hourly_limit": "harassment",
    "natural_language_warn_reply": "harassment",
    "warn_message_template": "harassment",
    "warn_once_memory_seconds": "harassment",
    "warn_once_inform_after_report": "harassment",
    "natural_language_report_reply": "harassment",
    "report_inform_template": "harassment",
    "include_message_text": "harassment",
    "attach_recent_context_summary": "harassment",
    "recent_context_summary_lines": "harassment",
    "recent_context_summary_max_chars": "harassment",
    "owner_report_style": "harassment",
    # 观察名单
    "auto_add_sender_to_watchlist": "watchlist",
    "watchlist_max_entries": "watchlist",
    "watchlist_entries": "watchlist",
    # 反馈转达
    "enable_feedback_relay": "feedback",
    "feedback_session_id": "feedback",
    "feedback_proactive_ask": "feedback",
    "feedback_allow_anyone": "feedback",
    "feedback_cooldown_seconds": "feedback",
    "feedback_hourly_limit": "feedback",
    "feedback_attach_chatlog": "feedback",
    "feedback_chatlog_count": "feedback",
    "feedback_append_source": "feedback",
    "feedback_reply_persona_rewrite": "feedback",
    "feedback_recent_max_entries": "feedback",
    # 群事件小报告
    "enable_notice": "notice",
    "notice_session_id": "notice",
    "notice_ban": "notice",
    "notice_admin": "notice",
    "notice_member_change": "notice",
    "notice_poke": "notice",
    "notice_persona_rewrite": "notice",
    "notice_snapshot": "notice",
    "notice_snapshot_count": "notice",
    "notice_hourly_limit": "notice",
    "notice_group_whitelist": "notice",
    # 聊天记录卡片
    "enable_card_report": "card",
    "card_for_harassment": "card",
    "card_for_feedback": "card",
    "card_for_notice": "card",
    "card_theme": "card",
    "card_max_messages": "card",
    "card_use_real_avatar": "card",
    "card_show_time": "card",
    "card_show_avatar": "card",
    # 高级与调试
    "inject_usage_prompt": "advanced",
    "max_excerpt_length": "advanced",
    "force_plain_text": "advanced",
    "debug_log": "advanced",
}

CONFIG_GROUPS: tuple[str, ...] = (
    "identity",
    "harassment",
    "watchlist",
    "feedback",
    "notice",
    "card",
    "advanced",
)


class Settings:
    """插件配置的统一读取入口。

    所有取值都做了类型收敛和范围兜底，调用方不需要再判空。
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self.migration_note = ""
        try:
            self.migration_note = self._migrate_layout()
        except Exception as exc:  # 迁移失败也不能拖垮插件加载
            self.migration_note = "配置迁移失败，插件将继续按旧的平铺配置运行：" + str(exc)

    @property
    def raw(self) -> Any:
        return self._config

    @property
    def available(self) -> bool:
        return self._config is not None

    # ------------------------------------------------------------------
    # 分组寻址
    # ------------------------------------------------------------------
    def _container(self, key: str) -> Any:
        """返回这个键实际存放的容器。

        优先用 2.1.0 的分组子字典；分组不存在（老配置文件、或用户手动删过键）
        时回退到配置根，这样新旧两种结构都能读写。
        """
        group = KEY_GROUPS.get(key)
        if group and self._config is not None:
            try:
                bucket = self._config.get(group)
                if isinstance(bucket, dict) and key in bucket:
                    return bucket
            except Exception:
                pass
        return self._config

    def get(self, key: str, default: Any = None) -> Any:
        if self._config is None:
            return default
        try:
            return self._container(key).get(key, default)
        except Exception:
            return default

    def set(self, key: str, value: Any) -> bool:
        """写入并持久化单个配置项，成功返回 True。"""
        if self._config is None:
            return False
        try:
            self._container(key)[key] = value
            self._config.save_config()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 平铺配置 -> 分组配置的一次性迁移
    # ------------------------------------------------------------------
    def _migrate_layout(self) -> str:
        """把 2.0.x 的平铺配置搬进 2.1.0 的分组结构，只跑一次。

        AstrBot 在实例化插件之前就会按 _conf_schema.json 补齐缺失的键，
        所以进到这里时分组子字典一定已经存在、且装的是默认值；
        旧的平铺键则因为 schema 里保留了隐藏存根而原封不动地留着。
        因此「旧值和分组里的默认值不一样」就等价于「用户改过这一项」，
        直接拷进分组即可。旧键一律不删，天然成为一份回退备份。
        """
        cfg = self._config
        if cfg is None:
            return ""
        if str(cfg.get(LAYOUT_KEY, "") or "") == CONFIG_LAYOUT_VERSION:
            return ""

        moved = 0
        for key, group in KEY_GROUPS.items():
            bucket = cfg.get(group)
            if not isinstance(bucket, dict) or key not in bucket:
                continue
            if key not in cfg:
                continue
            legacy = cfg.get(key)
            if legacy is None or bucket[key] == legacy:
                continue
            bucket[key] = legacy
            moved += 1

        cfg[LAYOUT_KEY] = CONFIG_LAYOUT_VERSION
        try:
            cfg.save_config()
        except Exception:
            pass
        if moved:
            return "配置已升级为分组结构，迁移了 " + str(moved) + " 项旧设置（旧字段保留作备份）。"
        return "配置分组结构已初始化。"

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

        上报里常有会话 ID、群号这类需要复制的内容，转成图片就没法用了。
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
        return self._int("recent_context_summary_max_chars", 600, 80, 8000)

    @property
    def owner_report_style(self) -> str:
        return self._choice("owner_report_style", "structured", OWNER_REPORT_STYLES)

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
    def feedback_attach_chatlog(self) -> bool:
        """带话时是否附上最近的群聊记录（卡片 + 文本兜底都受它控制）。"""
        return self._bool("feedback_attach_chatlog", True)

    @property
    def feedback_chatlog_count(self) -> int:
        return self._int("feedback_chatlog_count", 14, 1, 60)

    @property
    def feedback_append_source(self) -> bool:
        """在正文末尾补一行「谁 · 在哪儿」，方便主人定位。"""
        return self._bool("feedback_append_source", True)

    @property
    def feedback_reply_persona_rewrite(self) -> bool:
        return self._bool("feedback_reply_persona_rewrite", True)

    @property
    def feedback_recent_max_entries(self) -> int:
        return self._int("feedback_recent_max_entries", 50, 5, 500)

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
    def card_theme(self) -> str:
        return self._choice("card_theme", "aurora", CARD_THEMES)

    @property
    def card_max_messages(self) -> int:
        return self._int("card_max_messages", 14, 1, 60)

    @property
    def card_use_real_avatar(self) -> bool:
        """用群友和群的真实 QQ 头像，拉不到时自动退回彩色首字块。"""
        return self._bool("card_use_real_avatar", True)

    @property
    def card_show_time(self) -> bool:
        """在每条消息旁显示时间（拿不到时间戳的来源自然不显示）。"""
        return self._bool("card_show_time", True)

    @property
    def card_show_avatar(self) -> bool:
        """显示圆形头像，关掉会更紧凑。"""
        return self._bool("card_show_avatar", True)
