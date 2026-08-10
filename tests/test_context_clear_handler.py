"""NFC 跟随主程序清空上下文命令的回归测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from neo_fatum_chatter.domain.scene_state import SceneEvidence, SceneState
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.handlers.context_clear_handler import NFCContextClearHandler
from neo_fatum_chatter.models import WaitingConfig
from src.app.plugin_system.types import ChatStream
from src.core.components.types import EventType
from src.kernel.event import EventDecision


class _SessionStore:
    """提供清空处理器所需的最小会话存储。"""

    def __init__(self, session: NFCSession) -> None:
        self.session = session
        self.saved = 0

    @asynccontextmanager
    async def lock(self, stream_id: str) -> AsyncIterator[None]:
        """提供无竞争的每流锁。"""
        assert stream_id == self.session.stream_id
        yield

    async def get(self, stream_id: str) -> NFCSession | None:
        """返回已有测试会话。"""
        assert stream_id == self.session.stream_id
        return self.session

    async def save(self, session: NFCSession) -> None:
        """记录持久化行为。"""
        assert session is self.session
        self.saved += 1


def _dirty_session() -> NFCSession:
    """构造包含各类旧上下文的 NFC 会话。"""
    session = NFCSession(user_id="user", stream_id="stream-current", platform="qq")
    session.chain_payloads = [
        {"role": "user", "text": "旧问题", "ts": 100.0},
        {"role": "assistant", "text": "旧回答"},
    ]
    session.chain_cutoff_ts = 100.0
    session.frozen_narrative = "旧融合叙事"
    session.frozen_narrative_cutoff_ts = 100.0
    session.request_snapshot = {"payloads": ["旧请求"]}
    session.history_summary = "旧摘要"
    session.last_compress_at = 90.0
    session.compress_round_count = 3
    session.scene_state = SceneState(
        certainty="confirmed",
        location_type="home",
        evidence=[SceneEvidence(source="old", content="旧场景")],
    )
    session.mood_history = [{"mood": "旧心情", "ts": 90.0}]
    session.pending_proactive_context = "旧主动上下文"
    session.suppressed_messages = [object()]
    session.set_waiting(
        WaitingConfig(
            expected_reaction="旧预期",
            max_wait_seconds=60.0,
            started_at=50.0,
        )
    )
    session.consecutive_timeout_count = 2
    session.set_scheduled_proactive(200.0, "旧对话产生的预约")
    session.add_user_message("旧消息", "用户", "user", timestamp=100.0)
    session.add_habit("长期偏好应保留", "preference")
    session.set_proactive_enabled(False, "用户主动暂停")
    return session


def _event_params(*, success: bool = True) -> dict[str, object]:
    """构造主程序命令执行后的事件参数。"""
    return {
        "command_name": "清空上下文",
        "command_path": "清空上下文",
        "args": [],
        "success": success,
        "message": SimpleNamespace(stream_id="stream-current", platform="qq"),
    }


class _KnownSessionStore:
    """提供批量目标解析所需的已知 NFC 会话集合。"""

    def __init__(self, persisted: list[str], cached: list[str]) -> None:
        self.persisted = persisted
        self.cached = cached

    async def list_all_stream_ids(self) -> list[str]:
        """返回持久化会话 ID。"""
        return list(self.persisted)

    def get_all_cached(self) -> dict[str, object]:
        """返回内存会话 ID。"""
        return {stream_id: object() for stream_id in self.cached}


@pytest.mark.asyncio
async def test_clear_current_command_resets_nfc_context() -> None:
    """成功清空当前流后，NFC 下一轮不得恢复任何旧会话上下文。"""
    session = _dirty_session()
    store = _SessionStore(session)
    handler = NFCContextClearHandler(
        plugin=SimpleNamespace(session_store=store),
    )

    decision, _ = await handler.execute(
        EventType.AFTER_COMMAND_EXECUTE.value,
        _event_params(),
    )

    assert decision == EventDecision.SUCCESS
    assert session.chain_payloads == []
    assert session.chain_cutoff_ts == 0.0
    assert session.frozen_narrative == ""
    assert session.frozen_narrative_cutoff_ts == 0.0
    assert session.request_snapshot == {}
    assert session.history_summary == ""
    assert session.last_compress_at == 0.0
    assert session.compress_round_count == 0
    assert session.mental_log.entries == []
    assert session.scene_state == SceneState()
    assert session.mood_history == []
    assert session.pending_proactive_context == ""
    assert session.suppressed_messages == []
    assert session.is_waiting() is False
    assert session.consecutive_timeout_count == 0
    assert session.scheduled_proactive_at is None
    assert session.scheduled_proactive_reason == ""
    assert [habit["habit_text"] for habit in session.user_habits] == [
        "长期偏好应保留"
    ]
    assert session.proactive_enabled is False
    assert session.proactive_paused_reason == "用户主动暂停"
    assert store.saved == 1


@pytest.mark.asyncio
async def test_clearctx_alias_resets_current_nfc_context() -> None:
    """英文别名成功执行后应走同一 NFC 清空路径。"""
    session = _dirty_session()
    store = _SessionStore(session)
    handler = NFCContextClearHandler(
        plugin=SimpleNamespace(session_store=store),
    )
    params = _event_params()
    params["command_path"] = "clearctx"

    decision, _ = await handler.execute(
        EventType.AFTER_COMMAND_EXECUTE.value,
        params,
    )

    assert decision == EventDecision.SUCCESS
    assert session.chain_payloads == []
    assert store.saved == 1


@pytest.mark.asyncio
async def test_failed_clear_command_keeps_nfc_context() -> None:
    """权限拒绝或执行失败的清空命令不得修改 NFC 会话。"""
    session = _dirty_session()
    store = _SessionStore(session)
    handler = NFCContextClearHandler(
        plugin=SimpleNamespace(session_store=store),
    )

    decision, _ = await handler.execute(
        EventType.AFTER_COMMAND_EXECUTE.value,
        _event_params(success=False),
    )

    assert decision == EventDecision.PASS
    assert session.chain_payloads
    assert session.request_snapshot
    assert session.history_summary == "旧摘要"
    assert store.saved == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], ["stream-current"]),
        (
            ["群", "12345"],
            [ChatStream.generate_stream_id("qq", group_id="12345")],
        ),
        (
            ["私", "67890"],
            [ChatStream.generate_stream_id("qq", user_id="67890")],
        ),
        (["群"], ["group-cached", "group-persisted"]),
        (["私"], ["private-persisted"]),
        (
            ["all"],
            ["group-cached", "group-persisted", "private-persisted"],
        ),
        (
            ["全部"],
            ["group-cached", "group-persisted", "private-persisted"],
        ),
    ],
)
async def test_clear_command_routes_to_expected_nfc_sessions(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    expected: list[str],
) -> None:
    """主命令的全部附属路由应映射到相同范围的 NFC 会话。"""
    store = _KnownSessionStore(
        persisted=["group-persisted", "private-persisted"],
        cached=["group-cached"],
    )
    handler = NFCContextClearHandler(
        plugin=SimpleNamespace(session_store=store),
    )

    async def fake_get_stream_ids_from_db(chat_type: str = "") -> list[str]:
        """返回主程序中指定类型的流 ID。"""
        if chat_type == "group":
            return ["group-persisted", "group-cached", "group-without-nfc"]
        if chat_type == "private":
            return ["private-persisted", "private-without-nfc"]
        return []

    monkeypatch.setattr(
        "neo_fatum_chatter.handlers.context_clear_handler."
        "stream_api.get_stream_ids_from_db",
        fake_get_stream_ids_from_db,
    )
    params = _event_params()
    params["args"] = args

    stream_ids = await handler._resolve_nfc_stream_ids(params)

    assert stream_ids == expected