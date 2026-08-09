"""NFC provider 兼容适配器测试。"""

from __future__ import annotations

from types import SimpleNamespace

from neo_fatum_chatter.protocol.compat_adapter import (
    prepare_nfc_model_set,
    rewrite_response_as_unsent_draft,
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
    """Console Go DeepSeek 模型应回传 reasoning_text 历史。"""
    model_set = [
        {
            "api_provider": "cli",
            "model_identifier": "deepseek-v4-flash",
            "extra_params": {"tool_choice": "auto"},
        }
    ]

    prepared = prepare_nfc_model_set(model_set)

    assert "reasoning_history_mode" not in model_set[0]["extra_params"]
    assert prepared[0]["extra_params"]["reasoning_history_mode"] == "reasoning_text"