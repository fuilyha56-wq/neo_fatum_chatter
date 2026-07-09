"""reply_executor 段落规整与清洗测试。"""

from __future__ import annotations

import asyncio

from types import SimpleNamespace

import pytest

from neo_fatum_chatter.execution.reply_executor import (
    coerce_content_segments,
    sanitize_segment,
    send_reply_segments,
)


def test_coerce_none_returns_empty():
    assert coerce_content_segments(None) == []


def test_coerce_empty_string():
    assert coerce_content_segments("") == []
    assert coerce_content_segments("   ") == []


def test_coerce_plain_string():
    assert coerce_content_segments("你好") == ["你好"]


def test_coerce_json_string_unwrap():
    assert coerce_content_segments('["在呢。", "等下"]') == ["在呢。", "等下"]


def test_coerce_invalid_json_kept_as_string():
    assert coerce_content_segments('[broken') == ['[broken']


def test_coerce_list_passes_through_with_strip():
    assert coerce_content_segments(["a", "  b ", "", None]) == ["a", "b", "None"]


def test_sanitize_strips_thinking_block():
    cleaned, stripped_thinking, stripped_meta = sanitize_segment("hi <think>x</think>")
    assert cleaned == "hi"
    assert stripped_thinking is True
    assert stripped_meta is False


def test_sanitize_truncates_metadata_leak():
    raw = "好的，我去做\n想法：要回应温柔\n预计反应：对方会感动"
    cleaned, _, stripped_meta = sanitize_segment(raw)
    assert "想法" not in cleaned
    assert stripped_meta is True


def test_sanitize_single_metadata_keyword_passes():
    # 仅一个关键字命中不应触发截断（阈值 = 2）
    cleaned, _, stripped_meta = sanitize_segment("我想了一下，想法：很有意思")
    assert stripped_meta is False
    assert "想法" in cleaned


@pytest.mark.asyncio
async def test_send_reply_segments_stops_on_failure():
    sent = []
    calls = {"count": 0}

    async def send_segment(text: str) -> bool:
        calls["count"] += 1
        if calls["count"] == 2:
            return False
        sent.append(text)
        return True

    async def fast_sleeper(_seconds: float) -> None:
        return None

    final_sent, ok = await send_reply_segments(
        ["a", "b", "c"],
        stream_id="abcdef12",
        reply_to="",
        send_segment=send_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        sleeper=fast_sleeper,
    )
    assert ok is False
    assert final_sent == ["a"]


@pytest.mark.asyncio
async def test_send_reply_segments_all_ok():
    async def send_segment(text: str) -> bool:
        return True

    async def fast_sleeper(_seconds: float) -> None:
        return None

    sent, ok = await send_reply_segments(
        ["a", "b"],
        stream_id="abcdef12",
        reply_to="",
        send_segment=send_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        sleeper=fast_sleeper,
    )
    assert ok is True
    assert sent == ["a", "b"]


@pytest.mark.asyncio
async def test_send_reply_segments_uses_injected_reply_sender_for_reply_to():
    calls = []

    async def send_segment(text: str) -> bool:
        raise AssertionError(f"普通发送不应处理引用首段: {text}")

    async def send_reply_to_segment(text: str, stream_id: str, reply_to: str) -> bool:
        calls.append((text, stream_id, reply_to))
        return True

    async def fast_sleeper(_seconds: float) -> None:
        return None

    sent, ok = await send_reply_segments(
        ["a"],
        stream_id="abcdef12",
        reply_to="m1",
        send_segment=send_segment,
        send_reply_to_segment=send_reply_to_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        sleeper=fast_sleeper,
    )

    assert ok is True
    assert sent == ["a"]
    assert calls == [("a", "abcdef12", "m1")]


@pytest.mark.asyncio
async def test_send_reply_segments_empty_input():
    async def send_segment(text: str) -> bool:  # pragma: no cover
        raise AssertionError("不应被调用")

    sent, ok = await send_reply_segments(
        [],
        stream_id="abcdef12",
        reply_to="",
        send_segment=send_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        sleeper=asyncio.sleep,
    )
    assert ok is True
    assert sent == []


class FakeStreamingController:
    def __init__(self) -> None:
        self.updates = []
        self.ended_with = None

    async def update(self, text: str) -> None:
        self.updates.append(text)

    async def end(self, text: str) -> None:
        self.ended_with = text


class FakeStreamingService:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.controller = FakeStreamingController()
        self.calls = []

    async def start_streaming(
        self,
        *,
        user_openid: str,
        initial_text: str,
        event_id: str = "",
        msg_id: str = "",
    ):
        self.calls.append((user_openid, initial_text, event_id, msg_id))
        if not self.success:
            return {"success": False, "controller": None, "error": "boom"}
        return {"success": True, "controller": self.controller, "error": None}


def qqbot_trigger_msg():
    return SimpleNamespace(
        platform="qqbot",
        chat_type="private",
        sender_id="fallback-openid",
        message_id="message-id",
        extra={"qq_user_openid": "user-openid", "qq_event_id": "event-id"},
    )


@pytest.mark.asyncio
async def test_send_reply_segments_streaming_success_uses_service():
    service = FakeStreamingService()
    ordinary_sent = []

    async def send_segment(text: str) -> bool:
        ordinary_sent.append(text)
        return True

    async def fast_sleeper(_seconds: float) -> None:
        return None

    sent, ok = await send_reply_segments(
        ["abcdef"],
        stream_id="abcdef12",
        reply_to="",
        send_segment=send_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        sleeper=fast_sleeper,
        streaming_enabled=True,
        streaming_chunk_size=2,
        streaming_interval=0.0,
        trigger_msg=qqbot_trigger_msg(),
        streaming_service_getter=lambda _signature: service,
    )

    assert ok is True
    assert sent == ["abcdef"]
    assert ordinary_sent == []
    assert service.calls == [("user-openid", "ab", "event-id", "event-id")]
    assert service.controller.updates == ["abcd", "abcdef"]
    assert service.controller.ended_with == "abcdef"


@pytest.mark.asyncio
async def test_send_reply_segments_streaming_non_qqbot_falls_back():
    service = FakeStreamingService()
    ordinary_sent = []

    async def send_segment(text: str) -> bool:
        ordinary_sent.append(text)
        return True

    sent, ok = await send_reply_segments(
        ["hello"],
        stream_id="abcdef12",
        reply_to="",
        send_segment=send_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        streaming_enabled=True,
        trigger_msg=SimpleNamespace(platform="onebot", chat_type="private", extra={}),
        streaming_service_getter=lambda _signature: service,
    )

    assert ok is True
    assert sent == ["hello"]
    assert ordinary_sent == ["hello"]
    assert service.calls == []


@pytest.mark.asyncio
async def test_send_reply_segments_streaming_start_failure_falls_back():
    service = FakeStreamingService(success=False)
    ordinary_sent = []

    async def send_segment(text: str) -> bool:
        ordinary_sent.append(text)
        return True

    sent, ok = await send_reply_segments(
        ["hello"],
        stream_id="abcdef12",
        reply_to="",
        send_segment=send_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        streaming_enabled=True,
        trigger_msg=qqbot_trigger_msg(),
        streaming_service_getter=lambda _signature: service,
    )

    assert ok is True
    assert sent == ["hello"]
    assert ordinary_sent == ["hello"]


@pytest.mark.asyncio
async def test_send_reply_segments_reply_to_skips_streaming():
    service = FakeStreamingService()
    calls = []

    async def send_segment(text: str) -> bool:
        raise AssertionError(f"普通发送不应处理引用首段: {text}")

    async def send_reply_to_segment(text: str, stream_id: str, reply_to: str) -> bool:
        calls.append((text, stream_id, reply_to))
        return True

    sent, ok = await send_reply_segments(
        ["hello"],
        stream_id="abcdef12",
        reply_to="m1",
        send_segment=send_segment,
        send_reply_to_segment=send_reply_to_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        streaming_enabled=True,
        trigger_msg=qqbot_trigger_msg(),
        streaming_service_getter=lambda _signature: service,
    )

    assert ok is True
    assert sent == ["hello"]
    assert calls == [("hello", "abcdef12", "m1")]
    assert service.calls == []


@pytest.mark.asyncio
async def test_send_reply_segments_streaming_normalizes_chunk_and_interval():
    service = FakeStreamingService()

    async def send_segment(text: str) -> bool:
        raise AssertionError(f"不应降级普通发送: {text}")

    async def fast_sleeper(_seconds: float) -> None:
        raise AssertionError("负 interval 应规整为 0，不应 sleep")

    sent, ok = await send_reply_segments(
        ["abc"],
        stream_id="abcdef12",
        reply_to="",
        send_segment=send_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        sleeper=fast_sleeper,
        streaming_enabled=True,
        streaming_chunk_size=0,
        streaming_interval=-1.0,
        trigger_msg=qqbot_trigger_msg(),
        streaming_service_getter=lambda _signature: service,
    )

    assert ok is True
    assert sent == ["abc"]
    assert service.calls == [("user-openid", "a", "event-id", "event-id")]
    assert service.controller.updates == ["ab", "abc"]
    assert service.controller.ended_with == "abc"


@pytest.mark.asyncio
async def test_send_reply_segments_passes_configured_service_signature():
    service = FakeStreamingService()
    signatures = []

    async def send_segment(text: str) -> bool:
        raise AssertionError(f"不应降级普通发送: {text}")

    def get_service(signature: str):
        signatures.append(signature)
        return service

    sent, ok = await send_reply_segments(
        ["abcd"],
        stream_id="abcdef12",
        reply_to="",
        send_segment=send_segment,
        segment_delay_min=0.0,
        segment_delay_max=0.0,
        streaming_enabled=True,
        streaming_service_signature="custom_plugin:service:streamer",
        streaming_chunk_size=2,
        streaming_interval=0.0,
        trigger_msg=qqbot_trigger_msg(),
        streaming_service_getter=get_service,
    )

    assert ok is True
    assert sent == ["abcd"]
    assert signatures == ["custom_plugin:service:streamer"]
