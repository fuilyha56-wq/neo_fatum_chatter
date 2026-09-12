"""双登记簿世界状态测试。"""

from __future__ import annotations

from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.domain.world import (
    REGISTER_REALITY,
    REGISTER_STORY,
    StoryArchiveEntry,
    WorldState,
    WorldTracker,
    is_frame_event,
)


def test_enter_pause_resume_exit_cycle() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    WorldTracker.ensure_initialized(session)
    assert session.active_register == REGISTER_REALITY

    note = WorldTracker.apply_frame_event(session, "enter_story")
    assert session.active_register == REGISTER_STORY
    assert session.story_world is not None
    assert "进入剧情" in note

    WorldTracker.apply_frame_event(session, "pause_story")
    assert session.active_register == REGISTER_REALITY
    assert session.story_world is not None  # 世界保留

    WorldTracker.apply_frame_event(session, "resume_story")
    assert session.active_register == REGISTER_STORY


def test_exit_story_archives_meaningful_world() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    WorldTracker.ensure_initialized(session)
    WorldTracker.apply_frame_event(session, "enter_story")
    assert session.story_world is not None
    session.story_world.story.update(
        {"location": "深夜的便利店", "ongoing_event": "一起躲雨"}
    )

    WorldTracker.apply_frame_event(session, "exit_story")

    assert session.story_world is None
    assert session.active_register == REGISTER_REALITY
    assert len(session.story_archive) == 1
    entry = session.story_archive[0]
    assert entry.title  # 用事件/地点做了标题
    restored_world = WorldState.from_dict(entry.world)
    assert restored_world.story.location == "深夜的便利店"


def test_exit_story_with_empty_world_does_not_archive() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    WorldTracker.ensure_initialized(session)
    WorldTracker.apply_frame_event(session, "enter_story")
    WorldTracker.apply_frame_event(session, "exit_story")
    assert session.story_archive == []


def test_restore_archive_reactivates_story() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    WorldTracker.ensure_initialized(session)
    session.story_archive = [
        StoryArchiveEntry(
            id="arc1", title="咖啡店的故事", saved_at=1.0,
            world=WorldState.new_story().to_dict(),
        )
    ]
    note = WorldTracker.restore_archive(session, "arc1")
    assert "咖啡店的故事" in note
    assert session.active_register == REGISTER_STORY
    assert session.story_world is not None


def test_story_facts_update_merges_present_characters() -> None:
    world = WorldState.new_story()
    changed = world.story.update(
        {"story_time": "第一夜", "present": ["我", "她"]}
    )
    assert changed
    changed_again = world.story.update({"present": ["她", "店主"]})
    assert changed_again
    assert world.story.present == ["我", "她", "店主"]


def test_world_evidence_dedupes_and_reinforces() -> None:
    world = WorldState.new_story()
    assert world.add_evidence("appraisal", "她淋着雨跑进来", "user_message", 0.6)
    assert not world.add_evidence("appraisal", "她淋着雨跑进来", "user_message", 0.9)
    assert len(world.evidence) == 1
    assert world.evidence[0].confidence == 0.9


def test_render_story_text_includes_facts() -> None:
    world = WorldState.new_story()
    world.story.update({"location": "天台", "story_time": "黄昏"})
    text = world.render_story_text()
    assert "天台" in text and "黄昏" in text
    # 现实登记簿不渲染剧情块
    assert WorldState(register=REGISTER_REALITY).render_story_text() == ""


def test_is_frame_event_validation() -> None:
    assert is_frame_event("enter_story")
    assert not is_frame_event("teleport")


def test_world_state_serialization_round_trip() -> None:
    world = WorldState.new_story()
    world.story.update({"location": "旧书店", "present": ["我"]})
    world.add_evidence("appraisal", "她挑了一本诗集", "user_message", 0.8)
    restored = WorldState.from_dict(world.to_dict())
    assert restored.register == REGISTER_STORY
    assert restored.story.location == "旧书店"
    assert len(restored.evidence) == 1


def test_active_world_view_reality_falls_back_to_scene_state() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    WorldTracker.ensure_initialized(session)
    view = WorldTracker.active_world(session)
    assert view.register == REGISTER_REALITY
    assert view.certainty == session.scene_state.certainty
