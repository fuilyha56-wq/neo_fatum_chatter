"""NFC 工具执行与 Decision 构建的集成测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import neo_fatum_chatter.parser as parser_module
from neo_fatum_chatter.parser import parse_tool_calls
from neo_fatum_chatter.protocol.decision_parser import build_decision
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolResult


class _Registry:
    """提供调用名解析所需的最小工具注册表。"""

    def get_all_names(self) -> list[str]:
        """返回空注册名列表，让测试调用保持原名。"""
        return []


class _Response(SimpleNamespace):
    """可追加工具结果的最小响应对象。"""

    def add_payload(self, payload: LLMPayload) -> None:
        """追加 payload。"""
        self.payloads.append(payload)


def _response(calls: list[ToolCall]) -> _Response:
    """构造带当前 assistant 工具调用的响应链。"""
    return _Response(
        payloads=[
            LLMPayload(ROLE.USER, Text("测试消息")),
            LLMPayload(ROLE.ASSISTANT, list(calls)),
        ],
        call_list=list(calls),
        message="",
    )


def _config() -> SimpleNamespace:
    """构造 parser 使用的最小配置。"""
    return SimpleNamespace(
        general=SimpleNamespace(perception_extract_task="sub_actor"),
        debug=SimpleNamespace(show_prompt=False),
    )


@pytest.mark.asyncio
async def test_duplicate_call_ids_are_normalized_before_execution() -> None:
    """同一响应内重复 call_id 必须在产生副作用前改成唯一值。"""
    calls = [
        ToolCall(id="duplicate", name="query_habits", args={}),
        ToolCall(id="duplicate", name="record_habit", args={"habit": "早睡"}),
    ]
    response = _response(calls)
    executed_ids: list[str | None] = []

    async def run_tool_call(
        current_calls: list[ToolCall],
        current_response: Any,
        usable_map: Any,
        trigger_msg: Any,
    ) -> list[tuple[bool, bool]]:
        """记录执行前 ID，并模拟成功工具结果。"""
        del usable_map, trigger_msg
        executed_ids.extend(call.id for call in current_calls)
        assert len(set(executed_ids)) == len(executed_ids)
        for call in current_calls:
            current_response.add_payload(
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(value="ok", call_id=call.id, name=call.name),
                )
            )
        return [(True, True) for _ in current_calls]

    await parse_tool_calls(
        response,
        _Registry(),
        SimpleNamespace(),
        _config(),
        run_tool_call_fn=run_tool_call,
    )

    assistant_ids = [
        part.id
        for part in response.payloads[1].content
        if isinstance(part, ToolCall)
    ]
    assert len(set(assistant_ids)) == 2
    assert assistant_ids == executed_ids


@pytest.mark.asyncio
async def test_failed_schedule_does_not_create_proactive_plan() -> None:
    """预约工具执行失败时不得把参数提交成真实主动计划。"""
    call = ToolCall(
        id="schedule-1",
        name="schedule_proactive",
        args={"delay_minutes": 5, "reason": "稍后问候"},
    )
    response = _response([call])

    async def run_tool_call(
        current_calls: list[ToolCall],
        current_response: Any,
        usable_map: Any,
        trigger_msg: Any,
    ) -> list[tuple[bool, bool]]:
        """模拟工具结果已写回但 execute 返回失败。"""
        del usable_map, trigger_msg
        current_response.add_payload(
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(
                    value="执行失败: scheduler unavailable",
                    call_id=current_calls[0].id,
                    name=current_calls[0].name,
                ),
            )
        )
        return [(True, False)]

    result = await parse_tool_calls(
        response,
        _Registry(),
        SimpleNamespace(),
        _config(),
        run_tool_call_fn=run_tool_call,
    )
    decision = build_decision(result, response)

    assert decision.proactive_schedule is None


@pytest.mark.asyncio
async def test_failed_reply_is_not_reported_as_visible_output() -> None:
    """回复执行失败时不得把原始 content 当成已经发送的文本。"""
    call = ToolCall(
        id="reply-1",
        name="nfc_reply",
        args={"content": ["实际上没有发出去"]},
    )
    response = _response([call])

    async def run_tool_call(
        current_calls: list[ToolCall],
        current_response: Any,
        usable_map: Any,
        trigger_msg: Any,
    ) -> list[tuple[bool, bool]]:
        """模拟回复结果已写回但发送失败。"""
        del usable_map, trigger_msg
        current_response.add_payload(
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(
                    value="执行失败: 消息发送失败",
                    call_id=current_calls[0].id,
                    name=current_calls[0].name,
                ),
            )
        )
        return [(True, False)]

    result = await parse_tool_calls(
        response,
        _Registry(),
        SimpleNamespace(),
        _config(),
        run_tool_call_fn=run_tool_call,
    )
    decision = build_decision(result, response)

    assert decision.has_reply_action is True
    assert decision.visible_reply_segments == []


@pytest.mark.asyncio
async def test_information_query_defers_same_response_reply() -> None:
    """查询结果尚未返回时，不得执行同批次预先生成的回复。"""
    calls = [
        ToolCall(id="query", name="nfc_query_habits", args={}),
        ToolCall(
            id="reply",
            name="nfc_reply",
            args={"content": ["基于尚未读取的结果作答"]},
        ),
    ]
    response = _response(calls)
    executed_names: list[str] = []

    async def run_tool_call(
        current_calls: list[ToolCall],
        current_response: Any,
        usable_map: Any,
        trigger_msg: Any,
    ) -> list[tuple[bool, bool]]:
        """记录实际执行的调用并写回完整结果。"""
        del usable_map, trigger_msg
        for call in current_calls:
            executed_names.append(call.name)
            current_response.add_payload(
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(value="ok", call_id=call.id, name=call.name),
                )
            )
        return [(True, True) for _ in current_calls]

    result = await parse_tool_calls(
        response,
        _Registry(),
        SimpleNamespace(),
        _config(),
        run_tool_call_fn=run_tool_call,
    )
    decision = build_decision(result, response)

    assert executed_names == ["nfc_query_habits"]
    assert decision.has_info_tool_calls is True
    assert decision.has_reply_action is False
    assert decision.visible_reply_segments == []
    assert [call.name for call in response.call_list] == ["nfc_query_habits"]
    assistant_calls = [
        part
        for part in response.payloads[1].content
        if isinstance(part, ToolCall)
    ]
    assert [call.name for call in assistant_calls] == ["nfc_query_habits"]


@pytest.mark.asyncio
async def test_reused_provider_call_id_detects_new_result_by_count() -> None:
    """历史同 ID 结果存在时，本轮新增失败结果仍应与当前调用保持配对。"""
    reused_id = "provider-reused"
    current_call = ToolCall(
        id=reused_id,
        name="schedule_proactive",
        args={"delay_minutes": 5},
    )
    response = _Response(
        payloads=[
            LLMPayload(ROLE.USER, Text("第一轮")),
            LLMPayload(
                ROLE.ASSISTANT,
                ToolCall(id=reused_id, name="query_habits", args={}),
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(value="旧结果", call_id=reused_id, name="query_habits"),
            ),
            LLMPayload(ROLE.ASSISTANT, Text("已读取")),
            LLMPayload(ROLE.USER, Text("第二轮")),
            LLMPayload(ROLE.ASSISTANT, current_call),
        ],
        call_list=[current_call],
        message="",
    )

    async def run_tool_call(
        current_calls: list[ToolCall],
        current_response: Any,
        usable_map: Any,
        trigger_msg: Any,
    ) -> list[tuple[bool, bool]]:
        """写回同 ID 的本轮失败结果。"""
        del usable_map, trigger_msg
        current_response.add_payload(
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(
                    value="执行失败: scheduler unavailable",
                    call_id=current_calls[0].id,
                    name=current_calls[0].name,
                ),
            )
        )
        return [(True, False)]

    await parse_tool_calls(
        response,
        _Registry(),
        SimpleNamespace(),
        _config(),
        run_tool_call_fn=run_tool_call,
    )

    current_calls = [
        part
        for part in response.payloads[-2].content
        if isinstance(part, ToolCall)
    ]
    assert [call.name for call in current_calls] == ["schedule_proactive"]
    assert response.payloads[-1].role == ROLE.TOOL_RESULT


@pytest.mark.asyncio
async def test_perception_backfill_keeps_following_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回复草稿回填不得覆盖同批次后续第三方工具。"""
    calls = [
        ToolCall(id="reply", name="nfc_reply", args={"content": ""}),
        ToolCall(id="query", name="query_habits", args={}),
    ]
    response = _Response(
        payloads=[
            LLMPayload(ROLE.USER, Text("测试消息")),
            LLMPayload(
                ROLE.ASSISTANT,
                [
                    Text(
                        "<unsent_perception_draft>\n"
                        "以下内容是你刚才形成的内部感知/未发送草稿，并没有发送给对方：\n"
                        "草稿回复\n"
                        "请把它视为内部草稿，而不是已经发出的消息。\n"
                        "</unsent_perception_draft>"
                    ),
                    *calls,
                ],
            ),
        ],
        call_list=list(calls),
        message="",
    )
    executed: list[tuple[str, dict[str, Any] | str]] = []
    monkeypatch.setattr(
        parser_module,
        "extract_reply_from_perception",
        lambda *args, **kwargs: _async_value("草稿回复"),
    )

    async def run_tool_call(
        current_calls: list[ToolCall],
        current_response: Any,
        usable_map: Any,
        trigger_msg: Any,
    ) -> list[tuple[bool, bool]]:
        """记录最终执行的调用名和参数。"""
        del usable_map, trigger_msg
        for call in current_calls:
            executed.append((call.name, call.args))
            current_response.add_payload(
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(value="ok", call_id=call.id, name=call.name),
                )
            )
        return [(True, True) for _ in current_calls]

    await parse_tool_calls(
        response,
        _Registry(),
        SimpleNamespace(),
        _config(),
        run_tool_call_fn=run_tool_call,
    )

    assert [name for name, _args in executed] == ["nfc_reply", "query_habits"]
    assert response.call_list[0].args["content"] == ["草稿回复"]
    assert response.call_list[1].name == "query_habits"
    assistant_calls = [
        part
        for part in response.payloads[1].content
        if isinstance(part, ToolCall)
    ]
    assert assistant_calls[0].args["content"] == ["草稿回复"]
    assert assistant_calls[1].name == "query_habits"


async def _async_value(value: str) -> str:
    """返回可 await 的测试值。"""
    return value