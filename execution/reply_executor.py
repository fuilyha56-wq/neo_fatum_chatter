"""NFC 回复执行器。

把回复的"段落规整 → 元数据/thinking 清洗 → 段间延迟 → 实际发送"
集中到这一处，让 ``actions/reply.py`` 退化为薄壳。

设计要点：
    - ``coerce_content_segments`` 与 ``sanitize_segment`` 是纯函数，便于单元测试。
    - 真正的 IO（``send_text`` / ``self._send_to_stream``）由 ``send_reply_segments``
      统一调用，并在失败处快速返回避免后续段落继续抛出。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any, Awaitable, Callable

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text

from ..protocol.response_normalizer import strip_thinking_blocks

logger = get_logger("NFC_reply_exec")

# 元数据关键字模式（最后防线）。
# 仅当多个元数据关键字同时出现时才判定为泄漏，降低误伤概率。
_METADATA_KEYWORDS: tuple[str, ...] = (
    r"(?:想法|内心想法|思考|thought|thinking)\s*[:：]",
    r"(?:预计反应|预期反应|expected_reaction)\s*[:：]",
    r"(?:最大等待秒数|max_wait_seconds)\s*[:：]",
    r"(?:心情|情绪|mood)\s*[:：]",
)
_METADATA_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(kw, re.IGNORECASE) for kw in _METADATA_KEYWORDS
)

# 触发"最后防线"截断的元数据关键字命中阈值。
METADATA_LEAK_THRESHOLD: int = 2


def coerce_content_segments(content: list[str] | str | None) -> list[str]:
    """把模型传来的 content 统一规整成可发送文本段落。

    有些模型会把 ``content`` 错传成 JSON 字符串，例如 ``["在呢。"]``。
    如果不先解析，就会把方括号和引号原样发出去。
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


def sanitize_segment(segment: str) -> tuple[str, bool, bool]:
    """对单条回复段做"最后防线"清洗。

    Returns:
        ``(cleaned, stripped_thinking, stripped_metadata)``。
        ``cleaned`` 可能为空字符串，调用方据此判断是否跳过本段。
    """
    if not segment:
        return "", False, False

    stripped_thinking = False
    cleaned = strip_thinking_blocks(segment)
    if cleaned != segment:
        logger.warning(
            f"[最后防线] 检测到 content 中混入 thinking 块，已剥离。"
            f"原始长度={len(segment)}，剥离后={len(cleaned)}"
        )
        stripped_thinking = True
        if not cleaned:
            return "", stripped_thinking, False

    stripped_metadata = False
    keyword_matches = [p.search(cleaned) for p in _METADATA_PATTERNS]
    hit_count = sum(1 for m in keyword_matches if m is not None)
    if hit_count >= METADATA_LEAK_THRESHOLD:
        earliest = min(m.start() for m in keyword_matches if m is not None)
        truncated = cleaned[:earliest].strip()
        logger.warning(
            f"[最后防线] 检测到 content 中混入 {hit_count} 个元数据关键字，已截断。"
            f"原始长度={len(cleaned)}，截断后={len(truncated)}"
        )
        cleaned = truncated
        stripped_metadata = True

    return cleaned, stripped_thinking, stripped_metadata


async def _send_reply_to_stream(text: str, stream_id: str, reply_to: str) -> bool:
    """发送引用回复文本。"""
    return await send_text(content=text, stream_id=stream_id, reply_to=reply_to)


async def send_reply_segments(
    segments: list[str],
    *,
    stream_id: str,
    reply_to: str,
    send_segment: Callable[[str], Awaitable[bool]],
    segment_delay_min: float,
    segment_delay_max: float,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    yield_point: Callable[[], Awaitable[None]] | None = None,
    send_reply_to_segment: Callable[[str, str, str], Awaitable[bool]] | None = None,
) -> tuple[list[str], bool]:
    """串行发送已经清洗过的段落。

    Args:
        segments: 已通过 ``sanitize_segment`` 清洗的段落。
        stream_id: 当前聊天流 ID（用于带 ``reply_to`` 的首条消息）。
        reply_to: 引用的消息 ID；非空时仅作用于第一条段落。
        send_segment: 用于发送单条非引用段落的回调（通常是 action 的
            ``self._send_to_stream``，由调用方传入）。
        segment_delay_min/max: 段间延迟范围（秒）。
        sleeper: 注入的 sleep，便于测试覆盖。
        yield_point: 每次发送前调用的可选钩子（保留给标准 tool 调度器
            的 ``yield None`` 暂停点）。

    Returns:
        ``(sent_segments, all_ok)``。一旦某段失败立即返回，``all_ok=False``。
    """
    sent: list[str] = []
    if not segments:
        return sent, True

    delay_min = max(0.0, float(segment_delay_min))
    delay_max = max(delay_min, float(segment_delay_max))

    for index, segment in enumerate(segments):
        if index > 0 and delay_max > 0:
            await sleeper(random.uniform(delay_min, delay_max))

        if yield_point is not None:
            await yield_point()

        if reply_to and index == 0:
            reply_sender = send_reply_to_segment or _send_reply_to_stream
            success = await reply_sender(segment, stream_id, reply_to)
        else:
            success = await send_segment(segment)

        if not success:
            logger.warning(
                f"消息发送失败: stream={stream_id[:8]} "
                f"segment={segment[:50]}{'...' if len(segment) > 50 else ''}"
            )
            return sent, False

        sent.append(segment)
        logger.info(
            f"消息已发送: stream={stream_id[:8]} "
            f"({len(sent)}/{len(segments)}) "
            f"{segment[:60]}{'...' if len(segment) > 60 else ''}"
        )

    return sent, True
