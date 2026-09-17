"""NFC 临时请求视图。

发送视图用于在本轮 LLM 调用中临时加入额外 payload，但不把这些
payload 写回长期 response 链，避免动态上下文污染后续轮次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.kernel.llm import LLMPayload, ROLE
from src.kernel.llm.request import LLMRequest

from ..protocol.reasoning_transport import attach_nfc_model_clients
from ..services.context_sanitizer import repair_post_send_chain


@dataclass(slots=True)
class RequestView:
    """围绕现有 request/response 构造一次性发送视图。"""

    source: Any
    payloads: list[LLMPayload] = field(default_factory=list)
    source_payloads: list[LLMPayload] = field(default_factory=list)
    transient_count: int = 0
    transient_payloads: list[LLMPayload] = field(default_factory=list)

    @property
    def model_set(self) -> Any:
        """透传源 response 的模型集。"""
        return getattr(self.source, "model_set")

    @property
    def context_manager(self) -> Any:
        """透传源 response 的上下文管理器。"""
        return getattr(self.source, "context_manager", None)

    async def send(
        self,
        auto_append_response: bool = True,
        *,
        stream: bool = False,
    ) -> Any:
        """使用视图 payloads 发送请求，返回未消费的 LLMResponse。"""
        upper = getattr(self.source, "_upper", self.source)
        request = LLMRequest(
            getattr(self.source, "model_set"),
            request_name=getattr(upper, "request_name", ""),
            meta_data=dict(getattr(upper, "meta_data", {}) or {}),
            context_manager=getattr(self.source, "context_manager", None),
        )
        attach_nfc_model_clients(request)
        request.payloads = list(self.payloads)
        result = await request.send(auto_append_response=auto_append_response, stream=stream)
        if not getattr(result, "_consumed", False):
            await result
        return result


def strip_transient_payloads(view: RequestView, response: Any) -> Any:
    """从响应链中移除 RequestView 追加的 transient payload。"""
    payloads = getattr(response, "payloads", None)
    if isinstance(payloads, list):
        response.payloads = _without_transient_payloads(
            payloads,
            source_payloads=view.source_payloads,
            transient_payloads=view.transient_payloads,
            transient_count=view.transient_count,
        )
        repair_post_send_chain(response, reason="transient-strip")
    return response


def _without_transient_payloads(
    payloads: list[LLMPayload],
    *,
    source_payloads: list[LLMPayload],
    transient_payloads: list[LLMPayload],
    transient_count: int,
) -> list[LLMPayload]:
    """按对象身份移除 transient；下标切片仅作为旧 provider 兼容回退。"""
    transient_ids = {id(payload) for payload in transient_payloads}
    identity_filtered = [payload for payload in payloads if id(payload) not in transient_ids]
    if len(identity_filtered) != len(payloads):
        persistent_payloads = identity_filtered
    else:
        base_count = len(source_payloads)
        if transient_count > 0 and len(payloads) >= base_count + transient_count:
            persistent_payloads = (
                list(payloads[:base_count])
                + list(payloads[base_count + transient_count:])
            )
        else:
            persistent_payloads = list(payloads)

    # reminder hook 可能重建 USER 对象，因此 USER 允许“同位置同角色”；其余角色
    # 必须保持对象身份作为锚点。只要前缀发生裁剪/位移，就不按下标恢复旧 USER。
    prefix_aligned = len(persistent_payloads) >= len(source_payloads) and all(
        persistent_payloads[index] is source_payload
        or (
            source_payload.role == ROLE.USER
            and persistent_payloads[index].role == ROLE.USER
        )
        for index, source_payload in enumerate(source_payloads)
    )
    if prefix_aligned:
        for index, source_payload in enumerate(source_payloads):
            if source_payload.role == ROLE.USER:
                persistent_payloads[index] = source_payload
    return persistent_payloads


def build_request_view(
    response: Any,
    transient_payloads: list[LLMPayload] | None = None,
) -> RequestView:
    """基于 response 构造带 transient payload 的发送视图。"""
    source_payloads = list(getattr(response, "payloads", []) or [])
    payloads = list(source_payloads)
    transients = list(transient_payloads or [])
    payloads.extend(transients)
    return RequestView(
        source=response,
        payloads=payloads,
        source_payloads=source_payloads,
        transient_count=len(transients),
        transient_payloads=transients,
    )
