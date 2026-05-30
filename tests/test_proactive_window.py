"""主动预约窗口测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from neo_fatum_chatter.domain.decision import ProactiveSchedule
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.protocol.decision_parser import build_decision
from neo_fatum_chatter.services.proactive_service import ProactiveService
from neo_fatum_chatter.thinker.proactive import ProactiveThinker
from neo_fatum_chatter.prompts.modules import build_proactive_context


def test_session_sets_and_clears_proactive_window():
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")

    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=0.8,
        check_interval_seconds=600.0,
    )

    assert session.scheduled_proactive_start_at == 1_000.0
    assert session.scheduled_proactive_end_at == 2_000.0
    assert session.scheduled_proactive_context == "晚上问问状态"
    assert session.scheduled_proactive_interest == 0.8
    assert session.scheduled_proactive_check_interval == 600.0
    assert session.scheduled_proactive_check_count == 0

    session.clear_scheduled_proactive()

    assert session.scheduled_proactive_at is None
    assert session.scheduled_proactive_reason == ""
    assert session.scheduled_proactive_start_at is None
    assert session.scheduled_proactive_end_at is None
    assert session.scheduled_proactive_context == ""
    assert session.scheduled_proactive_interest == 0.0
    assert session.scheduled_proactive_last_check_at is None
    assert session.scheduled_proactive_check_interval == 600.0
    assert session.scheduled_proactive_check_count == 0


def test_session_window_round_trips_through_dict():
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=0.8,
        check_interval_seconds=600.0,
    )
    session.scheduled_proactive_last_check_at = 1_100.0
    session.scheduled_proactive_check_count = 2

    loaded = NFCSession.from_dict(session.to_dict())

    assert loaded.scheduled_proactive_start_at == 1_000.0
    assert loaded.scheduled_proactive_end_at == 2_000.0
    assert loaded.scheduled_proactive_context == "晚上问问状态"
    assert loaded.scheduled_proactive_interest == 0.8
    assert loaded.scheduled_proactive_last_check_at == 1_100.0
    assert loaded.scheduled_proactive_check_interval == 600.0
    assert loaded.scheduled_proactive_check_count == 2


def test_session_window_clamps_interest_and_interval():
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")

    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        interest=9.0,
        check_interval_seconds=1.0,
    )
    assert session.scheduled_proactive_interest == 1.0
    assert session.scheduled_proactive_check_interval == 300.0

    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        interest=-9.0,
        check_interval_seconds=9_999.0,
    )
    assert session.scheduled_proactive_interest == 0.0
    assert session.scheduled_proactive_check_interval == 900.0


def test_single_and_window_schedules_replace_each_other():
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")

    session.set_scheduled_proactive(500.0, reason="旧预约")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="窗口预约",
        interest=0.8,
        check_interval_seconds=600.0,
    )

    assert session.scheduled_proactive_at is None
    assert session.scheduled_proactive_reason == ""
    assert session.scheduled_proactive_start_at == 1_000.0

    session.set_scheduled_proactive(3_000.0, reason="单点预约")

    assert session.scheduled_proactive_at == 3_000.0
    assert session.scheduled_proactive_reason == "单点预约"
    assert session.scheduled_proactive_start_at is None
    assert session.scheduled_proactive_end_at is None
    assert session.scheduled_proactive_context == ""
    assert session.scheduled_proactive_interest == 0.0
    assert session.scheduled_proactive_last_check_at is None
    assert session.scheduled_proactive_check_interval == 600.0
    assert session.scheduled_proactive_check_count == 0


def test_session_deserialization_clamps_invalid_proactive_window_boundaries():
    data = {
        "user_id": "u1",
        "stream_id": "s1",
        "platform": "qq",
        "scheduled_proactive_start_at": 1_000.0,
        "scheduled_proactive_end_at": 2_000.0,
        "scheduled_proactive_interest": 99,
        "scheduled_proactive_check_interval": -1,
        "scheduled_proactive_check_count": -5,
    }

    loaded = NFCSession.from_dict(data)

    assert loaded.scheduled_proactive_interest == 1.0
    assert loaded.scheduled_proactive_check_interval == 300.0
    assert loaded.scheduled_proactive_check_count == 0

    data["scheduled_proactive_interest"] = -5
    data["scheduled_proactive_check_interval"] = 9999

    loaded = NFCSession.from_dict(data)

    assert loaded.scheduled_proactive_interest == 0.0
    assert loaded.scheduled_proactive_check_interval == 900.0
    assert loaded.scheduled_proactive_check_count == 0


def test_session_deserialization_clears_reversed_window():
    loaded = NFCSession.from_dict(
        {
            "user_id": "u1",
            "stream_id": "s1",
            "platform": "qq",
            "scheduled_proactive_start_at": 2_000.0,
            "scheduled_proactive_end_at": 1_000.0,
            "scheduled_proactive_context": "反向窗口",
            "scheduled_proactive_interest": 0.9,
        }
    )

    assert loaded.scheduled_proactive_start_at is None
    assert loaded.scheduled_proactive_end_at is None
    assert loaded.scheduled_proactive_context == ""
    assert loaded.scheduled_proactive_interest == 0.0


def test_session_rejects_reversed_proactive_window():
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")

    with pytest.raises(ValueError):
        session.set_scheduled_proactive_window(
            start_at=20.0,
            end_at=10.0,
            context="非法窗口",
            interest=0.8,
            check_interval_seconds=600.0,
        )


@dataclass
class _Call:
    name: str
    args: dict
    id: str = "call-1"


class _Response:
    def __init__(self, calls):
        self.tool_calls = calls


class _Result:
    thought = ""
    mood = ""
    expected_reaction = ""
    max_wait_seconds = 0.0
    actions = []
    has_reply = False
    has_do_nothing = False
    has_meaningful_action = True
    has_info_tool = False


def test_decision_parser_extracts_schedule_window():
    response = _Response([
        _Call(
            name="schedule_proactive",
            args={
                "start_at": "2026-05-30 20:00",
                "end_at": "2026-05-30 22:00",
                "context": "晚上问问状态",
                "interest": 0.9,
            },
        )
    ])

    decision = build_decision(_Result(), response)

    assert decision.proactive_schedule is not None
    assert decision.proactive_schedule.start_at == "2026-05-30 20:00"
    assert decision.proactive_schedule.end_at == "2026-05-30 22:00"
    assert decision.proactive_schedule.context == "晚上问问状态"
    assert decision.proactive_schedule.interest == 0.9


def test_proactive_service_rejects_window_with_less_than_minimum_remaining_time():
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    now_ts = datetime.strptime("2026-05-30 20:00", "%Y-%m-%d %H:%M").timestamp()

    with pytest.raises(ValueError):
        ProactiveService.apply_schedule(
            session,
            ProactiveSchedule(
                start_at="2026-05-30 19:55",
                end_at="2026-05-30 20:10",
                context="短剩余窗口",
                interest=0.9,
            ),
            now_ts=now_ts,
        )

    assert session.scheduled_proactive_start_at is None
    assert session.scheduled_proactive_end_at is None
    assert session.scheduled_proactive_context == ""


    ProactiveService.apply_schedule(
        session,
        ProactiveSchedule(
            start_at="2026-05-30 20:00",
            end_at="2026-05-30 22:00",
            context="晚上问问状态",
            interest=0.9,
        ),
        now_ts=1_000.0,
    )

    assert session.scheduled_proactive_start_at is not None
    assert session.scheduled_proactive_end_at is not None
    assert session.scheduled_proactive_end_at > session.scheduled_proactive_start_at
    assert session.scheduled_proactive_context == "晚上问问状态"
    assert session.scheduled_proactive_interest == 0.9


# --- Task 3: 后台窗口检查测试 ---


class _ProactiveConfig:
    enabled = True
    silence_threshold = 7200
    trigger_probability = 0.0
    min_interval = 1800
    quiet_hours_start = "23:00"
    quiet_hours_end = "07:00"
    check_interval = 60
    window_max_attempts = 3


class _Config:
    proactive = _ProactiveConfig()


class _Store:
    def __init__(self, sessions):
        self._sessions = sessions

    def get_all_cached(self):
        return self._sessions

    async def list_all_stream_ids(self):
        return list(self._sessions)

    async def peek(self, stream_id):
        return self._sessions.get(stream_id)


async def _always_true(*args, **kwargs):
    return True


async def _always_false(*args, **kwargs):
    return False


@pytest.mark.asyncio
async def test_window_check_triggers_when_sub_actor_approves(monkeypatch):
    """窗口内且 sub-actor 返回 True 时触发。"""
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=1.0,
        check_interval_seconds=300.0,
    )

    store = _Store({"s1": session})
    thinker = ProactiveThinker(_Config(), store)

    monkeypatch.setattr("time.time", lambda: 1_100.0)
    monkeypatch.setattr(thinker, "_ask_sub_actor", _always_true)

    triggered = await thinker.check_all_sessions()

    assert triggered == ["s1"]
    assert session.scheduled_proactive_last_check_at == 1_100.0
    assert session.scheduled_proactive_check_count == 1


@pytest.mark.asyncio
async def test_window_check_does_not_trigger_when_sub_actor_rejects(monkeypatch):
    """窗口内但 sub-actor 返回 False 时不触发。"""
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=1.0,
        check_interval_seconds=300.0,
    )

    store = _Store({"s1": session})
    thinker = ProactiveThinker(_Config(), store)

    monkeypatch.setattr("time.time", lambda: 1_100.0)
    monkeypatch.setattr(thinker, "_ask_sub_actor", _always_false)

    triggered = await thinker.check_all_sessions()

    assert triggered == []
    assert session.scheduled_proactive_last_check_at == 1_100.0
    assert session.scheduled_proactive_check_count == 1


@pytest.mark.asyncio
async def test_window_check_does_not_update_state_when_probability_gate_misses(monkeypatch):
    """概率门未命中时不更新窗口检查状态。"""
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="ctx",
        interest=0.0,
        check_interval_seconds=300.0,
    )

    store = _Store({"s1": session})
    thinker = ProactiveThinker(_Config(), store)

    async def _raise_if_called(*args, **kwargs):
        raise AssertionError("_ask_sub_actor should not be called")

    monkeypatch.setattr("time.time", lambda: 1_100.0)
    monkeypatch.setattr("random.random", lambda: 0.5)
    monkeypatch.setattr(thinker, "_ask_sub_actor", _raise_if_called)

    triggered = await thinker.check_all_sessions()

    assert triggered == []
    assert session.scheduled_proactive_last_check_at is None
    assert session.scheduled_proactive_check_count == 0


@pytest.mark.asyncio
async def test_window_check_respects_max_attempts(monkeypatch):
    """达到 max_attempts 后不再触发。"""
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=1.0,
        check_interval_seconds=300.0,
    )
    session.scheduled_proactive_check_count = 3  # == window_max_attempts

    store = _Store({"s1": session})
    thinker = ProactiveThinker(_Config(), store)

    monkeypatch.setattr("time.time", lambda: 1_100.0)
    monkeypatch.setattr(thinker, "_ask_sub_actor", _always_true)

    triggered = await thinker.check_all_sessions()

    assert triggered == []


@pytest.mark.asyncio
async def test_window_check_respects_interval(monkeypatch):
    """未过 check_interval 时不触发。"""
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=1.0,
        check_interval_seconds=600.0,
    )
    session.scheduled_proactive_last_check_at = 1_050.0  # 50s ago

    store = _Store({"s1": session})
    thinker = ProactiveThinker(_Config(), store)

    monkeypatch.setattr("time.time", lambda: 1_100.0)
    monkeypatch.setattr(thinker, "_ask_sub_actor", _always_true)

    triggered = await thinker.check_all_sessions()

    assert triggered == []


# --- Task 4: 窗口上下文和主 actor 结果处理测试 ---


def test_handle_window_actor_result_decays_interest_to_floor_and_clears_on_reply():
    """窗口唤醒后主 actor 不回复则衰减兴趣，回复则清除窗口。"""
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=1.0,
        check_interval_seconds=600.0,
    )

    ProactiveService.handle_window_actor_result(
        session,
        replied=False,
        decay=0.35,
        floor=0.1,
    )

    assert session.scheduled_proactive_interest == 0.35

    ProactiveService.handle_window_actor_result(
        session,
        replied=False,
        decay=0.1,
        floor=0.1,
    )

    assert session.scheduled_proactive_interest == 0.1

    ProactiveService.handle_window_actor_result(
        session,
        replied=True,
        decay=0.35,
        floor=0.1,
    )

    assert session.scheduled_proactive_start_at is None
    assert session.scheduled_proactive_end_at is None
    assert session.scheduled_proactive_context == ""
    assert session.scheduled_proactive_interest == 0.0


@pytest.mark.asyncio
async def test_build_proactive_context_includes_schedule_window_context():
    """主动发起上下文包含预约理由、上下文和格式化窗口。"""
    result = await build_proactive_context(
        silence_minutes=45,
        recent_activity="用户: 最近在准备晚饭",
        scheduled_reason="记得关心晚饭",
        scheduled_context="晚上问问状态",
        scheduled_start_at=datetime.strptime("2026-05-30 20:00", "%Y-%m-%d %H:%M").timestamp(),
        scheduled_end_at=datetime.strptime("2026-05-30 22:00", "%Y-%m-%d %H:%M").timestamp(),
    )

    assert "你之前为这次主动发起做了预约" in result
    assert "预约理由：记得关心晚饭" in result
    assert "预约上下文：晚上问问状态" in result
    assert "预约窗口：2026-05-30 20:00 - 2026-05-30 22:00" in result


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SavingStore(_Store):
    def __init__(self, sessions):
        super().__init__(sessions)
        self.saved = []

    def lock(self, stream_id):
        return _Lock()

    async def get(self, stream_id):
        return self._sessions.get(stream_id)

    async def save(self, session):
        self.saved.append(session)


@pytest.mark.asyncio
async def test_mark_triggered_preserves_window_and_returns_payload(monkeypatch):
    """窗口触发返回完整 payload 且保留窗口等待主 actor 结果处理。"""
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=0.8,
        check_interval_seconds=600.0,
    )
    session.scheduled_proactive_reason = "记得问候"
    store = _SavingStore({"s1": session})
    thinker = ProactiveThinker(_Config(), store)

    monkeypatch.setattr("time.time", lambda: 1_100.0)

    payload = await thinker.mark_triggered("s1")

    assert payload == {
        "scheduled_reason": "记得问候",
        "scheduled_context": "晚上问问状态",
        "scheduled_start_at": 1_000.0,
        "scheduled_end_at": 2_000.0,
        "scheduled_interest": 0.8,
        "from_window": True,
    }
    assert session.last_proactive_at == 1_100.0
    assert session.scheduled_proactive_start_at == 1_000.0
    assert session.scheduled_proactive_end_at == 2_000.0
    assert session.scheduled_proactive_context == "晚上问问状态"
    assert store.saved == [session]


def test_orchestrator_records_window_actor_result_after_fallback_reply():
    """兜底纯文本回复也会按主 actor 已回复处理预约窗口。"""
    session = NFCSession(user_id="u1", stream_id="s1", platform="qq")
    session.set_scheduled_proactive_window(
        start_at=1_000.0,
        end_at=2_000.0,
        context="晚上问问状态",
        interest=0.8,
        check_interval_seconds=600.0,
    )

    class _Decision:
        has_reply_action = True

    class _ProactiveSettings:
        window_interest_decay = 0.35
        window_interest_floor = 0.1

    class _RuntimeConfig:
        proactive = _ProactiveSettings()

    from neo_fatum_chatter.runtime.orchestrator import _handle_proactive_window_result

    _handle_proactive_window_result(
        session,
        _Decision(),
        _RuntimeConfig(),
        {"nfc_proactive_window": True},
    )

    assert session.scheduled_proactive_start_at is None
    assert session.scheduled_proactive_end_at is None
    assert session.scheduled_proactive_context == ""
    assert session.scheduled_proactive_interest == 0.0
