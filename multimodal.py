"""NFC 多模态辅助模块。

高内聚：所有图片提取、格式转换逻辑集中在此。
低耦合：仅依赖 Message 对象的公开属性和 kernel.llm 的 Content 类型。

健壮性约定：
    - 提取阶段对脏 media（空 data、类型异常、长度异常短）静默跳过并记录 debug 日志。
    - 构建阶段单张图片转换失败不影响其它图片，整体失败时返回纯文本 fallback。
    - 这是“发现脏了就修”而不是“在协议层把脏数据当合法数据丢给 provider”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import Content, Image, Text

if TYPE_CHECKING:
    from src.core.models.message import Message


logger = get_logger("NFC_multimodal")

# base64 图片数据的最小可信长度（< 此值视为脏数据）。
# 1x1 透明 PNG 的 base64 约 96 字节，常见缩略图 > 500 字节，留较宽容差。
_MIN_BASE64_BYTES = 64

# 受支持的媒体类型集合，避免因第三方扩展插入非图像类型导致 provider 拒绝。
_SUPPORTED_MEDIA_TYPES = frozenset({"image", "emoji"})


def _is_valid_base64_payload(data: Any) -> bool:
    """校验 base64 数据是否值得继续打包到 LLM payload。

    这一层是“老插件式”的运行时自愈：宁可丢一张图，也别把脏数据塞进 provider。
    """
    if not isinstance(data, str):
        return False
    stripped = data.strip()
    if not stripped:
        return False
    payload = stripped[len("base64|"):] if stripped.startswith("base64|") else stripped
    return len(payload) >= _MIN_BASE64_BYTES


@dataclass
class MediaItem:
    """从消息中提取的媒体条目。"""

    media_type: str  # "image" | "emoji"
    base64_data: str  # 原始 base64 数据（"base64|..." 格式，来自 normalize_base64）
    source_message_id: str  # 来源消息 ID


class ImageBudget:
    """跨 payload 的图片预算追踪器。

    在 execute() 初始化阶段创建一个实例，
    历史图片和当前循环中的图片共享同一预算，
    避免双重注入导致图片总量超出限制。
    """

    def __init__(self, total_max: int = 4) -> None:
        self._total_max = total_max
        self._used = 0

    @property
    def remaining(self) -> int:
        """剩余可用图片配额。"""
        return max(0, self._total_max - self._used)

    def consume(self, count: int) -> None:
        """消耗图片配额。"""
        self._used += count

    def is_exhausted(self) -> bool:
        """配额是否已用尽。"""
        return self._used >= self._total_max

    def reset(self) -> None:
        """重置预算（新一轮对话循环）。"""
        self._used = 0


def extract_media_from_messages(
    messages: list[Message],
    max_items: int = 4,
) -> list[MediaItem]:
    """从未读消息列表中提取图片 base64 数据。

    只提取当前轮的未读消息中的 media。历史消息中的图片
    已经在 LLMResponse 链的上下文中，不需要重复提取。

    Args:
        messages: 当前轮的未读消息列表
        max_items: 最大提取数量

    Returns:
        提取到的 MediaItem 列表（按消息顺序，截断至 max_items）。
        遇到脏 media（空 data、类型异常、长度过短）会静默跳过。
    """
    items: list[MediaItem] = []
    skipped_dirty = 0

    for msg in messages:
        if len(items) >= max_items:
            break

        media_list = _get_media_list(msg)
        if not media_list:
            continue

        msg_id = getattr(msg, "message_id", "")

        for media in media_list:
            if len(items) >= max_items:
                break
            if not isinstance(media, dict):
                skipped_dirty += 1
                continue
            media_type = media.get("type")
            if media_type not in _SUPPORTED_MEDIA_TYPES:
                continue
            data = media.get("data", "")
            if not _is_valid_base64_payload(data):
                skipped_dirty += 1
                continue

            items.append(
                MediaItem(
                    media_type=media_type,
                    base64_data=data,
                    source_message_id=msg_id,
                )
            )

    if skipped_dirty:
        logger.debug(
            f"多模态: 提取阶段跳过 {skipped_dirty} 条脏 media（空 data 或长度异常）"
        )
    return items


def build_multimodal_content(
    text: str,
    media_items: list[MediaItem],
) -> list[Content | Any]:
    """构建混合 Text + Image 的 content 列表，用于 LLMPayload。

    Args:
        text: 文本内容
        media_items: 媒体条目列表

    Returns:
        ``[Text(text), Image(data1), Image(data2), ...]`` 格式的 content 列表。
        若所有媒体都构建失败，至少返回 ``[Text(text)]``，保证文本部分不丢。

    Note:
        MediaItem.base64_data 已经是 ``"base64|..."`` 格式
        （来自 converter 的 ``normalize_base64``）。
        框架的 ``openai_client._image_to_data_url`` 会自动将其
        转换为 ``"data:image/png;base64,..."`` 格式。
        单张图片构建失败（例如 base64 字段被截断）会被记录并跳过，
        而不是中断整轮 LLM 请求。
    """
    content_list: list[Content] = [Text(text)]
    failed = 0
    for item in media_items:
        try:
            if not _is_valid_base64_payload(item.base64_data):
                failed += 1
                continue
            # 表情包类型添加标注，帮助模型区分贴纸/表情包与普通照片
            if item.media_type == "emoji":
                content_list.append(Text("[表情包]"))
            content_list.append(Image(item.base64_data))
        except Exception as exc:
            failed += 1
            logger.debug(
                f"多模态: 单张图片构建失败已跳过 (msg={item.source_message_id}): {exc}"
            )

    if failed:
        logger.debug(
            f"多模态: build_multimodal_content 跳过 {failed}/{len(media_items)} 张异常图片"
        )
    return content_list


# ──────────────────────────────────────────
# 内部辅助函数
# ──────────────────────────────────────────


def _get_media_list(msg: Message) -> list[dict[str, Any]]:
    """从 Message 中提取 media 列表。

    按优先级尝试以下路径获取媒体数据：
    1. content dict（当前会话内存消息，含完整 base64）
    2. extra dict
    3. message.media 直接属性
    4. EMOJI 类型原始 content 字符串

    Args:
        msg: 消息对象

    Returns:
        媒体字典列表，每项为 ``{"type": str, "data": str}``
    """
    content = getattr(msg, "content", None)

    # 路径 1: content 是 dict（当前会话内存中的消息，含完整 base64）
    if isinstance(content, dict):
        media = content.get("media")
        if isinstance(media, list) and media:
            # 仅在含有 data 的情况下从这里返回，避免返回被 stream_manager 剥离后的空壳
            if any(item.get("data") for item in media if isinstance(item, dict)):
                return media

    # 路径 2: extra 中的 media（converter 构造时通过 **extra 传入）
    extra = getattr(msg, "extra", {})
    if isinstance(extra, dict):
        media = extra.get("media")
        if isinstance(media, list) and media:
            return media

    # 路径 3: 直接属性 message.media（当前会话内存，含完整 base64）
    media = getattr(msg, "media", None)
    if isinstance(media, list) and media:
        return media

    # 路径 4: EMOJI 类型消息的原始 content（base64 字符串）
    # Bot 发送的表情包通过 send_api 构建，content 是原始 base64 数据
    msg_type = getattr(msg, "message_type", None)
    if (
        msg_type is not None
        and str(msg_type) == "emoji"
        and isinstance(content, str)
        and len(content) > 100  # base64 图片数据通常远大于 100 字符
    ):
        # 统一为 "base64|..." 格式
        data = content if content.startswith("base64|") else f"base64|{content}"
        return [{"type": "emoji", "data": data}]

    return []


# 公开别名，供外部模块（如 chatter.py）直接调用
get_media_list = _get_media_list
