"""日程化世界状态（domain/daily_life）测试。

设计移植自 private_companion：五段窗口、作息锚点、事实级条目
优先于计划、计划优先于作息推断。
"""

from __future__ import annotations

import time
from datetime import datetime

from neo_fatum_chatter.config import NFCConfig
from neo_fatum_chatter.context.sources.state_source import build_state_contributions
from neo_fatum_chatter.domain.daily_life import (
    AgendaItem,
    DailyLifeState,
    normalize_window,
    window_for_minutes,
)
from neo_fatum_chatter.domain.session_state import NFCSession


def _ts(hour: int, minute: int = 0) -> float:
    """构造当天某时刻的本地时间戳。"""
    today = datetime.now().date()
    return datetime(today.year, today.month, today.day, hour, minute).timestamp()


def test_window_for_minutes_basic_and_midnight() -> None:
    assert window_for_minutes(6 * 60) == "morning"
    assert window_for_minutes(9 * 60) == "morning"
    assert window_for_minutes(12 * 60) == "noon"
    assert window_for_minutes(15 * 60) == "afternoon"
    assert window_for_minutes(19 * 60) == "evening"
    assert window_for_minutes(22 * 60) == "late_night"
    assert window_for_minutes(2 * 60) == "late_night"  # 跨午夜
    assert window_for_minutes(5 * 60 + 59) == "late_night"


def test_normalize_window_aliases() -> None:
    assert normalize_window("凌晨") == "late_night"
    assert normalize_window("早上") == "morning"
    assert normalize_window("傍晚") == "evening"
    # 英文 slug 直通（对齐 private_companion 的 normalize 语义）
    assert normalize_window("afternoon") == "afternoon"
    assert normalize_window("不存在的窗口") == ""


def test_is_asleep_boundaries() -> None:
    state = DailyLifeState()
    assert state.is_asleep(_ts(23, 30)) is True
    assert state.is_asleep(_ts(3, 0)) is True
    assert state.is_asleep(_ts(10, 0)) is False
    assert state.is_asleep(_ts(7, 29)) is True
    assert state.is_asleep(_ts(7, 31)) is False


def test_current_activity_falls_back_to_routine() -> None:
    state = DailyLifeState()
    view = state.current_activity(_ts(15, 0))
    assert view is not None
    assert view["epistemic"] == "inferred"
    assert view["window"] == "afternoon"


def test_current_activity_prefers_commit_over_routine() -> None:
    state = DailyLifeState()
    state.commit_activity("在打排位", end="23:59", location="家里")
    view = state.current_activity(_ts(15, 0))
    assert view is not None
    assert view["epistemic"] == "fact"
    assert view["activity"] == "在打排位"
    assert view["location"] == "家里"


def test_current_activity_commit_expires_after_end() -> None:
    state = DailyLifeState()
    # 直接构造 08:00-09:00 的自述，验证 09:30 已失效、08:30 仍生效
    state.plan_items.append(
        AgendaItem(activity="临时出门", source="commit", start_minute=8 * 60, end_minute=9 * 60)
    )
    active = state.current_activity(_ts(8, 30))
    assert active is not None
    assert active["epistemic"] == "fact"
    expired = state.current_activity(_ts(9, 30))
    assert expired is not None
    assert expired["epistemic"] != "fact"


def test_current_activity_planned_item_beats_routine() -> None:
    state = DailyLifeState()
    local = time.localtime()
    now_minute = local.tm_hour * 60 + local.tm_min
    state.add_plan("图书馆自习", start=now_minute - 10, end=now_minute + 40)
    view = state.current_activity()
    assert view is not None
    assert view["epistemic"] == "committed"
    assert view["activity"] == "图书馆自习"


def test_recent_observation_is_fact() -> None:
    state = DailyLifeState()
    item = state.add_observation("刚跑完步回来")
    assert item is not None
    view = state.current_activity()
    assert view is not None
    assert view["epistemic"] == "fact"
    assert view["activity"] == "刚跑完步回来"


def test_new_commit_replaces_old_commit() -> None:
    state = DailyLifeState()
    state.commit_activity("在打游戏")
    state.commit_activity("在看书")
    commits = [
        item for item in state.plan_items if item.source == "commit"
    ]
    assert len(commits) == 1
    assert commits[0].activity == "在看书"


def test_agenda_item_covers_cross_midnight() -> None:
    item = AgendaItem(activity="夜班", source="planned", start_minute=22 * 60, end_minute=6 * 60)
    assert item.covers(23 * 60) is True
    assert item.covers(2 * 60) is True
    assert item.covers(12 * 60) is False


def test_today_plan_view_hides_finished_items() -> None:
    state = DailyLifeState()
    state.add_plan("早课", start=8 * 60, end=9 * 60 + 30)
    state.add_plan("晚上健身", start=19 * 60, end=20 * 60)
    view = state.today_plan_view(_ts(15, 0))
    joined = "\n".join(view)
    assert "晚上健身" in joined
    assert "早课" not in joined


def test_render_world_text_contains_living_state() -> None:
    state = DailyLifeState()
    # 确定性自述 14:00-16:00，渲染 15:00
    state.plan_items.append(
        AgendaItem(activity="在打排位", source="commit", start_minute=14 * 60, end_minute=16 * 60)
    )
    text = state.render_world_text(_ts(15, 0))
    assert "# 我的世界状态（现实）" in text
    assert "在打排位" in text
    assert "现在是" in text


def test_session_serialization_round_trip() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.daily_life.commit_activity("在做饭", end="19:00", location="家里")
    session.daily_life.location = "家里"
    session.daily_life.routine = {"早晨": "晨跑"}

    data = session.to_dict()
    restored = NFCSession.from_dict(data)

    assert restored.daily_life.location == "家里"
    assert restored.daily_life.routine == {"morning": "晨跑"}
    commits = [i for i in restored.daily_life.plan_items if i.source == "commit"]
    assert len(commits) == 1
    assert commits[0].activity == "在做饭"


def test_state_contribution_world_state_rendered_and_gated() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.daily_life.commit_activity("在画画")
    config = NFCConfig()

    contributions = build_state_contributions(session, config)
    world = next(
        (c for c in contributions if c.source == "nfc.world_state"), None
    )
    assert world is not None
    assert world.owner == "self_state"
    assert "在画画" in world.content

    # story 登记簿激活时，现实世界块被剧情世界块取代
    from neo_fatum_chatter.domain.world import WorldTracker

    WorldTracker.ensure_initialized(session)
    WorldTracker.apply_frame_event(session, "enter_story")
    contributions_story = build_state_contributions(session, config)
    assert all(c.source != "nfc.world_state" for c in contributions_story)


def test_state_contribution_absent_when_world_disabled() -> None:
    from types import SimpleNamespace

    session = NFCSession(user_id="u", stream_id="s")
    config = SimpleNamespace(world=SimpleNamespace(enabled=False))

    contributions = build_state_contributions(session, config)
    assert all(c.source != "nfc.world_state" for c in contributions)
