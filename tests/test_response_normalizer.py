"""response_normalizer 测试。"""

from __future__ import annotations

from neo_fatum_chatter.protocol.response_normalizer import (
    resolve_response_text,
    strip_thinking_blocks,
)


class _FakeResponse:
    def __init__(self, message=None, reasoning_content=None, call_list=None, model_set=None):
        self.message = message
        self.reasoning_content = reasoning_content
        self.call_list = call_list
        self.model_set = model_set


def test_strip_paired_think():
    text = "Hello <think>internal</think> world"
    assert strip_thinking_blocks(text) == "Hello  world"


def test_strip_orphan_end_think():
    text = "junk </think> visible"
    assert strip_thinking_blocks(text) == "visible"


def test_strip_orphan_start_think():
    text = "visible <think> truncated reasoning"
    # 仅有 start 标签，应剥离 <think> 起到末尾
    assert strip_thinking_blocks(text) == "visible"


def test_strip_thinking_caps_sensitive_to_case():
    assert "Reasoning" not in strip_thinking_blocks("<THINKING>Reasoning</THINKING>tail")


def test_strip_handles_empty():
    assert strip_thinking_blocks("") == ""
    assert strip_thinking_blocks(None) == ""


def test_resolve_falls_back_to_reasoning_when_message_empty():
    resp = _FakeResponse(message="", reasoning_content="actual answer")
    text, used_reasoning = resolve_response_text(resp)
    assert text == "actual answer"
    assert used_reasoning is True


def test_resolve_strips_thinking_in_message():
    resp = _FakeResponse(message="prefix <think>x</think> tail")
    text, used_reasoning = resolve_response_text(resp)
    assert "<think>" not in text
    assert "tail" in text
    assert used_reasoning is False


def test_resolve_returns_empty_when_no_text():
    resp = _FakeResponse(message=None, reasoning_content=None)
    text, used_reasoning = resolve_response_text(resp)
    assert text == ""
    assert used_reasoning is False
