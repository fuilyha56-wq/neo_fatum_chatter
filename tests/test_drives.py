"""内驱状态机测试。"""

from __future__ import annotations

import time

from neo_fatum_chatter.domain.drives import DriveState


def test_advance_recovers_energy_and_raises_social_drive() -> None:
    state = DriveState(energy=0.2, social_drive=0.3)
    base = time.time()
    state.last_updated = base

    state.advance_to(base + 3600)  # 1 小时

    assert state.energy > 0.2
    assert state.social_drive > 0.3
    assert 0.0 <= state.energy <= 1.0


def test_advance_is_idempotent_for_same_timestamp() -> None:
    state = DriveState(energy=0.2)
    now = time.time()
    state.last_updated = now - 7200
    state.advance_to(now)
    snapshot = state.to_dict()
    state.advance_to(now)
    assert state.to_dict() == snapshot


def test_user_message_satisfies_social_drive_and_clears_neglect() -> None:
    state = DriveState(social_drive=0.8, neglect=0.6)
    state.on_user_message(count=2)
    assert state.social_drive < 0.8
    assert state.neglect < 0.6


def test_reply_late_raises_neglect_and_drops_mood() -> None:
    state = DriveState(neglect=0.2, mood=0.6)
    state.on_reply_timing(in_time=False)
    assert state.neglect > 0.2
    assert state.mood < 0.6


def test_wait_timeout_drains_mood_and_energy() -> None:
    state = DriveState(mood=0.7, energy=0.8)
    state.on_wait_timeout()
    assert state.mood < 0.7
    assert state.energy < 0.8


def test_modulate_wait_shortens_when_eager_and_lengthens_when_neglected() -> None:
    eager = DriveState(social_drive=0.95, energy=0.9, neglect=0.0)
    neglected = DriveState(social_drive=0.1, energy=0.2, neglect=0.9)

    eager_wait = eager.modulate_wait(100.0, strength=0.3)
    neglected_wait = neglected.modulate_wait(100.0, strength=0.3)

    assert eager_wait < 100.0 < neglected_wait
    # 调制幅度有界
    assert 50.0 <= eager_wait <= 200.0
    assert 50.0 <= neglected_wait <= 200.0


def test_modulate_wait_disabled_with_zero_strength() -> None:
    state = DriveState(energy=0.05, neglect=0.95)
    assert state.modulate_wait(100.0, strength=0.0) == 100.0


def test_render_state_text_empty_when_neutral() -> None:
    assert DriveState().render_state_text() == ""


def test_render_state_text_lists_deviations_only() -> None:
    tired = DriveState(energy=0.2, social_drive=0.9)
    text = tired.render_state_text()
    assert "累" in text
    assert "想找人说话" in text
    # 分带量化：同带内两次渲染一致（prefix cache 友好）
    twin = DriveState(energy=0.22, social_drive=0.88)
    assert twin.render_state_text() == text


def test_serialization_round_trip() -> None:
    state = DriveState(social_drive=0.77, energy=0.33, curiosity=0.66, neglect=0.11, mood=0.44)
    restored = DriveState.from_dict(state.to_dict())
    assert restored.to_dict() == state.to_dict()


def test_from_dict_tolerates_garbage() -> None:
    restored = DriveState.from_dict({"energy": "broken", "mood": True})
    assert 0.0 <= restored.energy <= 1.0
    assert 0.0 <= restored.mood <= 1.0
