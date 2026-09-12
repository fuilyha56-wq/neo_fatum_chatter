"""NFC 信念固化服务。

在每次记忆压缩完成后追加执行：用轻量模型（sub_actor）从同一批
事件文本中蒸馏"对用户 / 对关系的持久判断"，合并进 session.beliefs。

与 compressor 的分工：压缩产出叙事（history_summary，"发生了什么"），
固化产出信念（beliefs，"我因此知道了什么"）。失败静默，绝不影响压缩主流程。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.llm import LLMPayload, ROLE, Text

if TYPE_CHECKING:
    from ..config import NFCConfig
    from ..session import NFCSession, NFCSessionStore

logger = get_logger("NFC_belief")

_BELIEF_SYSTEM_PROMPT = """\
你是一个记忆蒸馏器。输入是角色与某位用户最近的对话时间线（含角色的内心独白），\
以及角色目前已持有的关于这位用户的信念列表。

你的任务：提炼或修正"角色对这位用户与这段关系的持久判断"。

只输出 JSON 数组（可为空数组 []），每个元素：
{"statement": "一句话判断，第一人称视角的已知事实，例如：她最近在准备考研，讨厌这时被打扰",
 "subject": "user" 或 "relationship" 或 "self",
 "confidence": 0.3~0.9,
 "contradicts": ["与该新判断矛盾的旧信念原文，没有则空数组"]}

要求：
1. 只要"过几周仍然成立"的判断，不要流水账事件（事件由别处负责记忆）；
2. 新证据与旧信念冲突时，输出新判断并把旧信念原文放进 contradicts；
3. 每次最多 5 条，宁缺毋滥；没有值得记的就输出 []；
4. 只输出 JSON，不要任何其他文字。"""


async def consolidate_beliefs(
    session: "NFCSession",
    config: "NFCConfig",
    events_text: str,
    session_store: "NFCSessionStore | None" = None,
) -> None:
    """从事件文本蒸馏信念并合并进 session.beliefs。

    Args:
        session: 调用方持有的 session（结果会同步回写）
        config: NFC 配置
        events_text: 已格式化的对话时间线（与压缩同源）
        session_store: 会话存储；提供时在锁内合并最新会话，防止覆盖并发修改
    """
    beliefs_cfg = getattr(config, "beliefs", None)
    if beliefs_cfg is None or not beliefs_cfg.enabled:
        return
    if not events_text or not events_text.strip():
        return

    # 信念蒸馏只看最近一段，控制 token
    trimmed = events_text[-4000:]

    try:
        model_set = get_model_set_by_task(beliefs_cfg.model_task)
    except Exception:
        model_set = None
    if not model_set:
        logger.debug("[信念] 蒸馏模型不可用，跳过")
        return

    existing = "\n".join(
        f"- {b.statement}（subject={b.subject}，confidence={b.confidence:.2f}）"
        for b in session.beliefs.beliefs[:15]
    )
    user_prompt = (
        "【已持有信念】\n" + (existing or "（暂无）") + f"\n\n【最近对话时间线】\n{trimmed}"
    )

    request = create_llm_request(model_set, f"NFC_beliefs_{session.stream_id}")
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_BELIEF_SYSTEM_PROMPT)))
    request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

    try:
        llm_response = await request.send()
        raw_text = (await llm_response or "").strip()
    except Exception as exc:
        logger.debug(f"[信念] 蒸馏调用失败，跳过: {exc}")
        return

    extractions = _parse_belief_extractions(
        raw_text, max_items=int(getattr(beliefs_cfg, "max_extract", 5))
    )
    if not extractions:
        logger.debug("[信念] 本轮无可提炼信念")
        return

    def _apply(book_owner: Any) -> int:
        applied = 0
        for item in extractions:
            for old in item.get("contradicts", []):
                if book_owner.weaken(old):
                    applied += 1
            if book_owner.upsert(
                item["statement"],
                subject=item.get("subject", "user"),
                confidence=item.get("confidence", 0.5),
            ) in {"added", "reinforced"}:
                applied += 1
        book_owner.decay()
        return applied

    if session_store is not None:
        try:
            async with session_store.lock(session.stream_id):
                latest = await session_store.get(session.stream_id)
                target = latest if latest is not None else session
                count = _apply(target.beliefs)
                await session_store.save(target)
                if target is not session:
                    session.beliefs = target.beliefs
        except Exception as exc:
            logger.debug(f"[信念] 锁内合并失败: {exc}")
            return
    else:
        count = _apply(session.beliefs)

    logger.info(f"[信念] 固化完成：应用 {count} 条变更，现存 {len(session.beliefs)} 条信念")


def _parse_belief_extractions(
    raw_text: str,
    *,
    max_items: int = 5,
) -> list[dict[str, Any]]:
    """宽松解析蒸馏输出：JSON 数组或包在正文里的数组。"""
    text = (raw_text or "").strip()
    if not text:
        return []
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)

    start = text.find("[")
    if start < 0:
        # 单对象容错
        start = text.find("{")
        if start >= 0:
            end = text.rfind("}")
            if end > start:
                try:
                    single = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    return []
                if isinstance(single, dict):
                    return [_normalize_extraction(single)][:max_items]
            return []
        return []
    end = text.rfind("]")
    if end <= start:
        return []

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    results: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_extraction(item)
        if normalized:
            results.append(normalized)
        if len(results) >= max_items:
            break
    return results


def _normalize_extraction(item: dict[str, Any]) -> dict[str, Any] | None:
    statement = str(item.get("statement", "") or "").strip()
    if not statement:
        return None
    subject = str(item.get("subject", "user") or "user")
    if subject not in {"user", "relationship", "self"}:
        subject = "user"
    try:
        confidence = max(0.3, min(0.9, float(item.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    contradicts_raw = item.get("contradicts", [])
    contradicts = [
        str(c).strip()
        for c in (contradicts_raw if isinstance(contradicts_raw, list) else [])
        if str(c).strip()
    ][:3]
    return {
        "statement": statement[:200],
        "subject": subject,
        "confidence": confidence,
        "contradicts": contradicts,
    }
