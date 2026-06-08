"""NFC 配置边界校验测试。"""

from __future__ import annotations

from neo_fatum_chatter.config import NFCConfig


def test_proactive_probability_is_clamped_to_unit_interval() -> None:
    config = NFCConfig(proactive={"trigger_probability": 2.0})

    assert config.proactive.trigger_probability == 1.0


def test_wait_min_max_seconds_are_ordered() -> None:
    config = NFCConfig(wait={"min_seconds": 30.0, "max_seconds": 10.0})

    assert config.wait.min_seconds == 10.0
    assert config.wait.max_seconds == 30.0


def test_positive_intervals_fall_back_when_not_positive() -> None:
    config = NFCConfig(
        proactive={"min_interval": -1, "check_interval": 0},
        reply={"segment_delay_min": -1.0, "segment_delay_max": -2.0},
    )

    assert config.proactive.min_interval == 1800
    assert config.proactive.check_interval == 60
    assert config.reply.segment_delay_min == 0.5
    assert config.reply.segment_delay_max == 2.0
