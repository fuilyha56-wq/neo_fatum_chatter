"""NFC LLM payload 链路清洗工具。"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolResult

logger = get_logger("NFC_context_sanitizer")

_PINNED_ROLES = {ROLE.SYSTEM, ROLE.TOOL}


def _has_tool_call(payload: Any) -> bool:
    """判断 assistant payload 是否包含 ToolCall。"""
    return any(isinstance(part, ToolCall) for part in getattr(payload, "content", []))


def _valid_tool_results(payload: Any) -> list[ToolResult]:
    """提取带 call_id 的 ToolResult，避免 provider 拒绝非法 tool_result。"""
    return [
        part
        for part in getattr(payload, "content", [])
        if isinstance(part, ToolResult) and bool(getattr(part, "call_id", None))
    ]


def _has_tool_result(payload: Any) -> bool:
    """判断 tool_result payload 是否包含带 call_id 的 ToolResult。"""
    return bool(_valid_tool_results(payload))


def _payload_text(payload: Any) -> str:
    """提取 payload 中的纯文本内容，用于判断是否为空桥接。"""
    parts: list[str] = []
    for part in getattr(payload, "content", []):
        if isinstance(part, Text):
            parts.append(part.text)
    return "".join(parts).strip()


def _merge_payload_content(target: LLMPayload, source: LLMPayload) -> None:
    """把 source 的内容并入 target，避免产生相邻同角色 payload。"""
    if not getattr(source, "content", None):
        return
    if not getattr(target, "content", None):
        target.content = list(source.content)
        return
    target.content.extend(source.content)


def close_pending_tool_chain(response: Any, *, reason: str = "继续对话") -> bool:
    """必要时补 assistant 桥接，闭合尾部 tool_result 链。

    当前框架要求 tool_result 后必须由 assistant 承接，之后才能进入新的 user。
    这里仅在尾部确实是 tool_result 时补一个最小 assistant，不碰其它链路，别乱改啊笨蛋！
    """
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list) or not payloads:
        return False
    if getattr(payloads[-1], "role", None) != ROLE.TOOL_RESULT:
        return False

    logger.debug(f"[NFC] {reason}: 尾部 tool_result，补 assistant 桥接以闭合工具链")
    response.add_payload(LLMPayload(ROLE.ASSISTANT, Text("")))
    return True


def sanitize_payload_chain(response: Any, *, reason: str = "发送前") -> bool:
    """清洗 response.payloads 中会触发框架严格校验的相邻角色链。

    目标：
    1. 移除首个 user 之前的孤立 assistant/tool_result；
    2. 合并普通 assistant -> assistant；
    3. 保留 assistant(tool_calls) -> tool_result -> assistant 的标准工具链；
    4. 避免 tool_result 后直接进入 user。
    """
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list) or not payloads:
        return False

    changed = False
    cleaned: list[LLMPayload] = []
    last_convo_role: ROLE | None = None
    seen_user = False

    for payload in payloads:
        role = getattr(payload, "role", None)
        if role in _PINNED_ROLES:
            cleaned.append(payload)
            continue

        if role == ROLE.USER:
            if last_convo_role == ROLE.TOOL_RESULT:
                bridge = LLMPayload(ROLE.ASSISTANT, Text(""))
                cleaned.append(bridge)
                last_convo_role = ROLE.ASSISTANT
                changed = True
                logger.debug(f"[NFC] {reason}: user 前补 assistant 桥接，闭合 tool_result")
            cleaned.append(payload)
            last_convo_role = ROLE.USER
            seen_user = True
            continue

        if role == ROLE.ASSISTANT:
            if not seen_user and last_convo_role is None:
                changed = True
                logger.debug(f"[NFC] {reason}: 移除首个 user 前孤立 assistant")
                continue

            if last_convo_role == ROLE.ASSISTANT:
                previous = next(
                    (
                        item
                        for item in reversed(cleaned)
                        if getattr(item, "role", None) not in _PINNED_ROLES
                    ),
                    None,
                )
                if (
                    isinstance(previous, LLMPayload)
                    and previous.role == ROLE.ASSISTANT
                    and not _has_tool_call(previous)
                    and not _has_tool_call(payload)
                ):
                    if _payload_text(payload):
                        _merge_payload_content(previous, payload)
                        logger.debug(f"[NFC] {reason}: 合并连续 assistant payload")
                    else:
                        logger.debug(f"[NFC] {reason}: 丢弃空的连续 assistant payload")
                    changed = True
                    continue

                if not _payload_text(payload) and not _has_tool_call(payload):
                    changed = True
                    logger.debug(f"[NFC] {reason}: 丢弃非法连续空 assistant payload")
                    continue

                bridge = LLMPayload(ROLE.USER, Text("请继续根据上文完成本轮决策。"))
                cleaned.append(bridge)
                last_convo_role = ROLE.USER
                changed = True
                logger.debug(f"[NFC] {reason}: 连续 assistant 无法合并，插入 user 桥接")

            cleaned.append(payload)
            last_convo_role = ROLE.ASSISTANT
            continue

        if role == ROLE.TOOL_RESULT:
            valid_results = _valid_tool_results(payload)
            if last_convo_role != ROLE.ASSISTANT or not valid_results:
                changed = True
                logger.debug(f"[NFC] {reason}: 丢弃孤立、空或缺少 call_id 的 tool_result payload")
                continue
            if len(valid_results) != len(getattr(payload, "content", [])):
                payload.content = valid_results
                changed = True
                logger.debug(f"[NFC] {reason}: 移除缺少 call_id 的 tool_result 内容")
            cleaned.append(payload)
            last_convo_role = ROLE.TOOL_RESULT
            continue

        cleaned.append(payload)

    if changed:
        response.payloads = cleaned
    return changed


def prepare_payload_chain_for_send(response: Any, *, reason: str = "发送前") -> bool:
    """发送 LLM 前统一整理 payload 链。"""
    closed = close_pending_tool_chain(response, reason=reason)
    sanitized = sanitize_payload_chain(response, reason=reason)
    return closed or sanitized
