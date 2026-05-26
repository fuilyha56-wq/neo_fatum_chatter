"""NFC 执行层。

职责：
    - 执行已经形成的决策（reply / silence / 第三方工具）。
    - 输出 ``ExecutionResult``，把"动作之后要怎么改状态"以建议形式返回。
    - 不直接修改 session；状态写入由调用方在 turn_controller 中完成。

不在本层：
    - 决策生成（见 ``protocol/decision_parser.py``）。
    - LLM 协议归一化（见 ``protocol/response_normalizer.py``）。
    - 持久化（见 ``persistence/`` 与 ``domain/``）。
"""

from __future__ import annotations

from .reply_executor import (
    METADATA_LEAK_THRESHOLD,
    coerce_content_segments,
    sanitize_segment,
    send_reply_segments,
)
from .result import ExecutionResult

__all__ = [
    "ExecutionResult",
    "METADATA_LEAK_THRESHOLD",
    "coerce_content_segments",
    "sanitize_segment",
    "send_reply_segments",
]
