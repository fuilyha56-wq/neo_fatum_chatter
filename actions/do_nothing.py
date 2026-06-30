"""DoNothing 动作。

当 LLM 决定不回复时调用此动作。
通过原生 Tool Calling 的参数传递内心活动等元数据。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

logger = get_logger("NFC_do_nothing")


class DoNothingAction(BaseAction):
    """不回复，不做任何操作。"""

    action_name: str = "do_nothing"
    action_description: str = (
        "决定不做任何回复。当对方的消息不需要回应、纯表情、或者你选择已读不回时使用。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        thought: Annotated[str, "你此刻的内心想法，描述你为什么选择不回复"] = "",
        expected_reaction: Annotated[str, "你预期的对方反应"] = "",
        max_wait_seconds: Annotated[
            float, "是否继续等待对方（秒），0表示不等待"
        ] = 0.0,
        mood: Annotated[str, "你当前的心情"] = "",
        **_extra,
    ) -> tuple[bool, str]:
        """执行不回复的逻辑。

        参数由 chatter.py 提取用于状态记录，
        action 本身不使用这些参数。

        ``**_extra`` 用于吞掉 LLM 偶尔幻觉出的未知参数，避免 TypeError。

        Returns:
            (True, "已选择不回复")
        """
        if _extra:
            logger.debug(f"忽略 do_nothing 未知参数: {sorted(_extra.keys())}")
        return True, "已选择不回复"
