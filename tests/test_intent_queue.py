"""意图队列测试。"""

from __future__ import annotations

import time

from neo_fatum_chatter.domain.intent import (
    KIND_COMMITMENT,
    KIND_TOPIC_HOOK,
    Intent,
    IntentQueue,
)


def test_add_dedupes_by_content_and_refreshes() -> None:
    queue = IntentQueue()
    assert queue.add("问她面试结果", kind=KIND_TOPIC_HOOK, urge=0.3) == "added"
    assert queue.add("问她 面试结果", kind=KIND_TOPIC_HOOK, urge=0.5) == "refreshed"
    assert len(queue) == 1
    assert queue.intents[0].urge == 0.5


def test_grow_raises_urge_over_time() -> None:
    queue = IntentQueue()
    base = time.time()
    queue.intents.append(
        Intent(kind=KIND_TOPIC_HOOK, content="问问她考得怎么样", urge=0.5, created_at=base)
    )
    queue.grow_all(base + 5 * 3600)  # 5 小时
    assert queue.intents[0].urge > 0.5


def test_commitment_due_jumps_to_full_urge() -> None:
    queue = IntentQueue()
    base = time.time()
    queue.intents.append(
        Intent(
            kind=KIND_COMMITMENT,
            content="明天发作业给她",
            urge=0.4,
            created_at=base,
            deadline=base + 60,
        )
    )
    queue.grow_all(base + 120)
    assert queue.intents[0].urge == 1.0


def test_peek_fire_picks_strongest_pending() -> None:
    queue = IntentQueue()
    queue.add("弱钩子", urge=0.5)
    queue.add("强承诺", kind=KIND_COMMITMENT, urge=0.9)
    winner = queue.peek_fire(threshold=0.75)
    assert winner is not None and winner.content == "强承诺"
    queue.mark_fired(winner.id)
    assert queue.peek_fire(threshold=0.75) is None  # 已触发的不重复


def test_prune_removes_old_fired_and_stale_commitments() -> None:
    queue = IntentQueue()
    now = time.time()
    fired = Intent(content="已触发", urge=0.9)
    fired.fired_at = now - 7200
    stale = Intent(
        kind=KIND_COMMITMENT, content="过期承诺", deadline=now - 50 * 3600
    )
    fresh = Intent(content="新鲜钩子", urge=0.4)
    queue.intents = [fired, stale, fresh]

    removed = queue.prune(now)
    assert removed == 2
    assert [i.content for i in queue.intents] == ["新鲜钩子"]


def test_serialization_round_trip() -> None:
    queue = IntentQueue()
    queue.add("明天提醒她吃药", kind=KIND_COMMITMENT, deadline=time.time() + 3600)
    restored = IntentQueue.from_list(queue.to_list())
    assert len(restored) == 1
    assert restored.intents[0].kind == KIND_COMMITMENT
    assert restored.intents[0].deadline is not None
    assert Intent.from_dict({"content": "  "}) is None
