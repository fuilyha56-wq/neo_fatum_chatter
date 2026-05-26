"""reply_executor 段落规整与清洗测试。"""

from __future__ import annotations

import asyncio

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
