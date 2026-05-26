"""NFC 回复动作。

包含最后一道防线：防御性清洗 content 中混入的元数据。
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseAction

from ..protocol.response_normalizer import strip_thinking_blocks

logger = get_logger("NFC_reply")

# 元数据关键字模式（最后防线）
# 仅当多个元数据关键字同时出现时才判定为泄漏，降低误伤概率
_METADATA_KEYWORDS = [
    r"(?:想法|内心想法|思考|thought|thinking)\s*[:：]",
    r"(?:预计反应|预期反应|expected_reaction)\s*[:：]",
    r"(?:最大等待秒数|max_wait_seconds)\s*[:：]",
    r"(?:心情|情绪|mood)\s*[:：]",
]
_METADATA_PATTERNS = [re.compile(kw, re.IGNORECASE) for kw in _METADATA_KEYWORDS]


def _coerce_content_segments(content: list[str] | str | None) -> list[str]:
    """把模型传来的 content 统一规整成可发送文本段落。

    有些模型会把 `content` 错传成 JSON 字符串，例如 `["在呢。"]`。
    如果不先解析，就会把方括号和引号原样发出去，笨蛋模型真会添乱！
    """
    if content is None:
        return []

    raw_items: list[Any]
    if isinstance(content, str):
        stripped = content.strip()
        if not stripped:
            return []

        parsed: Any | None = None
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                candidate = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                candidate = None
            if isinstance(candidate, list):
                parsed = candidate

        if isinstance(parsed, list):
            raw_items = parsed
        else:
            raw_items = [stripped]
    else:
        raw_items = list(content)

    segments: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            text = item.strip()
        else:
            text = str(item).strip()
        if text:
            segments.append(text)
    return segments


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
        import asyncio
        import random as _random

        if _extra:
            logger.debug(f"忽略 nfc_reply 未知参数: {sorted(_extra.keys())}")

        _ = thought, expected_reaction, max_wait_seconds, mood

        segments = _coerce_content_segments(content)
        if not segments:
            yield False, "内容为空，未发送"
            return

        # 获取段间延迟配置
        segment_delay_min = 0.5
        segment_delay_max = 2.0
        try:
            from src.app.plugin_system.api.config_api import get_config
            nfc_config = get_config("neo_fatum_chatter")
            if nfc_config:
                segment_delay_min = getattr(
                    getattr(nfc_config, "reply", None), "segment_delay_min", 0.5
                )
                segment_delay_max = getattr(
                    getattr(nfc_config, "reply", None), "segment_delay_max", 2.0
                )
        except Exception:
            pass

        segment_delay_min = max(0.0, float(segment_delay_min))
        segment_delay_max = max(segment_delay_min, float(segment_delay_max))

        sent_count = 0
        for segment in segments:
            # 段间延迟：非首条消息前等待随机时间
            if sent_count > 0 and segment_delay_max > 0:
                delay = _random.uniform(segment_delay_min, segment_delay_max)
                await asyncio.sleep(delay)
            # 最后防线：剥离 thinking 块泄漏（DeepSeek V4 Pro Thinking 等模型偶尔会
            # 把 <think>/<thinking> 块直接吐到正文里）
            cleaned_segment = strip_thinking_blocks(segment)
            if cleaned_segment != segment:
                logger.warning(
                    f"[最后防线] 检测到 content 中混入 thinking 块，已剥离。"
                    f"原始长度={len(segment)}，剥离后={len(cleaned_segment)}"
                )
                segment = cleaned_segment
                if not segment:
                    continue

            keyword_matches = [p.search(segment) for p in _METADATA_PATTERNS]
            hit_count = sum(1 for m in keyword_matches if m is not None)
            if hit_count >= 2:
                earliest = min(m.start() for m in keyword_matches if m is not None)
                cleaned = segment[:earliest].strip()
                logger.warning(
                    f"[最后防线] 检测到 content 中混入 {hit_count} 个元数据关键字，已截断。"
                    f"原始长度={len(segment)}，截断后={len(cleaned)}"
                )
                segment = cleaned
                if not segment:
                    continue

            yield None
            if reply_to and sent_count == 0:
                success = await send_text(
                    content=segment,
                    stream_id=self.chat_stream.stream_id,
                    reply_to=reply_to,
                )
            else:
                success = await self._send_to_stream(segment)
            if not success:
                logger.warning(
                    f"消息发送失败: stream={self.chat_stream.stream_id[:8]} "
                    f"segment={segment[:50]}{'...' if len(segment) > 50 else ''}"
                )
                yield False, "消息发送失败"
                return
            sent_count += 1
            logger.info(
                f"消息已发送: stream={self.chat_stream.stream_id[:8]} "
                f"({sent_count}/{len(segments)}) "
                f"{segment[:60]}{'...' if len(segment) > 60 else ''}"
            )

        if sent_count <= 0:
            yield False, "清洗后内容为空，未发送"
            return
        yield True, f"已发送 {sent_count} 条消息"


