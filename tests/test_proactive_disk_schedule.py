"""磁盘 session 预约触发的回归测试。"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.thinker.proactive import ProactiveThinker


class _Store:
    """覆盖 check_all_sessions 所需的最小磁盘会话存储。"""

    def __init__(self, sessions: list[NFCSession]) -> None:
        self.sessions = {session.stream_id: session for session in sessions}
        self.saved: list[str] = []

    @asynccontextmanager
    async def lock(self, stream_id: str) -> AsyncIterator[None]:
        yield

    def get_all_cached(self) -> dict[str, NFCSession]:
        return {}

    async def list_all_stream_ids(self) -> list[str]:
        return list(self.sessions)

    async def get(self, stream_id: str) -> NFCSession | None:
        return self.sessions.get(stream_id)

    async def save(self, session: NFCSession) -> None:
        assert session is self.sessions[session.stream_id]
        self.saved.append(session.stream_id)


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        proactive=SimpleNamespace(
            enabled=True,
            min_interval=1800,
            silence_threshold=999999,
            trigger_probability=0.0,
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
            activity_service_signature="",
            activity_service_method="is_good_time",
        ),
        intent=SimpleNamespace(enabled=False, fire_threshold=0.75),
        drives=SimpleNamespace(enabled=False),
    )


@pytest.mark.asyncio
async def test_disk_sessions_without_schedule_do_not_break_check() -> None:
    """无预约的磁盘 session 不得让 scheduler 回调触发未绑定变量。"""
    plain = NFCSession(user_id="u1", stream_id="plain")
    due = NFCSession(user_id="u2", stream_id="due")
    due.scheduled_proactive_at = time.time() - 1
    due.scheduled_proactive_reason = "该问作业了"
    store = _Store([plain, due])
    thinker = ProactiveThinker(_config(), store)

    triggered = await thinker.check_all_sessions()

    assert triggered == ["due"]
    assert thinker._selected_candidates["due"].startswith("schedule:")
    assert "plain" not in store.saved
    assert "due" in store.saved


@pytest.mark.asyncio
async def test_future_disk_schedule_is_persisted_without_trigger() -> None:
    """未到时间的磁盘预约只审计候选，不触发主动联系。"""
    session = NFCSession(user_id="u", stream_id="future")
    session.scheduled_proactive_at = time.time() + 600
    store = _Store([session])
    thinker = ProactiveThinker(_config(), store)

    assert await thinker.check_all_sessions() == []
    assert thinker._selected_candidates == {}
    assert "future" in store.saved
    assert session.scheduled_proactive_at is not None
