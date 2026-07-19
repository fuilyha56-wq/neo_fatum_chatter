"""记录用户习惯观察动作。

LLM 主动记录对用户作息、行为模式的观察，持久化到 session 中。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

logger = get_logger("NFC_record_habit")


class RecordHabitAction(BaseAction):
    """记录一条用户习惯观察。"""

    name = "nfc_record_habit"
    description = (
        "记录你对对方习惯的观察，比如作息时间、出没规律、行为偏好等。"
        "这些记录会持久保存，以后可以通过 nfc_query_habits 查询。"
        "比如：「通常23点睡觉」「工作日早上9点上班」「周末下午喜欢打游戏」。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        habit_text: Annotated[
            str,
            "对对方习惯的描述，如「通常23点睡觉」「工作日早上9点上班」「周末下午喜欢打游戏」",
        ] = "",
        category: Annotated[
            str,
            "习惯分类标签，如 sleep/work/social/hobby/routine。留空则不分类。",
        ] = "",
        **_extra,
    ) -> tuple[bool, str]:
        """记录用户习惯观察。"""
        if _extra:
            logger.debug(f"忽略 record_habit 未知参数: {sorted(_extra.keys())}")

        if not habit_text or not habit_text.strip():
            return False, "习惯描述不能为空"

        # 通过 plugin 公共属性访问 session store
        session_store = self.plugin.session_store
        stream_id = self.chat_stream.stream_id

        async with session_store.lock(stream_id):
            session = await session_store.get_or_create(stream_id)
            session.add_habit(habit_text, category)
            await session_store.save(session)
            count = len(session.user_habits)

        cat_info = f" [{category.strip()}]" if category.strip() else ""
        logger.debug(f"记录习惯: {habit_text[:50]}{cat_info} (共 {count} 条)")
        return True, f"已记录习惯{cat_info}，当前共 {count} 条观察"
