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
import inspect
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
        elif isinstance(item, dict):
            # 有些模型把段落错传成 {"text": "..."} / {"content": "..."} 形式。
            # 直接 str(dict) 会把 "{'text': '...'}" 原样发出，这里优先提取常见文本键。
            raw_text = item.get("text") or item.get("content") or ""
            text = str(raw_text).strip() if raw_text else ""
        else:
            text = str(item).strip()
        if not text:
            continue
        # 按双换行拆分：模型用空行表达"这是两条独立消息"
        for part in re.split(r"\n\n+", text):
            part = part.strip()
            if part:
                segments.append(part)
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




def _resolve_streaming_service(signature: str = "") -> Any | None:
    """按配置签名或能力发现支持 ``start_streaming`` 的 Service。"""
    try:
        from src.app.plugin_system.api.service_api import get_all_services, get_service

        if signature:
            service = get_service(signature)
            if service is not None and callable(getattr(service, "start_streaming", None)):
                return service
            logger.warning(f"指定流式 Service 不可用或不支持 start_streaming: {signature}")
            return None

        for candidate_signature, service_cls in get_all_services().items():
            if not callable(getattr(service_cls, "start_streaming", None)):
                continue
            service = get_service(candidate_signature)
            if service is not None and callable(getattr(service, "start_streaming", None)):
                return service
    except Exception:
        logger.debug("流式 Service 发现失败", exc_info=True)
    return None


def extract_streaming_context(trigger_msg: Any | None) -> dict[str, str] | None:
    """从触发消息提取 QQBot C2C 流式发送上下文。"""
    if trigger_msg is None:
        return None

    platform = str(getattr(trigger_msg, "platform", "") or "").lower()
    if platform != "qqbot":
        return None

    chat_type = str(getattr(trigger_msg, "chat_type", "") or "").lower()
    if chat_type == "group":
        return None

    extra = getattr(trigger_msg, "extra", None)
    if not isinstance(extra, dict):
        extra = {}

    user_openid = str(
        extra.get("qq_user_openid") or getattr(trigger_msg, "sender_id", "") or ""
    )
    if not user_openid:
        return None

    event_id = str(extra.get("qq_event_id") or "")
    msg_id = event_id or str(getattr(trigger_msg, "message_id", "") or "")
    return {
        "user_openid": user_openid,
        "event_id": event_id,
        "msg_id": msg_id,
    }


async def _maybe_call(value: Any) -> Any:
    """兼容同步/异步测试替身与真实异步 controller 方法。"""
    if inspect.isawaitable(value):
        return await value
    return value


async def _send_streaming_segment(
    segment: str,
    *,
    trigger_msg: Any | None,
    streaming_chunk_size: int,
    streaming_interval: float,
    sleeper: Callable[[float], Awaitable[None]],
    streaming_service_getter: Callable[[str], Any | None] | None,
    streaming_service_signature: str,
) -> bool:
    """尝试使用外部流式 Service 发送单段文本。"""
    context = extract_streaming_context(trigger_msg)
    if context is None:
        return False

    service_getter = streaming_service_getter or _resolve_streaming_service
    service = service_getter(streaming_service_signature)
    if service is None:
        logger.debug("未发现可用流式 Service，降级普通发送")
        return False

    chunk_size = max(1, int(streaming_chunk_size))
    interval = max(0.0, float(streaming_interval))
    initial_text = segment[:chunk_size]

    try:
        result = await service.start_streaming(
            user_openid=context["user_openid"],
            initial_text=initial_text,
            event_id=context["event_id"],
            msg_id=context["msg_id"],
        )
    except Exception:
        logger.warning("调用流式 Service 启动失败，降级普通发送", exc_info=True)
        return False

    if not isinstance(result, dict) or not result.get("success"):
        logger.debug(f"流式 Service 未启动，降级普通发送: {result}")
        return False

    controller = result.get("controller")
    if controller is None:
        logger.debug("流式 Service 未返回 controller，降级普通发送")
        return False

    try:
        for end in range(chunk_size * 2, len(segment) + chunk_size, chunk_size):
            current_text = segment[:min(end, len(segment))]
            if current_text == initial_text:
                continue
            if interval > 0:
                await sleeper(interval)
            await _maybe_call(controller.update(current_text))
        await _maybe_call(controller.end(segment))
        return True
    except Exception:
        logger.warning("流式发送更新失败，尝试结束流式消息", exc_info=True)
        try:
            await _maybe_call(controller.end(segment))
            return True
        except Exception:
            logger.warning("流式消息结束失败，降级普通发送", exc_info=True)
            return False


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
    streaming_enabled: bool = False,
    streaming_service_signature: str = "",
    streaming_chunk_size: int = 10,
    streaming_interval: float = 0.1,
    trigger_msg: Any | None = None,
    streaming_service_getter: Callable[[str], Any | None] | None = None,
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
        elif streaming_enabled:
            success = await _send_streaming_segment(
                segment,
                trigger_msg=trigger_msg,
                streaming_chunk_size=streaming_chunk_size,
                streaming_interval=streaming_interval,
                sleeper=sleeper,
                streaming_service_getter=streaming_service_getter,
                streaming_service_signature=streaming_service_signature,
            )
            if not success:
                success = await send_segment(segment)
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
