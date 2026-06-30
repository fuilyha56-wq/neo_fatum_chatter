"""NFC 超时服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload

from .context_sanitizer import close_pending_tool_chain

from ..thinker.timeout_handler import TimeoutHandler

if TYPE_CHECKING:
    from ..config import NFCConfig
    from ..session import NFCSession


logger = get_logger("NFC_timeout_service")


@dataclass(slots=True)
class TimeoutResult:
    """一次超时处理的输出。"""

    payload: LLMPayload
    is_final_timeout: bool


class TimeoutService:
    """封装等待超时处理与 payload 构建。"""

    def __init__(self, config: NFCConfig) -> None:
        self._config = config
        self._handler = TimeoutHandler(config)

    def check_timeout(self, session: NFCSession) -> bool:
        """检查是否达到超时条件。"""
        return self._handler.check_timeout(session)

    def build_timeout_result(self, response: Any, session: NFCSession) -> TimeoutResult:
        """处理超时并返回追加到 response 的 user payload。"""
        self._close_pending_tool_chain(response)
        timeout_ctx = self._handler.handle_timeout(session)
        is_final_timeout = self._handler.should_give_up(session)

        from ..prompts.builder import NFCPromptBuilder

        payload = NFCPromptBuilder.build_timeout_payload(
            elapsed_seconds=timeout_ctx["elapsed_seconds"],  # type: ignore[arg-type]
            expected_reaction=timeout_ctx["expected_reaction"],  # type: ignore[arg-type]
            consecutive_timeouts=timeout_ctx["consecutive_timeouts"],  # type: ignore[arg-type]
            last_bot_message=timeout_ctx.get("last_bot_message", ""),  # type: ignore[arg-type]
            max_consecutive_timeouts=self._config.wait.max_consecutive_timeouts,
        )
        return TimeoutResult(payload=payload, is_final_timeout=is_final_timeout)

    @staticmethod
    def _close_pending_tool_chain(response: Any) -> None:
        """必要时插入 assistant 桥接 payload，闭合 tool_result 链。"""
        close_pending_tool_chain(response, reason="超时触发")