"""查询用户消息活跃分布动作。

从数据库查询当前聊天流中用户的历史消息，按小时统计分布，
返回格式化的活跃时段报告。支持工作日/周末分列统计。
"""

from __future__ import annotations

import time
from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction
from src.kernel.db import QueryBuilder

logger = get_logger("NFC_query_activity_pattern")

_MAX_DAYS = 365


def _format_hourly_bar(count: int, max_count: int, bar_width: int = 10) -> str:
    """生成简易柱状条。"""
    if max_count <= 0:
        return ""
    filled = int(count / max_count * bar_width)
    return "█" * filled + "░" * (bar_width - filled)


class QueryActivityPatternAction(BaseAction):
    """查询用户消息活跃分布。"""

    name = "nfc_query_activity_pattern"
    description = (
        "查询对方的消息活跃时段分布。从数据库统计对方在最近一段时间内"
        "每小时的消息数量，了解对方的作息和出没时间。"
        "数据越久越稳定，建议至少回溯 30 天。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        days: Annotated[
            int,
            "回溯天数，查询最近多少天内的消息分布。默认30天，最大365天。",
        ] = 30,
        include_weekday_breakdown: Annotated[
            bool,
            "是否按工作日/周末分别统计。开启后返回三组分布：整体、工作日、周末。",
        ] = False,
        **_extra,
    ) -> tuple[bool, str]:
        """查询用户消息活跃分布。"""
        if _extra:
            logger.debug(f"忽略 query_activity_pattern 未知参数: {sorted(_extra.keys())}")

        days = max(1, min(days, _MAX_DAYS))
        stream_id = self.chat_stream.stream_id

        now = time.time()
        start_time = now - days * 86400

        # 查询消息
        from src.core.models.sql_alchemy import Messages

        hourly = [0] * 24
        weekday_hourly = [0] * 24
        weekend_hourly = [0] * 24
        total = 0

        try:
            async for row in QueryBuilder(Messages).filter(
                stream_id=stream_id,
                time__gte=start_time,
                time__lt=now,
            ).iter_all(batch_size=1000, as_dict=True):
                total += 1
                ts = row.get("time", 0)
                if not isinstance(ts, (int, float)) or ts <= 0:
                    continue
                lt = time.localtime(float(ts))
                hour = lt.tm_hour
                hourly[hour] += 1
                if include_weekday_breakdown:
                    if lt.tm_wday < 5:
                        weekday_hourly[hour] += 1
                    else:
                        weekend_hourly[hour] += 1
        except Exception as e:
            logger.warning(f"查询消息分布失败: {e}")
            return True, f"查询消息分布时出错: {e}"

        if total == 0:
            return True, f"最近 {days} 天内无消息记录，无法分析活跃分布。"

        # 格式化输出
        max_count = max(hourly) if hourly else 1
        lines = [f"对方最近 {days} 天消息活跃分布（共 {total} 条消息）："]

        for hour in range(24):
            bar = _format_hourly_bar(hourly[hour], max_count)
            lines.append(f"{hour:02d}: {bar} ({hourly[hour]})")

        # 峰值时段
        peak_hours = sorted(range(24), key=lambda h: hourly[h], reverse=True)[:5]
        peak_str = ", ".join(f"{h}时({hourly[h]}条)" for h in peak_hours if hourly[h] > 0)
        if peak_str:
            lines.append(f"峰值时段: {peak_str}")

        # 低谷时段
        low_hours = [h for h in range(24) if hourly[h] == 0]
        if low_hours:
            low_str = ", ".join(f"{h}时" for h in low_hours)
            lines.append(f"无消息时段: {low_str}")

        # 工作日/周末分列
        if include_weekday_breakdown:
            lines.append("")
            lines.append("── 工作日分布 ──")
            wd_max = max(weekday_hourly) if weekday_hourly else 1
            for hour in range(24):
                bar = _format_hourly_bar(weekday_hourly[hour], wd_max)
                lines.append(f"{hour:02d}: {bar} ({weekday_hourly[hour]})")

            lines.append("")
            lines.append("── 周末分布 ──")
            we_max = max(weekend_hourly) if weekend_hourly else 1
            for hour in range(24):
                bar = _format_hourly_bar(weekend_hourly[hour], we_max)
                lines.append(f"{hour:02d}: {bar} ({weekend_hourly[hour]})")

        return True, "\n".join(lines)
