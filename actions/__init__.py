"""NFC 动作组件模块。

提供核心动作：
- NFCReplyAction: 发送消息
- DoNothingAction: 选择不回复
"""

from __future__ import annotations

from .do_nothing import DoNothingAction
from .pass_and_wait import NFCPassAndWaitAction
from .reply import NFCReplyAction
from .send_text import NFCSendTextAction
from .stop_conversation import NFCStopConversationAction

__all__ = [
    "DoNothingAction",
    "NFCPassAndWaitAction",
    "NFCReplyAction",
    "NFCSendTextAction",
    "NFCStopConversationAction",
]
