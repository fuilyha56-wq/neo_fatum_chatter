"""NFC 完整请求体快照的捕获与冷启动恢复处理器。"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.core.components.types import EventType

from ..snapshot import (
    PayloadSnapshot,
    capture_payload_snapshot,
    restore_payload_snapshot,
)

logger = get_logger("NFC_request_snapshot")

_NFC_REQUEST_NAME = "neo_fatum_chatter"
_NON_HISTORY_ROLES = {"system", "tool"}


class NFCRequestSnapshotHandler(BaseEventHandler):
    """在 NFC 请求发送前保存并在冷启动首个请求恢复完整 payload 链。"""

    handler_name = "nfc_request_snapshot_handler"
    handler_description = "保存并恢复 NFC 的完整 LLM 请求体"
    display_name = "请求体恢复"
    weight = 20
    intercept_message = False
    init_subscribe = [EventType.BEFORE_LLM_REQUEST]

    _restored_streams: set[str] = set()

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """恢复上次快照并覆盖保存本次最终请求 payload。"""
        if str(event_name) != EventType.BEFORE_LLM_REQUEST.value:
            return EventDecision.PASS, params
        if str(params.get("request_name") or "") != _NFC_REQUEST_NAME:
            return EventDecision.PASS, params

        config = getattr(self.plugin, "config", None)
        prompt_config = getattr(config, "prompt", None)
        if not bool(getattr(prompt_config, "request_snapshot_enabled", True)):
            return EventDecision.PASS, params

        meta_data = params.get("meta_data")
        stream_id = (
            str(meta_data.get("stream_id") or "")
            if isinstance(meta_data, dict)
            else ""
        )
        payloads = params.get("payloads")
        if not stream_id or not isinstance(payloads, list):
            return EventDecision.PASS, params

        session_store = self.plugin.session_store
        async with session_store.lock(stream_id):
            session = await session_store.get_or_create(stream_id)
            if stream_id not in self._restored_streams:
                snapshot_data = session.request_snapshot
                if snapshot_data:
                    restored = restore_payload_snapshot(
                        PayloadSnapshot.from_dict(snapshot_data)
                    )
                    if restored:
                        params["payloads"] = self._inject_history(payloads, restored)
                        payloads = params["payloads"]
                        logger.info(
                            f"[NFC] 已恢复完整请求体: stream={stream_id[:8]} "
                            f"payloads={len(restored)}"
                        )
                self._restored_streams.add(stream_id)
                setattr(session, "_nfc_request_snapshot_restored", True)

            snapshot = capture_payload_snapshot(stream_id, payloads)
            if snapshot is not None:
                session.request_snapshot = snapshot.to_dict()
                await session_store.save(session)

        return EventDecision.SUCCESS, params

    @staticmethod
    def _inject_history(
        payloads: list[Any],
        restored: list[Any],
    ) -> list[Any]:
        """把恢复历史插入当前 system/tool 声明之后。"""
        split_at = 0
        for index, payload in enumerate(payloads):
            role = getattr(getattr(payload, "role", None), "value", None)
            if str(role or "") not in _NON_HISTORY_ROLES:
                break
            split_at = index + 1
        return payloads[:split_at] + restored + payloads[split_at:]


__all__ = ["NFCRequestSnapshotHandler"]