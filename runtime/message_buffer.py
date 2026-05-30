"""NFC 消息积累窗口运行时。"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..chatter import NeoFatumChatter
    from ..config import NFCConfig


async def accumulate_message_buffer(
    chatter: NeoFatumChatter,
    config: NFCConfig,
) -> tuple[str, list[Any]]:
    """在积累窗口内等待并聚合连发消息。"""
    window = max(0.0, float(config.buffer.accumulate_window))
    max_window = max(window, float(config.buffer.accumulate_max_window))

    if window <= 0:
        formatted_text, messages = await chatter.fetch_unreads(time_format="%Y-%m-%d %H:%M:%S")
        messages = dedupe_messages_by_id(messages)
        return _format_messages(chatter, messages, formatted_text)

    deadline = time.monotonic() + max_window
    last_count = 0

    while True:
        _, current_msgs = await chatter.fetch_unreads(time_format="%Y-%m-%d %H:%M:%S")
        current_count = len(dedupe_messages_by_id(current_msgs))

        if current_count > last_count:
            last_count = current_count
            next_check = time.monotonic() + window
        else:
            next_check = time.monotonic()

        remaining = min(deadline, next_check) - time.monotonic()
        if remaining <= 0:
            break

        await asyncio.sleep(min(0.2, remaining))

    formatted_text, messages = await chatter.fetch_unreads(time_format="%Y-%m-%d %H:%M:%S")
    messages = dedupe_messages_by_id(messages)
    return _format_messages(chatter, messages, formatted_text)


def dedupe_messages_by_id(messages: list[Any]) -> list[Any]:
    """按 message_id 去重，保留首次出现的消息。"""
    seen: set[str] = set()
    deduped: list[Any] = []
    for message in messages:
        message_id = getattr(message, "message_id", None)
        if message_id:
            key = str(message_id)
            if key in seen:
                continue
            seen.add(key)
        deduped.append(message)
    return deduped


def _format_messages(
    chatter: NeoFatumChatter,
    messages: list[Any],
    fallback_text: str,
) -> tuple[str, list[Any]]:
    """用去重后的消息重建未读文本。"""
    if not messages:
        return "", []
    if len(messages) == 1 and fallback_text:
        return fallback_text, messages
    return "\n".join(chatter.format_message_line(message) for message in messages), messages
