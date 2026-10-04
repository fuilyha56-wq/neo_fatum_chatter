"""NFC 感知重试策略测试：纯文本重试的提示逐次升级（学习自 KFC）。

首次纯文本注入温和引导（<perception_completed>），重试仍失败则升级为
硬纠正提醒（<perception_retry_warning>），明确告知纯文本不会被执行、
务必通过工具调用收口。DeepSeek compat 路径保持硬性措辞不变。
"""

from __future__ import annotations

from types import SimpleNamespace

from neo_fatum_chatter.protocol.perception_retry import (
    apply_perception_followup,
    select_followup_prompt,
)
from neo_fatum_chatter.prompts.templates import (
    NFC_PERCEIVE_HARD_REMINDER_PROMPT,
    NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING,
)
from src.kernel.llm import LLMPayload, ROLE, Text


def test_first_pure_text_uses_gentle_followup() -> None:
    """第一次纯文本：温和引导模板。"""
    prompt, used_compat, escalated = select_followup_prompt(
        SimpleNamespace(model_set=None), attempt=0
    )
    assert prompt is NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING
    assert used_compat is False
    assert escalated is False
    assert "<perception_completed>" in prompt
    assert "纯文本不会被执行" not in prompt


def test_compat_mode_uses_compat_followup_prompt() -> None:
    """tool_call_compat=true 时，NFC 重试必须继续使用兼容上下文。"""

    class NfcReply:
        @classmethod
        def to_schema(cls):
            return {
                "name": "nfc_reply",
                "description": "send a reply",
                "parameters": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                },
            }

    response = SimpleNamespace(
        model_set=[{"tool_call_compat": True}],
        payloads=[LLMPayload(ROLE.TOOL, [NfcReply])],
    )

    prompt, used_compat, escalated = select_followup_prompt(response)

    assert used_compat is True
    assert escalated is True
    assert '"tool_calls"' in prompt
    assert "nfc_reply" in prompt


def test_repeated_pure_text_escalates_to_hard_reminder() -> None:
    """重试后仍是纯文本：升级为硬纠正提醒。"""
    prompt, used_compat, escalated = select_followup_prompt(
        SimpleNamespace(model_set=None), attempt=1
    )
    assert prompt is NFC_PERCEIVE_HARD_REMINDER_PROMPT
    assert used_compat is False
    assert escalated is True
    assert "<perception_retry_warning>" in prompt
    # KFC 式核心语义：纯文本不会被执行 + 务必通过工具调用收口
    assert "纯文本不会被执行" in prompt
    assert "务必通过工具调用" in prompt


def test_apply_followup_injects_user_payload_per_attempt() -> None:
    """attempt 决定注入哪档提示，且作为 USER payload 追加到链尾。"""
    for attempt, marker in [(0, "<perception_completed>"), (1, "<perception_retry_warning>")]:
        response = SimpleNamespace(
            call_list=[],
            model_set=None,
            payloads=[LLMPayload(ROLE.ASSISTANT, [Text("纯文本草稿")])],
        )
        response.add_payload = lambda payload, _resp=response: _resp.payloads.append(payload)
        outcome = apply_perception_followup(response, "纯文本草稿", attempt=attempt)
        assert outcome.escalated == (attempt >= 1)
        assert outcome.used_compat is False
        # 草稿改写 + followup 注入，共两个 payload
        assert len(response.payloads) == 2
        assert response.payloads[0].role == ROLE.ASSISTANT
        assert "<unsent_perception_draft>" in response.payloads[0].content[0].text
        assert response.payloads[1].role == ROLE.USER
        assert marker in response.payloads[1].content[0].text


def test_apply_followup_defaults_keep_backward_compat() -> None:
    """不传 attempt 时保持旧行为（温和引导），旧调用方不受影响。"""
    response = SimpleNamespace(
        call_list=[],
        model_set=None,
        payloads=[LLMPayload(ROLE.ASSISTANT, [Text("草稿")])],
    )
    response.add_payload = response.payloads.append
    outcome = apply_perception_followup(response, "草稿")
    assert outcome.escalated is False
    assert "<perception_completed>" in response.payloads[-1].content[0].text


def test_final_retry_escalates_immediately() -> None:
    """max_retries=1 的唯一重试没有再失败的余地，直接用硬纠正提醒。

    回归：升级条件只看 attempt≥1 时，常规配置下硬提醒永远轮不到注入
    （2026-10-03 05:21 线上日志：第 1 轮失败注入温和引导，第 2 轮仍复读
    草稿后直接收口）。
    """
    prompt, used_compat, escalated = select_followup_prompt(
        SimpleNamespace(model_set=None), attempt=0, is_final=True
    )
    assert prompt is NFC_PERCEIVE_HARD_REMINDER_PROMPT
    assert used_compat is False
    assert escalated is True


def test_final_retry_not_reached_uses_gentle() -> None:
    """max_retries=2 时的第一次重试仍用温和引导，留给最后一轮升级。"""
    prompt, used_compat, escalated = select_followup_prompt(
        SimpleNamespace(model_set=None), attempt=0, is_final=False
    )
    assert prompt is NFC_PERCEIVE_FOLLOWUP_PROMPT_TOOL_CALLING
    assert escalated is False
