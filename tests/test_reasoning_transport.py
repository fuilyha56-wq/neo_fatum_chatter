"""NFC Console Go 推理字段传输适配测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openai.types.chat import ChatCompletion

import neo_fatum_chatter.protocol.reasoning_transport as reasoning_transport
from neo_fatum_chatter.protocol.reasoning_transport import (
    ConsoleGoCompletionsProxy,
    _alias_completion_reasoning,
    attach_nfc_model_clients,
    send_with_nfc_model_clients,
)
from src.kernel.llm import LLMPayload, LLMRequest, ROLE, Text, ToolResult
from src.kernel.llm.model_client import OpenAIChatClient


@pytest.mark.asyncio
async def test_console_go_proxy_translates_reasoning_both_ways() -> None:
    """请求使用 reasoning_text，响应回填核心可识别的 reasoning_content。"""
    message = SimpleNamespace(
        content="",
        reasoning_text="provider reasoning",
        reasoning_content=None,
        tool_calls=None,
    )
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
    )
    upstream = SimpleNamespace(create=AsyncMock(return_value=completion))
    proxy = ConsoleGoCompletionsProxy(upstream)

    result = await proxy.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "previous reasoning",
            },
        ],
    )

    sent_messages = upstream.create.await_args.kwargs["messages"]
    assert sent_messages[1]["reasoning_text"] == "previous reasoning"
    assert "reasoning_content" not in sent_messages[1]
    assert result.choices[0].message.reasoning_content == "provider reasoning"


@pytest.mark.asyncio
async def test_console_go_proxy_logs_only_structure_not_message_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Console Go 诊断日志不得包含请求或响应正文。"""
    private_text = "private-message-and-reasoning"
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=private_text,
                    reasoning_text=private_text,
                    reasoning_content=None,
                    tool_calls=[SimpleNamespace()],
                ),
            )
        ]
    )
    upstream = SimpleNamespace(create=AsyncMock(return_value=completion))
    info = Mock()
    debug = Mock()
    warning = Mock()
    monkeypatch.setattr(
        reasoning_transport,
        "logger",
        SimpleNamespace(info=info, debug=debug, warning=warning),
    )

    proxy = ConsoleGoCompletionsProxy(upstream)
    await proxy.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "user", "content": private_text},
            {
                "role": "assistant",
                "content": private_text,
                "reasoning_content": private_text,
                "tool_calls": [
                    {
                        "id": "call_private",
                        "type": "function",
                        "function": {
                            "name": "private_tool",
                            "arguments": private_text,
                        },
                    }
                ],
            },
        ],
        extra_body={"enable_thinking": True},
    )

    assert info.call_count == 0
    assert debug.call_count == 2
    assert warning.call_count == 0
    assert private_text not in repr(debug.call_args_list)

    request_metadata = debug.call_args_list[0].kwargs
    assert request_metadata["extra_body_keys"] == ["enable_thinking"]
    assert request_metadata["message_summary"][1]["reasoning_text_present"] is True
    assert request_metadata["message_summary"][1]["reasoning_text_length"] == len(
        private_text
    )
    assert request_metadata["message_summary"][1]["tool_call_count"] == 1

    response_metadata = debug.call_args_list[1].kwargs
    upstream_summary = response_metadata["upstream_choice_summary"][0]
    adapted_summary = response_metadata["adapted_choice_summary"][0]
    assert upstream_summary["reasoning_text_present"] is True
    assert upstream_summary["reasoning_content_present"] is False
    assert adapted_summary["reasoning_content_present"] is True
    assert upstream_summary["reasoning_text_length"] == len(private_text)


@pytest.mark.asyncio
async def test_console_go_proxy_prepares_missing_reasoning_tool_followup_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少真实 reasoning 的工具事务应在首次请求前局部降级。"""
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content="done",
                    reasoning_text=None,
                    reasoning_content=None,
                    tool_calls=None,
                ),
            )
        ]
    )
    upstream = SimpleNamespace(create=AsyncMock(return_value=completion))
    info = Mock()
    debug = Mock()
    warning = Mock()
    monkeypatch.setattr(
        reasoning_transport,
        "logger",
        SimpleNamespace(info=info, debug=debug, warning=warning),
    )
    proxy = ConsoleGoCompletionsProxy(upstream)
    tools = [{"type": "function", "function": {"name": "get_status"}}]
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "old private reasoning",
            "tool_calls": [
                {
                    "id": "call_old",
                    "type": "function",
                    "function": {
                        "name": "old_tool",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": "old result must stay in history",
            "tool_call_id": "call_old",
        },
        {"role": "user", "content": "turn on the speaker"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_status",
                    "type": "function",
                    "function": {
                        "name": "get_status",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": "speaker is offline",
            "tool_call_id": "call_status",
        },
    ]

    result = await proxy.create(
        model="deepseek-v4-flash",
        messages=messages,
        tools=tools,
        extra_body={"enable_thinking": True},
    )

    assert result is completion
    assert upstream.create.await_count == 1
    sent_params = upstream.create.await_args.kwargs
    sent_messages = sent_params["messages"]
    assert [message["role"] for message in sent_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
        "user",
    ]
    assert sent_messages[1]["content"] == "old request"
    assert sent_messages[2]["reasoning_text"] == "old private reasoning"
    assert sent_messages[3]["content"] == "old result must stay in history"
    assert sent_messages[4]["content"] == "turn on the speaker"
    assert "get_status" in sent_messages[-1]["content"]
    assert "speaker is offline" in sent_messages[-1]["content"]
    assert sent_params["tools"] is tools
    assert sent_params["extra_body"] == {"enable_thinking": True}
    assert warning.call_count == 0
    assert "reasoning_text" not in messages[5]
    assert messages[6]["role"] == "tool"


@pytest.mark.asyncio
async def test_console_go_proxy_does_not_retry_other_bad_requests() -> None:
    """任意上游 400 都不得在 adapter 内部改写历史后重试。"""

    class OtherBadRequestError(Exception):
        status_code = 400

    error = OtherBadRequestError("invalid tool schema")
    upstream = SimpleNamespace(create=AsyncMock(side_effect=error))
    proxy = ConsoleGoCompletionsProxy(upstream)

    with pytest.raises(OtherBadRequestError, match="invalid tool schema"):
        await proxy.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert upstream.create.await_count == 1


def test_attach_nfc_model_clients_replaces_request_registry() -> None:
    """NFC 初始请求必须使用插件专属模型客户端 registry。"""
    request = LLMRequest(model_set=[])
    original_clients = request.clients

    attach_nfc_model_clients(request)

    assert request.clients is not original_clients
    assert request.clients is not None
    assert request.clients.openai is not None


def test_alias_completion_reasoning_supports_openai_sdk_model() -> None:
    """OpenAI SDK 模型对象也应获得 reasoning_content 别名。"""
    completion = ChatCompletion.model_validate(
        {
            "id": "test",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_text": "provider reasoning",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )

    _alias_completion_reasoning(completion)

    assert completion.choices[0].message.reasoning_content == "provider reasoning"


@pytest.mark.asyncio
async def test_tool_result_followup_passes_reasoning_text_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具执行后的第二轮请求必须原样回传首轮 reasoning_text。"""
    first_completion = ChatCompletion.model_validate(
        {
            "id": "first",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_text": "先判断是否需要分享图片",
                        "tool_calls": [
                            {
                                "id": "provider_call",
                                "type": "function",
                                "function": {
                                    "name": "share_visual",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )
    second_completion = ChatCompletion.model_validate(
        {
            "id": "second",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    create = AsyncMock(side_effect=[first_completion, second_completion])
    sdk_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(
        OpenAIChatClient,
        "_get_client",
        lambda self, **kwargs: sdk_client,
    )

    model_set = [
        {
            "api_provider": "cli",
            "base_url": "https://example.invalid/v1",
            "model_identifier": "deepseek-v4-flash",
            "api_key": "test-key",
            "client_type": "openai",
            "max_retry": 0,
            "timeout": 30,
            "retry_interval": 0,
            "price_in": 0.0,
            "price_out": 0.0,
            "temperature": 0.7,
            "max_tokens": 100,
            "extra_params": {
                "enable_thinking": False,
                "thinking": {"type": "disabled", "enabled": False},
                "reasoning_history_mode": True,
            },
        }
    ]
    request = LLMRequest(model_set, request_name="neo_fatum_chatter")
    attach_nfc_model_clients(request)
    request.add_payload(LLMPayload(ROLE.USER, Text("请分享一张图片")))

    first_response = await request.send(auto_append_response=True, stream=False)
    await first_response
    first_response.add_call_reflex(
        [
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(
                    value="图片发送完成",
                    call_id="provider_call",
                    name="share_visual",
                ),
            )
        ]
    )
    second_response = await send_with_nfc_model_clients(
        first_response,
        auto_append_response=True,
        stream=False,
    )
    await second_response

    second_messages = create.await_args_list[1].kwargs["messages"]
    assistant_message = next(
        message
        for message in second_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant_message["reasoning_text"] == "先判断是否需要分享图片"
    assert "reasoning_content" not in assistant_message