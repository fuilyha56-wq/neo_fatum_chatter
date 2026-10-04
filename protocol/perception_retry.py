"""NFC 感知阶段重试策略。

把"模型输出纯文本而非 tool_call 时该如何写入草稿、注入哪种 followup 提示"
这块决策从 ``chatter._send_with_perceive_loop`` 中独立出来。
真正的发送循环留在 chatter（它依赖 watchdog/stream_id），但策略部分纯逻辑、
便于单独覆盖。

设计原则：
    - 不直接 mutate session / response 字段以外的内容。
    - DeepSeek compat 优先；非 compat 路径回退到通用 followup。
    - 提示按重试进度逐次升级：首次纯文本用温和引导，之后升级为硬纠正
      （学习自 KFC：明确告知纯文本不会被执行，务必通过工具调用收口）。
    - 任何步骤抛出异常都不影响主链路（保留兜底）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text

from ..prompts.templates import (
    NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING,
    NFC_PERCEIVE_HARD_REMINDER_PROMPT,
)
from .compat_adapter import (
    build_tool_call_compat_retry_prompt,
    rewrite_response_as_unsent_draft,
)

logger = get_logger("NFC_perception_retry")


@dataclass(slots=True)
class FollowupOutcome:
    """感知重试 followup 注入的结果摘要。"""

    used_compat: bool = False
    rewrote_draft: bool = False
    escalated: bool = False


def select_followup_prompt(
    response: Any, attempt: int = 0, is_final: bool = False
) -> tuple[str, bool, bool]:
    """根据 response 所属 provider 与重试进度选择最合适的 followup 提示。

    Args:
        response: 本轮 LLM 响应链对象。
        attempt: 已发生的纯文本重试序号（0 表示第一次纯文本后的引导）。
        is_final: 本次注入是否服务于最后一次重试。此时没有再失败的余地，
            直接升级为硬纠正提醒——否则在 max_retries=1 的常规配置下
            硬提醒永远轮不到注入。

    Returns:
        ``(prompt_text, used_compat, escalated)``。
        ``used_compat`` 为 True 表示采用 DeepSeek compat JSON 重试模板；
        ``escalated`` 为 True 表示本次已升级为硬纠正提醒。
    """
    model_set = getattr(response, "model_set", None)
    if isinstance(model_set, list):
        uses_compat = any(
            isinstance(entry, dict) and entry.get("tool_call_compat") is True
            for entry in model_set
        )
    else:
        uses_compat = isinstance(model_set, dict) and model_set.get(
            "tool_call_compat"
        ) is True

    if uses_compat:
        compat_prompt = build_tool_call_compat_retry_prompt(
            getattr(response, "payloads", None)
        )
        if compat_prompt:
            return compat_prompt, True, True

    escalated = attempt >= 1 or is_final
    if escalated:
        return NFC_PERCEIVE_HARD_REMINDER_PROMPT, False, True
    return NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING, False, False


def apply_perception_followup(
    response: Any, perceive_text: str, attempt: int = 0, is_final: bool = False
) -> FollowupOutcome:
    """在 response 链上写入"未发送草稿"标记并注入 followup user payload。

    调用者通常已经检查过 ``response.call_list`` 为空（即模型本轮没产出
    工具调用）。本函数负责：

    1. 将 assistant payload 改写为 ``<unsent_perception_draft>`` 形式，
       以避免下一轮重试时被误读为已发送的回复历史。
    2. 选择 followup prompt（DeepSeek compat 优先，否则通用模板）；
       重试不止一次或本次为最后一次重试时升级为硬纠正提醒。
    3. 把 followup 作为 USER payload 追加到链尾。

    Returns:
        ``FollowupOutcome`` 用于审计 followup 选择路径。
    """
    outcome = FollowupOutcome()

    rewrote = False
    try:
        rewrote = rewrite_response_as_unsent_draft(response, perceive_text)
    except Exception as exc:
        logger.debug(f"[NFC] perception draft 改写失败: {exc}")
    if not rewrote:
        logger.debug("[NFC] 未能将纯文本响应改写为未发送草稿，保留原始上下文")
    outcome.rewrote_draft = rewrote

    followup, used_compat, escalated = select_followup_prompt(
        response, attempt, is_final
    )
    if used_compat:
        logger.debug("[NFC] DeepSeek 纯文本重试使用 compat JSON 提示")
    elif escalated:
        logger.info("[NFC] 纯文本重试再次失败，升级为硬纠正提醒")
    outcome.used_compat = used_compat
    outcome.escalated = escalated

    response.add_payload(LLMPayload(ROLE.USER, Text(followup)))
    return outcome
