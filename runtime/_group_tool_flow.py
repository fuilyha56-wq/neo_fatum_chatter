"""NFC 群聊工具调用控制流模块。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.core.models.message import Message
from src.kernel.concurrency import get_watchdog
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolRegistry, ToolResult
from src.kernel.logger import Logger


@dataclass
class ToolCallOutcome:
    """一次 tool call 列表的控制流处理结果。"""

    should_wait: bool = False
    wait_seconds: float | None = None
    should_stop: bool = False
    stop_minutes: float = 0.0
    has_pending_tool_results: bool = False


async def process_tool_calls(
    *,
    stream_id: str,
    calls: list[ToolCall],
    response: object,
    run_tool_call: Callable[
        [list[ToolCall], object, ToolRegistry, Message | None],
        Awaitable[list[tuple[bool, bool]]],
    ],
    usable_map: ToolRegistry,
    trigger_msg: Message | None,
    pass_call_name: str,
    stop_call_name: str,
    cross_round_seen_signatures: set[str] | None = None,
) -> ToolCallOutcome:
    """处理单轮 LLM tool calls 并返回控制流结果。"""
    outcome = ToolCallOutcome()
    seen_call_signatures: set[str] = set()
    pending_calls: list[ToolCall] = []

    async def flush_pending_calls() -> None:
        """批量执行暂存的普通调用，并更新本轮控制流状态。"""
        if not pending_calls:
            return

        current_pending = list(pending_calls)
        pending_calls.clear()
        results = await run_tool_call(current_pending, response, usable_map, trigger_msg)

        for pending_call, (appended, success) in zip(current_pending, results, strict=False):
            _ = success
            if appended and not pending_call.name.startswith("action-"):
                outcome.has_pending_tool_results = True

    for call in calls:
        get_watchdog().feed_dog(stream_id)

        args = call.args if isinstance(call.args, dict) else {}
        dedupe_args = {key: value for key, value in args.items() if key != "reason"}
        dedupe_key = _build_call_dedupe_key(call.name, dedupe_args)
        if dedupe_key in seen_call_signatures:
            await flush_pending_calls()
            _append_tool_result(
                response,
                call_id=call.id,
                name=call.name,
                value="检测到同一轮重复工具调用，已自动跳过",
            )
            continue

        if cross_round_seen_signatures is not None and dedupe_key in cross_round_seen_signatures:
            await flush_pending_calls()
            _append_tool_result(
                response,
                call_id=call.id,
                name=call.name,
                value="检测到跨轮重复工具调用，已自动跳过",
            )
            continue

        seen_call_signatures.add(dedupe_key)
        if cross_round_seen_signatures is not None:
            cross_round_seen_signatures.add(dedupe_key)

        if call.name == pass_call_name:
            await flush_pending_calls()
            wait_seconds = args.get("seconds")
            outcome.wait_seconds = None if wait_seconds is None else float(wait_seconds)
            wait_text = (
                "已登记等待，本轮动作完成后等待用户新消息"
                if outcome.wait_seconds is None
                else f"已登记等待，本轮动作完成后等待 {outcome.wait_seconds} 秒后继续对话"
            )
            _append_tool_result(response, call_id=call.id, name=call.name, value=wait_text)
            outcome.should_wait = True
            continue

        if call.name == stop_call_name:
            await flush_pending_calls()
            outcome.stop_minutes = float(args.get("minutes", 5.0))
            _append_tool_result(
                response,
                call_id=call.id,
                name=call.name,
                value=f"对话已结束，将在 {outcome.stop_minutes} 分钟后允许新对话",
            )
            outcome.should_stop = True
            continue

        pending_calls.append(call)

    await flush_pending_calls()
    return outcome


def _append_tool_result(response: object, *, call_id: str, name: str, value: str) -> None:
    """向 response 追加 TOOL_RESULT payload。"""
    response.add_payload(  # type: ignore[attr-defined]
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value=value, call_id=call_id, name=name),  # type: ignore[arg-type]
        )
    )


def _build_call_dedupe_key(call_name: str, args: object) -> str:
    """构建 tool call 去重键。"""
    try:
        serialized_args = json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except TypeError:
        serialized_args = str(args)
    return f"{call_name}:{serialized_args}"


def append_suspend_payload_if_action_only(
    *,
    calls: list[ToolCall],
    response: object,
    suspend_text: str,
    enable_action_suspend: bool,
    logger: Logger,
) -> None:
    """当本轮全是 action 调用时，补充 SUSPEND 占位 assistant 消息。"""
    if enable_action_suspend and calls and all(call.name.startswith("action-") for call in calls):
        response.add_payload(LLMPayload(ROLE.ASSISTANT, Text(suspend_text)))  # type: ignore[attr-defined]
        logger.debug("已注入 SUSPEND 占位符（本轮全部为 action 调用）")
