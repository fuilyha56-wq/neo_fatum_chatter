"""NFC 运行时总控。"""

from __future__ import annotations

import asyncio
import time
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
from ..services.perception_extractor import extract_reply_from_perception
from .turn_controller import commit_turn_decision, prepare_turn_input

if TYPE_CHECKING:
    from ..chatter import NeoFatumChatter


logger = get_logger("NFC_chatter")


def _clone_payload(payload: LLMPayload) -> LLMPayload:
    """复制 payload 外壳，避免临时追加时改写快照对象。"""
    return LLMPayload(payload.role, list(payload.content))


def append_temporary_payload(response: Any, payload: LLMPayload) -> list[LLMPayload]:
    """临时追加 payload，并返回追加前快照。"""
    snapshot = [_clone_payload(item) for item in getattr(response, "payloads", []) or []]
    response.payloads = [_clone_payload(item) for item in snapshot]
    response.add_payload(payload)
    return snapshot


def restore_temporary_payload(response: Any, snapshot: list[LLMPayload]) -> None:
    """移除临时 payload，保留发送后追加的响应 payload。"""
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list):
        response.payloads = snapshot
        return

    if payloads[:len(snapshot)] == snapshot:
        restored_tail = payloads[len(snapshot):]
        if restored_tail:
            response.payloads = snapshot + restored_tail[1:]
        else:
            response.payloads = snapshot
        return

    response.payloads = snapshot + payloads[len(snapshot):]


async def execute_orchestrator(
    chatter: NeoFatumChatter,
) -> AsyncGenerator[Wait | Success | Failure | Stop, None]:
    """执行 NFC 对话主循环。"""
    from src.app.plugin_system.api.stream_api import activate_stream

    self = chatter

    chat_stream = await activate_stream(self.stream_id)
    if chat_stream is None:
        logger.error(f"无法激活聊天流: {self.stream_id}")
        yield Failure("聊天流激活失败")
        return
    config = self._get_config()

    if not config.general.enabled:
        logger.debug("NFC 插件已禁用，跳过 execute")
        yield Stop(0)
        return

    session = await self._get_session()
    timeout_service = TimeoutService(config)

    if config.general.native_multimodal:
        self._register_vlm_skip()

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
    ) = await self._build_initial_context(
        chat_stream,
        config,
        session,
        model_set,
    )

    history_images_injected = False
    has_pending_tool_results = False
    is_final_timeout = False
    pre_send_user_text = ""
    last_user_ts = 0.0
    chain_user_pre_saved = False
    extra_payload: LLMPayload | None = None
    consecutive_llm_failures = 0

    while True:
        turn_input = await prepare_turn_input(
            self,
            response,
            chat_stream,
            config,
            session,
            prompt_builder,
            timeout_service,
            image_budget,
            has_history,
            history_images_injected,
            has_pending_tool_results,
        )
        response = turn_input.response
        unread_msgs = turn_input.unread_msgs
        extra_payload = turn_input.extra_payload
        history_images_injected = turn_input.history_images_injected
        has_pending_tool_results = turn_input.has_pending_tool_results
        is_final_timeout = turn_input.is_final_timeout
        is_timeout_turn = turn_input.is_timeout_turn

        if turn_input.next_signal is not None:
            yield turn_input.next_signal
        if turn_input.continue_loop:
            continue

        if unread_msgs:
            last_user_ts = min(
                (self._extract_timestamp(message) for message in unread_msgs),
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
            if new_user_text != pre_send_user_text:
                pre_send_user_text = new_user_text
                chain_user_pre_saved = False

        if pre_send_user_text and not chain_user_pre_saved and not is_timeout_turn:
            session.update_chain(
                [{"role": "user", "text": pre_send_user_text, "ts": last_user_ts}],
                config.prompt.max_context_payloads,
            )
            await self._save_session(session)
            chain_user_pre_saved = True

        temporary_payload_snapshot: list[LLMPayload] | None = None
        if extra_payload is not None:
            temporary_payload_snapshot = append_temporary_payload(response, extra_payload)
        if config.debug.show_prompt:
            self._log_prompt(response)

        if unread_msgs:
            known_ids: frozenset[str] = frozenset(
                message_id
                for message in unread_msgs
                if (message_id := getattr(message, "message_id", None)) is not None
            )
        else:
            _, current_snapshot = await self.fetch_unreads(
                time_format="%Y-%m-%d %H:%M:%S"
            )
            known_ids = frozenset(
                message_id
                for message in current_snapshot
                if (message_id := getattr(message, "message_id", None)) is not None
            )

        try:
            if config.buffer.interrupt_enabled:
                new_response, interrupt_msgs = await self._send_interruptable(
                    response,
                    config,
                    known_ids,
                )
                if interrupt_msgs:
                    if temporary_payload_snapshot is not None:
                        restore_temporary_payload(response, temporary_payload_snapshot)
                    extra_payload = None
                    await self.flush_unreads(unread_msgs or [])
                    session.add_interrupt_event(interrupt_msgs)
                    await self._save_session(session)
                    continue
                assert new_response is not None
                response = new_response
            else:
                response = await self._send_with_perceive_loop(
                    response,
                    config.general.max_compat_retries,
                )
            await self.flush_unreads(unread_msgs if unread_msgs else [])
        except (LLMAuthenticationError, LLMRateLimitError, LLMTimeoutError,
                LLMTokenLimitError, LLMAPIError, LLMError) as exc:
            # 按子类输出差异化日志
            if isinstance(exc, LLMAuthenticationError):
                logger.error(
                    f"LLM 认证失败，请检查 API Key 配置 (model={exc.model}): {exc}",
                    exc_info=True,
                )
                await self._save_session(session)
                yield Failure("LLM 认证失败", exc)
                break
            elif isinstance(exc, LLMRateLimitError):
                logger.warning(
                    f"LLM 触发速率限制，建议等待 {exc.retry_after or '未知'} 秒后重试"
                    f" (model={exc.model}): {exc}",
                    exc_info=True,
                )
                retry_after = getattr(exc, "retry_after", None)
                delay = float(retry_after) if isinstance(retry_after, (int, float)) else 1.0
                await asyncio.sleep(max(0.1, min(delay, 30.0)))
            elif isinstance(exc, LLMTimeoutError):
                logger.warning(
                    f"LLM 请求超时 (timeout={exc.timeout}s, model={exc.model}): {exc}",
                    exc_info=True,
                )
                await asyncio.sleep(min(2.0 ** min(consecutive_llm_failures, 4), 30.0))
            elif isinstance(exc, LLMTokenLimitError):
                logger.error(
                    f"LLM Token 超限 (max={exc.max_tokens}, requested={exc.requested_tokens},"
                    f" model={exc.model}): {exc}",
                    exc_info=True,
                )
                await self._save_session(session)
                yield Failure("LLM Token 超限", exc)
                break
            elif isinstance(exc, LLMAPIError):
                logger.error(
                    f"LLM API 错误 (status={exc.status_code}, code={exc.error_code},"
                    f" model={exc.model}): {exc}",
                    exc_info=True,
                )
            else:
                logger.error(f"LLM 请求失败: {exc}", exc_info=True)

            # 统一清理与失败计数
            if temporary_payload_snapshot is not None:
                restore_temporary_payload(response, temporary_payload_snapshot)
            extra_payload = None
            consecutive_llm_failures += 1
            _fail_limit = config.general.max_consecutive_llm_failures
            if _fail_limit > 0 and consecutive_llm_failures >= _fail_limit:
                logger.error(
                    f"连续 LLM 失败已达上限 ({consecutive_llm_failures}/{_fail_limit})，终止会话循环"
                )
                await self._save_session(session)
                yield Failure("LLM 连续失败次数超限", exc)
                break
            continue
        except Exception as exc:
            logger.error(f"LLM 请求失败（未知错误）: {repr(exc)}", exc_info=True)
            if temporary_payload_snapshot is not None:
                restore_temporary_payload(response, temporary_payload_snapshot)
            extra_payload = None
            await self._save_session(session)
            yield Failure("LLM 请求失败", exc)
            break

        # LLM 请求成功，重置连续失败计数
        consecutive_llm_failures = 0

        if temporary_payload_snapshot is not None:
            restore_temporary_payload(response, temporary_payload_snapshot)
        extra_payload = None

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
                    model_task=config.general.model_task,
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
                        trigger_msg = await self._get_virtual_trigger_message()
                    sent = await self._execute_reply(
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
                            self,
                            decision,
                            response,
                            session,
                            config,
                            prompt_builder,
                            chat_stream,
                            pre_send_user_text,
                            last_user_ts,
                            chain_user_pre_saved,
                            is_final_timeout,
                        )
                        is_final_timeout = turn_control.is_final_timeout
                        if turn_control.has_pending_tool_results:
                            has_pending_tool_results = True
                        if turn_control.next_signal is not None:
                            yield turn_control.next_signal
                        if turn_control.return_after_yield:
                            return
                        if turn_control.continue_loop:
                            continue
                        return

        trigger_msg = unread_msgs[-1] if unread_msgs else None
        if trigger_msg is None:
            trigger_msg = await self._get_virtual_trigger_message()
        decision = await parse_response_decision(
            response,
            usable_map,
            trigger_msg,
            config,
            execute_reply_fn=self._execute_reply,
            run_tool_call_fn=self.run_tool_call,
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
            self,
            decision,
            response,
            session,
            config,
            prompt_builder,
            chat_stream,
            pre_send_user_text,
            last_user_ts,
            chain_user_pre_saved,
            is_final_timeout,
        )
        is_final_timeout = turn_control.is_final_timeout

        if turn_control.has_pending_tool_results:
            has_pending_tool_results = True

        if turn_control.next_signal is not None:
            yield turn_control.next_signal
        if turn_control.return_after_yield:
            return
        if turn_control.continue_loop:
            continue
