"""NFC 专用的 Console Go 推理字段传输适配。"""

from __future__ import annotations

import inspect
from typing import Any

from src.kernel.llm import LLMRequest, LLMResponse
from src.kernel.llm.model_client import ModelClientRegistry, OpenAIChatClient


def _uses_console_go_reasoning_text(model_entry: Any) -> bool:
    """判断模型是否使用 Console Go 的 ``reasoning_text`` 协议。"""
    if not isinstance(model_entry, dict):
        return False

    provider = str(model_entry.get("api_provider") or "").lower()
    model_identifier = str(model_entry.get("model_identifier") or "").lower()
    return provider in {"cli", "opencode"} and "deepseek" in model_identifier


def _translate_reasoning_messages(messages: Any) -> Any:
    """把 assistant 历史中的 reasoning_content 改为 reasoning_text。"""
    if not isinstance(messages, list):
        return messages

    translated: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            translated.append(message)
            continue

        copied = dict(message)
        if copied.get("role") == "assistant":
            reasoning_content = copied.pop("reasoning_content", None)
            if "reasoning_text" not in copied and reasoning_content is not None:
                copied["reasoning_text"] = reasoning_content
        translated.append(copied)
    return translated


def _read_reasoning_text(target: Any) -> Any:
    """从字典或 SDK 模型对象中读取 reasoning_text。"""
    if isinstance(target, dict):
        return target.get("reasoning_text")
    return getattr(target, "reasoning_text", None)


def _alias_reasoning_content(target: Any) -> None:
    """为核心客户端补出其可识别的 reasoning_content 别名。"""
    reasoning_text = _read_reasoning_text(target)
    if reasoning_text is None:
        return

    if isinstance(target, dict):
        target.setdefault("reasoning_content", reasoning_text)
        return

    if getattr(target, "reasoning_content", None) is not None:
        return
    try:
        setattr(target, "reasoning_content", reasoning_text)
    except (AttributeError, TypeError, ValueError):
        extra = getattr(target, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            extra["reasoning_content"] = reasoning_text


def _alias_completion_reasoning(completion: Any) -> Any:
    """为非流响应或流式 chunk 中的 reasoning_text 建立别名。"""
    choices = (
        completion.get("choices")
        if isinstance(completion, dict)
        else getattr(completion, "choices", None)
    )
    if not isinstance(choices, list):
        return completion

    for choice in choices:
        if isinstance(choice, dict):
            target = choice.get("message") or choice.get("delta")
        else:
            target = getattr(choice, "message", None)
            if target is None:
                target = getattr(choice, "delta", None)
        if target is not None:
            _alias_reasoning_content(target)
    return completion


class ConsoleGoStreamProxy:
    """为 Console Go 流式响应逐块补 reasoning_content 别名。"""

    def __init__(self, upstream: Any) -> None:
        self._upstream = upstream
        self._iterator = upstream.__aiter__()

    def __aiter__(self) -> ConsoleGoStreamProxy:
        """返回当前异步迭代器。"""
        return self

    async def __anext__(self) -> Any:
        """读取并适配下一个流式 chunk。"""
        chunk = await anext(self._iterator)
        return _alias_completion_reasoning(chunk)

    async def aclose(self) -> None:
        """关闭上游流。"""
        close = getattr(self._upstream, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def __getattr__(self, name: str) -> Any:
        """透传其他流属性。"""
        return getattr(self._upstream, name)


class ConsoleGoCompletionsProxy:
    """拦截 chat.completions.create 的 NFC 局部代理。"""

    def __init__(self, upstream: Any) -> None:
        self._upstream = upstream

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        """发送前改名历史字段，响应后补核心兼容别名。"""
        params = dict(kwargs)
        params["messages"] = _translate_reasoning_messages(params.get("messages"))
        result = self._upstream.create(*args, **params)
        if inspect.isawaitable(result):
            result = await result
        if hasattr(result, "__aiter__"):
            return ConsoleGoStreamProxy(result)
        return _alias_completion_reasoning(result)

    def __getattr__(self, name: str) -> Any:
        """透传 completions 的其他属性。"""
        return getattr(self._upstream, name)


class _ConsoleGoChatProxy:
    """仅替换 SDK 客户端的 completions 接口。"""

    def __init__(self, upstream: Any) -> None:
        self._upstream = upstream
        self.completions = ConsoleGoCompletionsProxy(upstream.completions)

    def __getattr__(self, name: str) -> Any:
        """透传 chat 的其他属性。"""
        return getattr(self._upstream, name)


class _ConsoleGoClientProxy:
    """保持 AsyncOpenAI 其他接口不变的轻量代理。"""

    def __init__(self, upstream: Any) -> None:
        self._upstream = upstream
        self.chat = _ConsoleGoChatProxy(upstream.chat)

    def __getattr__(self, name: str) -> Any:
        """透传 SDK 客户端的其他属性。"""
        return getattr(self._upstream, name)


class ConsoleGoOpenAIChatClient(OpenAIChatClient):
    """仅用于 NFC Console Go DeepSeek 请求的 OpenAI 客户端。"""

    def _get_client(self, **kwargs: Any) -> Any:
        """包装核心创建的 AsyncOpenAI 客户端。"""
        return _ConsoleGoClientProxy(super()._get_client(**kwargs))


class NFCModelClientRegistry:
    """按模型条目选择 NFC 专用或框架默认客户端。"""

    def __init__(self, base_registry: ModelClientRegistry | None = None) -> None:
        self._base = base_registry or ModelClientRegistry()
        self.openai = ConsoleGoOpenAIChatClient()

    def get_client_for_model(self, model: dict[str, Any]) -> Any:
        """Console Go DeepSeek 使用专用客户端，其他模型走默认 registry。"""
        if _uses_console_go_reasoning_text(model):
            return self.openai
        return self._base.get_client_for_model(model)

    def get_embedding_client_for_model(self, model: dict[str, Any]) -> Any:
        """Embedding 请求继续交给框架默认 registry。"""
        return self._base.get_embedding_client_for_model(model)

    def get_rerank_client_for_model(self, model: dict[str, Any]) -> Any:
        """Rerank 请求继续交给框架默认 registry。"""
        return self._base.get_rerank_client_for_model(model)

    def get_asr_client_for_model(self, model: dict[str, Any]) -> Any:
        """ASR 请求继续交给框架默认 registry。"""
        return self._base.get_asr_client_for_model(model)


def attach_nfc_model_clients(request: Any) -> Any:
    """为单个 NFC LLMRequest 安装局部客户端 registry。"""
    current = getattr(request, "clients", None)
    if isinstance(current, NFCModelClientRegistry):
        return request
    request.clients = NFCModelClientRegistry(current)
    return request


async def send_with_nfc_model_clients(
    chain: Any,
    *,
    auto_append_response: bool = True,
    stream: bool = False,
) -> Any:
    """发送 NFC 请求，并保证 response 续轮仍沿用专属客户端。"""
    if isinstance(chain, LLMRequest):
        attach_nfc_model_clients(chain)
        return await chain.send(
            auto_append_response=auto_append_response,
            stream=stream,
        )

    if not isinstance(chain, LLMResponse):
        attach_nfc_model_clients(chain)
        return await chain.send(
            auto_append_response=auto_append_response,
            stream=stream,
        )

    if not chain._consumed:
        await chain
    if not chain._appended_to_context:
        chain.add_payload(chain.to_payload())
        chain._appended_to_context = True

    upper = getattr(chain, "_upper", chain)
    request = LLMRequest(
        chain.model_set,
        request_name=getattr(upper, "request_name", ""),
        meta_data=dict(getattr(upper, "meta_data", {}) or {}),
        context_manager=chain.context_manager,
    )
    request.payloads = list(chain.payloads)
    attach_nfc_model_clients(request)
    return await request.send(
        auto_append_response=auto_append_response,
        stream=stream,
    )