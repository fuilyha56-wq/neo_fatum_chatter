"""NFC 群聊场景：pass_and_wait 动作。

本动作不立刻执行任何对外行为，仅向 orchestrator 发出"本轮结束后等待"的信号。
真正的等待状态切换由 group_orchestrator 检测 ToolCallOutcome.should_wait 后实现。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

logger = get_logger("NFC_pass_and_wait")


class NFCPassAndWaitAction(BaseAction):
    """登记一个等待点，可单独使用，也可与 send_text 同轮组合。"""

    action_name: str = "nfc_pass_and_wait"
    action_description: str = (
        "为当前对话登记一个等待点。你可以单独调用它，让本轮什么都不做直接等待；"
        "也可以在同一轮先调用其他 action（如 send_text），再调用本工具，"
        "表示这些动作执行完成后进入等待。默认会等待用户新消息；如果传入 seconds 参数，"
        "则会在指定秒数到达后由框架主动恢复对话流程，即使期间没有收到新消息。"
        "适合需要回复后稍后主动继续、定时追问或延时确认的场景。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]

    async def execute(
        self,
        seconds: Annotated[
            float | None,
            "等待秒数；为 None 时等待新消息，为数字时到时主动继续",
        ] = None,
    ) -> tuple[bool, str]:
        """登记等待意图。orchestrator 会在 tool_flow 中识别该 action。

        Args:
            seconds: 等待秒数，None 表示等待新消息

        Returns:
            (True, 描述文本)
        """
        if seconds is None:
            return True, "已登记等待，将在本轮动作完成后等待新消息"
        return True, f"已登记等待，将在本轮动作完成后等待 {seconds} 秒再继续对话"
