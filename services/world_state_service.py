"""现实日常世界状态的统一读写服务。

该服务把 action 层与 NFCSessionStore 的锁、幂等和领域状态转换隔开：
计划、明确观察和角色自述仍然是不同的证据等级，任何操作都不会仅凭
模型传入的 status 把计划伪造成现实执行事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.daily_life import (
    AgendaItem,
    DailyLifeState,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
)


@dataclass(slots=True)
class WorldMutationResult:
    """供 action 层自然语言化的结构化结果。"""

    ok: bool
    operation: str
    message: str
    item: AgendaItem | None = None
    items: list[AgendaItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation,
            "message": self.message,
            "item": self.item.to_dict() if self.item else None,
            "items": [item.to_dict() for item in self.items],
        }


class WorldStateService:
    """按 stream 原子修改现实日常状态。"""

    def __init__(self, session_store: Any) -> None:
        self._session_store = session_store

    async def update_self_state(
        self,
        stream_id: str,
        *,
        activity: str = "",
        end_time: str = "",
        location: str = "",
    ) -> WorldMutationResult:
        """写入或清除角色自己的当前状态（self_state_commit）。"""
        async with self._session_store.lock(stream_id):
            session = await self._session_store.get_or_create(stream_id)
            if getattr(session, "active_register", "reality") == "story":
                return WorldMutationResult(
                    False,
                    "update_self_state",
                    "当前处于剧情登记簿，不能写入现实日常；请先暂停或退出剧情。",
                )
            daily = session.daily_life
            activity = str(activity or "")
            end_time = str(end_time or "")
            location = str(location or "")
            if not activity.strip():
                changed = daily.clear_commit()
                await self._session_store.save(session)
                return WorldMutationResult(
                    ok=True,
                    operation="clear_self_state",
                    message=("已清除当前活动自述" if changed else "当前没有活动自述"),
                )
            item = daily.commit_activity(
                activity,
                end=end_time or None,
                location=location,
            )
            await self._session_store.save(session)
            return WorldMutationResult(
                ok=item is not None,
                operation="update_self_state",
                message="已记录当前活动自述" if item else "活动不能为空",
                item=item,
            )

    async def manage_schedule(
        self,
        stream_id: str,
        *,
        operation: str,
        item_id: str = "",
        activity: str = "",
        start_time: str = "",
        end_time: str = "",
        location: str = "",
        note: str = "",
        source_ref: str = "",
        idempotency_key: str = "",
        reason: str = "",
    ) -> WorldMutationResult:
        """执行 add/list/confirm/observe/cancel/expire。

        ``observe`` 必须由调用方显式选择；它会新增 observed 条目，原计划
        仍保留为 planned/confirmed 记录，不会被覆盖成执行事实。
        """
        op = str(operation or "").strip().lower()
        item_id = str(item_id or "")
        activity = str(activity or "")
        start_time = str(start_time or "")
        end_time = str(end_time or "")
        location = str(location or "")
        note = str(note or "")
        source_ref = str(source_ref or "")
        idempotency_key = str(idempotency_key or "")
        reason = str(reason or "")
        if op not in {"add", "list", "confirm", "observe", "cancel", "expire"}:
            return WorldMutationResult(False, op, f"不支持的日程操作：{op or '空'}")

        async with self._session_store.lock(stream_id):
            session = await self._session_store.get_or_create(stream_id)
            if getattr(session, "active_register", "reality") == "story":
                return WorldMutationResult(
                    False,
                    op,
                    "当前处于剧情登记簿，不能修改现实日程；请先暂停或退出剧情。",
                )
            daily: DailyLifeState = session.daily_life
            revision_before_rollover = daily.revision
            daily.ensure_day()

            if op == "add":
                if not activity.strip():
                    return WorldMutationResult(False, op, "新增日程时 activity 不能为空")
                item = daily.add_plan(
                    activity,
                    start=start_time or None,
                    end=end_time or None,
                    location=location,
                    note=note,
                    source_ref=source_ref,
                    idempotency_key=idempotency_key,
                )
                await self._session_store.save(session)
                if item is None:
                    return WorldMutationResult(False, op, "日程内容无效")
                return WorldMutationResult(
                    True,
                    op,
                    "已新增计划（仍是 planned，不代表已经执行）",
                    item=item,
                )

            if op == "list":
                items = [*daily.plan_items, *daily.observations]
                items = [item for item in items if item.is_active or item.is_fact]
                if daily.revision != revision_before_rollover:
                    await self._session_store.save(session)
                return WorldMutationResult(True, op, "已读取当前日程", items=items[-20:])

            if op == "observe":
                source_item = daily.find_item(item_id) if item_id else None
                # observed 必须有本次独立观察文本；不能仅复制计划内容，
                # 否则 planned 会被模型无意间伪造成 L2 执行事实。
                observed_activity = activity.strip()
                if not observed_activity:
                    return WorldMutationResult(False, op, "observe 必须提供独立的 activity 观察描述")
                key = idempotency_key or (
                    f"observe:{source_item.item_id}" if source_item else ""
                )
                existing = daily.find_by_idempotency(key) if key else None
                if existing is not None:
                    return WorldMutationResult(True, op, "重复观察已幂等忽略", item=existing)
                item = daily.add_observation(
                    observed_activity,
                    location=location or (source_item.location if source_item else ""),
                    source_ref=source_item.item_id if source_item else source_ref,
                    idempotency_key=key,
                )
                if item is not None and source_item is not None:
                    daily.reconcile_observation(source_item.item_id, item)
                    for candidate in session.proactive_candidates:
                        if candidate.dedupe_key.startswith(f"agenda:{source_item.item_id}:"):
                            candidate.mark("cancelled", reason="agenda_observed")
                await self._session_store.save(session)
                return WorldMutationResult(
                    item is not None,
                    op,
                    "已记录明确观察事实" if item else "观察内容无效",
                    item=item,
                )

            item = daily.find_item(item_id)
            if item is None:
                return WorldMutationResult(False, op, "未找到对应日程条目")
            target_status = {
                "confirm": STATUS_CONFIRMED,
                "cancel": STATUS_CANCELLED,
                "expire": STATUS_EXPIRED,
            }[op]
            changed = daily.update_item_status(item.item_id, target_status, reason=reason)
            if changed is None:
                return WorldMutationResult(False, op, "该条目不允许进行此状态转换", item=item)
            if op in {"cancel", "expire"}:
                for candidate in session.proactive_candidates:
                    if candidate.dedupe_key.startswith(f"agenda:{item.item_id}:"):
                        candidate.mark("cancelled", reason=f"agenda_{op}")
            await self._session_store.save(session)
            return WorldMutationResult(True, op, f"已将条目标记为 {target_status}", item=changed)


__all__ = ["WorldMutationResult", "WorldStateService"]
