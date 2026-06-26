"""NFC 动作组件模块。

提供核心动作：
- NFCReplyAction: 发送消息
- DoNothingAction: 选择不回复
- QueryActivityPatternAction: 查询用户消息活跃分布
- RecordHabitAction: 记录用户习惯观察
- QueryHabitsAction: 查询已记录的用户习惯
"""

from __future__ import annotations

from .do_nothing import DoNothingAction
from .query_activity_pattern import QueryActivityPatternAction
from .query_habits import QueryHabitsAction
from .record_habit import RecordHabitAction
from .reply import NFCReplyAction

__all__ = [
    "DoNothingAction",
    "NFCReplyAction",
    "QueryActivityPatternAction",
    "RecordHabitAction",
    "QueryHabitsAction",
]
