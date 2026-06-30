"""NFC 协议层导出。"""

from .compat_adapter import (
    build_tool_call_compat_retry_prompt,
    is_deepseek_model_set,
    prepare_nfc_model_set,
    rewrite_response_as_unsent_draft,
    try_parse_tool_call_compat_response,
)
from .response_normalizer import NormalizedResponse, normalize_response, resolve_response_text

__all__ = [
    "NormalizedResponse",
    "build_decision",
    "build_tool_call_compat_retry_prompt",
    "is_deepseek_model_set",
    "normalize_response",
    "parse_response_decision",
    "prepare_nfc_model_set",
    "resolve_response_text",
    "rewrite_response_as_unsent_draft",
    "try_parse_tool_call_compat_response",
]


def __getattr__(name: str):
    """延迟导入决策解析器，避免 parser 与 protocol 包初始化循环导入。"""
    if name in {"build_decision", "parse_response_decision"}:
        from .decision_parser import build_decision, parse_response_decision

        return {
            "build_decision": build_decision,
            "parse_response_decision": parse_response_decision,
        }[name]
    raise AttributeError(name)
