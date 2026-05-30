"""主动发起模块。

ProactiveThinker 负责在长时间沉默后评估是否主动发起对话。
通过 Scheduler 定期调度。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from ..config import NFCConfig
    from ..session import NFCSession, NFCSessionStore

logger = get_logger("NFC_proactive")


class ProactiveThinker:
    """主动发起思考器。

    检查所有活跃 Session 的沉默时长，
    在满足条件时通过事件总线触发主动对话。
    """

    def __init__(
        self,
        config: NFCConfig,
        session_store: NFCSessionStore,
    ) -> None:
        self._config = config
        self._session_store = session_store

    async def check_all_sessions(self) -> list[str]:
        """检查所有缓存中的 Session，返回需要主动发起的 stream_id 列表。"""
        proactive_config = self._config.proactive
        if not proactive_config.enabled:
            return []

        # 注意：勿扰时段只对沉默触发生效，模型预约不受限制
        # 勿扰时段检查已移入 _should_trigger

        triggered: list[str] = []

        # 检查内存中的 session（完整逻辑：预约 + 沉默触发）
        cached_sessions = self._session_store.get_all_cached()
        for stream_id, session in cached_sessions.items():
            if await self._check_and_trigger(stream_id, session):
                triggered.append(stream_id)

        # 检查磁盘上未在内存中的 session（仅检查预约，避免大量沉默触发）
        all_stream_ids = await self._session_store.list_all_stream_ids()
        for stream_id in all_stream_ids:
            if stream_id in cached_sessions:
                continue
            session = await self._session_store.peek(stream_id)  # 不缓存，避免污染内存
            if session is None:
                continue
            # 窗口预约
            if session.scheduled_proactive_start_at is not None:
                if await self._check_window(stream_id, session, time.time()):
                    triggered.append(stream_id)
                continue
            # 单点预约
            if session.scheduled_proactive_at is not None:
                now = time.time()
                if now >= session.scheduled_proactive_at:
                    logger.info(f"主动思考（磁盘 session）：触发预约 stream={stream_id[:8]}")
                    triggered.append(stream_id)

        return triggered

    async def _check_and_trigger(self, stream_id: str, session: NFCSession) -> bool:
        """检查单个 session 是否应触发，处理过期预约的持久化清除。"""
        now = time.time()

        # 窗口预约优先
        if session.scheduled_proactive_start_at is not None:
            return await self._check_window(stream_id, session, now)

        if session.scheduled_proactive_at is not None:
            if now >= session.scheduled_proactive_at:
                logger.info(f"主动思考：触发模型预约 stream={stream_id[:8]}")
                return True
            return False

        return self._should_trigger(session)

    async def _check_window(self, stream_id: str, session: NFCSession, now: float) -> bool:
        """检查窗口预约是否应触发 sub-actor。"""
        start = session.scheduled_proactive_start_at
        end = session.scheduled_proactive_end_at
        if start is None or end is None:
            return False

        # 窗口尚未开始
        if now < start:
            return False

        # 窗口已过期
        if now > end:
            session.clear_scheduled_proactive()
            logger.info(f"主动思考窗口已过期，清除: stream={stream_id[:8]}")
            return False

        # 达到最大尝试次数
        max_attempts = getattr(self._config.proactive, "window_max_attempts", 3)
        if session.scheduled_proactive_check_count >= max_attempts:
            return False

        # 检查间隔节流
        last_check = session.scheduled_proactive_last_check_at
        if last_check is not None and (now - last_check) < session.scheduled_proactive_check_interval:
            return False

        # 兴趣值概率门控
        interest = max(0.0, min(1.0, session.scheduled_proactive_interest))
        if random.random() > interest:
            return False

        # 更新检查状态
        session.scheduled_proactive_last_check_at = now
        session.scheduled_proactive_check_count += 1

        # 询问 sub-actor
        should_wake = await self._ask_sub_actor(stream_id, session, now)
        if should_wake:
            logger.info(
                f"主动思考窗口触发: stream={stream_id[:8]}, "
                f"check_count={session.scheduled_proactive_check_count}"
            )
        return should_wake

    async def _ask_sub_actor(self, stream_id: str, session: NFCSession, now: float) -> bool:
        """询问 sub-actor 是否应唤醒主 actor。

        最小实现：有上下文则唤醒。后续 Task 4 接入真实模型。
        """
        await asyncio.sleep(0)
        return bool(session.scheduled_proactive_context)

    def _should_trigger(self, session: NFCSession) -> bool:
        """判断无预约情况下是否应主动发起（沉默条件 + 衰减概率）。

        概率随沉默时长递增：超过阈值后，每多沉默一个阈值时长，
        概率在基础值上线性递增，最高不超过 1.0。

        勿扰时段只在本路径（沉默触发）中检查，模型预约不受此限制。

        Args:
            session: NFC 会话对象
        """
        # 勿扰时段：仅拦截沉默触发，不影响模型预约
        if self._is_quiet_hours():
            return False

        # 用户活跃时段检查：非典型活跃时段时降低概率（乘以 0.3），而非完全阻止
        _activity_penalty = 1.0
        if not session.is_user_typically_active_now():
            _activity_penalty = 0.3
            logger.debug(
                f"当前时段非用户典型活跃时段，降低主动发起概率: stream={session.stream_id[:8]}"
            )

        proactive_config = self._config.proactive
        now = time.time()

        # 检查最后活动时间
        silence_duration = now - session.last_activity_at
        if silence_duration < proactive_config.silence_threshold:
            return False

        # 检查最小间隔
        if session.last_proactive_at:
            interval = now - session.last_proactive_at
            if interval < proactive_config.min_interval:
                return False

        # 概率衰减触发：沉默越久概率越高
        base_prob = proactive_config.trigger_probability
        threshold = proactive_config.silence_threshold
        # 超出阈值的倍数（至少为 1）
        excess_ratio = max(1.0, silence_duration / threshold) if threshold > 0 else 1.0
        # 线性递增：每多一个阈值时长，概率增加 base_prob * 0.3，上限 1.0
        effective_prob = min(1.0, base_prob * (1.0 + 0.3 * (excess_ratio - 1.0)))
        # 非活跃时段惩罚
        effective_prob *= _activity_penalty

        if random.random() > effective_prob:
            return False

        logger.info(
            f"主动发起条件满足: stream={session.stream_id[:8]}, "
            f"沉默 {silence_duration:.0f}s, 有效概率 {effective_prob:.2f}"
        )
        return True

    def _is_quiet_hours(self) -> bool:
        """检查当前是否在勿扰时段。"""
        proactive_config = self._config.proactive

        try:
            now = time.localtime()
            current_minutes = now.tm_hour * 60 + now.tm_min

            start_parts = proactive_config.quiet_hours_start.split(":")
            start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])

            end_parts = proactive_config.quiet_hours_end.split(":")
            end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])

            if start_minutes <= end_minutes:
                return start_minutes <= current_minutes < end_minutes
            # 跨午夜
            return current_minutes >= start_minutes or current_minutes < end_minutes

        except (ValueError, IndexError):
            return False

    async def mark_triggered(self, stream_id: str) -> dict[str, object]:
        """标记 Session 已触发主动发起，并返回预约 payload。"""
        async with self._session_store.lock(stream_id):
            session = await self._session_store.get(stream_id)
            if not session:
                return {}

            payload = {
                "scheduled_reason": session.scheduled_proactive_reason,
                "scheduled_context": session.scheduled_proactive_context,
                "scheduled_start_at": session.scheduled_proactive_start_at,
                "scheduled_end_at": session.scheduled_proactive_end_at,
                "scheduled_interest": session.scheduled_proactive_interest,
                "from_window": session.scheduled_proactive_start_at is not None,
            }
            session.last_proactive_at = time.time()
            if session.scheduled_proactive_start_at is None:
                session.scheduled_proactive_at = None
                session.scheduled_proactive_reason = ""
            await self._session_store.save(session)
            return payload
        return {}
