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

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from ..mental_log import MentalLog, MentalLogEntry
from ..models import NFCEventType, WaitingConfig
from .scene_state import SceneState

logger = get_logger("NFC_session_state")

_SYSTEM_REMINDER_BLOCK_RE = re.compile(
    r"\n?\s*<system_reminder>.*?</system_reminder>\s*\n?",
    re.DOTALL,
)

_LEGACY_SYSTEM_REMINDER_BLOCK_RE = re.compile(
    r"\n?\s*\[SYSTEM REMINDER\]\n.*?(?=\n\[[^\n\]]+\]|\Z)",
    re.DOTALL,
)

# 匹配 timeout 提示块：以"你发出消息已经过去"或"你已经主动说了"开头的段落
_TIMEOUT_PROMPT_RE = re.compile(
    r"\n?(?:你发出消息已经过去|你已经主动说了)\s*\d+.*?(?=\n\[新消息\]|\n\[|\Z)",
    re.DOTALL,
)

# 匹配 send_to 动态注入块标题
_SEND_TO_DYNAMIC_BLOCK_RE = re.compile(
    r"\n?## 本轮末尾动态补充上下文\n.*?(?=\n## |\n\[新消息\]|\Z)",
    re.DOTALL,
)

# 匹配 perception 内部提示标签
_PERCEPTION_TAG_RE = re.compile(
    r"\n?\s*<(?:perception_completed|unsent_perception_draft|inner_response_to_silence)>.*?"
    r"</(?:perception_completed|unsent_perception_draft|inner_response_to_silence)>\s*\n?",
    re.DOTALL,
)


def _optional_float(value: Any) -> float | None:
    """将值转为 float，若为 None 返回 None。用于 from_dict 中时间戳字段。"""
    if value is None:
        return None
    return float(value)


def _is_real_number(value: Any) -> bool:
    """判断是否为真正的数值（排除 bool，bool 是 int 的子类）。

    JSON 中损坏的 ``true`` / ``false`` 会被解析为 Python ``bool``，
    若用 ``isinstance(value, (int, float))`` 校验会被误判为合法时间戳/计数。
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def strip_persisted_system_reminders(text: str) -> str:
    """移除误写入持久历史的 system reminder 注入块及运行时临时提示。"""

    cleaned = _SYSTEM_REMINDER_BLOCK_RE.sub("\n", text)
    cleaned = _LEGACY_SYSTEM_REMINDER_BLOCK_RE.sub("\n", cleaned)
    cleaned = _TIMEOUT_PROMPT_RE.sub("\n", cleaned)
    cleaned = _SEND_TO_DYNAMIC_BLOCK_RE.sub("\n", cleaned)
    cleaned = _PERCEPTION_TAG_RE.sub("\n", cleaned)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


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

    # 主动思考触发时由 ProactiveHandler 写入的富上下文（silence/recent_activity/reason）。
    # 仅运行时传递，不持久化：plan_user_turn 读取后作为 turn contribution 注入到
    # transient extra_payload，避免写入 user_text 破坏 prompt prefix cache。
    pending_proactive_context: str = ""

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

    # 抑制期消息缓冲：当 wait.suppress_early_wake=true 时，等待期间到达的
    # 新消息会被显式收集到这里（按 message_id 去重），等待超时后一次性
    # 合并为单条 USER payload 注入，避免每条新消息都触发一次上下文构建。
    # 注意：仅在运行时使用，不参与持久化。
    suppressed_messages: list[Any] = field(default_factory=list, repr=False)

    # 用户活跃时段学习：按小时统计活跃次数
    # 格式：{hour_int: count}，24 个槽位
    activity_hours: dict[str, int] = field(default_factory=dict)

    # 用户习惯观察：LLM 主动记录的作息、行为模式等
    # 每条格式：{"habit_text": str, "category": str, "recorded_at": float}
    user_habits: list[dict[str, Any]] = field(default_factory=list)
    _max_habit_entries: int = field(default=50, repr=False)

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

    def add_habit(self, habit_text: str, category: str = "") -> None:
        """记录一条用户习惯观察。"""
        if not habit_text or not habit_text.strip():
            return
        self.user_habits.append({
            "habit_text": habit_text.strip(),
            "category": category.strip(),
            "recorded_at": time.time(),
        })
        if len(self.user_habits) > self._max_habit_entries:
            self.user_habits = self.user_habits[-self._max_habit_entries:]

    def get_habits(self, category: str = "") -> list[dict[str, Any]]:
        """获取已记录的习惯观察，可按分类过滤。"""
        if not category or not category.strip():
            return list(self.user_habits)
        cat = category.strip().lower()
        return [
            h for h in self.user_habits
            if h.get("category", "").lower() == cat
        ]

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
        cleaned_entries: list[dict[str, Any]] = []
        for entry in new_entries:
            if entry.get("role") == "user":
                cleaned_entry = dict(entry)
                cleaned_entry["text"] = strip_persisted_system_reminders(
                    str(cleaned_entry.get("text", "") or "")
                )
                if not cleaned_entry["text"]:
                    continue
                cleaned_entries.append(cleaned_entry)
            else:
                cleaned_entries.append(entry)

        self.chain_payloads.extend(cleaned_entries)
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
            "user_habits": self.user_habits,
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
        session.last_user_message_at = _optional_float(data.get("last_user_message_at"))
        session.last_proactive_at = _optional_float(data.get("last_proactive_at"))
        session.scheduled_proactive_at = _optional_float(data.get("scheduled_proactive_at"))
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
                and _is_real_number(entry.get("ts", 0))
            ]
        else:
            session.mood_history = []
        # 用户活跃时段
        raw_hours = data.get("activity_hours", {})
        if isinstance(raw_hours, dict):
            session.activity_hours = {
                str(k): int(v) for k, v in raw_hours.items()
                if _is_real_number(v)
            }
        else:
            session.activity_hours = {}
        # 用户习惯观察
        raw_habits = data.get("user_habits", [])
        if isinstance(raw_habits, list):
            session.user_habits = [
                entry for entry in raw_habits
                if isinstance(entry, dict)
                and isinstance(entry.get("habit_text"), str)
                and entry.get("habit_text", "").strip()
                and _is_real_number(entry.get("recorded_at"))
            ]
        else:
            session.user_habits = []
        return session
