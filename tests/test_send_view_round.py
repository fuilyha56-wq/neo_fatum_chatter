"""send_view_round 测试：带注入包的感知重试轮次。

带 transient 注入包的回合此前走裸发送分支、绕过感知循环；修复后由
send_view_round 发送——链前置处理与 send_with_nfc_model_clients 的
LLMResponse 分支一致，且注入包每轮重建后保持在请求体链尾。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from neo_fatum_chatter.runtime.request_view import (
    RequestView,
    build_request_view,
    send_view_round,
)
from src.kernel.llm import LLMPayload, ROLE, Text


class _FakeChain:
    """最小化模拟 LLMResponse 链：消费/回填语义与框架一致。"""

    def __init__(self, payloads: list[LLMPayload]) -> None:
        self.payloads = list(payloads)
        self._consumed = False
        self._appended_to_context = False
        self.model_set = None
        self._upper = SimpleNamespace(request_name="nfc_test", meta_data={})
        self.context_manager = None

    def add_payload(self, payload: LLMPayload) -> None:
        self.payloads.append(payload)

    def to_payload(self) -> LLMPayload:
        return LLMPayload(ROLE.ASSISTANT, [Text("上一轮输出")])

    def __await__(self):
        async def _consume():
            self._consumed = True
            return self

        return _consume().__await__()


@pytest.mark.asyncio
async def test_view_round_consumes_chain_and_appends_assistant(monkeypatch) -> None:
    """未消费的链先消费并回填 assistant payload（LLMResponse 前置处理）。"""
    chain = _FakeChain([LLMPayload(ROLE.USER, [Text("历史")])])
    transient = LLMPayload(ROLE.USER, [Text("[附加上下文] 注入包")])
    send_target = build_request_view(chain, [transient])

    sent_payloads: list[LLMPayload] = []

    async def fake_send(self, auto_append_response=True, *, stream=False):
        sent_payloads.extend(self.payloads)
        return SimpleNamespace(_consumed=True)

    monkeypatch.setattr(RequestView, "send", fake_send)

    result = await send_view_round(chain, send_target)

    assert result is not None
    # 前置处理：链被消费、assistant payload 回填
    assert chain._consumed is True
    assert chain._appended_to_context is True
    assert chain.payloads[-1].role == ROLE.ASSISTANT
    # 发送的视图链 = 回填后的链 + 注入包，注入包保持绝对链尾
    assert sent_payloads[-1] is transient
    assert sent_payloads[:-1] == chain.payloads


@pytest.mark.asyncio
async def test_view_round_keeps_transients_after_followup(monkeypatch) -> None:
    """重试轮次：提醒注入等新 payload 排在注入包之前，注入包仍居链尾。"""
    chain = _FakeChain(
        [
            LLMPayload(ROLE.USER, [Text("历史")]),
            LLMPayload(ROLE.USER, [Text("<perception_completed> 引导提示")]),
        ]
    )
    # 模拟已消费且已回填的第二轮链
    chain._consumed = True
    chain._appended_to_context = True
    transient = LLMPayload(ROLE.USER, [Text("注入包")])
    send_target = build_request_view(chain, [transient])

    sent_payloads: list[LLMPayload] = []

    async def fake_send(self, auto_append_response=True, *, stream=False):
        sent_payloads.extend(self.payloads)
        return SimpleNamespace(_consumed=True)

    monkeypatch.setattr(RequestView, "send", fake_send)

    await send_view_round(chain, send_target)

    # 链尾顺序：…引导提示 → 注入包（绝对末尾）
    assert sent_payloads[-1] is transient
    assert "<perception_completed>" in sent_payloads[-2].content[0].text
    # 不重复回填 assistant
    assert len(sent_payloads) == len(chain.payloads) + 1


@pytest.mark.asyncio
async def test_perceive_loop_with_view_does_not_double_consume(monkeypatch) -> None:
    """视图路径的响应已被 RequestView.send 消费，循环不得二次 await。

    回归：二次 await 已消费响应会抛 LLMResponseConsumedError
    （2026-10-03 05:17 线上日志，带注入包回合首轮请求即失败）。
    旧代码无条件 ``await new_response``，不可 await 的假对象会直接抛
    TypeError，因此本测试能捕获该回归。
    """
    import neo_fatum_chatter.chatter as chatter_module

    monkeypatch.setattr(
        chatter_module,
        "get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda stream_id: None),
    )
    monkeypatch.setattr(
        chatter_module,
        "prepare_payload_chain_for_send",
        lambda response, reason="": False,
    )

    consumed_response = SimpleNamespace(
        message="纯文本感言",
        call_list=[],
        model_set=None,
        reasoning_content="",
        payloads=[LLMPayload(ROLE.ASSISTANT, [Text("纯文本感言")])],
        _consumed=True,
    )

    async def fake_send_view_round(chain, send_target):
        # 模拟 RequestView.send：返回的响应已消费
        return consumed_response

    monkeypatch.setattr(chatter_module, "send_view_round", fake_send_view_round)

    fake_self = SimpleNamespace(stream_id="stream-test")
    base_chain = SimpleNamespace(payloads=[])
    view = SimpleNamespace(transient_payloads=[])

    result = await chatter_module.NeoFatumChatter._send_with_perceive_loop(
        fake_self, base_chain, 0, send_target=view
    )

    assert result is consumed_response
