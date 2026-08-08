"""NFC 请求体快照处理器测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.handlers.request_snapshot_handler import (
    NFCRequestSnapshotHandler,
)
from src.core.components.types import EventType
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolResult, capture_payload_snapshot


class _SessionStore:
    """提供快照处理器所需的最小会话存储。"""

    def __init__(self, session: NFCSession) -> None:
        self.session = session
        self.saved = 0

    @asynccontextmanager
    async def lock(self, stream_id: str) -> AsyncIterator[None]:
        """提供无竞争的每流锁。"""
        assert stream_id == self.session.stream_id
        yield

    async def get_or_create(self, stream_id: str) -> NFCSession:
        """返回测试会话。"""
        assert stream_id == self.session.stream_id
        return self.session

    async def save(self, session: NFCSession) -> None:
        """记录保存行为。"""
        assert session is self.session
        self.saved += 1


def _params(stream_id: str, payloads: list[LLMPayload]) -> dict[str, object]:
    """构造一次 NFC 的 before_llm_request 事件参数。"""
    return {
        "request_name": "neo_fatum_chatter",
        "meta_data": {"stream_id": stream_id},
        "payloads": payloads,
    }


@pytest.mark.asyncio
async def test_request_snapshot_restores_once_and_captures_final_payloads() -> None:
    """冷启动首个 NFC 请求应恢复历史，后续请求不得重复注入。"""
    stream_id = "snapshot-private"
    session = NFCSession(user_id="user", stream_id=stream_id)
    session.request_snapshot = capture_payload_snapshot(
        stream_id,
        [
            LLMPayload(ROLE.USER, Text("旧用户消息")),
            LLMPayload(
                ROLE.ASSISTANT,
                ToolCall("call-1", "action-nfc_reply", {"content": "旧回复"}),
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult("已发送", call_id="call-1"),
            ),
        ],
    ).to_dict()
    plugin = SimpleNamespace(
        config=SimpleNamespace(
            prompt=SimpleNamespace(request_snapshot_enabled=True),
        ),
        session_store=_SessionStore(session),
    )
    handler = NFCRequestSnapshotHandler(plugin=plugin)
    handler._restored_streams.clear()

    _, first = await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _params(
            stream_id,
            [
                LLMPayload(ROLE.SYSTEM, Text("当前系统提示")),
                LLMPayload(ROLE.TOOL, []),
                LLMPayload(ROLE.USER, Text("本轮新消息")),
            ],
        ),
    )

    assert [payload.role for payload in first["payloads"]] == [
        ROLE.SYSTEM,
        ROLE.TOOL,
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.USER,
    ]
    assert getattr(session, "_nfc_request_snapshot_restored") is True
    assert plugin.session_store.saved == 1

    _, second = await handler.execute(
        EventType.BEFORE_LLM_REQUEST.value,
        _params(stream_id, [LLMPayload(ROLE.USER, Text("续轮消息"))]),
    )

    assert second["payloads"] == [LLMPayload(ROLE.USER, Text("续轮消息"))]
    assert plugin.session_store.saved == 2