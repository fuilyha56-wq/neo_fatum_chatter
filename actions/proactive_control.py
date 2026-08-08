"""NFC 会话级主动联系控制 Action。"""

from __future__ import annotations

import time
from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

logger = get_logger("NFC_proactive_control")


class SetProactiveEnabledAction(BaseAction):
    """启用或暂停当前私聊的主动联系。"""

    action_name = "nfc_set_proactive_enabled"
    action_description = (
        "启用或暂停当前私聊的主动联系。暂停后不会因预约或沉默自动联系，"
        "恢复后会继续使用原有预约和频率限制。"
    )
    display_name = "设置主动联系"
    chatter_allow = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        enabled: Annotated[bool, "是否允许当前私聊被主动联系。"] = True,
        reason: Annotated[str, "暂停原因；恢复时可留空。"] = "",
        **extra: object,
    ) -> tuple[bool, str]:
        """保存当前会话的主动联系开关。"""
        if extra:
            logger.debug(f"忽略 set_proactive_enabled 未知参数: {sorted(extra)}")
        stream_id = self.chat_stream.stream_id
        session_store = self.plugin.session_store
        async with session_store.lock(stream_id):
            session = await session_store.get_or_create(stream_id)
            session.set_proactive_enabled(enabled, reason)
            await session_store.save(session)
        if enabled:
            return True, "已恢复当前私聊的主动联系"
        return True, f"已暂停当前私聊的主动联系：{reason.strip() or '未说明原因'}"


class QueryProactiveStatusAction(BaseAction):
    """查询当前私聊的主动联系状态和冷却原因。"""

    action_name = "nfc_query_proactive_status"
    action_description = "查询当前私聊是否允许主动联系，以及预约、冷却或暂停原因。"
    display_name = "查询主动联系状态"
    chatter_allow = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(self, **extra: object) -> tuple[bool, str]:
        """返回当前会话的主动联系状态。"""
        if extra:
            logger.debug(f"忽略 query_proactive_status 未知参数: {sorted(extra)}")
        stream_id = self.chat_stream.stream_id
        session = await self.plugin.session_store.peek(stream_id)
        if session is None:
            return True, "当前私聊尚无主动联系状态记录"

        config = self.plugin.config.proactive
        if not config.enabled:
            return True, "全局主动联系功能当前未启用"
        if not session.proactive_enabled:
            return True, (
                "当前私聊已暂停主动联系："
                f"{session.proactive_paused_reason or '未说明原因'}"
            )

        now = time.time()
        if session.scheduled_proactive_at is not None:
            schedule_text = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime(session.scheduled_proactive_at),
            )
            return True, (
                f"当前私聊已预约在 {schedule_text} 主动联系："
                f"{session.scheduled_proactive_reason or '未说明理由'}"
            )

        if session.last_proactive_at is not None:
            remaining = max(
                0.0,
                float(config.min_interval) - (now - session.last_proactive_at),
            )
            if remaining > 0:
                return True, f"当前处于主动联系冷却期，约 {remaining / 60:.0f} 分钟后可再次触发"

        return True, "当前允许主动联系，尚无预约或冷却限制"


__all__ = ["QueryProactiveStatusAction", "SetProactiveEnabledAction"]