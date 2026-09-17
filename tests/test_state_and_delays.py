"""状态渲染与语义延迟、session 新字段序列化测试。"""

from __future__ import annotations

import time

from neo_fatum_chatter.config import NFCConfig
from neo_fatum_chatter.context.sources.state_source import build_state_contributions
from neo_fatum_chatter.domain.drives import DriveState
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.domain.world import WorldTracker
from neo_fatum_chatter.execution.reply_executor import compute_segment_delay


def test_state_contributions_empty_when_all_neutral() -> None:
    """中性状态下只剩世界状态块：角色的日常始终在场（日程化世界状态）。"""
    session = NFCSession(user_id="u", stream_id="s")
    config = NFCConfig()
    contributions = build_state_contributions(session, config)
    assert {c.source for c in contributions} == {"nfc.world_state"}


def test_state_contributions_include_drives_and_beliefs() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.drives = DriveState(energy=0.2, social_drive=0.85)
    session.beliefs.upsert("她在准备考试", subject="user", confidence=0.8)
    config = NFCConfig()

    contributions = build_state_contributions(session, config)
    sources = {c.source for c in contributions}
    assert "nfc.drives" in sources
    assert "nfc.beliefs" in sources
    drives_block = next(c for c in contributions if c.source == "nfc.drives")
    assert "累" in drives_block.content


def test_state_contributions_render_story_world_when_active() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    WorldTracker.ensure_initialized(session)
    WorldTracker.apply_frame_event(session, "enter_story")
    assert session.story_world is not None
    session.story_world.story.update({"location": "灯塔下"})
    config = NFCConfig()

    contributions = build_state_contributions(session, config)
    story = [c for c in contributions if c.source == "nfc.story_world"]
    assert story and "灯塔下" in story[0].content


def test_state_contributions_include_character_card_layers() -> None:
    config = NFCConfig()
    config.character.redlines = ["不会发语音"]
    config.character.hidden_facts = [{"fact": "秘密", "condition": "x"}]
    session = NFCSession(user_id="u", stream_id="s")

    contributions = build_state_contributions(session, config)
    card = [c for c in contributions if c.source == "nfc.character_card"]
    assert card
    # 红线不再走 turn 级 contribution：已并入系统提示词安全节（见 modules.py）
    assert "不会发语音" not in card[0].content
    assert "秘密" not in card[0].content  # 隐藏事实内容不进 prompt


# ── 语义化打字延迟 ────────────────────────────────────────


def test_longer_next_segment_needs_longer_delay() -> None:
    short = compute_segment_delay(
        "嗯。", "好", chars_per_sec=10.0, delay_min=0.5, delay_max=4.0
    )
    long = compute_segment_delay(
        "嗯。", "这是一段很长很长很长的话需要打好久", chars_per_sec=10.0,
        delay_min=0.5, delay_max=4.0,
    )
    assert long > short


def test_question_mark_adds_pauses() -> None:
    plain = compute_segment_delay(
        "好嘞。", "收到", chars_per_sec=100.0, delay_min=0.0, delay_max=10.0
    )
    questioned = compute_segment_delay(
        "你吃饭了吗？", "收到", chars_per_sec=100.0, delay_min=0.0, delay_max=10.0
    )
    ellipsis = compute_segment_delay(
        "其实我……", "收到", chars_per_sec=100.0, delay_min=0.0, delay_max=10.0
    )
    assert questioned > plain
    assert ellipsis > questioned


def test_delay_always_clamped() -> None:
    too_slow = compute_segment_delay(
        "前", "超长的二十个字以上的段落！！！！！！！！",
        chars_per_sec=1.0, delay_min=0.5, delay_max=4.0,
    )
    assert too_slow == 4.0


def test_zero_chars_per_sec_falls_back_to_midpoint() -> None:
    delay = compute_segment_delay(
        "a", "b", chars_per_sec=0.0, delay_min=1.0, delay_max=2.0
    )
    assert 1.0 <= delay <= 2.0


# ── session 新字段序列化 ──────────────────────────────────


def test_session_round_trips_new_core_fields() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.drives = DriveState(energy=0.3, neglect=0.5)
    session.beliefs.upsert("她怕黑", subject="user", confidence=0.8)
    session.intents.add("问她面试", urge=0.6)
    session.character_state = {"revealed_fact_ids": ["hf0"], "active_overlay": None}
    WorldTracker.apply_frame_event(session, "enter_story")

    restored = NFCSession.from_dict(session.to_dict())
    assert restored.drives.energy == 0.3
    assert len(restored.beliefs) == 1
    assert restored.intents.pending_count() == 1
    assert restored.character_state["revealed_fact_ids"] == ["hf0"]
    assert restored.active_register == "story"
    assert restored.story_world is not None


def test_old_session_dict_loads_with_defaults() -> None:
    legacy = NFCSession.from_dict({"user_id": "u", "stream_id": "s"})
    assert legacy.active_register == "reality"
    assert legacy.story_world is None
    assert legacy.story_archive == []
    assert len(legacy.beliefs) == 0
    assert len(legacy.intents) == 0


def test_reset_context_keeps_long_term_assets_drops_story() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.beliefs.upsert("她怕黑", confidence=0.9)
    session.intents.add("问她吃药", kind="commitment", deadline=time.time() + 3600)
    WorldTracker.apply_frame_event(session, "enter_story")

    session.reset_context()

    assert session.story_world is None
    assert session.active_register == "reality"
    assert len(session.beliefs) == 1          # 信念保留
    assert session.intents.pending_count() == 1  # 意图保留
