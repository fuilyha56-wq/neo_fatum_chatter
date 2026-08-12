"""NFC 专用的 Console Go 推理字段传输适配。"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMRequest, LLMResponse
from src.kernel.llm.model_client import ModelClientRegistry, OpenAIChatClient

logger = get_logger("NFC_reasoning_transport")


def _uses_console_go_reasoning_text(model_entry: Any) -> bool:
    """判断模型是否使用 Console Go 的 ``reasoning_text`` 协议。"""
    if not isinstance(model_entry, dict):
        return False

    provider = str(model_entry.get("api_provider") or "").lower()
    model_identifier = str(model_entry.get("model_identifier") or "").lower()
    return provider in {"cli", "opencode"} and "deepseek" in model_identifier


def _translate_reasoning_messages(messages: Any) -> Any:
    """适配 Console Go 历史，避免伪造不存在的 reasoning。"""
    if not isinstance(messages, list):
        return messages

    translated: list[Any] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            translated.append(message)
            index += 1
            continue

        copied = dict(message)
        if copied.get("role") != "assistant":
            translated.append(copied)
            index += 1
            continue

        reasoning_content = copied.pop("reasoning_content", None)
        reasoning_text = copied.get("reasoning_text")
        real_reasoning = next(
            (
                value
                for value in (reasoning_text, reasoning_content)
                if isinstance(value, str) and value
            ),
            None,
        )
        if real_reasoning is not None:
            copied["reasoning_text"] = real_reasoning
            translated.append(copied)
            index += 1
            continue

        copied.pop("reasoning_text", None)
        tool_calls = copied.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            tool_messages: list[dict[str, Any]] = []
            cursor = index + 1
            while cursor < len(messages):
                candidate = messages[cursor]
                if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                    break
                tool_messages.append(candidate)
                cursor += 1

            if tool_messages:
                translated.append(
                    _build_tool_result_context_message(copied, tool_messages)
                )
                index = cursor
                continue

        content = _content_to_text(copied.get("content")).strip()
        if content:
            translated.append(
                {
                    "role": "user",
                    "content": f"上一轮模型留下的状态记录：\n{content}",
                }
            )
        index += 1

    return translated


def _build_tool_result_context_message(
    assistant_message: dict[str, Any],
    tool_messages: list[dict[str, Any]],
) -> dict[str, str]:
    """把无法回放 reasoning 的已完成工具事务表示为 user 上下文。"""
    call_names: dict[str, str] = {}
    for tool_call in assistant_message.get("tool_calls", []):
        if not isinstance(tool_call, dict):
            continue
        call_id = tool_call.get("id")
        function = tool_call.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if call_id and name:
            call_names[str(call_id)] = str(name)

    sections: list[str] = []
    assistant_content = _content_to_text(assistant_message.get("content")).strip()
    if assistant_content:
        sections.append(f"模型当时的输出：\n{assistant_content}")

    for tool_message in tool_messages:
        call_id = str(tool_message.get("tool_call_id") or "")
        name = str(tool_message.get("name") or call_names.get(call_id) or "工具")
        result_text = _content_to_text(tool_message.get("content"))
        sections.append(f"工具 {name} 已执行，结果：\n{result_text}")

    return {
        "role": "user",
        "content": (
            "以下是上一轮已经发生的工具调用结果。请基于这些结果继续当前任务，"
            "不要重复已完成的调用。\n\n" + "\n\n".join(sections)
        ),
    }


def _content_to_text(content: Any) -> str:
    """把工具结果内容转成可放入 user 消息的文本。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def _read_reasoning_text(target: Any) -> Any:
    """从字典或 SDK 模型对象中读取 reasoning_text。"""
    if isinstance(target, dict):
        return target.get("reasoning_text")
    return getattr(target, "reasoning_text", None)


def _field_names(target: Any) -> list[str]:
    """返回对象已暴露的字段名，不读取任何字段值。"""
    if isinstance(target, dict):
        return sorted(str(key) for key in target)

    names: set[str] = set()
    try:
        values = vars(target)
    except TypeError:
        values = {}
    if isinstance(values, dict):
        names.update(str(key) for key in values if not str(key).startswith("_"))

    extra = getattr(target, "__pydantic_extra__", None)
    if isinstance(extra, dict):
        names.update(str(key) for key in extra)
    return sorted(names)


def _text_length(value: Any) -> int | None:
    """返回字符串长度；非字符串值不记录长度。"""
    return len(value) if isinstance(value, str) else None


def _summarize_message(message: Any, index: int) -> dict[str, Any]:
    """生成 OpenAI 消息的脱敏结构摘要。"""
    if not isinstance(message, dict):
        return {
            "index": index,
            "message_type": type(message).__name__,
        }

    tool_calls = message.get("tool_calls")
    reasoning_text = message.get("reasoning_text")
    reasoning_content = message.get("reasoning_content")
    content = message.get("content")
    return {
        "index": index,
        "role": message.get("role"),
        "field_names": _field_names(message),
        "content_type": type(content).__name__ if content is not None else None,
        "content_length": _text_length(content),
        "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
        "tool_call_id_present": bool(message.get("tool_call_id")),
        "reasoning_text_present": "reasoning_text" in message,
        "reasoning_text_length": _text_length(reasoning_text),
        "reasoning_content_present": "reasoning_content" in message,
        "reasoning_content_length": _text_length(reasoning_content),
    }


def _summarize_messages(messages: Any) -> list[dict[str, Any]]:
    """生成请求消息列表的脱敏结构摘要。"""
    if not isinstance(messages, list):
        return [{"messages_type": type(messages).__name__}]
    return [_summarize_message(message, index) for index, message in enumerate(messages)]


def _completion_choices(completion: Any) -> list[Any]:
    """从字典或 SDK Completion 对象中提取 choices。"""
    choices = (
        completion.get("choices")
        if isinstance(completion, dict)
        else getattr(completion, "choices", None)
    )
    return choices if isinstance(choices, list) else []


def _choice_message(choice: Any) -> Any:
    """从字典或 SDK Choice 对象中提取 message/delta。"""
    if isinstance(choice, dict):
        return choice.get("message") or choice.get("delta")
    return getattr(choice, "message", None) or getattr(choice, "delta", None)


def _choice_finish_reason(choice: Any) -> Any:
    """从字典或 SDK Choice 对象中提取 finish_reason。"""
    if isinstance(choice, dict):
        return choice.get("finish_reason")
    return getattr(choice, "finish_reason", None)


def _summarize_completion(completion: Any) -> list[dict[str, Any]]:
    """生成 Completion 响应的脱敏结构摘要。"""
    summary: list[dict[str, Any]] = []
    for index, choice in enumerate(_completion_choices(completion)):
        message = _choice_message(choice)
        reasoning_text = _read_reasoning_text(message)
        reasoning_content = (
            message.get("reasoning_content")
            if isinstance(message, dict)
            else getattr(message, "reasoning_content", None)
        )
        tool_calls = (
            message.get("tool_calls")
            if isinstance(message, dict)
            else getattr(message, "tool_calls", None)
        )
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        summary.append(
            {
                "index": index,
                "finish_reason": _choice_finish_reason(choice),
                "message_field_names": _field_names(message),
                "content_type": type(content).__name__ if content is not None else None,
                "content_length": _text_length(content),
                "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
                "reasoning_text_present": reasoning_text is not None,
                "reasoning_text_length": _text_length(reasoning_text),
                "reasoning_content_present": reasoning_content is not None,
                "reasoning_content_length": _text_length(reasoning_content),
            }
        )
    return summary


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

    def __init__(self, upstream: Any, request_id: str) -> None:
        self._upstream = upstream
        self._iterator = upstream.__aiter__()
        self._request_id = request_id
        self._chunk_count = 0
        self._reasoning_chunk_count = 0
        self._reasoning_text_length = 0
        self._stream_logged = False

    def __aiter__(self) -> ConsoleGoStreamProxy:
        """返回当前异步迭代器。"""
        return self

    async def __anext__(self) -> Any:
        """读取并适配下一个流式 chunk。"""
        try:
            chunk = await anext(self._iterator)
        except StopAsyncIteration:
            self._log_stream_summary(completed=True)
            raise
        self._chunk_count += 1
        for choice in _summarize_completion(chunk):
            length = choice["reasoning_text_length"]
            if isinstance(length, int):
                self._reasoning_chunk_count += 1
                self._reasoning_text_length += length
        return _alias_completion_reasoning(chunk)

    async def aclose(self) -> None:
        """关闭上游流。"""
        self._log_stream_summary(completed=False)
        close = getattr(self._upstream, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def _log_stream_summary(self, completed: bool) -> None:
        """记录一次流式响应的脱敏汇总。"""
        if self._stream_logged:
            return
        self._stream_logged = True
        logger.debug(
            "[NFC ConsoleGo] stream response",
            request_id=self._request_id,
            completed=completed,
            chunk_count=self._chunk_count,
            reasoning_text_chunk_count=self._reasoning_chunk_count,
            reasoning_text_length=self._reasoning_text_length,
        )

    def __getattr__(self, name: str) -> Any:
        """透传其他流属性。"""
        return getattr(self._upstream, name)


class ConsoleGoCompletionsProxy:
    """拦截 chat.completions.create 的 NFC 局部代理。"""

    def __init__(self, upstream: Any) -> None:
        self._upstream = upstream

    async def _call_upstream(
        self,
        args: tuple[Any, ...],
        params: dict[str, Any],
    ) -> Any:
        """调用上游 completions，并等待异步结果。"""
        result = self._upstream.create(*args, **params)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        """发送前改名历史字段，响应后补核心兼容别名。"""
        params = dict(kwargs)
        params["messages"] = _translate_reasoning_messages(params.get("messages"))
        request_id = uuid.uuid4().hex[:12]
        extra_body = params.get("extra_body")
        logger.debug(
            "[NFC ConsoleGo] request",
            request_id=request_id,
            model=params.get("model"),
            stream=bool(params.get("stream")),
            parameter_keys=sorted(str(key) for key in params if key != "messages"),
            extra_body_keys=(
                sorted(str(key) for key in extra_body)
                if isinstance(extra_body, dict)
                else []
            ),
            message_summary=_summarize_messages(params["messages"]),
        )
        try:
            result = await self._call_upstream(args, params)
        except Exception as exc:
            logger.warning(
                "[NFC ConsoleGo] request failed",
                request_id=request_id,
                exception_type=type(exc).__name__,
                status_code=getattr(exc, "status_code", None),
            )
            raise
        if hasattr(result, "__aiter__"):
            return ConsoleGoStreamProxy(result, request_id)
        upstream_choice_summary = _summarize_completion(result)
        result = _alias_completion_reasoning(result)
        logger.debug(
            "[NFC ConsoleGo] response",
            request_id=request_id,
            upstream_choice_summary=upstream_choice_summary,
            adapted_choice_summary=_summarize_completion(result),
        )
        return result

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