"""NFC 主动思考服务。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from ..domain.decision import ProactiveSchedule
    from ..session import NFCSession


logger = get_logger("NFC_proactive_service")


class ProactiveService:
    """处理主动思考预约的 session 副作用。"""

    @staticmethod
    def _parse_absolute_time(value: str) -> float:
        """解析绝对预约时间，格式为 YYYY-MM-DD HH:MM。"""
        return datetime.strptime(value, "%Y-%m-%d %H:%M").timestamp()

    @staticmethod
    def apply_schedule(
        session: NFCSession,
        proactive_schedule: ProactiveSchedule,
        now_ts: float | None = None,
    ) -> None:
        """根据决策结果更新主动思考预约。"""
        current_ts = time.time() if now_ts is None else now_ts
        if proactive_schedule.start_at and proactive_schedule.end_at:
            start_ts = ProactiveService._parse_absolute_time(proactive_schedule.start_at)
            end_ts = ProactiveService._parse_absolute_time(proactive_schedule.end_at)
            if end_ts <= start_ts:
                raise ValueError("预约窗口结束时间必须晚于开始时间")
            if end_ts <= current_ts:
                raise ValueError("预约窗口结束时间必须晚于当前时间")
            start_ts = max(start_ts, current_ts + 30 * 60)
            if end_ts <= start_ts:
                raise ValueError("预约窗口剩余时间不足 30 分钟")
            context = proactive_schedule.context or proactive_schedule.reason
            session.set_scheduled_proactive_window(
                start_ts,
                end_ts,
                context=context,
                interest=proactive_schedule.interest,
                check_interval_seconds=600.0,
            )
            logger.info(
                "[NFC] 已预约主动思考窗口: "
                f"{proactive_schedule.start_at} 到 {proactive_schedule.end_at}"
                + (f"，上下文：{context}" if context else "")
            )
            return

        delay_minutes = proactive_schedule.delay_minutes
        if delay_minutes == 0:
            session.clear_scheduled_proactive()
            logger.info("[NFC] 已取消主动思考预约")
            return

        if delay_minutes is None:
            delay_minutes = 30.0
        delay_minutes = max(30.0, min(1440.0, float(delay_minutes)))
        delay_seconds = delay_minutes * 60
        reason = proactive_schedule.reason
        session.set_scheduled_proactive(
            current_ts + delay_seconds,
            reason=reason,
        )
        logger.info(
            f"[NFC] 已预约主动思考: {delay_minutes:.0f} 分钟后"
            + (f"，理由：{reason}" if reason else "")
        )

    @staticmethod
    def decay_window_interest(session: NFCSession, decay: float, floor: float) -> None:
        """在预约窗口存在时衰减兴趣值。"""
        if (
            session.scheduled_proactive_start_at is None
            or session.scheduled_proactive_end_at is None
        ):
            return
        session.scheduled_proactive_interest = max(
            floor,
            session.scheduled_proactive_interest * decay,
        )

    @staticmethod
    def handle_window_actor_result(
        session: NFCSession,
        replied: bool,
        decay: float,
        floor: float,
    ) -> None:
        """处理预约窗口唤醒后的主 actor 决策结果。"""
        if session.scheduled_proactive_start_at is None:
            return
        if replied:
            session.clear_scheduled_proactive()
            return
        ProactiveService.decay_window_interest(session, decay=decay, floor=floor)
