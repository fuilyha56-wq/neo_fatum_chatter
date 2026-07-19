"""ScheduleProactive 动作。

允许 LLM 预约下一次主动思考的时间。
预约存在时，条件主动发起逻辑暂停，直到预约时间到达。

注意：本 action 是工具调用薄壳，仅负责向 LLM 返回执行结果字符串。
真正的预约写入由 ``runtime/orchestrator`` 通过 ``ProactiveService.apply_schedule``
统一应用，避免 action 与 orchestrator 双轨制造成状态不一致。
"""

from __future__ import annotations

from typing import Annotated, ClassVar

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

logger = get_logger("NFC_schedule_proactive")

# 工具基础描述（固定，不可配置）
_BASE_DESCRIPTION = (
    "预约一个时间点，届时系统会主动唤醒你去发起新一轮对话。"
    "**新的预约会覆盖旧的预约；传 delay_minutes=0 可取消当前预约。**\n"
    "**预约不受勿扰时段限制**，即使是深夜或清晨设定的预约也会如期触发。\n"
    "delay_minutes=0 取消预约；其他值范围限制为 30~1440 分钟（30 分钟~24 小时）。\n\n"
    "**reason（必填）：**\n"
    "记录此刻的真实想法。可以是一件具体的事，也可以只是「那个时间想找 Ta 说说话」"
    "——怎么自然怎么写，重要的是让未来的你看到时能自然接上。取消预约时可留空。"
)


class ScheduleProactiveAction(BaseAction):
    """预约下一次主动思考时间。"""

    name: str = "schedule_proactive"
    description: str = _BASE_DESCRIPTION
    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

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
        **_extra,
    ) -> tuple[bool, str]:
        """设置或取消主动思考预约。

        Args:
            delay_minutes: 延迟分钟数，0 表示取消当前预约，其他值会被夹到 30~1440 范围。
            reason: 预约原因，取消时可留空。

        ``**_extra`` 用于吞掉 LLM 偶尔幻觉出的未知参数，避免 TypeError。

        Returns:
            (True, 状态描述)

        注意：本方法不写入 session，仅返回状态字符串供 LLM 感知。
        预约的实际写入由 ``ProactiveService.apply_schedule`` 在决策提交阶段统一完成。
        """
        if _extra:
            logger.debug(f"忽略 schedule_proactive 未知参数: {sorted(_extra.keys())}")
        if delay_minutes == 0:
            logger.debug("取消主动思考预约")
            return True, "已取消当前主动思考预约"

        delay_minutes = max(self._MIN_DELAY_MIN, min(self._MAX_DELAY_MIN, delay_minutes))
        logger.debug(
            f"预约主动思考: {delay_minutes} 分钟后"
            + (f"（{reason}）" if reason else "")
        )
        return True, f"已预约在 {delay_minutes} 分钟后主动思考"
