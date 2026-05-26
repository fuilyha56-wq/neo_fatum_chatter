"""框架 StreamLoopManager 唤醒适配器。

把"清除 stream 等待状态以便下一次 tick 立即唤醒"这块对私有
``_wait_states`` 的访问集中到这里。一旦框架公开正式的 ``wake_stream`` API，
只需要替换本模块即可，调用方（``ProactiveHandler``）不需要再改。

约定：
    - 仅在确认 stream 处于热状态（已经在内存中）时调用。
    - 任何异常都被吞掉转为日志，唤醒失败不应中断主动发起流程。
"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("NFC_stream_wake")


def wake_hot_stream(stream_id: str) -> bool:
    """清除框架内部 ``_wait_states`` 让 stream 在下一 tick 立即唤醒。

    Returns:
        bool: True 表示确实清除了等待状态；False 表示没有等待状态需要清，
        或框架不可用。两种情况都不算错误。
    """
    try:
        # HACK: 等待框架公开 ``loop_mgr.wake_stream(stream_id)`` 的稳定 API。
        # 在那之前用私有字段切换状态——见 mofox-wire issue（暂无）。
        from src.core.transport.distribution.stream_loop_manager import (
            get_stream_loop_manager,
        )
    except ImportError:
        logger.warning("StreamLoopManager 不可用，无法清除等待状态")
        return False

    try:
        loop_mgr = get_stream_loop_manager()
        wait_states = getattr(loop_mgr, "_wait_states", None)
        if not isinstance(wait_states, dict):
            logger.debug(
                f"_wait_states 不可访问（type={type(wait_states).__name__}），"
                "改由下一个 tick 自然驱动"
            )
            return False
        removed = wait_states.pop(stream_id, None)
        if removed:
            logger.debug(f"已清除流 {stream_id[:8]} 的等待状态")
            return True
        return False
    except Exception as exc:
        logger.warning(f"清除等待状态失败 stream={stream_id[:8]}: {exc}")
        return False
