"""主动发起模块。

ProactiveThinker 负责在长时间沉默后评估是否主动发起对话。
通过 Scheduler 定期调度。
"""

from __future__ import annotations

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
        self._activity_service_cache: object | None = None
        self._activity_service_resolved = False

    async def check_all_sessions(self) -> list[str]:
        """检查所有缓存中的 Session，返回需要主动发起的 stream_id 列表。"""
        proactive_config = self._config.proactive
        if not proactive_config.enabled:
            return []

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
            session = await self._session_store.peek(stream_id)
            if session is None:
                continue
            if session.scheduled_proactive_at is not None:
                now = time.time()
                if now >= session.scheduled_proactive_at:
                    logger.info(f"主动思考（磁盘 session）：触发预约 stream={stream_id[:8]}")
                    triggered.append(stream_id)

        return triggered

    async def _check_and_trigger(self, stream_id: str, session: NFCSession) -> bool:
        """检查单个 session 是否应触发，处理过期预约的持久化清除。"""
        now = time.time()
        if session.scheduled_proactive_at is not None:
            if now >= session.scheduled_proactive_at:
                logger.info(f"主动思考：触发模型预约 stream={stream_id[:8]}")
                return True
            return False

        return await self._should_trigger(session)

    async def _should_trigger(self, session: NFCSession) -> bool:
        """判断无预约情况下是否应主动发起（沉默条件 + 衰减概率）。"""
        # 勿扰时段：仅拦截沉默触发，不影响模型预约
        if self._is_quiet_hours():
            return False

        # 用户活跃时段检查：使用外部 Service 或 fallback 内置
        _activity_penalty = await self._get_activity_penalty(session)

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
        excess_ratio = max(1.0, silence_duration / threshold) if threshold > 0 else 1.0
        effective_prob = min(1.0, base_prob * (1.0 + 0.3 * (excess_ratio - 1.0)))
        # 活跃度惩罚
        effective_prob *= _activity_penalty

        if random.random() > effective_prob:
            return False

        logger.info(
            f"主动发起条件满足: stream={session.stream_id[:8]}, "
            f"沉默 {silence_duration:.0f}s, 有效概率 {effective_prob:.2f}"
        )
        return True

    async def _get_activity_penalty(self, session: NFCSession) -> float:
        """获取活跃度乘数。

        优先调用配置指定的外部 Service（如 BCT），
        失败或未配置时 fallback 到内置的 is_user_typically_active_now()。

        Returns:
            float 0~1 作为概率乘数。1.0 = 活跃时段不降级。
        """
        proactive_config = self._config.proactive
        signature = proactive_config.activity_service_signature
        method_name = proactive_config.activity_service_method

        if signature:
            score = await self._call_activity_service(
                signature, method_name, session.stream_id
            )
            if score is not None:
                # score 0~1 直接作为乘数：高分(活跃)→乘数大，低分(不活跃)→乘数小
                return max(0.1, score)  # 下限 0.1，不完全阻断

        # Fallback: 内置判断
        if not session.is_user_typically_active_now():
            logger.debug(
                f"当前时段非用户典型活跃时段，降低主动发起概率: stream={session.stream_id[:8]}"
            )
            return 0.3
        return 1.0

    async def _call_activity_service(
        self, signature: str, method_name: str, stream_id: str
    ) -> float | None:
        """尝试调用外部活跃度服务。

        Returns:
            float 0~1 或 None（调用失败时）。
        """
        # 延迟解析 service，缓存结果避免每次 import
        if not self._activity_service_resolved:
            self._activity_service_resolved = True
            try:
                from src.core.components.managers.service_manager import ServiceManager
                self._activity_service_cache = ServiceManager.get_service(signature)
            except Exception as e:
                logger.debug(f"活跃度服务 {signature} 解析失败: {e}")
                self._activity_service_cache = None

        service = self._activity_service_cache
        if service is None:
            return None

        try:
            method = getattr(service, method_name, None)
            if method is None:
                logger.debug(f"活跃度服务无 {method_name} 方法")
                return None
            result = await method(stream_id)
            if isinstance(result, (int, float)):
                return max(0.0, min(1.0, float(result)))
        except Exception as e:
            logger.debug(f"调用活跃度服务失败: {e}")

        return None

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

    async def mark_triggered(self, stream_id: str) -> str:
        """标记 Session 已触发主动发起，同时清除模型预约。

        Returns:
            str: 清除前的预约理由，无预约时为空字符串。
        """
        async with self._session_store.lock(stream_id):
            session = await self._session_store.get(stream_id)
            if session:
                reason = session.scheduled_proactive_reason
                session.last_proactive_at = time.time()
                session.scheduled_proactive_at = None
                session.scheduled_proactive_reason = ""
                await self._session_store.save(session)
                return reason
        return ""
