"""发送通道故障误判空回复 + 末尾工具调用重申回归测试。

背景（2026-09-18 线上日志）：
- onebot WebSocket 断开时，nfc_reply 带真实内容发送失败，sent_segments 为空，
  decision.visible_reply_segments 变空，被"空回复打回重试"误判——向模型谎报
  "你没填 content"，白烧 2 轮重试并让模型困惑（"数组格式好像发不出去"）。
- 请求体最后一个 payload 是贡献注入包（transient extra_payload），
  "重申：必须工具调用"块被挤出末尾位置，模型容易遗忘硬约束。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from neo_fatum_chatter.context.planner import ContextPlanner
from neo_fatum_chatter.context.renderer import ContextRenderer
from neo_fatum_chatter.context.types import ContextContribution, ContextPlan
from neo_fatum_chatter.domain.decision import Decision
from neo_fatum_chatter.models import ToolCallResult
from neo_fatum_chatter.prompts.builder import NFCPromptBuilder
from neo_fatum_chatter.prompts.templates import NFC_TAIL_TOOL_REMINDER
from neo_fatum_chatter.protocol.decision_parser import build_decision
from neo_fatum_chatter.runtime.orchestrator import empty_reply_retry_due


def _payload_text(payload: Any) -> str:
    """提取 LLMPayload 的纯文本内容。"""
    content = payload.content
    if isinstance(content, list):
        return "".join(str(getattr(item, "text", item)) for item in content)
    return str(getattr(content, "text", content))


def _decision(**overrides: Any) -> Decision:
    base: dict[str, Any] = dict(
        has_reply_action=True,
        visible_reply_segments=[],
        reply_send_infra_failed=False,
    )
    base.update(overrides)
    return Decision(**base)


# ── 空回复打回重试判定 ─────────────────────────────


def test_send_infra_failure_does_not_trigger_retry() -> None:
    """有内容但发送通道故障（WS 断开）不算空回复，禁止打回重试。"""
    decision = _decision(reply_send_infra_failed=True, reply_execution_failed=True)
    assert empty_reply_retry_due(decision, retries=0, max_retries=2) is False


def test_genuine_empty_reply_still_triggers_retry() -> None:
    """content 为空被执行层拒发时维持 v2.6.1 打回重试语义。"""
    assert empty_reply_retry_due(_decision(), retries=0, max_retries=2) is True
    assert empty_reply_retry_due(_decision(), retries=2, max_retries=2) is False
    assert empty_reply_retry_due(_decision(), retries=0, max_retries=0) is False


def test_non_empty_segments_never_retry() -> None:
    assert empty_reply_retry_due(
        _decision(visible_reply_segments=["你好"]), retries=0, max_retries=2
    ) is False


def test_execution_crash_without_infra_flag_still_retries() -> None:
    """执行异常（非通道故障）按原设计仍进入重试。"""
    decision = _decision(reply_execution_failed=True)
    assert empty_reply_retry_due(decision, retries=0, max_retries=2) is True


def test_build_decision_passes_send_infra_flag() -> None:
    """decision_parser 必须把通道故障标记透传到 Decision。"""
    result = ToolCallResult(
        has_reply=True,
        reply_execution_failed=True,
        reply_send_infra_failed=True,
    )
    decision = build_decision(result, SimpleNamespace(payloads=[]))
    assert decision.reply_send_infra_failed is True
    assert decision.reply_execution_failed is True


# ── 末尾工具调用重申位置 ───────────────────────────


def test_contribution_payload_ends_with_tail_reminder() -> None:
    """贡献注入包是请求的最后一个 payload，重申必须保持在绝对末尾。"""
    plan = ContextPlan(
        user_text="[新消息]\n在吗" + NFC_TAIL_TOOL_REMINDER,
        contributions=[
            ContextContribution(
                source="test.cross_stream",
                owner="notice",
                scope="turn",
                priority=10,
                content="跨流上下文示例",
            )
        ],
    )
    _user_payload, extra_payload = ContextRenderer().render_user_payload(plan)
    assert extra_payload is not None
    text = _payload_text(extra_payload)
    assert "跨流上下文示例" in text
    assert text.endswith(NFC_TAIL_TOOL_REMINDER)


def test_no_contributions_returns_no_extra_payload() -> None:
    """无贡献时 extra_payload 为 None，重申由 user_text 末尾承担。"""
    plan = ContextPlan(user_text="[新消息]\n在吗" + NFC_TAIL_TOOL_REMINDER)
    _user_payload, extra_payload = ContextRenderer().render_user_payload(plan)
    assert extra_payload is None


@pytest.mark.asyncio
async def test_plan_user_turn_user_text_keeps_reminder_at_tail() -> None:
    """user_text 必须以重申块收尾。"""
    plan = await ContextPlanner().plan_user_turn(
        formatted_unreads="在吗", stream_id="stream-tail-test"
    )
    assert plan.user_text.startswith("[新消息]\n在吗")
    assert plan.user_text.endswith(NFC_TAIL_TOOL_REMINDER)


@pytest.mark.asyncio
async def test_timeout_payload_ends_with_tail_reminder() -> None:
    """超时提示也必须以重申块收尾。"""
    payload = NFCPromptBuilder().build_timeout_payload(
        elapsed_seconds=120.0,
        expected_reaction="",
        consecutive_timeouts=0,
        last_bot_message="",
        max_consecutive_timeouts=3,
    )
    assert _payload_text(payload).endswith(NFC_TAIL_TOOL_REMINDER)
