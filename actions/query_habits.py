"""查询已记录的用户习惯动作。

从 session 中读取 LLM 之前记录的习惯观察，格式化返回。
"""

from __future__ import annotations

import time
from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

logger = get_logger("NFC_query_habits")


def _format_time_ago(ts: float, now: float) -> str:
    """将时间戳格式化为相对时间描述。"""
    delta = max(0.0, now - ts)
    if delta < 3600:
        return "刚刚"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    return f"{int(delta // 86400)}天前"


class QueryHabitsAction(BaseAction):
    """查询已记录的用户习惯观察。"""

    name = "nfc_query_habits"
    description = (
        "查询之前记录的关于对方习惯的观察。可按分类过滤。"
        "如果没有记录过，会提示无数据。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        category: Annotated[
            str,
            "按分类过滤，如 sleep/work/social/hobby/routine。留空返回全部。",
        ] = "",
        **_extra,
    ) -> tuple[bool, str]:
        """查询已记录的用户习惯。"""
        if _extra:
            logger.debug(f"忽略 query_habits 未知参数: {sorted(_extra.keys())}")

        # 通过 plugin 公共属性访问 session store
        session_store = self.plugin.session_store
        stream_id = self.chat_stream.stream_id

        # 只读访问，用 peek 避免 get_or_create 带来的无谓写入
        session = await session_store.peek(stream_id)
        if session is None:
            if category.strip():
                return True, f"未记录过分类为 [{category.strip()}] 的习惯观察"
            return True, "尚未记录任何习惯观察，可以使用 nfc_record_habit 工具来记录"
        habits = session.get_habits(category)

        if not habits:
            if category.strip():
                return True, f"未记录过分类为 [{category.strip()}] 的习惯观察"
            return True, "尚未记录任何习惯观察，可以使用 nfc_record_habit 工具来记录"

        now = time.time()
        lines = [f"已记录 {len(habits)} 条习惯观察："]
        for habit in habits:
            cat = habit.get("category", "")
            cat_prefix = f"[{cat}] " if cat else ""
            text = habit.get("habit_text", "")
            ts = habit.get("recorded_at", 0)
            time_ago = _format_time_ago(ts, now) if isinstance(ts, (int, float)) and ts > 0 else "未知时间"
            lines.append(f"{cat_prefix}{text}（记录于 {time_ago}）")

        return True, "\n".join(lines)
