"""NFC 近期摘要服务。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.concurrency import get_task_manager

from ..compressor import compress_history, should_compress

if TYPE_CHECKING:
    from ..config import NFCConfig
    from ..prompts.builder import NFCPromptBuilder
    from ..session import NFCSession


logger = get_logger("NFC_summary_service")


class SummaryService:
    """处理对话链摘要压缩触发。"""

    @staticmethod
    def maybe_schedule_compression(
        session: NFCSession,
        prompt_builder: NFCPromptBuilder,
        config: NFCConfig,
        chat_stream: Any,
    ) -> bool:
        """按当前 session 状态决定是否调度近期摘要压缩。"""
        trigger_empty = not session.history_summary
        trigger_periodic = should_compress(session, config)
        if not (trigger_empty or trigger_periodic):
            return False

        reason = (
            "摘要为空（首次生成）"
            if trigger_empty
            else f"满足周期条件（{session.compress_round_count}轮）"
        )
        logger.info(f"[NFC] 触发近期记忆压缩：流 {session.stream_id}，原因：{reason}")
        get_task_manager().create_task(
            compress_history(session, prompt_builder, config, chat_stream),
            name=f"NFC_compress_{session.stream_id}",
        )
        return True