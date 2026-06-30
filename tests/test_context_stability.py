"""NFC 上下文稳态工具测试。"""

from __future__ import annotations

from types import SimpleNamespace

from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolResult

from neo_fatum_chatter.context.renderer import ContextRenderer
from neo_fatum_chatter.runtime.request_view import _without_transient_payloads
from neo_fatum_chatter.services.context_sanitizer import heal_orphan_tool_results


def _payload_texts(payloads: list[LLMPayload]) -> list[str]:
    """提取 payload 中 Text 内容。"""
    texts: list[str] = []
    for payload in payloads:
        for part in payload.content:
            if isinstance(part, Text):
                texts.append(part.text)
    return texts


def test_without_transient_payloads_removes_extra_user_keeps_assistant() -> None:
    """RequestView 回写时移除临时 USER，并保留 assistant 响应。"""
    source = [LLMPayload(ROLE.USER, Text("base"))]
    payloads = [
        LLMPayload(ROLE.USER, Text("base")),
        LLMPayload(ROLE.USER, Text("extra")),
        LLMPayload(ROLE.ASSISTANT, Text("reply")),
    ]

    result = _without_transient_payloads(
        payloads,
        source_payloads=source,
        transient_count=1,
    )

    assert [payload.role for payload in result] == [ROLE.USER, ROLE.ASSISTANT]
    assert _payload_texts(result) == ["base", "reply"]


def test_without_transient_payloads_restores_source_user_payload() -> None:
    """RequestView 回写时恢复被 reminder 改写的原始 USER。"""
    source_user = LLMPayload(ROLE.USER, Text("base"))
    payloads = [
        LLMPayload(ROLE.USER, Text("reminder\nbase")),
        LLMPayload(ROLE.ASSISTANT, Text("reply")),
    ]

    result = _without_transient_payloads(
        payloads,
        source_payloads=[source_user],
        transient_count=0,
    )

    assert result[0] is source_user
    assert _payload_texts(result) == ["base", "reply"]


def test_heal_orphan_tool_result_after_user() -> None:
    """删除 USER 后面的孤立 tool_result。"""
    response = SimpleNamespace(payloads=[
        LLMPayload(ROLE.USER, Text("u")),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value="r", call_id="call_1")),
    ])

    assert heal_orphan_tool_results(response, where="test") is True
    assert [payload.role for payload in response.payloads] == [ROLE.USER]


def test_heal_orphan_tool_result_after_plain_assistant() -> None:
    """删除普通 assistant 后面的孤立 tool_result。"""
    response = SimpleNamespace(payloads=[
        LLMPayload(ROLE.USER, Text("u")),
        LLMPayload(ROLE.ASSISTANT, Text("a")),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value="r", call_id="call_1")),
    ])

    assert heal_orphan_tool_results(response, where="test") is True
    assert [payload.role for payload in response.payloads] == [ROLE.USER, ROLE.ASSISTANT]


def test_heal_keeps_tool_result_before_user() -> None:
    """保留框架允许的 assistant(tool_calls) -> tool_result -> user。"""
    assistant = LLMPayload(
        ROLE.ASSISTANT,
        ToolCall(id="call_1", name="tool", args={}),
    )
    response = SimpleNamespace(payloads=[
        LLMPayload(ROLE.USER, Text("u")),
        assistant,
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value="r", call_id="call_1")),
        LLMPayload(ROLE.USER, Text("next")),
    ])

    assert heal_orphan_tool_results(response, where="test") is False
    assert [payload.role for payload in response.payloads] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.USER,
    ]


def test_heal_keeps_tool_result_before_assistant() -> None:
    """保留框架允许的 assistant(tool_calls) -> tool_result -> assistant。"""
    response = SimpleNamespace(payloads=[
        LLMPayload(ROLE.USER, Text("u")),
        LLMPayload(ROLE.ASSISTANT, ToolCall(id="call_1", name="tool", args={})),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value="r", call_id="call_1")),
        LLMPayload(ROLE.ASSISTANT, Text("ok")),
    ])

    assert heal_orphan_tool_results(response, where="test") is False
    assert [payload.role for payload in response.payloads] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.ASSISTANT,
    ]


def test_heal_removes_mismatched_tool_result_and_unclosed_call() -> None:
    """清理 call_id 不匹配导致的未闭合工具链。"""
    response = SimpleNamespace(payloads=[
        LLMPayload(ROLE.USER, Text("u")),
        LLMPayload(ROLE.ASSISTANT, ToolCall(id="call_1", name="tool", args={})),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value="r", call_id="call_2")),
        LLMPayload(ROLE.USER, Text("next")),
    ])

    assert heal_orphan_tool_results(response, where="test") is True
    assert [payload.role for payload in response.payloads] == [ROLE.USER, ROLE.USER]


def test_heal_removes_duplicate_tool_result() -> None:
    """删除重复 call_id 的 tool_result。"""
    response = SimpleNamespace(payloads=[
        LLMPayload(ROLE.USER, Text("u")),
        LLMPayload(ROLE.ASSISTANT, ToolCall(id="call_1", name="tool", args={})),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value="r1", call_id="call_1")),
        LLMPayload(ROLE.TOOL_RESULT, ToolResult(value="r2", call_id="call_1")),
    ])

    assert heal_orphan_tool_results(response, where="test") is True
    assert [payload.role for payload in response.payloads] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
    ]


def test_heal_preserves_tail_unclosed_tool_calls() -> None:
    """尾部未闭合 assistant.tool_calls 保留——本轮尚未执行，等待 run_tool_call 回写。"""
    response = SimpleNamespace(payloads=[
        LLMPayload(ROLE.USER, Text("u")),
        LLMPayload(ROLE.ASSISTANT, ToolCall(id="call_1", name="tool", args={})),
    ])

    assert heal_orphan_tool_results(response, where="post-send") is False
    assert [payload.role for payload in response.payloads] == [
        ROLE.USER,
        ROLE.ASSISTANT,
    ]


def test_heal_removes_mid_chain_unclosed_tool_calls() -> None:
    """链中间未闭合 assistant.tool_calls 删除——历史遗留，后续已有其他 payload。"""
    response = SimpleNamespace(payloads=[
        LLMPayload(ROLE.USER, Text("u")),
        LLMPayload(ROLE.ASSISTANT, ToolCall(id="call_1", name="tool", args={})),
        LLMPayload(ROLE.USER, Text("next")),
    ])

    assert heal_orphan_tool_results(response, where="test") is True
    assert [payload.role for payload in response.payloads] == [ROLE.USER, ROLE.USER]


def test_limit_serialized_chain_keeps_tail_starting_user() -> None:
    """initial chain 控量后首条仍为 user。"""
    entries = [
        {"role": "assistant", "text": "a0"},
        {"role": "user", "text": "u1"},
        {"role": "assistant", "text": "a1"},
        {"role": "user", "text": "u2"},
        {"role": "assistant", "text": "a2"},
    ]

    limited = ContextRenderer._limit_serialized_chain(entries, max_payloads=3)

    assert [entry["role"] for entry in limited] == ["user", "assistant"]
    assert limited[0]["text"] == "u2"


def test_limit_fused_narrative_keeps_recent_tail() -> None:
    """融合叙事过长时只保留最近部分。"""
    text = "old line\n" + "x" * 30 + "\nrecent line"

    limited = ContextRenderer._limit_fused_narrative(text, max_chars=20)

    assert limited.startswith("（较早的融合叙事已省略，仅保留最近部分）")
    assert "recent line" in limited
    assert "old line" not in limited
