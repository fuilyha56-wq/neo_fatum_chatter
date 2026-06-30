"""NFC 响应标准化。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .compat_adapter import is_deepseek_model_set, try_parse_tool_call_compat_response


# ── thinking 标签泄漏清洗 ──────────────────────────────────
# 部分 provider（典型如 DeepSeek V4 Pro Thinking）有时会把 reasoning
# 块以 <think>/<thinking> 标签的形式直接吐到正文 message 里，没有走
# reasoning_content 字段。若不剥离，兜底直发逻辑会把整段思考过程
# 当成正常回复发给用户，体验非常糟糕。

# 配对 thinking 块
_PAIRED_THINK_RE = re.compile(
    r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?\s*>",
    re.DOTALL | re.IGNORECASE,
)
# 孤立的结束标签（DeepSeek 风格：只输出 </think> 不输出 <think>）
_ORPHAN_END_THINK_RE = re.compile(
    r"\A.*?</think(?:ing)?\s*>",
    re.DOTALL | re.IGNORECASE,
)
# 孤立的开始标签（思考块被截断未闭合）
_ORPHAN_START_THINK_RE = re.compile(
    r"<think(?:ing)?\b[^>]*>.*\Z",
    re.DOTALL | re.IGNORECASE,
)
_HAS_END_THINK_RE = re.compile(r"</think(?:ing)?\s*>", re.IGNORECASE)
_HAS_START_THINK_RE = re.compile(r"<think(?:ing)?\b[^>]*>", re.IGNORECASE)


def strip_thinking_blocks(text: str | None) -> str:
    """剥离正文中混入的 thinking 块。

    覆盖三种泄漏场景：
        1. ``<think>...</think>`` / ``<thinking>...</thinking>`` 配对：整段移除。
        2. 仅有结束标签（DeepSeek 不输出开始标签）：移除起始至 ``</think>`` 的内容。
        3. 仅有开始标签（响应被截断）：移除 ``<think>`` 起至末尾的内容。

    Args:
        text: 原始响应正文。

    Returns:
        str: 剥离后的文本（首尾去空白）。
    """
    if not isinstance(text, str) or not text:
        return text or ""

    cleaned = _PAIRED_THINK_RE.sub("", text)

    if _HAS_END_THINK_RE.search(cleaned) and not _HAS_START_THINK_RE.search(cleaned):
        cleaned = _ORPHAN_END_THINK_RE.sub("", cleaned)

    if _HAS_START_THINK_RE.search(cleaned) and not _HAS_END_THINK_RE.search(cleaned):
        cleaned = _ORPHAN_START_THINK_RE.sub("", cleaned)

    return cleaned.strip()


@dataclass(slots=True)
class NormalizedResponse:
    """标准化后的响应视图。"""

    response: Any
    text: str
    used_reasoning_content: bool = False
    used_compat_tool_calls: bool = False
    stripped_thinking_block: bool = False

    @property
    def has_tool_calls(self) -> bool:
        """是否已经形成标准工具调用。"""
        return bool(getattr(self.response, "call_list", None))


def _clean_message_text(message: Any) -> tuple[str, bool]:
    """清洗 message 正文中的 thinking 泄漏，返回 (清洗后文本, 是否做了剥离)。"""
    if not isinstance(message, str):
        return "", False
    stripped_input = message.strip()
    if not stripped_input:
        return "", False
    cleaned = strip_thinking_blocks(stripped_input)
    return cleaned, cleaned != stripped_input


def resolve_response_text(response: Any) -> tuple[str, bool]:
    """统一提取响应正文；正文为空时回退到 reasoning_content。

    会就地剥离 message 中混入的 ``<think>``/``<thinking>`` 块，并把
    清洗结果写回 ``response.message``，确保下游（兜底直发、JSON 解析等）
    全部拿到干净文本。
    """
    message = getattr(response, "message", None)
    cleaned_message, stripped = _clean_message_text(message)
    if stripped:
        try:
            response.message = cleaned_message
        except Exception:
            pass

    if cleaned_message:
        return cleaned_message, False

    reasoning_text = getattr(response, "reasoning_content", None)
    if isinstance(reasoning_text, str) and reasoning_text.strip():
        return reasoning_text.strip(), True

    if isinstance(message, str):
        return cleaned_message, False

    return "", False


def normalize_response(response: Any) -> NormalizedResponse:
    """将 provider 原始响应标准化为 NFC 统一视图。"""
    original_message = getattr(response, "message", None)
    original_message_str = original_message if isinstance(original_message, str) else ""

    resolved_text, used_reasoning = resolve_response_text(response)
    cleaned_message_str = getattr(response, "message", None) or ""
    stripped_thinking_block = (
        isinstance(cleaned_message_str, str)
        and original_message_str.strip() != cleaned_message_str.strip()
    )

    if used_reasoning and not (cleaned_message_str or "").strip() and not getattr(response, "call_list", None):
        response.message = resolved_text

    used_compat_tool_calls = False
    if not getattr(response, "call_list", None) and is_deepseek_model_set(getattr(response, "model_set", None)):
        used_compat_tool_calls = try_parse_tool_call_compat_response(response)

    normalized_text, _ = resolve_response_text(response)
    return NormalizedResponse(
        response=response,
        text=normalized_text,
        used_reasoning_content=used_reasoning,
        used_compat_tool_calls=used_compat_tool_calls,
        stripped_thinking_block=stripped_thinking_block,
    )