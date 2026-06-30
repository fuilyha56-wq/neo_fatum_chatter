"""NFC 感知阶段重试策略。

把"模型输出纯文本而非 tool_call 时该如何写入草稿、注入哪种 followup 提示"
这块决策从 ``chatter._send_with_perceive_loop`` 中独立出来。
真正的发送循环留在 chatter（它依赖 watchdog/stream_id），但策略部分纯逻辑、
便于单独覆盖。

设计原则：
    - 不直接 mutate session / response 字段以外的内容。
    - DeepSeek compat 优先；非 compat 路径回退到通用 followup。
    - 任何步骤抛出异常都不影响主链路（保留兜底）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text

from ..prompts.templates import NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING
from .compat_adapter import (
    build_tool_call_compat_retry_prompt,
    is_deepseek_model_set,
    rewrite_response_as_unsent_draft,
)

logger = get_logger("NFC_perception_retry")


@dataclass(slots=True)
class FollowupOutcome:
    """感知重试 followup 注入的结果摘要。"""

    used_compat: bool = False
    rewrote_draft: bool = False


def select_followup_prompt(response: Any) -> tuple[str, bool]:
    """根据 response 所属 provider 选择最合适的 followup 提示。

    Returns:
        ``(prompt_text, used_compat)``。
        ``used_compat`` 为 True 表示采用 DeepSeek compat JSON 重试模板。
    """
    if is_deepseek_model_set(getattr(response, "model_set", None)):
        compat_prompt = build_tool_call_compat_retry_prompt(
            getattr(response, "payloads", None)
        )
        if compat_prompt:
            return compat_prompt, True
    return NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING, False


def apply_perception_followup(response: Any, perceive_text: str) -> FollowupOutcome:
    """在 response 链上写入"未发送草稿"标记并注入 followup user payload。

    调用者通常已经检查过 ``response.call_list`` 为空（即模型本轮没产出
    工具调用）。本函数负责：

    1. 将 assistant payload 改写为 ``<unsent_perception_draft>`` 形式，
       以避免下一轮重试时被误读为已发送的回复历史。
    2. 选择 followup prompt（DeepSeek compat 优先，否则通用模板）。
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

    followup, used_compat = select_followup_prompt(response)
    if used_compat:
        logger.debug("[NFC] DeepSeek 纯文本重试使用 compat JSON 提示")
    outcome.used_compat = used_compat

    response.add_payload(LLMPayload(ROLE.USER, Text(followup)))
    return outcome
