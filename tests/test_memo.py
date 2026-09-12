"""备忘录层测试。"""

from __future__ import annotations

import time

from neo_fatum_chatter.config import NFCConfig
from neo_fatum_chatter.context.sources.state_source import build_state_contributions
from neo_fatum_chatter.domain.memo import (
    MEMO_MAX_ENTRIES,
    Memo,
    MemoBook,
    clamp_expire_hours,
)
from neo_fatum_chatter.domain.session_state import NFCSession
from neo_fatum_chatter.models import NFCEventType

HOUR = 3600.0


def make_memo(content: str, *, hours: float = 24.0, created_at: float = 0.0) -> Memo:
    now = time.time()
    return Memo(
        content=content,
        expires_at=now + hours * HOUR,
        created_at=created_at or now,
    )


def test_upsert_new_then_refresh_keeps_created_at() -> None:
    book = MemoBook()
    old = time.time() - 5 * HOUR
    saved, is_new = book.upsert(make_memo("周六陪她去市场", created_at=old))
    assert is_new is True

    refreshed, is_new = book.upsert(make_memo("周六陪她去市场", hours=48.0))
    assert is_new is False
    assert refreshed.memo_id == saved.memo_id
    assert refreshed.created_at == old
    assert refreshed.remaining_seconds() > 47 * HOUR
    assert len(book) == 1


def test_upsert_refresh_updates_intent() -> None:
    book = MemoBook()
    book.upsert(Memo(content="别提游戏", intent="她在备考", expires_at=time.time() + HOUR))
    book.upsert(Memo(content="别提游戏", intent="她在备考，情绪敏感", expires_at=time.time() + HOUR))
    assert book.memos[0].intent == "她在备考，情绪敏感"


def test_capacity_evicts_oldest() -> None:
    book = MemoBook()
    for i in range(MEMO_MAX_ENTRIES + 2):
        book.upsert(
            make_memo(f"备忘{i}", created_at=time.time() - (100 - i) * HOUR)
        )
    assert len(book) == MEMO_MAX_ENTRIES
    assert all(memo.content != "备忘0" for memo in book.memos)
    assert all(memo.content != "备忘1" for memo in book.memos)


def test_expired_hidden_and_pruned() -> None:
    book = MemoBook()
    expired = make_memo("早就过期的约定", hours=2.0)
    expired.expires_at = time.time() - HOUR
    book.memos.append(expired)
    book.memos.append(make_memo("还有效的备忘"))

    assert [m.content for m in book.active()] == ["还有效的备忘"]
    assert book.prune_expired() == 1
    assert len(book) == 1


def test_delete_by_ids() -> None:
    book = MemoBook()
    a, _ = book.upsert(make_memo("甲"))
    b, _ = book.upsert(make_memo("乙"))
    book.upsert(make_memo("丙"))

    deleted = book.delete_by_ids([a.memo_id, b.memo_id, "不存在的id"])
    assert {m.content for m in deleted} == {"甲", "乙"}
    assert [m.content for m in book.memos] == ["丙"]
    assert book.delete_by_ids([]) == []


def test_render_empty_when_no_active_memos() -> None:
    assert MemoBook().render() == ""
    book = MemoBook()
    expired = make_memo("过期条目")
    expired.expires_at = time.time() - 1
    book.memos.append(expired)
    assert book.render() == ""


def test_render_contains_id_content_and_remaining() -> None:
    book = MemoBook()
    saved, _ = book.upsert(
        Memo(content="周五前想好送什么", intent="她生日", expires_at=time.time() + 3.5 * HOUR)
    )
    text = book.render()
    assert "## 我的备忘录" in text
    assert saved.memo_id in text
    assert "周五前想好送什么" in text
    assert "她生日" in text
    assert "剩余约 3 小时" in text


def test_clamp_expire_hours() -> None:
    assert clamp_expire_hours(0.5) == 1.0
    assert clamp_expire_hours(10_000) == 14 * 24.0
    assert clamp_expire_hours(48) == 48.0
    assert clamp_expire_hours("不是数字") == 24.0


def test_from_list_skips_invalid_entries() -> None:
    now = time.time()
    book = MemoBook.from_list(
        [
            "不是字典",
            {"content": ""},
            {"content": "缺时间的备忘"},  # expires_at 缺失 → 永久有效
            {"content": "正常备忘", "expires_at": now + 2 * HOUR, "created_at": now},
        ]
    )
    assert len(book) == 2
    assert book.memos[0].expires_at == 0.0
    assert book.memos[0].is_expired() is False


def test_session_roundtrip_preserves_memos() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    saved, _ = session.memos.upsert(make_memo("记得提醒她带伞"))
    session.add_memo_event(NFCEventType.MEMO_WRITTEN, saved)

    restored = NFCSession.from_dict(session.to_dict())
    assert len(restored.memos) == 1
    assert restored.memos.memos[0].content == "记得提醒她带伞"
    assert restored.memos.memos[0].memo_id == saved.memo_id
    # 审计事件进入心理活动流
    assert any(
        entry.event_type == NFCEventType.MEMO_WRITTEN
        for entry in restored.mental_log.entries
    )


def test_reset_context_keeps_memos() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.memos.upsert(make_memo("清空上下文也不该忘的事"))
    session.reset_context()
    assert len(session.memos) == 1


def test_state_contributions_render_memo_block() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    config = NFCConfig()
    # 中性状态：无备忘不渲染
    assert all(c.source != "nfc.memo" for c in build_state_contributions(session, config))

    session.memos.upsert(make_memo("明晚八点她有演唱会"))
    contributions = build_state_contributions(session, config)
    memo_blocks = [c for c in contributions if c.source == "nfc.memo"]
    assert len(memo_blocks) == 1
    assert memo_blocks[0].scope == "turn"
    assert memo_blocks[0].priority == 80
    assert "演唱会" in memo_blocks[0].content


def test_state_contributions_skip_memo_when_disabled() -> None:
    session = NFCSession(user_id="u", stream_id="s")
    session.memos.upsert(make_memo("被关闭的便签"))
    config = NFCConfig()
    config.memo.enabled = False
    assert all(c.source != "nfc.memo" for c in build_state_contributions(session, config))
