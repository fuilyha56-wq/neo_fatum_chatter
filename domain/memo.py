"""NFC 备忘录领域模型。

定位：LLM 显式标记的中短期关键事项——「接下来一段时间需要明确意识到
的事」。与既有记忆层互补：mental_log 是自动事件流，history_summary 是
叙事压缩，beliefs 是消化后的持久判断；备忘录由模型显式写入/删除，
带过期时间兜底，语义上更接近"贴在脑门上的便签"。

渲染经 ``context.sources.state_source`` 作为 turn 级 contribution 注入
提示词末尾，不进入持久对话链、不影响前缀缓存。数据随会话持久化，
``reset_context``（清空上下文）时作为模型的自主笔记保留，靠过期时间
自然消亡。
"""

from __future__ import annotations

import datetime
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

MEMO_MAX_ENTRIES: int = 10
"""单流最大有效备忘条目数；超出按 ``created_at`` 升序淘汰。"""

MEMO_DEFAULT_EXPIRE_HOURS: float = 24.0
"""LLM 未指定 ``expire_hours`` 时的默认存活时长。"""

MEMO_MIN_EXPIRE_HOURS: float = 1.0
"""单条备忘最短存活时长（小时）。"""

MEMO_MAX_EXPIRE_HOURS: float = 14 * 24.0
"""单条备忘最长存活时长（小时）。"""

_MEMO_ID_LENGTH = 8
_SECONDS_PER_HOUR = 3600.0


def clamp_expire_hours(value: float) -> float:
    """把过期时长夹到边界内，非法输入回退默认值。"""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return MEMO_DEFAULT_EXPIRE_HOURS
    return max(MEMO_MIN_EXPIRE_HOURS, min(hours, MEMO_MAX_EXPIRE_HOURS))


@dataclass
class Memo:
    """一条带过期时间的私人备忘。"""

    content: str
    intent: str = ""
    memo_id: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0

    def __post_init__(self) -> None:
        """补齐缺省的 id 与创建时间。"""
        if not self.memo_id:
            self.memo_id = uuid.uuid4().hex[:_MEMO_ID_LENGTH]
        if self.created_at <= 0:
            self.created_at = time.time()

    def is_expired(self, now: float | None = None) -> bool:
        """判断该备忘是否已过期。"""
        current = now if now is not None else time.time()
        return self.expires_at > 0 and current >= self.expires_at

    def remaining_seconds(self, now: float | None = None) -> float:
        """返回剩余秒数；已过期返回 0。"""
        current = now if now is not None else time.time()
        return max(0.0, self.expires_at - current)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "memo_id": self.memo_id,
            "content": self.content,
            "intent": self.intent,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Memo | None:
        """从字典反序列化；结构非法时返回 ``None``。"""
        if not isinstance(data, dict):
            return None
        content = str(data.get("content", "") or "").strip()
        if not content:
            return None
        memo = cls(
            content=content,
            intent=str(data.get("intent", "") or ""),
            memo_id=str(data.get("memo_id", "") or ""),
        )
        # 缺失/非法的时间戳回退为"永久有效"：宁可多显示一条，也不静默丢失
        expires_at = data.get("expires_at")
        memo.expires_at = (
            float(expires_at) if isinstance(expires_at, (int, float)) else 0.0
        )
        created_at = data.get("created_at")
        if isinstance(created_at, (int, float)) and created_at > 0:
            memo.created_at = float(created_at)
        return memo


@dataclass
class MemoBook:
    """单流备忘集合：写入去重、容量淘汰、过期清理与提示词渲染。"""

    memos: list[Memo] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.memos)

    def upsert(
        self,
        memo: Memo,
        *,
        max_entries: int = MEMO_MAX_ENTRIES,
        now: float | None = None,
    ) -> tuple[Memo, bool]:
        """写入或刷新一条备忘。

        若已存在 ``content`` 完全相同的有效备忘，只刷新其过期时间和
        intent（保留原 ``created_at`` 与 id）；否则在必要时淘汰最早的
        一条后追加。

        Args:
            memo: 待写入的备忘。
            max_entries: 容量上限，超出按创建时间淘汰最早条目。
            now: 当前时间戳，默认取系统时间。

        Returns:
            tuple: ``(最终落盘的备忘, 是否为新建)``。
        """
        self.prune_expired(now=now)

        normalized = memo.content.strip()
        if not normalized:
            return memo, False

        for existing in self.memos:
            if existing.content.strip() == normalized:
                existing.expires_at = memo.expires_at
                if memo.intent.strip():
                    existing.intent = memo.intent
                return existing, False

        limit = max(1, int(max_entries))
        while len(self.memos) >= limit:
            oldest_index = min(
                range(len(self.memos)),
                key=lambda index: self.memos[index].created_at,
            )
            self.memos.pop(oldest_index)

        self.memos.append(memo)
        return memo, True

    def delete_by_ids(self, memo_ids: list[str]) -> list[Memo]:
        """按 id 删除备忘，返回实际被删除的条目。"""
        target_ids = {
            str(item).strip()
            for item in memo_ids
            if str(item).strip()
        }
        if not target_ids or not self.memos:
            return []
        deleted = [memo for memo in self.memos if memo.memo_id in target_ids]
        if deleted:
            self.memos = [
                memo for memo in self.memos if memo.memo_id not in target_ids
            ]
        return deleted

    def active(self, now: float | None = None) -> list[Memo]:
        """返回未过期备忘，按创建时间升序（模型读到的时序自然）。"""
        current = now if now is not None else time.time()
        return sorted(
            (memo for memo in self.memos if not memo.is_expired(current)),
            key=lambda memo: memo.created_at,
        )

    def prune_expired(self, now: float | None = None) -> int:
        """清除已过期备忘，返回清除条数。"""
        current = now if now is not None else time.time()
        expired = [memo for memo in self.memos if memo.is_expired(current)]
        if expired:
            self.memos = [memo for memo in self.memos if not memo.is_expired(current)]
        return len(expired)

    def render(self, now: float | None = None) -> str:
        """渲染完整备忘录文本块；无有效备忘时返回空串（中性不渲染）。

        剩余时间按小时取整，避免同一小时内渲染文本抖动。
        """
        current = now if now is not None else time.time()
        valid = self.active(now=current)
        if not valid:
            return ""

        lines: list[str] = [
            "## 我的备忘录",
            _MEMO_GUIDANCE,
            "",
            "### 当前条目",
        ]
        for index, memo in enumerate(valid, start=1):
            entry_lines = [
                f"#{index}",
                f"- id: {memo.memo_id}",
                f"- 内容: {memo.content}",
            ]
            if memo.intent.strip():
                entry_lines.append(f"- 动机: {memo.intent.strip()}")
            entry_lines.append(
                f"- 记于: {_format_datetime(memo.created_at)}"
                f"（{_format_remaining(memo, current)}）"
            )
            lines.append("\n".join(entry_lines))

        return "\n".join(lines)

    def to_list(self) -> list[dict[str, Any]]:
        """序列化为列表。"""
        return [memo.to_dict() for memo in self.memos]

    @classmethod
    def from_list(cls, data: Any) -> "MemoBook":
        """从列表反序列化；非法条目逐条跳过。"""
        book = cls()
        if isinstance(data, list):
            for item in data:
                memo = Memo.from_dict(item)
                if memo is not None:
                    book.memos.append(memo)
        return book


_MEMO_GUIDANCE = (
    "这些是你给自己留下的备忘便签，记着接下来一段时间需要意识到的事。"
    "**不需要时刻提起或反复念叨**，只在恰当的时机自然地用上：\n"
    "- 对方提到的话题刚好和某条备忘相关时，你心里能想起这事；\n"
    "- 某件被记录的事到了该兑现的时间，你能主动行动；\n"
    "- 某件事已经做了或不再相关时，主动调用 nfc_memo_delete 清理它，"
    "避免便签和实际状态对不上。\n\n"
    "写入时机：你觉得「过几个小时或几天后回看时还想知道这件事」，"
    "就可以调用 nfc_memo 记下，不必拘泥于「该不该记」。"
    "过期时间只是兜底，不要依赖它。"
)


def _format_datetime(timestamp: float) -> str:
    """把时间戳格式化为「年-月-日 时:分」。"""
    try:
        return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "未知时间"


def _format_remaining(memo: Memo, now: float) -> str:
    """把剩余时间格式化为人类可读文本（按小时取整，避免渲染抖动）。"""
    remaining_hours = memo.remaining_seconds(now) / _SECONDS_PER_HOUR
    if remaining_hours <= 0:
        return "已过期"
    if remaining_hours < 1:
        return "剩余不到 1 小时"
    if remaining_hours < 48:
        return f"剩余约 {int(remaining_hours)} 小时"
    return f"剩余约 {int(remaining_hours / 24)} 天"
