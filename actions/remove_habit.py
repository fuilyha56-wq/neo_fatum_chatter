"""删除错误或过期用户习惯的 Action。"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

logger = get_logger("NFC_remove_habit")


class RemoveHabitAction(BaseAction):
    """按查询结果中的 ID 删除一条用户习惯观察。"""

    action_name = "nfc_remove_habit"
    action_description = (
        "删除已被证伪、过期或不应继续保留的用户习惯。必须先通过 "
        "nfc_query_habits 获取 habit_id。"
    )
    display_name = "删除用户习惯"
    chatter_allow = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        habit_id: Annotated[str, "nfc_query_habits 返回的习惯 ID。"] = "",
        **extra: object,
    ) -> tuple[bool, str]:
        """删除指定习惯并持久化。"""
        if extra:
            logger.debug(f"忽略 remove_habit 未知参数: {sorted(extra)}")
        if not habit_id.strip():
            return False, "habit_id 不能为空，请先查询已记录的习惯"

        stream_id = self.chat_stream.stream_id
        session_store = self.plugin.session_store
        async with session_store.lock(stream_id):
            session = await session_store.get_or_create(stream_id)
            removed = session.remove_habit(habit_id)
            if not removed:
                return False, f"未找到习惯 ID：{habit_id.strip()}"
            await session_store.save(session)
        return True, f"已删除习惯：{habit_id.strip()}"


__all__ = ["RemoveHabitAction"]