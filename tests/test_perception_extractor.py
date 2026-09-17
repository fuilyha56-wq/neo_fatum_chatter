"""感知文本提取的安全回归测试。"""

from __future__ import annotations

import pytest

import neo_fatum_chatter.services.perception_extractor as extractor


@pytest.mark.asyncio
async def test_extractor_does_not_leak_raw_perception_when_model_missing(monkeypatch) -> None:
    monkeypatch.setattr(extractor, "get_model_set_by_task", lambda task: None)
    assert await extractor.extract_reply_from_perception("内心分析：先观察一下") == ""


def test_invalid_json_never_falls_back_to_thought_text() -> None:
    assert extractor._parse_extraction_result("不是 JSON", "内心分析原文") == ""


def test_valid_json_returns_only_reply() -> None:
    result = extractor._parse_extraction_result(
        '{"reply":"直接给对方的话","reason":"只保留表态"}',
        "混合感知原文",
    )
    assert result == "直接给对方的话"
