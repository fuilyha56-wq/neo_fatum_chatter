"""NFC 工具调用名规整器。

职责：把模型返回的工具调用名（可能带 ``action-`` / ``tool-`` / ``agent-`` 前缀
或 ``plugin:type:name`` 形式）映射回 ``ToolRegistry`` 里实际注册的工具名。
原本散在 ``parser.py`` 和 ``protocol/decision_parser.py`` 中，抽出来便于单元测试。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.kernel.llm import ToolCall

if TYPE_CHECKING:
    from src.kernel.llm import ToolRegistry


def normalize_call_name(name: str) -> str:
    """归一化工具调用名称，兼容带前缀/带插件名前缀的格式。

    规则：
        - ``"plugin:action:nfc_reply"`` → ``"nfc_reply"``
        - ``"action-nfc_reply"`` / ``"tool-foo"`` / ``"agent-bar"`` → 去前缀
        - 其它原样返回
    """
    if not name:
        return ""

    if ":" in name:
        return name.rsplit(":", 1)[-1]

    for prefix in ("action-", "tool-", "agent-"):
        if name.startswith(prefix):
            return name[len(prefix):]

    return name


def registered_tool_names(usable_map: "ToolRegistry") -> set[str]:
    """从注册表提取当前可执行工具名。"""
    get_all_names = getattr(usable_map, "get_all_names", None)
    if callable(get_all_names):
        try:
            return {str(name) for name in get_all_names()}
        except Exception:
            return set()
    return set()


def resolve_registered_call_name(name: str, usable_map: "ToolRegistry") -> str:
    """将模型返回的短名解析为注册表中的实际工具名。

    若注册表里已存在精确匹配则原样返回；否则尝试 ``action-/tool-/agent-`` 三种
    候选；仍未命中且“同名规整后唯一对应”一个注册项时取那个，否则返回原名。
    """
    registered_names = registered_tool_names(usable_map)
    if not name or not registered_names or name in registered_names:
        return name

    normalized_name = normalize_call_name(name)
    candidates = [
        f"action-{normalized_name}",
        f"tool-{normalized_name}",
        f"agent-{normalized_name}",
    ]
    for candidate in candidates:
        if candidate in registered_names:
            return candidate

    matches = [
        registered_name
        for registered_name in registered_names
        if normalize_call_name(registered_name) == normalized_name
    ]
    return sorted(matches)[0] if len(matches) == 1 else name


def retarget_call_name(call: ToolCall, name: str) -> ToolCall:
    """保持 call id/args 不变，仅替换执行用工具名。"""
    if call.name == name:
        return call
    return ToolCall(id=call.id, name=name, args=call.args)
