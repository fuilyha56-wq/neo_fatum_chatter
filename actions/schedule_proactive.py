"""ScheduleProactive 动作。

允许 LLM 预约下一次主动思考的时间。
预约存在时，条件主动发起逻辑暂停，直到预约时间到达。
"""

from __future__ import annotations

import time
from typing import Annotated, ClassVar

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

logger = get_logger("NFC_schedule_proactive")

# 工具基础描述（固定，不可配置）
_BASE_DESCRIPTION = (
    "预约一个时间点或绝对时间窗口，届时系统会主动唤醒你去发起新一轮对话。"
    "**新的预约会覆盖旧的预约；传 delay_minutes=0 可取消当前预约。**\n"
    "**预约不受勿扰时段限制**，即使是深夜或清晨设定的预约也会如期触发。\n"
    "delay_minutes=0 取消预约；其他值范围限制为 30~1440 分钟（30 分钟~24 小时）。\n\n"
    "也可以通过 start_at 和 end_at 设置一个主动思考窗口，格式必须是 YYYY-MM-DD HH:mm。\n"
    "设置窗口时，系统会在该时间段内根据 context 和 interest 判断是否主动思考。\n\n"
    "**reason（必填）：**\n"
    "记录此刻的真实想法。可以是一件具体的事，也可以只是「那个时间想找 Ta 说说话」"
    "——怎么自然怎么写，重要的是让未来的你看到时能自然接上。取消预约时可留空。"
)


class ScheduleProactiveAction(BaseAction):
    """预约下一次主动思考时间。"""

    action_name: str = "schedule_proactive"
    action_description: str = _BASE_DESCRIPTION
    chatter_allow: list[str] = ["neo_fatum_chatter"]

    # 可配置的指导语（由插件在 on_plugin_loaded 时从 config 写入，初始为空）
    _guidance: ClassVar[str] = ""

    _MIN_DELAY_MIN = 30    # 最小 30 分钟
    _MAX_DELAY_MIN = 1440  # 最大 24 小时

    @classmethod
    def to_schema(cls) -> dict:  # type: ignore[override]
        """动态拼接 base 描述 + 可配置指导语生成 schema。"""
        schema = super().to_schema()
        guidance = cls._guidance
        if guidance:
            func = schema.get("function", {})
            func["description"] = _BASE_DESCRIPTION + "\n\n" + guidance
        return schema

    async def execute(
        self,
        delay_minutes: Annotated[
            int,
            "多少分钟后发起主动思考。传 0 表示取消当前预约；其他值范围 30~1440（30 分钟~24 小时）。",
        ] = 30,
        reason: Annotated[
            str,
            "此刻的真实想法：可以是一件具体的事，也可以只是「那个时间想找 Ta 说说话」。"
            "取消预约时（delay_minutes=0）可留空。",
        ] = "",
        start_at: Annotated[
            str,
            "主动思考窗口开始时间，格式 YYYY-MM-DD HH:mm；与 end_at 同时提供时优先使用窗口预约。",
        ] = "",
        end_at: Annotated[
            str,
            "主动思考窗口结束时间，格式 YYYY-MM-DD HH:mm；必须晚于 start_at。",
        ] = "",
        context: Annotated[
            str,
            "窗口预约的上下文，让未来的你知道这个时间段想关注什么。",
        ] = "",
        interest: Annotated[
            float,
            "窗口预约的兴趣强度，范围 0~1。",
        ] = 1.0,
        **_extra,
    ) -> tuple[bool, str]:
        """设置或取消主动思考预约。

        Args:
            delay_minutes: 延迟分钟数，0 表示取消当前预约，其他值会被夹到 30~1440 范围。
            reason: 预约原因，取消时可留空。
            start_at: 主动思考窗口开始时间，格式 YYYY-MM-DD HH:mm。
            end_at: 主动思考窗口结束时间，格式 YYYY-MM-DD HH:mm。
            context: 窗口预约上下文。
            interest: 窗口预约兴趣强度。

        ``**_extra`` 用于吞掉 LLM 偶尔幻觉出的未知参数，避免 TypeError。

        Returns:
            (True, 状态描述)
        """
        if _extra:
            logger.debug(f"忽略 schedule_proactive 未知参数: {sorted(_extra.keys())}")
        if start_at and end_at:
            interest = max(0.0, min(1.0, interest))
            logger.debug(
                f"预约主动思考窗口: {start_at} 到 {end_at}"
                + (f"（{context}，兴趣 {interest:.2f}）" if context else "")
            )
            return True, f"已预约在 {start_at} 到 {end_at} 之间主动思考"
        if delay_minutes == 0:
            logger.debug("取消主动思考预约")
            return True, "已取消当前主动思考预约"

        delay_minutes = max(self._MIN_DELAY_MIN, min(self._MAX_DELAY_MIN, delay_minutes))
        delay_seconds = delay_minutes * 60
        at = time.time() + delay_seconds
        from datetime import datetime

        dt_str = datetime.fromtimestamp(at).strftime("%H:%M:%S")
        if reason:
            logger.debug(f"预约主动思考: {dt_str}（{reason}）")
        else:
            logger.debug(f"预约主动思考: {dt_str}")
        return True, f"已预约在 {delay_minutes} 分钟后主动思考"
