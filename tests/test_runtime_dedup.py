"""runtime 重复上下文防护测试。"""

from __future__ import annotations

from neo_fatum_chatter.runtime.message_buffer import dedupe_messages_by_id
from neo_fatum_chatter.runtime.orchestrator import (
    append_temporary_payload,
    restore_temporary_payload,
)
from neo_fatum_chatter.runtime.turn_controller import filter_messages_already_in_payloads
from src.kernel.llm import LLMPayload, ROLE, Text


class _FakeMessage:
    def __init__(self, message_id: str, text: str) -> None:
        self.message_id = message_id
        self.processed_plain_text = text


class _FakeResponse:
    def __init__(self, payloads: list[LLMPayload]) -> None:
        self.payloads = list(payloads)

    def add_payload(self, payload: LLMPayload, position=None) -> None:
        if position is None:
            self.payloads.append(payload)
        else:
            self.payloads.insert(position, payload)


def _payload(role: ROLE, text: str) -> LLMPayload:
    return LLMPayload(role, Text(text))


def _texts(response: _FakeResponse) -> list[str]:
    texts: list[str] = []
    for payload in response.payloads:
        content = payload.content
        if isinstance(content, list):
            texts.extend(item.text for item in content if isinstance(item, Text))
        elif isinstance(content, Text):
            texts.append(content.text)
    return texts


def test_dedupe_messages_by_id_keeps_first_duplicate_message() -> None:
    first = _FakeMessage("m1", "first")
    duplicate = _FakeMessage("m1", "duplicate")
    second = _FakeMessage("m2", "second")

    assert dedupe_messages_by_id([first, duplicate, second]) == [first, second]


def test_filter_messages_already_in_payloads_skips_replayed_message_id() -> None:
    response = _FakeResponse([_payload(ROLE.USER, "[消息id:m1] old")])
    replayed = _FakeMessage("m1", "old")
    fresh = _FakeMessage("m2", "new")

    assert filter_messages_already_in_payloads(response, [replayed, fresh]) == [fresh]


def test_restore_temporary_payload_removes_extra_context_and_keeps_assistant_tail() -> None:
    response = _FakeResponse([_payload(ROLE.USER, "message")])
    extra = _payload(ROLE.USER, "[附加上下文]\ncontext")
    snapshot = append_temporary_payload(response, extra)
    response.add_payload(_payload(ROLE.ASSISTANT, "reply"))

    restore_temporary_payload(response, snapshot)

    assert _texts(response) == ["message", "reply"]
