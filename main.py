"""AstrBot 插件「我会打小报告」：让 Bot 学会主动来找你。

三条主线：
1. 帮群友传话：有人说「跟你主人说一声」，或者 Bot 自己读出了不满，
   它就会用自己的口吻去找主人，顺手附上一张最近群聊记录的卡片。
2. 骚扰上报：Bot 在对话里觉得自己被骚扰时，主动把情况报给主人。
3. 群事件小报告：Bot 被禁言 / 被踢 / 被设管理员这类事件，用 Bot 自己的人格告诉主人。

具体业务都在 core/ 下面，这个文件只负责装配依赖、注入提示词和提供命令入口。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.star.filter.custom_filter import CustomFilter

from .core.card import PAGE_WIDTH, CardRenderer
from .core.chatlog import ChatLogCollector
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
from .core.text import PLUGIN_DISPLAY_NAME, clean_text, truncate
from .core.tools import FeedbackRelayTool, HarassmentReportTool
from .core.watchlist import Watchlist

LOG_PREFIX = "[HarassmentReporter]"
REPO_URL = "https://github.com/Whereis-Alice/astrbot_plugin_harassment_reporter"


def _plugin_version(fallback: str = "2.2.0") -> str:
    """版本号以 metadata.yaml 为准，代码这边只兜底。

    AstrBot 认的是 metadata.yaml；而 @star.register、/hr_status、/hr_help 和启动日志
    用的是这个常量。以前两处各写一份，改版本时漏掉一边，用户就会看到两个不同的号
    （2.1.2 就发生过）。现在直接从同一份文件读，想漂也漂不了。
    """
    try:
        text = (Path(__file__).parent / "metadata.yaml").read_text(encoding="utf-8")
    except Exception:
        return fallback
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("version:"):
            continue
        value = stripped.split(":", 1)[1].strip().strip("\"'")
        if value:
            return value
    return fallback


PLUGIN_VERSION = _plugin_version()

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
    "我会打小报告：让 Bot 用自己的口吻主动来找你——帮群友传话、被骚扰时告状、"
    "被禁言被踢时汇报，还会带上一张最近群聊记录卡片",
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
        self.chatlog = ChatLogCollector(
            settings=self.settings,
            bridge=self.bridge,
            history=self.history,
        )
        self.outbox = Outbox(context, self.store, self.settings, self.card)

        self.harassment = HarassmentService(
            context=context,
            settings=self.settings,
            store=self.store,
            watchlist=self.watchlist,
            persona=self.persona,
            history=self.history,
            chatlog=self.chatlog,
            card=self.card,
            outbox=self.outbox,
        )
        self.feedback = FeedbackService(
            context=context,
            settings=self.settings,
            store=self.store,
            persona=self.persona,
            chatlog=self.chatlog,
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
        note = clean_text(getattr(settings, "migration_note", ""))
        if note:
            logger.info("%s %s", LOG_PREFIX, note)
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
        message: str,
        send_images: bool = False,
    ) -> str:
        self._refresh_runtime()
        try:
            return await self.feedback.handle_tool_call(
                event=event,
                message=message,
                send_images=send_images,
            )
        except Exception as exc:
            logger.error("%s 处理反馈转达工具调用失败：%s", LOG_PREFIX, exc)
            return (
                "转达过程中出了点问题，这句话没能送出去。"
                "请诚实、自然地告诉用户消息没送到，让他稍后再试，不要假装已经带到。"
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
                f"他们经常直接喊你的名字，例如「{me}，点歌插件报错了，"
                f"你跟{receiver}说一下」。\n"
            )
        else:
            call_example = ""
        if settings.feedback_proactive_ask:
            proactive = (
                "2. 你自己读出了不满。比如某个功能不好用、你答非所问、某个插件报错、"
                "结果和他预期不符、他在抱怨体验——这时不要干等他开口，"
                f"先用你自己的语气自然地问一句：要不要我去跟{receiver}说一声？"
                "他愿意了再调用工具。\n"
                "\n"
                "判断完全靠你读空气，不存在触发关键词。宁可用一句轻松的关心去问，"
                "也不要漏掉一个真实的问题。\n"
            )
        else:
            proactive = "别人没开口请你传话时，不要主动提这件事。\n"
        # 关掉附带聊天记录时不能再说「记录会自动附上」，否则模型会以为可以少写，
        # 结果主人收到一句没有上下文的空话。
        if settings.feedback_attach_chatlog:
            chatlog_note = "最近的群聊记录会自动附在这句话后面，所以你不用复述聊天内容。\n"
        else:
            chatlog_note = "这次不会附带聊天记录，所以该讲清的来龙去脉都要写在这句话里。\n"
        # 同理，转图关掉时不能让模型跟用户保证「图我带过去了」。
        if settings.feedback_forward_images:
            image_note = (
                f"如果他这条消息里带了图，或者他引用了一条带图的消息，那些图会自动一起带给{receiver}，"
                "你只要在话里提一句那是什么图就行。"
                "他要是说「把刚才群里那张图也带上」，调用时额外把 `send_images` 设为 true。\n"
            )
        else:
            image_note = "这次不会转发图片，所以图上是什么内容，得你自己用话讲清楚。\n"
        return (
            "[传话能力提示]\n"
            f"你可以调用 `relay_feedback_to_owner` 工具，直接去找{receiver}说一句话。"
            f"{receiver}是你的主人，也是这个 Bot 的维护者。"
            "这个动作就像你自己走出去喊他一声，不是提交表单，也不是开工单。\n"
            "\n"
            "什么时候用：\n"
            f"1. 有人请你传话。不需要任何固定句式，只要意思是想让{receiver}知道就算，"
            "哪怕只是顺口一提。\n"
            f"{call_example}"
            f"{proactive}"
            "\n"
            "怎么调用：\n"
            f"主参数是 `message`，写你要对{receiver}说的那句话。"
            "用你自己的口吻完整地说出来，像真人帮群友传话，"
            "说清是谁、在哪儿、遇到了什么。\n"
            f"例如：「星之卡比群的群友A找你呀{receiver}，说是画图插件用不了了」。\n"
            "不要写成工单格式，不要只填关键词，也不要写成冷冰冰的第三人称报告。\n"
            f"{chatlog_note}"
            f"{image_note}"
            "\n"
            "分寸：\n"
            "- 同一个话题不要反复追问；用户说不用了，这一轮就别再提。\n"
            "- 纯闲聊、和 Bot 无关的抱怨、你当场就能解答的问题，都不用走这个工具。\n"
            f"- 说完之后用你自己的话告诉他你已经去找{receiver}了，让他等回音。\n"
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
            and self._feedback_allowed(event)
        ):
            # 注意：feedback_proactive_ask 只决定「要不要主动察觉不满」，
            # 关掉它之后模型仍然要知道这个工具的存在，否则用户明确请它传话时会答不上来。
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
            contacts = await self.store.get_contacts()
        except Exception:
            contacts = []

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
            "【" + PLUGIN_DISPLAY_NAME + " v" + PLUGIN_VERSION + "】",
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
            "附带最近群聊记录：" + _switch(settings.feedback_attach_chatlog)
            + "（" + str(settings.feedback_chatlog_count) + " 条）",
            "一起带上图片：" + _switch(settings.feedback_forward_images)
            + "（最多 " + str(settings.feedback_image_limit) + " 张）",
            "正文补一行来源：" + _switch(settings.feedback_append_source),
            "联系记录：" + str(len(contacts)) + " 条（上限 "
            + str(settings.feedback_recent_max_entries) + "）",
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
            + "（主题 " + settings.card_theme
            + " ｜ 最多 " + str(settings.card_max_messages) + " 条）",
            "适用：骚扰 " + _yes(settings.card_for_harassment)
            + " ｜ 反馈 " + _yes(settings.card_for_feedback)
            + " ｜ 群事件 " + _yes(settings.card_for_notice),
            "清晰度：" + str(settings.card_scale) + " 倍（约 "
            + str(PAGE_WIDTH * settings.card_scale) + " 像素宽）"
            + " ｜ " + ("PNG 无损" if settings.card_lossless else "JPEG 高质量"),
            "显示时间 " + _yes(settings.card_show_time)
            + " ｜ 显示头像 " + _yes(settings.card_show_avatar)
            + " ｜ 真实 QQ 头像 " + _yes(settings.card_use_real_avatar)
            + " ｜ 画出图片 " + _yes(settings.card_show_images),
            "渲染服务：" + ("暂时熔断中（会自动恢复）" if self.card.muted else "正常"),
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
        """走一遍完整的传话链路：文本 + 最近群聊记录卡片 + 图片转发。"""
        self._refresh_runtime()
        note = truncate(self._arg_text(event, "hr_feedback_test"), 200)
        if not self.settings.feedback_session_id:
            yield event.plain_result("还没绑定反馈窗口，先在目标会话里发 /hr_bind 或 /hr_bind_feedback。")
            return
        outcome = await self.feedback.relay(
            event=event,
            message=note or "这是一条 /hr_feedback_test 测试消息，用来确认我能不能找到你。",
            # 测试就要走满整条链路，所以连群里刚发过的图也一起试着带上。
            send_images=True,
            ignore_limits=True,
        )
        if outcome.ok:
            if outcome.chatlog_attached:
                attachment = "聊天记录卡片"
            elif outcome.chatlog_inlined:
                attachment = "纯文本聊天记录（卡片没画出来）"
            else:
                attachment = "无（没拉到群聊记录，或者没开这个开关）"
            if outcome.images_forwarded:
                attachment += " + " + str(outcome.images_forwarded) + " 张图"
            yield event.plain_result(
                "测试消息已发往：\n"
                + self.settings.feedback_session_id
                + "\n附件：" + attachment
                + "\n在那边可以用 /hr_recent 看列表、/hr_back 内容 回话。"
            )
        else:
            yield event.plain_result("测试消息没发出去：" + outcome.detail)

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
    # 谁找过我 / 回话
    # ------------------------------------------------------------------
    @filter.command("hr_recent", alias={"hr_tickets", "hr_ticket"})
    async def cmd_recent(self, event: AstrMessageEvent):
        """列出最近谁通过 Bot 找过你，带序号方便回话。"""
        if not self._can_view_watchlist(event):
            yield event.plain_result("只有管理员或上报窗口所在会话可以查看联系记录。")
            return
        raw = self._arg_text(event, "hr_recent", "hr_tickets", "hr_ticket")
        limit = 10
        if raw.isdigit():
            limit = max(1, min(50, int(raw)))
        yield event.plain_result(await self.feedback.format_recent(limit))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("hr_back", alias={"hr_reply"})
    async def cmd_back(self, event: AstrMessageEvent):
        """把回话带回原会话。默认回最近一次，也可以在内容前写序号。"""
        self._refresh_runtime()
        raw = self._arg_text(event, "hr_back", "hr_reply")
        if not raw:
            yield event.plain_result(
                "用法：/hr_back 你要说的话（默认回最近一次）\n"
                "      /hr_back 2 你要说的话（回 /hr_recent 里的第 2 条）"
            )
            return
        index = 1
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            index = max(1, int(parts[0]))
            raw = parts[1]
        ok, message = await self.feedback.reply_back(content=raw, index=index)
        yield event.plain_result(message if message else ("已回话。" if ok else "回话失败。"))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("hr_recent_clear", alias={"hr_ticket_clear"})
    async def cmd_recent_clear(self, event: AstrMessageEvent):
        """清空联系记录（不影响观察名单）。"""
        await self.store.clear_contacts()
        yield event.plain_result("联系记录已清空。")

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
            "【" + PLUGIN_DISPLAY_NAME + " v" + PLUGIN_VERSION + "】命令一览\n"
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
            "  /hr_feedback_test    走一遍传话链路，看看文本和卡片长什么样（管理员）\n"
            "\n"
            "· 谁找过我\n"
            "  /hr_recent [条数]    列出最近谁通过我找过你\n"
            "  /hr_back 内容        把回话带回最近那个人（管理员）\n"
            "  /hr_back 2 内容      回 /hr_recent 里的第 2 条（管理员）\n"
            "  /hr_recent_clear     清空联系记录（管理员）\n"
            "\n"
            "· 观察名单\n"
            "  /hr_watchlist        查看被上报过的人\n"
            "  /hr_watch_remove 键  移除一条\n"
            "  /hr_watch_clear      清空名单\n"
            "\n"
            "· 群消息抽查（需要 OneBot v11 协议端）\n"
            "  /hr_peek 群号 [条数] 抓一份该群最近的聊天记录（管理员）\n"
            "\n"
            "旧命令都还留着：/harassment_* 长命令、以及 /hr_tickets、/hr_reply、\n"
            "/hr_ticket_clear 现在分别指向 /hr_recent、/hr_back、/hr_recent_clear。\n"
            "详细说明见：" + REPO_URL
        )
