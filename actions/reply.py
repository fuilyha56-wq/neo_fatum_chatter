"""NFC 回复动作（薄壳）。

真正的清洗、分段与发送逻辑在 ``execution/reply_executor.py``。
本文件只保留 LLM 工具调用 schema 与最小的执行桥接，便于通过
``BaseAction`` 注册到框架的 tool 路由。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from ..execution.reply_executor import (
    coerce_content_segments,
    sanitize_segment,
    send_reply_segments,
)

logger = get_logger("NFC_reply")


class NFCReplyAction(BaseAction):
    """发送文本消息给对方。"""

    action_name = "nfc_reply"
    action_description = (
        "发送文本消息给对方。"
        "content 为消息段落列表，每个元素是一条独立消息，系统会依次发出。"
        "可选的 reply_to 参数允许你引用消息（虽然私聊中较少用到，但引用旧消息时可能有用）。"
        "注意：本工具无法发送表情包等非文本内容。"
    )

    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        content: Annotated[
            list[str] | str | None,
            "要发送的消息段落列表；可为单条字符串、字符串列表，或为空。",
        ] = None,
        thought: Annotated[str, "你此刻的内心想法和感受，描述你为什么要这样回复"] = "",
        expected_reaction: Annotated[str, "你期望对方看到你这条消息后的反应"] = "",
        max_wait_seconds: Annotated[float, "你愿意等待对方回复的最长时间(秒)，0表示不等待"] = 0.0,
        mood: Annotated[str, "你当前的心情，用一两个词描述"] = "",
        reply_to: Annotated[str, "可选，要引用回复的消息 ID"] = "",
        **_extra,
    ):
        """执行发送文本消息的逻辑。

        支持异步生成器暂停点，让标准 tool 调度器能按调用顺序门控多个发送动作。

        ``**_extra`` 用于吞掉 LLM 偶尔幻觉出的未知参数（例如 ``emotion_tags``），
        避免 ``TypeError`` 中断整轮执行。schema 解析器会跳过 VAR_KEYWORD，
        因此该参数不会出现在 Tool Schema 中。
        """
        if _extra:
            logger.debug(f"忽略 nfc_reply 未知参数: {sorted(_extra.keys())}")

        _ = thought, expected_reaction, max_wait_seconds, mood

        raw_segments = coerce_content_segments(content)
        if not raw_segments:
            yield False, "内容为空，未发送"
            return

        cleaned_segments: list[str] = []
        for segment in raw_segments:
            cleaned, _stripped_thinking, _stripped_meta = sanitize_segment(segment)
            if cleaned:
                cleaned_segments.append(cleaned)

        if not cleaned_segments:
            yield False, "清洗后内容为空，未发送"
            return

        # 段间延迟从 plugin 配置读取，沿用旧路径以避免产生新的依赖入口。
        segment_delay_min = 0.5
        segment_delay_max = 2.0
        streaming_enabled = False
        streaming_service_signature = ""
        streaming_chunk_size = 10
        streaming_interval = 0.1
        try:
            from src.app.plugin_system.api.config_api import get_config
            nfc_config = get_config("neo_fatum_chatter")
            if nfc_config:
                reply_section = getattr(nfc_config, "reply", None)
                segment_delay_min = float(getattr(reply_section, "segment_delay_min", 0.5))
                segment_delay_max = float(getattr(reply_section, "segment_delay_max", 2.0))
                streaming_enabled = bool(getattr(reply_section, "streaming_enabled", False))
                streaming_service_signature = str(
                    getattr(reply_section, "streaming_service_signature", "") or ""
                ).strip()
                streaming_chunk_size = int(getattr(reply_section, "streaming_chunk_size", 10))
                streaming_interval = float(getattr(reply_section, "streaming_interval", 0.1))
        except Exception:
            pass

        async def _yield_point() -> None:
            # 让出控制权给标准 tool 调度器；保持 action 旧版的暂停语义。
            await asyncio.sleep(0)

        yield None
        trigger_msg = self._get_context_message_for_target(reply_to or None)
        sent, ok = await send_reply_segments(
            cleaned_segments,
            stream_id=self.chat_stream.stream_id,
            reply_to=reply_to,
            send_segment=self._send_to_stream,
            segment_delay_min=segment_delay_min,
            segment_delay_max=segment_delay_max,
            yield_point=_yield_point,
            streaming_enabled=streaming_enabled,
            streaming_service_signature=streaming_service_signature,
            streaming_chunk_size=streaming_chunk_size,
            streaming_interval=streaming_interval,
            trigger_msg=trigger_msg,
        )

        if not ok:
            yield False, "消息发送失败"
            return

        if not sent:
            yield False, "未发送任何消息"
            return

        yield True, f"已发送 {len(sent)} 条消息"
