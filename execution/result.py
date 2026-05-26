"""NFC 执行结果数据模型。

把"一轮决策执行完成后，外层应该如何推进状态"显式化为一个 dataclass。
不做 patch 总线/StateCommitter 这种正式协议，仅作为 reply / 第三方工具
执行返回值的轻量载体——TurnController 仍然直接读它的字段并写 session。
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

    @property
    def has_visible_output(self) -> bool:
        """是否有任何段落真的发到了对方。"""
        return bool(self.sent_segments)
