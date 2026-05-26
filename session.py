"""NFC 会话状态：兼容入口。

实际定义在：
    - ``neo_fatum_chatter.domain.session_state.NFCSession``（领域模型）
    - ``neo_fatum_chatter.persistence.session_store.NFCSessionStore``（持久化）

外部代码可继续从 ``neo_fatum_chatter.session`` 导入两者；新代码请直接
从对应子包引用，让分层意图更清晰。
"""

from __future__ import annotations

from .domain.session_state import NFCSession
from .persistence.session_store import NFCSessionStore

__all__ = ["NFCSession", "NFCSessionStore"]
