"""让 NFC 会话跟随主程序的清空上下文命令。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.app.plugin_system.api import stream_api
from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import ChatStream, EventType
from src.kernel.concurrency import get_task_manager

from .request_snapshot_handler import NFCRequestSnapshotHandler

logger = get_logger("NFC_context_clear")

_CLEAR_COMMAND_NAME = "清空上下文"
_NFC_CHATTER_NAME = "neo_fatum_chatter"


class NFCContextClearHandler(BaseEventHandler):
    """在主程序成功清空聊天流后同步重置 NFC 的独立上下文。"""

    name = "nfc_context_clear_handler"
    description = "同步清空 NFC 的持久化会话上下文与运行时请求链"
    display_name = "上下文清空同步"
    weight = 20
    intercept_message = False
    init_subscribe = [EventType.AFTER_COMMAND_EXECUTE]
    timeout = 0

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理成功的清空上下文命令及其全部子命令。"""
        if str(event_name) != EventType.AFTER_COMMAND_EXECUTE.value:
            return EventDecision.PASS, params
        if str(params.get("command_name") or "") != _CLEAR_COMMAND_NAME:
            return EventDecision.PASS, params
        if not bool(params.get("success")):
            return EventDecision.PASS, params

        stream_ids = await self._resolve_nfc_stream_ids(params)
        for stream_id in stream_ids:
            try:
                await self._quiesce_nfc_runtime(stream_id)
            except Exception as exc:
                logger.error(
                    f"[NFC] 停止旧上下文运行态失败: stream={stream_id[:8]}, {exc}",
                    exc_info=True,
                )

        try:
            await self._refresh_core_clear_boundary(params)
        except Exception as exc:
            logger.error(
                f"[NFC] 刷新主程序清空水位失败: {exc}",
                exc_info=True,
            )

        cleared_at = time.time()
        cleared_count = 0
        for stream_id in stream_ids:
            try:
                if await self._reset_session(stream_id, cleared_at):
                    cleared_count += 1
            except Exception as exc:
                logger.error(
                    f"[NFC] 重置会话上下文失败: stream={stream_id[:8]}, {exc}",
                    exc_info=True,
                )

        logger.info(
            f"[NFC] 已同步清空上下文: targets={len(stream_ids)}, "
            f"sessions={cleared_count}"
        )
        return EventDecision.SUCCESS, params

    async def _resolve_nfc_stream_ids(
        self,
        params: dict[str, Any],
    ) -> list[str]:
        """按主命令路由语义解析需要重置的 NFC 会话 ID。"""
        message = params.get("message")
        current_stream_id = str(getattr(message, "stream_id", "") or "")
        platform = str(getattr(message, "platform", "") or "")
        raw_args = params.get("args")
        args = (
            [str(arg) for arg in raw_args]
            if isinstance(raw_args, (list, tuple))
            else []
        )

        if not args:
            return [current_stream_id] if current_stream_id else []

        route = args[0]
        if route == "群" and len(args) >= 2 and args[1]:
            if not platform:
                return []
            return [ChatStream.generate_stream_id(platform, group_id=args[1])]
        if route == "私" and len(args) >= 2 and args[1]:
            if not platform:
                return []
            return [ChatStream.generate_stream_id(platform, user_id=args[1])]

        known_ids = await self._known_nfc_stream_ids()
        if route in {"all", "全部"}:
            return sorted(known_ids)
        if route == "群":
            group_ids = set(await stream_api.get_stream_ids_from_db("group"))
            return sorted(known_ids & group_ids)
        if route == "私":
            private_ids = set(await stream_api.get_stream_ids_from_db("private"))
            return sorted(known_ids & private_ids)
        return []

    async def _known_nfc_stream_ids(self) -> set[str]:
        """合并 NFC 已持久化和仅在内存中的会话 ID。"""
        session_store = self.plugin.session_store
        persisted = await session_store.list_all_stream_ids()
        cached = session_store.get_all_cached()
        return {str(stream_id) for stream_id in persisted} | {
            str(stream_id) for stream_id in cached
        }

    async def _refresh_core_clear_boundary(self, params: dict[str, Any]) -> None:
        """在命令确认消息发送后再次推进主程序清空水位。"""
        message = params.get("message")
        current_stream_id = str(getattr(message, "stream_id", "") or "")
        platform = str(getattr(message, "platform", "") or "")
        raw_args = params.get("args")
        args = (
            [str(arg) for arg in raw_args]
            if isinstance(raw_args, (list, tuple))
            else []
        )

        if not args:
            if current_stream_id:
                await stream_api.load_and_clear_context(current_stream_id)
            return

        route = args[0]
        if route == "群":
            if len(args) >= 2 and args[1]:
                if platform:
                    stream_id = ChatStream.generate_stream_id(
                        platform,
                        group_id=args[1],
                    )
                    await stream_api.load_and_clear_context(stream_id)
                return
            await stream_api.bulk_clear_streams("group")
            return

        if route == "私":
            if len(args) >= 2 and args[1]:
                if platform:
                    stream_id = ChatStream.generate_stream_id(
                        platform,
                        user_id=args[1],
                    )
                    await stream_api.load_and_clear_context(stream_id)
                return
            await stream_api.bulk_clear_streams("private")
            return

        if route in {"all", "全部"}:
            await stream_api.bulk_clear_streams()

    async def _quiesce_nfc_runtime(self, stream_id: str) -> None:
        """停止旧 NFC 生成器并取消尚未完成的旧摘要任务。"""
        await self._stop_active_nfc_runtime(stream_id)

        task_name = f"NFC_compress_{stream_id}"
        tasks = [
            task_info.task
            for task_info in get_task_manager().get_active_tasks()
            if task_info.name == task_name and task_info.task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _stop_active_nfc_runtime(stream_id: str) -> None:
        """停止当前流的 NFC 生成器，避免旧 response 链继续运行。"""
        from src.core.managers.chatter_manager import get_chatter_manager
        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )

        chatter_manager = get_chatter_manager()
        chatter = chatter_manager.get_chatter_by_stream(stream_id)
        if getattr(chatter, "name", "") != _NFC_CHATTER_NAME:
            return

        stream_loop_manager = get_stream_loop_manager()
        await stream_loop_manager.stop_stream_loop(stream_id)

        chatter_gene = stream_loop_manager._chatter_genes.pop(stream_id, None)
        if chatter_gene is not None:
            try:
                await chatter_gene.aclose()
            except (RuntimeError, StopAsyncIteration):
                pass

        stream_loop_manager._wait_states.pop(stream_id, None)
        stream_loop_manager._pending_wait_resume_events.pop(stream_id, None)
        chatter_manager.unregister_active_chatter(stream_id)

    async def _reset_session(self, stream_id: str, cleared_at: float) -> bool:
        """持锁重置并保存一个已存在的 NFC 会话。"""
        session_store = self.plugin.session_store
        async with session_store.lock(stream_id):
            session = await session_store.get(stream_id)
            if session is None:
                return False
            session.reset_context(cleared_at)
            await session_store.save(session)

        NFCRequestSnapshotHandler._restored_streams.discard(stream_id)
        return True


__all__ = ["NFCContextClearHandler"]