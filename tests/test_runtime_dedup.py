"""runtime 重复上下文防护测试。"""

from __future__ import annotations

from neo_fatum_chatter.context.sources.history_source import restore_chain_payloads
from neo_fatum_chatter.domain.session_state import (
    NFCSession,
    strip_persisted_system_reminders,
)
import neo_fatum_chatter.runtime.interrupt_controller as interrupt_controller
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


class _FakeMaybeIdMessage:
    def __init__(self, message_id: str | None, text: str) -> None:
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


def test_filter_interrupt_messages_ignores_messages_without_id() -> None:
    known_ids = frozenset({"m1"})
    old_without_id = _FakeMaybeIdMessage(None, "旧消息")
    new_with_id = _FakeMaybeIdMessage("m2", "新消息")

    assert interrupt_controller.filter_interrupt_messages(
        [old_without_id, new_with_id], known_ids
    ) == [new_with_id]


def test_restore_temporary_payload_removes_extra_context_and_keeps_assistant_tail() -> None:
    response = _FakeResponse([_payload(ROLE.USER, "message")])
    extra = _payload(ROLE.USER, "[附加上下文]\ncontext")
    snapshot = append_temporary_payload(response, extra)
    response.add_payload(_payload(ROLE.ASSISTANT, "reply"))

    restore_temporary_payload(response, snapshot)

    assert _texts(response) == ["message", "reply"]


def test_strip_persisted_runtime_context_keeps_real_message() -> None:
    text = """<system_reminder>
[booku_memory]
旧记忆
</system_reminder>
## 本轮末尾动态补充上下文
send_to 临时上下文
[新消息]
[2026-05-31 12:00:00] 用户说：你好
你发出消息已经过去 5 分钟了，对方还没有回应。
**你发的最后一条消息**：「你好」
<perception_completed>内部状态</perception_completed>"""

    cleaned = strip_persisted_system_reminders(text)

    assert "system_reminder" not in cleaned
    assert "booku_memory" not in cleaned
    assert "本轮末尾动态补充上下文" not in cleaned
    assert "你发出消息已经过去" not in cleaned
    assert "perception_completed" not in cleaned
    assert "用户说：你好" in cleaned


def test_update_chain_sanitizes_persisted_user_text() -> None:
    session = NFCSession(user_id="u1", stream_id="s1")

    session.update_chain(
        [
            {
                "role": "user",
                "text": "<system_reminder>\n[活跃记忆速览]\n旧内容\n</system_reminder>\n[新消息]\n用户说：在吗",
                "ts": 1.0,
            }
        ],
        max_payloads=10,
    )

    stored_text = session.chain_payloads[0]["text"]
    assert "system_reminder" not in stored_text
    assert "活跃记忆速览" not in stored_text
    assert stored_text == "[新消息]\n用户说：在吗"


def test_restore_chain_payloads_sanitizes_polluted_history() -> None:
    payloads = restore_chain_payloads(
        [
            {
                "role": "user",
                "text": "[SYSTEM REMINDER]\n旧规则\n[新消息]\n用户说：回来了吗",
            },
            {"role": "assistant", "text": "回来了"},
        ]
    )

    assert _texts(_FakeResponse(payloads)) == ["[新消息]\n用户说：回来了吗", "回来了"]
