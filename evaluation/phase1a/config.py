"""
Phase 1a 配置：mem_gen 独立优化
"""

PHASE1A_CONFIG = {
    "max_generations": 40,
    "stagnation_limit": 8,
    "target_score": 0.80,
    # ── 语言开关 ──────────────────────────────────────────────────────────────
    # 控制所有 agent（task / judge / meta）输出的语言。
    # 支持的值：
    #   "zh"  →  简体中文（默认）
    #   "en"  →  English（不注入任何语言指令，模型自由选择）
    "output_language": "zh",
}


# ──────────────────────────────────────────────────────────────────────────────
# 语言指令工具函数
# ──────────────────────────────────────────────────────────────────────────────

_LANGUAGE_DIRECTIVES: dict[str, str] = {
    "zh": (
        "LANGUAGE DIRECTIVE: You MUST write ALL your analysis, reasoning, "
        "diagnostic summaries, change descriptions, and explanatory text in "
        "Simplified Chinese (简体中文). "
        "Code, file paths, shell commands, JSON keys, and technical "
        "identifiers must remain in English."
    ),
    "en": "",   # no override — model defaults to English naturally
}


def get_language_directive(lang: str | None = None) -> str:
    """
    Return the language instruction prefix to prepend to any agent's system prompt.

    Args:
        lang: Language code ("zh" | "en"). Defaults to PHASE1A_CONFIG["output_language"].

    Returns:
        A non-empty instruction string for non-English languages, empty string for English.
    """
    if lang is None:
        lang = PHASE1A_CONFIG.get("output_language", "zh")
    return _LANGUAGE_DIRECTIVES.get(lang, "")
