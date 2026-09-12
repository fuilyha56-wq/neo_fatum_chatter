"""信念层测试。"""

from __future__ import annotations

import time

from neo_fatum_chatter.domain.beliefs import Belief, BeliefBook


def test_upsert_adds_then_reinforces() -> None:
    book = BeliefBook()
    assert book.upsert("她最近在准备考试，讨厌这时被打扰") == "added"
    assert book.upsert("她最近在准备考试，讨厌这时被打扰！") == "reinforced"  # 标点差异归一
    assert len(book) == 1
    belief = book.beliefs[0]
    assert belief.evidence_count == 2
    assert belief.confidence > 0.5


def test_weaken_removes_below_threshold() -> None:
    book = BeliefBook()
    book.upsert("她喜欢在深夜聊天", confidence=0.4)
    assert book.weaken("她喜欢在深夜聊天") is True  # 0.4-0.35 < 0.15 → 移除
    assert len(book) == 0


def test_decay_drops_stale_beliefs() -> None:
    book = BeliefBook()
    book.upsert("她养了一只猫", confidence=0.5)
    book.beliefs[0].last_evidence_at = time.time() - 20 * 86400  # 20 天无证据
    removed = book.decay()
    assert removed == 1
    assert len(book) == 0


def test_for_prompt_filters_low_confidence_and_renders_labels() -> None:
    book = BeliefBook()
    book.upsert("她最近在准备考研", subject="user", confidence=0.8)
    book.upsert("我们几乎每天都会聊几句", subject="relationship", confidence=0.7)
    book.upsert("不太确定的一句", subject="user", confidence=0.2)  # 低于阈值

    text = book.for_prompt(register="reality")
    assert "考研" in text
    assert "你们" in text
    assert "不太确定" not in text


def test_for_prompt_cross_register_hint_limited() -> None:
    book = BeliefBook()
    for i in range(4):
        book.upsert(f"故事事实{i}", subject="story", register="story", confidence=0.9)
    book.upsert("现实中她怕黑", subject="user", register="reality", confidence=0.8)

    text = book.for_prompt(register="reality")
    # 跨登记簿只保留 2 条钩子
    assert text.count("故事事实") <= 2
    assert "现实中她怕黑" in text


def test_clear_register_drops_story_beliefs() -> None:
    book = BeliefBook()
    book.upsert("剧情：她是侦探", register="story")
    book.upsert("现实：她怕黑")
    book.clear_register("story")
    assert len(book) == 1
    assert book.beliefs[0].register == "reality"


def test_book_capacity_cap() -> None:
    from neo_fatum_chatter.domain import beliefs as beliefs_module

    book = BeliefBook()
    original = beliefs_module._MAX_BELIEFS
    beliefs_module._MAX_BELIEFS = 5
    try:
        for i in range(10):
            book.upsert(f"信念编号{i}")
        assert len(book) <= 5
    finally:
        beliefs_module._MAX_BELIEFS = original


def test_serialization_round_trip() -> None:
    book = BeliefBook()
    book.upsert("她周末喜欢睡懒觉", subject="user", confidence=0.7)
    restored = BeliefBook.from_list(book.to_list())
    assert len(restored) == 1
    assert restored.beliefs[0].statement == "她周末喜欢睡懒觉"
    assert Belief.from_dict({"statement": ""}) is None
