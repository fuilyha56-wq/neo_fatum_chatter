"""NFC 会话状态领域模型。

只包含会话状态的数据 + 行为方法（无 IO、无锁、无文件系统副作用）。
持久化由 ``persistence.session_store`` 负责。

历史路径：本模块原本在 ``neo_fatum_chatter.session``，与 Store 共处一文。
拆分后：
    - 状态结构与状态变更逻辑落在这里。
    - JSON 文件读写 / 锁 / 索引落在 ``persistence/session_store.py``。
    - ``session.py`` 退化为兼容 re-export，保持外部 import 路径不变。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from ..mental_log import MentalLog, MentalLogEntry
from ..models import NFCEventType, WaitingConfig
from .scene_state import SceneState

logger = get_logger("NFC_session_state")


@dataclass
class NFCSession:
    """NFC 会话状态数据。"""

    user_id: str
    stream_id: str
    platform: str = ""

    # 等待状态
    waiting_config: WaitingConfig = field(default_factory=WaitingConfig)
    consecutive_timeout_count: int = 0

    # 时间戳
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    last_user_message_at: float | None = None
    last_proactive_at: float | None = None

    # 模型预约的下一次主动思考时间（Unix 时间戳）
    # 若存在，条件主动发起逻辑不生效，直到预约时间到来或被清除
    scheduled_proactive_at: float | None = None
    scheduled_proactive_reason: str = ""  # 预约时给出的理由，触发时注入提示词

    # 心理活动流
    mental_log: MentalLog = field(default_factory=MentalLog)

    # 持久化对话链：序列化的 USER/ASSISTANT payload 列表，跨 execute() 重启保留上下文。
    # 每条条目格式：{"role": "user"|"assistant", "text": "...", "ts": <float，仅 user>}
    # chain_cutoff_ts 为链头第一个 user 条目的时间戳，供 build_fused_narrative 做截断。
    chain_payloads: list[dict[str, Any]] = field(default_factory=list)
    chain_cutoff_ts: float = 0.0

    # 融合叙事冻结缓存：跨 execute() 重建时复用字节级一致的历史叙事，
    # 以提升 LLM prompt prefix cache 命中率。cutoff 与 chain_cutoff_ts 对齐。
    frozen_narrative: str = ""
    frozen_narrative_cutoff_ts: float = 0.0

    # 近期记忆摘要（滚动压缩，替换式）：覆盖最近 compress_days_window 天的对话。
    # 由主聊天模型异步生成，以第一人称书写，重启后持久保留。
    history_summary: str = ""
    last_compress_at: float = 0.0     # 上次触发压缩的时间戳
    compress_round_count: int = 0     # 距上次压缩已完成的对话轮次数

    # 显式场景状态：只记录有证据支撑的场景信息，默认 unknown。
    scene_state: SceneState = field(default_factory=SceneState)

    # 情绪轨迹：记录最近的情绪变化，用于主动发起和行为调整
    # 每条格式：{"mood": str, "ts": float}
    mood_history: list[dict[str, Any]] = field(default_factory=list)
    _max_mood_entries: int = field(default=30, repr=False)

    # 用户活跃时段学习：按小时统计活跃次数
    # 格式：{hour_int: count}，24 个槽位
    activity_hours: dict[str, int] = field(default_factory=dict)

    # 统计
    total_interactions: int = 0

    def set_waiting(self, config: WaitingConfig) -> None:
        """设置等待状态。"""
        if config.max_wait_seconds <= 0:
            self.clear_waiting()
            return
        self.waiting_config = config

    def clear_waiting(self) -> None:
        """清除等待状态。"""
        self.waiting_config.reset()
        self.last_activity_at = time.time()

    def is_waiting(self) -> bool:
        """是否处于等待状态。"""
        return self.waiting_config.is_active()

    def add_user_message(
        self,
        content: str,
        user_name: str,
        user_id: str,
        timestamp: float | None = None,
        message_id: str = "",
    ) -> MentalLogEntry:
        """记录用户消息到活动流。"""
        msg_time = timestamp or time.time()
        entry = MentalLogEntry(
            event_type=NFCEventType.USER_MESSAGE,
            timestamp=msg_time,
            content=content,
            user_name=user_name,
            user_id=user_id,
            message_id=message_id,
        )

        # 标记回复时效
        if self.waiting_config.is_active():
            elapsed = self.waiting_config.get_elapsed_seconds()
            max_wait = self.waiting_config.max_wait_seconds
            if elapsed <= max_wait:
                entry.metadata["reply_status"] = "in_time"
            else:
                entry.metadata["reply_status"] = "late"
            entry.metadata["elapsed_seconds"] = elapsed
            entry.metadata["max_wait_seconds"] = max_wait

        self.mental_log.add(entry)
        self.consecutive_timeout_count = 0
        self.last_user_message_at = msg_time
        self.last_activity_at = msg_time
        # 记录用户活跃时段
        self.record_activity_hour(msg_time)
        return entry

    def add_bot_planning(
        self,
        thought: str,
        actions: list[dict[str, Any]],
        expected_reaction: str = "",
        max_wait_seconds: float = 0.0,
        raw_response: str = "",
    ) -> MentalLogEntry:
        """记录 Bot 规划到活动流。"""
        entry = MentalLogEntry(
            event_type=NFCEventType.BOT_PLANNING,
            timestamp=time.time(),
            thought=thought,
            actions=actions,
            expected_reaction=expected_reaction,
            max_wait_seconds=max_wait_seconds,
        )
        if raw_response:
            entry.metadata["raw_response"] = raw_response
        self.mental_log.add(entry)
        self.total_interactions += 1
        self.last_activity_at = time.time()
        return entry

    def set_scheduled_proactive(self, at: float | None, reason: str = "") -> None:
        """设置（或清除）模型预约的主动思考时间。

        Args:
            at: Unix 时间戳，None 表示清除预约
            reason: 预约理由，触发时注入提示词
        """
        self.scheduled_proactive_at = at
        self.scheduled_proactive_reason = reason if at is not None else ""

    def record_mood(self, mood: str) -> None:
        """记录一次情绪到轨迹历史。"""
        if not mood or not mood.strip():
            return
        self.mood_history.append({"mood": mood.strip(), "ts": time.time()})
        if len(self.mood_history) > self._max_mood_entries:
            self.mood_history = self.mood_history[-self._max_mood_entries:]

    def get_mood_summary(self, recent_n: int = 10) -> str:
        """获取最近 N 条情绪的简要描述。"""
        if not self.mood_history:
            return ""
        recent = self.mood_history[-recent_n:]
        moods = [entry["mood"] for entry in recent]
        return "、".join(moods)

    def get_dominant_mood(self, recent_n: int = 5) -> str:
        """获取最近 N 条中出现最多的情绪。"""
        if not self.mood_history:
            return ""
        recent = self.mood_history[-recent_n:]
        from collections import Counter
        counter = Counter(entry["mood"] for entry in recent)
        return counter.most_common(1)[0][0] if counter else ""

    def record_activity_hour(self, timestamp: float | None = None) -> None:
        """记录用户活跃时段（按小时统计）。"""
        ts = timestamp or time.time()
        hour = str(time.localtime(ts).tm_hour)
        self.activity_hours[hour] = self.activity_hours.get(hour, 0) + 1

    def get_active_hours(self, top_n: int = 5) -> list[int]:
        """获取用户最活跃的 N 个小时（按活跃次数降序）。"""
        if not self.activity_hours:
            return []
        sorted_hours = sorted(
            self.activity_hours.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        return [int(h) for h, _ in sorted_hours[:top_n]]

    def is_user_typically_active_now(self) -> bool:
        """判断当前时间是否在用户的典型活跃时段内。"""
        if not self.activity_hours:
            return True  # 无数据时默认活跃
        current_hour = str(time.localtime().tm_hour)
        total = sum(self.activity_hours.values())
        if total == 0:
            return True
        hour_count = self.activity_hours.get(current_hour, 0)
        # 如果当前小时的活跃占比超过平均值的 50%，认为活跃
        avg = total / 24.0
        return hour_count >= avg * 0.5

    def update_chain(
        self, new_entries: list[dict[str, Any]], max_payloads: int
    ) -> None:
        """追加新的对话条目到持久化链，并裁剪至 max_payloads 条目。

        Args:
            new_entries: 要追加的条目列表，每条格式为
                ``{"role": "user"|"assistant", "text": "...", "ts": float}``。
                USER 条目应携带 ``ts``（第一条未读消息的时间戳），
                ASSISTANT 条目无需携带 ``ts``。
            max_payloads: 链最大条目数，超出时删除最老的条目。
        """
        self.chain_payloads.extend(new_entries)
        if len(self.chain_payloads) > max_payloads:
            self.chain_payloads = self.chain_payloads[-max_payloads:]
            # 确保裁剪后首条是 user，避免孤立的 assistant 导致上下文非法
            while self.chain_payloads and self.chain_payloads[0].get("role") != "user":
                self.chain_payloads.pop(0)
        # 更新截止时间戳为链头第一个 user 条目的时间
        self.chain_cutoff_ts = 0.0
        for entry in self.chain_payloads:
            if entry.get("role") == "user":
                ts = entry.get("ts", 0.0)
                if isinstance(ts, (int, float)) and ts > 0:
                    self.chain_cutoff_ts = float(ts)
                break

        # 对话链推进或裁剪后，融合叙事截止点已变化，旧冻结缓存不再可靠。
        if self.frozen_narrative_cutoff_ts != self.chain_cutoff_ts:
            self.frozen_narrative = ""
            self.frozen_narrative_cutoff_ts = 0.0

    def clear_chain(self) -> None:
        """清空持久化对话链（重置上下文）。"""
        self.chain_payloads = []
        self.chain_cutoff_ts = 0.0
        self.frozen_narrative = ""
        self.frozen_narrative_cutoff_ts = 0.0

    def add_interrupt_event(self, interrupt_msgs: list[Any]) -> MentalLogEntry:
        """记录用户打断事件到活动流。

        当 LLM 生成期间检测到新消息时调用，让模型在下一轮上下文
        中感知到"我正在思考时被打断"这一事实，从而做出更自然的响应。

        Args:
            interrupt_msgs: 打断时到达的消息列表

        Returns:
            MentalLogEntry: 写入活动流的条目
        """
        count = len(interrupt_msgs)
        senders = {
            getattr(m, "sender_name", "") or getattr(m, "sender_id", "未知")
            for m in interrupt_msgs
        }
        sender_str = "、".join(sorted(senders))
        entry = MentalLogEntry(
            event_type=NFCEventType.USER_INTERRUPTED,
            timestamp=time.time(),
            content=(
                f"我正在思考时，{sender_str} 发来了 {count} 条新消息，"
                "我的回复是在没看到这些消息的情况下做出的。"
            ),
        )
        self.mental_log.add(entry)
        return entry

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "user_id": self.user_id,
            "stream_id": self.stream_id,
            "platform": self.platform,
            "waiting_config": self.waiting_config.to_dict(),
            "consecutive_timeout_count": self.consecutive_timeout_count,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "last_user_message_at": self.last_user_message_at,
            "last_proactive_at": self.last_proactive_at,
            "scheduled_proactive_at": self.scheduled_proactive_at,
            "scheduled_proactive_reason": self.scheduled_proactive_reason,
            "mental_log": self.mental_log.to_list(),
            "total_interactions": self.total_interactions,
            "chain_payloads": self.chain_payloads,
            "chain_cutoff_ts": self.chain_cutoff_ts,
            "frozen_narrative": self.frozen_narrative,
            "frozen_narrative_cutoff_ts": self.frozen_narrative_cutoff_ts,
            "history_summary": self.history_summary,
            "last_compress_at": self.last_compress_at,
            "compress_round_count": self.compress_round_count,
            "scene_state": self.scene_state.to_dict(),
            "mood_history": self.mood_history,
            "activity_hours": self.activity_hours,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], max_log_entries: int = 50) -> NFCSession:
        """从字典反序列化。

        Args:
            data: 序列化的字典数据
            max_log_entries: 活动流最大条目数（来自配置）
        """
        session = cls(
            user_id=data.get("user_id", ""),
            stream_id=data.get("stream_id", ""),
            platform=data.get("platform", ""),
        )
        session.waiting_config = WaitingConfig.from_dict(
            data.get("waiting_config", {})
        )
        session.consecutive_timeout_count = int(
            data.get("consecutive_timeout_count", 0)
        )
        session.created_at = float(data.get("created_at", time.time()))
        session.last_activity_at = float(data.get("last_activity_at", time.time()))
        session.last_user_message_at = data.get("last_user_message_at")
        session.last_proactive_at = data.get("last_proactive_at")
        session.scheduled_proactive_at = data.get("scheduled_proactive_at")
        session.scheduled_proactive_reason = data.get("scheduled_proactive_reason", "")
        session.mental_log = MentalLog.from_list(
            data.get("mental_log", []),
            max_entries=max_log_entries,
        )
        session.total_interactions = int(data.get("total_interactions", 0))
        # 持久化对话链（带基本校验）
        raw_chain = data.get("chain_payloads", [])
        if isinstance(raw_chain, list):
            session.chain_payloads = [
                entry for entry in raw_chain
                if isinstance(entry, dict)
                and entry.get("role") in ("user", "assistant")
                and isinstance(entry.get("text", ""), str)
            ]
        else:
            session.chain_payloads = []
        session.chain_cutoff_ts = float(data.get("chain_cutoff_ts", 0.0))
        session.frozen_narrative = str(data.get("frozen_narrative", "") or "")
        session.frozen_narrative_cutoff_ts = float(
            data.get("frozen_narrative_cutoff_ts", 0.0) or 0.0
        )
        # 近期记忆摘要
        session.history_summary = data.get("history_summary", "")
        session.last_compress_at = float(data.get("last_compress_at", 0.0))
        session.compress_round_count = int(data.get("compress_round_count", 0))
        session.scene_state = SceneState.from_dict(data.get("scene_state", {}))
        # 情绪轨迹
        raw_mood = data.get("mood_history", [])
        if isinstance(raw_mood, list):
            session.mood_history = [
                entry for entry in raw_mood
                if isinstance(entry, dict)
                and isinstance(entry.get("mood", ""), str)
                and isinstance(entry.get("ts", 0), (int, float))
            ]
        else:
            session.mood_history = []
        # 用户活跃时段
        raw_hours = data.get("activity_hours", {})
        if isinstance(raw_hours, dict):
            session.activity_hours = {
                str(k): int(v) for k, v in raw_hours.items()
                if isinstance(v, (int, float))
            }
        else:
            session.activity_hours = {}
        return session
