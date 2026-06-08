"""NFC 打断控制运行时。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.concurrency import get_task_manager

if TYPE_CHECKING:
    from ..chatter import NeoFatumChatter
    from ..config import NFCConfig


logger = get_logger("NFC_chatter")


def filter_interrupt_messages(
    messages: list[Any],
    known_unread_ids: frozenset[str],
) -> list[Any]:
    """筛选可用于打断当前 LLM 请求的新消息。"""
    interrupt_msgs: list[Any] = []
    for message in messages:
        message_id = getattr(message, "message_id", None)
        if not message_id:
            continue
        if str(message_id) not in known_unread_ids:
            interrupt_msgs.append(message)
    return interrupt_msgs


async def _cancel_and_await_task(task: asyncio.Task[Any]) -> None:
    """取消并等待任务结束，吞掉取消异常。"""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def send_interruptable_response(
    chatter: NeoFatumChatter,
    response: Any,
    config: NFCConfig,
    known_unread_ids: frozenset[str],
) -> tuple[Any | None, list[Any]]:
    """以可打断方式发送 LLM 请求。"""

    async def _llm_work() -> Any:
        return await chatter._send_with_perceive_loop(
            response,
            config.general.max_compat_retries,
        )

    tm = get_task_manager()
    llm_coro = _llm_work()
    task_handle = tm.create_task(
        llm_coro,
        name=f"NFC_llm_{chatter.stream_id[:8]}",
    )
    if task_handle.task is None:  # pragma: no cover
        llm_coro.close()
        raise RuntimeError("task_manager 未返回有效的 Task")
    llm_task: asyncio.Task[Any] = task_handle.task

    poll_interval = max(0.1, float(config.buffer.interrupt_poll_seconds))

    try:
        while not llm_task.done():
            await asyncio.sleep(poll_interval)
            if llm_task.done():
                break

            _, current_msgs = await chatter.fetch_unreads(
                time_format="%Y-%m-%d %H:%M:%S"
            )
            interrupt_msgs = filter_interrupt_messages(current_msgs, known_unread_ids)
            if interrupt_msgs:
                llm_task.cancel()
                try:
                    await llm_task
                except asyncio.CancelledError:
                    pass
                logger.info(
                    f"[打断] LLM 被取消，检测到 {len(interrupt_msgs)} 条新消息"
                )
                return None, interrupt_msgs

    except asyncio.CancelledError:
        llm_task.cancel()
        raise

    return llm_task.result(), []