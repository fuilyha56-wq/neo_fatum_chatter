"""世界状态/日程融合的新增契约测试。"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from neo_fatum_chatter.config import NFCConfig
from neo_fatum_chatter.context.sources.plugin_source import _normalize_context_contribution
from neo_fatum_chatter.context.types import ContextContribution
from neo_fatum_chatter.context.sources.state_source import build_state_contributions
from neo_fatum_chatter.domain.daily_life import AgendaItem, DailyLifeState, parse_clock
from neo_fatum_chatter.domain.proactive_candidate import (
    CANDIDATE_CANCELLED,
    CANDIDATE_DISPATCHING,
    CANDIDATE_QUEUED,
    CANDIDATE_SENT,
    ProactiveCandidate,
)
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.domain.scene_state import SceneEvidence
from neo_fatum_chatter.domain.world import StoryArchiveEntry, WorldState, WorldTracker
from neo_fatum_chatter.plugin import NFCPlugin
from neo_fatum_chatter.services.world_state_service import WorldStateService
from neo_fatum_chatter.thinker.proactive import ProactiveThinker


def _ts(hour: int, minute: int = 0) -> float:
    today = datetime.now().date()
    return datetime(today.year, today.month, today.day, hour, minute).timestamp()


class _Store:
    def __init__(self, session: NFCSession) -> None:
        self.session = session
        self.saved = 0

    @asynccontextmanager
    async def lock(self, stream_id: str) -> AsyncIterator[None]:
        assert stream_id == self.session.stream_id
        yield

    async def get_or_create(self, stream_id: str) -> NFCSession:
        assert stream_id == self.session.stream_id
        return self.session

    async def get(self, stream_id: str) -> NFCSession | None:
        assert stream_id == self.session.stream_id
        return self.session

    async def save(self, session: NFCSession) -> None:
        assert session is self.session
        self.saved += 1


def test_persistent_context_scope_is_explicitly_downgraded() -> None:
    contribution = ContextContribution(
        source="external",
        owner="notice",
        scope="persistent",
        priority=1,
        content="不应假装已持久化",
    )
    normalized_object = _normalize_context_contribution(contribution)
    normalized_dict = _normalize_context_contribution({
        "source": "external",
        "owner": "notice",
        "scope": "persistent",
        "priority": 1,
        "content": "不应假装已持久化",
    })
    assert normalized_object is not None and normalized_object.scope == "turn"
    assert normalized_dict is not None and normalized_dict.scope == "turn"


def test_world_config_is_part_of_nfc_config() -> None:
    config = NFCConfig(
        world={
            "wake_time": "08:00",
            "sleep_time": "23:00",
            "location": "家里",
            "routine": {"早晨": "晨跑"},
        }
    )
    assert config.world.wake_time == "08:00"
    assert config.world.sleep_time == "23:00"
    assert config.world.location == "家里"
    assert config.world.routine["早晨"] == "晨跑"


def test_daily_life_rollover_discards_stale_items_but_keeps_overnight_plan() -> None:
    yesterday = (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
    state = DailyLifeState(routine={"morning": "晨跑"})
    state._last_day_key = yesterday
    stale = AgendaItem(
        activity="昨天的计划",
        source="planned",
        start_minute=10 * 60,
        end_minute=11 * 60,
        day_key=yesterday,
    )
    overnight = AgendaItem(
        activity="夜班",
        source="planned",
        start_minute=22 * 60,
        end_minute=6 * 60,
        day_key=yesterday,
    )
    state.plan_items = [stale, overnight]

    state.ensure_day(_ts(2, 0))

    assert stale not in state.plan_items
    assert overnight in state.plan_items
    assert overnight.day_key == yesterday  # 保留原计划日，避免下一晚再次续命
    assert state.routine["morning"] == "晨跑"

    state.ensure_day(_ts(7, 0))
    assert overnight.status == "expired"


def test_planned_item_never_becomes_fact_by_status_update() -> None:
    state = DailyLifeState()
    item = state.add_plan("去图书馆", start="10:00", end="12:00")
    assert item is not None
    assert item.source == "planned"
    assert item.is_fact is False

    confirmed = state.update_item_status(item.item_id, "confirmed")
    assert confirmed is item
    assert item.source == "planned"
    assert item.is_fact is False


def test_clock_parser_rejects_out_of_range_values() -> None:
    assert parse_clock(-1) == -1
    assert parse_clock(24 * 60) == -1
    assert parse_clock("24:30") == -1


def test_implicit_two_hour_plan_crosses_midnight_only_once() -> None:
    yesterday = (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
    state = DailyLifeState()
    state._last_day_key = yesterday
    item = AgendaItem(
        activity="深夜阅读",
        source="planned",
        start_minute=23 * 60,
        end_minute=-1,
        day_key=yesterday,
    )
    state.plan_items = [item]
    state.ensure_day(_ts(0, 30))
    assert item in state.plan_items
    state.ensure_day(_ts(2, 0))
    assert item.status == "expired"


def test_cancelled_plan_is_not_rendered_or_used_as_activity() -> None:
    state = DailyLifeState()
    item = state.add_plan("取消的安排", start="00:00", end="23:59")
    assert item is not None
    state.update_item_status(item.item_id, "cancelled")
    assert "取消的安排" not in "\n".join(state.today_plan_view())
    assert state.current_activity()["activity"] != "取消的安排"


def test_malformed_agenda_numbers_are_defensive() -> None:
    item = AgendaItem.from_dict(
        {
            "activity": "可恢复条目",
            "start_minute": "not-a-number",
            "end_minute": None,
            "created_at": "broken",
            "revision": "broken",
        }
    )
    assert item is not None
    assert item.start_minute == -1
    assert item.end_minute == -1
    assert item.revision == 1


def test_story_archive_restores_full_overlay_and_context_reset_removes_it() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.character_state = {
        "revealed_fact_ids": ["hf0"],
        "active_overlay": {"name": "夜班店员", "persona": "便利店夜班人格", "tone": "克制"},
    }
    WorldTracker.apply_frame_event(session, "enter_story")
    assert session.story_world is not None
    session.story_world.story.update({"ongoing_event": "一起躲雨"})
    WorldTracker.apply_frame_event(session, "exit_story")

    archive = session.story_archive[0]
    assert archive.overlay_state["persona"] == "便利店夜班人格"
    WorldTracker.restore_archive(session, archive.id)
    assert session.character_state["active_overlay"]["tone"] == "克制"

    session.reset_context()
    assert session.story_world is None
    assert session.character_state["active_overlay"] is None
    assert session.character_state["revealed_fact_ids"] == ["hf0"]


def test_reality_view_fuses_daily_life_and_scene_evidence_with_sources() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.daily_life.commit_activity("在画画", location="家里")
    session.scene_state.evidence.append(
        SceneEvidence(
            source="test", content="用户在通勤", kind="user_message", confidence=0.8
        )
    )
    view = WorldTracker.reality_view(session)
    assert view.register == "reality"
    assert view.current_activity is not None
    assert view.current_activity["source"] == "commit"
    assert view.location == "家里"
    assert view.scene_evidence[0]["source"] == "test"


def test_proactive_candidate_round_trip_and_user_message_cancellation() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    candidate = ProactiveCandidate(
        origin_id="origin-1",
        dedupe_key="dedupe-1",
        route="continuation",
        source="topic_hook",
        reason="想继续问问",
    )
    assert session.upsert_proactive_candidate(candidate) is candidate
    assert session.upsert_proactive_candidate(
        ProactiveCandidate(origin_id="other", dedupe_key="dedupe-1", reason="刷新")
    ) is candidate
    assert candidate.reason == "刷新"

    restored = NFCSession.from_dict(session.to_dict())
    assert restored.proactive_candidates[0].dedupe_key == "dedupe-1"
    assert restored.cancel_proactive_candidates(route="continuation") == 1
    assert restored.proactive_candidates[0].status == CANDIDATE_CANCELLED

    # 终态候选不再被同一 dedupe key 覆盖，发送状态也可序列化。
    candidate.mark(CANDIDATE_SENT, reason="triggered")
    assert candidate.status == CANDIDATE_SENT


def test_manifest_lists_new_world_actions() -> None:
    manifest_path = Path(__file__).resolve().parent.parent / "manifest.json"
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    names = {
        item["component_name"]
        for item in manifest["include"]
        if item["component_type"] == "action"
    }
    assert {"nfc_update_world", "nfc_manage_schedule"} <= names

    plugin = NFCPlugin(NFCConfig())
    runtime_names = {
        component.name
        for component in plugin.get_components()
        if hasattr(component, "name")
    }
    assert {"nfc_update_world", "nfc_manage_schedule"} <= runtime_names


@pytest.mark.asyncio
async def test_world_service_keeps_plan_and_observation_separate() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    store = _Store(session)
    service = WorldStateService(store)

    added = await service.manage_schedule(
        "s",
        operation="add",
        activity="去图书馆",
        start_time="10:00",
        end_time="12:00",
        idempotency_key="plan-1",
    )
    assert added.ok and added.item is not None
    assert added.item.source == "planned"
    assert added.item.is_fact is False

    confirmed = await service.manage_schedule(
        "s", operation="confirm", item_id=added.item.item_id
    )
    assert confirmed.ok
    assert confirmed.item is not None and confirmed.item.is_fact is False

    observed = await service.manage_schedule(
        "s",
        operation="observe",
        item_id=added.item.item_id,
        activity="明确看到对方已经到图书馆",
    )
    assert observed.ok and observed.item is not None
    assert observed.item.source == "observed"
    assert observed.item.is_fact is True
    assert observed.item.source_ref == added.item.item_id
    assert added.item.status == "completed"
    assert added.item.materialization_state == "reconciled"
    assert added.item.is_fact is False
    assert store.saved == 3


@pytest.mark.asyncio
async def test_confirmed_agenda_candidate_fires_once_and_settles() -> None:
    now = time.time()
    local = time.localtime(now)
    minute = local.tm_hour * 60 + local.tm_min
    session = NFCSession(user_id="u", stream_id="s")
    item = session.daily_life.add_plan(
        "开始晚间阅读",
        start=max(0, minute - 1),
        end=min(23 * 60 + 59, minute + 30),
    )
    assert item is not None
    session.daily_life.update_item_status(item.item_id, "confirmed")
    store = _Store(session)
    config = SimpleNamespace(
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
    thinker = ProactiveThinker(config, store)

    assert await thinker._check_and_trigger("s", session) is True
    reason = await thinker.mark_triggered("s")
    candidate = next(c for c in session.proactive_candidates if c.source == "agenda")
    assert candidate.status == CANDIDATE_SENT
    assert "晚间阅读" in reason
    assert await thinker._check_and_trigger("s", session) is False


def test_restoring_archive_without_overlay_clears_previous_overlay() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.character_state = {"active_overlay": {"name": "旧故事人物"}}
    session.story_archive = [
        StoryArchiveEntry(
            id="plain",
            title="无覆层故事",
            saved_at=time.time(),
            world=WorldState.new_story().to_dict(),
        )
    ]
    WorldTracker.restore_archive(session, "plain")
    assert session.character_state["active_overlay"] is None


@pytest.mark.asyncio
async def test_reality_mutations_are_rejected_in_story_register() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    WorldTracker.apply_frame_event(session, "enter_story")
    service = WorldStateService(_Store(session))

    update = await service.update_self_state("s", activity="剧情里打牌")
    schedule = await service.manage_schedule(
        "s", operation="add", activity="剧情里的安排", start_time="12:00"
    )
    assert update.ok is False
    assert schedule.ok is False
    assert session.daily_life.plan_items == []


def test_invalid_self_state_end_time_preserves_previous_commit() -> None:
    state = DailyLifeState()
    previous = state.commit_activity("之前的活动")
    assert previous is not None
    assert state.commit_activity("新活动", end="25:99") is None
    commits = [item for item in state.plan_items if item.source == "commit"]
    assert commits == [previous]


def test_temporary_location_expires_with_owning_state() -> None:
    state = DailyLifeState()
    item = state.commit_activity("在咖啡店", location="咖啡店")
    assert item is not None
    state.location_expires_at = time.time() - 1
    state.current_activity()
    assert state.location == ""
    assert state.location_source == "persistent"
    assert state.location_item_id == ""


def test_weak_scene_evidence_is_rendered_as_clue_not_confirmation() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.scene_state.certainty = "weak"
    session.scene_state.evidence.append(
        SceneEvidence(source="appraisal", content="可能在路上", kind="inference", confidence=0.4)
    )
    contribution = next(
        item for item in build_state_contributions(session, NFCConfig())
        if item.source == "nfc.world_state"
    )
    assert "现实场景证据" in contribution.content
    assert "[不确定线索] 可能在路上" in contribution.content
    assert "已确认的场景事实" not in contribution.content


@pytest.mark.asyncio
async def test_proactive_prepare_settle_is_retryable_and_marks_sent() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    candidate = ProactiveCandidate(
        origin_id="dispatch-1",
        dedupe_key="dispatch-1",
        route="continuation",
        source="topic_hook",
        reason="想继续聊",
        preferred_at=time.time() - 1,
        window_start=time.time() - 1,
    )
    session.upsert_proactive_candidate(candidate)
    store = _Store(session)
    config = SimpleNamespace(
        proactive=SimpleNamespace(
            enabled=True, min_interval=1800, silence_threshold=999999,
            trigger_probability=0.0, quiet_hours_start="00:00", quiet_hours_end="00:00",
            activity_service_signature="", activity_service_method="is_good_time",
        ),
        intent=SimpleNamespace(enabled=False, fire_threshold=0.75),
        drives=SimpleNamespace(enabled=False),
    )
    thinker = ProactiveThinker(config, store)
    thinker._selected_candidates["s"] = candidate.dedupe_key

    key, _ = await thinker.prepare_trigger("s")
    assert key == candidate.dedupe_key
    assert candidate.status == CANDIDATE_DISPATCHING
    await thinker.settle_trigger("s", key, delivered=False)
    assert candidate.status == CANDIDATE_QUEUED

    thinker._selected_candidates["s"] = candidate.dedupe_key
    key, reason = await thinker.prepare_trigger("s")
    assert key == candidate.dedupe_key
    assert reason == "想继续聊"
    await thinker.settle_trigger("s", key, delivered=True)
    assert candidate.status == CANDIDATE_SENT


@pytest.mark.asyncio
async def test_proactive_prepare_cancels_candidate_after_user_message() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    candidate = ProactiveCandidate(
        origin_id="race-1",
        dedupe_key="race-1",
        route="continuation",
        source="topic_hook",
        created_at=time.time() - 10,
    )
    session.upsert_proactive_candidate(candidate)
    session.last_user_message_at = time.time()
    store = _Store(session)
    config = SimpleNamespace(
        proactive=SimpleNamespace(enabled=True),
        intent=SimpleNamespace(enabled=False),
        drives=SimpleNamespace(enabled=False),
    )
    thinker = ProactiveThinker(config, store)
    thinker._selected_candidates["s"] = candidate.dedupe_key
    key, _ = await thinker.prepare_trigger("s")
    assert key == ""
    assert candidate.status == CANDIDATE_CANCELLED
