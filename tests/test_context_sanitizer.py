"""context_sanitizer 测试：payload 链清洗常见路径。"""

from __future__ import annotations

from neo_fatum_chatter.services.context_sanitizer import (
    append_suspend_payload_if_tool_result_tail,
    prepare_payload_chain_for_send,
    sanitize_payload_chain,
)
from neo_fatum_chatter.services.timeout_service import TimeoutService
from src.kernel.llm import LLMContextManager, LLMPayload, ROLE, Text, ToolCall, ToolResult


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


def test_append_suspend_payload_when_tail_is_tool_result():
    resp = _FakeResponse(
        [
            _user("hi"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _tool_result("c1", "ok"),
        ]
    )
    closed = append_suspend_payload_if_tool_result_tail(resp, reason="test")
    assert closed is True
    assert resp.payloads[-1].role == ROLE.ASSISTANT
    assert resp.payloads[-1].content == [Text("__SUSPEND__")]


def test_prepare_keeps_tool_result_tail_for_normal_followup():
    """正常工具续轮应直接把尾部结果交给下一次模型请求。"""
    resp = _FakeResponse(
        [
            _user("hi"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _tool_result("c1", "ok"),
        ]
    )
    original_payloads = list(resp.payloads)

    assert prepare_payload_chain_for_send(resp, reason="normal followup") is False
    assert resp.payloads == original_payloads
    assert resp.payloads[-1].role == ROLE.TOOL_RESULT


def test_append_suspend_payload_noop_when_tail_is_user():
    resp = _FakeResponse([_user("hi")])
    assert append_suspend_payload_if_tool_result_tail(resp, reason="test") is False


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


def test_sanitize_preserves_valid_tool_result_to_user_sequence():
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _tool_result("c1", "ok"),
            _user("next"),
        ]
    )
    original_payloads = list(resp.payloads)

    assert prepare_payload_chain_for_send(resp, reason="normal tool followup") is False
    assert resp.payloads == original_payloads
    assert [payload.role for payload in resp.payloads] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.USER,
    ]


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


def test_prepare_sanitizes_roles_without_closing_tool_result_tail():
    """普通发送前仅修复非法遗留角色，不闭合合法工具结果尾态。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _assistant([Text("orphan")]),
            _user("hello"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _tool_result("c1", "ok"),
        ]
    )

    def add_payload(payload, position=None):
        resp.payloads = context_manager.add_payload(
            resp.payloads,
            payload,
            position=position,
        )

    resp.add_payload = add_payload

    assert prepare_payload_chain_for_send(resp, reason="test") is True
    context_manager.validate_for_send(resp.payloads)
    assert [payload.role for payload in resp.payloads] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
    ]


def test_prepare_repairs_partially_completed_tool_group_before_closing():
    """部分工具缺少结果时，应只保留已有完整结果的调用再闭合。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _user("hello"),
            _assistant(
                [
                    ToolCall(id="done", name="tool-a", args={}),
                    ToolCall(id="missing", name="tool-b", args={}),
                ]
            ),
            _tool_result("done", "ok"),
        ]
    )

    def add_payload(payload, position=None):
        resp.payloads = context_manager.add_payload(
            resp.payloads,
            payload,
            position=position,
        )

    resp.add_payload = add_payload

    assert prepare_payload_chain_for_send(resp, reason="test") is True
    context_manager.validate_for_send(resp.payloads)
    retained_calls = [
        part
        for part in resp.payloads[1].content
        if isinstance(part, ToolCall)
    ]
    assert [call.id for call in retained_calls] == ["done"]


def test_timeout_entry_sanitizes_before_appending_suspend_payload():
    """超时恢复需先处理旧链，再追加非空挂起标记。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _assistant([Text("orphan")]),
            _user("hello"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _tool_result("c1", "ok"),
        ]
    )

    def add_payload(payload, position=None):
        resp.payloads = context_manager.add_payload(
            resp.payloads,
            payload,
            position=position,
        )

    resp.add_payload = add_payload

    TimeoutService._append_suspend_payload(resp)
    context_manager.validate_for_send(resp.payloads)
    assert resp.payloads[0].role == ROLE.USER
    assert resp.payloads[-1].role == ROLE.ASSISTANT
    assert resp.payloads[-1].content == [Text("__SUSPEND__")]


# ── 配对追踪清洗（bugfix: assistant 前必须是 user 或 tool_result）────────


def _assert_validator_clean(payloads) -> None:
    """最终链必须通过框架严格校验（发送态，不允许未闭合尾）。"""
    LLMContextManager().validate_for_send(list(payloads))


def test_sanitize_repairs_adjacent_assistants_with_tool_calls():
    """相邻 assistant（前一个的 tool_result 丢失）必须修复为合法链。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _assistant([ToolCall(id="c2", name="tool-y", args={})]),
            _tool_result("c2", "ok"),
        ]
    )
    sanitize_payload_chain(resp, reason="test")
    _assert_validator_clean(resp.payloads)
    assert [p.role for p in resp.payloads] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
    ]


def test_sanitize_strips_unclosed_calls_before_next_user():
    """assistant 的调用未拿到结果就遇到下一条 user，应摘除调用声明。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _user("next"),
        ]
    )
    sanitize_payload_chain(resp, reason="test")
    _assert_validator_clean(resp.payloads)
    assert [p.role for p in resp.payloads] == [ROLE.USER, ROLE.USER]


def test_sanitize_drops_tool_result_after_plain_assistant():
    """纯文本 assistant 之后的 tool_result 是孤立的，必须丢弃。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([Text("plain")]),
            _tool_result("c9", "orphan"),
            _user("next"),
        ]
    )
    sanitize_payload_chain(resp, reason="test")
    _assert_validator_clean(resp.payloads)
    assert all(p.role != ROLE.TOOL_RESULT for p in resp.payloads)


def test_sanitize_drops_mismatched_tool_result_and_trailing_calls():
    """call_id 不匹配的结果被丢弃后，链尾未闭合调用也要摘除。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _tool_result("cX", "mismatch"),
        ]
    )
    sanitize_payload_chain(resp, reason="test")
    _assert_validator_clean(resp.payloads)
    assert all(p.role != ROLE.TOOL_RESULT for p in resp.payloads)


def test_sanitize_drops_duplicate_tool_result():
    """同一 call_id 的重复结果第二次出现必须丢弃。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _tool_result("c1", "first"),
            _tool_result("c1", "dup"),
        ]
    )
    sanitize_payload_chain(resp, reason="test")
    _assert_validator_clean(resp.payloads)
    tool_results = [p for p in resp.payloads if p.role == ROLE.TOOL_RESULT]
    assert len(tool_results) == 1


def test_sanitize_merges_text_assistant_after_call_assistant_without_results():
    """前一个 assistant 因丢结果被摘成纯文本后，与相邻 assistant 合并。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _user("u"),
            _assistant([Text("keep"), ToolCall(id="c1", name="tool-x", args={})]),
            _assistant([Text("trailing")]),
        ]
    )
    sanitize_payload_chain(resp, reason="test")
    _assert_validator_clean(resp.payloads)
    assistants = [p for p in resp.payloads if p.role == ROLE.ASSISTANT]
    assert len(assistants) == 1
    texts = [part.text for part in assistants[0].content if isinstance(part, Text)]
    assert texts == ["keep", "trailing"]


def test_prepare_full_corruption_chain_becomes_validator_clean():
    """综合损坏链（孤立 assistant + 相邻 assistant + 孤立结果）一次清洗到位。"""
    context_manager = LLMContextManager()
    resp = _FakeResponse(
        [
            _assistant([Text("leading-orphan")]),
            _user("u"),
            _assistant([ToolCall(id="c1", name="tool-x", args={})]),
            _assistant([Text("crash after calls")]),
            _tool_result("cZ", "orphan"),
        ]
    )
    assert prepare_payload_chain_for_send(resp, reason="test") is True
    _assert_validator_clean(resp.payloads)
