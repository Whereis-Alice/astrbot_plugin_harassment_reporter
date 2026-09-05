"""AstrBot 插件：骚扰上报 · 反馈窗口 · 群事件小报告。

三条主线：
1. 骚扰上报：模型在对话里觉得自己被骚扰时，主动把情况报给主人。
2. 反馈窗口：用户想给主人传话，或者模型察觉到用户不满意，主动帮他把问题带过去。
3. 群事件小报告：Bot 被禁言 / 被踢 / 被设管理员这类事件，用 Bot 自己的人格告诉主人。

具体业务都在 core/ 下面，这个文件只负责装配依赖、注入提示词和提供命令入口。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.star.filter.custom_filter import CustomFilter

from .core.card import CardRenderer
from .core.config import Settings
from .core.eventinfo import origin_label, session_id
from .core.feedback import FeedbackService
from .core.harassment import HarassmentService
from .core.history import HistoryReader
from .core.notice import NoticeService
from .core.onebot import OneBotBridge, get_raw_notice, is_onebot_event
from .core.outbox import Outbox
from .core.persona import PersonaWriter
from .core.store import Store
from .core.text import clean_text, truncate
from .core.tools import FeedbackRelayTool, HarassmentReportTool
from .core.watchlist import Watchlist

LOG_PREFIX = "[HarassmentReporter]"
PLUGIN_VERSION = "2.0.0"
REPO_URL = "https://github.com/Whereis-Alice/astrbot_plugin_harassment_reporter"

MODE_LABELS = {
    "silent": "静默上报（不告诉对方）",
    "warn_only": "只警告，不上报",
    "warn_once_then_report": "先警告一次，再犯才上报",
    "report_then_silent": "先上报，然后保持静默",
    "report_then_inform": "先上报，再告诉对方",
}

COMMAND_PREFIX_CHARS = "/!#.！＃。 "


def _yes(flag: bool) -> str:
    return "✓" if flag else "✗"


def _switch(flag: bool) -> str:
    return "开启" if flag else "关闭"


class OneBotNoticeFilter(CustomFilter):
    """只让 OneBot 的通知事件（禁言、踢人、管理员变动……）唤醒通知处理器。

    这样普通聊天消息不会因为这个插件多走一遍 handler，开销接近零。
    """

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        try:
            return get_raw_notice(event) is not None
        except Exception:
            return False


@star.register(
    "astrbot_plugin_harassment_reporter",
    "Huli3",
    "让 Bot 主动上报骚扰、帮用户把反馈带给主人，并在被禁言或被踢时打小报告，支持精美聊天卡片",
    PLUGIN_VERSION,
    REPO_URL,
)
class HarassmentReporterPlugin(star.Star):
    """插件主体：只做装配、提示注入和命令入口。"""

    def __init__(self, context: star.Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.settings = Settings(config)
        self.store = Store(self)
        self.persona = PersonaWriter(context, bot_self_name=self.settings.bot_self_name)
        self.bridge = OneBotBridge(debug=self.settings.debug_log)
        self.card = CardRenderer(self, self.settings)
        self.watchlist = Watchlist(self.settings, self.store)
        self.history = HistoryReader(context)
        self.outbox = Outbox(context, self.store, self.settings, self.card)

        self.harassment = HarassmentService(
            context=context,
            settings=self.settings,
            store=self.store,
            watchlist=self.watchlist,
            persona=self.persona,
            history=self.history,
            card=self.card,
            outbox=self.outbox,
        )
        self.feedback = FeedbackService(
            context=context,
            settings=self.settings,
            store=self.store,
            persona=self.persona,
            history=self.history,
            card=self.card,
            outbox=self.outbox,
        )
        self.notice = NoticeService(
            context=context,
            settings=self.settings,
            store=self.store,
            persona=self.persona,
            card=self.card,
            outbox=self.outbox,
            bridge=self.bridge,
        )

        self.report_tool = HarassmentReportTool(plugin=self)
        self.feedback_tool = FeedbackRelayTool(plugin=self)
        try:
            context.add_llm_tools(self.report_tool, self.feedback_tool)
        except Exception as exc:
            logger.error("%s 注册 LLM 工具失败：%s", LOG_PREFIX, exc)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        try:
            await self.watchlist.migrate()
        except Exception as exc:
            logger.error("%s 迁移观察名单失败：%s", LOG_PREFIX, exc)

        settings = self.settings
        logger.info(
            "%s v%s 已就绪 ｜ 上报=%s 反馈=%s 群通知=%s 卡片=%s",
            LOG_PREFIX,
            PLUGIN_VERSION,
            settings.report_session_id or "未绑定",
            _switch(settings.feedback_enabled),
            _switch(settings.notice_enabled),
            _switch(settings.card_enabled),
        )
        if not settings.available:
            logger.warning("%s 读不到插件配置，将全部使用默认值。", LOG_PREFIX)

    async def terminate(self) -> None:
        logger.info("%s v%s 已卸载。", LOG_PREFIX, PLUGIN_VERSION)

    # ------------------------------------------------------------------
    # 运行期同步：配置在 WebUI 改过之后，让子模块拿到最新值
    # ------------------------------------------------------------------
    def _refresh_runtime(self) -> None:
        try:
            self.persona.bot_self_name = self.settings.bot_self_name
            self.bridge.debug = self.settings.debug_log
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _arg_text(event: AstrMessageEvent, *names: str) -> str:
        """取命令后面剩下的整段文本，多余空格和命令名都会被剥掉。"""
        raw = clean_text(getattr(event, "message_str", ""))
        if not raw:
            return ""
        body = raw.lstrip(COMMAND_PREFIX_CHARS)
        lowered = body.lower()
        for name in sorted(names, key=len, reverse=True):
            if lowered.startswith(name.lower()):
                return body[len(name) :].strip()
        parts = body.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _owner_sessions(self) -> set[str]:
        settings = self.settings
        return {
            sid
            for sid in (
                settings.report_session_id,
                settings.feedback_session_id,
                settings.notice_session_id,
            )
            if sid
        }

    def _in_owner_session(self, event: AstrMessageEvent) -> bool:
        return session_id(event) in self._owner_sessions()

    def _can_view_watchlist(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return self._in_owner_session(event)

    # ------------------------------------------------------------------
    # 供 core/tools.py 回调
    # ------------------------------------------------------------------
    async def handle_tool_report(
        self,
        *,
        event: AstrMessageEvent,
        reason: str,
        severity: str,
        evidence: str,
        expected_help: str,
    ) -> str:
        self._refresh_runtime()
        try:
            return await self.harassment.handle_tool_call(
                event=event,
                reason=reason,
                severity=severity,
                evidence=evidence,
                expected_help=expected_help,
            )
        except Exception as exc:
            logger.error("%s 处理骚扰上报工具调用失败：%s", LOG_PREFIX, exc)
            return (
                "上报过程中出了点问题，这次没有真正送出去。"
                "请自然地把话题带回去，不要声称你已经上报，也不要提到工具调用。"
            )

    async def handle_tool_feedback(
        self,
        *,
        event: AstrMessageEvent,
        summary: str,
        category: str,
        detail: str,
        urgency: str,
        include_history: bool | None,
        reporter_note: str,
    ) -> str:
        self._refresh_runtime()
        try:
            return await self.feedback.handle_tool_call(
                event=event,
                summary=summary,
                category=category,
                detail=detail,
                urgency=urgency,
                include_history=include_history,
                reporter_note=reporter_note,
            )
        except Exception as exc:
            logger.error("%s 处理反馈转达工具调用失败：%s", LOG_PREFIX, exc)
            return (
                "转达过程中出了点问题，这条反馈没能送出去。"
                "请诚实、自然地告诉用户消息没发出去，让他稍后再试，不要假装已经转达。"
            )

    # ------------------------------------------------------------------
    # 提示词注入：不靠关键词，让模型自己判断什么时候该上报、什么时候该问
    # ------------------------------------------------------------------
    def _mode_hint(self) -> str:
        settings = self.settings
        mode = settings.tool_response_mode
        receiver = settings.receiver_name
        if mode == "silent":
            return "上报成功后，默认不要让对方知道你已经上报。"
        if mode == "warn_only":
            return f"调用后优先只发出警告，可以自然提到你会告诉{receiver}。"
        if mode == "warn_once_then_report":
            base = f"第一次调用时先警告；如果对方继续骚扰，再次调用时再正式上报给{receiver}。"
            if settings.warn_once_inform_after_report:
                return base + "上报后会自然告诉对方。"
            return base + "上报后默认保持静默。"
        if mode == "report_then_silent":
            return "调用后先完成上报，但不要向对方透露你已经上报。"
        if mode == "report_then_inform":
            return f"调用后先完成上报，再自然告诉对方你已经报告给{receiver}。"
        return ""

    def _harassment_hint(self) -> str:
        return (
            "[可用安全工具提示]\n"
            "如果你在当前对话里感觉自己正在被骚扰、辱骂、挑衅、恶意消耗，"
            "或者对方持续让你明显不舒服，你可以主动调用 `report_harassment` 工具。\n"
            "调用时请简洁填写：\n"
            "- `reason`: 为什么你觉得这是骚扰\n"
            "- `severity`: low / medium / high\n"
            "- `evidence`: 可选，摘录关键内容\n"
            "- `expected_help`: 可选，希望主人如何介入\n"
            f"{self._mode_hint()}\n"
            "只有在你真觉得需要提醒主人时才调用，不要因为普通分歧或正常玩笑滥用。"
        )

    def _feedback_hint(self) -> str:
        settings = self.settings
        receiver = settings.receiver_name
        me = settings.bot_self_name
        if me:
            call_example = (
                f"用户经常会直接喊你的名字来让你传话，例如「{me}，点歌插件报错了，"
                f"你跟{receiver}说一下」。\n"
            )
        else:
            call_example = ""
        return (
            "[反馈窗口提示]\n"
            f"你可以调用 `relay_feedback_to_owner` 工具，把用户的问题、报错、建议或吐槽"
            f"直接带给{receiver}——他是你的主人，也是这个 Bot 的维护者。"
            "你相当于随身带着一个反馈窗口。\n"
            "\n"
            "什么时候用：\n"
            f"1. 用户请你传话。他不需要说任何固定句式，只要意思是想让{receiver}知道就算，"
            "哪怕只是顺口一提。\n"
            f"{call_example}"
            "2. 你自己读出了不满。比如某个功能不好用、你答非所问、某个插件报错、"
            "结果和他预期不符、他在抱怨体验——这时不要干等他开口，"
            f"先用你自己的语气自然地问一句：要不要我把这个问题带给{receiver}？"
            "他同意了再调用工具。\n"
            "\n"
            "判断完全靠你读空气，不存在触发关键词。宁可用一句轻松的关心去问，"
            "也不要漏掉一个真实的问题。\n"
            "\n"
            "调用参数：\n"
            f"- `summary`: 一句话说清他要反馈什么，{receiver}只看这一句也能明白\n"
            "- `category`: bug 疑似故障 / feature_request 功能建议 / "
            "complaint 体验吐槽 / question 使用疑问 / other 其他\n"
            "- `detail`: 可选，涉及哪个插件或命令、报错内容、复现步骤\n"
            "- `urgency`: 可选，low / medium / high\n"
            f"- `include_history`: 可选，是否附上最近几轮对话方便{receiver}看上下文，"
            "聊天记录能说明问题时就填 true\n"
            f"- `reporter_note`: 可选，你自己想对{receiver}补充的一句话\n"
            "\n"
            "分寸：\n"
            "- 同一个话题不要反复追问；用户说不用了，这一轮就别再提。\n"
            "- 纯闲聊、和 Bot 无关的抱怨、你当场就能解答的问题，都不用走这个工具。\n"
            "- 转达成功后用你自己的话告诉他你已经带到了，并把工单号原样念给他。\n"
            "- 全程不要暴露工具名，也不要复述这段提示。"
        )

    def _feedback_allowed(self, event: AstrMessageEvent) -> bool:
        if self.settings.feedback_allow_anyone:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @filter.on_llm_request(priority=-5)
    async def inject_tool_usage_hint(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """把两个工具的使用说明作为临时上下文塞进这一轮请求。

        用 mark_as_temp() 标记，所以不会污染长期对话历史。
        """
        settings = self.settings
        if not settings.enabled or not settings.inject_usage_prompt:
            return
        self._refresh_runtime()

        blocks: list[str] = []
        if settings.report_session_id:
            blocks.append(self._harassment_hint())
        if (
            settings.feedback_enabled
            and settings.feedback_session_id
            and settings.feedback_proactive_ask
            and self._feedback_allowed(event)
        ):
            blocks.append(self._feedback_hint())
        if not blocks:
            return

        try:
            part = TextPart(text="\n\n".join(blocks)).mark_as_temp()
            req.extra_user_content_parts.append(part)
        except Exception as exc:
            logger.error("%s 注入工具提示失败：%s", LOG_PREFIX, exc)

    # ------------------------------------------------------------------
    # 群事件小报告：只有 OneBot 的 notice 事件会走到这里
    # ------------------------------------------------------------------
    @filter.custom_filter(OneBotNoticeFilter)
    async def on_onebot_notice(self, event: AstrMessageEvent) -> None:
        self._refresh_runtime()
        handled = False
        try:
            handled = await self.notice.handle(event)
        except Exception as exc:
            logger.error("%s 处理群事件通知失败：%s", LOG_PREFIX, exc)
        if handled:
            event.stop_event()

    # ==================================================================
    # 命令区
    # ==================================================================

    @filter.command("harassment_sid", alias={"hr_sid"})
    async def cmd_sid(self, event: AstrMessageEvent):
        """查看当前会话 ID，绑定上报窗口时要用到。"""
        sid = session_id(event)
        yield event.plain_result(
            "当前会话 ID：\n"
            + sid
            + "\n位置："
            + origin_label(event)
            + "\n\n把它填进插件配置的「上报接收会话 ID」，"
            "或者直接在这个会话里发 /hr_bind 一键绑定。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_bind_here", alias={"hr_bind"})
    async def cmd_bind_here(self, event: AstrMessageEvent):
        """把当前会话绑定为骚扰上报窗口（反馈和群通知默认也会发到这里）。"""
        sid = session_id(event)
        if not self.settings.set("report_session_id", sid):
            yield event.plain_result("绑定失败：插件配置不可写，请手动在 WebUI 里填写。")
            return
        yield event.plain_result(
            "已把这里设为上报窗口：\n"
            + sid
            + "\n\n反馈转达和群事件小报告如果没有单独指定，也会一起发到这里。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("hr_bind_feedback")
    async def cmd_bind_feedback(self, event: AstrMessageEvent):
        """把当前会话单独设为反馈接收窗口。"""
        sid = session_id(event)
        if not self.settings.set("feedback_session_id", sid):
            yield event.plain_result("绑定失败：插件配置不可写，请手动在 WebUI 里填写。")
            return
        yield event.plain_result("已把这里设为反馈接收窗口：\n" + sid)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("hr_bind_notice")
    async def cmd_bind_notice(self, event: AstrMessageEvent):
        """把当前会话单独设为群事件小报告窗口。"""
        sid = session_id(event)
        if not self.settings.set("notice_session_id", sid):
            yield event.plain_result("绑定失败：插件配置不可写，请手动在 WebUI 里填写。")
            return
        yield event.plain_result("已把这里设为群事件小报告窗口：\n" + sid)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_unbind", alias={"hr_unbind"})
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解绑窗口。默认解绑上报窗口，可加 feedback / notice / all。"""
        which = self._arg_text(event, "harassment_unbind", "hr_unbind").lower()
        mapping = {
            "": ["report_session_id"],
            "report": ["report_session_id"],
            "feedback": ["feedback_session_id"],
            "notice": ["notice_session_id"],
            "all": ["report_session_id", "feedback_session_id", "notice_session_id"],
        }
        keys = mapping.get(which)
        if keys is None:
            yield event.plain_result("用法：/hr_unbind [report|feedback|notice|all]")
            return
        ok = True
        for key in keys:
            ok = self.settings.set(key, "") and ok
        if not ok:
            yield event.plain_result("解绑失败：插件配置不可写，请手动在 WebUI 里清空。")
            return
        names = {
            "report_session_id": "上报窗口",
            "feedback_session_id": "反馈窗口",
            "notice_session_id": "群通知窗口",
        }
        yield event.plain_result("已解绑：" + "、".join(names[k] for k in keys))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_status", alias={"hr_status"})
    async def cmd_status(self, event: AstrMessageEvent):
        """一屏看完三条通道 + 卡片 + 名单的当前状态。"""
        self._refresh_runtime()
        settings = self.settings
        try:
            entries = await self.watchlist.all()
        except Exception:
            entries = {}
        try:
            tickets = await self.store.get_tickets()
        except Exception:
            tickets = {}

        same = "（同上报窗口）"
        report_target = settings.report_session_id or "未绑定"
        feedback_target = settings.feedback_session_id or "未绑定"
        notice_target = settings.notice_session_id or "未绑定"
        if settings.feedback_session_id and settings.feedback_session_id == settings.report_session_id:
            feedback_target = report_target + same
        if settings.notice_session_id and settings.notice_session_id == settings.report_session_id:
            notice_target = report_target + same

        whitelist = settings.notice_group_whitelist
        lines = [
            "【骚扰上报器 v" + PLUGIN_VERSION + "】",
            "总开关：" + _switch(settings.enabled),
            "当前会话：" + session_id(event),
            "当前平台：" + event.get_platform_name()
            + ("（OneBot 增强功能可用）" if is_onebot_event(event) else "（非 OneBot，抽查/合并转发不可用）"),
            "",
            "— 骚扰上报 —",
            "上报窗口：" + report_target,
            "主人称呼：" + settings.receiver_name,
            "应答模式：" + MODE_LABELS.get(settings.tool_response_mode, settings.tool_response_mode),
            "冷却 " + str(settings.report_cooldown_seconds) + " 秒 ｜ 每小时上限 "
            + str(settings.report_hourly_limit) + " 次",
            "提示注入：" + _switch(settings.inject_usage_prompt),
            "附带近期对话：" + _switch(settings.recent_summary_enabled)
            + "（" + str(settings.recent_summary_lines) + " 行 / "
            + str(settings.recent_summary_max_chars) + " 字）",
            "上报文风：" + ("人格化叙述" if settings.owner_report_style == "persona_natural" else "结构化列表"),
            "",
            "— 反馈窗口 —",
            "状态：" + _switch(settings.feedback_enabled),
            "反馈窗口：" + feedback_target,
            "主动询问不满：" + _switch(settings.feedback_proactive_ask),
            "开放对象：" + ("所有人" if settings.feedback_allow_anyone else "仅管理员"),
            "冷却 " + str(settings.feedback_cooldown_seconds) + " 秒 ｜ 每小时上限 "
            + str(settings.feedback_hourly_limit) + " 次",
            "默认附带历史：" + _switch(settings.feedback_history_default)
            + "（" + str(settings.feedback_history_lines) + " 行）",
            "工单：" + str(len(tickets)) + " 条（上限 " + str(settings.ticket_max_entries) + "）",
            "",
            "— 群事件小报告 —",
            "状态：" + _switch(settings.notice_enabled),
            "通知窗口：" + notice_target,
            "监听：禁言 " + _yes(settings.notice_ban)
            + " ｜ 管理员变动 " + _yes(settings.notice_admin)
            + " ｜ 进出群 " + _yes(settings.notice_member_change)
            + " ｜ 戳一戳 " + _yes(settings.notice_poke),
            "人格改写：" + _switch(settings.notice_persona_rewrite),
            "每小时上限 " + (str(settings.notice_hourly_limit) + " 次（独立额度）"
                            if settings.notice_hourly_limit else "不限制"),
            "附带群内近期消息：" + _switch(settings.notice_snapshot)
            + "（" + str(settings.notice_snapshot_count) + " 条）",
            "群白名单：" + ("、".join(whitelist) if whitelist else "未设置（监听全部群）"),
            "",
            "— 聊天卡片 —",
            "状态：" + _switch(settings.card_enabled)
            + "（主题 " + settings.card_theme + " ｜ 宽 " + str(settings.card_width)
            + " ｜ 最多 " + str(settings.card_max_messages) + " 条）",
            "适用：骚扰 " + _yes(settings.card_for_harassment)
            + " ｜ 反馈 " + _yes(settings.card_for_feedback)
            + " ｜ 群事件 " + _yes(settings.card_for_notice),
            "渲染服务：" + ("暂时熔断中（会自动恢复）" if self.card.muted else "正常"),
            "保留纯文本副本：" + _switch(settings.card_keep_text),
            "",
            "— 观察名单 —",
            str(len(entries)) + " 条记录（上限 " + str(settings.watchlist_max_entries) + "）",
            "自动登记上报对象：" + _switch(settings.watchlist_auto_add),
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("harassment_test", alias={"hr_test"})
    async def cmd_test(self, event: AstrMessageEvent):
        """发一条测试上报，确认链路通不通。"""
        self._refresh_runtime()
        note = truncate(self._arg_text(event, "harassment_test", "hr_test"), 200)
        target = self.settings.report_session_id
        if not target:
            yield event.plain_result("还没绑定上报窗口，先在目标会话里发 /hr_bind。")
            return
        delivery = await self.harassment.send_report(
            event=event,
            reason=note or "这是一条来自 /hr_test 的测试上报，用来确认链路是否通畅。",
            severity="low",
            evidence="",
            expected_help="不用处理，只是测试。",
            ignore_limits=True,
        )
        if delivery.ok:
            yield event.plain_result("测试上报已发往：\n" + target)
        else:
            yield event.plain_result("测试上报没发出去：" + delivery.detail)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("hr_feedback_test")
    async def cmd_feedback_test(self, event: AstrMessageEvent):
        """发一条测试反馈，顺便拿到一个可以用来试 /hr_reply 的工单号。"""
        self._refresh_runtime()
        note = truncate(self._arg_text(event, "hr_feedback_test"), 200)
        if not self.settings.feedback_session_id:
            yield event.plain_result("还没绑定反馈窗口，先在目标会话里发 /hr_bind 或 /hr_bind_feedback。")
            return
        delivery, ticket_id = await self.feedback.relay(
            event=event,
            summary=note or "这是一条来自 /hr_feedback_test 的测试反馈。",
            category="other",
            detail="用于确认反馈窗口是否连通。",
            urgency="low",
            include_history=False,
            reporter_note="测试消息，不用当真。",
            ignore_limits=True,
        )
        if delivery.ok:
            yield event.plain_result(
                "测试反馈已发往：\n"
                + self.settings.feedback_session_id
                + "\n工单号："
                + ticket_id
                + "\n可以用 /hr_reply "
                + ticket_id
                + " 内容 来试试回复。"
            )
        else:
            yield event.plain_result("测试反馈没发出去：" + delivery.detail)

    # ------------------------------------------------------------------
    # 观察名单
    # ------------------------------------------------------------------
    @filter.command("harassment_watchlist", alias={"hr_watchlist"})
    async def cmd_watchlist(self, event: AstrMessageEvent):
        """看看都有谁被记进了观察名单。"""
        if not self._can_view_watchlist(event):
            yield event.plain_result("只有管理员或上报窗口所在会话可以查看观察名单。")
            return
        entries = await self.watchlist.all()
        if not entries:
            yield event.plain_result("观察名单还是空的。")
            return
        rows = sorted(
            entries.items(),
            key=lambda item: self.watchlist.sort_key(item[1]),
            reverse=True,
        )
        blocks = [self.watchlist.format_entry(key, entry) for key, entry in rows[:20]]
        text = "【观察名单】共 " + str(len(entries)) + " 条\n\n" + "\n\n".join(blocks)
        if len(entries) > 20:
            text += "\n\n（只显示最近 20 条）"
        yield event.plain_result(text)

    @filter.command("harassment_watch_remove", alias={"hr_watch_remove"})
    async def cmd_watch_remove(self, event: AstrMessageEvent):
        """把某个人从观察名单里删掉，参数可以是完整 key 或纯 QQ 号。"""
        if not self._can_view_watchlist(event):
            yield event.plain_result("只有管理员或上报窗口所在会话可以修改观察名单。")
            return
        target = self._arg_text(event, "harassment_watch_remove", "hr_watch_remove")
        if not target:
            yield event.plain_result("用法：/hr_watch_remove 平台:用户ID（也可以只写用户 ID）")
            return
        key = await self.watchlist.resolve_key(target)
        if not key:
            yield event.plain_result("名单里没找到「" + target + "」。")
            return
        if await self.watchlist.remove(key):
            yield event.plain_result("已从观察名单移除：" + key)
        else:
            yield event.plain_result("移除失败，名单里没有：" + key)

    @filter.command("harassment_watch_clear", alias={"hr_watch_clear"})
    async def cmd_watch_clear(self, event: AstrMessageEvent):
        """清空整份观察名单。"""
        if not self._can_view_watchlist(event):
            yield event.plain_result("只有管理员或上报窗口所在会话可以清空观察名单。")
            return
        await self.watchlist.clear()
        yield event.plain_result("观察名单已清空。")

    # ------------------------------------------------------------------
    # 反馈工单
    # ------------------------------------------------------------------
    @filter.command("hr_tickets", alias={"hr_ticket"})
    async def cmd_tickets(self, event: AstrMessageEvent):
        """列出最近的反馈工单。"""
        if not self._can_view_watchlist(event):
            yield event.plain_result("只有管理员或上报窗口所在会话可以查看反馈工单。")
            return
        raw = self._arg_text(event, "hr_tickets", "hr_ticket")
        limit = 10
        if raw.isdigit():
            limit = max(1, min(50, int(raw)))
        yield event.plain_result(await self.feedback.format_tickets(limit))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("hr_reply")
    async def cmd_reply(self, event: AstrMessageEvent):
        """回复某个工单，内容会用 Bot 的人格送回原会话。"""
        self._refresh_runtime()
        raw = self._arg_text(event, "hr_reply")
        parts = raw.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法：/hr_reply 工单号 你要回复的内容\n（工单号支持只写后 4 位）")
            return
        ok, message = await self.feedback.reply_ticket(
            ticket_id=parts[0],
            content=parts[1],
            event=event,
        )
        yield event.plain_result(message if message else ("已回复。" if ok else "回复失败。"))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("hr_ticket_clear")
    async def cmd_ticket_clear(self, event: AstrMessageEvent):
        """清空所有反馈工单记录。"""
        await self.store.clear_tickets()
        yield event.plain_result("反馈工单已清空。")

    # ------------------------------------------------------------------
    # 群消息抽查（OneBot v11）
    # ------------------------------------------------------------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("hr_peek", alias={"抽查"})
    async def cmd_peek(self, event: AstrMessageEvent):
        """抽查某个群最近的聊天记录，可选指定条数。"""
        self._refresh_runtime()
        raw = self._arg_text(event, "hr_peek", "抽查")
        parts = raw.split()
        if not parts:
            yield event.plain_result("用法：/hr_peek 群号 [条数]")
            return
        count = 0
        if len(parts) > 1 and parts[1].isdigit():
            count = max(1, min(100, int(parts[1])))
        ok, message = await self.notice.peek(event, parts[0], count=count)
        yield event.plain_result(message)

    # ------------------------------------------------------------------
    # 帮助
    # ------------------------------------------------------------------
    @filter.command("harassment_help", alias={"hr_help"})
    async def cmd_help(self, event: AstrMessageEvent):
        """列出这个插件的所有命令。"""
        yield event.plain_result(
            "【骚扰上报器 v" + PLUGIN_VERSION + "】命令一览\n"
            "（斜杠前缀按你自己的 AstrBot 设置来）\n"
            "\n"
            "· 绑定与状态\n"
            "  /hr_sid              查看当前会话 ID\n"
            "  /hr_bind             把这里设为上报窗口（管理员）\n"
            "  /hr_bind_feedback    把这里设为反馈窗口（管理员）\n"
            "  /hr_bind_notice      把这里设为群通知窗口（管理员）\n"
            "  /hr_unbind [目标]    解绑，目标可填 report/feedback/notice/all（管理员）\n"
            "  /hr_status           查看全部开关和窗口（管理员）\n"
            "\n"
            "· 自测\n"
            "  /hr_test [备注]      发一条测试骚扰上报（管理员）\n"
            "  /hr_feedback_test    发一条测试反馈并拿到工单号（管理员）\n"
            "\n"
            "· 反馈工单\n"
            "  /hr_tickets [条数]   列出最近的反馈工单\n"
            "  /hr_reply 工单号 内容 回复工单，Bot 会带着人格送回原会话（管理员）\n"
            "  /hr_ticket_clear     清空工单记录（管理员）\n"
            "\n"
            "· 观察名单\n"
            "  /hr_watchlist        查看被上报过的人\n"
            "  /hr_watch_remove 键  移除一条\n"
            "  /hr_watch_clear      清空名单\n"
            "\n"
            "· 群消息抽查（需要 OneBot v11 协议端）\n"
            "  /hr_peek 群号 [条数] 抓一份该群最近的聊天记录（管理员）\n"
            "\n"
            "旧版的 /harassment_* 长命令全部保留，可以继续用。\n"
            "详细说明见：" + REPO_URL
        )
