"""NFC 群聊 sub-agent 决策模块。

照搬 DFC ``decision_agent.py`` 核心逻辑：
- 使用 ``chatter.create_request("sub_actor", ...)`` 构建请求；
- 系统提示词来自 ``prompts.group_builder.build_group_sub_agent_prompt``；
- Token 预算裁剪沿用 ``_fit_unreads_to_sub_agent_budget``；
- ``json_repair.loads()`` 解析；
- 异常时默认 respond。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

import json_repair

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text
from src.kernel.llm.token_counter import count_text_tokens

if TYPE_CHECKING:
    from src.core.models.stream import ChatStream

    from ..config import NFCConfig

logger = get_logger("NFC_group_decision")


class GroupDecisionResult(TypedDict):
    """群聊 sub-agent 决策返回结构。"""

    should_respond: bool
    reason: str


# ─── Token 预算工具 ─────────────────────────────────────────────


def _safe_count_tokens(text: str, model_identifier: str) -> int:
    """安全计算文本 token 数量，失败时返回 0。"""
    try:
        return count_text_tokens(text, model_identifier=model_identifier)
    except Exception:
        return 0


def _trim_text_suffix_by_budget(
    text: str,
    model_identifier: str,
    token_budget: int,
) -> str:
    """保留文本尾部内容并控制在 token 预算内。"""
    if token_budget <= 0 or not text:
        return ""

    total_tokens = _safe_count_tokens(text, model_identifier)
    if total_tokens <= token_budget:
        return text

    # 按行从尾部保留
    lines = text.splitlines()
    kept_reversed: list[str] = []
    used_tokens = 0
    for line in reversed(lines):
        line_tokens = _safe_count_tokens(line, model_identifier)
        if kept_reversed and used_tokens + line_tokens > token_budget:
            break
        kept_reversed.append(line)
        used_tokens += line_tokens

    candidate = "\n".join(reversed(kept_reversed)).strip()
    if candidate and _safe_count_tokens(candidate, model_identifier) <= token_budget:
        return candidate

    # 二分回退
    left = 0
    right = len(text)
    best = text[-512:]
    while left <= right:
        middle = (left + right) // 2
        suffix = text[middle:]
        token_count = _safe_count_tokens(suffix, model_identifier)
        if token_count == 0 or token_count > token_budget:
            left = middle + 1
            continue
        best = suffix
        right = middle - 1

    return best.strip()


def _fit_unreads_to_budget(request: Any, unreads_text: str) -> str:
    """将未读消息压缩到 sub-agent 可控 token 预算内。"""
    model_set = getattr(request, "model_set", None)
    if not isinstance(model_set, list) or not model_set:
        return unreads_text

    first_model = model_set[0]
    if not isinstance(first_model, dict):
        return unreads_text

    model_identifier = first_model.get("model_identifier")
    if not isinstance(model_identifier, str) or not model_identifier:
        return unreads_text

    max_context = first_model.get("max_context")
    if isinstance(max_context, int) and max_context > 0:
        token_budget = min(max(1024, max_context // 4), 8000)
    else:
        token_budget = 6000

    return _trim_text_suffix_by_budget(unreads_text, model_identifier, token_budget)


# ─── 核心决策函数 ───────────────────────────────────────────────


async def decide_group_response(
    chatter: Any,
    unreads_text: str,
    chat_stream: "ChatStream",
    config: "NFCConfig",
) -> GroupDecisionResult:
    """执行群聊 sub-agent 决策并返回 should_respond 结果。

    Args:
        chatter: NeoFatumChatter 实例（需暴露 ``create_request``）
        unreads_text: 格式化后的未读消息文本
        chat_stream: 当前聊天流
        config: NFC 配置

    Returns:
        GroupDecisionResult: 包含 should_respond 和 reason
    """
    try:
        request = chatter.create_request(
            "sub_actor",
            "nfc_group_decision",
            with_reminder="sub_actor",
        )
    except (ValueError, KeyError):
        return {"should_respond": True, "reason": "未找到 sub_actor 配置，默认响应"}

    # 构建系统提示词
    from ..prompts.group_builder import build_group_sub_agent_prompt

    sub_prompt = await build_group_sub_agent_prompt(chat_stream, config)
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(sub_prompt)))

    # Token 预算裁剪
    fitted_unreads = _fit_unreads_to_budget(request, unreads_text)
    if len(fitted_unreads) < len(unreads_text):
        logger.info(
            "Sub-agent 输入已截断以控制上下文长度: "
            f"{len(unreads_text)} -> {len(fitted_unreads)} 字符"
        )

    request.add_payload(
        LLMPayload(ROLE.USER, Text(f"【新收到待判定消息】\n{fitted_unreads}"))
    )

    try:
        response = await request.send(stream=False)
        await response

        content = response.message
        if not content or not content.strip():
            logger.warning("Sub-agent 返回了空内容，默认进行响应")
            return {"should_respond": True, "reason": "模型未返回判断内容"}

        try:
            result = json_repair.loads(content)
            if isinstance(result, dict):
                return {
                    "should_respond": bool(result.get("should_respond", True)),
                    "reason": str(result.get("reason", "未提供理由")),
                }
        except Exception as error:
            logger.debug(f"Sub-agent JSON 解析失败: {error} | 内容: {content[:500]}")

        logger.warning(f"Sub-agent 无法找到有效的 JSON 结构: {content[:200]}...")
        return {"should_respond": True, "reason": "解析 JSON 失败，默认响应"}
    except Exception as error:
        logger.error(f"Sub-agent 决策过程异常: {error}", exc_info=True)
        return {"should_respond": True, "reason": f"执行异常: {error}"}
