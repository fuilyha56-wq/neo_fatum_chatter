"""NFC 群聊状态机（DFC 模式）。

群聊路径与私聊心理活动流完全解耦：
- 不经过 _send_with_perceive_loop（无感知重试）
- 不经过 mental_log / scene_state / chain_payloads
- 不经过 interrupt_controller（群聊不打断）
- 固定使用 config.general.model_task
- 纯文本 fallback → 直接 Stop（DFC 群聊行为）

四阶段状态机：WAIT_USER → MODEL_TURN → TOOL_EXEC → FOLLOW_UP
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.llm_api import get_model_set_by_task
from src.app.plugin_system.base import Failure, Stop, Wait
from src.core.components.base import WaitResumeEvent
from src.core.models.message import Message
from src.core.models.stream import ChatStream
from src.kernel.llm import LLMPayload, ROLE, Text
from src.kernel.llm.context_structure import validate_payload_sequence
from src.kernel.llm.context_budget import (
    build_qa_groups,
    flatten_groups,
    split_pinned_prefix,
)

from ._group_tool_flow import (
    ToolCallOutcome,
    append_suspend_payload_if_action_only,
    process_tool_calls,
)
from .group_gate import should_respond_in_group

logger = get_logger("NFC_group_orchestrator")

_SUSPEND_TEXT = "__SUSPEND__"
_PASS_CALL_NAME = "action-nfc_pass_and_wait"
_STOP_CALL_NAME = "action-nfc_stop_conversation"


class _Phase(str, Enum):
    """群聊状态机阶段。"""

    WAIT_USER = "wait_user"
    MODEL_TURN = "model_turn"
    TOOL_EXEC = "tool_exec"
    FOLLOW_UP = "follow_up"


@dataclass(slots=True)
class _GroupState:
    """群聊状态机运行时状态。"""

    response: Any  # LLMRequest | LLMResponse
    phase: _Phase
    history_merged: bool
    unreads: list[Message]
    cross_round_seen_signatures: set[str]
    unread_msgs_to_flush: list[Message]
    used_tools_in_round: set[str] = field(default_factory=set)


def _transition(state: _GroupState, to_phase: _Phase, reason: str) -> None:
    """状态机转换（日志追踪）。"""
    if state.phase == to_phase:
        return
    logger.debug(f"[FSM] {state.phase.value} -> {to_phase.value}: {reason}")
    state.phase = to_phase


def _is_timer_resume(event: WaitResumeEvent | None) -> bool:
    """判断是否为定时器恢复事件。"""
    return event is not None and event.source == "timer"


def _is_suspend_message(message: str | None) -> bool:
    """判断消息内容是否为 SUSPEND 占位。"""
    return isinstance(message, str) and message.strip() == _SUSPEND_TEXT


def _has_tool_result_tail(response: Any) -> bool:
    """检查 response 的 payloads 末尾是否为 TOOL_RESULT。"""
    payloads = getattr(response, "payloads", None)
    return bool(payloads and payloads[-1].role == ROLE.TOOL_RESULT)


def _trim_response_payloads(response: Any, max_groups: int) -> None:
    """按 user 锚点分组，仅保留最近 max_groups 个 QA 组，避免群聊上下文无限膨胀。

    pinned 前缀（system / tool）始终保留；可裁剪尾部按 user 起点划分为若干分组，
    与 kernel 的 trim_payloads_by_tokens 同源策略，确保 tool_call 和后续
    tool_result 留在同一组内不会被切断。max_groups <= 0 时不裁剪。
    """
    if max_groups <= 0:
        return
    payloads = getattr(response, "payloads", None)
    if not payloads:
        return
    pinned, tail = split_pinned_prefix(payloads)
    groups = build_qa_groups(tail)
    if len(groups) <= max_groups:
        return
    kept = groups[-max_groups:]
    new_payloads = pinned + flatten_groups(kept)
    response.payloads = new_payloads


def _consume_step_data(state: _GroupState) -> dict[str, Any]:
    """消费并返回当前轮次的 step_data。"""
    used_tools = sorted(state.used_tools_in_round)
    state.used_tools_in_round.clear()
    return {"step_scope": "actor_round", "used_tools": used_tools}


def _should_disable_stream_for_tool_call_compat(response: Any) -> bool:
    """检查是否因 tool_call_compat 需要禁用流式。"""
    model_set = getattr(response, "model_set", None)
    if not isinstance(model_set, list):
        return False
    return any(bool(model.get("tool_call_compat", False)) for model in model_set)


def _apply_stop_wake_config(result: Stop, config: Any) -> Stop:
    """应用 stop 直唤配置。"""
    return Stop(
        time=result.time,
        direct_message_wake_enabled=config.group.enable_stop_direct_message_wake,
        direct_message_wake_probability=max(
            0.0, min(1.0, float(config.group.stop_direct_message_wake_probability))
        ),
        step_data=result.step_data,
    )


def _format_tool_args(args: Any) -> str:
    """格式化工具调用参数用于日志显示。"""
    if not isinstance(args, dict):
        return ""
    display_items: list[str] = []
    for key, value in args.items():
        if key == "reason":
            continue
        display_items.append(f"{key}: {value}")
    return ", ".join(display_items)


def _build_actor_decision_panel(chat_stream: ChatStream, response: Any) -> str:
    """构建决策面板文本。"""
    stream_name = chat_stream.stream_name or chat_stream.stream_id or "未知聊天流"
    thought = (response.reasoning_content or "").strip() or "（无）"
    monologue = (response.message or "").strip() or "（无）"

    tool_lines = []
    for call in response.call_list or []:
        formatted_args = _format_tool_args(call.args)
        if formatted_args:
            tool_lines.append(f"    {call.name} ({formatted_args})")
        else:
            tool_lines.append(f"    {call.name}")
    tools_text = "\n".join(tool_lines) if tool_lines else "    （无）"

    return (
        f"聊天流名称：{stream_name}\n\n"
        f"思考：{thought}\n\n"
        f"独白：{monologue}\n\n"
        f"调用工具：\n{tools_text}"
    )


def _pick_trigger_message(
    chat_stream: ChatStream,
    state: _GroupState,
) -> Message:
    """选择触发消息供 Action 使用。

    当所有真实消息源均为空时，构造一个虚拟 trigger message 以确保
    FOLLOW_UP / timer resume 等场景下工具调用不会因 trigger_msg=None 被跳过。
    """
    if state.unreads:
        return state.unreads[-1]

    context = chat_stream.context
    if context.current_message is not None:
        return context.current_message
    if context.unread_messages:
        return context.unread_messages[-1]
    if context.history_messages:
        return context.history_messages[-1]

    from uuid import uuid4
    from src.core.models.message import MessageType

    return Message(
        message_id=f"nfc_group_virtual_{uuid4().hex}",
        content="",
        processed_plain_text="",
        message_type=MessageType.TEXT,
        sender_id="",
        sender_name="",
        platform=chat_stream.platform,
        chat_type=chat_stream.chat_type,
        stream_id=chat_stream.stream_id,
    )


async def execute_group_orchestrator(
    chatter: Any,
) -> AsyncGenerator[Wait | Stop | Failure, Any]:
    """NFC 群聊状态机入口。

    作为异步生成器运行，由 chatter.execute() 驱动。

    Args:
        chatter: NeoFatumChatter 实例（需具备 stream_id, _get_config,
            create_request, inject_usables, format_message_line,
            fetch_unreads, flush_unreads, run_tool_call 等方法）
    """
    from src.core.managers.stream_manager import get_stream_manager

    # ── 1. 激活 stream ──
    chat_stream = await get_stream_manager().activate_stream(chatter.stream_id)
    if chat_stream is None:
        logger.error(f"无法激活聊天流: {chatter.stream_id}")
        yield Failure("无法激活聊天流")
        return

    config = chatter._get_config()
    if not config.group.enabled:
        yield Stop(0)
        return

    # ── 1.5. 原生多模态：跳过 VLM ──
    if config.group.native_multimodal:
        from src.core.managers.media_manager import get_media_manager

        get_media_manager().skip_vlm_for_stream(chat_stream.stream_id, ["image"])
        logger.debug(
            f"Skipped VLM image recognition for stream {chat_stream.stream_id[:8]}"
        )

    # ── 2. 获取模型配置 ──
    try:
        model_set = get_model_set_by_task(config.general.model_task)
    except (ValueError, KeyError) as error:
        logger.error(f"群聊模型配置错误: {error}")
        yield Failure(f"群聊模型配置错误: {error}")
        return

    # ── 3. 构建 LLM Request ──
    try:
        request = chatter.create_request(
            config.general.model_task,
            request_name="nfc_group",
            with_reminder="actor",
        )
    except (ValueError, KeyError) as error:
        logger.error(f"群聊创建 LLM request 失败: {error}")
        yield Failure(f"群聊创建 LLM request 失败: {error}")
        return

    # 注入系统提示词
    from ..prompts.group_builder import build_group_system_prompt

    system_prompt_text = await build_group_system_prompt(
        chat_stream,
        config,
        enable_action_suspend=config.group.enable_action_suspend,
    )
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt_text)))

    # 构建历史文本
    from ..prompts.group_builder import build_group_user_prompt, build_group_history_text

    history_text = build_group_history_text(chat_stream, chatter.format_message_line)

    # 注册工具
    usable_map = await chatter.inject_usables(request)

    # ── 4. 初始化状态机 ──
    state = _GroupState(
        response=request,
        phase=_Phase.WAIT_USER,
        history_merged=False,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
    )

    resume_event: WaitResumeEvent | None = None

    # ── 5. 状态机主循环 ──
    while True:
        current_resume_event = resume_event
        resume_event = None

        # 获取未读消息
        _, unread_msgs = await chatter.fetch_unreads()

        # ── 3d. has_tool_result_tail 边缘转换 ──
        if (
            state.phase == _Phase.WAIT_USER
            and _has_tool_result_tail(state.response)
            and not unread_msgs
        ):
            _transition(state, _Phase.FOLLOW_UP, "context tail is TOOL_RESULT and no unread")

        # ══════ WAIT_USER 阶段 ══════
        if state.phase == _Phase.WAIT_USER:
            # timer resume：超时后重新进入 MODEL_TURN
            if _is_timer_resume(current_resume_event):
                assert current_resume_event is not None
                state.cross_round_seen_signatures.clear()
                state.used_tools_in_round.clear()
                state.unreads = []
                state.unread_msgs_to_flush = []
                timeout_text = (
                    "系统事件：你之前设置的等待时间已经结束。当前没有新的用户消息。"
                    "请基于已有上下文主动决定下一步。"
                    "如果现在不应继续，请再次调用 nfc_pass_and_wait；"
                    "如果需要回复或执行动作，请直接使用相应工具。"
                )
                state.response.add_payload(LLMPayload(ROLE.USER, Text(timeout_text)))
                _transition(state, _Phase.MODEL_TURN, "等待计时器到期")
                continue

            if not unread_msgs:
                resume_event = yield Wait(step_data=_consume_step_data(state))
                continue

            # 有未读消息 → 门控判断
            state.cross_round_seen_signatures.clear()
            state.used_tools_in_round.clear()
            state.unreads = unread_msgs

            unread_lines = "\n".join(
                chatter.format_message_line(msg) for msg in unread_msgs
            )

            should_respond = await should_respond_in_group(
                chatter=chatter,
                chat_stream=chat_stream,
                config=config,
                unread_msgs=unread_msgs,
                unreads_text=unread_lines,
            )

            if not should_respond:
                logger.info("群聊门控: 决定不响应，继续等待")
                resume_event = yield Wait(step_data=_consume_step_data(state))
                continue

            # ── 3h. 负面行为强化 extra ──
            extra = ""
            if config.group.reinforce_negative_behaviors:
                from src.core.config import get_core_config

                negative_behaviors = get_core_config().personality.negative_behaviors
                if negative_behaviors:
                    lines = "\n".join(negative_behaviors)
                    extra = f"行为提醒：请在本轮回复中严格遵守以下约束：\n{lines}"

            # 构建 user prompt 并注入
            unread_user_prompt = await build_group_user_prompt(
                chat_stream,
                history_text=history_text if not state.history_merged else "",
                unread_lines=unread_lines,
                extra=extra,
            )
            state.history_merged = True

            # ── 3e. 原生多模态 payload 注入 ──
            if config.group.native_multimodal and unread_msgs:
                from ..multimodal import (
                    build_multimodal_content,
                    extract_media_from_messages,
                )

                images = extract_media_from_messages(unread_msgs)
                content_list = build_multimodal_content(unread_user_prompt, images)
            else:
                content_list = [Text(unread_user_prompt)]

            state.response.add_payload(LLMPayload(ROLE.USER, content_list))
            state.unread_msgs_to_flush = unread_msgs
            _transition(state, _Phase.MODEL_TURN, "接受未读批次")
            continue

        # ══════ FOLLOW_UP 阶段：工具返回后检查是否有新未读 ══════
        if state.phase == _Phase.FOLLOW_UP:
            if unread_msgs:
                # 有新消息到达 → 走门控
                unread_lines = "\n".join(
                    chatter.format_message_line(msg) for msg in unread_msgs
                )
                should_respond = await should_respond_in_group(
                    chatter=chatter,
                    chat_stream=chat_stream,
                    config=config,
                    unread_msgs=unread_msgs,
                    unreads_text=unread_lines,
                )
                if should_respond:
                    # ── 3h. 负面行为强化 extra ──
                    extra = ""
                    if config.group.reinforce_negative_behaviors:
                        from src.core.config import get_core_config

                        negative_behaviors = get_core_config().personality.negative_behaviors
                        if negative_behaviors:
                            lines = "\n".join(negative_behaviors)
                            extra = f"行为提醒：请在本轮回复中严格遵守以下约束：\n{lines}"

                    unread_user_prompt = await build_group_user_prompt(
                        chat_stream,
                        history_text="",
                        unread_lines=unread_lines,
                        extra=extra,
                    )
                    state.unreads = unread_msgs

                    # ── 3e. 原生多模态 payload 注入 ──
                    if config.group.native_multimodal and unread_msgs:
                        from ..multimodal import (
                            build_multimodal_content,
                            extract_media_from_messages,
                        )

                        images = extract_media_from_messages(unread_msgs)
                        content_list = build_multimodal_content(unread_user_prompt, images)
                    else:
                        content_list = [Text(unread_user_prompt)]

                    state.response.add_payload(LLMPayload(ROLE.USER, content_list))
                    state.unread_msgs_to_flush = unread_msgs
                    _transition(state, _Phase.MODEL_TURN, "跟进阶段接受新未读")
                    continue

            # 无新消息或不响应 → 继续进入 MODEL_TURN 让 LLM 处理 tool result
            _transition(state, _Phase.MODEL_TURN, "跟进阶段继续 LLM 推理")

        # ══════ MODEL_TURN 阶段 ══════
        if state.phase == _Phase.MODEL_TURN:
            try:
                # ── 上下文裁剪：避免群聊跨轮 payload 累积导致连接异常 ──
                _trim_response_payloads(state.response, config.group.max_context_groups)

                # ── 3g. LLM 流式响应 ──
                use_stream = bool(config.group.enable_llm_stream)

                # ── 3j. tool_call_compat stream 禁用 ──
                if use_stream and _should_disable_stream_for_tool_call_compat(state.response):
                    use_stream = False
                    logger.warning(
                        "LLM stream disabled because tool_call_compat is enabled."
                    )

                state.response = await state.response.send(stream=use_stream)
                if use_stream:
                    await state.response.stream_events_with_callback(lambda e: None)
                else:
                    await state.response

                # 发送成功后 flush 未读
                if state.unread_msgs_to_flush:
                    await chatter.flush_unreads(state.unread_msgs_to_flush)
                    state.unread_msgs_to_flush = []
            except Exception as error:
                logger.error(f"群聊 LLM 请求失败: {error}", exc_info=True)
                yield Failure("群聊 LLM 请求失败", error)
                _transition(state, _Phase.WAIT_USER, "请求失败")
                continue

            _transition(state, _Phase.TOOL_EXEC, "模型已响应")
            continue

        # ══════ TOOL_EXEC 阶段 ══════
        if state.phase == _Phase.TOOL_EXEC:
            llm_response = state.response
            current_calls = llm_response.call_list or []
            state.used_tools_in_round.update(
                str(getattr(call, "name", "") or "").strip()
                for call in current_calls
                if str(getattr(call, "name", "") or "").strip()
            )

            # 打印决策面板
            if current_calls:
                print_panel = getattr(logger, "print_panel", None)
                if callable(print_panel):
                    print_panel(
                        _build_actor_decision_panel(chat_stream, llm_response),
                        title="NFC 群聊决策",
                        border_style="green",
                    )

            # ── 3f. 纯文本 fallback retry 机制 ──
            if not current_calls:
                if _is_suspend_message(llm_response.message):
                    # SUSPEND → 等待
                    resume_event = yield Wait(step_data=_consume_step_data(state))
                    _transition(state, _Phase.WAIT_USER, "模型返回挂起")
                    continue

                if llm_response.message and llm_response.message.strip():
                    # 纯文本 → 群聊直接 Stop（DFC 行为，群聊无 plain_text_adapter）
                    logger.warning(
                        f"LLM 返回纯文本: {llm_response.message[:100]}"
                    )
                    stop_result = Stop(0, step_data=_consume_step_data(state))
                    yield _apply_stop_wake_config(stop_result, config)
                    return

                # 空响应 → 等待
                resume_event = yield Wait(step_data=_consume_step_data(state))
                _transition(state, _Phase.WAIT_USER, "没有调用列表")
                continue

            # 有工具调用 → 处理
            logger.info(f"群聊工具调用: {[call.name for call in current_calls]}")
            for call in current_calls:
                args = dict(call.args) if isinstance(call.args, dict) else {}
                reason = args.pop("reason", "未提供原因")
                logger.info(f"  {call.name}; 原因={reason}; 参数={args}")

            call_outcome = await process_tool_calls(
                stream_id=chat_stream.stream_id,
                calls=current_calls,
                response=llm_response,
                run_tool_call=chatter.run_tool_call,
                usable_map=usable_map,
                trigger_msg=_pick_trigger_message(chat_stream, state),
                pass_call_name=_PASS_CALL_NAME,
                stop_call_name=_STOP_CALL_NAME,
                cross_round_seen_signatures=state.cross_round_seen_signatures,
            )

            # ── 3a. enable_cooldown ──
            if call_outcome.should_stop:
                cooldown_seconds = call_outcome.stop_minutes * 60 if config.group.enable_cooldown else 0
                stop_result = Stop(
                    cooldown_seconds,
                    step_data=_consume_step_data(state),
                )
                yield _apply_stop_wake_config(stop_result, config)
                return

            # 有待处理的 tool result → 跟进
            if call_outcome.has_pending_tool_results:
                _transition(state, _Phase.FOLLOW_UP, "待处理的工具结果")
                continue

            # ── 3b. enable_action_suspend 可关闭 ──
            action_only_round = bool(current_calls) and all(
                call.name.startswith("action-") for call in current_calls
            )
            append_suspend_payload_if_action_only(
                calls=current_calls,
                response=llm_response,
                suspend_text=_SUSPEND_TEXT,
                enable_action_suspend=config.group.enable_action_suspend,
                logger=logger,
            )

            if action_only_round and not call_outcome.should_wait:
                if config.group.enable_action_suspend:
                    resume_event = yield Wait(step_data=_consume_step_data(state))
                    _transition(state, _Phase.WAIT_USER, "仅动作回合挂起")
                    continue
                else:
                    # enable_action_suspend 关闭 → 进入 FOLLOW_UP 继续推理
                    _transition(state, _Phase.FOLLOW_UP, "仅动作回合继续跟进")
                    continue

            # pass_and_wait
            if call_outcome.should_wait:
                # 确保工具结果尾部有 SUSPEND 闭合
                payloads = getattr(llm_response, "payloads", None) or []
                if payloads and payloads[-1].role == ROLE.TOOL_RESULT:
                    llm_response.add_payload(LLMPayload(ROLE.ASSISTANT, Text(_SUSPEND_TEXT)))
                    logger.debug("注入 SUSPEND 占位符以在等待之前关闭工具结果尾部")

                resume_event = yield Wait(
                    time=getattr(call_outcome, "wait_seconds", None),
                    step_data=_consume_step_data(state),
                )
            else:
                _consume_step_data(state)
            _transition(state, _Phase.WAIT_USER, "工具执行完成")
            continue
