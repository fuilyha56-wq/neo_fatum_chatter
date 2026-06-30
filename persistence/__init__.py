"""NFC 持久化层。

负责把 ``domain`` 状态写到磁盘并管理并发锁/索引。
不应包含状态变更行为（那是 ``domain`` 的事），也不应包含 LLM 协议（那是 ``protocol`` 的事）。
"""

from __future__ import annotations

from .session_store import NFCSessionStore

__all__ = ["NFCSessionStore"]
