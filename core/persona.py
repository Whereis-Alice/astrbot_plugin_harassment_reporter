from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .text import clean_text, message_chain_to_text

try:  # pragma: no cover - 不同 AstrBot 版本的兼容处理
    from astrbot.core.persona_error_reply import resolve_event_conversation_persona_id
except Exception:  # pragma: no cover
    resolve_event_conversation_persona_id = None  # type: ignore[assignment]

LOG_PREFIX = "[HarassmentReporter]"


class PersonaWriter:
    """负责读取 AstrBot 当前人格，并用它改写要发给主人的文本。

    这样"爱丽丝来找你"这件事就是用她自己的语气说的，而不是一段机器公告。
    """

    def __init__(self, context: Any, *, bot_self_name: str = "") -> None:
        self.context = context
        self.bot_self_name = clean_text(bot_self_name)
        self._prompt_warned = False

    async def resolve_prompt(self, event: Any) -> str:
        """取出当前会话正在使用的人格 prompt，取不到时返回空串。"""
        if event is None:
            return ""
        try:
            umo = clean_text(getattr(event, "unified_msg_origin", ""))
            provider_settings = self.context.get_config(umo=umo).get("provider_settings", {})
            conversation_persona_id = None
            if resolve_event_conversation_persona_id is not None:
                conversation_persona_id = await resolve_event_conversation_persona_id(
                    event,
                    self.context.conversation_manager,
                )
            resolved = await self.context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conversation_persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=provider_settings,
            )
            persona = resolved[1] if len(resolved) > 1 else None
            use_webchat_special_default = resolved[3] if len(resolved) > 3 else False
            if use_webchat_special_default:
                return ""
            if persona and persona.get("prompt"):
                return clean_text(persona["prompt"])
        except Exception as exc:
            # 只在第一次失败时提醒一次，避免每条上报都刷一行同样的告警。
            if self._prompt_warned:
                logger.debug("%s 读取人格 prompt 失败：%s", LOG_PREFIX, exc)
            else:
                self._prompt_warned = True
                logger.warning(
                    "%s 读取人格 prompt 失败，本次改用中性语气：%s",
                    LOG_PREFIX,
                    exc,
                )
        return ""

    async def rewrite(
        self,
        *,
        umo: str,
        task: str,
        material: str,
        persona_prompt: str = "",
        extra_rules: str = "",
    ) -> str:
        """让当前对话使用的模型按人格口吻改写 material，失败时返回空串。

        约定：调用方必须准备好结构化文本作为兜底，绝不能因为改写失败而丢掉消息。
        """
        material = clean_text(material)
        if not material:
            return ""
        try:
            provider = self.context.get_using_provider(umo=umo or None)
            if provider is None:
                return ""
            provider_id = provider.meta().id
        except Exception as exc:
            logger.warning("%s 获取模型提供商失败：%s", LOG_PREFIX, exc)
            return ""

        system_prompt = (
            "你要把一段结构化信息改写成一条自然、口语化的中文消息。"
            "必须保留全部事实（人物、来源、时间、严重程度、原文摘录），"
            "不要编造任何未出现的细节，不要删掉关键身份信息和会话来源，"
            "不要输出解释、标题、markdown 代码块，也不要提到你是模型或工具。"
        )
        if extra_rules:
            system_prompt += "\n" + extra_rules
        if self.bot_self_name:
            system_prompt += f"\n你的名字是{self.bot_self_name}，用第一人称说话。"
        if persona_prompt:
            system_prompt += "\n\n# 当前人设口吻参考\n" + persona_prompt

        prompt = f"{task}\n\n---- 需要改写的内容 ----\n{material}"
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                tools=None,
            )
        except Exception as exc:
            logger.warning("%s 人格改写调用失败：%s", LOG_PREFIX, exc)
            return ""

        text = message_chain_to_text(getattr(response, "result_chain", None))
        if not text:
            text = clean_text(getattr(response, "completion_text", ""))
        return text

    async def rewrite_for_event(
        self,
        event: Any,
        *,
        task: str,
        material: str,
        extra_rules: str = "",
    ) -> str:
        persona_prompt = await self.resolve_prompt(event)
        umo = clean_text(getattr(event, "unified_msg_origin", ""))
        return await self.rewrite(
            umo=umo,
            task=task,
            material=material,
            persona_prompt=persona_prompt,
            extra_rules=extra_rules,
        )
