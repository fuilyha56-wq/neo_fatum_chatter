"""NFC 运行时总控。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncGenerator

from src.app.plugin_system.api.llm_api import (
    get_model_set_by_name,
    get_model_set_by_task,
)
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import Failure, Stop, Success, Wait
from src.kernel.llm import LLMPayload, ROLE, Text
from src.kernel.llm.exceptions import (
    LLMAPIError,
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTokenLimitError,
    LLMError,
)

from ..debug.log_formatter import log_nfc_result
from ..parser import coerce_call_list
from ..protocol.compat_adapter import prepare_nfc_model_set
from ..protocol.decision_parser import parse_response_decision
from ..services import (
    ProactiveService,
    TimeoutService,
)
from ..services.context_sanitizer import heal_orphan_tool_results
from ..services.perception_extractor import extract_reply_from_perception
from .request_view import build_request_view, strip_transient_payloads
from .turn_controller import (
    commit_turn_decision,
    filter_messages_already_in_payloads,
    prepare_turn_input,
)

if TYPE_CHECKING:
    from ..chatter import NeoFatumChatter


logger = get_logger("NFC_chatter")

# 热替换摘要的 marker 前缀
_SUMMARY_MARKER_PREFIX = "【你对"
_SUMMARY_MARKER_SUFFIX = "的近期记忆】"


def _hot_update_summary(response: Any, session: Any) -> None:
    """当异步压缩完成后，热替换 response chain 中的 summary 部分。

    定位动态 USER payload 中以「【你对...的近期记忆】」开头的段落，
    如果 session.history_summary 已更新则原地替换，使当前对话立即受益。
    """
    new_summary = getattr(session, "history_summary", "") or ""
    if not new_summary.strip():
        return

    payloads = getattr(response, "payloads", None)
    if not payloads:
        return

    for payload in payloads:
        if payload.role != ROLE.USER:
            continue
        content_items = payload.content if isinstance(payload.content, list) else [payload.content]
        for i, item in enumerate(content_items):
            text = getattr(item, "text", "")
            if not isinstance(text, str):
                continue
            marker_idx = text.find(_SUMMARY_MARKER_PREFIX)
            if marker_idx < 0:
                continue

            # 找到 marker 所在行末尾（包含 "的近期记忆】"）
            line_end = text.find("\n", marker_idx)
            if line_end < 0:
                # marker 是最后一行，后面全是 summary
                old_block_end = len(text)
            else:
                # 找下一个分隔符 "---" 或下一个 marker
                next_sep = text.find("\n\n---\n\n", line_end)
                old_block_end = next_sep if next_sep > 0 else len(text)

            # 提取 user_name 用于重建 marker
            suffix_pos = text.find(_SUMMARY_MARKER_SUFFIX, marker_idx)
            if suffix_pos > marker_idx:
                user_name = text[marker_idx + len(_SUMMARY_MARKER_PREFIX):suffix_pos]
            else:
                user_name = "对方"

            new_block = f"{_SUMMARY_MARKER_PREFIX}{user_name}{_SUMMARY_MARKER_SUFFIX}\n{new_summary.strip()}"
            new_text = text[:marker_idx] + new_block + text[old_block_end:]

            if isinstance(payload.content, list):
                payload.content[i] = Text(new_text)
            else:
                payload.content = Text(new_text)
            return  # 只替换第一个匹配


@dataclass(slots=True)
class _LoopState:
    """主循环跨迭代可变状态。"""

    pre_send_user_text: str = ""
    last_user_ts: float = 0.0
    chain_user_pre_saved: bool = False
    extra_payload: LLMPayload | None = None
    consecutive_llm_failures: int = 0
    has_pending_tool_results: bool = False
    is_final_timeout: bool = False
    history_images_injected: bool = False


class _LLMErrorOutcome:
    """LLM 错误处理结果。"""
    __slots__ = ("should_break", "should_continue", "failure", "retry_delay")

    def __init__(
        self,
        *,
        should_break: bool = False,
        should_continue: bool = False,
        failure: Failure | None = None,
        retry_delay: float = 0.0,
    ) -> None:
        self.should_break = should_break
        self.should_continue = should_continue
        self.failure = failure
        self.retry_delay = retry_delay


def _handle_llm_error(
    exc: Exception,
    *,
    config: NFCConfig,
    consecutive_llm_failures: int,
) -> _LLMErrorOutcome:
    """处理 LLM 请求异常，返回控制指令。"""
    if isinstance(exc, LLMAuthenticationError):
        logger.error(
            f"LLM 认证失败，请检查 API Key 配置 (model={exc.model}): {exc}",
            exc_info=True,
        )
        return _LLMErrorOutcome(
            should_break=True,
            failure=Failure("LLM 认证失败", exc),
        )

    if isinstance(exc, LLMRateLimitError):
        logger.warning(
            f"LLM 触发速率限制，建议等待 {exc.retry_after or '未知'} 秒后重试"
            f" (model={exc.model}): {exc}",
            exc_info=True,
        )
        retry_after = getattr(exc, "retry_after", None)
        delay = float(retry_after) if isinstance(retry_after, (int, float)) else 1.0
        return _LLMErrorOutcome(
            should_continue=True,
            retry_delay=max(0.1, min(delay, 30.0)),
        )

    if isinstance(exc, LLMTimeoutError):
        logger.warning(
            f"LLM 请求超时 (timeout={exc.timeout}s, model={exc.model}): {exc}",
            exc_info=True,
        )
        return _LLMErrorOutcome(should_continue=True)

    if isinstance(exc, LLMTokenLimitError):
        logger.error(
            f"LLM Token 超限 (max={exc.max_tokens}, requested={exc.requested_tokens},"
            f" model={exc.model}): {exc}",
            exc_info=True,
        )
        return _LLMErrorOutcome(
            should_break=True,
            failure=Failure("LLM Token 超限", exc),
        )

    if isinstance(exc, LLMAPIError):
        logger.error(
            f"LLM API 错误 (status={exc.status_code}, code={exc.error_code},"
            f" model={exc.model}): {exc}",
            exc_info=True,
        )
        # 不 break，可重试
        return _LLMErrorOutcome(should_continue=True)

    # LLMError 或其他已知 LLM 异常
    if isinstance(exc, LLMError):
        logger.error(f"LLM 请求失败: {exc}", exc_info=True)
        return _LLMErrorOutcome(should_continue=True)

    # 未知异常
    logger.error(f"LLM 请求失败（未知错误）: {repr(exc)}", exc_info=True)
    return _LLMErrorOutcome(
        should_break=True,
        failure=Failure("LLM 请求失败", exc),
    )


async def execute_orchestrator(
    chatter: NeoFatumChatter,
) -> AsyncGenerator[Wait | Success | Failure | Stop, None]:
    """执行 NFC 对话主循环。"""
    from src.app.plugin_system.api.stream_api import activate_stream

    chat_stream = await activate_stream(chatter.stream_id)
    if chat_stream is None:
        logger.error(f"无法激活聊天流: {chatter.stream_id}")
        yield Failure("聊天流激活失败")
        return
    config = chatter._get_config()

    if not config.general.enabled:
        logger.debug("NFC 插件已禁用，跳过 execute")
        yield Stop(0)
        return

    session = await chatter._get_session()
    timeout_service = TimeoutService(config)

    if config.general.native_multimodal:
        chatter._register_vlm_skip()

    model_set = None
    temperature = config.general.temperature
    max_tokens = config.general.max_tokens
    if config.general.models:
        parts = [
            get_model_set_by_name(
                model_name,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            for model_name in config.general.models
        ]
        valid_parts = [part for part in parts if part]
        if valid_parts:
            model_set = valid_parts[0]
            for part in valid_parts[1:]:
                model_set = model_set + part
        if not model_set:
            logger.warning(
                f"models 中的模型均未注册: {config.general.models}，"
                f"回退到任务模型 '{config.general.model_task}'"
            )

    if not model_set:
        model_set = get_model_set_by_task(config.general.model_task)

    if not model_set:
        logger.error("无法获取模型配置")
        yield Failure("模型配置错误：未找到有效的模型配置")
        return

    model_set = prepare_nfc_model_set(model_set)

    (
        response,
        image_budget,
        usable_map,
        prompt_builder,
        has_history,
    ) = await chatter._build_initial_context(
        chat_stream,
        config,
        session,
        model_set,
    )

    loop_state = _LoopState()

    while True:
        heal_orphan_tool_results(response, where="loop-top")
        _hot_update_summary(response, session)
        turn_input = await prepare_turn_input(
            chatter,
            response,
            chat_stream,
            config,
            session,
            prompt_builder,
            timeout_service,
            image_budget,
            has_history,
            loop_state.history_images_injected,
            loop_state.has_pending_tool_results,
        )
        response = turn_input.response
        unread_msgs = turn_input.unread_msgs
        loop_state.extra_payload = turn_input.extra_payload
        loop_state.history_images_injected = turn_input.history_images_injected
        loop_state.has_pending_tool_results = turn_input.has_pending_tool_results
        loop_state.is_final_timeout = turn_input.is_final_timeout
        is_timeout_turn = turn_input.is_timeout_turn

        if turn_input.next_signal is not None:
            yield turn_input.next_signal
        if turn_input.continue_loop:
            continue

        if unread_msgs:
            loop_state.last_user_ts = max(
                (chatter._extract_timestamp(message) for message in unread_msgs),
                default=time.time(),
            )

        # timeout turn 不应将 timeout 提示文本持久化进 chain，
        # 它是运行时临时提示，不是用户真实消息。
        if not is_timeout_turn:
            new_user_text = ""
            for payload in reversed(response.payloads):
                if payload.role == ROLE.USER:
                    new_user_text = "".join(
                        chunk.text  # type: ignore[attr-defined]
                        for chunk in payload.content
                        if isinstance(chunk, Text)
                    )
                    break
            if new_user_text != loop_state.pre_send_user_text:
                loop_state.pre_send_user_text = new_user_text
                loop_state.chain_user_pre_saved = False

        if loop_state.pre_send_user_text and not loop_state.chain_user_pre_saved and not is_timeout_turn:
            session.update_chain(
                [{"role": "user", "text": loop_state.pre_send_user_text, "ts": loop_state.last_user_ts}],
                config.prompt.max_context_payloads,
            )
            await chatter._save_session(session)
            loop_state.chain_user_pre_saved = True

        transient_payloads: list[LLMPayload] = []
        if loop_state.extra_payload is not None:
            transient_payloads.append(loop_state.extra_payload)
        send_target = build_request_view(response, transient_payloads)
        if config.debug.show_prompt:
            chatter._log_prompt(send_target)

        if unread_msgs:
            known_ids: frozenset[str] = frozenset(
                message_id
                for message in unread_msgs
                if (message_id := getattr(message, "message_id", None)) is not None
            )
        else:
            _, current_snapshot = await chatter.fetch_unreads(
                time_format="%Y-%m-%d %H:%M:%S"
            )
            known_ids = frozenset(
                message_id
                for message in current_snapshot
                if (message_id := getattr(message, "message_id", None)) is not None
            )

        try:
            if config.buffer.interrupt_enabled and not transient_payloads:
                new_response, interrupt_msgs = await chatter._send_interruptable(
                    response,
                    config,
                    known_ids,
                )
                if interrupt_msgs:
                    loop_state.extra_payload = None
                    await chatter.flush_unreads(unread_msgs or [])
                    session.add_interrupt_event(interrupt_msgs)
                    await chatter._save_session(session)
                    continue
                if new_response is None:
                    logger.warning("[打断] LLM 被取消但无新消息，重新发起请求")
                    continue
                response = new_response
            else:
                if transient_payloads:
                    response = await send_target.send(
                        auto_append_response=True,
                        stream=False,
                    )
                    response = strip_transient_payloads(send_target, response)
                else:
                    response = await chatter._send_with_perceive_loop(
                        response,
                        config.general.max_compat_retries,
                    )
            await chatter.flush_unreads(unread_msgs if unread_msgs else [])
        except (LLMAuthenticationError, LLMRateLimitError, LLMTimeoutError,
                LLMTokenLimitError, LLMAPIError, LLMError) as exc:
            outcome = _handle_llm_error(
                exc,
                config=config,
                consecutive_llm_failures=loop_state.consecutive_llm_failures,
            )
            loop_state.extra_payload = None
            loop_state.consecutive_llm_failures += 1

            # 可重试类错误：指数退避后 continue
            if outcome.should_continue:
                if outcome.retry_delay > 0:
                    await asyncio.sleep(outcome.retry_delay)
                elif isinstance(exc, LLMTimeoutError):
                    await asyncio.sleep(
                        min(2.0 ** min(loop_state.consecutive_llm_failures, 4), 30.0)
                    )

            # 不可重试类错误：保存 session 并 yield Failure
            if outcome.should_break:
                await chatter._save_session(session)
                if outcome.failure:
                    yield outcome.failure
                break

            # 连续失败上限检查
            _fail_limit = config.general.max_consecutive_llm_failures
            if _fail_limit > 0 and loop_state.consecutive_llm_failures >= _fail_limit:
                logger.error(
                    f"连续 LLM 失败已达上限 ({loop_state.consecutive_llm_failures}/{_fail_limit})，终止会话循环"
                )
                await chatter._save_session(session)
                yield Failure("LLM 连续失败次数超限", exc)
                break
            continue
        except Exception as exc:
            logger.error(f"LLM 请求失败（未知错误）: {repr(exc)}", exc_info=True)
            loop_state.extra_payload = None
            await chatter._save_session(session)
            yield Failure("LLM 请求失败", exc)
            break

        # LLM 请求成功，重置连续失败计数
        loop_state.consecutive_llm_failures = 0
        heal_orphan_tool_results(response, where="post-send")
        loop_state.extra_payload = None

        call_list = coerce_call_list(response)
        if call_list:
            logger.info(f"本轮调用列表：{[call.name for call in call_list]}")
        elif getattr(response, "message", ""):
            logger.debug("[NFC] 本轮无 tool call，等待标准化器判定是否需要重试")

        # ── 兜底：感知阶段耗尽重试仍无工具调用时，用 sub actor 提取回复 ──
        if not call_list:
            fallback_text = (getattr(response, "message", "") or "").strip()
            if fallback_text:
                logger.warning(
                    f"[NFC] 感知阶段耗尽重试仍无工具调用，交由 sub actor 提取回复: "
                    f"{fallback_text[:80]}{'...' if len(fallback_text) > 80 else ''}"
                )
                extracted = await extract_reply_from_perception(
                    fallback_text,
                    model_task=config.general.perception_extract_task,
                )
                if not extracted:
                    logger.debug("[NFC] sub actor 未提取到有效内容，跳过发送")
                else:
                    reply_text = extracted
                    logger.info(
                        f"[NFC] sub actor 提取结果: "
                        f"{reply_text[:80]}{'...' if len(reply_text) > 80 else ''}"
                    )

                    trigger_msg = unread_msgs[-1] if unread_msgs else None
                    if trigger_msg is None:
                        trigger_msg = await chatter._get_virtual_trigger_message()
                    sent = await chatter._execute_reply(
                        reply_text, config, trigger_msg, ""
                    )
                    if sent:
                        # 构造等效的 decision 以正确更新 session
                        from ..domain.decision import Decision
                        decision = Decision(
                            thought="(兜底：模型未输出工具调用，纯文本直接发送)",
                            visible_reply_segments=[reply_text],
                            has_reply_action=True,
                            has_meaningful_action=True,
                            actions=[{"type": "nfc_reply", "content": [reply_text]}],
                        )
                        turn_control = await commit_turn_decision(
                            chatter,
                            decision,
                            response,
                            session,
                            config,
                            prompt_builder,
                            chat_stream,
                            loop_state.pre_send_user_text,
                            loop_state.last_user_ts,
                            loop_state.chain_user_pre_saved,
                            loop_state.is_final_timeout,
                        )
                        loop_state.is_final_timeout = turn_control.is_final_timeout
                        if turn_control.has_pending_tool_results:
                            loop_state.has_pending_tool_results = True
                        if turn_control.next_signal is not None:
                            yield turn_control.next_signal
                        if turn_control.return_after_yield:
                            return
                        if turn_control.continue_loop:
                            continue
                        return

        trigger_msg = unread_msgs[-1] if unread_msgs else None
        if trigger_msg is None:
            trigger_msg = await chatter._get_virtual_trigger_message()
        decision = await parse_response_decision(
            response,
            usable_map,
            trigger_msg,
            config,
            run_tool_call_fn=chatter.run_tool_call,
            pre_execute_hook=lambda result: log_nfc_result(result, config),
        )

        if decision.has_reply_action:
            reply_preview = (
                decision.visible_reply_segments[0][:60]
                if decision.visible_reply_segments
                else "(无可见文本)"
            )
            logger.info(
                f"决策完成: has_reply=True, "
                f"segments={len(decision.visible_reply_segments)}, "
                f"preview={reply_preview!r}"
            )
        elif not decision.has_meaningful_action:
            logger.warning(
                "决策完成: 无有效动作 (has_meaningful_action=False)"
            )

        if decision.proactive_schedule is not None:
            try:
                ProactiveService.apply_schedule(
                    session,
                    decision.proactive_schedule,
                )
            except Exception as exc:
                logger.warning(f"[NFC] schedule_proactive 参数解析失败: {exc}")

        turn_control = await commit_turn_decision(
            chatter,
            decision,
            response,
            session,
            config,
            prompt_builder,
            chat_stream,
            loop_state.pre_send_user_text,
            loop_state.last_user_ts,
            loop_state.chain_user_pre_saved,
            loop_state.is_final_timeout,
        )
        loop_state.is_final_timeout = turn_control.is_final_timeout

        if turn_control.has_pending_tool_results:
            loop_state.has_pending_tool_results = True

        # yield Wait/Stop 前补探一次未读。
        # 框架的 _wait_state_check 以 yield 瞬间的未读计数为基线，仅当
        # 之后计数继续增长才唤醒。若有消息恰好在本轮处理末尾、yield 之前
        # 到达，它会被算进基线却从未被处理，导致该消息被"困住"——必须再
        # 发一条新消息把计数推高才会触发。这里在让出前直接探测：若发现尚
        # 未进入 payload 的新消息，就不让出，直接续轮交由 prepare_turn_input
        # 当作 NEW_MESSAGES 处理。
        if turn_control.next_signal is not None:
            _, pending_msgs = await chatter.fetch_unreads(
                time_format="%Y-%m-%d %H:%M:%S"
            )
            pending_msgs = filter_messages_already_in_payloads(response, pending_msgs)
            if pending_msgs:
                logger.info(
                    f"[补探] yield 前检测到 {len(pending_msgs)} 条未处理新消息，"
                    "跳过让出直接续轮"
                )
                continue

        if turn_control.next_signal is not None:
            yield turn_control.next_signal
        if turn_control.return_after_yield:
            return
        if turn_control.continue_loop:
            continue
