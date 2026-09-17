"""主动联系候选的轻量生命周期模型。

候选是“可能要联系”的审计记录，不是发送保证。它与实际调度时间、
用户状态和发送结果分开保存，避免把一次模型意图误当成已经发送。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

CANDIDATE_QUEUED = "queued"
CANDIDATE_DEFERRED = "deferred"
CANDIDATE_BLOCKED = "blocked"
CANDIDATE_DISPATCHING = "dispatching"
CANDIDATE_SENT = "sent"
CANDIDATE_EXPIRED = "expired"
CANDIDATE_CANCELLED = "cancelled"
_CANDIDATE_STATES = frozenset({
    CANDIDATE_QUEUED,
    CANDIDATE_DEFERRED,
    CANDIDATE_BLOCKED,
    CANDIDATE_DISPATCHING,
    CANDIDATE_SENT,
    CANDIDATE_EXPIRED,
    CANDIDATE_CANCELLED,
})



@dataclass
class ProactiveCandidate:
    """一个待评估的主动联系候选。"""

    origin_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    dedupe_key: str = ""
    route: str = "continuation"
    source: str = "intent"
    reason: str = ""
    preferred_at: float = 0.0
    window_start: float = 0.0
    best_until: float = 0.0
    expire_at: float = 0.0
    status: str = CANDIDATE_QUEUED
    block_reason: str = ""
    defer_reason: str = ""
    settlement_reason: str = ""
    revision: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.origin_id = str(self.origin_id or uuid.uuid4().hex[:12])[:64]
        self.dedupe_key = str(self.dedupe_key or self.origin_id)[:120]
        self.route = str(self.route or "continuation")[:40]
        self.source = str(self.source or "intent")[:40]
        self.reason = str(self.reason or "")[:300]
        if self.status not in _CANDIDATE_STATES:
            self.status = CANDIDATE_QUEUED
        self.revision = _positive_int(self.revision, 1)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            CANDIDATE_SENT,
            CANDIDATE_EXPIRED,
            CANDIDATE_CANCELLED,
        }

    @property
    def is_dispatching(self) -> bool:
        return self.status == CANDIDATE_DISPATCHING

    def is_due(self, now: float | None = None) -> bool:
        now = float(now if now is not None else time.time())
        if self.status != CANDIDATE_QUEUED:
            return False
        if self.expire_at and now >= self.expire_at:
            return False
        return now >= max(self.preferred_at, self.window_start)

    def mark(self, status: str, *, reason: str = "") -> bool:
        if status not in _CANDIDATE_STATES:
            return False
        if self.is_terminal and status != self.status:
            return False
        if status == self.status and not reason:
            return True
        self.status = status
        if status == CANDIDATE_QUEUED:
            self.block_reason = ""
            self.defer_reason = ""
        if status == CANDIDATE_BLOCKED:
            self.block_reason = reason[:160]
        elif status == CANDIDATE_DEFERRED:
            self.defer_reason = reason[:160]
        elif reason:
            self.settlement_reason = reason[:160]
        self.revision += 1
        self.updated_at = time.time()
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin_id": self.origin_id,
            "dedupe_key": self.dedupe_key,
            "route": self.route,
            "source": self.source,
            "reason": self.reason,
            "preferred_at": self.preferred_at,
            "window_start": self.window_start,
            "best_until": self.best_until,
            "expire_at": self.expire_at,
            "status": self.status,
            "block_reason": self.block_reason,
            "defer_reason": self.defer_reason,
            "settlement_reason": self.settlement_reason,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProactiveCandidate | None":
        if not isinstance(data, dict):
            return None
        return cls(
            origin_id=str(data.get("origin_id", "") or ""),
            dedupe_key=str(data.get("dedupe_key", "") or ""),
            route=str(data.get("route", "continuation") or "continuation"),
            source=str(data.get("source", "intent") or "intent"),
            reason=str(data.get("reason", "") or ""),
            preferred_at=_number(data.get("preferred_at")),
            window_start=_number(data.get("window_start")),
            best_until=_number(data.get("best_until")),
            expire_at=_number(data.get("expire_at")),
            status=str(data.get("status", CANDIDATE_QUEUED) or CANDIDATE_QUEUED),
            block_reason=str(data.get("block_reason", "") or ""),
            defer_reason=str(data.get("defer_reason", "") or ""),
            settlement_reason=str(data.get("settlement_reason", "") or ""),
            revision=_positive_int(data.get("revision"), 1),
            created_at=_number(data.get("created_at"), time.time()),
            updated_at=_number(data.get("updated_at"), time.time()),
        )


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        return value if value == value and value not in {float("inf"), float("-inf")} else default
    return default


def _positive_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return max(1, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


__all__ = [
    "CANDIDATE_BLOCKED",
    "CANDIDATE_CANCELLED",
    "CANDIDATE_DEFERRED",
    "CANDIDATE_DISPATCHING",
    "CANDIDATE_EXPIRED",
    "CANDIDATE_QUEUED",
    "CANDIDATE_SENT",
    "ProactiveCandidate",
]
