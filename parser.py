"""NFC 工具调用解析器。

这一版保留原插件全部能力，但把实际执行统一收敛到 MoFox 标准
`BaseChatter.run_tool_call()` / `src.core.utils.llm_tool_call.run_tool_call()` 链路。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolResult

from .execution.reply_executor import coerce_content_segments, sanitize_segment
from .execution.result import ExecutionResult
from .models import DO_NOTHING, NFC_REPLY, ToolCallResult
from .protocol.call_resolver import (
    normalize_call_name as _normalize_call_name,
    resolve_registered_call_name as _resolve_registered_call_name,
    retarget_call_name as _retarget_call_name,
)
from .services.perception_extractor import extract_reply_from_perception

if TYPE_CHECKING:
    from src.kernel.llm import ToolRegistry

    from .config import NFCConfig

logger = get_logger("NFC_parser")

_RESULT_DEPENDENT_ACTIONS = {
    "nfc_query_activity_pattern",
    "nfc_query_habits",
    "nfc_query_proactive_status",
}


def coerce_call_list(response: Any) -> list[Any]:
    """将 response.call_list 规整为列表，兼容单个 ToolCall 形态。"""
    raw_call_list = getattr(response, "call_list", None)
    if raw_call_list is None:
        normalized_calls: list[Any] = []
    elif isinstance(raw_call_list, list):
        normalized_calls = raw_call_list
    elif isinstance(raw_call_list, tuple):
        normalized_calls = list(raw_call_list)
    elif hasattr(raw_call_list, "name") and hasattr(raw_call_list, "args"):
        normalized_calls = [raw_call_list]
    else:
        try:
            normalized_calls = list(raw_call_list)
        except TypeError:
            normalized_calls = [raw_call_list]

    try:
        response.call_list = normalized_calls
    except Exception:
        pass
    return normalized_calls


def _tool_result_call_counts(response: Any) -> dict[str, int]:
    """统计 response 链中各 tool_result call_id 的出现次数。"""
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list):
        return {}

    call_counts: dict[str, int] = {}
    for payload in payloads:
        if getattr(payload, "role", None) != ROLE.TOOL_RESULT:
            continue
        for part in getattr(payload, "content", []) or []:
            if isinstance(part, ToolResult) and part.call_id:
                call_id = str(part.call_id)
                call_counts[call_id] = call_counts.get(call_id, 0) + 1
    return call_counts


def _has_new_tool_result(
    call_id: str | None,
    before_counts: dict[str, int],
    after_counts: dict[str, int],
) -> bool:
    """判断指定调用是否在本轮新增了 ToolResult。"""
    if call_id is None:
        return False
    normalized_id = str(call_id)
    return after_counts.get(normalized_id, 0) > before_counts.get(normalized_id, 0)


def _latest_tool_result(response: Any, call_id: str | None) -> ToolResult | None:
    """读取指定 call_id 最近写回的 ToolResult。"""
    if not call_id:
        return None
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list):
        return None

    for payload in reversed(payloads):
        if getattr(payload, "role", None) != ROLE.TOOL_RESULT:
            continue
        for part in reversed(getattr(payload, "content", []) or []):
            if isinstance(part, ToolResult) and str(part.call_id) == str(call_id):
                return part
    return None


def _remove_failed_tool_calls(
    response: Any,
    failed_call_ids: set[str],
) -> tuple[int, bool] | None:
    """从最新工具回合移除失败 ToolCall，并返回其位置与保留状态。"""
    if not failed_call_ids:
        return None

    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list):
        return None

    for index in range(len(payloads) - 1, -1, -1):
        payload = payloads[index]
        if getattr(payload, "role", None) != ROLE.ASSISTANT:
            continue

        content = getattr(payload, "content", None)
        if not isinstance(content, list):
            continue

        has_failed_call = any(
            isinstance(part, ToolCall)
            and part.id is not None
            and str(part.id) in failed_call_ids
            for part in content
        )
        if not has_failed_call:
            continue

        cleaned_content = [
            part
            for part in content
            if not (
                isinstance(part, ToolCall)
                and part.id is not None
                and str(part.id) in failed_call_ids
            )
        ]
        if cleaned_content:
            payload.content = cleaned_content
            return index, True

        payloads.pop(index)
        return index, False

    return None


def _extract_args(raw_args: Any) -> dict[str, Any]:
    """提取工具参数字典，兼容字符串 JSON。"""
    if isinstance(raw_args, dict):
        return dict(raw_args)
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _build_fallback_call_id(index: int, name: str) -> str:
    """为缺失 id 的 tool call 生成跨响应唯一的兜底 id。"""
    safe_name = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in name)
    safe_name = safe_name or "tool"
    return f"NFC_call_{uuid.uuid4().hex[:12]}_{index}_{safe_name}"


def _ensure_standard_call(call: Any, index: int) -> ToolCall:
    """把任意 call-like 对象转换为框架标准 ToolCall，并补齐 call id。"""
    name = str(getattr(call, "name", "") or "")
    call_id = getattr(call, "id", None)
    if not call_id:
        call_id = _build_fallback_call_id(index, name)
    return ToolCall(
        id=str(call_id),
        name=name,
        args=_extract_args(getattr(call, "args", {})),
    )


def _standardize_calls(
    calls: list[Any],
    usable_map: ToolRegistry,
) -> list[ToolCall]:
    """执行前统一调用名、参数与重复 call_id。"""
    standardized: list[ToolCall] = []
    seen_ids: set[str] = set()
    for index, raw_call in enumerate(calls):
        call = _ensure_standard_call(raw_call, index)
        call_id = str(call.id)
        if call_id in seen_ids:
            call = ToolCall(
                id=_build_fallback_call_id(index, call.name),
                name=call.name,
                args=call.args,
            )
            call_id = str(call.id)
        seen_ids.add(call_id)
        standardized.append(
            _retarget_call_name(
                call,
                _resolve_registered_call_name(call.name, usable_map),
            )
        )
    return standardized


def _clean_reply_segments(raw_content: Any) -> list[str]:
    """使用回复 Action 相同规则得到实际可发送段落。"""
    segments = coerce_content_segments(raw_content)
    cleaned: list[str] = []
    for segment in segments:
        text, _stripped_thinking, _stripped_metadata = sanitize_segment(segment)
        if text:
            cleaned.append(text)
    return cleaned


def _is_result_dependent_call(call: ToolCall) -> bool:
    """判断工具结果是否应先回传给模型，再允许其做最终回复决策。"""
    name = str(call.name or "")
    normalized_name = _normalize_call_name(name)
    return (
        normalized_name in _RESULT_DEPENDENT_ACTIONS
        or name.startswith(("agent-", "tool-"))
        or ":agent:" in name
        or ":tool:" in name
    )


def _sync_assistant_tool_calls(response: Any, calls: list[ToolCall]) -> None:
    """同步最后一个 assistant payload 中的 ToolCall，避免 ToolResult 缺少/错配 call_id。"""
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list) or not calls:
        return

    assistant_payload = next(
        (
            payload
            for payload in reversed(payloads)
            if getattr(payload, "role", None) == ROLE.ASSISTANT
            and any(isinstance(part, ToolCall) for part in getattr(payload, "content", []))
        ),
        None,
    )
    if not isinstance(assistant_payload, LLMPayload):
        return

    call_iter = iter(calls)
    synced_content: list[Any] = []
    changed = False
    for part in getattr(assistant_payload, "content", []):
        if not isinstance(part, ToolCall):
            synced_content.append(part)
            continue
        replacement = next(call_iter, None)
        if replacement is None:
            synced_content.append(part)
            continue
        if part != replacement:
            changed = True
        synced_content.append(replacement)

    if changed:
        assistant_payload.content = synced_content


def extract_metadata(result: ToolCallResult, args: dict[str, Any]) -> None:
    """从工具调用参数中提取元数据到 ToolCallResult。"""
    if "thought" in args:
        result.thought = args["thought"]
    if "expected_reaction" in args:
        result.expected_reaction = args["expected_reaction"]
    if "max_wait_seconds" in args:
        result.max_wait_seconds = float(args["max_wait_seconds"])
    if "mood" in args:
        result.mood = args["mood"]


# 匹配 <unsent_perception_draft> 标签内的原始草稿文本
_PERCEPTION_DRAFT_RE = re.compile(
    r"<unsent_perception_draft>\s*"
    r"以下内容是你刚才形成的内部感知/未发送草稿，并没有发送给对方：\s*"
    r"(.+?)\s*"
    r"请把它视为内部草稿，而不是已经发出的消息。\s*"
    r"</unsent_perception_draft>",
    re.DOTALL,
)


def _extract_perception_draft(response: Any) -> str:
    """从 response 链中提取感知阶段的未发送草稿文本。

    当模型在感知阶段输出了纯文本后，系统会将其改写为
    <unsent_perception_draft> 格式存入 assistant payload。
    此函数逆向提取该草稿的原始文本，用于在 nfc_reply content
    为空时作为兜底回填。

    Returns:
        str: 提取到的草稿文本，未找到时返回空串。
    """
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list):
        return ""

    # 从后往前搜索最近的 assistant payload 中的草稿标记
    for payload in reversed(payloads):
        if getattr(payload, "role", None) != ROLE.ASSISTANT:
            continue
        content = getattr(payload, "content", None)
        if not content:
            continue
        for part in content:
            if not isinstance(part, Text):
                continue
            text = getattr(part, "text", "") or ""
            match = _PERCEPTION_DRAFT_RE.search(text)
            if match:
                draft = match.group(1).strip()
                if draft and draft != "（本轮仅完成内部感知，尚未形成可发送正文）":
                    return draft
    return ""


async def parse_tool_calls(
    response: Any,
    usable_map: ToolRegistry,
    trigger_msg: Any | None,
    config: NFCConfig,
    *,
    run_tool_call_fn: Callable[[Any, Any, ToolRegistry, Any | None], Awaitable[list[tuple[bool, bool]]]],
    pre_execute_hook: Callable[[ToolCallResult], None] | None = None,
) -> ToolCallResult:
    """遍历 LLM 返回的 call_list，提取元数据并执行动作。

    这里不再手写发送逻辑，而是直接把 call 委托给标准工具执行器。
    这样 reply / do_nothing / third-party tool 的回写链路都保持一致。
    """
    result = ToolCallResult()
    pending_third_party_calls: list[ToolCall] = []
    standardized_calls = _standardize_calls(coerce_call_list(response), usable_map)
    failed_call_ids: set[str] = set()
    response.call_list = standardized_calls
    _sync_assistant_tool_calls(response, standardized_calls)

    if any(_is_result_dependent_call(call) for call in standardized_calls):
        deferred_call_ids = {
            str(call.id)
            for call in standardized_calls
            if call.id is not None
            and _normalize_call_name(call.name) in {NFC_REPLY, DO_NOTHING}
        }
        if deferred_call_ids:
            standardized_calls = [
                call
                for call in standardized_calls
                if call.id is None or str(call.id) not in deferred_call_ids
            ]
            response.call_list = standardized_calls
            _remove_failed_tool_calls(response, deferred_call_ids)
            logger.warning(
                "[NFC] 查询型工具与最终决策同批出现，延后 reply/do_nothing，"
                "等待模型读取 tool_result 后重新决策"
            )

    async def flush_pending_third_party() -> None:
        """批量执行暂存的第三方工具。"""
        if not pending_third_party_calls:
            return

        logger.debug(f"[NFC] 标准批量执行 {len(pending_third_party_calls)} 个第三方工具")
        current_pending = list(pending_third_party_calls)
        pending_third_party_calls.clear()
        before_result_counts = _tool_result_call_counts(response)

        results = await run_tool_call_fn(current_pending, response, usable_map, trigger_msg)
        after_result_counts = _tool_result_call_counts(response)
        for call, call_result in zip(current_pending, results, strict=False):
            appended, success = call_result
            if call.id is not None:
                result.execution_success_by_call_id[str(call.id)] = bool(success)
            has_result = _has_new_tool_result(
                call.id,
                before_result_counts,
                after_result_counts,
            )
            if not has_result and (not appended or not success):
                if call.id is not None:
                    failed_call_ids.add(str(call.id))
                logger.warning(
                    f"[NFC] 工具 {call.name} 执行失败或被跳过"
                    "（可能原因：工具未注册、无触发消息或执行异常）"
                )

    async def execute_calls(
        calls: list[ToolCall],
    ) -> tuple[list[tuple[bool, bool]], dict[str, bool]]:
        """执行调用并标记每个 call_id 是否新增了 ToolResult。"""
        before_counts = _tool_result_call_counts(response)
        execution_results = await run_tool_call_fn(
            calls,
            response,
            usable_map,
            trigger_msg,
        )
        after_counts = _tool_result_call_counts(response)
        appended_by_call_id = {
            str(call.id): _has_new_tool_result(call.id, before_counts, after_counts)
            for call in calls
            if call.id is not None
        }
        return execution_results, appended_by_call_id

    # 先提取一次元数据，便于日志与决策层提前使用。
    if standardized_calls:
        for call in standardized_calls:
            args = _extract_args(getattr(call, "args", {}))
            normalized_name = _normalize_call_name(getattr(call, "name", ""))
            if normalized_name in (NFC_REPLY, DO_NOTHING):
                extract_metadata(result, args)
                break

    # 按原始顺序整理调用，遇到 reply / do_nothing 时仍由标准调度器执行。
    for index, call in enumerate(standardized_calls):
        args = dict(call.args) if isinstance(call.args, dict) else {}
        normalized_name = _normalize_call_name(call.name)
        reason = args.get("reason", "未提供原因")
        logger.info(f"LLM 调用 {call.name}，原因: {reason}")

        if normalized_name == NFC_REPLY:
            await flush_pending_third_party()
            result.has_reply = True
            extract_metadata(result, args)
            action_dict = {"type": normalized_name}
            action_dict.update({key: value for key, value in args.items() if key != "reason"})
            action_dict["content"] = _clean_reply_segments(action_dict.get("content"))

            # ── 兜底：感知阶段草稿回填 ──
            # 当模型在感知阶段已输出有效文本，但决策阶段调用 nfc_reply 时
            # content 为空（模型误以为感知文本已发送），从 response 链中
            # 提取草稿文本作为实际发送内容。
            raw_content = action_dict.get("content")
            content_is_empty = (
                raw_content is None
                or raw_content == []
                or (isinstance(raw_content, str) and not raw_content.strip())
            )
            if content_is_empty:
                draft_text = _extract_perception_draft(response)
                if draft_text:
                    # 使用 sub actor 从感知草稿中提取可发送内容
                    extracted = await extract_reply_from_perception(
                        draft_text,
                        model_task=config.general.perception_extract_task,
                    )
                    # 提取失败时回退到原始草稿（此处已有有效草稿，不跳过发送）
                    backfill_text = extracted if extracted else draft_text
                    logger.info(
                        f"[NFC] nfc_reply content 为空，回填感知阶段草稿"
                        f"{'(经 sub actor 提取)' if extracted else '(原始)'}: "
                        f"{backfill_text[:80]}{'...' if len(backfill_text) > 80 else ''}"
                    )
                    action_dict["content"] = [backfill_text]
                    # 同步更新 call.args 以确保实际执行时也使用回填内容
                    call = ToolCall(
                        id=call.id,
                        name=call.name,
                        args={**call.args, "content": [backfill_text]},
                    )
                    standardized_calls[index : index + 1] = [call]
            result.actions.append(action_dict)
            results, appended_by_call_id = await execute_calls([call])
            success = bool(results and results[0][1])
            if call.id is not None:
                result.execution_success_by_call_id[str(call.id)] = success
            execution_result = ExecutionResult.from_tool_result(
                getattr(_latest_tool_result(response, call.id), "value", None)
            )
            if execution_result is not None:
                action_dict["content"] = list(execution_result.sent_segments)
                result.reply_execution_failed = (
                    execution_result.failed
                    and execution_result.failure_kind == "send_failure"
                )
            elif not success:
                action_dict["content"] = []
                result.reply_execution_failed = True
            has_result = bool(
                call.id is not None and appended_by_call_id.get(str(call.id), False)
            )
            if results and not has_result and (not results[0][0] or not results[0][1]):
                if call.id is not None:
                    failed_call_ids.add(str(call.id))
            continue

        if normalized_name == DO_NOTHING:
            await flush_pending_third_party()
            result.has_do_nothing = True
            extract_metadata(result, args)
            action_dict = {"type": normalized_name}
            action_dict.update({key: value for key, value in args.items() if key != "reason"})
            result.actions.append(action_dict)
            before_result_counts = _tool_result_call_counts(response)
            results = await run_tool_call_fn([call], response, usable_map, trigger_msg)
            after_result_counts = _tool_result_call_counts(response)
            if call.id is not None:
                result.execution_success_by_call_id[str(call.id)] = bool(
                    results and results[0][1]
                )
            has_result = _has_new_tool_result(
                call.id,
                before_result_counts,
                after_result_counts,
            )
            if results and not has_result and (not results[0][0] or not results[0][1]):
                if call.id is not None:
                    failed_call_ids.add(str(call.id))
            continue

        result.has_third_party = True
        if _is_result_dependent_call(call):
            result.has_info_tool = True
        action_dict = {"type": normalized_name}
        action_dict.update({key: value for key, value in args.items() if key != "reason"})
        result.actions.append(action_dict)
        pending_third_party_calls.append(call)

    await flush_pending_third_party()
    if failed_call_ids:
        standardized_calls = [
            call
            for call in standardized_calls
            if call.id is None or str(call.id) not in failed_call_ids
        ]
        _remove_failed_tool_calls(response, failed_call_ids)

    try:
        response.call_list = standardized_calls
    except Exception:
        pass
    _sync_assistant_tool_calls(response, standardized_calls)

    if pre_execute_hook is not None:
        pre_execute_hook(result)

    if config.debug.show_prompt:
        call_names = [c.name for c in standardized_calls] if standardized_calls else []
        logger.debug(f"[NFC] LLM 响应: tool_calls={len(call_names)} {call_names}")

    return result
