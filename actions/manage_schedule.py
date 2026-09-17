"""现实日程管理动作。

计划、明确观察和角色自身当前状态保持不同证据等级；模型必须显式选择
``observe`` 才能写入 observed 记录。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction

from ..services.world_state_service import WorldMutationResult, WorldStateService

logger = get_logger("NFC_manage_schedule")


class ManageScheduleAction(BaseAction):
    """新增、查询、确认、观察或收口现实日程。"""

    name = "nfc_manage_schedule"
    description = (
        "管理你自己的现实日程。operation 可用 add/list/confirm/observe/cancel/expire。"
        "add 只创建 planned 计划；只有显式 observe 才记录 observed 事实，"
        "不能把计划或模型推断当作已经完成。"
    )
    chatter_allow: list[str] = ["neo_fatum_chatter"]
    associated_types = ["text"]

    async def execute(
        self,
        operation: Annotated[
            str,
            "操作：add 新增计划；list 查询；confirm 确认计划；observe 记录明确观察；cancel 取消；expire 标记失效。",
        ] = "list",
        item_id: Annotated[str, "目标条目 ID（confirm/cancel/expire/observe 时可用）。"] = "",
        activity: Annotated[str, "活动内容；add/observe 时可填。"] = "",
        start_time: Annotated[str, "开始时间，HH:MM 或中文时间；add 时可选。"] = "",
        end_time: Annotated[str, "结束时间，HH:MM 或中文时间；add 时可选。"] = "",
        location: Annotated[str, "地点，可选。"] = "",
        note: Annotated[str, "备注或取消原因，可选。"] = "",
        source_ref: Annotated[str, "外部结构化来源引用，可选；不会改变事实等级。"] = "",
        idempotency_key: Annotated[str, "重复请求幂等键，可选。"] = "",
        **_extra,
    ) -> tuple[bool, str]:
        if _extra:
            logger.debug(f"忽略 manage_schedule 未知参数: {sorted(_extra.keys())}")

        result = await WorldStateService(self.plugin.session_store).manage_schedule(
            self.chat_stream.stream_id,
            operation=operation,
            item_id=item_id,
            activity=activity,
            start_time=start_time,
            end_time=end_time,
            location=location,
            note=note,
            source_ref=source_ref,
            idempotency_key=idempotency_key,
            reason=note,
        )
        return result.ok, _render_result(result)


def _render_result(result: WorldMutationResult) -> str:
    if result.operation == "list":
        if not result.items:
            return "当前没有可用的现实日程。"
        lines = [f"{result.message}（{len(result.items)} 条）："]
        for item in result.items:
            start = _clock(item.start_minute)
            end = _clock(item.end_minute)
            period = f"{start}-{end}" if end else (f"{start} 起" if start else "未定时")
            lines.append(
                f"- ID={item.item_id} | {period} | {item.activity} | "
                f"source={item.source} status={item.status} "
                f"evidence={item.evidence_level}/{item.epistemic_status} "
                f"rev={item.revision}"
            )
        return "\n".join(lines)

    if result.item is not None:
        item = result.item
        return (
            f"{result.message}：{item.activity}（ID={item.item_id}，"
            f"source={item.source}，status={item.status}，"
            f"evidence={item.evidence_level}/{item.epistemic_status}，"
            f"revision={item.revision}）"
        )
    return result.message


def _clock(minutes: int) -> str:
    if not isinstance(minutes, int) or minutes < 0:
        return ""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


__all__ = ["ManageScheduleAction"]
