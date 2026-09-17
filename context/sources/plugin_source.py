"""NFC 第三方上下文贡献接入点。"""

from __future__ import annotations

from typing import Any, cast, get_args

from src.app.plugin_system.api.log_api import get_logger

from ..types import ContextContribution, ContextOwner, ContextScope


logger = get_logger("NFC_context_plugin_source")

_VALID_CONTEXT_OWNERS = frozenset(get_args(ContextOwner))
_SUPPORTED_CONTEXT_SCOPES = frozenset({"turn", "session"})


def _normalize_context_contribution(raw: Any) -> ContextContribution | None:
    """将第三方返回值归一化为 ContextContribution。"""
    if isinstance(raw, ContextContribution):
        if raw.scope in _SUPPORTED_CONTEXT_SCOPES:
            return raw
        # persistent 目前没有持久化执行器，显式降级为本轮贡献，
        # 避免第三方误以为状态已经写入 NFC session。
        return ContextContribution(
            source=raw.source,
            owner=raw.owner,
            scope="turn",
            priority=raw.priority,
            ttl_turns=raw.ttl_turns,
            content=raw.content,
            evidence_only=raw.evidence_only,
        )
    if not isinstance(raw, dict):
        return None

    try:
        content = str(raw.get("content", "") or "").strip()
        if not content:
            return None

        owner = str(raw.get("owner", "notice") or "notice")
        scope = str(raw.get("scope", "turn") or "turn")
        normalized_owner = owner if owner in _VALID_CONTEXT_OWNERS else "notice"
        normalized_scope = scope if scope in _SUPPORTED_CONTEXT_SCOPES else "turn"

        return ContextContribution(
            source=str(raw.get("source", "plugin.on_prompt_build") or "plugin.on_prompt_build"),
            owner=cast(ContextOwner, normalized_owner),
            scope=cast(ContextScope, normalized_scope),
            priority=int(raw.get("priority", 0) or 0),
            ttl_turns=(
                int(raw["ttl_turns"])
                if raw.get("ttl_turns") is not None
                else None
            ),
            content=content,
            evidence_only=bool(raw.get("evidence_only", False)),
        )
    except Exception:
        return None


async def collect_plugin_turn_contributions(
    *,
    prompt_name: str,
    content: str,
    stream_id: str = "",
) -> list[ContextContribution]:
    """收集第三方在本轮提交的上下文贡献。

    兼容期内继续监听 on_prompt_build，但会把 legacy extra 文本
    立即归一化成 notice/turn 的 ContextContribution，避免主流程继续
    直接拼接 raw extra user payload。
    """
    try:
        from src.kernel.event import get_event_bus

        event_bus = get_event_bus()
        subscribers = event_bus.get_subscribers("on_prompt_build")
        if not subscribers:
            logger.debug(
                f"on_prompt_build 无订阅者: prompt={prompt_name}, "
                f"stream_id_present={bool(str(stream_id or '').strip())}"
            )
            return []

        template = "{content}\n{extra}"
        values: dict[str, Any] = {
            "content": content,
            "extra": "",
            "stream_id": stream_id,
        }
        event_params: dict[str, Any] = {
            # name 是当前框架规范字段；prompt_name 是早期注入器使用的别名。
            # 两者都在初始参数中预置，避免 EventBus 的 key 签名校验丢弃 legacy handler。
            "name": prompt_name,
            "prompt_name": prompt_name,
            "template": template,
            "values": values,
            "policies": {},
            "strict": False,
            "context_contributions": [],
        }
        _, final_params = await event_bus.publish(
            "on_prompt_build",
            event_params,
        )

        if not isinstance(final_params, dict):
            logger.warning("on_prompt_build 返回参数不是 dict，忽略本轮注入")
            return []

        contributions: list[ContextContribution] = []
        raw_contributions = final_params.get("context_contributions", [])
        if not isinstance(raw_contributions, list):
            logger.debug("on_prompt_build context_contributions 不是 list，忽略该通道")
            raw_contributions = []
        for raw in raw_contributions:
            normalized = _normalize_context_contribution(raw)
            if normalized is not None:
                contributions.append(normalized)

        rendered_values = dict(final_params.get("values", values))
        legacy_extra = str(rendered_values.get("extra", "") or "").strip()
        if legacy_extra:
            contributions.append(
                ContextContribution(
                    source="legacy.on_prompt_build.extra",
                    owner="notice",
                    scope="turn",
                    priority=0,
                    ttl_turns=1,
                    content=legacy_extra,
                )
            )

        logger.debug(
            f"on_prompt_build 已收集贡献: prompt={prompt_name}, "
            f"subscribers={len(subscribers)}, contributions={len(contributions)}, "
            f"stream_id_present={bool(str(stream_id or '').strip())}"
        )
        return contributions
    except Exception as exc:
        logger.warning(f"on_prompt_build 注入失败，将忽略额外上下文: {exc}")
        return []