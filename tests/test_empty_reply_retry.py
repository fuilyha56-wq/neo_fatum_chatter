"""空回复打回重试逻辑测试。

覆盖 ``orchestrator._collect_empty_reply_call_ids`` 与
``orchestrator._purge_empty_reply_artifacts`` 两个纯函数，以及
``NFC_EMPTY_REPLY_RETRY_PROMPT`` 模板存在性校验。
"""

from __future__ import annotations

from types import SimpleNamespace

from neo_fatum_chatter.prompts.templates import NFC_EMPTY_REPLY_RETRY_PROMPT
from neo_fatum_chatter.runtime.orchestrator import (
    _collect_empty_reply_call_ids,
    _purge_empty_reply_artifacts,
)
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolResult


def _make_response(
    calls: list[ToolCall],
    tool_results: list[ToolResult] | None = None,
) -> SimpleNamespace:
    """构造一个包含 assistant tool_call 与 tool_result 的假 response。"""
    payloads: list[LLMPayload] = [
        LLMPayload(ROLE.USER, Text("你好")),
        LLMPayload(ROLE.ASSISTANT, list(calls)),
    ]
    if tool_results:
        payloads.append(LLMPayload(ROLE.TOOL_RESULT, list(tool_results)))
    return SimpleNamespace(
        payloads=payloads,
        call_list=list(calls),
        message="",
    )


class TestCollectEmptyReplyCallIds:
    """``_collect_empty_reply_call_ids`` 行为测试。"""

    def test_collects_empty_content_string(self) -> None:
        """content 为空字符串时收集对应 call_id。"""
        calls = [
            ToolCall(id="call-1", name="nfc_reply", args={"content": ""}),
        ]
        response = _make_response(calls)

        result = _collect_empty_reply_call_ids(response, decision=None)

        assert result == {"call-1"}

    def test_collects_empty_content_list(self) -> None:
        """content 为空列表时收集对应 call_id。"""
        calls = [
            ToolCall(id="call-2", name="nfc_reply", args={"content": []}),
        ]
        response = _make_response(calls)

        result = _collect_empty_reply_call_ids(response, decision=None)

        assert result == {"call-2"}

    def test_collects_whitespace_only_content(self) -> None:
        """content 仅含空白时收集对应 call_id。"""
        calls = [
            ToolCall(id="call-3", name="nfc_reply", args={"content": "   \n  "}),
        ]
        response = _make_response(calls)

        result = _collect_empty_reply_call_ids(response, decision=None)

        assert result == {"call-3"}

    def test_collects_none_content(self) -> None:
        """content 为 None 时收集对应 call_id。"""
        calls = [
            ToolCall(id="call-4", name="nfc_reply", args={}),
        ]
        response = _make_response(calls)

        result = _collect_empty_reply_call_ids(response, decision=None)

        assert result == {"call-4"}

    def test_collects_list_of_empty_strings(self) -> None:
        """content 列表中全是空字符串时收集对应 call_id。"""
        calls = [
            ToolCall(id="call-5", name="nfc_reply", args={"content": ["", "  ", None]}),
        ]
        response = _make_response(calls)

        result = _collect_empty_reply_call_ids(response, decision=None)

        assert result == {"call-5"}

    def test_skips_non_empty_content(self) -> None:
        """content 有有效文本时不收集。"""
        calls = [
            ToolCall(id="call-6", name="nfc_reply", args={"content": "你好啊"}),
        ]
        response = _make_response(calls)

        result = _collect_empty_reply_call_ids(response, decision=None)

        assert result == set()

    def test_skips_non_reply_calls(self) -> None:
        """非 nfc_reply 工具调用不收集。"""
        calls = [
            ToolCall(id="call-7", name="do_nothing", args={}),
        ]
        response = _make_response(calls)

        result = _collect_empty_reply_call_ids(response, decision=None)

        assert result == set()

    def test_handles_prefixed_call_names(self) -> None:
        """带前缀的 call name（action-nfc_reply）也能识别。"""
        calls = [
            ToolCall(id="call-8", name="action-nfc_reply", args={"content": ""}),
        ]
        response = _make_response(calls)

        result = _collect_empty_reply_call_ids(response, decision=None)

        assert result == {"call-8"}

    def test_mixed_empty_and_non_empty(self) -> None:
        """混合空与非空 content 时只收集空的。"""
        calls = [
            ToolCall(id="call-empty", name="nfc_reply", args={"content": ""}),
            ToolCall(id="call-valid", name="nfc_reply", args={"content": "你好"}),
        ]
        response = _make_response(calls)

        result = _collect_empty_reply_call_ids(response, decision=None)

        assert result == {"call-empty"}


class TestPurgeEmptyReplyArtifacts:
    """``_purge_empty_reply_artifacts`` 行为测试。"""

    def test_removes_tool_call_from_assistant_payload(self) -> None:
        """清理后 assistant payload 中不再包含空的 ToolCall。

        当 assistant payload 中只有空的 ToolCall 时，整个 payload 会被移除
       （_remove_failed_tool_calls 的标准行为）。
        """
        calls = [
            ToolCall(id="call-1", name="nfc_reply", args={"content": ""}),
        ]
        response = _make_response(calls)

        _purge_empty_reply_artifacts(response, {"call-1"})

        # 整个只含空 ToolCall 的 assistant payload 被移除
        assistant_payloads = [
            p for p in response.payloads if p.role == ROLE.ASSISTANT
        ]
        assert assistant_payloads == []

    def test_removes_tool_call_but_preserves_assistant_text(self) -> None:
        """assistant payload 中有 Text + 空 ToolCall 时，保留 Text 部分。"""
        from src.kernel.llm import Text as LLMText

        calls = [
            ToolCall(id="call-1", name="nfc_reply", args={"content": ""}),
        ]
        response = _make_response(calls)
        # 在 assistant payload 中额外加入 Text
        response.payloads[1].content.insert(0, LLMText("some reasoning"))

        _purge_empty_reply_artifacts(response, {"call-1"})

        assistant_payloads = [
            p for p in response.payloads if p.role == ROLE.ASSISTANT
        ]
        assert len(assistant_payloads) == 1
        remaining_calls = [
            p for p in assistant_payloads[0].content if isinstance(p, ToolCall)
        ]
        assert remaining_calls == []

    def test_removes_corresponding_tool_result(self) -> None:
        """清理后对应的 tool_result 也被移除。"""
        calls = [
            ToolCall(id="call-1", name="nfc_reply", args={"content": ""}),
        ]
        tool_results = [
            ToolResult(value="内容为空，未发送", call_id="call-1", name="nfc_reply"),
        ]
        response = _make_response(calls, tool_results)

        _purge_empty_reply_artifacts(response, {"call-1"})

        # tool_result payload 中的 content 应为空
        tool_result_payload = response.payloads[-1]
        remaining_results = [
            p for p in tool_result_payload.content if isinstance(p, ToolResult)
        ]
        assert remaining_results == []

    def test_preserves_non_empty_calls(self) -> None:
        """非空的 ToolCall 不受影响。"""
        calls = [
            ToolCall(id="call-empty", name="nfc_reply", args={"content": ""}),
            ToolCall(id="call-valid", name="nfc_reply", args={"content": "你好"}),
        ]
        response = _make_response(calls)

        _purge_empty_reply_artifacts(response, {"call-empty"})

        assistant_payload = response.payloads[1]
        remaining_calls = [
            p for p in assistant_payload.content if isinstance(p, ToolCall)
        ]
        assert len(remaining_calls) == 1
        assert remaining_calls[0].id == "call-valid"

    def test_noop_on_empty_set(self) -> None:
        """空 call_id 集合时不做任何修改。"""
        calls = [
            ToolCall(id="call-1", name="nfc_reply", args={"content": "你好"}),
        ]
        response = _make_response(calls)
        original_payloads = list(response.payloads)

        _purge_empty_reply_artifacts(response, set())

        assert response.payloads == original_payloads


class TestEmptyReplyRetryPrompt:
    """``NFC_EMPTY_REPLY_RETRY_PROMPT`` 模板校验。"""

    def test_prompt_mentions_empty_content(self) -> None:
        """提示词中需明确指出 content 为空。"""
        assert "content" in NFC_EMPTY_REPLY_RETRY_PROMPT
        assert "空" in NFC_EMPTY_REPLY_RETRY_PROMPT

    def test_prompt_instructs_retry_or_do_nothing(self) -> None:
        """提示词需指导模型重新调用 nfc_reply 或改用 do_nothing。"""
        assert "nfc_reply" in NFC_EMPTY_REPLY_RETRY_PROMPT
        assert "do_nothing" in NFC_EMPTY_REPLY_RETRY_PROMPT

    def test_prompt_wrapped_in_tag(self) -> None:
        """提示词以 <empty_reply_detected> 标签包裹。"""
        assert NFC_EMPTY_REPLY_RETRY_PROMPT.startswith("<empty_reply_detected>")
        assert NFC_EMPTY_REPLY_RETRY_PROMPT.rstrip().endswith("</empty_reply_detected>")
