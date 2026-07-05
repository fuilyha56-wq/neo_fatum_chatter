"""NFC 提示词模块函数。

提供基于 PromptManager 的模板注入和上下文构建辅助函数。
"""

from __future__ import annotations

import datetime
import re

from src.app.plugin_system.api.config_api import get_config
from src.app.plugin_system.api.log_api import get_logger
from src.core.config import get_core_config
from src.core.prompt import get_prompt_manager, optional, wrap, min_len

from .templates import (
    NFC_SYSTEM_PROMPT,
    NFC_PROACTIVE_PROMPT,
    NFC_TIMEOUT_PROMPT,
    NFC_PROACTIVE_DECISION_TOOL_CALLING,
    NFC_REPLY_MODE_TOOL_CALLING,
)

logger = get_logger("NFC_prompts")

# NFC 系统提示词允许的全部占位名（与 register_nfc_prompts 中 policies 键一致）
_NFC_SYSTEM_PROMPT_PLACEHOLDERS: frozenset[str] = frozenset({
    "nickname", "alias_names",
    "personality_core", "personality_side", "identity",
    "personality_core_line", "personality_side_line", "identity_line",
    "background_story", "reply_style",
    "safety_guidelines", "negative_behaviors_section",
    "reply_mode_instruction",
    "platform", "chat_type", "bot_id", "stream_id",
    "mental_log_hint", "theme_guide",
    "custom_decision_prompt", "scene_state_info",
    "current_time",
    "segment_instruction", "wait_instruction",
})

# 必含的 6 大核心标签，保证提示词结构完整
_NFC_REQUIRED_TAGS: tuple[str, ...] = (
    "existence_logic", "personality", "behavioral_guidance",
    "the_inner_voice", "tool_usage", "extra_context",
)

_TAG_PATTERN = re.compile(r"</?([a-zA-Z_][a-zA-Z0-9_]*)\s*>")
_PLACEHOLDER_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _validate_system_prompt_override(template: str) -> tuple[bool, str]:
    """校验自定义系统提示词。

    Returns:
        (ok, reason): ok=True 时可使用；ok=False 时 reason 说明打回原因。
    """
    if not template or not template.strip():
        return False, "空模板，使用默认"

    # 1. XML 标签开闭配对（栈式匹配）
    stack: list[str] = []
    for match in _TAG_PATTERN.finditer(template):
        tag = match.group(1)
        full = match.group(0)
        if full.startswith("</"):
            if not stack or stack[-1] != tag:
                return False, f"XML 标签未配对：闭合 </{tag}> 无对应开标签"
            stack.pop()
        else:
            stack.append(tag)
    if stack:
        return False, f"XML 标签未配对：开标签 <{stack[-1]}> 未闭合"

    # 2. 必含 6 大核心标签
    for required in _NFC_REQUIRED_TAGS:
        if f"<{required}>" not in template or f"</{required}>" not in template:
            return False, f"缺少核心标签 <{required}>"

    # 3. 所有 {占位} 必须在 NFC 可渲染占位集合内
    for match in _PLACEHOLDER_PATTERN.finditer(template):
        name = match.group(1)
        if name not in _NFC_SYSTEM_PROMPT_PLACEHOLDERS:
            return False, f"占位 {{{name}}} 无法被 NFC 渲染"

    return True, ""


def _resolve_system_prompt_template() -> str:
    """读取 config 中的 system_prompt_override 并校验，失败回退默认模板。"""
    nfc_config = get_config("neo_fatum_chatter")
    override = ""
    if nfc_config is not None:
        override = getattr(
            getattr(nfc_config, "prompt", None),
            "system_prompt_override",
            "",
        ) or ""

    if not override.strip():
        return NFC_SYSTEM_PROMPT

    # 与默认模板一致时静默走默认分支，不打 info 日志
    if override == NFC_SYSTEM_PROMPT:
        return NFC_SYSTEM_PROMPT

    ok, reason = _validate_system_prompt_override(override)
    if ok:
        logger.info("NFC 系统提示词使用用户自定义模板")
        return override

    logger.warning(f"NFC 自定义系统提示词校验失败，回退默认：{reason}")
    return NFC_SYSTEM_PROMPT


def register_nfc_prompts() -> None:
    """注册 NFC 所有提示词模板到 PromptManager。

    在 plugin.on_plugin_loaded() 中调用一次即可。
    """
    config = get_core_config()
    personality = config.personality

    pm = get_prompt_manager()

    # 主系统提示词（支持 config.system_prompt_override 自定义，校验失败回退默认）
    pm.get_or_create(
        name="NFC_system_prompt",
        template=_resolve_system_prompt_template(),
        policies={
            "nickname": optional(personality.nickname),
            "alias_names": optional("、".join(personality.alias_names)),
            "personality_core": optional(personality.personality_core),
            "personality_side": optional(personality.personality_side),
            "personality_core_line": optional(personality.personality_core)
            .then(min_len(1))
            .then(wrap("你", "\n")),
            "personality_side_line": optional(personality.personality_side)
            .then(min_len(1))
            .then(wrap("", "。\n")),
            "identity": optional(personality.identity),
            "identity_line": optional(personality.identity)
            .then(min_len(1))
            .then(wrap("你的身份是", "。\n")),
            "background_story": optional(personality.background_story)
            .then(min_len(10))
            .then(
                wrap(
                    "# 背景故事\n",
                    "\n- （以上为背景知识，请理解并作为行动依据，但不要在对话中直接复述。）",
                )
            ),
            "reply_style": optional(personality.reply_style),
            "safety_guidelines": optional(
                "\n".join(personality.safety_guidelines)
            ),
            "negative_behaviors_section": optional(
                "\n".join(personality.negative_behaviors)
            ).then(min_len(1)).then(
                wrap(
                    "<absolute_prohibitions>\n以下行为绝对禁止，无论任何情境你都不得违反：\n",
                    "\n</absolute_prohibitions>",
                )
            ),
            "custom_decision_prompt": optional(""),
            "scene_state_info": optional(""),
            # reply_mode_instruction 由 _build_initial_context 动态注入，此处提供 tool calling 兜底
            "reply_mode_instruction": optional(NFC_REPLY_MODE_TOOL_CALLING),
            "current_time": optional(
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ),
        },
    )

    # 主动发起提示词
    pm.get_or_create(
        name="NFC_proactive_prompt",
        template=NFC_PROACTIVE_PROMPT,
        policies={
            "current_time": optional(
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            ),
            "silence_duration": optional("未知"),
            "recent_activity": optional("（无近期活动记录）"),
        },
    )


def build_mental_log_hint() -> str:
    """构建活动流格式提示。"""
    return (
        "你的活动流会以线性叙事的形式呈现在消息中，"
        "帮助你回顾之前的互动和内心活动。"
    )


async def build_proactive_context(
    silence_minutes: float,
    recent_activity: str,
    scheduled_reason: str = "",
) -> str:
    """构建主动发起上下文。"""
    pm = get_prompt_manager()
    tmpl = pm.get_template("NFC_proactive_prompt")
    if not tmpl:
        return f"已沉默 {silence_minutes:.0f} 分钟"

    # 格式化沉默持续时间为可读文本
    if silence_minutes >= 60:
        hours = silence_minutes / 60
        silence_str = f"{hours:.1f} 小时"
    else:
        silence_str = f"{silence_minutes:.0f} 分钟"

    decision_instruction = NFC_PROACTIVE_DECISION_TOOL_CALLING

    result = await (
        tmpl.clone()
        .set("current_time", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        .set("silence_duration", silence_str)
        .set("recent_activity", recent_activity or "（无近期活动记录）")
        .set("proactive_decision_instruction", decision_instruction)
        .build()
    )

    if scheduled_reason:
        result = f"【你在上次对话结束时为这次主动发起做了预约，预约理由：{scheduled_reason}】\n\n" + result

    return result


def build_timeout_context(
    elapsed_seconds: float,
    expected_reaction: str,
    consecutive_timeouts: int,
    last_bot_message: str = "",
    max_consecutive_timeouts: int = 3,
) -> str:
    """构建等待超时决策上下文。

    Args:
        elapsed_seconds: 已等待秒数
        expected_reaction: 预期对方的反应
        consecutive_timeouts: 连续超时次数（含本次）
        last_bot_message: 最后一条 Bot 发送的消息
        max_consecutive_timeouts: 配置的连续超时上限
    """
    elapsed_minutes = elapsed_seconds / 60
    is_first = consecutive_timeouts == 1
    is_last = consecutive_timeouts >= max_consecutive_timeouts
    msg_snippet = last_bot_message or "（消息内容不可用）"

    # ── 情境描述 ──
    if is_first:
        timeout_situation = (
            f"你发出消息已经过去 {elapsed_minutes:.0f} 分钟了，对方还没有回应。\n"
            f"**你发的最后一条消息**：「{msg_snippet}」"
        )
    else:
        timeout_situation = (
            f"你已经主动说了 {consecutive_timeouts} 次，对方一直没有回应。\n"
            f"距上次发消息已有 {elapsed_minutes:.0f} 分钟。\n"
            f"**你最后说的**：「{msg_snippet}」"
        )

    # ── 引导语 ──
    if is_last:
        timeout_guidance = (
            "你已经等了很久，对方始终没有出现。\n"
            "这种时候，你会怎么做？"
        )
    elif is_first:
        timeout_guidance = (
            "你想想：有没有什么没说完的话，或者忽然想到什么想跟对方说的？\n"
            "如果有，发出去就好；如果脑子里没什么，继续等一等也无妨。"
        )
    else:
        timeout_guidance = (
            "对方一直没有回复。\n"
            "你有没有真的需要说的内容——还是只是想打破沉默？"
        )

    # ── 操作指令 ──
    if is_last:
        decision_instructions = (
            "本次等待到此为止，**不得**再设置新的等待（`max_wait_seconds` 必须为 0）。"
        )
    elif is_first:
        decision_instructions = (
            "可以调用 `nfc_reply(...)` 发送消息，"
            "或调用 `do_nothing(max_wait_seconds>0)` 继续等待，"
            "或调用 `do_nothing(max_wait_seconds=0)` 结束等待。"
        )
    else:
        decision_instructions = (
            "如果确实有话说，可以调用 `nfc_reply(...)` 发送消息；"
            "或调用 `do_nothing(max_wait_seconds=0)` 结束等待。"
        )

    return NFC_TIMEOUT_PROMPT.format(
        timeout_situation=timeout_situation,
        timeout_guidance=timeout_guidance,
        decision_instructions=decision_instructions,
    )
