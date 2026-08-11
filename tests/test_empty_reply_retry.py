"""空回复打回重试逻辑测试。

覆盖 ``orchestrator._collect_empty_reply_call_ids`` 与
``orchestrator._purge_empty_reply_artifacts`` 两个纯函数，以及
``NFC_EMPTY_REPLY_RETRY_PROMPT`` 模板存在性校验。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import neo_fatum_chatter.runtime.orchestrator as orchestrator_module
from neo_fatum_chatter.chatter import NeoFatumChatter
from neo_fatum_chatter.domain.decision import Decision
from neo_fatum_chatter.prompts.templates import NFC_EMPTY_REPLY_RETRY_PROMPT
from neo_fatum_chatter.runtime.orchestrator import (
    _collect_empty_reply_call_ids,
    _purge_empty_reply_artifacts,
    execute_orchestrator,
)
from neo_fatum_chatter.runtime.turn_controller import (
    TurnControlResult,
    TurnInputResult,
    commit_turn_decision,
)
from neo_fatum_chatter.services.context_sanitizer import close_pending_tool_chain
from src.app.plugin_system.base import Stop
from src.kernel.llm import (
    LLMContextManager,
    LLMPayload,
    ROLE,
    Text,
    ToolCall,
    ToolResult,
)


@pytest.mark.asyncio
async def test_execute_reply_returns_framework_failure() -> None:
    """Action 未抛异常但返回失败时，纯文本发送回退也必须报告失败。"""
    chatter = object.__new__(NeoFatumChatter)

    async def exec_llm_usable(*args, **kwargs):
        del args, kwargs
        return False, "执行失败: send rejected"

    chatter.exec_llm_usable = exec_llm_usable

    success = await chatter._execute_reply(
        "未发送内容",
        SimpleNamespace(),
        trigger_msg=SimpleNamespace(),
    )

    assert success is False


@pytest.mark.asyncio
async def test_commit_does_not_persist_unsent_plain_text() -> None:
    """无有效动作的模型纯文本不是已发送回复，不得写入持久会话链。"""
    updates: list[list[dict[str, object]]] = []
    session = SimpleNamespace(
        add_bot_planning=lambda **kwargs: None,
        update_chain=lambda entries, max_payloads: updates.append(entries),
        compress_round_count=0,
    )

    async def save_session(current_session):
        del current_session

    chatter = SimpleNamespace(_save_session=save_session)
    result = await commit_turn_decision(
        chatter,
        Decision(),
        SimpleNamespace(message="模型生成但没有发送的纯文本"),
        session,
        SimpleNamespace(prompt=SimpleNamespace(max_context_payloads=20)),
        SimpleNamespace(),
        SimpleNamespace(),
        "用户消息",
        0.0,
        True,
        False,
    )

    assert updates == []
    assert isinstance(result.next_signal, Stop)


@pytest.mark.asyncio
async def test_commit_does_not_wait_after_complete_reply_failure() -> None:
    """回复完全未发送时不得进入等待，否则新消息会被抑制到超时。"""
    waiting_configs: list[object] = []
    cleared: list[bool] = []
    session = SimpleNamespace(
        add_bot_planning=lambda **kwargs: None,
        record_mood=lambda mood: None,
        update_chain=lambda entries, max_payloads: None,
        compress_round_count=0,
        consecutive_timeout_count=0,
        set_waiting=waiting_configs.append,
        clear_waiting=lambda: cleared.append(True),
    )

    async def save_session(current_session):
        del current_session

    chatter = SimpleNamespace(_save_session=save_session)
    config = SimpleNamespace(
        prompt=SimpleNamespace(max_context_payloads=20, summary_enabled=False),
        wait=SimpleNamespace(apply_rules=lambda seconds, timeout_count: seconds),
    )
    result = await commit_turn_decision(
        chatter,
        Decision(
            wait_seconds=30,
            has_reply_action=True,
            reply_execution_failed=True,
            has_meaningful_action=True,
        ),
        SimpleNamespace(message=""),
        session,
        config,
        SimpleNamespace(),
        SimpleNamespace(),
        "用户消息",
        0.0,
        True,
        False,
    )

    assert waiting_configs == []
    assert cleared == [True]
    assert isinstance(result.next_signal, Stop)


@pytest.mark.asyncio
async def test_commit_can_wait_after_partial_reply_success() -> None:
    """部分发送已有可见段落时，可继续等待用户回应。"""
    waiting_configs: list[object] = []
    session = SimpleNamespace(
        add_bot_planning=lambda **kwargs: None,
        record_mood=lambda mood: None,
        update_chain=lambda entries, max_payloads: None,
        compress_round_count=0,
        consecutive_timeout_count=0,
        set_waiting=waiting_configs.append,
        clear_waiting=lambda: None,
        waiting_config=None,
    )

    async def save_session(current_session):
        del current_session

    chatter = SimpleNamespace(
        _save_session=save_session,
        _get_session_store=lambda: SimpleNamespace(),
    )
    config = SimpleNamespace(
        prompt=SimpleNamespace(max_context_payloads=20, summary_enabled=False),
        wait=SimpleNamespace(apply_rules=lambda seconds, timeout_count: seconds),
    )

    def set_waiting(waiting_config):
        waiting_configs.append(waiting_config)
        session.waiting_config = waiting_config

    session.set_waiting = set_waiting
    result = await commit_turn_decision(
        chatter,
        Decision(
            wait_seconds=30,
            visible_reply_segments=["已发送部分"],
            has_reply_action=True,
            reply_execution_failed=True,
            has_meaningful_action=True,
        ),
        SimpleNamespace(message=""),
        session,
        config,
        SimpleNamespace(),
        SimpleNamespace(),
        "用户消息",
        0.0,
        True,
        False,
    )

    assert len(waiting_configs) == 1
    assert result.continue_loop is True


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
        """清理后仅含对应结果的 tool_result payload 被整体移除。"""
        calls = [
            ToolCall(id="call-1", name="nfc_reply", args={"content": ""}),
        ]
        tool_results = [
            ToolResult(value="内容为空，未发送", call_id="call-1", name="nfc_reply"),
        ]
        response = _make_response(calls, tool_results)

        _purge_empty_reply_artifacts(response, {"call-1"})

        assert all(payload.role != ROLE.TOOL_RESULT for payload in response.payloads)

    def test_preserves_other_results_in_shared_payload(self) -> None:
        """同一 payload 中非目标工具结果应被保留。"""
        calls = [
            ToolCall(id="call-empty", name="nfc_reply", args={"content": ""}),
            ToolCall(id="call-valid", name="query_habits", args={}),
        ]
        tool_results = [
            ToolResult(value="内容为空，未发送", call_id="call-empty", name="nfc_reply"),
            ToolResult(value="查询成功", call_id="call-valid", name="query_habits"),
        ]
        response = _make_response(calls, tool_results)

        _purge_empty_reply_artifacts(response, {"call-empty"})

        tool_result_payloads = [
            payload for payload in response.payloads if payload.role == ROLE.TOOL_RESULT
        ]
        assert len(tool_result_payloads) == 1
        remaining_results = [
            part
            for part in tool_result_payloads[0].content
            if isinstance(part, ToolResult)
        ]
        assert [result.call_id for result in remaining_results] == ["call-valid"]

    def test_preserves_older_tool_turn_with_reused_call_id(self) -> None:
        """清理当前空回复时不得误删历史中同 ID 的完整工具链。"""
        reused_id = "NFC_compat_call_0"
        response = SimpleNamespace(
            payloads=[
                LLMPayload(ROLE.USER, Text("第一轮")),
                LLMPayload(
                    ROLE.ASSISTANT,
                    ToolCall(id=reused_id, name="query_habits", args={}),
                ),
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(value="查询成功", call_id=reused_id, name="query_habits"),
                ),
                LLMPayload(ROLE.ASSISTANT, Text("已读取习惯")),
                LLMPayload(ROLE.USER, Text("第二轮")),
                LLMPayload(
                    ROLE.ASSISTANT,
                    ToolCall(
                        id=reused_id,
                        name="nfc_reply",
                        args={"content": ""},
                    ),
                ),
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(
                        value="内容为空，未发送",
                        call_id=reused_id,
                        name="nfc_reply",
                    ),
                ),
            ],
            call_list=[
                ToolCall(id=reused_id, name="nfc_reply", args={"content": ""}),
            ],
        )

        _purge_empty_reply_artifacts(response, {reused_id})

        historical_calls = [
            part
            for part in response.payloads[1].content
            if isinstance(part, ToolCall)
        ]
        historical_results = [
            part
            for part in response.payloads[2].content
            if isinstance(part, ToolResult)
        ]
        assert [call.name for call in historical_calls] == ["query_habits"]
        assert [result.name for result in historical_results] == ["query_habits"]
        assert [payload.role for payload in response.payloads] == [
            ROLE.USER,
            ROLE.ASSISTANT,
            ROLE.TOOL_RESULT,
            ROLE.ASSISTANT,
            ROLE.USER,
        ]

    def test_removes_empty_tool_result_payload_before_retry_bridge(self) -> None:
        """空回复清理后不应留下会在追加重试提示前触发校验的空壳。"""
        calls = [
            ToolCall(id="call-1", name="nfc_reply", args={"content": ""}),
        ]
        tool_results = [
            ToolResult(value="内容为空，未发送", call_id="call-1", name="nfc_reply"),
        ]
        response = _make_response(calls, tool_results)
        context_manager = LLMContextManager()

        def add_payload(payload: LLMPayload) -> None:
            """复现生产链通过上下文管理器追加 payload 的校验行为。"""
            response.payloads = context_manager.add_payload(response.payloads, payload)

        response.add_payload = add_payload

        _purge_empty_reply_artifacts(response, {"call-1"})

        assert close_pending_tool_chain(response, reason="test-empty-reply") is False
        assert [payload.role for payload in response.payloads] == [ROLE.USER]

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


@pytest.mark.asyncio
async def test_orchestrator_uses_all_configured_empty_reply_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续空回复时应真正用完配置的两次重试机会。"""
    def make_response(call_id: str, content: str) -> SimpleNamespace:
        call = ToolCall(
            id=call_id,
            name="nfc_reply",
            args={"content": content},
        )
        response = SimpleNamespace(
            payloads=[
                LLMPayload(ROLE.USER, Text("你好")),
                LLMPayload(ROLE.ASSISTANT, call),
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(
                        value="已处理" if content else "内容为空，未发送",
                        call_id=call_id,
                        name="nfc_reply",
                    ),
                ),
            ],
            call_list=[call],
            message="",
        )

        def add_payload(payload: LLMPayload) -> None:
            response.payloads.append(payload)

        response.add_payload = add_payload
        return response

    responses = [
        make_response("empty-1", ""),
        make_response("empty-2", ""),
        make_response("valid-3", "终于有内容"),
    ]
    decisions = [
        Decision(
            actions=[{"type": "nfc_reply", "content": []}],
            has_reply_action=True,
            has_meaningful_action=True,
        ),
        Decision(
            actions=[{"type": "nfc_reply", "content": []}],
            has_reply_action=True,
            has_meaningful_action=True,
        ),
        Decision(
            actions=[{"type": "nfc_reply", "content": ["终于有内容"]}],
            visible_reply_segments=["终于有内容"],
            has_reply_action=True,
            has_meaningful_action=True,
        ),
    ]
    send_count = 0
    committed: list[Decision] = []

    async def fake_send(response, max_retries):
        nonlocal send_count
        result = responses[send_count]
        send_count += 1
        return result

    async def fake_parse(*args, **kwargs):
        return decisions.pop(0)

    async def fake_commit(*args, **kwargs):
        committed.append(args[1])
        return TurnControlResult(
            next_signal=Stop(0),
            return_after_yield=True,
        )

    initial_response = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, Text("你好"))],
        call_list=[],
        message="",
    )
    session = SimpleNamespace(
        update_chain=lambda *args, **kwargs: None,
    )
    config = SimpleNamespace(
        general=SimpleNamespace(
            enabled=True,
            native_multimodal=False,
            models=[],
            model_task="actor",
            temperature=0.7,
            max_tokens=1024,
            max_compat_retries=0,
            max_empty_reply_retries=2,
            max_consecutive_llm_failures=3,
        ),
        buffer=SimpleNamespace(interrupt_enabled=False),
        debug=SimpleNamespace(show_prompt=False),
        prompt=SimpleNamespace(max_context_payloads=20, summary_enabled=False),
    )
    chatter = SimpleNamespace(
        stream_id="stream-test",
        _get_config=lambda: config,
        _get_session=lambda: session,
        _build_initial_context=lambda *args: (
            initial_response,
            None,
            SimpleNamespace(),
            SimpleNamespace(),
            False,
        ),
        _send_with_perceive_loop=fake_send,
        _get_virtual_trigger_message=lambda: SimpleNamespace(),
        fetch_unreads=lambda **kwargs: ("", []),
        flush_unreads=lambda messages: None,
        _save_session=lambda session: None,
        run_tool_call=lambda *args, **kwargs: None,
    )

    async def resolve(value):
        return value

    chatter._get_session = lambda: resolve(session)
    chatter._build_initial_context = lambda *args: resolve(
        (initial_response, None, SimpleNamespace(), SimpleNamespace(), False)
    )
    chatter._get_virtual_trigger_message = lambda: resolve(SimpleNamespace())
    chatter.fetch_unreads = lambda **kwargs: resolve(("", []))
    chatter.flush_unreads = lambda messages: resolve(None)
    chatter._save_session = lambda session: resolve(None)

    monkeypatch.setattr(
        "src.app.plugin_system.api.stream_api.activate_stream",
        lambda stream_id: resolve(SimpleNamespace(stream_id=stream_id)),
    )
    monkeypatch.setattr(orchestrator_module, "get_model_set_by_task", lambda task: object())
    monkeypatch.setattr(
        orchestrator_module,
        "prepare_turn_input",
        lambda *args, **kwargs: resolve(
            TurnInputResult(response=args[1], unread_msgs=[])
        ),
    )
    monkeypatch.setattr(orchestrator_module, "parse_response_decision", fake_parse)
    monkeypatch.setattr(orchestrator_module, "commit_turn_decision", fake_commit)

    yielded = [signal async for signal in execute_orchestrator(chatter)]

    assert send_count == 3
    assert committed[0].visible_reply_segments == ["终于有内容"]
    assert len(yielded) == 1
    assert isinstance(yielded[0], Stop)


@pytest.mark.asyncio
async def test_empty_reply_retry_plain_text_uses_send_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空回复重试若得到纯文本，也应实际发送后再提交可见回复。"""
    empty_call = ToolCall(
        id="empty-1",
        name="nfc_reply",
        args={"content": ""},
    )
    empty_response = SimpleNamespace(
        payloads=[
            LLMPayload(ROLE.USER, Text("你好")),
            LLMPayload(ROLE.ASSISTANT, empty_call),
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(
                    value="内容为空，未发送",
                    call_id="empty-1",
                    name="nfc_reply",
                ),
            ),
        ],
        call_list=[empty_call],
        message="",
    )
    empty_response.add_payload = empty_response.payloads.append
    plain_response = SimpleNamespace(
        payloads=[
            LLMPayload(ROLE.USER, Text("你好")),
            LLMPayload(ROLE.ASSISTANT, Text("重试后的自然语言")),
        ],
        call_list=[],
        message="重试后的自然语言",
    )
    plain_response.add_payload = plain_response.payloads.append
    responses = [empty_response, plain_response]
    decisions = [
        Decision(
            actions=[{"type": "nfc_reply", "content": []}],
            has_reply_action=True,
            has_meaningful_action=True,
        ),
        Decision(),
    ]
    sent_texts: list[str] = []
    committed: list[Decision] = []

    async def fake_send(response, max_retries):
        del response, max_retries
        return responses.pop(0)

    async def fake_parse(*args, **kwargs):
        del args, kwargs
        return decisions.pop(0)

    async def fake_execute_reply(content, config, trigger_msg, reply_to):
        del config, trigger_msg, reply_to
        sent_texts.append(content)
        return True

    async def fake_commit(*args, **kwargs):
        del kwargs
        committed.append(args[1])
        return TurnControlResult(
            next_signal=Stop(0),
            return_after_yield=True,
        )

    initial_response = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, Text("你好"))],
        call_list=[],
        message="",
    )
    session = SimpleNamespace(update_chain=lambda *args, **kwargs: None)
    config = SimpleNamespace(
        general=SimpleNamespace(
            enabled=True,
            native_multimodal=False,
            models=[],
            model_task="actor",
            perception_extract_task="sub_actor",
            temperature=0.7,
            max_tokens=1024,
            max_compat_retries=0,
            max_empty_reply_retries=1,
            max_consecutive_llm_failures=3,
        ),
        buffer=SimpleNamespace(interrupt_enabled=False),
        debug=SimpleNamespace(show_prompt=False),
        prompt=SimpleNamespace(max_context_payloads=20, summary_enabled=False),
    )

    async def resolve(value):
        return value

    chatter = SimpleNamespace(
        stream_id="stream-test",
        _get_config=lambda: config,
        _get_session=lambda: resolve(session),
        _build_initial_context=lambda *args: resolve(
            (initial_response, None, SimpleNamespace(), SimpleNamespace(), False)
        ),
        _send_with_perceive_loop=fake_send,
        _get_virtual_trigger_message=lambda: resolve(SimpleNamespace()),
        _execute_reply=fake_execute_reply,
        fetch_unreads=lambda **kwargs: resolve(("", [])),
        flush_unreads=lambda messages: resolve(None),
        _save_session=lambda current_session: resolve(None),
        run_tool_call=lambda *args, **kwargs: None,
    )

    monkeypatch.setattr(
        "src.app.plugin_system.api.stream_api.activate_stream",
        lambda stream_id: resolve(SimpleNamespace(stream_id=stream_id)),
    )
    monkeypatch.setattr(orchestrator_module, "get_model_set_by_task", lambda task: object())
    monkeypatch.setattr(
        orchestrator_module,
        "prepare_turn_input",
        lambda *args, **kwargs: resolve(
            TurnInputResult(response=args[1], unread_msgs=[])
        ),
    )
    monkeypatch.setattr(orchestrator_module, "parse_response_decision", fake_parse)
    monkeypatch.setattr(orchestrator_module, "commit_turn_decision", fake_commit)
    monkeypatch.setattr(
        orchestrator_module,
        "extract_reply_from_perception",
        lambda *args, **kwargs: resolve("提取后的可发送回复"),
    )

    yielded = [signal async for signal in execute_orchestrator(chatter)]

    assert sent_texts == ["提取后的可发送回复"]
    assert committed[0].visible_reply_segments == ["提取后的可发送回复"]
    assert len(yielded) == 1
    assert isinstance(yielded[0], Stop)
