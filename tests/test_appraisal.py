"""S1 感知评估：解析与应用测试。"""

from __future__ import annotations

from neo_fatum_chatter.config import NFCConfig
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.services.appraisal import (
    AppraisalResult,
    apply_appraisal,
    parse_appraisal,
)


def _make_config() -> NFCConfig:
    config = NFCConfig()
    config.character.redlines = ["不会发语音"]
    config.character.hidden_facts = [
        {"fact": "偷偷准备了礼物", "condition": "到生日那天"},
    ]
    return config


def test_parse_full_json() -> None:
    raw = """```json
    {"emotion": "有点担心", "urgency": 0.8, "register": "story",
     "frame_event": "enter_story",
     "world_facts": ["她说自己在医院"],
     "story_update": {"location": "病房"},
     "topic_hooks": ["她为什么去医院"],
     "commitment": {"content": "明天陪她复诊", "due_hours": 20},
     "defer_seconds": 25, "respond_now": true, "reveal_ids": ["hf0"]}
    ```"""
    result = parse_appraisal(raw)
    assert result is not None
    assert result.emotion == "有点担心"
    assert result.urgency == 0.8
    assert result.register == "story"
    assert result.frame_event == "enter_story"
    assert result.world_facts == ["她说自己在医院"]
    assert result.story_update == {"location": "病房"}
    assert result.commitment["due_hours"] == 20
    assert result.defer_seconds == 20.0  # 超上限被夹
    assert result.reveal_ids == ["hf0"]


def test_parse_garbage_returns_none() -> None:
    assert parse_appraisal("") is None
    assert parse_appraisal("这不是JSON") is None
    assert parse_appraisal('{"broken": ') is None


def test_parse_invalid_frame_event_dropped() -> None:
    result = parse_appraisal('{"frame_event": "teleport", "register": "mars"}')
    assert result is not None
    assert result.frame_event == ""
    assert result.register == ""  # 非法 register 被丢弃


def test_apply_frame_event_switches_register() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    config = _make_config()
    appraisal = AppraisalResult(frame_event="enter_story")

    note = apply_appraisal(session, appraisal, config=config)
    assert "enter_story" in note
    assert session.active_register == "story"
    assert session.story_world is not None


def test_apply_implicit_register_needs_two_consecutive_votes() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    config = _make_config()

    apply_appraisal(session, AppraisalResult(register="story"), config=config)
    assert session.active_register == "reality"  # 第一次不换挡（粘滞）

    apply_appraisal(session, AppraisalResult(register="story"), config=config)
    assert session.active_register == "story"    # 连续两次才切换


def test_apply_world_facts_route_by_active_register() -> None:
    config = _make_config()

    # 现实登记簿 → scene_state 证据
    reality_session = NFCSession(user_id="u", stream_id="s")
    apply_appraisal(
        reality_session,
        AppraisalResult(world_facts=["她说她在图书馆"]),
        config=config,
    )
    assert any(
        e.content == "她说她在图书馆" for e in reality_session.scene_state.evidence
    )
    assert reality_session.scene_state.certainty == "weak"

    # 故事登记簿 → story_world 证据
    story_session = NFCSession(user_id="u", stream_id="s")
    apply_appraisal(story_session, AppraisalResult(frame_event="enter_story"), config=config)
    apply_appraisal(
        story_session,
        AppraisalResult(world_facts=["龙盘踞在塔顶"]),
        config=config,
    )
    assert story_session.story_world is not None
    assert any(e.content == "龙盘踞在塔顶" for e in story_session.story_world.evidence)
    assert not any(
        e.content == "龙盘踞在塔顶" for e in story_session.scene_state.evidence
    )


def test_apply_hooks_and_commitment_feed_intent_queue() -> None:
    import time as _time

    session = NFCSession(user_id="u", stream_id="s")
    config = _make_config()
    appraisal = AppraisalResult(
        topic_hooks=["她周五要面试"],
        commitment={"content": "面试完听她复盘", "due_hours": 48},
    )

    apply_appraisal(session, appraisal, config=config)
    pending = [i.content for i in session.intents.intents]
    assert "她周五要面试" in pending
    assert "面试完听她复盘" in pending
    commitment = next(i for i in session.intents.intents if "复盘" in i.content)
    assert commitment.kind == "commitment"
    assert commitment.deadline is not None and commitment.deadline > _time.time()


def test_apply_reveal_updates_character_state() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    config = _make_config()
    appraisal = AppraisalResult(reveal_ids=["hf0"])

    note = apply_appraisal(session, appraisal, config=config)
    assert "reveal:1" in note
    assert "hf0" in session.character_state["revealed_fact_ids"]


def test_apply_defer_sets_respond_not_before() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    config = _make_config()
    config.appraisal.defer_enabled = True
    config.appraisal.max_defer_seconds = 15.0

    appraisal = AppraisalResult(defer_seconds=10.0)
    apply_appraisal(session, appraisal, config=config)
    assert 0 < session.respond_not_before

    # 等待中不设延迟
    from neo_fatum_chatter.models import WaitingConfig
    import time as _time

    waiting_session = NFCSession(user_id="u", stream_id="s")
    waiting_session.set_waiting(
        WaitingConfig(max_wait_seconds=60, started_at=_time.time())
    )
    apply_appraisal(waiting_session, AppraisalResult(defer_seconds=10.0), config=config)
    assert waiting_session.respond_not_before == 0.0


def test_apply_emotion_nudges_drives_mood() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    config = _make_config()
    before = session.drives.mood

    apply_appraisal(session, AppraisalResult(emotion="很开心"), config=config)
    assert session.drives.mood > before

    apply_appraisal(session, AppraisalResult(emotion="有点难过"), config=config)
    assert session.drives.mood < before + 0.06
