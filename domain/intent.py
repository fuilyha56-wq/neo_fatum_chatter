"""NFC 主动意图队列（Intent Queue）。

把"想主动找人"从掷骰子改成欲望溢出：话题钩子、到期承诺、内驱冲动
各自生成带冲动值（urge）的候选意图，冲动随时间增长，最强者越过阈值
才触发主动轮。旧的沉默概率路径保留为无意图时的兜底。

意图来源（kind）：
    - commitment  承诺到期（"明天给你看作业"）—— deadline 到点冲动拉满
    - topic_hook  未闭合话题（S1 从对话中挖出的"想追问的事"）
    - drive       内驱溢出（社交欲/被忽视感自然涨过阈值时生成，一次性）
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

_MAX_INTENTS = 12
_COMMITMENT_URGE_WHEN_DUE = 1.0
_FIRED_KEEP_SECONDS = 3600.0
_COMMITMENT_EXPIRE_AFTER_DUE = 48 * 3600.0

KIND_COMMITMENT = "commitment"
KIND_TOPIC_HOOK = "topic_hook"
KIND_DRIVE = "drive"

_DEFAULT_GROWTH_PER_HOUR = {
    KIND_COMMITMENT: 0.05,
    KIND_TOPIC_HOOK: 0.06,
    KIND_DRIVE: 0.0,  # drive 意图的 urge 由 thinker 每次现算
}


def _content_key(content: str) -> str:
    return "".join(content.split()).lower()


@dataclass
class Intent:
    """一条主动意图。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    kind: str = KIND_TOPIC_HOOK
    content: str = ""
    register: str = "reality"
    urge: float = 0.3
    created_at: float = field(default_factory=time.time)
    deadline: float | None = None
    fired_at: float | None = None
    growth_per_hour: float | None = None
    last_grown_at: float = 0.0  # 上次冲动增长结算时刻；0 = 尚未结算过

    def effective_growth(self) -> float:
        if self.growth_per_hour is not None:
            return self.growth_per_hour
        return _DEFAULT_GROWTH_PER_HOUR.get(self.kind, 0.05)

    def grow(self, now: float) -> None:
        """按流逝时间增量增长冲动；到期承诺直接拉满。

        以 ``last_grown_at``（缺省回落到 ``created_at``）为结算基准，
        幂等可重入：多次调用不会重复计入同一段时间。
        """
        if self.fired_at is not None:
            return
        if self.deadline is not None and now >= self.deadline:
            self.urge = _COMMITMENT_URGE_WHEN_DUE
            return
        base = self.last_grown_at if self.last_grown_at > 0 else self.created_at
        hours = max(0.0, (now - base) / 3600.0)
        if hours <= 0:
            return
        self.urge = min(1.0, self.urge + self.effective_growth() * hours)
        self.last_grown_at = now

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "register": self.register,
            "urge": round(self.urge, 4),
            "created_at": self.created_at,
            "deadline": self.deadline,
            "fired_at": self.fired_at,
            "last_grown_at": self.last_grown_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Intent | None:
        content = str(data.get("content", "") or "").strip()
        if not content:
            return None
        deadline = data.get("deadline")
        fired_at = data.get("fired_at")
        return cls(
            id=str(data.get("id", "") or uuid.uuid4().hex[:8]),
            kind=str(data.get("kind", KIND_TOPIC_HOOK) or KIND_TOPIC_HOOK),
            content=content,
            register=str(data.get("register", "reality") or "reality"),
            urge=_clamp01(data.get("urge", 0.3)),
            created_at=_as_float(data.get("created_at"), time.time()),
            deadline=_as_float_or_none(deadline),
            fired_at=_as_float_or_none(fired_at),
            last_grown_at=_as_float(data.get("last_grown_at"), 0.0),
        )


class IntentQueue:
    """意图集合：添加去重、冲动增长、阈值触发、过期清理。"""

    def __init__(self, intents: list[Intent] | None = None) -> None:
        self.intents: list[Intent] = intents or []

    def add(
        self,
        content: str,
        *,
        kind: str = KIND_TOPIC_HOOK,
        register: str = "reality",
        urge: float = 0.3,
        deadline: float | None = None,
    ) -> str:
        """添加/刷新一条意图。同内容意图存在时刷新 urge 与 deadline。

        Returns:
            "added" | "refreshed" | "ignored"
        """
        text = (content or "").strip()
        if not text:
            return "ignored"
        key = _content_key(text)
        if not key:
            return "ignored"

        for intent in self.intents:
            if intent.fired_at is None and _content_key(intent.content) == key:
                intent.urge = max(intent.urge, _clamp01(urge))
                if deadline is not None:
                    intent.deadline = deadline
                if kind == KIND_COMMITMENT:
                    intent.kind = KIND_COMMITMENT
                return "refreshed"

        self.intents.append(
            Intent(
                kind=kind,
                content=text,
                register=register,
                urge=_clamp01(urge),
                deadline=deadline,
            )
        )
        self._trim()
        return "added"

    def grow_all(self, now: float | None = None) -> None:
        now = now or time.time()
        for intent in self.intents:
            intent.grow(now)

    def peek_fire(self, threshold: float = 0.75, *, now: float | None = None) -> Intent | None:
        """取当前冲动最高、达到阈值且未触发过的意图（不修改状态）。

        冲动值打平时承诺优先（时间敏感），其次按创建时间先后。
        """
        now = now or time.time()
        candidates = [
            i
            for i in self.intents
            if i.fired_at is None and i.urge >= threshold
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda i: (
                -i.urge,
                0 if i.kind == KIND_COMMITMENT else 1,
                -i.created_at,
            )
        )
        return candidates[0]

    def mark_fired(self, intent_id: str, now: float | None = None) -> None:
        now = now or time.time()
        for intent in self.intents:
            if intent.id == intent_id:
                intent.fired_at = now
                return

    def prune(self, now: float | None = None) -> int:
        """清理已触发过久的与过期承诺。返回清理数量。"""
        now = now or time.time()
        kept: list[Intent] = []
        removed = 0
        for intent in self.intents:
            if intent.fired_at is not None and now - intent.fired_at > _FIRED_KEEP_SECONDS:
                removed += 1
                continue
            if (
                intent.kind == KIND_COMMITMENT
                and intent.deadline is not None
                and now - intent.deadline > _COMMITMENT_EXPIRE_AFTER_DUE
            ):
                removed += 1
                continue
            kept.append(intent)
        self.intents = kept
        return removed

    def pending_count(self) -> int:
        return sum(1 for i in self.intents if i.fired_at is None)

    def __len__(self) -> int:
        return len(self.intents)

    def to_list(self) -> list[dict]:
        return [i.to_dict() for i in self.intents]

    @classmethod
    def from_list(cls, data: list | None) -> IntentQueue:
        queue = cls()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    intent = Intent.from_dict(item)
                    if intent is not None:
                        queue.intents.append(intent)
            queue._trim()
        return queue

    def _trim(self) -> None:
        if len(self.intents) > _MAX_INTENTS:
            pending = [i for i in self.intents if i.fired_at is None]
            fired = [i for i in self.intents if i.fired_at is not None]
            pending.sort(key=lambda i: -i.urge)
            self.intents = pending[:_MAX_INTENTS] + fired[-2:]


def _clamp01(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.3


def _as_float(value, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _as_float_or_none(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
