"""
Phase 1a Meta Agent
-------------------
职责：读取 skill 文档 + 评估历史 + 执行日志，诊断 Task Agent 失败的根本原因，
      对 skill 文档做出有针对性的改进。

接收信息：
  - 当前 skill 文档全文（SKILL.md + 所有 0x_*.md）
  - 最近 N 代的评估报告历史（Judge Agent 输出的 JSON 列表）
  - archive 中的最高分
  - Task Agent 执行日志路径（task_chat_file）——meta agent 应主动读取
  - Judge Agent 完整分析路径（judge_chat_file）——meta agent 应主动读取

产出：
  - 对 skill 文档的直接编辑（通过 opencode 工具）
  - meta_result.json（分析摘要）
"""

import os
import sys
import json
import re
sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))

from phase1a.opencode_runner import run as opencode_run
from phase1a.config import get_language_directive

# ──────────────────────────────────────────────────────────────────────────────
# Prompt 设计
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior engineering coach optimizing skill documents for an AI coding
agent that builds SRAM memory compiler infrastructure.

Your goal: analyze task agent performance, identify skill doc weaknesses, and
improve the skill documents to raise the task agent's output quality.

══════════════════════════════════════════════════════════════════════════════
IMPROVEMENT PHILOSOPHY
══════════════════════════════════════════════════════════════════════════════

Your improvements should be **principled and generalizable**:
  - Prefer adding clear methodology, design principles, and reusable patterns
    over patching individual symptoms.
  - If the agent failed at step X, think about WHY the skill doc didn't
    prepare it — was the principle unclear? Was the workflow incomplete?
    Was a prerequisite missing?
  - Good skill improvements help the agent succeed not just on the current
    failure, but on similar future challenges.

Avoid overly narrow fixes that only address one specific error message.
Instead, strengthen the underlying guidance that would have prevented
the entire class of errors.

══════════════════════════════════════════════════════════════════════════════
MUTATION RULES
══════════════════════════════════════════════════════════════════════════════
ALLOWED (all of these):
  ✅ Add, edit, or remove pitfall entries based on evidence from the logs
  ✅ Restructure sections when the current structure is confusing or incomplete
  ✅ Add concrete, runnable command examples (exact paths, exact syntax)
  ✅ Add or update step-by-step workflow instructions
  ✅ Delete outdated, wrong, or misleading guidance
  ✅ Promote critical information to a prominent position
  ✅ Correct factual errors in parameter enumeration or interface descriptions
  ✅ Add prerequisite checks or environment setup steps
  ✅ Add general principles, design patterns, or decision frameworks

FORBIDDEN:
  ❌ Modify files outside the mem_gen/skill/ directory
  ❌ Change objective hardware specs (port polarity: CEB/WEB are active-low)

══════════════════════════════════════════════════════════════════════════════
OUTPUT REQUIREMENTS
══════════════════════════════════════════════════════════════════════════════
WORKFLOW:
  1. Read the task agent log and judge analysis to understand what happened.
  2. Inspect the relevant skill files.
  3. Use the edit tool to apply your changes directly to the skill files.
  4. Write `meta_result.json` as the LAST step.

meta_result.json format:
  {
    "weakness":        "<1-2 sentences: what aspect of the task agent's output was insufficient>",
    "skill_gap":       "<1-2 sentences: what guidance was missing or unclear in the skill docs>",
    "analysis":        "<2-4 sentences: what you changed and why it will help>",
    "changes": [
      "<filename> §<section>: was '<old>' → now '<new (≤30 chars or paraphrase)>'",
      ...
    ],
    "patch":           "<unified diff of all changes, or empty string if no diff available>",
    "expected_impact": "<which aspects of output quality should improve in the next generation>"
  }

Rules for "changes" list:
  - One item per edit hunk.
  - Format: "<filename> §<nearest_heading>: was '<old>' → now '<new>'"
  - For additions: "<filename> §<section>: added '<what was added>'"
  - For deletions: "<filename> §<section>: removed '<what was removed>'"
  - Each item on one line; truncate fragments after ~30 chars.

Do NOT stop before writing meta_result.json.
"""

META_PROMPT_TEMPLATE = """\
## Your Inputs

### Current Skill Documents

The skill documents are in the current working directory (your CWD = the skill
directory). The following reference log files are also available in your CWD:

  - `task_chat.md`   — task agent execution log for this generation
  - `judge_chat.md`  — judge agent evaluation log for this generation

Use the file system tools to inspect them:

  ls              — list all files
  read <file>     — open and read a specific file

Read the log files and skill files you need before making edits. You do NOT need
to read every file upfront — focus on the ones relevant to the diagnosed problem.

---

## Evaluation History (most recent {history_len} runs, newest first)

{eval_history_formatted}

---

## Run Status

- Best score so far: {best_score}
- Current generation score: {current_score}
- Consecutive generations without improvement: {stagnation_count}

{stagnation_hint}

---

Based on the eval history above, read the log files in your CWD and apply your
improvements to the skill files, then write meta_result.json.
"""

STAGNATION_HINTS = {
    0: "",
    1: (
        "### Stagnation Notice (1 generation)\n"
        "The pipeline has not produced any wrapper files yet. Focus exclusively on\n"
        "what blocked the task agent from completing even one step — not on fine-tuning\n"
        "output quality."
    ),
    2: (
        "### ⚠ Stagnation Warning (2 generations)\n"
        "The same fundamental blockage has persisted for 2 generations. The current skill\n"
        "docs are not providing the right guidance. Read the task log carefully — the agent\n"
        "is likely hitting the same error or wrong path. Consider:\n"
        "  - Is the error caused by a missing prerequisite step?\n"
        "  - Is the agent using the wrong command or wrong path?\n"
        "  - Is a critical piece of information buried or absent from the skill docs?\n"
        "Restructure or rewrite the relevant section. Do not just add more pitfall notes."
    ),
    3: (
        "### 🚨 Stagnation Alert (3+ generations — strong intervention required)\n"
        "Three or more consecutive generations have all scored 0.0. The skill documents\n"
        "are failing to guide the agent past a fundamental blocker.\n\n"
        "Required actions:\n"
        "  1. Read the task log from ALL recent generations and find the COMMON error.\n"
        "  2. Add a 'CRITICAL PREREQUISITE' block at the TOP of the most relevant skill file.\n"
        "  3. Include the exact command, exact path, and exact expected output.\n"
        "  4. Delete or quarantine any guidance that has led to repeated wrong attempts.\n\n"
        "Do NOT add more pitfall entries to the existing list — the agent is not reaching\n"
        "that point. Fix what comes first."
    ),
}


class MetaAgent:
    """
    Phase 1a Meta Agent.

    ⚠️  此文件不在 mutation_constraints 的允许修改范围内。
    Meta Agent 不能修改 judge_agent.py 或 eval_tools/。
    """

    def __init__(self, model_config: dict, chat_history_file: str = "./outputs/meta_chat.md"):
        self.model_config = model_config
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
        eval_history_formatted = self._format_eval_history(inputs["eval_history"])
        stagnation_count = inputs.get("stagnation_count", 0)
        stagnation_hint = STAGNATION_HINTS.get(
            min(stagnation_count, 3),
            STAGNATION_HINTS[3]
        )

        prompt = META_PROMPT_TEMPLATE.format(
            history_len            = len(inputs["eval_history"]),
            eval_history_formatted = eval_history_formatted,
            best_score             = f"{inputs['best_score']:.4f}",
            current_score          = f"{inputs['current_score']:.4f}",
            stagnation_count       = stagnation_count,
            stagnation_hint        = stagnation_hint,
        )

        messages = [{"role": "user", "content": prompt}]

        chat_history_file = inputs.get("chat_history_file", self.chat_history_file)

        # opencode runs in skill_dir with bash+edit tools:
        # the agent reads skill files, edits them directly, then outputs XML.
        lang_directive = get_language_directive()
        full_prompt = (lang_directive + "\n\n" + SYSTEM_PROMPT + "\n\n" + prompt).lstrip()
        final_response = opencode_run(
            prompt            = full_prompt,
            cwd               = inputs["skill_dir"],
            model             = self.model_config["opencode_model"],
            chat_history_file = chat_history_file,
            timeout           = 7200,
            result_file       = "meta_result.json",
            agent             = "no-skill",
        )

        try:
            result = json.loads(final_response)
        except (json.JSONDecodeError, TypeError):
            # meta_result.json missing or malformed — extract what we can
            result = self._parse_response(final_response)
        # Signal to loop.py that opencode already applied edits directly
        result["applied_directly"] = True
        return result

    @staticmethod
    def _format_eval_history(history: list[dict]) -> str:
        """将评估历史格式化为 Meta Agent 可读的文本。"""
        lines = []
        for i, report in enumerate(history):
            gen = len(history) - i
            lines.append(f"### Generation -{i} (score: {report['score']:.4f})")
            lines.append(f"passed_threshold: {report.get('passed_threshold', False)}")
            if report.get("failure_analysis"):
                lines.append("**Failure analysis:**")
                for fa in report["failure_analysis"]:
                    _val = fa.get('value', 'N/A')
                    _val_str = f"{_val:.3f}" if isinstance(_val, (int, float)) else str(_val)
                    _thr = fa.get('threshold', 'N/A')
                    _thr_str = f"{_thr:.3f}" if isinstance(_thr, (int, float)) else str(_thr)
                    lines.append(
                        f"  - [{fa['metric']}] value={_val_str} "
                        f"(threshold={_thr_str}): {fa['diagnosis']}"
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
        weakness   = re.search(r"<weakness>(.*?)</weakness>",     response, re.DOTALL)
        root_cause = re.search(r"<root_cause>(.*?)</root_cause>", response, re.DOTALL)
        skill_gap  = re.search(r"<skill_gap>(.*?)</skill_gap>",   response, re.DOTALL)
        analysis   = re.search(r"<analysis>(.*?)</analysis>",     response, re.DOTALL)
        patch      = re.search(r"<patch>(.*?)</patch>",           response, re.DOTALL)
        impact     = re.search(r"<expected_impact>(.*?)</expected_impact>", response, re.DOTALL)
        return {
            "weakness":        weakness.group(1).strip() if weakness else (root_cause.group(1).strip() if root_cause else ""),
            "skill_gap":       skill_gap.group(1).strip()  if skill_gap  else "",
            "analysis":        analysis.group(1).strip()   if analysis   else "",
            "patch":           patch.group(1).strip()      if patch      else "",
            "expected_impact": impact.group(1).strip()     if impact     else "",
        }
