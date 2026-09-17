"""更新现实世界状态动作。

角色自述"我现在在做什么/在哪"，写入日程化世界状态（self_state_commit，
事实级条目，优先于作息推断渲染）。设计对应 private_companion 的
self_state_commit 证据类型。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from ..services.world_state_service import WorldStateService

logger = get_logger("NFC_update_world")


class UpdateWorldAction(BaseAction):
    """自述当前活动/位置，更新现实世界状态。"""

    name = "nfc_update_world"
    description = (
        "更新你自己的现实生活状态：告诉系统你此刻在做什么、在哪、大概到几点。"
        "比如开始打游戏了、出门吃饭了、准备睡了。"
        "这会成为'当前活动'的事实来源（优先于作息推断），"
        "让你聊天时带着真实的生活上下文。不带 activity 调用则清除当前自述"
        "（表示之前记录的活动已结束，回到按作息推断）。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        activity: Annotated[
            str,
            "你此刻正在做的事，如「在打排位」「出门买奶茶」「躺床上刷视频」。留空表示清除当前自述。",
        ] = "",
        end_time: Annotated[
            str,
            "预计结束时间（HH:MM 或「18点半」），可选。到期后自动回到作息推断。",
        ] = "",
        location: Annotated[
            str,
            "当前位置，如「家里」「图书馆」。可选，会覆盖已记录的位置。",
        ] = "",
        **_extra,
    ) -> tuple[bool, str]:
        """写入或清除当前活动自述。"""
        if _extra:
            logger.debug(f"忽略 update_world 未知参数: {sorted(_extra.keys())}")

        stream_id = self.chat_stream.stream_id
        result = await WorldStateService(self.plugin.session_store).update_self_state(
            stream_id,
            activity=activity or "",
            end_time=end_time or "",
            location=location or "",
        )
        if not result.ok:
            return False, result.message
        if result.item is None:
            return True, f"{result.message}，之后按作息模板推断你的状态。"

        item = result.item
        end_text = f"，预计 {end_time.strip()} 结束" if (end_time or "").strip() else ""
        location_text = f"，位置：{location.strip()}" if (location or "").strip() else ""
        logger.debug(f"[世界状态] 自述活动: {item.activity[:50]}{end_text}{location_text}")
        return True, f"已记录你正在：{item.activity}{end_text}{location_text}（self_state_commit）"
