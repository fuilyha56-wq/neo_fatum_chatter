"""NFC Console Go 推理字段传输适配测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletion

from neo_fatum_chatter.protocol.reasoning_transport import (
    ConsoleGoCompletionsProxy,
    _alias_completion_reasoning,
    attach_nfc_model_clients,
    send_with_nfc_model_clients,
)
from neo_fatum_chatter.services.context_sanitizer import close_pending_tool_chain
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
    close_pending_tool_chain(first_response, reason="test followup")

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