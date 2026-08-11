"""NFC 执行结果数据模型。

把"一轮决策执行完成后，外层应该如何推进状态"显式化为一个 dataclass。
不做 patch 总线/StateCommitter 这种正式协议，仅作为 reply / 第三方工具
执行返回值的轻量载体——TurnController 仍然直接读它的字段并写 session。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


_TOOL_RESULT_KIND = "nfc_reply_execution"


@dataclass(slots=True)
class ExecutionResult:
    """一次执行的结果摘要。

    Attributes:
        sent_segments: 实际发送给对方的消息段落（已经做过元数据清洗）。
        attempted_segments: 模型期望发送的段落数（用于审计与对比 sent）。
        failed: 是否因发送链路异常导致整体失败。
        reason: 失败/特殊情况说明，仅用于日志和 mental_log 注解。
        stripped_metadata: 是否触发了"最后防线"的元数据泄漏剥离。
        stripped_thinking: 是否触发了 thinking 块剥离。
    """

    sent_segments: list[str] = field(default_factory=list)
    attempted_segments: int = 0
    failed: bool = False
    reason: str = ""
    stripped_metadata: bool = False
    stripped_thinking: bool = False
    failure_kind: str = ""

    @property
    def has_visible_output(self) -> bool:
        """是否有任何段落真的发到了对方。"""
        return bool(self.sent_segments)

    def to_tool_result(self) -> str:
        """编码成可写入 ToolResult 的稳定 JSON 文本。"""
        return json.dumps(
            {
                "kind": _TOOL_RESULT_KIND,
                "sent_segments": list(self.sent_segments),
                "attempted_segments": self.attempted_segments,
                "failed": self.failed,
                "failure_kind": self.failure_kind,
                "reason": self.reason,
                "stripped_metadata": self.stripped_metadata,
                "stripped_thinking": self.stripped_thinking,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_tool_result(cls, value: Any) -> ExecutionResult | None:
        """从 ToolResult 文本恢复回复执行摘要，非 NFC 结果返回 None。"""
        if not isinstance(value, str):
            return None

        raw = value.strip()
        failure_prefix = "执行失败:"
        if raw.startswith(failure_prefix):
            raw = raw[len(failure_prefix):].strip()

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("kind") != _TOOL_RESULT_KIND:
            return None

        raw_segments = data.get("sent_segments", [])
        sent_segments = (
            [str(segment) for segment in raw_segments]
            if isinstance(raw_segments, list)
            else []
        )
        return cls(
            sent_segments=sent_segments,
            attempted_segments=int(data.get("attempted_segments", 0) or 0),
            failed=bool(data.get("failed", False)),
            failure_kind=str(data.get("failure_kind", "") or ""),
            reason=str(data.get("reason", "") or ""),
            stripped_metadata=bool(data.get("stripped_metadata", False)),
            stripped_thinking=bool(data.get("stripped_thinking", False)),
        )
