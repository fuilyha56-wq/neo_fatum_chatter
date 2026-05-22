"""提示词模块。

提供 NFC 专用的提示词模板、模块函数和构建器。
"""

from __future__ import annotations

from .templates import (
    NFC_SYSTEM_PROMPT,
    NFC_PROACTIVE_PROMPT,
    NFC_TIMEOUT_PROMPT,
)

__all__ = [
    "NFC_SYSTEM_PROMPT",
    "NFC_PROACTIVE_PROMPT",
    "NFC_TIMEOUT_PROMPT",
    "NFCPromptBuilder",
]


def __getattr__(name: str) -> object:
    """惰性导出 builder，避免 context/planner 导入 templates 时触发循环导入。"""
    if name == "NFCPromptBuilder":
        from .builder import NFCPromptBuilder

        return NFCPromptBuilder
    raise AttributeError(name)
