"""NFC 信念层（Belief Book）。

把"想过的"沉淀为"知道的"：对用户 / 关系 / 剧情的持久化理解，
带置信度与证据时间。与 mental_log（事件日志）、history_summary（叙事压缩）
构成三层记忆——前两者记录"发生了什么"，信念层记录"我因此知道了什么"。

纯 Python 结构，LLM 提取由 ``services/belief_service`` 负责；
本模块只做合并语义：同信念强化、矛盾削弱、随时间衰减、容量上限。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

_MAX_BELIEFS = 30
_REINFORCE_STEP = 0.15
_MAX_CONFIDENCE = 0.95
_DECAY_PER_DAY = 0.05
_DROP_BELOW = 0.15

# 信念归属主体
SUBJECT_USER = "user"
SUBJECT_RELATIONSHIP = "relationship"
SUBJECT_STORY = "story"
_VALID_SUBJECTS = frozenset(
    {SUBJECT_USER, SUBJECT_RELATIONSHIP, "self", SUBJECT_STORY}
)


def _normalize_statement(statement: str) -> str:
    """归一化陈述文本用于去重比对（去空白、去首尾标点差异）。"""
    text = re.sub(r"\s+", "", statement or "")
    text = re.sub(r"[。．.!！?？?，,、;；:：~～…\-—]+$", "", text)
    return text.lower()


def _belief_id(normalized: str) -> str:
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:8]


@dataclass
class Belief:
    """单条信念。"""

    statement: str
    subject: str = SUBJECT_USER
    register: str = "reality"
    confidence: float = 0.5
    evidence_count: int = 1
    created_at: float = field(default_factory=time.time)
    last_evidence_at: float = field(default_factory=time.time)
    decayed_through: float = 0.0  # 衰减已结算到的时刻；0 = 尚未结算过

    def to_dict(self) -> dict:
        return {
            "statement": self.statement,
            "subject": self.subject,
            "register": self.register,
            "confidence": round(self.confidence, 3),
            "evidence_count": self.evidence_count,
            "created_at": self.created_at,
            "last_evidence_at": self.last_evidence_at,
            "decayed_through": self.decayed_through,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Belief | None:
        statement = str(data.get("statement", "") or "").strip()
        if not statement:
            return None
        subject = str(data.get("subject", SUBJECT_USER) or SUBJECT_USER)
        belief = cls(
            statement=statement,
            subject=subject if subject in _VALID_SUBJECTS else SUBJECT_USER,
            register=str(data.get("register", "reality") or "reality"),
            confidence=_clamp01_float(data.get("confidence", 0.5)),
            evidence_count=max(1, int(data.get("evidence_count", 1) or 1)),
            created_at=_as_float(data.get("created_at"), time.time()),
            last_evidence_at=_as_float(data.get("last_evidence_at"), time.time()),
            decayed_through=_as_float(data.get("decayed_through"), 0.0),
        )
        return belief


class BeliefBook:
    """信念集合：去重强化、矛盾削弱、时间衰减、上限裁剪。"""

    def __init__(self, beliefs: list[Belief] | None = None) -> None:
        self.beliefs: list[Belief] = beliefs or []

    # ── 写入 ─────────────────────────────────────────────

    def upsert(
        self,
        statement: str,
        *,
        subject: str = SUBJECT_USER,
        register: str = "reality",
        confidence: float = 0.5,
    ) -> str:
        """新增或强化一条信念。

        Returns:
            "added" | "reinforced"
        """
        text = (statement or "").strip()
        if not text:
            return "ignored"
        normalized = _normalize_statement(text)
        if not normalized:
            return "ignored"

        for belief in self.beliefs:
            if _normalize_statement(belief.statement) == normalized:
                belief.confidence = min(
                    _MAX_CONFIDENCE,
                    max(belief.confidence, _clamp01_float(confidence))
                    + _REINFORCE_STEP * (1.0 - belief.confidence),
                )
                belief.evidence_count += 1
                belief.last_evidence_at = time.time()
                return "reinforced"

        self.beliefs.append(
            Belief(
                statement=text,
                subject=subject if subject in _VALID_SUBJECTS else SUBJECT_USER,
                register=register,
                confidence=_clamp01_float(confidence),
            )
        )
        self._trim()
        return "added"

    def weaken(self, statement: str) -> bool:
        """削弱一条与新证据矛盾的信念；置信度跌破阈值时移除。"""
        normalized = _normalize_statement(statement)
        for belief in list(self.beliefs):
            if _normalize_statement(belief.statement) == normalized:
                belief.confidence -= 0.35
                if belief.confidence < _DROP_BELOW:
                    self.beliefs.remove(belief)
                    return True
                return False
        return False

    def decay(self, now: float | None = None) -> int:
        """按最后证据时间增量衰减陈旧信念，移除失效项。返回移除数量。

        以 ``decayed_through`` 结算已衰减过的时间段，幂等可重入：
        多次调用不会对同一段时间重复扣减；首次调用会一次性追平
        全部历史衰减（含磁盘会话恢复后的补算）。
        """
        now = now or time.time()
        removed = 0
        for belief in list(self.beliefs):
            base = max(belief.decayed_through, belief.last_evidence_at)
            days = max(0.0, (now - base) / 86400.0)
            belief.decayed_through = now
            if days > 0:
                belief.confidence -= _DECAY_PER_DAY * days
            if belief.confidence < _DROP_BELOW:
                self.beliefs.remove(belief)
                removed += 1
        return removed

    def clear_register(self, register: str) -> None:
        """清空某个登记簿的信念（如剧情结束时丢弃剧情信念）。"""
        self.beliefs = [b for b in self.beliefs if b.register != register]

    # ── 读取 ─────────────────────────────────────────────

    def for_prompt(
        self,
        *,
        register: str = "reality",
        include_cross_register: bool = True,
        top_n: int = 8,
        min_confidence: float = 0.3,
    ) -> str:
        """渲染 prompt 信念块；无合格信念时返回空串。

        主动渲染当前登记簿的信念，另一登记簿只保留一条梗概行
        （跨世界渗透的钩子），避免 prompt 膨胀与串戏。
        """
        primary = [
            b
            for b in self.beliefs
            if b.register == register and b.confidence >= min_confidence
        ]
        primary.sort(key=lambda b: (-b.confidence, -b.last_evidence_at))
        primary = primary[:top_n]

        other_register = "story" if register == "reality" else "reality"
        cross = [
            b
            for b in self.beliefs
            if b.register == other_register and b.confidence >= min_confidence
        ]
        cross.sort(key=lambda b: (-b.confidence, -b.last_evidence_at))
        cross = cross[:2]

        if not primary and not cross:
            return ""

        lines: list[str] = []
        if register == "reality":
            lines.append("# 你已经知道的（关于 Ta 和你们的关系）")
        else:
            lines.append("# 故事里你已经知道的")
        subject_label = {
            SUBJECT_USER: "关于Ta",
            SUBJECT_RELATIONSHIP: "关于你们",
            "self": "关于自己",
            SUBJECT_STORY: "剧情",
        }
        for belief in primary:
            label = subject_label.get(belief.subject, "")
            prefix = f"{label}：" if label else ""
            lines.append(f"- {prefix}{belief.statement}")

        if cross and include_cross_register:
            joined = "；".join(b.statement for b in cross)
            if other_register == "story":
                lines.append(
                    f"- 你们的故事里：{joined}"
                    "（那是你们一起编的故事里的事，可以在合适的时候自然提起）"
                )
            else:
                lines.append(
                    f"- 现实中：{joined}"
                    "（那是现实中真实发生的事，在故事里也不要忘了它）"
                )

        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.beliefs)

    # ── 序列化 ───────────────────────────────────────────

    def to_list(self) -> list[dict]:
        return [b.to_dict() for b in self.beliefs]

    @classmethod
    def from_list(cls, data: list | None) -> BeliefBook:
        book = cls()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    belief = Belief.from_dict(item)
                    if belief is not None:
                        book.beliefs.append(belief)
            book._trim()
        return book

    def _trim(self) -> None:
        if len(self.beliefs) > _MAX_BELIEFS:
            self.beliefs.sort(key=lambda b: -b.last_evidence_at)
            self.beliefs = self.beliefs[-_MAX_BELIEFS:]


def _clamp01_float(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _as_float(value, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default
