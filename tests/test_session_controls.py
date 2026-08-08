"""NFC 习惯纠正与会话级主动联系控制的状态测试。"""

from __future__ import annotations

from neo_fatum_chatter.domain.session_state import NFCSession


def test_habit_can_be_updated_and_removed_by_stable_id() -> None:
    """新习惯应带稳定 ID，允许后续精准纠正和删除。"""
    session = NFCSession(user_id="user", stream_id="stream-1")
    session.add_habit("通常 23 点睡觉", "sleep")

    habit = session.get_habits()[0]
    habit_id = habit["id"]

    assert session.update_habit(
        habit_id,
        habit_text="通常 24 点睡觉",
        category="routine",
    ) is True
    assert session.get_habits()[0]["habit_text"] == "通常 24 点睡觉"
    assert session.get_habits()[0]["category"] == "routine"
    assert session.remove_habit(habit_id) is True
    assert session.get_habits() == []


def test_legacy_habit_is_assigned_persistent_id_during_load() -> None:
    """无 ID 的旧习惯记录加载后也应可被纠正。"""
    session = NFCSession.from_dict(
        {
            "user_id": "user",
            "stream_id": "stream-1",
            "user_habits": [
                {
                    "habit_text": "工作日早上九点上班",
                    "category": "work",
                    "recorded_at": 100.0,
                }
            ],
        }
    )

    habit = session.get_habits()[0]
    assert habit["id"]
    assert session.update_habit(habit["id"], habit_text="工作日十点上班")
    assert session.to_dict()["user_habits"][0]["id"] == habit["id"]


def test_proactive_can_be_paused_per_session() -> None:
    """暂停主动联系应持久化原因，并保留原有预约供恢复后使用。"""
    session = NFCSession(user_id="user", stream_id="stream-1")
    session.set_scheduled_proactive(1234.0, "晚上问候")

    session.set_proactive_enabled(False, "用户要求暂时不要主动联系")

    assert session.proactive_enabled is False
    assert session.proactive_paused_reason == "用户要求暂时不要主动联系"
    assert session.scheduled_proactive_at == 1234.0
    restored = NFCSession.from_dict(session.to_dict())
    assert restored.proactive_enabled is False
    assert restored.proactive_paused_reason == "用户要求暂时不要主动联系"