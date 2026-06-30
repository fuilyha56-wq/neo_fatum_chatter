"""context_sanitizer 测试：payload 链清洗常见路径。"""

from __future__ import annotations

from neo_fatum_chatter.services.context_sanitizer import (
    close_pending_tool_chain,
    sanitize_payload_chain,
)
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolResult


class _FakeResponse:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def add_payload(self, payload, position=None):
        if position is None:
            self.payloads.append(payload)
        else:
            self.payloads.insert(position, payload)


def _user(text):
    return LLMPayload(ROLE.USER, [Text(text)])


def _assistant(content):
    return LLMPayload(ROLE.ASSISTANT, content)


def _tool_result(call_id, text):
    return LLMPayload(ROLE.TOOL_RESULT, [ToolResult(value=text, call_id=call_id, name="t")])


def test_close_pending_tool_chain_appends_bridge_when_tail_is_tool_result():
    resp = _FakeResponse(
        [
            _user("hi"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _tool_result("c1", "ok"),
        ]
    )
    closed = close_pending_tool_chain(resp, reason="test")
    assert closed is True
    assert resp.payloads[-1].role == ROLE.ASSISTANT


def test_close_pending_tool_chain_noop_when_tail_is_user():
    resp = _FakeResponse([_user("hi")])
    assert close_pending_tool_chain(resp, reason="test") is False


def test_sanitize_drops_orphan_assistant_before_first_user():
    resp = _FakeResponse(
        [
            _assistant([Text("orphan")]),
            _user("hello"),
        ]
    )
    changed = sanitize_payload_chain(resp, reason="test")
    assert changed is True
    assert resp.payloads[0].role == ROLE.USER


def test_sanitize_merges_consecutive_assistants():
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([Text("first")]),
            _assistant([Text("second")]),
        ]
    )
    sanitize_payload_chain(resp, reason="test")
    assistant_count = sum(1 for p in resp.payloads if p.role == ROLE.ASSISTANT)
    # 两个普通 assistant 合并为一个
    assert assistant_count == 1


def test_sanitize_inserts_user_bridge_between_tool_result_and_user():
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _tool_result("c1", "ok"),
            _user("next"),
        ]
    )
    sanitize_payload_chain(resp, reason="test")
    # tool_result 后必须先有 assistant 才能跟 user
    roles = [p.role for p in resp.payloads]
    tool_idx = roles.index(ROLE.TOOL_RESULT)
    assert roles[tool_idx + 1] == ROLE.ASSISTANT


def test_sanitize_drops_empty_tool_result():
    # tool_result payload 没有有效 ToolResult 内容应被丢弃
    empty_tr = LLMPayload(ROLE.TOOL_RESULT, [])
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            empty_tr,
        ]
    )
    sanitize_payload_chain(resp, reason="test")
    assert all(p.role != ROLE.TOOL_RESULT for p in resp.payloads)
