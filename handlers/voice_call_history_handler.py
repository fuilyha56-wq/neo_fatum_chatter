"""voice_call.ended 事件处理器：把通话整段打包成一对摘要补到 chain_payloads。

设计背景：

- 用户在 NFC 私聊里和 bot 聊天 → 模型调 ``start_voice_call`` →
  anima_chatter 接管该 stream，进入语音通话。
- 通话期间所有 user / assistant 对话由 anima_chatter 处理；它会写入
  ``chat_stream.context.history_messages``，但**不会**写入 NFC 的
  ``session.chain_payloads``。
- 通话结束时 anima_chatter 广播 ``voice_call.ended`` 事件，payload 含通话
  期间发生的所有消息。

关键设计权衡（**不**逐条入 chain）：

- NFC 的 ``chain_payloads`` 默认上限 20 条（``max_context_payloads``）。
  一通 5 分钟的通话很可能产生 10+ 条消息，逐条补入会**吞掉一半 chain 额度**。
- 改为把整段通话**打包成一对**（1 user + 1 assistant）摘要：
  - user 条目：把通话期间所有对话按时间顺序编号排列，加边界标记。
  - assistant 条目：简短确认 + 通话结束边界。
- 这样无论通话多长，对 chain 占用都恒定为 1 对。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler

if TYPE_CHECKING:
    from src.app.plugin_system.api.event_api import EventType


logger = get_logger("NFC_voice_call_history_handler")

_VOICE_CALL_ENDED_EVENT = "voice_call.ended"

# NFC chatter 的组件签名
_NFC_SIGNATURE = "neo_fatum_chatter:chatter:neo_fatum_chatter"


class VoiceCallHistoryHandler(BaseEventHandler):
    """订阅 ``voice_call.ended``，把通话历史打包成一对摘要补回 NFC session。"""

    handler_name: str = "nfc_voice_call_history_handler"
    handler_description: str = (
        "通话结束后把 anima_chatter 在 NFC 流上记录的整段对话打包成一对 "
        "user/assistant 摘要补回 session.chain_payloads，保证挂断后上下文连贯，"
        "且不会用一通通话挤占多个 chain 槽位。"
    )
    weight: int = 0
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [_VOICE_CALL_ENDED_EVENT]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 voice_call.ended 事件。"""

        # 只处理通话发起前是 NFC 接管的 stream
        previous_signature = str(params.get("previous_chatter_signature") or "")
        if previous_signature != _NFC_SIGNATURE:
            return EventDecision.PASS, params

        stream_id = str(params.get("caller_stream_id") or "")
        if not stream_id:
            return EventDecision.PASS, params

        messages_in_call = params.get("messages_in_call") or []
        if not isinstance(messages_in_call, list) or not messages_in_call:
            logger.debug(f"voice_call.ended 无消息，跳过 stream={stream_id[:8]}")
            return EventDecision.PASS, params

        try:
            await self._patch_chain(stream_id, messages_in_call, params)
        except Exception as exc:
            logger.error(
                f"补 chain_payloads 异常 stream={stream_id[:8]}: {exc}",
                exc_info=True,
            )
            return EventDecision.PASS, params

        return EventDecision.SUCCESS, params

    @staticmethod
    def _summarize_messages(
        messages_in_call: list[Any],
    ) -> tuple[str, str, float]:
        """把整段通话压缩成"严格按时间顺序的编号交替对话稿"。

        组装策略：
        - 把 user / assistant 消息按时间顺序穿插编号；
        - system 标注（开始 / 结束元事件）单独写在对话稿的顶部 / 底部，作为
          通话边界，不参与编号；
        - user 摘要 = 顶部边界 + 整段对话稿；
        - assistant 摘要 = 底部边界 + 一句简短确认。

        Args:
            messages_in_call: anima_chatter 传过来的消息列表，每条形如
                ``{"role": "user"|"assistant"|"system", "text": str, "ts": float}``。

        Returns:
            ``(user_summary, assistant_summary, first_user_ts)``
        """
        system_open: str = ""
        system_close: str = ""
        seen_system_count = 0
        timeline: list[tuple[str, str, float]] = []
        first_user_ts: float = 0.0
        fallback_ts: float = 0.0

        for msg in messages_in_call:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            ts_raw = msg.get("ts")
            ts = float(ts_raw) if isinstance(ts_raw, (int, float)) and ts_raw > 0 else 0.0

            if role == "system":
                if seen_system_count == 0:
                    system_open = text
                else:
                    system_close = text
                seen_system_count += 1
                if fallback_ts == 0.0 and ts > 0:
                    fallback_ts = ts
            elif role in ("user", "assistant"):
                timeline.append((role, text, ts))
                if role == "user" and first_user_ts == 0.0 and ts > 0:
                    first_user_ts = ts

        # 编号交替对话稿
        lines: list[str] = []
        round_no = 0
        bot_pre_label = "（接通时）"
        for role, text, _ in timeline:
            if role == "user":
                round_no += 1
                lines.append(f"【第 {round_no} 轮 / 用户】{text}")
            else:
                if round_no == 0:
                    label = bot_pre_label
                else:
                    label = f"（第 {round_no} 轮回应）"
                lines.append(f"【你的回应{label}】{text}")

        if not lines:
            timeline_block = "（通话期间没有任何对话发生。）"
        else:
            timeline_block = "\n".join(lines)

        # user 摘要
        user_parts: list[str] = []
        if system_open:
            user_parts.append(system_open)
        user_parts.append("【通话对话稿（按时间顺序）】")
        user_parts.append(timeline_block)
        user_summary = "\n\n".join(user_parts)

        # assistant 摘要
        assistant_parts: list[str] = []
        assistant_parts.append("（已收到上面整段通话稿。）")
        if system_close:
            assistant_parts.append(system_close)
        assistant_summary = "\n\n".join(assistant_parts)

        return (
            user_summary,
            assistant_summary,
            first_user_ts or fallback_ts or time.time(),
        )

    async def _patch_chain(
        self,
        stream_id: str,
        messages_in_call: list[Any],
        event_params: dict[str, Any],
    ) -> None:
        """把整段通话打包成一对 (user, assistant) 摘要写入 NFC chain_payloads。"""

        from ..config import NFCConfig
        from ..plugin import NFCPlugin

        plugin = self.plugin
        if not isinstance(plugin, NFCPlugin):
            logger.warning("VoiceCallHistoryHandler 不在 NFCPlugin 上下文，跳过")
            return

        config = plugin.config
        if not isinstance(config, NFCConfig):
            logger.warning("NFC 配置未加载，跳过 chain_payloads 补丁")
            return

        user_summary, assistant_summary, first_user_ts = self._summarize_messages(
            messages_in_call
        )

        if not user_summary.strip() and not assistant_summary.strip():
            logger.debug(f"通话摘要全为空 stream={stream_id[:8]}，跳过 chain 补丁")
            return

        entries = [
            {"role": "user", "text": user_summary, "ts": first_user_ts},
            {"role": "assistant", "text": assistant_summary},
        ]

        # 走 per-stream 锁串行化
        store = plugin.session_store
        async with store.lock(stream_id):
            session = await store.get_or_create(stream_id)
            session.update_chain(entries, config.prompt.max_context_payloads)
            await store.save(session)

        raw_count = sum(
            1
            for m in messages_in_call
            if isinstance(m, dict) and str(m.get("text") or "").strip()
        )
        duration = float(event_params.get("duration_seconds") or 0.0)
        logger.info(
            f"已把通话打包成 1 对 chain entry stream={stream_id[:8]} "
            f"(原始消息 {raw_count} 条 / 持续 {duration:.0f}s)"
        )


__all__ = ["VoiceCallHistoryHandler"]
