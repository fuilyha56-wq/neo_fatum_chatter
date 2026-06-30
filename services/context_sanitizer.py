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


def _tool_calls(payload: Any) -> list[ToolCall]:
    """提取 assistant payload 中的 ToolCall。"""
    return [part for part in getattr(payload, "content", []) if isinstance(part, ToolCall)]


def _valid_tool_results(payload: Any) -> list[ToolResult]:
    """提取带 call_id 的 ToolResult，避免 provider 拒绝非法 tool_result。"""
    return [
        part
        for part in getattr(payload, "content", [])
        if isinstance(part, ToolResult) and bool(getattr(part, "call_id", None))
    ]


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


def _preview_roles(payloads: list[Any], index: int) -> list[str]:
    """生成局部 role 预览，辅助定位被清理的非法链路。"""
    start = max(0, index - 3)
    end = min(len(payloads), index + 4)
    return [str(getattr(item, "role", "?")) for item in payloads[start:end]]


def close_pending_tool_chain(response: Any, *, reason: str = "继续对话") -> bool:
    """必要时补 assistant 桥接，闭合尾部 tool_result 链。"""
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list) or not payloads:
        return False
    if getattr(payloads[-1], "role", None) != ROLE.TOOL_RESULT:
        return False

    logger.debug(f"[NFC] {reason}: 尾部 tool_result，补 assistant 桥接以闭合工具链")
    response.add_payload(LLMPayload(ROLE.ASSISTANT, Text("")))
    return True


def heal_orphan_tool_results(response: Any, *, where: str = "发送前") -> bool:
    """按 ToolCall.call_id 配对清理真正孤立的 tool_result。"""
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list) or not payloads:
        return False

    cleaned: list[LLMPayload] = []
    changed = False
    index = 0
    while index < len(payloads):
        payload = payloads[index]
        role = getattr(payload, "role", None)

        if role in _PINNED_ROLES:
            cleaned.append(payload)
            index += 1
            continue

        if role != ROLE.ASSISTANT:
            if role == ROLE.TOOL_RESULT:
                logger.warning(
                    f"[NFC] {where}: 删除孤立 tool_result idx={index}, "
                    f"near_roles={_preview_roles(payloads, index)}"
                )
                changed = True
            else:
                cleaned.append(payload)
            index += 1
            continue

        calls = _tool_calls(payload)
        if not calls:
            cleaned.append(payload)
            index += 1
            continue

        expected_ids = {str(call.id) for call in calls if getattr(call, "id", None)}
        if not expected_ids:
            logger.warning(
                f"[NFC] {where}: 删除缺少 id 的 assistant tool_calls idx={index}"
            )
            changed = True
            index += 1
            continue

        result_payloads: list[LLMPayload] = []
        seen_ids: set[str] = set()
        cursor = index + 1
        while cursor < len(payloads):
            candidate = payloads[cursor]
            candidate_role = getattr(candidate, "role", None)
            if candidate_role in _PINNED_ROLES:
                result_payloads.append(candidate)
                cursor += 1
                continue
            if candidate_role != ROLE.TOOL_RESULT:
                break

            valid_results: list[ToolResult] = []
            for result in _valid_tool_results(candidate):
                call_id = str(result.call_id)
                if call_id not in expected_ids:
                    logger.warning(
                        f"[NFC] {where}: 删除 call_id 不匹配的 tool_result "
                        f"call_id={call_id}, expected={sorted(expected_ids)}"
                    )
                    changed = True
                    continue
                if call_id in seen_ids:
                    logger.warning(
                        f"[NFC] {where}: 删除重复 tool_result call_id={call_id}"
                    )
                    changed = True
                    continue
                valid_results.append(result)
                seen_ids.add(call_id)

            if valid_results:
                if len(valid_results) != len(getattr(candidate, "content", [])):
                    candidate.content = valid_results
                    changed = True
                result_payloads.append(candidate)
            else:
                logger.warning(
                    f"[NFC] {where}: 删除空或非法 tool_result idx={cursor}, "
                    f"near_roles={_preview_roles(payloads, cursor)}"
                )
                changed = True
            cursor += 1

        missing = expected_ids - seen_ids
        if missing:
            # 尾部未闭合 = 本轮尚未执行，保留以等待 run_tool_call 回写 tool_result
            is_tail = cursor >= len(payloads)
            if is_tail:
                logger.debug(
                    f"[NFC] {where}: 保留尾部未闭合 assistant tool_calls idx={index}, "
                    f"missing={sorted(missing)} (本轮尚未执行)"
                )
                cleaned.append(payload)
                cleaned.extend(result_payloads)
                index = cursor
                continue

            # 非尾部 = 历史遗留，安全删除
            logger.warning(
                f"[NFC] {where}: 删除未闭合 assistant tool_calls idx={index}, "
                f"missing={sorted(missing)}"
            )
            changed = True
            index = cursor
            continue

        cleaned.append(payload)
        cleaned.extend(result_payloads)
        index = cursor

    if changed:
        response.payloads = cleaned
    return changed


def sanitize_payload_chain(response: Any, *, reason: str = "发送前") -> bool:
    """清洗 response.payloads 中会触发框架严格校验的相邻角色链。"""
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
            if last_convo_role not in {ROLE.ASSISTANT, ROLE.TOOL_RESULT} or not valid_results:
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
    healed = heal_orphan_tool_results(response, where=reason)
    closed = close_pending_tool_chain(response, reason=reason)
    sanitized = sanitize_payload_chain(response, reason=reason)
    return healed or closed or sanitized
