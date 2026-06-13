"""NFC 感知文本提取服务。

当模型在感知阶段反复输出纯文本而未生成工具调用时，
使用 sub actor 从感知文本中提取可发送的回复内容。
"""

from __future__ import annotations

import json

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text

logger = get_logger("NFC_perception_extractor")

# Sub actor 提取提示词
_EXTRACTION_SYSTEM_PROMPT = """\
你是一个消息提取助手。你的任务是从 AI 角色产生的混合文本中，\
精确提取出"最终要发送给对方的那句话"。

核心判断标准——这段文字是"说给对方听的"还是"自己在脑子里想的"：
- 对事件的观察、描述、评论、吐槽 → 内心活动，不发送
- 对局势的分析、归因、复盘 → 内心活动，不发送
- 表达情绪但是在自言自语（如"他怎么这样啊"） → 内心活动，不发送
- 直接对对方说的话、回应、表态（如"行吧"、"好好好都是我的错"） → 提取

规则：
1. 只提取直接面向对方的、作为对话回复的最终表态部分
2. 宁可少提取，也不要把内心观察/评论混入回复
3. 保留原始措辞、语气、表情符号，一字不改
4. 如果全文都是内心活动没有任何面向对方的回复，reply 返回空字符串
5. 不要添加任何原文没有的内容
6. 如果文本很短且整体就是一句直接回复（无内心活动混杂），原样返回

示例：
输入："他明明自己先挑事的，现在反过来怪我😭 好好好，都是我的错行了吧～"
正确提取：{"reply": "好好好，都是我的错行了吧～", "reason": "前半段是对事件的观察吐槽，后半段是面向对方的回应表态"}

输入："嗯…感觉他今天心情不太好，要不要主动关心一下？算了先等等看"
正确提取：{"reply": "", "reason": "全文都是内心思考和犹豫，没有面向对方的回复"}

输入："晚安呀～明天见"
正确提取：{"reply": "晚安呀～明天见", "reason": "整段就是直接对对方说的话"}

请严格以 JSON 格式返回，不要输出任何其他内容：
{"reply": "提取出的回复内容", "reason": "简短说明你的提取逻辑"}"""


async def extract_reply_from_perception(
    perception_text: str,
    model_task: str = "actor",
) -> str:
    """使用 sub actor 从感知文本中提取可发送的回复内容。

    Args:
        perception_text: 模型感知阶段输出的纯文本
        model_task: 使用的模型任务名称

    Returns:
        提取出的回复文本；提取失败或无有效内容时返回空字符串
    """
    if not perception_text or not perception_text.strip():
        return ""

    model_set = get_model_set_by_task(model_task)
    if not model_set:
        logger.warning("[NFC] perception_extractor: 无法获取 sub actor 模型配置")
        return perception_text

    request = create_llm_request(model_set, "NFC_perception_extract")
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_EXTRACTION_SYSTEM_PROMPT)))
    request.add_payload(LLMPayload(
        ROLE.USER,
        Text(f"以下是需要提取的感知文本：\n\n{perception_text}"),
    ))

    try:
        llm_response = await request.send()
        raw_result = (await llm_response or "").strip()
    except Exception as exc:
        logger.warning(f"[NFC] perception_extractor: LLM 调用失败: {exc}")
        return perception_text

    if not raw_result:
        logger.debug("[NFC] perception_extractor: LLM 返回空结果")
        return ""

    return _parse_extraction_result(raw_result, perception_text)


def _parse_extraction_result(raw_result: str, fallback: str) -> str:
    """解析 sub actor 的 JSON 响应。

    Args:
        raw_result: LLM 返回的原始文本
        fallback: 解析失败时的兜底文本

    Returns:
        提取出的回复文本
    """
    try:
        json_start = raw_result.find("{")
        json_end = raw_result.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            json_str = raw_result[json_start:json_end]
            parsed = json.loads(json_str)
            reply = parsed.get("reply", "").strip()
            reason = parsed.get("reason", "")
            if reason:
                logger.debug(f"[NFC] perception_extractor 提取原因: {reason}")
            return reply
        else:
            logger.debug("[NFC] perception_extractor: 响应中未找到 JSON")
            return fallback
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        logger.debug(f"[NFC] perception_extractor: JSON 解析失败: {exc}")
        return fallback
