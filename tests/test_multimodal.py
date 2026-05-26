"""multimodal 提取与构建测试。"""

from __future__ import annotations

from neo_fatum_chatter.multimodal import (
    MediaItem,
    _is_valid_base64_payload,
    build_multimodal_content,
    extract_media_from_messages,
)


_VALID_DATA = "base64|" + "A" * 256


class _FakeMsg:
    def __init__(self, *, content=None, extra=None, media=None, message_type=None, message_id="m1"):
        self.content = content
        self.extra = extra or {}
        self.media = media
        self.message_type = message_type
        self.message_id = message_id


def test_is_valid_base64_payload_rejects_short():
    assert _is_valid_base64_payload("base64|short") is False
    assert _is_valid_base64_payload("") is False
    assert _is_valid_base64_payload(None) is False


def test_is_valid_base64_payload_accepts_long():
    assert _is_valid_base64_payload(_VALID_DATA) is True
    assert _is_valid_base64_payload("A" * 200) is True


def test_extract_via_content_dict():
    msg = _FakeMsg(content={"media": [{"type": "image", "data": _VALID_DATA}]})
    items = extract_media_from_messages([msg], max_items=4)
    assert len(items) == 1
    assert items[0].media_type == "image"


def test_extract_via_extra_dict():
    msg = _FakeMsg(extra={"media": [{"type": "emoji", "data": _VALID_DATA}]})
    items = extract_media_from_messages([msg], max_items=4)
    assert items and items[0].media_type == "emoji"


def test_extract_skips_short_data():
    msg = _FakeMsg(content={"media": [{"type": "image", "data": "base64|x"}]})
    assert extract_media_from_messages([msg], max_items=4) == []


def test_extract_skips_unknown_type():
    msg = _FakeMsg(content={"media": [{"type": "video", "data": _VALID_DATA}]})
    assert extract_media_from_messages([msg], max_items=4) == []


def test_extract_respects_max_items():
    media = [{"type": "image", "data": _VALID_DATA} for _ in range(5)]
    msg = _FakeMsg(content={"media": media})
    items = extract_media_from_messages([msg], max_items=3)
    assert len(items) == 3


def test_extract_emoji_string_content():
    long_data = "A" * 256
    msg = _FakeMsg(message_type="emoji", content=long_data)
    items = extract_media_from_messages([msg], max_items=4)
    assert items and items[0].media_type == "emoji"
    assert items[0].base64_data.startswith("base64|")


def test_build_multimodal_content_filters_invalid():
    items = [
        MediaItem(media_type="image", base64_data=_VALID_DATA, source_message_id="m1"),
        MediaItem(media_type="image", base64_data="base64|short", source_message_id="m2"),
    ]
    content = build_multimodal_content("hi", items)
    # 至少包含 Text("hi") 与有效 Image，无效那张被跳过
    assert content[0].__class__.__name__ == "Text"
    image_count = sum(1 for c in content if c.__class__.__name__ == "Image")
    assert image_count == 1


def test_build_multimodal_content_with_no_items():
    content = build_multimodal_content("hello", [])
    assert len(content) == 1
    assert content[0].__class__.__name__ == "Text"
