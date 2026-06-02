"""NFC 群聊提示词构建器。

群聊路径与 NFC 私聊心理活动流完全解耦。本模块负责：

1. 注册三套群聊模板（system / user / sub_agent）到 PromptManager；
2. 提供 ``build_group_system_prompt`` / ``build_group_user_prompt`` /
   ``build_group_sub_agent_prompt`` 三个异步构建器；
3. 把 ``[prompts]`` Section 中的覆盖文本应用到对应槽位。

模板定义集中在 ``group_templates.py``。所有渲染都通过 PromptManager 完成，
和 DefaultChatter 的提示词管线对齐，方便 prompt_injector 等插件做事件挂钩。
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from src.core.config import get_core_config
from src.core.prompt import get_prompt_manager, optional, wrap, min_len

from .group_templates import (
    NFC_GROUP_ACTION_SUSPEND_GUIDANCE_DISABLED,
    NFC_GROUP_ACTION_SUSPEND_GUIDANCE_ENABLED,
    NFC_GROUP_SUB_AGENT_PROMPT,
    NFC_GROUP_SYSTEM_PROMPT,
    NFC_GROUP_USER_PROMPT,
)

if TYPE_CHECKING:
    from src.core.models.message import Message
    from src.core.models.stream import ChatStream

    from ..config import NFCConfig


_GROUP_SYSTEM_PROMPT_NAME = "nfc_group_system_prompt"
_GROUP_USER_PROMPT_NAME = "nfc_group_user_prompt"
_GROUP_SUB_AGENT_PROMPT_NAME = "nfc_group_sub_agent_prompt"


def register_nfc_group_prompts() -> None:
    """注册 NFC 群聊场景的所有提示词模板。

    在 ``plugin.on_plugin_loaded()`` 中调用一次即可。模板使用项目内置的
    ``optional`` / ``wrap`` / ``min_len`` 策略，与 DFC 模板风格一致。
    """
    config = get_core_config()
    personality = config.personality

    pm = get_prompt_manager()

    pm.get_or_create(
        name=_GROUP_SYSTEM_PROMPT_NAME,
        template=NFC_GROUP_SYSTEM_PROMPT,
        policies={
            "nickname": optional(personality.nickname),
            "alias_names": optional("、".join(personality.alias_names)),
            "personality_core": optional(personality.personality_core),
            "personality_side": optional(personality.personality_side),
            "identity": optional(personality.identity),
            "background_story": optional(personality.background_story)
            .then(min_len(10))
            .then(
                wrap(
                    "# 背景故事\n",
                    "\n- （以上为背景知识，请理解并作为行动依据，但不要在对话中直接复述。）",
                )
            ),
            "reply_style": optional(personality.reply_style),
            "safety_guidelines": optional("\n".join(personality.safety_guidelines)),
            "negative_behaviors": optional("\n".join(personality.negative_behaviors)),
            "theme_guide": optional(""),
            "action_suspend_guidance": optional(""),
            "segment_instruction": optional(""),
        },
    )

    pm.get_or_create(
        name=_GROUP_USER_PROMPT_NAME,
        template=NFC_GROUP_USER_PROMPT,
        policies={
            "stream_name": optional("未知对话"),
            "current_time": optional("未知时间"),
            "platform": optional("未知平台"),
            "chat_type": optional("未知类型"),
            "platform_name": optional("未知"),
            "platform_id": optional("未知ID"),
            "history": optional("")
            .then(min_len(2))
            .then(
                wrap(
                    "# 历史消息\n",
                    "\n- （以上为历史消息摘要，供你参考了解之前的对话历史但不必复述）",
                )
            ),
            "unreads": optional("")
            .then(min_len(2))
            .then(
                wrap(
                    "# 新收到的消息\n",
                    "\n- （以上为新收到的消息，请基于这些消息生成回复）",
                )
            ),
            "extra": optional("")
            .then(min_len(2))
            .then(wrap("# 额外信息\n", "\n- （以上为额外信息，你可以适当参考）")),
        },
    )

    pm.get_or_create(
        name=_GROUP_SUB_AGENT_PROMPT_NAME,
        template=NFC_GROUP_SUB_AGENT_PROMPT,
        policies={
            "nickname": optional(personality.nickname),
            "bot_id": optional(""),
            "bot_id_section": optional(""),
            "personality_core_section": optional(personality.personality_core)
            .then(wrap("它的核心人格是：", "\n")),
            "personality_side_section": optional(personality.personality_side)
            .then(wrap("它的人格侧面是：", "\n")),
        },
    )


def _build_action_suspend_guidance(enabled: bool) -> str:
    """根据开关返回 Action SUSPEND 指引文本。"""
    return (
        NFC_GROUP_ACTION_SUSPEND_GUIDANCE_ENABLED
        if enabled
        else NFC_GROUP_ACTION_SUSPEND_GUIDANCE_DISABLED
    )


async def build_group_system_prompt(
    chat_stream: "ChatStream",
    config: "NFCConfig",
    *,
    enable_action_suspend: bool = True,
) -> str:
    """构建群聊系统提示词。

    Args:
        chat_stream: 当前聊天流
        config: NFC 配置
        enable_action_suspend: 当本轮全是 action 调用时，是否要求 LLM 输出
            ``__SUSPEND__`` 占位。当前群聊状态机始终启用此约束。
    """
    pm = get_prompt_manager()
    tmpl = pm.get_template(_GROUP_SYSTEM_PROMPT_NAME)
    if not tmpl:
        return ""

    return await (
        tmpl.set("nickname", chat_stream.bot_nickname or "")
        .set("theme_guide", config.prompts.group_theme_guide)
        .set("segment_instruction", config.prompts.segment_instruction)
        .set(
            "action_suspend_guidance",
            _build_action_suspend_guidance(enable_action_suspend),
        )
        .build()
    )


def build_group_history_text(
    chat_stream: "ChatStream",
    formatter,
) -> str:
    """构建群聊历史消息文本。

    Args:
        chat_stream: 当前聊天流
        formatter: 单条消息格式化函数，与 ``BaseChatter.format_message_line`` 同型

    Returns:
        以换行连接的格式化历史消息
    """
    history_lines: list[str] = []
    history_messages = getattr(getattr(chat_stream, "context", None), "history_messages", []) or []
    for msg in history_messages:
        history_lines.append(formatter(msg))
    return "\n".join(history_lines)


async def build_group_user_prompt(
    chat_stream: "ChatStream",
    history_text: str,
    unread_lines: str,
    extra: str = "",
) -> str:
    """构建群聊 USER 提示词。

    Args:
        chat_stream: 当前聊天流
        history_text: 已格式化的历史文本
        unread_lines: 已格式化的未读消息文本
        extra: 额外信息文本（可选）
    """
    from src.app.plugin_system.api import adapter_api

    bot_info = await adapter_api.get_bot_info_by_platform(chat_stream.platform) or {}
    platform_name = str(
        bot_info.get("bot_name") or chat_stream.bot_nickname or "未知"
    )
    platform_id = str(
        bot_info.get("bot_id") or chat_stream.bot_id or "未知"
    )
    stream_name = chat_stream.stream_name

    pm = get_prompt_manager()
    tmpl = pm.get_template(_GROUP_USER_PROMPT_NAME)
    if not tmpl:
        return f"{history_text}\n\n{unread_lines}"

    return await (
        tmpl.set("stream_name", stream_name)
        .set("current_time", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        .set("platform", chat_stream.platform)
        .set("chat_type", chat_stream.chat_type)
        .set("platform_id", platform_id)
        .set("platform_name", platform_name)
        .set("history", history_text)
        .set("unreads", unread_lines)
        .set("extra", extra)
        .build()
    )


async def build_group_sub_agent_prompt(
    chat_stream: "ChatStream",
    config: "NFCConfig",
) -> str:
    """构建群聊 sub-agent 决策系统提示词。

    优先使用 ``config.prompts.sub_agent_prompt`` 的覆盖值；为空时回退到
    模板系统中注册的内置默认值。
    """
    nickname = get_core_config().personality.nickname
    bot_id = chat_stream.bot_id or ""
    bot_id_section = f"它的 QQ 号是 {bot_id}。\n" if bot_id else ""

    override = (config.prompts.sub_agent_prompt or "").strip()
    if override:
        try:
            return override.format(
                nickname=nickname,
                bot_id=bot_id,
                bot_id_section=bot_id_section,
            )
        except (KeyError, IndexError):
            # 占位符不完整时，按原样返回，避免渲染崩溃
            return override

    pm = get_prompt_manager()
    tmpl = pm.get_template(_GROUP_SUB_AGENT_PROMPT_NAME)
    if not tmpl:
        personality = get_core_config().personality
        personality_core_section = (
            f"它的核心人格是：{personality.personality_core}\n"
            if personality.personality_core
            else ""
        )
        personality_side_section = (
            f"它的人格侧面是：{personality.personality_side}\n"
            if personality.personality_side
            else ""
        )
        return NFC_GROUP_SUB_AGENT_PROMPT.format(
            nickname=nickname,
            bot_id=bot_id,
            bot_id_section=bot_id_section,
            personality_core_section=personality_core_section,
            personality_side_section=personality_side_section,
        )

    return await (
        tmpl.set("nickname", nickname)
        .set("bot_id", bot_id)
        .set("bot_id_section", bot_id_section)
        .build()
    )


__all__ = [
    "build_group_history_text",
    "build_group_sub_agent_prompt",
    "build_group_system_prompt",
    "build_group_user_prompt",
    "register_nfc_group_prompts",
]
