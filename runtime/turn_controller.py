"""NFC 回合提交控制。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import Stop, Wait
from src.kernel.llm import LLMPayload, ROLE, Text

from ..domain.turn_trigger import TurnTrigger, classify_turn_trigger
from ..models import WaitingConfig
from ..services import SummaryService
from ..services.context_sanitizer import close_pending_tool_chain, prepare_payload_chain_for_send
from .message_buffer import dedupe_messages_by_id

if TYPE_CHECKING:
    from ..config import NFCConfig
    from ..domain.decision import Decision
    from ..prompts.builder import NFCPromptBuilder
    from ..session import NFCSession
    from ..chatter import NeoFatumChatter
    from src.core.models.stream import ChatStream
    from ..services.timeout_service import TimeoutService


logger = get_logger("NFC_chatter")


def _wait_until_session_timeout(session: Any) -> Wait:
    """按当前等待配置生成框架等待信号。"""
    waiting_config = getattr(session, "waiting_config", None)
    max_wait_seconds = float(getattr(waiting_config, "max_wait_seconds", 0.0) or 0.0)
    started_at = getattr(waiting_config, "started_at", None)
    if isinstance(started_at, (int, float)) and max_wait_seconds > 0:
        return Wait(max(0.0, started_at + max_wait_seconds - time.time()))
    if max_wait_seconds > 0:
        return Wait(max_wait_seconds)
    return Wait()


@dataclass(slots=True)
class TurnControlResult:
    """一轮决策提交后的控制结果。"""

    next_signal: Wait | Stop | None = None
    continue_loop: bool = False
    return_after_yield: bool = False
    has_pending_tool_results: bool = False
    is_final_timeout: bool = False


@dataclass(slots=True)
class TurnInputResult:
    """一轮主循环在发起 LLM 请求前的准备结果。"""

    response: Any
    unread_msgs: list[Any]
    extra_payload: LLMPayload | None = None
    next_signal: Wait | None = None
    continue_loop: bool = False
    history_images_injected: bool = False
    has_pending_tool_results: bool = False
    is_final_timeout: bool = False
    is_timeout_turn: bool = False


async def prepare_turn_input(
    chatter: NeoFatumChatter,
    response: Any,
    chat_stream: ChatStream,
    config: NFCConfig,
    session: NFCSession,
    prompt_builder: NFCPromptBuilder,
    timeout_service: TimeoutService,
    image_budget: Any,
    has_history: bool,
    history_images_injected: bool,
    has_pending_tool_results: bool,
) -> TurnInputResult:
    """准备一轮 LLM 调用前的触发输入。"""
    formatted_text, unread_msgs = await chatter.fetch_unreads(
        time_format="%Y-%m-%d %H:%M:%S"
    )
    extra_payload: LLMPayload | None = None
    is_final_timeout = False
    is_timeout_turn = False

    # 使用显式枚举分类触发类型
    trigger = classify_turn_trigger(
        has_unread=bool(formatted_text and unread_msgs),
        has_pending_tool_results=has_pending_tool_results,
        session=session,
        is_timeout=timeout_service.check_timeout(session) if session.is_waiting() else False,
    )

    if trigger == TurnTrigger.NEW_MESSAGES:
        # 等待期间抑制提前唤醒：收到新消息但 session 仍在 waiting 且未超时时，
        # 不立即处理，把消息显式收集到 session.suppressed_messages，
        # 等待超时后由下方"超时合并"分支一次性合并为单条 USER payload。
        if (
            config.wait.suppress_early_wake
            and session.is_waiting()
            and not session.waiting_config.is_timeout()
        ):
            _collect_suppressed_messages(session, unread_msgs)
            # 用绝对截止时间计算 remaining，避免每条新消息都重新计时把超时点往后推
            started_at = getattr(session.waiting_config, "started_at", None)
            max_wait = float(getattr(session.waiting_config, "max_wait_seconds", 0.0) or 0.0)
            if isinstance(started_at, (int, float)) and max_wait > 0:
                remaining = max(0.0, started_at + max_wait - time.time())
            else:
                remaining = max(
                    0.0,
                    max_wait - session.waiting_config.get_elapsed_seconds(),
                )
            logger.debug(
                f"[等待抑制] 收集 {len(unread_msgs)} 条新消息到缓冲区"
                f"（累计 {len(session.suppressed_messages)} 条），剩余 {remaining:.1f}s"
            )
            return TurnInputResult(
                response=response,
                unread_msgs=[],
                next_signal=Wait(remaining) if remaining > 0 else None,
                continue_loop=True if remaining > 0 else False,
                history_images_injected=history_images_injected,
                has_pending_tool_results=has_pending_tool_results,
                is_final_timeout=is_final_timeout,
            )

        # 超时已到 + 抑制期有缓冲消息：一次性合并消费，避免对每条缓冲消息
        # 单独构建上下文。注意：此分支也会把当前 fetch_unreads 拿到的最新
        # 未读消息一并纳入合并（它们尚未进入 suppressed_messages）。
        if (
            config.wait.suppress_early_wake
            and session.suppressed_messages
        ):
            _collect_suppressed_messages(session, unread_msgs)
            buffered = _drain_suppressed_messages(session)
            return await _build_suppressed_batch_turn(
                chatter=chatter,
                response=response,
                chat_stream=chat_stream,
                config=config,
                session=session,
                prompt_builder=prompt_builder,
                image_budget=image_budget,
                has_history=has_history,
                history_images_injected=history_images_injected,
                buffered_msgs=buffered,
            )

        formatted_text, unread_msgs = await chatter._accumulate_messages(config)
        unread_msgs = filter_messages_already_in_payloads(response, dedupe_messages_by_id(unread_msgs))
        if not unread_msgs:
            return TurnInputResult(
                response=response,
                unread_msgs=[],
                next_signal=Wait(),
                continue_loop=True,
                history_images_injected=history_images_injected,
                has_pending_tool_results=has_pending_tool_results,
                is_final_timeout=is_final_timeout,
            )
        formatted_text = "\n".join(
            chatter.format_message_line(message, time_format="%Y-%m-%d %H:%M:%S")
            for message in unread_msgs
        )
        has_pending_tool_results = False
        for msg in unread_msgs:
            sender_id = getattr(msg, "sender_id", "")
            session.add_user_message(
                content=getattr(msg, "processed_plain_text", "")
                or str(getattr(msg, "content", "")),
                user_name=getattr(msg, "sender_name", "用户"),
                user_id=sender_id,
                timestamp=chatter._extract_timestamp(msg),
                message_id=getattr(msg, "message_id", ""),
            )
            if sender_id:
                session.user_id = sender_id
            if chat_stream.platform:
                session.platform = chat_stream.platform

        if session.is_waiting():
            chatter._record_reply_timing(session)
            session.clear_waiting()

        media_items = chatter._extract_media(
            unread_msgs,
            config,
            image_budget,
        )

        if (
            not history_images_injected
            and has_history
            and image_budget is not None
            and not image_budget.is_exhausted()
        ):
            history_images_injected = True
            history_imgs = chatter._extract_history_media(
                chat_stream,
                image_budget,
            )
            from ..services import MultimodalService

            MultimodalService.append_history_reference(response, history_imgs)

        user_payload, extra_payload = await prompt_builder.build_user_payload(
            formatted_unreads=formatted_text,
            media_items=media_items,
            stream_id=chatter.stream_id,
            config=config,
        )

        close_pending_tool_chain(response, reason="新消息到达")

        upserted = False
        if (
            not media_items
            and response.payloads
            and response.payloads[-1].role == ROLE.USER
        ):
            last_payload = response.payloads[-1]
            if last_payload.content and isinstance(last_payload.content[-1], Text):
                existing = last_payload.content[-1].text  # type: ignore[attr-defined]
                last_payload.content[-1] = Text(
                    f"{existing}\n{user_payload.content[-1].text}"  # type: ignore[attr-defined]
                    if isinstance(user_payload.content, list)
                    else f"{existing}\n{user_payload.content.text}"  # type: ignore[attr-defined]
                )
                upserted = True
                logger.debug("[NFC] Upsert USER payload（打断重来合并新消息）")
        if not upserted:
            response.add_payload(user_payload)
    elif trigger == TurnTrigger.FOLLOWUP_TOOL_RESULT:
        has_pending_tool_results = False
    elif trigger == TurnTrigger.TIMEOUT_EXPIRED:
        # 优先消费抑制期显式缓冲的消息：若有，合并为单条 USER payload，
        # 走与 NEW_MESSAGES 相同的注入路径，避免对每条缓冲消息单独构建上下文。
        buffered = _drain_suppressed_messages(session)
        if buffered:
            return await _build_suppressed_batch_turn(
                chatter=chatter,
                response=response,
                chat_stream=chat_stream,
                config=config,
                session=session,
                prompt_builder=prompt_builder,
                image_budget=image_budget,
                has_history=has_history,
                history_images_injected=history_images_injected,
                buffered_msgs=buffered,
            )

        timeout_result = timeout_service.build_timeout_result(
            response,
            session,
        )
        is_final_timeout = timeout_result.is_final_timeout
        is_timeout_turn = True
        timeout_upserted = False
        if response.payloads and response.payloads[-1].role == ROLE.USER:
            last_payload = response.payloads[-1]
            timeout_text = (
                timeout_result.payload.content.text  # type: ignore[attr-defined]
                if isinstance(timeout_result.payload.content, Text)
                else ""
            )
            if timeout_text and last_payload.content and isinstance(last_payload.content[-1], Text):
                last_payload.content[-1] = Text(
                    f"{last_payload.content[-1].text}\n{timeout_text}"  # type: ignore[attr-defined]
                )
                timeout_upserted = True
        if not timeout_upserted:
            response.add_payload(timeout_result.payload)
    else:  # TurnTrigger.IDLE_WAIT
        if session.is_waiting():
            return TurnInputResult(
                response=response,
                unread_msgs=[],
                next_signal=_wait_until_session_timeout(session),
                continue_loop=True,
                history_images_injected=history_images_injected,
                has_pending_tool_results=has_pending_tool_results,
                is_final_timeout=is_final_timeout,
            )
        return TurnInputResult(
            response=response,
            unread_msgs=[],
            next_signal=Wait(),
            continue_loop=True,
            history_images_injected=history_images_injected,
            has_pending_tool_results=has_pending_tool_results,
            is_final_timeout=is_final_timeout,
        )

    prepare_payload_chain_for_send(response, reason="回合输入准备完成")

    return TurnInputResult(
        response=response,
        unread_msgs=unread_msgs,
        extra_payload=extra_payload,
        history_images_injected=history_images_injected,
        has_pending_tool_results=has_pending_tool_results,
        is_final_timeout=is_final_timeout,
        is_timeout_turn=is_timeout_turn,
    )


def _collect_suppressed_messages(session: NFCSession, messages: list[Any]) -> None:
    """把抑制期间到达的新消息追加到 session.suppressed_messages（按 message_id 去重）。"""
    if not messages:
        return
    existing_ids = {
        str(getattr(m, "message_id", "") or "")
        for m in session.suppressed_messages
        if getattr(m, "message_id", None) is not None
    }
    for msg in messages:
        mid = getattr(msg, "message_id", None)
        if mid is not None and str(mid) in existing_ids:
            continue
        session.suppressed_messages.append(msg)
        if mid is not None:
            existing_ids.add(str(mid))


def _drain_suppressed_messages(session: NFCSession) -> list[Any]:
    """取出并清空 session.suppressed_messages，返回去重后的消息列表。"""
    if not session.suppressed_messages:
        return []
    drained = dedupe_messages_by_id(session.suppressed_messages)
    session.suppressed_messages = []
    return drained


async def _build_suppressed_batch_turn(
    *,
    chatter: NeoFatumChatter,
    response: Any,
    chat_stream: ChatStream,
    config: NFCConfig,
    session: NFCSession,
    prompt_builder: NFCPromptBuilder,
    image_budget: Any,
    has_history: bool,
    history_images_injected: bool,
    buffered_msgs: list[Any],
) -> TurnInputResult:
    """把抑制期缓冲的所有消息合并为单条 USER payload 并注入。

    复用 NEW_MESSAGES 分支的注入逻辑（add_user_message / build_user_payload /
    历史图片 / upsert 合并），但只调用一次 build_user_payload，确保上下文构建不重复。
    """
    filtered = filter_messages_already_in_payloads(response, buffered_msgs)
    if not filtered:
        # 缓冲消息全部已存在于 payload 中（罕见，可能被其他路径注入过），
        # 退化为常规超时分支。重新塞回 session 让上层走 timeout 路径。
        return TurnInputResult(
            response=response,
            unread_msgs=[],
            next_signal=Wait(),
            continue_loop=True,
            history_images_injected=history_images_injected,
            has_pending_tool_results=False,
            is_final_timeout=False,
            is_timeout_turn=False,
        )

    formatted_text = "\n".join(
        chatter.format_message_line(message, time_format="%Y-%m-%d %H:%M:%S")
        for message in filtered
    )
    logger.info(
        f"[等待抑制] 超时到期，一次性合并 {len(filtered)} 条缓冲消息为单条 USER payload"
    )

    for msg in filtered:
        sender_id = getattr(msg, "sender_id", "")
        session.add_user_message(
            content=getattr(msg, "processed_plain_text", "")
            or str(getattr(msg, "content", "")),
            user_name=getattr(msg, "sender_name", "用户"),
            user_id=sender_id,
            timestamp=chatter._extract_timestamp(msg),
            message_id=getattr(msg, "message_id", ""),
        )
        if sender_id:
            session.user_id = sender_id
        if chat_stream.platform:
            session.platform = chat_stream.platform

    if session.is_waiting():
        chatter._record_reply_timing(session)
        session.clear_waiting()

    media_items = chatter._extract_media(filtered, config, image_budget)

    if (
        not history_images_injected
        and has_history
        and image_budget is not None
        and not image_budget.is_exhausted()
    ):
        history_images_injected = True
        history_imgs = chatter._extract_history_media(chat_stream, image_budget)
        from ..services import MultimodalService

        MultimodalService.append_history_reference(response, history_imgs)

    user_payload, extra_payload = await prompt_builder.build_user_payload(
        formatted_unreads=formatted_text,
        media_items=media_items,
        stream_id=chatter.stream_id,
        config=config,
    )

    close_pending_tool_chain(response, reason="抑制期消息合并到达")

    upserted = False
    if (
        not media_items
        and response.payloads
        and response.payloads[-1].role == ROLE.USER
    ):
        last_payload = response.payloads[-1]
        if last_payload.content and isinstance(last_payload.content[-1], Text):
            existing = last_payload.content[-1].text  # type: ignore[attr-defined]
            last_payload.content[-1] = Text(
                f"{existing}\n{user_payload.content[-1].text}"  # type: ignore[attr-defined]
                if isinstance(user_payload.content, list)
                else f"{existing}\n{user_payload.content.text}"  # type: ignore[attr-defined]
            )
            upserted = True
            logger.debug("[NFC] Upsert USER payload（抑制期消息合并）")
    if not upserted:
        response.add_payload(user_payload)

    prepare_payload_chain_for_send(response, reason="抑制期合并完成")

    return TurnInputResult(
        response=response,
        unread_msgs=filtered,
        extra_payload=extra_payload,
        history_images_injected=history_images_injected,
        has_pending_tool_results=False,
        is_final_timeout=False,
        is_timeout_turn=False,
    )


def filter_messages_already_in_payloads(response: Any, messages: list[Any]) -> list[Any]:
    """过滤已存在于 response payload 文本中的 message_id。"""
    existing_ids = _message_ids_in_payloads(response)
    if not existing_ids:
        return messages
    filtered: list[Any] = []
    for message in messages:
        message_id = getattr(message, "message_id", None)
        if message_id and str(message_id) in existing_ids:
            continue
        filtered.append(message)
    return filtered


def _message_ids_in_payloads(response: Any) -> set[str]:
    """从现有 payload 文本中提取已经拼入的消息 ID。"""
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list):
        return set()
    ids: set[str] = set()
    for payload in payloads:
        content = getattr(payload, "content", None)
        parts = content if isinstance(content, list) else [content]
        for part in parts:
            text = getattr(part, "text", "")
            if not isinstance(text, str):
                continue
            marker = "[消息id:"
            start = 0
            while True:
                idx = text.find(marker, start)
                if idx < 0:
                    break
                value_start = idx + len(marker)
                value_end = text.find("]", value_start)
                if value_end < 0:
                    break
                message_id = text[value_start:value_end].strip()
                if message_id:
                    ids.add(message_id)
                start = value_end + 1
    return ids


async def commit_turn_decision(
    chatter: NeoFatumChatter,
    decision: Decision,
    response: Any,
    session: NFCSession,
    config: NFCConfig,
    prompt_builder: NFCPromptBuilder,
    chat_stream: ChatStream,
    pre_send_user_text: str,
    last_user_ts: float,
    chain_user_pre_saved: bool,
    is_final_timeout: bool,
) -> TurnControlResult:
    """提交本轮 Decision 对 session 与主循环的影响。"""
    session.add_bot_planning(
        thought=decision.thought,
        actions=decision.actions,
        expected_reaction=decision.expected_reaction,
        max_wait_seconds=decision.wait_seconds,
        raw_response=getattr(response, "message", "") or "",
    )

    # 记录情绪轨迹
    if decision.mood:
        session.record_mood(decision.mood)

    assistant_text = (getattr(response, "message", "") or "").strip()
    if not assistant_text:
        assistant_text = decision.reply_text
    if pre_send_user_text and assistant_text:
        if chain_user_pre_saved:
            session.update_chain(
                [{"role": "assistant", "text": assistant_text}],
                config.prompt.max_context_payloads,
            )
        else:
            session.update_chain(
                [
                    {"role": "user", "text": pre_send_user_text, "ts": last_user_ts},
                    {"role": "assistant", "text": assistant_text},
                ],
                config.prompt.max_context_payloads,
            )
        await chatter._save_session(session)
        session.compress_round_count += 1
        SummaryService.maybe_schedule_compression(
            session,
            prompt_builder,
            config,
            chat_stream,
            chatter._get_session_store(),
        )

    if not decision.has_meaningful_action:
        if response.message and response.message.strip():
            logger.warning(
                f"LLM 返回未形成有效决策: {response.message[:100]}"
            )
        await chatter._save_session(session)
        return TurnControlResult(
            next_signal=Stop(0),
            return_after_yield=True,
            is_final_timeout=is_final_timeout,
        )

    if decision.chose_silence and not decision.should_reply:
        if decision.wait_seconds <= 0:
            logger.debug("do_nothing（无等待），结束对话")
            await chatter._save_session(session)
            return TurnControlResult(
                next_signal=Stop(0),
                return_after_yield=True,
                is_final_timeout=is_final_timeout,
            )

    if decision.has_info_tool_calls and not decision.should_reply:
        logger.debug("信息工具调用完成，tool_result 已积累到 response 链，立即续轮")
        return TurnControlResult(
            continue_loop=True,
            has_pending_tool_results=True,
            is_final_timeout=is_final_timeout,
        )
    if (
        decision.has_third_party_calls
        and not decision.should_reply
        and not decision.chose_silence
    ):
        logger.debug(
            "第三方工具调用完成，tool_result 已积累到 response 链，下轮循环继续"
        )
        return TurnControlResult(
            continue_loop=True,
            has_pending_tool_results=True,
            is_final_timeout=is_final_timeout,
        )

    wait_seconds = config.wait.apply_rules(
        decision.wait_seconds,
        session.consecutive_timeout_count,
    )

    if is_final_timeout and wait_seconds > 0:
        logger.info("最后一次超时决策完成，强制结束等待")
        wait_seconds = 0
        is_final_timeout = False

    if wait_seconds > 0:
        waiting_config = WaitingConfig(
            expected_reaction=decision.expected_reaction,
            max_wait_seconds=wait_seconds,
            started_at=time.time(),
        )
        session.set_waiting(waiting_config)
        await chatter._save_session(session)
        return TurnControlResult(
            next_signal=_wait_until_session_timeout(session),
            continue_loop=True,
            is_final_timeout=is_final_timeout,
        )

    session.clear_waiting()
    await chatter._save_session(session)
    return TurnControlResult(
        next_signal=Stop(0),
        return_after_yield=True,
        is_final_timeout=is_final_timeout,
    )