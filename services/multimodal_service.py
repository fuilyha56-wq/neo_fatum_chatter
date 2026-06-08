"""NFC 多模态服务。"""

from __future__ import annotations

from typing import Any

from src.kernel.llm import LLMPayload, ROLE

from ..multimodal import MediaItem, build_multimodal_content


_PINNED_ROLES = {ROLE.SYSTEM, ROLE.TOOL}


class MultimodalService:
    """处理运行时多模态上下文拼装。"""

    @staticmethod
    def append_history_reference(response: Any, history_images: list[MediaItem]) -> None:
        """将历史图片参考插入到 response 链的 pinned 前缀末尾。"""
        if not history_images:
            return

        payload = LLMPayload(
            ROLE.SYSTEM,
            build_multimodal_content("[历史图片参考]", history_images),
        )
        payloads = getattr(response, "payloads", None)
        if isinstance(payloads, list):
            insert_at = 0
            for idx, existing in enumerate(payloads):
                if getattr(existing, "role", None) in _PINNED_ROLES:
                    insert_at = idx + 1
                else:
                    break
            response.add_payload(payload, position=insert_at)
            return

        response.add_payload(payload)
