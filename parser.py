"""KFC 工具调用解析器。

这一版保留原插件全部能力，但把实际执行统一收敛到 MoFox 标准
`BaseChatter.run_tool_call()` / `src.core.utils.llm_tool_call.run_tool_call()` 链路。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall

from .models import DO_NOTHING, KFC_REPLY, ToolCallResult

if TYPE_CHECKING:
    from src.kernel.llm import ToolRegistry

    from .config import KFCConfig

logger = get_logger("kfc_parser")


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


def _normalize_call_name(name: str) -> str:
    """归一化工具调用名称，兼容带前缀/带插件名前缀的格式。"""
    if not name:
        return ""

    if ":" in name:
        return name.rsplit(":", 1)[-1]

    for prefix in ("action-", "tool-", "agent-"):
        if name.startswith(prefix):
            return name[len(prefix) :]

    return name


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
    """为缺失 id 的 tool call 生成稳定的本轮兜底 id。"""
    safe_name = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in name)
    safe_name = safe_name or "tool"
    return f"kfc_call_{index}_{safe_name}"


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
    此函数逆向提取该草稿的原始文本，用于在 kfc_reply content
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
    config: KFCConfig,
    *,
    execute_reply_fn: Callable[[str, KFCConfig, Any | None, str], Awaitable[bool]],
    run_tool_call_fn: Callable[[Any, Any, ToolRegistry, Any | None], Awaitable[list[tuple[bool, bool]]]],
    pre_execute_hook: Callable[[ToolCallResult], None] | None = None,
) -> ToolCallResult:
    """遍历 LLM 返回的 call_list，提取元数据并执行动作。

    这里不再手写发送逻辑，而是直接把 call 委托给标准工具执行器。
    这样 reply / do_nothing / third-party tool 的回写链路都保持一致。
    """
    _ = execute_reply_fn  # 保留旧签名兼容性：实际执行已走标准 tool 调度链。

    result = ToolCallResult()
    pending_third_party_calls: list[ToolCall] = []
    standardized_calls: list[ToolCall] = []
    call_list = coerce_call_list(response)

    async def flush_pending_third_party() -> None:
        """批量执行暂存的第三方工具。"""
        if not pending_third_party_calls:
            return

        logger.debug(f"[KFC] 标准批量执行 {len(pending_third_party_calls)} 个第三方工具")
        current_pending = list(pending_third_party_calls)
        pending_third_party_calls.clear()

        results = await run_tool_call_fn(current_pending, response, usable_map, trigger_msg)
        for call, call_result in zip(current_pending, results, strict=False):
            appended, success = call_result
            if not appended or not success:
                logger.warning(
                    f"[KFC] 工具 {call.name} 执行失败或被跳过"
                    "（可能原因：工具未注册、无触发消息或执行异常）"
                )

    # 先提取一次元数据，便于日志与决策层提前使用。
    if call_list:
        for raw_call in call_list:
            args = _extract_args(getattr(raw_call, "args", {}))
            normalized_name = _normalize_call_name(getattr(raw_call, "name", ""))
            if normalized_name in (KFC_REPLY, DO_NOTHING):
                extract_metadata(result, args)
                break

    # 按原始顺序整理调用，遇到 reply / do_nothing 时仍由标准调度器执行。
    for index, raw_call in enumerate(call_list):
        call = _ensure_standard_call(raw_call, index)
        standardized_calls.append(call)
        args = dict(call.args) if isinstance(call.args, dict) else {}
        normalized_name = _normalize_call_name(call.name)
        reason = args.get("reason", "未提供原因")
        logger.info(f"LLM 调用 {call.name}，原因: {reason}")

        if normalized_name == KFC_REPLY:
            await flush_pending_third_party()
            result.has_reply = True
            extract_metadata(result, args)
            action_dict = {"type": normalized_name}
            action_dict.update({key: value for key, value in args.items() if key != "reason"})
            if isinstance(action_dict.get("content"), str):
                content = str(action_dict["content"]).strip()
                action_dict["content"] = [content] if content else []

            # ── 兜底：感知阶段草稿回填 ──
            # 当模型在感知阶段已输出有效文本，但决策阶段调用 kfc_reply 时
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
                    logger.info(
                        f"[KFC] kfc_reply content 为空，回填感知阶段草稿: "
                        f"{draft_text[:80]}{'...' if len(draft_text) > 80 else ''}"
                    )
                    action_dict["content"] = [draft_text]
                    # 同步更新 call.args 以确保实际执行时也使用回填内容
                    call = ToolCall(
                        id=call.id,
                        name=call.name,
                        args={**call.args, "content": [draft_text]},
                    )
                    standardized_calls[-1] = call

            result.actions.append(action_dict)
            await run_tool_call_fn([call], response, usable_map, trigger_msg)
            continue

        if normalized_name == DO_NOTHING:
            await flush_pending_third_party()
            result.has_do_nothing = True
            extract_metadata(result, args)
            action_dict = {"type": normalized_name}
            action_dict.update({key: value for key, value in args.items() if key != "reason"})
            result.actions.append(action_dict)
            await run_tool_call_fn([call], response, usable_map, trigger_msg)
            continue

        result.has_third_party = True
        if call.name.startswith(("agent-", "tool-")):
            result.has_info_tool = True
        action_dict = {"type": normalized_name}
        action_dict.update({key: value for key, value in args.items() if key != "reason"})
        result.actions.append(action_dict)
        pending_third_party_calls.append(call)

    try:
        response.call_list = standardized_calls
    except Exception:
        pass
    _sync_assistant_tool_calls(response, standardized_calls)

    await flush_pending_third_party()

    if pre_execute_hook is not None:
        pre_execute_hook(result)

    if config.debug.show_prompt:
        call_names = [c.name for c in standardized_calls] if standardized_calls else []
        logger.debug(f"[KFC] LLM 响应: tool_calls={len(call_names)} {call_names}")

    return result
