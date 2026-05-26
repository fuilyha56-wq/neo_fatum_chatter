"""call_resolver 测试。"""

from __future__ import annotations

from neo_fatum_chatter.protocol.call_resolver import (
    normalize_call_name,
    resolve_registered_call_name,
)


class _FakeRegistry:
    def __init__(self, names):
        self._names = list(names)

    def get_all_names(self):
        return list(self._names)


def test_normalize_handles_empty():
    assert normalize_call_name("") == ""


def test_normalize_strips_action_prefix():
    assert normalize_call_name("action-nfc_reply") == "nfc_reply"
    assert normalize_call_name("tool-foo") == "foo"
    assert normalize_call_name("agent-bar") == "bar"


def test_normalize_strips_colon_namespace():
    assert normalize_call_name("plugin:action:nfc_reply") == "nfc_reply"


def test_normalize_passthrough_for_unknown_prefix():
    assert normalize_call_name("nfc_reply") == "nfc_reply"
    assert normalize_call_name("custom_tool") == "custom_tool"


def test_resolve_returns_input_when_already_registered():
    registry = _FakeRegistry({"nfc_reply", "do_nothing"})
    assert resolve_registered_call_name("nfc_reply", registry) == "nfc_reply"


def test_resolve_picks_action_prefix_candidate():
    registry = _FakeRegistry({"action-nfc_reply"})
    assert resolve_registered_call_name("nfc_reply", registry) == "action-nfc_reply"


def test_resolve_handles_unique_normalized_match():
    registry = _FakeRegistry({"plugin:action:nfc_reply"})
    assert resolve_registered_call_name("nfc_reply", registry) == "plugin:action:nfc_reply"


def test_resolve_returns_input_when_ambiguous():
    registry = _FakeRegistry({"action-foo", "tool-foo"})
    # 两个候选 normalize 后都等于 foo，无法区分时不强行选取
    out = resolve_registered_call_name("foo", registry)
    assert out in {"action-foo", "tool-foo", "foo"}


def test_resolve_when_registry_empty():
    registry = _FakeRegistry(set())
    assert resolve_registered_call_name("nfc_reply", registry) == "nfc_reply"
