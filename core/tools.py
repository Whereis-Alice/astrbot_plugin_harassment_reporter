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
    """让模型主动去找主人带句话，相当于给它加了一条随身的传话通道。"""

    name: str = "relay_feedback_to_owner"
    description: str = (
        "主动去找主人（Bot 的开发者/主人）带一句话，就像你自己走出去喊他一声。"
        "适用场景：用户请你帮忙找主人或传话；或者你察觉到用户遇到报错、功能不好用、"
        "答非所问，并且他愿意让你把这件事告诉主人。"
        "调用时把要对主人说的话完整写进 message，用你自己的口吻写，"
        "就像你亲自去找他说话一样，说清楚是谁在哪里、遇到了什么。"
        "最近的群聊记录会自动附上，你不用复述聊天内容。"
        "带话前请先确认用户愿意，不要背着用户上报闲聊。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "你要对主人说的完整一句话，用你自己的口吻写，"
                        "例如「群里有人找你呀，说是画图插件坏了」。"
                        "不要写成工单或模板，也不要只写一个关键词。"
                    ),
                },
            },
            "required": ["message"],
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
            message=clean_text(kwargs.get("message")),
        )
