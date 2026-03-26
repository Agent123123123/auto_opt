"""
Phase 1a Meta Agent
-------------------
职责：读取 skill 文档 + 评估历史，分析失败模式，提出 skill 改进 patch。

接收信息：
  - 当前 skill 文档全文（SKILL.md + 所有 0x_*.md）
  - 最近 N 代的评估报告历史（Judge Agent 输出的 JSON 列表）
  - archive 中的最高分（不含具体 skill 内容，避免直接抄）
  - mutation_constraints（禁止修改的内容清单）

不接收：
  - Task Agent 的执行日志（太长，且 failure_analysis 已提炼关键信息）
  - Judge Agent 的 prompt / 评分公式（Meta Agent 不应知道具体打分方式，
    防止它反向设计 skill 来迎合评分逻辑而非真正改进工程质量）

产出：
  - unified diff patch（作用于 mem_gen/skill/ 目录下的文件）
  - 改动摘要
"""

import os
import sys
import json
sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))

from agent.llm_withtools import run_agent_to_completion
from agent.tools import load_tools
from utils.skill_loader import load_skill_bundle

# ──────────────────────────────────────────────────────────────────────────────
# Prompt 设计
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior engineer optimizing skill documents for an AI agent that builds \
SRAM memory compiler infrastructure.

You MUST follow the mutation constraints exactly. Violations will cause the patch \
to be rejected.

MUTATION CONSTRAINTS FOR PHASE 1a:
  ALLOWED:
    ✅ Add new pitfall entries (append to existing pitfall lists)
    ✅ Improve clarity of workflow step descriptions
    ✅ Modify tiling algorithm boundary handling descriptions
    ✅ Add new interface verification steps
    ✅ Correct factual errors in parameter enumeration ranges
    ✅ Add examples for edge cases that caused failures

  FORBIDDEN:
    ❌ Delete existing pitfall entries (only add or update)
    ❌ Modify objective facts about TSMC macro naming conventions
    ❌ Modify port polarity definitions (CEB/WEB active-low is a hardware spec)
    ❌ Change the core wrapper generation algorithm structure
    ❌ Modify any file outside mem_gen/skill/ directory

OUTPUT FORMAT:
  1. Brief analysis of failure patterns (2-5 sentences)
  2. Proposed changes (as unified diff, one diff block per changed file)
  3. Expected impact on each failing metric

Use this format:
  <analysis>
  ...
  </analysis>
  <patch>
  --- a/mem_gen/skill/<filename>
  +++ b/mem_gen/skill/<filename>
  @@ ... @@
  ...
  </patch>
  <expected_impact>
  ...
  </expected_impact>
"""

META_PROMPT_TEMPLATE = """\
## Current Skill Document

{skill_content}

## Evaluation History (most recent {history_len} runs, newest first)

{eval_history_formatted}

## Archive Status

- Best score achieved so far: {best_score}
- Current run score: {current_score}
- Generations without improvement: {stagnation_count}

## Instructions

Analyze the failure patterns across the evaluation history above.
Focus especially on **wrapper_interface_match** failures — these are the most \
critical metric for Phase 1a (weight: 35%).

Common failure patterns to look for:
  1. Port mismatch at NW segment boundaries (e.g., NW=4→8 transition)
  2. BWEB port handling missing for certain family types
  3. DATA/Q port width formula incorrect for large NB values
  4. Clock/enable pin naming convention inconsistency across families
  5. Missing combos due to incorrect parameter range enumeration

{stagnation_hint}

Produce a patch that addresses the top 1-2 failure patterns. Do not try to fix \
everything at once — focused changes converge faster.
"""

STAGNATION_HINTS = {
    0: "",
    1: "## Stagnation Hint\nTry a more targeted change: focus on the single lowest-scoring metric.",
    2: "## Stagnation Hint\nThe current approach is not working. Consider restructuring the "
       "relevant section rather than adding more notes.",
    3: "## Stagnation Hint\nStrong stagnation signal. Consider whether the failure is in "
       "the workflow description order vs. in the parameter enumeration logic.",
}


class MetaAgent:
    """
    Phase 1a Meta Agent.

    ⚠️  此文件不在 mutation_constraints 的允许修改范围内。
    Meta Agent 不能修改 judge_agent.py 或 eval_tools/。
    """

    def __init__(self, model_config: dict, chat_history_file: str = "./outputs/meta_chat.md"):
        self.model_config = model_config
        # Meta Agent 使用 edit 工具来直接修改 skill 文件
        self.tools = load_tools(["bash", "edit"])
        self.chat_history_file = chat_history_file

    def propose_improvement(self, inputs: dict) -> dict:
        """
        inputs keys:
            skill_dir        : str               mem_gen/skill/ 路径
            eval_history     : list[dict]        最近 N 代的 judge 报告
            best_score       : float
            current_score    : float
            stagnation_count : int               连续多少代无提升
        Returns:
            {"patch": str, "analysis": str, "expected_impact": str}
        """
        skill_content = load_skill_bundle(inputs["skill_dir"])

        eval_history_formatted = self._format_eval_history(inputs["eval_history"])
        stagnation_count = inputs.get("stagnation_count", 0)
        stagnation_hint = STAGNATION_HINTS.get(
            min(stagnation_count, 3),
            STAGNATION_HINTS[3]
        )

        prompt = META_PROMPT_TEMPLATE.format(
            skill_content          = skill_content,
            history_len            = len(inputs["eval_history"]),
            eval_history_formatted = eval_history_formatted,
            best_score             = f"{inputs['best_score']:.4f}",
            current_score          = f"{inputs['current_score']:.4f}",
            stagnation_count       = stagnation_count,
            stagnation_hint        = stagnation_hint,
        )

        messages = [{"role": "user", "content": prompt}]

        final_response, _ = run_agent_to_completion(
            model             = self.model_config["model"],
            system_prompt     = SYSTEM_PROMPT,
            messages          = messages,
            tools             = self.tools,
            max_steps         = 10,
            chat_history_file = self.chat_history_file,
            api_base          = self.model_config.get("api_base"),
            api_key           = self.model_config.get("api_key"),
            extra_headers     = self.model_config.get("extra_headers"),
        )

        return self._parse_response(final_response)

    @staticmethod
    def _format_eval_history(history: list[dict]) -> str:
        """将评估历史格式化为 Meta Agent 可读的文本。"""
        lines = []
        for i, report in enumerate(history):
            gen = len(history) - i
            lines.append(f"### Generation -{i} (score: {report['score']:.4f})")
            lines.append(f"passed_threshold: {report['passed_threshold']}")
            if report.get("failure_analysis"):
                lines.append("**Failure analysis:**")
                for fa in report["failure_analysis"]:
                    lines.append(
                        f"  - [{fa['metric']}] value={fa.get('value', 'N/A'):.3f} "
                        f"(threshold={fa.get('threshold', 'N/A')}): {fa['diagnosis']}"
                    )
            if report.get("improvement_hints"):
                lines.append("**Improvement hints from judge:**")
                for hint in report["improvement_hints"]:
                    lines.append(f"  - {hint}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _parse_response(response: str) -> dict:
        import re
        analysis = re.search(r"<analysis>(.*?)</analysis>", response, re.DOTALL)
        patch    = re.search(r"<patch>(.*?)</patch>",    response, re.DOTALL)
        impact   = re.search(r"<expected_impact>(.*?)</expected_impact>", response, re.DOTALL)
        return {
            "analysis":        analysis.group(1).strip() if analysis else "",
            "patch":           patch.group(1).strip()    if patch    else "",
            "expected_impact": impact.group(1).strip()   if impact   else "",
        }
