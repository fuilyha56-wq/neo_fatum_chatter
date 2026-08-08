"""NFC 习惯纠正与主动联系控制 Action 测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from neo_fatum_chatter.actions.proactive_control import (
    QueryProactiveStatusAction,
    SetProactiveEnabledAction,
)
from neo_fatum_chatter.actions.query_habits import QueryHabitsAction
from neo_fatum_chatter.actions.remove_habit import RemoveHabitAction
from neo_fatum_chatter.actions.update_habit import UpdateHabitAction
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.thinker.proactive import ProactiveThinker


class _SessionStore:
    """提供控制 Action 所需的最小会话存储。"""

    def __init__(self, session: NFCSession) -> None:
        self.session = session
        self.saved = 0

    @asynccontextmanager
    async def lock(self, stream_id: str) -> AsyncIterator[None]:
        """提供无竞争的每流锁。"""
        assert stream_id == self.session.stream_id
        yield

    async def get_or_create(self, stream_id: str) -> NFCSession:
        """返回唯一测试会话。"""
        assert stream_id == self.session.stream_id
        return self.session

    async def peek(self, stream_id: str) -> NFCSession | None:
        """返回唯一测试会话。"""
        assert stream_id == self.session.stream_id
        return self.session

    async def save(self, session: NFCSession) -> None:
        """记录 Action 触发的持久化。"""
        assert session is self.session
        self.saved += 1


def _plugin(session: NFCSession) -> SimpleNamespace:
    """构造当前私聊的插件替身。"""
    return SimpleNamespace(
        session_store=_SessionStore(session),
        config=SimpleNamespace(
            proactive=SimpleNamespace(
                enabled=True,
                min_interval=1800,
                silence_threshold=0,
                trigger_probability=1.0,
                quiet_hours_start="00:00",
                quiet_hours_end="00:00",
                activity_service_signature="",
                activity_service_method="is_good_time",
            )
        ),
    )


def _chat_stream(stream_id: str) -> SimpleNamespace:
    """构造 Action 需要的最小聊天流。"""
    return SimpleNamespace(stream_id=stream_id, chat_type="private")


@pytest.mark.asyncio
async def test_habit_actions_query_update_and_remove() -> None:
    """查询暴露 ID 后，Action 应能精准更正并删除习惯。"""
    session = NFCSession(user_id="user", stream_id="private-1")
    session.add_habit("通常 23 点睡觉", "sleep")
    plugin = _plugin(session)
    chat_stream = _chat_stream(session.stream_id)

    success, listing = await QueryHabitsAction(
        chat_stream=chat_stream,
        plugin=plugin,
    ).execute()

    habit_id = session.get_habits()[0]["id"]
    assert success is True
    assert habit_id in listing

    success, _ = await UpdateHabitAction(
        chat_stream=chat_stream,
        plugin=plugin,
    ).execute(habit_id=habit_id, habit_text="通常 24 点睡觉")

    assert success is True
    assert session.get_habits()[0]["habit_text"] == "通常 24 点睡觉"

    success, _ = await RemoveHabitAction(
        chat_stream=chat_stream,
        plugin=plugin,
    ).execute(habit_id=habit_id)

    assert success is True
    assert session.get_habits() == []
    assert plugin.session_store.saved == 2


@pytest.mark.asyncio
async def test_paused_session_skips_proactive_trigger() -> None:
    """会话暂停后，Action 与思考器都不应发起主动联系。"""
    session = NFCSession(user_id="user", stream_id="private-1")
    plugin = _plugin(session)
    chat_stream = _chat_stream(session.stream_id)

    success, message = await SetProactiveEnabledAction(
        chat_stream=chat_stream,
        plugin=plugin,
    ).execute(enabled=False, reason="对方希望安静")

    assert success is True
    assert "暂停" in message
    thinker = ProactiveThinker(plugin.config, plugin.session_store)
    assert await thinker._check_and_trigger(session.stream_id, session) is False

    success, status = await QueryProactiveStatusAction(
        chat_stream=chat_stream,
        plugin=plugin,
    ).execute()

    assert success is True
    assert "对方希望安静" in status