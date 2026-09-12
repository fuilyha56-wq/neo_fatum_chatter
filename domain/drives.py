"""NFC 内驱状态机（Homeostatic Drives）。

角色的"潜意识层"：一组 0~1 的连续内部变量，随时间演化、被事件扰动，
不依赖任何消息、不消耗 LLM 调用。它是新核心机制与 KFC 反射弧的分水岭——
bot 在两次消息之间不再冻结，而是持续"过日子"。

设计约束：
    - 纯数学、无 IO、无 LLM；所有值 clamp 到 [0, 1]。
    - ``advance_to(now)`` 按真实流逝时间演化（幂等，重复调用同一时刻无副作用），
      因此磁盘会话加载后无需后台补算，下次事件触发时自然追平。
    - ``render_state_text()`` 输出分带量化（低/中/高）的第一人称状态描述，
      中性状态返回空串——保证 prompt 前缀缓存稳定，只在状态显著偏离时才渲染。

五个维度：
    - social_drive  社交欲：想找人说话的程度。空闲时缓慢上升（想说话），
                    得到回应后下降（满足）。
    - energy        精力：随时间恢复；每次回复消耗少量；深夜恢复更慢。
    - curiosity     好奇心：对对方近况的求知欲，缓慢上升，被消息喂饱后下降。
    - neglect       被忽视感：说完话对方迟迟不回时上升，得到回应后大幅衰减。
    - mood          情绪基线：向 0.5 回归；被及时回应抬升、被晾着压低。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 每小时演化速率（占满量程的比例）
_ENERGY_RECOVERY_PER_HOUR = 0.12
_ENERGY_NIGHT_RECOVERY_PER_HOUR = 0.05  # 23:00~07:00 恢复更慢
_SOCIAL_RISE_PER_HOUR = 0.08
_CURIOSITY_RISE_PER_HOUR = 0.05
_NEGLECT_DECAY_PER_HOUR = 0.03
_MOOD_REGRESSION_PER_HOUR = 0.06  # 向 0.5 均值回归

# 事件扰动幅度
_USER_MESSAGE_SOCIAL_DROP = 0.25
_USER_MESSAGE_CURIOSITY_RISE = 0.05
_USER_MESSAGE_NEGLECT_FACTOR = 0.3
_BOT_REPLY_ENERGY_COST_PER_SEG = 0.02
_BOT_REPLY_SOCIAL_DROP = 0.15
_REPLY_LATE_NEGLECT_RISE = 0.30
_REPLY_LATE_MOOD_DROP = 0.05
_REPLY_IN_TIME_MOOD_RISE = 0.03
_REPLY_IN_TIME_NEGLECT_FACTOR = 0.5
_TIMEOUT_MOOD_DROP = 0.05
_TIMEOUT_ENERGY_DROP = 0.03
_TIMEOUT_CURIOSITY_RISE = 0.02
_PROACTIVE_SOCIAL_FACTOR = 0.4


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _is_night(now: float) -> bool:
    hour = time.localtime(now).tm_hour
    return hour >= 23 or hour < 7


@dataclass
class DriveState:
    """角色内驱状态。"""

    social_drive: float = 0.5
    energy: float = 0.7
    curiosity: float = 0.4
    neglect: float = 0.0
    mood: float = 0.5
    last_updated: float = field(default_factory=time.time)

    # ── 时间演化 ─────────────────────────────────────────

    def advance_to(self, now: float) -> None:
        """把状态推进到指定时刻（按真实流逝时长演化，幂等）。"""
        if now <= self.last_updated:
            return
        hours = (now - self.last_updated) / 3600.0
        if hours <= 0:
            return

        recovery = (
            _ENERGY_NIGHT_RECOVERY_PER_HOUR
            if _is_night(now)
            else _ENERGY_RECOVERY_PER_HOUR
        )
        self.energy = _clamp01(self.energy + recovery * hours)

        # 社交欲空闲上升，但被忽视感高时会抑制（不想自讨没趣）
        rise = _SOCIAL_RISE_PER_HOUR * hours * (1.0 - 0.5 * self.neglect)
        self.social_drive = _clamp01(self.social_drive + rise)

        self.curiosity = _clamp01(
            self.curiosity + _CURIOSITY_RISE_PER_HOUR * hours
        )
        self.neglect = _clamp01(
            self.neglect - _NEGLECT_DECAY_PER_HOUR * hours
        )

        # 情绪向均值回归
        self.mood = _clamp01(
            self.mood + (0.5 - self.mood) * min(1.0, _MOOD_REGRESSION_PER_HOUR * hours)
        )

        self.last_updated = now

    # ── 事件扰动 ─────────────────────────────────────────

    def on_user_message(self, count: int = 1, now: float | None = None) -> None:
        """对方来消息：社交欲得到回应、被忽视感消散、好奇略升。"""
        self.advance_to(now or time.time())
        self.social_drive = _clamp01(
            self.social_drive - _USER_MESSAGE_SOCIAL_DROP * max(1, count) * 0.6
        )
        self.neglect = _clamp01(self.neglect * _USER_MESSAGE_NEGLECT_FACTOR)
        self.curiosity = _clamp01(self.curiosity + _USER_MESSAGE_CURIOSITY_RISE)
        self.mood = _clamp01(self.mood + 0.02)

    def on_bot_reply(self, segment_count: int = 1, now: float | None = None) -> None:
        """自己发完话：精力小幅消耗、社交欲释放。"""
        self.advance_to(now or time.time())
        self.energy = _clamp01(
            self.energy - _BOT_REPLY_ENERGY_COST_PER_SEG * max(1, segment_count)
        )
        self.social_drive = _clamp01(self.social_drive - _BOT_REPLY_SOCIAL_DROP)
        self.curiosity = _clamp01(self.curiosity - 0.03)

    def on_reply_timing(self, in_time: bool, now: float | None = None) -> None:
        """等待期结束后对方回应的时效反馈。"""
        self.advance_to(now or time.time())
        if in_time:
            self.mood = _clamp01(self.mood + _REPLY_IN_TIME_MOOD_RISE)
            self.neglect = _clamp01(self.neglect * _REPLY_IN_TIME_NEGLECT_FACTOR)
        else:
            self.neglect = _clamp01(self.neglect + _REPLY_LATE_NEGLECT_RISE)
            self.mood = _clamp01(self.mood - _REPLY_LATE_MOOD_DROP)

    def on_wait_timeout(self, now: float | None = None) -> None:
        """一次等待超时（对方一直没回）。"""
        self.advance_to(now or time.time())
        self.mood = _clamp01(self.mood - _TIMEOUT_MOOD_DROP)
        self.energy = _clamp01(self.energy - _TIMEOUT_ENERGY_DROP)
        self.curiosity = _clamp01(self.curiosity + _TIMEOUT_CURIOSITY_RISE)

    def on_proactive_fired(self, now: float | None = None) -> None:
        """主动发起后：社交欲释放。"""
        self.advance_to(now or time.time())
        self.social_drive = _clamp01(self.social_drive * _PROACTIVE_SOCIAL_FACTOR)

    # ── 决策调制 ─────────────────────────────────────────

    def modulate_wait(self, wait_seconds: float, strength: float = 0.3) -> float:
        """用内驱状态调制等待时长。

        社交欲高 → 更想在线等（缩短）；精力低/被忽视感高 → 懒得挂在线（拉长）。
        strength=0 时完全不干预；调制幅度被夹在 [0.5, 2.0] 倍。
        """
        if wait_seconds <= 0 or strength <= 0:
            return wait_seconds
        pull = (
            (0.5 - self.energy)          # 精力低 → 拉长
            + 0.6 * self.neglect          # 被冷落 → 拉长
            - 0.4 * (self.social_drive - 0.5)  # 社交欲高 → 缩短
        )
        factor = _clamp_factor(1.0 + strength * pull, 0.5, 2.0)
        return wait_seconds * factor

    def proactive_urge(self) -> float:
        """当前"想主动找人"的冲动强度（0~1）。"""
        return _clamp01(
            0.55 * self.social_drive
            + 0.30 * self.curiosity
            + 0.35 * self.neglect
            + 0.15 * (self.mood - 0.5)
        )

    # ── Prompt 渲染（分带量化，中性时不渲染） ───────────

    def render_state_text(self) -> str:
        """渲染为第一人称状态描述；全部中性时返回空串。

        刻意只描述"偏离中性"的维度，且用分带措辞而非数值——
        同一带内的轮次渲染结果字节级一致，保护 prefix cache。
        """
        lines: list[str] = []
        if self.energy < 0.25:
            lines.append("我现在挺累的，不太想动脑子")
        elif self.energy < 0.45:
            lines.append("我有点疲惫")
        elif self.energy > 0.75:
            lines.append("我现在精神不错")

        if self.social_drive > 0.75:
            lines.append("我挺想找人说话的")
        elif self.social_drive < 0.25:
            lines.append("我现在不太想聊天，想自己待着")

        if self.neglect > 0.5:
            lines.append("Ta 一直没回我，我心里有点不舒服")
        elif self.neglect > 0.25:
            lines.append("我有点在意 Ta 怎么还没回")

        if self.mood < 0.35:
            lines.append("我心情有点低落")
        elif self.mood > 0.65:
            lines.append("我现在心情不错")

        if self.curiosity > 0.7:
            lines.append("我挺好奇 Ta 最近在忙什么")

        if not lines:
            return ""
        return "# 我的状态\n" + "\n".join(f"- {line}" for line in lines)

    # ── 序列化 ───────────────────────────────────────────

    def to_dict(self) -> dict[str, float]:
        return {
            "social_drive": round(self.social_drive, 4),
            "energy": round(self.energy, 4),
            "curiosity": round(self.curiosity, 4),
            "neglect": round(self.neglect, 4),
            "mood": round(self.mood, 4),
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> DriveState:
        if not isinstance(data, dict):
            return cls()
        state = cls()
        for key in ("social_drive", "energy", "curiosity", "neglect", "mood"):
            raw = data.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                setattr(state, key, _clamp01(raw))
        raw_ts = data.get("last_updated")
        if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
            state.last_updated = float(raw_ts)
        return state


def _clamp_factor(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
