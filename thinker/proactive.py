"""主动发起模块。

ProactiveThinker 负责在长时间沉默后评估是否主动发起对话。
通过 Scheduler 定期调度。
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger

from ..domain.proactive_candidate import (
    CANDIDATE_BLOCKED,
    CANDIDATE_DEFERRED,
    CANDIDATE_DISPATCHING,
    CANDIDATE_QUEUED,
    CANDIDATE_SENT,
    ProactiveCandidate,
)

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
        # 本次检查中通过意图队列选中的意图（stream_id → Intent），
        # 由 mark_triggered 消费并标记 fired
        self._selected_intents: dict[str, Any] = {}
        # 无意图的沉默触发也需要在 mark_triggered 时收口候选状态。
        self._selected_candidates: dict[str, str] = {}

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

        # 检查磁盘上未在内存中的 session（仅检查预约，避免大量沉默触发）。
        # 必须在锁内 get 当前 session，不能修改 peek() 返回的 detached 快照后
        # 再整表覆盖，否则并发用户消息/新预约可能被旧快照复活。
        all_stream_ids = await self._session_store.list_all_stream_ids()
        for stream_id in all_stream_ids:
            if stream_id in cached_sessions:
                continue
            async with self._session_store.lock(stream_id):
                session = await self._session_store.get(stream_id)
                if session is None or not session.proactive_enabled:
                    continue
                if session.scheduled_proactive_at is None:
                    continue

                now = time.time()
                scheduled_at = float(session.scheduled_proactive_at)
                candidate = ProactiveCandidate(
                    origin_id=f"schedule:{int(scheduled_at)}",
                    dedupe_key=f"schedule:{int(scheduled_at)}",
                    route="timer",
                    source="schedule",
                    reason=session.scheduled_proactive_reason,
                    preferred_at=scheduled_at,
                    window_start=scheduled_at,
                    best_until=scheduled_at + 6 * 3600,
                    expire_at=scheduled_at + 48 * 3600,
                )
                selected_candidate = session.upsert_proactive_candidate(candidate)
                if selected_candidate.is_dispatching:
                    await self._session_store.save(session)
                    continue
                if selected_candidate.is_terminal:
                    session.scheduled_proactive_at = None
                    session.scheduled_proactive_reason = ""
                    await self._session_store.save(session)
                    continue

                if now >= scheduled_at:
                    self._selected_candidates[stream_id] = selected_candidate.dedupe_key
                    logger.info(f"主动思考（磁盘 session）：触发预约 stream={stream_id[:8]}")
                    triggered.append(stream_id)
                await self._session_store.save(session)

        return triggered

    async def _check_and_trigger(self, stream_id: str, session: NFCSession) -> bool:
        """检查单个 session 是否应触发，处理过期预约的持久化清除。"""
        if not session.proactive_enabled:
            logger.debug(
                f"当前会话已暂停主动联系: stream={stream_id[:8]} "
                f"reason={session.proactive_paused_reason or '未说明'}"
            )
            return False
        now = time.time()
        if session.expire_proactive_candidates(now):
            await self._persist_candidate_state(stream_id, session)
        if session.scheduled_proactive_at is not None:
            scheduled_at = float(session.scheduled_proactive_at)
            candidate = ProactiveCandidate(
                origin_id=f"schedule:{int(scheduled_at)}",
                dedupe_key=f"schedule:{int(scheduled_at)}",
                route="timer",
                source="schedule",
                reason=session.scheduled_proactive_reason,
                preferred_at=scheduled_at,
                window_start=scheduled_at,
                best_until=scheduled_at + 6 * 3600,
                expire_at=scheduled_at + 48 * 3600,
            )
            selected_candidate = session.upsert_proactive_candidate(candidate)
            if selected_candidate.is_dispatching:
                return False
            if selected_candidate.is_terminal:
                session.scheduled_proactive_at = None
                session.scheduled_proactive_reason = ""
                await self._persist_candidate_state(stream_id, session)
                return False
            if now >= scheduled_at:
                self._selected_candidates[stream_id] = selected_candidate.dedupe_key
                logger.info(f"主动思考：触发模型预约 stream={stream_id[:8]}")
                await self._persist_candidate_state(stream_id, session)
                return True
            return False

        # 意图队列优先：欲望溢出 / 承诺到期 / 话题钩子（有目的的主动）
        if await self._check_intent_queue(stream_id, session, now):
            return True

        # 已确认的角色日程可形成 self-life 候选，但仍受勿扰/冷却约束。
        if await self._check_agenda_candidates(stream_id, session, now):
            return True

        return await self._should_trigger(session)

    async def _check_intent_queue(
        self, stream_id: str, session: NFCSession, now: float
    ) -> bool:
        """检查意图队列是否有冲动值过阈的意图。"""
        intent_cfg = getattr(self._config, "intent", None)
        if intent_cfg is None or not intent_cfg.enabled:
            return False

        queue = session.intents
        queue.grow_all(now)
        queue.prune(now)

        # 内驱溢出生成一次性 drive 意图
        drives_cfg = getattr(self._config, "drives", None)
        if drives_cfg is not None and drives_cfg.enabled:
            session.drives.advance_to(now)
            urge = session.drives.proactive_urge()
            if urge >= 0.6:
                queue.add(
                    "就是忽然想找 Ta 说说话",
                    kind="drive",
                    urge=urge * 0.9,
                )

        intent = queue.peek_fire(intent_cfg.fire_threshold, now=now)
        if intent is None:
            session.expire_proactive_candidates(now)
            return False

        # 候选是审计与去重层，不改变既有 IntentQueue 的决策结果。
        candidate = ProactiveCandidate(
            origin_id=f"intent:{intent.id}",
            dedupe_key=f"intent:{intent.id}",
            route=("commitment" if intent.kind == "commitment" else "continuation"),
            source=intent.kind,
            reason=intent.content,
            preferred_at=float(intent.deadline or now),
            window_start=float(intent.deadline or now),
            best_until=float(intent.deadline or now) + 6 * 3600,
            expire_at=float(intent.deadline or now) + 48 * 3600,
        )
        selected = session.upsert_proactive_candidate(candidate)
        if selected.is_dispatching or selected.is_terminal:
            return False
        if selected.status in {"blocked", "deferred"}:
            selected.mark("queued", reason="gate_recheck")
        await self._persist_candidate_state(stream_id, session)

        # 承诺到期不受勿扰与最小间隔限制（说好的事要做到）；
        # 话题钩子/内驱溢出仍走勿扰与冷却。
        if intent.kind != "commitment":
            candidate = next(
                (item for item in session.proactive_candidates
                 if item.dedupe_key == f"intent:{intent.id}"),
                None,
            )
            if self._is_quiet_hours():
                if candidate is not None:
                    candidate.mark(CANDIDATE_BLOCKED, reason="quiet_hours")
                    await self._persist_candidate_state(stream_id, session)
                return False
            if session.last_proactive_at:
                if now - session.last_proactive_at < self._config.proactive.min_interval:
                    if candidate is not None:
                        candidate.mark(CANDIDATE_DEFERRED, reason="min_interval")
                        await self._persist_candidate_state(stream_id, session)
                    return False

        logger.info(
            f"主动发起（意图队列）: stream={stream_id[:8]}, "
            f"kind={intent.kind}, urge={intent.urge:.2f}, content={intent.content[:50]}"
        )
        self._selected_intents[stream_id] = intent
        self._selected_candidates[stream_id] = selected.dedupe_key
        return True

    async def _check_agenda_candidates(
        self, stream_id: str, session: NFCSession, now: float
    ) -> bool:
        """把已确认的角色日程映射为受 gate 约束的 self-life 候选。"""
        daily = getattr(session, "daily_life", None)
        if daily is None:
            return False
        daily.ensure_day(now)
        local = time.localtime(now)
        midnight = time.mktime((
            local.tm_year, local.tm_mon, local.tm_mday,
            0, 0, 0, local.tm_wday, local.tm_yday, local.tm_isdst,
        ))
        due_candidates: list[ProactiveCandidate] = []
        for item in daily.plan_items:
            if item.source != "planned" or item.status != "confirmed":
                continue
            if item.start_minute < 0:
                continue
            preferred_at = midnight + item.start_minute * 60
            if item.end_minute >= 0:
                end_at = midnight + item.end_minute * 60
                if item.end_minute <= item.start_minute:
                    end_at += 24 * 3600
                best_until = end_at
            else:
                best_until = preferred_at + 3 * 3600
            candidate = ProactiveCandidate(
                origin_id=f"agenda:{item.item_id}:{item.revision}",
                dedupe_key=f"agenda:{item.item_id}:{item.revision}",
                route="self_life",
                source="agenda",
                reason=f"你的安排「{item.activity}」到时间了",
                preferred_at=preferred_at,
                window_start=preferred_at,
                best_until=best_until,
                expire_at=max(best_until, preferred_at + 30 * 60),
            )
            selected = session.upsert_proactive_candidate(candidate)
            if selected.is_dispatching or selected.is_terminal:
                continue
            if selected.status in {"blocked", "deferred"}:
                selected.mark("queued", reason="gate_recheck")
            if selected.is_due(now):
                due_candidates.append(selected)

        session.expire_proactive_candidates(now)
        if not due_candidates:
            return False
        due_candidates.sort(key=lambda item: (item.preferred_at, item.created_at))
        selected = due_candidates[0]
        if self._is_quiet_hours():
            selected.mark(CANDIDATE_BLOCKED, reason="quiet_hours")
            await self._persist_candidate_state(stream_id, session)
            return False
        if session.last_proactive_at and (
            now - session.last_proactive_at < self._config.proactive.min_interval
        ):
            selected.mark(CANDIDATE_DEFERRED, reason="min_interval")
            await self._persist_candidate_state(stream_id, session)
            return False
        self._selected_candidates[stream_id] = selected.dedupe_key
        await self._persist_candidate_state(stream_id, session)
        return True

    async def _persist_candidate_state(self, stream_id: str, session: NFCSession) -> None:
        """保存候选审计状态；失败不阻塞主动决策。"""
        try:
            async with self._session_store.lock(stream_id):
                current = await self._session_store.get(stream_id)
                if current is not None:
                    current.proactive_candidates = session.proactive_candidates
                    current.scheduled_proactive_at = session.scheduled_proactive_at
                    current.scheduled_proactive_reason = session.scheduled_proactive_reason
                    await self._session_store.save(current)
        except Exception as exc:
            logger.debug(f"主动候选状态保存失败: stream={stream_id[:8]}: {exc}")

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
        candidate = ProactiveCandidate(
            origin_id=f"silence:{int(session.last_activity_at or 0)}",
            dedupe_key=f"silence:{int(session.last_activity_at or 0)}",
            route="silence",
            source="silence",
            reason=f"沉默 {silence_duration:.0f} 秒",
            preferred_at=now,
            window_start=now,
            best_until=now + max(3600.0, proactive_config.min_interval),
            expire_at=now + 6 * 3600,
        )
        selected_candidate = session.upsert_proactive_candidate(candidate)
        if selected_candidate.is_dispatching or selected_candidate.is_terminal:
            return False
        self._selected_intents.pop(session.stream_id, None)
        self._selected_candidates[session.stream_id] = selected_candidate.dedupe_key
        await self._persist_candidate_state(session.stream_id, session)
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

    async def prepare_trigger(self, stream_id: str) -> tuple[str, str]:
        """原子地声明一个待投递候选，返回 ``(candidate_key, reason)``。

        这里只进入 ``dispatching``，不消耗预约、意图或冷却；事件发布失败时
        可由 :meth:`settle_trigger` 回滚为 queued，避免“没发出去但已完成”的丢失。
        """
        async with self._session_store.lock(stream_id):
            session = await self._session_store.get(stream_id)
            if session is None or not session.proactive_enabled:
                return "", ""

            candidate_key = self._selected_candidates.get(stream_id, "")
            intent = self._selected_intents.get(stream_id)
            if not candidate_key and intent is not None:
                candidate_key = f"intent:{intent.id}"
            if not candidate_key and session.scheduled_proactive_at is not None:
                candidate_key = f"schedule:{int(session.scheduled_proactive_at)}"

            candidate = next(
                (item for item in session.proactive_candidates
                 if item.dedupe_key == candidate_key),
                None,
            ) if candidate_key else None
            if candidate is None and session.scheduled_proactive_at is not None and candidate_key.startswith("schedule:"):
                scheduled_at = float(session.scheduled_proactive_at)
                candidate = session.upsert_proactive_candidate(ProactiveCandidate(
                    origin_id=candidate_key,
                    dedupe_key=candidate_key,
                    route="timer",
                    source="schedule",
                    reason=session.scheduled_proactive_reason,
                    preferred_at=scheduled_at,
                    window_start=scheduled_at,
                    best_until=scheduled_at + 6 * 3600,
                    expire_at=scheduled_at + 48 * 3600,
                ))

            if candidate is None:
                self._selected_candidates.pop(stream_id, None)
                self._selected_intents.pop(stream_id, None)
                return "", ""
            # 用户消息已经取消的 continuation/silence 候选不得再投递。
            if (
                candidate.route in {"continuation", "silence"}
                and session.last_user_message_at is not None
                and session.last_user_message_at > candidate.created_at
            ):
                candidate.mark("cancelled", reason="user_message_before_dispatch")
                self._selected_candidates.pop(stream_id, None)
                self._selected_intents.pop(stream_id, None)
                await self._session_store.save(session)
                return "", ""
            if candidate.is_terminal:
                self._selected_candidates.pop(stream_id, None)
                self._selected_intents.pop(stream_id, None)
                return "", ""
            if candidate.is_dispatching:
                return "", ""
            if not candidate.mark(CANDIDATE_DISPATCHING, reason="dispatch_started"):
                return "", ""

            reason = session.scheduled_proactive_reason
            if intent is not None:
                if intent.kind == "commitment":
                    intent_reason = f"到了你答应过的时间——{intent.content}"
                else:
                    intent_reason = f"你一直惦记着一件事：{intent.content}"
                reason = f"{reason}\n{intent_reason}".strip()
            elif candidate is not None and not reason:
                reason = candidate.reason

            await self._session_store.save(session)
            return candidate_key, reason

    async def settle_trigger(
        self, stream_id: str, candidate_key: str, *, delivered: bool
    ) -> str:
        """提交主动事件投递结果；成功才消耗预约/意图，失败则可重试。"""
        async with self._session_store.lock(stream_id):
            session = await self._session_store.get(stream_id)
            if session is None:
                self._selected_candidates.pop(stream_id, None)
                self._selected_intents.pop(stream_id, None)
                return ""

            candidate = next(
                (item for item in session.proactive_candidates
                 if item.dedupe_key == candidate_key),
                None,
            )
            if candidate is not None:
                if delivered:
                    candidate.mark(CANDIDATE_SENT, reason="triggered")
                elif candidate.is_dispatching:
                    candidate.mark(CANDIDATE_QUEUED, reason="delivery_failed")
                elif candidate.is_terminal:
                    self._selected_candidates.pop(stream_id, None)
                    self._selected_intents.pop(stream_id, None)

            reason = session.scheduled_proactive_reason
            intent = self._selected_intents.get(stream_id)
            if delivered:
                session.last_proactive_at = time.time()
                if candidate is None or candidate.route == "timer" or session.scheduled_proactive_at is not None:
                    session.scheduled_proactive_at = None
                    session.scheduled_proactive_reason = ""
                if intent is not None:
                    session.intents.mark_fired(intent.id)
                    if getattr(self._config, "drives", None) and self._config.drives.enabled:
                        session.drives.on_proactive_fired()
                    if intent.kind == "commitment":
                        intent_reason = f"到了你答应过的时间——{intent.content}"
                    else:
                        intent_reason = f"你一直惦记着一件事：{intent.content}"
                    reason = f"{reason}\n{intent_reason}".strip()
                elif not reason and candidate is not None:
                    reason = candidate.reason
                self._selected_candidates.pop(stream_id, None)
                self._selected_intents.pop(stream_id, None)
            await self._session_store.save(session)
            return reason

    async def mark_triggered(self, stream_id: str) -> str:
        """兼容旧调用方：准备并视为本地投递成功。"""
        candidate_key, reason = await self.prepare_trigger(stream_id)
        if not candidate_key:
            return ""
        settled_reason = await self.settle_trigger(
            stream_id, candidate_key, delivered=True
        )
        return settled_reason or reason
