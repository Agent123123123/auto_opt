"""
Phase 1a Judge Agent  (global workspace evaluation)
----------------------------------------------------
职责：对 Task Agent 整个 workspace 的产出做一次全局评估，返回结构化 JSON 评分报告。

接收信息（inputs dict）：
  workspace_dir      : Task Agent 工作目录
  skill_dir          : skill bundle 目录（用于 vendor-agnostic 检查）
  chat_history_file  : （可选）保存 judge 对话记录的路径

产出 JSON 格式（固定 schema）：
{
  "score": float,
  "passed_threshold": bool,
  "breakdown": {
    "wrapper_completeness_rate":  float,  # weight=0.30
    "wrapper_parse_rate":         float,  # weight=0.20
    "family_breadth_score":       float,  # weight=0.20
    "lint_clean_rate":            float,  # weight=0.15
    "cross_family_consistency":   float,  # weight=0.10
    "vendor_agnostic_generality": float   # weight=0.05
  },
  "family_summary": {"<memory_type>": <combo_count>, ...},
  "failure_analysis": [{"metric": str, "diagnosis": str}],
  "improvement_hints": [str]
}
"""

import json
import os
import re
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))

from phase1a.opencode_runner import run as opencode_run
from phase1a.config import get_language_directive
from eval_tools.lint_check import run_lint_check
from eval_tools.interface_spec_consistency import check_interface_spec_consistency
from eval_tools.skill_generality_check import read_skill_docs

# ──────────────────────────────────────────────────────────────────────────────
# Scoring weights (must sum to 1.0) and constants
# ──────────────────────────────────────────────────────────────────────────────

SCORING_WEIGHTS = {
    "wrapper_completeness_rate":  0.30,
    "wrapper_parse_rate":         0.20,
    "family_breadth_score":       0.20,
    "lint_clean_rate":            0.15,
    "cross_family_consistency":   0.10,
    "vendor_agnostic_generality": 0.05,
}

# ≥ BREADTH_TARGET distinct memory families → full breadth score
BREADTH_TARGET = 5

TARGET_SCORE = 0.80

# ──────────────────────────────────────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an impartial hardware infrastructure quality evaluator performing a
GLOBAL assessment of an AI agent's Memory Compiler workspace.

You have already received objective Python tool measurements below.
Your job:
  1. Interpret the measurements and identify the root cause of failures.
  2. Compute the final weighted score using the formula provided.
  3. Write specific, actionable improvement_hints for the Meta Agent.

SCORING FORMULA:
  s1 = 0.30 * wrapper_completeness_rate   (claimed wrappers that were actually produced)
  s2 = 0.20 * wrapper_parse_rate          (wrappers with valid 'module' declaration)
  s3 = 0.20 * family_breadth_score        (breadth: ≥5 distinct memory families = 1.0)
  s4 = 0.15 * lint_clean_rate             (wrappers with zero lint warnings/errors)
  s5 = 0.10 * cross_family_consistency    (uniform port naming across all families)
  s6 = 0.05 * vendor_agnostic_generality  (skill docs free of vendor/node bindings)
  final_score = s1 + s2 + s3 + s4 + s5 + s6

GATE RULE: If pipeline_complete=False OR total_wrappers=0 → final_score = 0.0

ANTI-LENIENCY RULES:
  - Do NOT round up scores. Use exact values from tool outputs.
  - wrapper_completeness_rate = found_wrappers / claimed_in_done_txt
    (if DONE.txt not written, it is 0.0 — not estimated)
  - family_breadth_score = min(1.0, total_families / 5.0)
  - vendor_agnostic_generality: evaluate from the Skill Documents appendix.
    Penalise vendor/process-specific normative language in skill docs.
    vendor_penalty = min(1.0, vendor_violation_count * 0.20)
    enum_penalty   = min(0.5, type_enum_violation_count * 0.15)
    vendor_agnostic_generality = max(0.0, 1.0 - vendor_penalty - enum_penalty)
  - improvement_hints must be specific (mention failing family, metric, or file).
    No vague hints like "improve the workflow".

OUTPUT INSTRUCTIONS:
  Use the bash tool to write your complete JSON response to a file named
  `judge_result.json` in the current working directory.  Example:
    bash: echo '{...}' > judge_result.json
  Do NOT print the JSON as text — write the file only.
"""

JUDGE_PROMPT_TEMPLATE = """\
## Global Workspace Evaluation

Workspace: {workspace_dir}

## 1. Pipeline Completion

```
pipeline_complete (DONE.txt present): {pipeline_complete}
DONE.txt contents (first 30 lines):
{done_txt_contents}
```

## 2. Discovered Artifacts (scanned from artifacts/)

```
total_families_produced: {total_families}
total_combos_produced:   {total_combos}
total_wrapper_sv_files:  {total_wrappers}

Family breakdown:
{family_summary_table}
```

## 3. Wrapper Completeness

```
claimed_in_done_txt:  {claimed_count}
found_on_disk:        {found_wrappers}
wrapper_completeness_rate: {wrapper_completeness_rate:.4f}
```

## 4. Wrapper Parse Rate

```
parseable_wrappers:   {parseable_count}
total_sv_wrappers:    {total_wrappers}
wrapper_parse_rate:   {wrapper_parse_rate:.4f}
parse_errors_sample:
{parse_errors_sample}
```

## 5. Lint Results

```
total_files_linted:   {total_linted}
lint_clean_files:     {lint_clean_count}
lint_clean_rate:      {lint_clean_rate:.4f}
lint_warnings_sample:
{lint_warnings_sample}
```

## 6. Family Breadth Score

```
total_families: {total_families}
breadth_target: {breadth_target}  (≥{breadth_target} = 1.0)
family_breadth_score: {family_breadth_score:.4f}
```

## 7. Cross-Family Interface Consistency

```
families_checked:             {families_checked}
interface_consistent:         {interface_consistent}
cross_family_consistency:     {cross_family_consistency:.4f}
spec_violations:
{spec_violations}
```

## 8. Vendor-Agnostic Generality

```
total_skill_docs: {total_skill_docs}
skill_dir: {skill_dir}
```

To evaluate vendor_agnostic_generality, read the skill documents directly:
  bash: ls {skill_dir}
  bash: cat {skill_dir}/<filename>

Look for vendor/process-specific normative language (e.g. "TSMC", "N12", specific
macro names used as requirements rather than examples). Read only what you need.

## Your Task

Based ONLY on the measurements above:
1. Compute final_score using the scoring formula from the system prompt.
2. For each metric below its expected value, write a specific diagnosis.
3. Write improvement_hints that the Meta Agent can act on to improve skill docs.
4. Use bash to write the result to `judge_result.json` in the current directory.

JSON schema to write:
{{
  "score": <float>,
  "passed_threshold": <bool>,
  "breakdown": {{
    "wrapper_completeness_rate":  <float>,
    "wrapper_parse_rate":         <float>,
    "family_breadth_score":       <float>,
    "lint_clean_rate":            <float>,
    "cross_family_consistency":   <float>,
    "vendor_agnostic_generality": <float>
  }},
  "family_summary": {{{family_summary_json}}},
  "failure_analysis": [{{"metric": "...", "diagnosis": "..."}}],
  "improvement_hints": ["..."]
}}

## 9. Task Agent Work Log

File: `{task_chat_file}`
Use `tail -150 {task_chat_file}` to read the task agent's execution log before writing your assessment.
"""

# Judge Agent class
# ──────────────────────────────────────────────────────────────────────────────


class JudgeAgent:
    """
    Phase 1a Judge Agent — single global workspace evaluation.

    ⚠️  此文件不在 mutation_constraints 的允许修改范围内。
    Meta Agent 的 skill_dirs 只包含 mem_gen/skill/，不包含 evaluation/。
    """

    def __init__(self, model_config: dict):
        self.model_config = model_config

    def evaluate(self, inputs: dict) -> dict:
        """
        inputs keys:
            workspace_dir      : str
            skill_dir          : str   (skill bundle dir for vendor-agnostic check)
            chat_history_file  : str   (optional)
        Returns:
            dict matching the judge output schema
        """
        # ── Step 1: Python tools (objective measurements) ─────────────────────
        tool_results = self._run_tools(inputs)

        # Save Python tool measurements (deterministic ground truth, before LLM)
        chat_history_file = inputs.get("chat_history_file")
        if chat_history_file:
            _tools_path = os.path.join(
                os.path.dirname(os.path.abspath(chat_history_file)),
                "judge_tools.json",
            )
            os.makedirs(os.path.dirname(_tools_path), exist_ok=True)
            _serializable = {
                k: v for k, v in tool_results.items()
                if k not in ("family_combos",)
            }
            _serializable["family_combos"] = {
                k: list(v) for k, v in tool_results["family_combos"].items()
            }
            with open(_tools_path, "w", encoding="utf-8") as _f:
                import json as _json
                _json.dump(_serializable, _f, indent=2)

        # ── Step 2: Gate check ────────────────────────────────────────────────
        if not tool_results["pipeline_complete"] or tool_results["total_wrappers"] == 0:
            report = self._gate_fail_report(tool_results)
            # Write minimal gate-fail log so judge_chat.md always exists
            _chf = inputs.get("chat_history_file")
            if _chf:
                os.makedirs(os.path.dirname(os.path.abspath(_chf)), exist_ok=True)
                with open(_chf, "w", encoding="utf-8") as _f:
                    _f.write("# Judge Agent — Gate Failure\n\n")
                    _f.write(f"pipeline_complete: {tool_results['pipeline_complete']}\n")
                    _f.write(f"total_wrappers: {tool_results['total_wrappers']}\n\n")
                    _f.write("## Failure Analysis\n\n")
                    for fa in report.get("failure_analysis", []):
                        _f.write(f"- **{fa['metric']}**: {fa['diagnosis']}\n")
                    _f.write("\n## Improvement Hints\n\n")
                    for hint in report.get("improvement_hints", []):
                        _f.write(f"- {hint}\n")
            return report

        # ── Step 3: Build judge prompt ────────────────────────────────────────
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            workspace_dir             = inputs["workspace_dir"],
            pipeline_complete         = tool_results["pipeline_complete"],
            done_txt_contents         = tool_results["done_txt_contents"][:2000] or "(empty)",
            total_families            = tool_results["total_families"],
            total_combos              = tool_results["total_combos"],
            total_wrappers            = tool_results["total_wrappers"],
            family_summary_table      = self._format_family_table(tool_results["family_combos"]),
            claimed_count             = tool_results["claimed_count"],
            found_wrappers            = tool_results["found_wrappers"],
            wrapper_completeness_rate = tool_results['wrapper_completeness_rate'],
            parseable_count           = tool_results["parseable_count"],
            wrapper_parse_rate        = tool_results['wrapper_parse_rate'],
            parse_errors_sample       = tool_results["parse_errors_sample"] or "(none)",
            total_linted              = tool_results["total_linted"],
            lint_clean_count          = tool_results["lint_clean_count"],
            lint_clean_rate           = tool_results['lint_clean_rate'],
            lint_warnings_sample      = tool_results.get("lint_warnings_sample", "(none)"),
            breadth_target            = BREADTH_TARGET,
            family_breadth_score      = tool_results['family_breadth_score'],
            families_checked          = ", ".join(sorted(tool_results["family_combos"].keys())) or "(none)",
            interface_consistent      = tool_results["interface_consistent"],
            cross_family_consistency  = tool_results['cross_family_consistency'],
            spec_violations           = self._format_violations(tool_results["spec_violations"]),
            total_skill_docs          = tool_results["total_skill_docs"],
            family_summary_json       = ", ".join(
                f'"{k}": {len(v)}' for k, v in sorted(tool_results["family_combos"].items())
            ),
            skill_dir                 = inputs.get("skill_dir", "(not provided)"),
            task_chat_file            = inputs.get("task_chat_file") or "(not provided)",
        )

        # ── Step 4: LLM interprets + scores (via opencode) ────────────────────
        chat_history_file = inputs.get("chat_history_file")
        lang_directive = get_language_directive()
        full_prompt = (lang_directive + "\n\n" + SYSTEM_PROMPT + "\n\n" + prompt).lstrip()
        raw_text = opencode_run(
            prompt            = full_prompt,
            cwd               = inputs["workspace_dir"],
            model             = self.model_config["opencode_model"],
            chat_history_file = chat_history_file,
            timeout           = 7200,
            result_file       = "judge_result.json",
            agent             = "no-skill",
        )
        try:
            report = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError, ValueError):
            # opencode timed out or returned empty — fall back to tool-computed scores
            report = {
                "score": 0.0,
                "passed_threshold": False,
                "breakdown": {k: 0.0 for k in SCORING_WEIGHTS},
                "family_summary": {},
                "failure_analysis": [{"metric": "judge_llm_timeout",
                                      "diagnosis": "Judge LLM timed out; scores computed from Python tools only"}],
                "improvement_hints": ["Judge LLM did not respond — check task agent logs for root cause"],
            }

        # ── Step 5: Enforce Python-computed values (LLM can't override these) ─
        report = self._enforce_tool_values(report, tool_results)
        return report

    # ─── Private helpers ──────────────────────────────────────────────────────

    def _run_tools(self, inputs: dict) -> dict:
        """Scan the workspace and run all objective measurement tools."""
        workspace = inputs["workspace_dir"]
        skill_dir = inputs.get("skill_dir", workspace)

        # 1. DONE.txt check
        done_path = os.path.join(workspace, "DONE.txt")
        pipeline_complete = os.path.exists(done_path)
        done_txt_contents = ""
        claimed_paths: list[str] = []
        if pipeline_complete:
            try:
                with open(done_path) as fh:
                    done_txt_contents = fh.read()
                claimed_paths = [
                    ln.strip() for ln in done_txt_contents.splitlines()
                    if ln.strip() and not ln.startswith("#")
                ]
            except Exception:
                pass

        # 2. Discover produced artifacts by scanning workspace/artifacts/
        artifacts_root = os.path.join(workspace, "artifacts")
        family_combos: dict[str, list[str]] = {}
        if os.path.isdir(artifacts_root):
            for family in sorted(os.listdir(artifacts_root)):
                family_path = os.path.join(artifacts_root, family)
                if not os.path.isdir(family_path):
                    continue
                combos = sorted(
                    c for c in os.listdir(family_path)
                    if os.path.isdir(os.path.join(family_path, c))
                )
                if combos:
                    family_combos[family] = combos

        total_families = len(family_combos)
        total_combos   = sum(len(v) for v in family_combos.values())

        # 3. Scan all wrapper .sv files
        all_sv_wrappers: list[str] = []
        for family, combos in family_combos.items():
            for combo in combos:
                sv = os.path.join(artifacts_root, family, combo, f"{family}_{combo}_wrapper.sv")
                if os.path.exists(sv):
                    all_sv_wrappers.append(sv)

        total_wrappers = len(all_sv_wrappers)

        # 4. Wrapper completeness: claimed in DONE.txt vs found
        claimed_count = len(claimed_paths)
        found_wrappers = total_wrappers
        wrapper_completeness_rate = (
            min(found_wrappers, claimed_count) / claimed_count if claimed_count > 0
            else (1.0 if found_wrappers > 0 and pipeline_complete else 0.0)
        )

        # 5. Parse rate: check each wrapper for 'module' keyword
        parse_errors: list[str] = []
        for sv_path in all_sv_wrappers:
            try:
                with open(sv_path) as fh:
                    content = fh.read()
                if not re.search(r"\bmodule\b", content):
                    fam   = sv_path.split(os.sep)[-4] if os.sep in sv_path else sv_path
                    combo = sv_path.split(os.sep)[-3] if os.sep in sv_path else sv_path
                    parse_errors.append(f"{fam}/{combo}: missing 'module' keyword")
            except Exception as exc:
                parse_errors.append(f"{sv_path}: {exc}")

        parseable_count  = total_wrappers - len(parse_errors)
        wrapper_parse_rate = parseable_count / total_wrappers if total_wrappers > 0 else 0.0

        # 6. Lint check (aggregate across all families)
        total_linted = 0
        lint_error_files = 0
        lint_warnings_parts: list[str] = []
        for family, combos in family_combos.items():
            lr = run_lint_check(workspace, family, combos)
            total_linted   += lr["total_files_checked"]
            lint_error_files += lr["files_with_errors"]
            if lr.get("details"):
                lint_warnings_parts.append(str(lr["details"])[:200])

        lint_clean_count = total_linted - lint_error_files
        lint_clean_rate  = lint_clean_count / total_linted if total_linted > 0 else 1.0

        # 7. Family breadth score
        family_breadth_score = min(1.0, total_families / BREADTH_TARGET)

        # 8. Cross-family interface consistency (one representative combo per family)
        combo_per_type = {fam: combos[0] for fam, combos in family_combos.items() if combos}
        ispec = check_interface_spec_consistency(
            workspace_dir    = workspace,
            memory_type_list = list(family_combos.keys()),
            combo_per_type   = combo_per_type,
        )

        # 9. Skill docs count (for prompt display; LLM will read them directly)
        skill_doc_result = read_skill_docs(
            workspace_dir = skill_dir,
            skill_dirs    = None,
        )

        return {
            "pipeline_complete":          pipeline_complete,
            "done_txt_contents":          done_txt_contents,
            "claimed_count":              claimed_count,
            "family_combos":              family_combos,
            "total_families":             total_families,
            "total_combos":               total_combos,
            "total_wrappers":             total_wrappers,
            "found_wrappers":             found_wrappers,
            "wrapper_completeness_rate":  wrapper_completeness_rate,
            "parseable_count":            parseable_count,
            "parse_errors_sample":        "; ".join(parse_errors[:5]),
            "wrapper_parse_rate":         wrapper_parse_rate,
            "total_linted":               total_linted,
            "lint_clean_count":           lint_clean_count,
            "lint_clean_rate":            lint_clean_rate,
            "lint_warnings_sample":       "; ".join(lint_warnings_parts[:3]),
            "family_breadth_score":       family_breadth_score,
            "interface_consistent":       ispec["consistent"],
            "cross_family_consistency":   ispec["score"],
            "spec_violations":            ispec["violations"],
            "total_skill_docs":           skill_doc_result["total_md_files"],
        }

    def _enforce_tool_values(self, report: dict, tool_results: dict) -> dict:
        """Overwrite LLM-reported values with Python-computed ground truth."""
        bd = report.setdefault("breakdown", {})
        bd["wrapper_completeness_rate"] = round(tool_results["wrapper_completeness_rate"], 4)
        bd["wrapper_parse_rate"]        = round(tool_results["wrapper_parse_rate"],        4)
        bd["family_breadth_score"]      = round(tool_results["family_breadth_score"],      4)
        bd["lint_clean_rate"]           = round(tool_results["lint_clean_rate"],           4)
        bd["cross_family_consistency"]  = round(tool_results["cross_family_consistency"],  4)
        # vendor_agnostic_generality: LLM semantic judgment — only clamp range
        gen_score = float(bd.get("vendor_agnostic_generality", 1.0))
        bd["vendor_agnostic_generality"] = round(max(0.0, min(1.0, gen_score)), 4)

        # Recompute final score in Python (ignore LLM arithmetic)
        score = sum(SCORING_WEIGHTS[k] * bd[k] for k in SCORING_WEIGHTS)
        report["score"]            = round(score, 4)
        report["passed_threshold"] = (report["score"] >= TARGET_SCORE)
        report.setdefault("family_summary", {
            k: len(v) for k, v in tool_results["family_combos"].items()
        })
        return report

    def _gate_fail_report(self, tool_results: dict) -> dict:
        reasons = []
        if not tool_results["pipeline_complete"]:
            reasons.append("DONE.txt not found — pipeline did not complete")
        if tool_results["total_wrappers"] == 0:
            reasons.append("no wrapper .sv files produced in artifacts/")
        return {
            "score":            0.0,
            "passed_threshold": False,
            "breakdown":        {k: 0.0 for k in SCORING_WEIGHTS},
            "family_summary":   {},
            "failure_analysis": [{"metric": "gate", "diagnosis": r} for r in reasons],
            "improvement_hints": [f"Fix gate failure first: {r}" for r in reasons],
        }

    @staticmethod
    def _format_family_table(family_combos: dict) -> str:
        if not family_combos:
            return "  (no artifacts found)"
        lines = []
        for fam, combos in sorted(family_combos.items()):
            lines.append(f"  {fam}: {len(combos)} combos  {combos[:4]!r}{'...' if len(combos)>4 else ''}")
        return "\n".join(lines)

    @staticmethod
    def _format_violations(violations: list[dict]) -> str:
        if not violations:
            return "  (none)"
        return "\n".join(f"  [{v['rule']}] {v['detail']}" for v in violations)

    @staticmethod
    def _format_skill_docs(files: dict) -> str:
        if not files:
            return "(no skill documents found)"
        sections = []
        for path, content in files.items():
            sections.append(f"### {path}\n\n{content}")
        return "\n\n---\n\n".join(sections)

