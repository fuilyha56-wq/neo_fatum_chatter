"""纠正已记录用户习惯的 Action。"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

logger = get_logger("NFC_update_habit")


class UpdateHabitAction(BaseAction):
    """按查询结果中的 ID 纠正一条用户习惯观察。"""

    action_name = "nfc_update_habit"
    action_description = (
        "纠正先前记录但已过时或有误的用户习惯。必须先通过 "
        "nfc_query_habits 获取 habit_id；至少提供新的 habit_text 或 category。"
    )
    display_name = "纠正用户习惯"
    chatter_allow = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        habit_id: Annotated[str, "nfc_query_habits 返回的习惯 ID。"] = "",
        habit_text: Annotated[str, "新的习惯描述；不修改描述时留空。"] = "",
        category: Annotated[str, "新的分类；不修改分类时留空。"] = "",
        **extra: object,
    ) -> tuple[bool, str]:
        """更新一条已有习惯并持久化。"""
        if extra:
            logger.debug(f"忽略 update_habit 未知参数: {sorted(extra)}")
        if not habit_id.strip():
            return False, "habit_id 不能为空，请先查询已记录的习惯"
        if not habit_text.strip() and not category.strip():
            return False, "请至少提供新的习惯描述或分类"

        stream_id = self.chat_stream.stream_id
        session_store = self.plugin.session_store
        async with session_store.lock(stream_id):
            session = await session_store.get_or_create(stream_id)
            updated = session.update_habit(
                habit_id,
                habit_text=habit_text,
                category=category,
            )
            if not updated:
                return False, f"未找到习惯 ID：{habit_id.strip()}"
            await session_store.save(session)
        return True, f"已纠正习惯：{habit_id.strip()}"


__all__ = ["UpdateHabitAction"]