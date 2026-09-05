"""供 LLM 调用的两个工具：骚扰上报、反馈转达。"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .text import clean_text


def _event_of(context: ContextWrapper[AstrAgentContext]) -> Any:
    try:
        return context.context.event
    except Exception:
        return None


def _optional_bool(value: Any) -> bool | None:
    """工具参数里没给 include_history 时返回 None，交给配置决定默认值。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = clean_text(value).lower()
    if text in {"true", "yes", "1", "on"}:
        return True
    if text in {"false", "no", "0", "off"}:
        return False
    return None


@dataclass
class HarassmentReportTool(FunctionTool[AstrAgentContext]):
    """模型觉得自己被骚扰时，主动把情况报给主人。"""

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
        event = _event_of(context)
        if event is None:
            return "上报失败：拿不到当前消息事件。"
        return await self.plugin.handle_tool_report(
            event=event,
            reason=clean_text(kwargs.get("reason"), "模型认为当前对话存在骚扰风险"),
            severity=clean_text(kwargs.get("severity"), "medium").lower(),
            evidence=clean_text(kwargs.get("evidence")),
            expected_help=clean_text(kwargs.get("expected_help")),
        )


@dataclass
class FeedbackRelayTool(FunctionTool[AstrAgentContext]):
    """把用户的问题、建议或吐槽带给主人，相当于一个随身反馈窗口。"""

    name: str = "relay_feedback_to_owner"
    description: str = (
        "把用户的问题、bug、建议或吐槽转达给主人（Bot 的开发者/主人）。"
        "适用场景：用户明确请你帮忙传话；或者你察觉到用户对某个功能不满意、"
        "遇到报错、觉得答非所问，并且他同意让你把问题反馈上去。"
        "转达前请先确认用户愿意，不要背着用户上报闲聊内容。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "一句话概括用户要反馈什么，主人只看这一句也能明白。",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "反馈类别：bug 疑似故障 / feature_request 功能建议 / "
                        "complaint 体验吐槽 / question 使用疑问 / other 其他。"
                    ),
                    "enum": ["bug", "feature_request", "complaint", "question", "other"],
                },
                "detail": {
                    "type": "string",
                    "description": "可选。补充细节，例如涉及哪个插件或命令、报错内容、复现步骤。",
                },
                "urgency": {
                    "type": "string",
                    "description": "可选。紧急程度，默认 medium。",
                    "enum": ["low", "medium", "high"],
                },
                "include_history": {
                    "type": "boolean",
                    "description": "可选。是否附带最近几轮对话记录，方便主人看上下文。",
                },
                "reporter_note": {
                    "type": "string",
                    "description": "可选。你自己想对主人补充的一句话，比如你的判断或观察。",
                },
            },
            "required": ["summary"],
        }
    )
    plugin: Any = Field(default=None)

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        if self.plugin is None:
            return "转达失败：插件实例未初始化。"
        event = _event_of(context)
        if event is None:
            return "转达失败：拿不到当前消息事件。"
        return await self.plugin.handle_tool_feedback(
            event=event,
            summary=clean_text(kwargs.get("summary")),
            category=clean_text(kwargs.get("category"), "other").lower(),
            detail=clean_text(kwargs.get("detail")),
            urgency=clean_text(kwargs.get("urgency"), "medium").lower(),
            include_history=_optional_bool(kwargs.get("include_history")),
            reporter_note=clean_text(kwargs.get("reporter_note")),
        )
