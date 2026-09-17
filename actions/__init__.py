"""NFC 动作组件模块。

提供核心动作：
- NFCReplyAction: 发送消息
- DoNothingAction: 选择不回复
- QueryActivityPatternAction: 查询用户消息活跃分布
- RecordHabitAction: 记录用户习惯观察
- QueryHabitsAction: 查询已记录的用户习惯
- NFCMemoAction / NFCMemoDeleteAction: 私人备忘录写入与删除
"""

from __future__ import annotations

from .do_nothing import DoNothingAction
from .memo import NFCMemoAction, NFCMemoDeleteAction
from .query_activity_pattern import QueryActivityPatternAction
from .query_habits import QueryHabitsAction
from .record_habit import RecordHabitAction
from .remove_habit import RemoveHabitAction
from .reply import NFCReplyAction
from .proactive_control import QueryProactiveStatusAction, SetProactiveEnabledAction
from .update_habit import UpdateHabitAction
from .update_world import UpdateWorldAction
from .manage_schedule import ManageScheduleAction

__all__ = [
    "DoNothingAction",
    "NFCMemoAction",
    "NFCMemoDeleteAction",
    "NFCReplyAction",
    "QueryActivityPatternAction",
    "QueryProactiveStatusAction",
    "RecordHabitAction",
    "QueryHabitsAction",
    "RemoveHabitAction",
    "SetProactiveEnabledAction",
    "UpdateHabitAction",
    "UpdateWorldAction",
    "ManageScheduleAction",
]
