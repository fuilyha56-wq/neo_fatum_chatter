"""NFC provider 兼容适配器测试。"""

from __future__ import annotations

from types import SimpleNamespace

from neo_fatum_chatter.protocol.compat_adapter import (
    prepare_nfc_model_set,
    rewrite_response_as_unsent_draft,
    try_parse_tool_call_compat_response,
)
from src.kernel.llm import LLMPayload, ReasoningText, ROLE, Text


def test_rewrite_response_as_unsent_draft_preserves_reasoning() -> None:
    """感知草稿改写后仍需回传 thinking 模式的推理内容。"""
    response = SimpleNamespace(
        call_list=[],
        payloads=[
            LLMPayload(
                ROLE.ASSISTANT,
                [ReasoningText("provider reasoning"), Text("draft")],
            )
        ],
    )

    assert rewrite_response_as_unsent_draft(response, "draft") is True
    assert isinstance(response.payloads[-1].content[0], ReasoningText)
    assert response.payloads[-1].content[0].text == "provider reasoning"
    assert isinstance(response.payloads[-1].content[1], Text)
    assert "<unsent_perception_draft>" in response.payloads[-1].content[1].text


def test_prepare_nfc_model_set_uses_reasoning_text_for_cli_deepseek() -> None:
    """Console Go DeepSeek 模型应显式开启 thinking 并回传 reasoning_text。"""
    model_set = [
        {
            "api_provider": "cli",
            "model_identifier": "deepseek-v4-flash",
            "extra_params": {"tool_choice": "auto"},
        }
    ]

    prepared = prepare_nfc_model_set(model_set)

    assert "reasoning_history_mode" not in model_set[0]["extra_params"]
    assert prepared[0]["extra_params"]["reasoning_history_mode"] is True
    # Console Go 强制 thinking mode 且要求回传 reasoning_text：必须显式开启
    # thinking（否则响应不带 reasoning_text，后续轮次无法回传导致 400）
    assert prepared[0]["extra_params"]["enable_thinking"] is True
    assert prepared[0]["extra_params"]["thinking"]["enabled"] is True


def test_compat_missing_call_ids_are_unique_across_responses() -> None:
    """不同响应缺失 call_id 时不得复用同一个兜底 ID。"""
    raw_message = '{"tool_calls":[{"name":"nfc_reply","args":{"content":""}}]}'
    first = SimpleNamespace(message=raw_message, call_list=[], payloads=[])
    second = SimpleNamespace(message=raw_message, call_list=[], payloads=[])

    assert try_parse_tool_call_compat_response(first) is True
    assert try_parse_tool_call_compat_response(second) is True

    assert first.call_list[0].id != second.call_list[0].id