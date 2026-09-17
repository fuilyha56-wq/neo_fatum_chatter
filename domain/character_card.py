"""NFC 角色卡（三层人设 + 剧情覆层）。

人格不崩的关键是把人设从"一整块平铺文本"拆成三层：
    - 公开层：名字/性格/说话习惯——由宿主人格配置承担（不在此重复）；
    - 隐藏层：角色的秘密、过去、真实想法——**平时不进 prompt**，
      满足揭示条件（S1 判定）后才注入。没有这层，模型全知，
      角色不会欲言又止、没有可揭示的东西；
    - 红线层：这个角色绝不会做的事——防 OOC 崩坏的行为闸门
      （由 prompts/modules.py 只读合并进宿主 safety_guidelines /
      negative_behaviors，随系统提示词渲染，本模块不再单独输出）。

剧情覆层（overlay）是套在基础角色外的临时戏服：进入剧情时由 S1 或
用户指定，退出剧情时摘除——演角色时她听起来还是她。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_MAX_HIDDEN_FACTS = 12


@dataclass
class HiddenFact:
    """一条隐藏事实（角色的秘密）。"""

    id: str
    fact: str
    condition: str = ""  # 揭示条件（自然语言，供 S1 判断）
    revealed: bool = False


@dataclass
class CharacterCard:
    """角色卡运行时状态。

    定义来自配置（``[character]``），揭示状态与覆层随 session 持久化，
    加载时用 :meth:`merge_state` 回填。
    """

    redlines: list[str] = field(default_factory=list)
    hidden_facts: list[HiddenFact] = field(default_factory=list)
    overlay_name: str = ""
    overlay_persona: str = ""

    # ── 构建 ─────────────────────────────────────────────

    @classmethod
    def from_config(cls, config_section: Any | None) -> CharacterCard:
        """从配置段构建角色卡定义。"""
        card = cls()
        if config_section is None:
            return card

        raw_redlines = getattr(config_section, "redlines", None) or []
        if isinstance(raw_redlines, str):
            raw_redlines = [line for line in raw_redlines.splitlines() if line.strip()]
        card.redlines = [str(r).strip() for r in raw_redlines if str(r).strip()]

        raw_facts = getattr(config_section, "hidden_facts", None) or []
        if isinstance(raw_facts, str):
            # 宽松格式：每行 "事实 | 揭示条件"
            entries: list[Any] = []
            for line in raw_facts.splitlines():
                if not line.strip():
                    continue
                fact, _, condition = line.partition("|")
                entries.append({"fact": fact.strip(), "condition": condition.strip()})
            raw_facts = entries
        for index, item in enumerate(raw_facts):
            if isinstance(item, str):
                # 宽松格式：整段字符串时支持 "事实 | 揭示条件"
                fact_text, _, condition = item.partition("|")
                fact_text, condition = fact_text.strip(), condition.strip()
            elif isinstance(item, dict):
                fact_text = str(item.get("fact", "") or item.get("内容", "") or "")
                condition = str(item.get("condition", "") or item.get("条件", "") or "")
            else:
                continue
            fact_text = fact_text.strip()
            if not fact_text:
                continue
            card.hidden_facts.append(
                HiddenFact(
                    id=f"hf{index}",
                    fact=fact_text,
                    condition=condition.strip(),
                )
            )
        card.hidden_facts = card.hidden_facts[:_MAX_HIDDEN_FACTS]
        return card

    # ── 状态合并 ─────────────────────────────────────────

    def merge_state(self, state: dict | None) -> None:
        """把 session 持久化的揭示状态与覆层合并进卡。"""
        if not isinstance(state, dict):
            return
        revealed_ids = set(
            str(x) for x in (state.get("revealed_fact_ids", []) or []) if x
        )
        for fact in self.hidden_facts:
            if fact.id in revealed_ids:
                fact.revealed = True
        overlay = state.get("active_overlay")
        if isinstance(overlay, dict):
            self.overlay_name = str(overlay.get("name", "") or "")
            self.overlay_persona = str(overlay.get("persona", "") or "")

    def export_state(self) -> dict:
        return {
            "revealed_fact_ids": [f.id for f in self.hidden_facts if f.revealed],
            "active_overlay": (
                {"name": self.overlay_name, "persona": self.overlay_persona}
                if self.overlay_name or self.overlay_persona
                else None
            ),
        }

    # ── 操作 ─────────────────────────────────────────────

    def reveal(self, fact_ids: list[str]) -> list[HiddenFact]:
        """揭示一批隐藏事实，返回本次新揭示的条目。"""
        newly: list[HiddenFact] = []
        wanted = {str(x) for x in fact_ids if str(x).strip()}
        for fact in self.hidden_facts:
            if fact.id in wanted and not fact.revealed:
                fact.revealed = True
                newly.append(fact)
        return newly

    def set_overlay(self, name: str, persona: str) -> None:
        self.overlay_name = (name or "").strip()
        self.overlay_persona = (persona or "").strip()

    def clear_overlay(self) -> None:
        self.overlay_name = ""
        self.overlay_persona = ""

    # ── 渲染 ─────────────────────────────────────────────

    def render_prompt(self) -> str:
        """渲染角色卡状态块；全空时返回空串（保护 prefix cache）。"""
        sections: list[str] = []

        if self.overlay_name or self.overlay_persona:
            lines = ["# 当前剧情角色（覆层）"]
            if self.overlay_name:
                lines.append(f"- 你在这段故事里的角色：{self.overlay_name}")
            if self.overlay_persona:
                lines.append(f"- 角色设定：{self.overlay_persona}")
            lines.append(
                "- 这只是暂时扮演的戏服：你底层的人格、说话习惯、和 Ta 的真实关系"
                "仍然生效，会自然地从角色里漏出来。"
            )
            sections.append("\n".join(lines))

        revealed = [f for f in self.hidden_facts if f.revealed]
        if revealed:
            lines = ["# 你藏了一阵子的事（现在可以自然说出口了）"]
            for fact in revealed[-5:]:
                lines.append(f"- {fact.fact}")
            sections.append("\n".join(lines))

        unrevealed = [f for f in self.hidden_facts if not f.revealed]
        if unrevealed:
            lines = ["# 你还没说出口的事"]
            lines.append(
                "- 你心里还压着一些没告诉 Ta 的事。**不要主动说出**，"
                "除非 Ta 问到点子上、或时机真的成熟——欲言又止也是真实的反应。"
            )
            sections.append("\n".join(lines))

        # 红线（redlines）不再在此渲染：由 prompts/modules.py 合并进
        # 宿主 safety_guidelines / negative_behaviors，随系统提示词生效。

        return "\n".join(sections)

    def hidden_condition_brief(self) -> str:
        """给 S1 的揭示条件简表（只含未揭示项）。"""
        unrevealed = [f for f in self.hidden_facts if not f.revealed]
        if not unrevealed:
            return ""
        lines = []
        for fact in unrevealed:
            condition = fact.condition or "Ta 明确问到这件事"
            lines.append(f"- id={fact.id} 揭示条件：{condition}")
        return "\n".join(lines)
