"""NFC 摘要压缩遵守上下文清空水位的回归测试。"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.services import compressor


class _AwaitableSummary:
    """模拟需要二次 await 的 LLM 文本响应。"""

    def __init__(self, summary: str, before_return: Any = None) -> None:
        self.summary = summary
        self.before_return = before_return

    def __await__(self):
        async def _resolve() -> str:
            if callable(self.before_return):
                self.before_return()
            return self.summary

        return _resolve().__await__()


class _Request:
    """记录压缩请求 payload 并返回固定摘要。"""

    def __init__(self, response: _AwaitableSummary) -> None:
        self.response = response
        self.payloads: list[Any] = []

    def add_payload(self, payload: Any) -> None:
        """保存请求 payload。"""
        self.payloads.append(payload)

    async def send(self) -> _AwaitableSummary:
        """返回可等待的摘要响应。"""
        return self.response


class _PromptBuilder:
    """提供摘要器所需的最小提示词构建接口。"""

    async def build_system_prompt(self, chat_stream: Any) -> str:
        """返回固定系统提示。"""
        return "system"


def _message(text: str, timestamp: float) -> SimpleNamespace:
    """构造摘要器可读取的消息对象。"""
    return SimpleNamespace(
        time=timestamp,
        processed_plain_text=text,
        sender_id="user",
        sender_name="用户",
        message_id=f"message-{timestamp}",
    )


def _config() -> SimpleNamespace:
    """构造摘要器所需配置。"""
    return SimpleNamespace(
        prompt=SimpleNamespace(compress_days_window=1.0),
        general=SimpleNamespace(model_task="actor"),
    )


@pytest.mark.asyncio
async def test_compressor_ignores_messages_before_context_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清空后的首次摘要不得读取清空水位之前的数据库消息。"""
    now = time.time()
    session = NFCSession(user_id="user", stream_id="stream-1")
    session.context_cleared_at = now - 30.0
    request = _Request(_AwaitableSummary("新摘要"))

    async def fake_get_stream_messages(
        stream_id: str,
        limit: int,
    ) -> list[SimpleNamespace]:
        """同时返回清空前后的消息。"""
        assert stream_id == "stream-1"
        assert limit == 10000
        return [
            _message("清空前旧消息", now - 60.0),
            _message("清空后新消息", now - 10.0),
        ]

    monkeypatch.setattr(compressor, "get_stream_messages", fake_get_stream_messages)
    monkeypatch.setattr(compressor, "get_model_set_by_task", lambda _: object())
    monkeypatch.setattr(
        compressor,
        "create_compress_request",
        lambda model_set, stream_id: request,
    )

    await compressor.compress_history(
        session,
        _PromptBuilder(),
        _config(),
        SimpleNamespace(bot_id="", partner_name="用户"),
    )

    prompt_text = request.payloads[-1].content[0].text
    assert "清空后新消息" in prompt_text
    assert "清空前旧消息" not in prompt_text
    assert session.history_summary == "新摘要"


@pytest.mark.asyncio
async def test_compressor_discards_result_if_context_cleared_during_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清空前启动的压缩任务不得在清空后回写旧摘要。"""
    now = time.time()
    session = NFCSession(user_id="user", stream_id="stream-1")

    def clear_during_request() -> None:
        session.reset_context(now + 1.0)

    request = _Request(
        _AwaitableSummary("迟到旧摘要", before_return=clear_during_request)
    )

    async def fake_get_stream_messages(
        stream_id: str,
        limit: int,
    ) -> list[SimpleNamespace]:
        """返回一条压缩任务启动时可见的消息。"""
        return [_message("旧消息", now - 10.0)]

    monkeypatch.setattr(compressor, "get_stream_messages", fake_get_stream_messages)
    monkeypatch.setattr(compressor, "get_model_set_by_task", lambda _: object())
    monkeypatch.setattr(
        compressor,
        "create_compress_request",
        lambda model_set, stream_id: request,
    )

    await compressor.compress_history(
        session,
        _PromptBuilder(),
        _config(),
        SimpleNamespace(bot_id="", partner_name="用户"),
    )

    assert session.history_summary == ""