"""
Phase 1a Judge Agent
--------------------
职责：评估 Task Agent 的产出，返回结构化 JSON 评分报告。

接收信息（inputs dict）：
  workspace_dir       : Task Agent 工作目录
  memory_type         : 主测 memory type
  param_combos        : 标准参数组合列表
  behavior_model_dir  : Golden 行为模型目录
  expected_kits       : 期望 artifact 类型列表
  all_on_combo        : 全参数开的 combo string（用于 param_toggle 检查）
  min_combo           : 最小参数的 combo string
  split_combos        : 拆分边界组合列表（见 split_check.py）
  all_memory_types    : 全部期望 family 列表（用于 mem_type_full_coverage）
  combo_per_type      : {family: 代表性 combo}（用于 interface_spec_consistency）

产出 JSON 格式（固定 schema）：
{
  "score": float,
  "passed_threshold": bool,
  "breakdown": {
    "layer0_gates": {"pipeline_complete": bool, "wrapper_parsable": bool},
    "layer1": {
      "artifact_completeness":    float,  # weight=0.10
      "wrapper_interface_match":  float,  # weight=0.20
      "param_toggle_correctness": float,  # weight=0.20
      "split_correctness":        float,  # weight=0.15
      "tiein_correctness":        float   # weight=0.10
    },
    "layer2": {
      "mem_type_full_coverage":        float,  # weight=0.10
      "interface_spec_consistency":    float,  # weight=0.05
      "lint_clean_rate":               float,  # weight=0.05
      "vendor_agnostic_generality":    float   # weight=0.05
    }
  },
  "failure_analysis": [{"metric": str, "value": float, "threshold": float, "diagnosis": str}],
  "improvement_hints": [str]
}
"""

import json
import math
import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))

from agent.llm import get_response_from_llm
from eval_tools.compute_metrics import compute_artifact_completeness
from eval_tools.lint_check import run_lint_check
from eval_tools.interface_match import check_wrapper_interface_match
from eval_tools.param_toggle_check import check_param_toggle
from eval_tools.split_check import check_split_correctness
from eval_tools.interface_spec_consistency import check_interface_spec_consistency
from eval_tools.skill_generality_check import read_skill_docs

# ──────────────────────────────────────────────────────────────────────────────
# Prompt 设计
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an impartial hardware infrastructure quality evaluator.
You have already received the objective measurement results from Python tools.
Your job is to:
  1. Interpret the tool measurements and identify the root cause of failures.
  2. Produce a structured evaluation report with actionable improvement hints.
  3. Compute the final weighted score using the formula provided.

SCORING FORMULA (Phase 1a):
  layer0 gates: pipeline_complete AND wrapper_parsable (binary - if either fails, score = 0.0)
  s1 = 0.10 * artifact_completeness
  s2 = 0.20 * wrapper_interface_match
  s3 = 0.20 * param_toggle_correctness    (all-params-on AND min-params both pass)
  s4 = 0.15 * split_correctness           (non-divisible width/depth splits handled correctly)
  s5 = 0.10 * tiein_correctness           (unused bits/ports properly tied or left open)
  s6 = 0.10 * mem_type_full_coverage      (all expected memory families produced)
  s7 = 0.05 * interface_spec_consistency  (cross-family naming/polarity uniformity)
  s8 = 0.05 * lint_clean_rate
  s9 = 0.05 * vendor_agnostic_generality  (skill docs must be vendor/process-neutral)
  final_score = s1+s2+s3+s4+s5+s6+s7+s8+s9

ANTI-LENIENCY RULES (must follow strictly):
  - Do NOT round up scores. Use exact values from the tool outputs.
  - An interface mismatch counts as 0 for that combo — no partial credit.
  - param_toggle_correctness: both all_on_pass AND min_pass must be True for score > 0.
  - split_correctness: any address coverage gap or missing tie-high is a FAIL for that combo.
  - mem_type_full_coverage: ALL families must be present — if any is missing, score = 0.0.
  - vendor_agnostic_generality: YOU evaluate this directly from the skill documents appended
    at the end of this prompt. Report vendor/process bindings and type enumerations found.
  - improvement_hints must be specific (mention the failing combo or port name).

Output ONLY valid JSON matching the schema. No extra text.
"""

JUDGE_PROMPT_TEMPLATE = """\
## Evaluation Task

You are evaluating a mem_gen execution for: **{memory_type}**

## Python Tool Measurement Results

### 1. Pipeline Status
```
pipeline_complete: {pipeline_complete}
wrapper_parsable: {wrapper_parsable}
parse_errors: {parse_errors}
```

### 2. Artifact Completeness
```
expected_combos: {expected_combos}
found_combos: {found_combos}
missing_combos: {missing_combos}
per_kit_completeness:
{per_kit_completeness}
artifact_completeness_score: {artifact_completeness}
```

### 3. Wrapper Interface Match (vs. Behavior Model)
Checks: port names, polarities (CEB/WEB active-low), widths (DATA/BWEB/ADR), clock/enable completeness.

```
total_combos_checked: {total_combos}
combos_with_zero_mismatch: {zero_mismatch_count}
wrapper_interface_match_score: {wrapper_interface_match}

Per-combo details:
{per_combo_interface}
```

### 4. Parameter Toggle Correctness
Checks that wrapper generates correctly with ALL optional params enabled and with MINIMUM params only.

```
all_on_combo: {all_on_combo}
min_combo: {min_combo}
all_on_pass: {all_on_pass}
min_pass: {min_pass}
param_toggle_correctness_score: {param_toggle_correctness}
all_on_details: {all_on_details}
min_details: {min_details}
```

### 5. Split Correctness
Checks wrapper correctness for non-power-of-2 depth and non-divisible width splits.

```
split_correctness_score: {split_correctness}
address_coverage_ok: {address_coverage_ok}
per_combo_split_details:
{per_combo_split}
```

### 6. Tie-off / Leave-Open Correctness
Checks unused bits are properly tied high (BWEB→1) or left open (Q→z), never left floating.

```
tiein_correctness_score: {tiein_correctness}
tiein_details: {tiein_details}
```

### 7. Memory Type Full Coverage
All expected memory families must be produced (all-or-nothing).

```
expected_families: {expected_families}
built_families: {built_families}
mem_type_full_coverage_score: {mem_type_full_coverage}
```

### 8. Interface Spec Consistency (Cross-Family)
Checks that port naming (CLK/CK, A/ADR, DI/DO) and polarity rules are uniform across all families.

```
interface_spec_consistent: {interface_spec_consistent}
interface_spec_consistency_score: {interface_spec_consistency}
spec_violations:
{spec_violations}
```

### 9. Lint Check
```
total_wrappers_linted: {total_linted}
wrappers_with_zero_warnings: {clean_count}
lint_clean_rate: {lint_clean_rate}
lint_warnings_summary:
{lint_warnings_summary}
```

### 10. Vendor-Agnostic Generality
Read the skill documents in the **Appendix** below and identify:

  VIOLATION TYPE 1 — vendor_specific:
    Normative references to a specific foundry (TSMC, Samsung Foundry, GF, SMIC, UMC)
    or process node (N12FFC, N7FF, 28nm, 5nm...) that bind the skill to one platform.
    NOT a violation: usage examples in code blocks, or purely illustrative mentions.

  VIOLATION TYPE 2 — type_enumeration:
    Coverage claims listing 2+ specific SRAM type names (spsram_ulvt, dpsram_lvt, 2prf_lvt...)
    instead of abstract architecture categories (single-port SRAM, dual-port RF, ...).
    NOT a violation: type names inside code/CLI examples, or single-type worked examples.

Scoring:
  vendor_penalty = min(1.0, vendor_violation_count * 0.20)   # 5+ → score = 0
  enum_penalty   = min(0.5, enum_violation_count   * 0.15)   # 4+ → deduct 0.5
  vendor_agnostic_generality = max(0.0, 1.0 - vendor_penalty - enum_penalty)

```
total_skill_docs: {total_skill_docs}
```

## Your Task

Based ONLY on the measurements above:

1. Compute the final score using the scoring formula.
2. For each metric below threshold, write a specific diagnosis.
3. Write improvement_hints that a Meta Agent can use to improve the skill document.
   - Hints must be actionable and reference the specific failing combo, port, or rule.
   - Examples:
     * "split_check: combo 512x40m8 — BWEB[39:32] tie-to-1 not found; skill §split should add
        explicit BWEB remainder handling"
     * "param_toggle: all_on wrapper missing DSLP port — check skill's optional-pin declaration flow"
     * "interface_spec_consistency: CLK vs CK mismatch between spsram and 2prf families"
   - Do NOT write vague hints like "improve the workflow".

Output the JSON report now.

## Appendix: Skill Documents

{skill_doc_appendix}
"""

# ──────────────────────────────────────────────────────────────────────────────
# Thresholds（固化，不在进化目标中）
# ──────────────────────────────────────────────────────────────────────────────

THRESHOLDS = {
    "artifact_completeness":       0.90,
    "wrapper_interface_match":     0.90,
    "param_toggle_correctness":    1.00,   # both configs must pass completely
    "split_correctness":           0.85,
    "tiein_correctness":           0.90,
    "mem_type_full_coverage":      1.00,   # all-or-nothing: all families required
    "interface_spec_consistency":  1.00,   # zero naming violations tolerated
    "lint_clean_rate":             0.80,
    "vendor_agnostic_generality":  1.00,   # zero vendor/enum violations required
}


class JudgeAgent:
    """
    Phase 1a Judge Agent.

    ⚠️  此文件不在 mutation_constraints 的允许修改范围内。
    Meta Agent 的 skill_dirs 只包含 mem_gen/skill/，不包含 evaluation/。
    """

    def __init__(self, model_config: dict, expected_families: list[str] | None = None):
        self.model_config = model_config
        self.expected_families = expected_families or ["spsram", "dpsram", "1prf", "2prf", "uhd1prf"]

    def evaluate(self, inputs: dict) -> dict:
        """
        inputs keys:
            workspace_dir      : str
            memory_type        : str
            param_combos       : list[str]
            behavior_model_dir : str
            expected_kits      : list[str]  e.g. ["sv", "lef", "lib", "gds", "ds"]
            agent_steps        : int
        Returns:
            dict matching the judge output schema
        """
        # ── Step 1: 运行 Python 工具（客观测量，不经过 LLM）──────────────────
        tool_results = self._run_tools(inputs)

        # ── Step 2: 门槛检查（纯 Python，不过 LLM）──────────────────────────
        if not tool_results["pipeline_complete"] or not tool_results["wrapper_parsable"]:
            return self._gate_fail_report(tool_results)

        # ── Step 3: 构造 Judge Prompt（把工具结果传给 LLM）───────────────────
        agent_steps = inputs.get("agent_steps", 20)
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            memory_type                  = inputs["memory_type"],
            pipeline_complete            = tool_results["pipeline_complete"],
            wrapper_parsable             = tool_results["wrapper_parsable"],
            parse_errors                 = tool_results.get("parse_errors", "none"),
            expected_combos              = len(inputs["param_combos"]),
            found_combos                 = tool_results["found_combos"],
            missing_combos               = ", ".join(tool_results["missing_combos"]) or "none",
            per_kit_completeness         = self._format_kit_table(tool_results["per_kit"]),
            artifact_completeness        = f"{tool_results['artifact_completeness']:.3f}",
            total_combos                 = tool_results["total_combos"],
            zero_mismatch_count          = tool_results["zero_mismatch_count"],
            wrapper_interface_match      = f"{tool_results['wrapper_interface_match']:.3f}",
            per_combo_interface          = self._format_interface_table(tool_results["per_combo_interface"]),
            all_on_combo                 = inputs.get("all_on_combo", "N/A"),
            min_combo                    = inputs.get("min_combo", "N/A"),
            all_on_pass                  = tool_results["all_on_pass"],
            min_pass                     = tool_results["min_pass"],
            param_toggle_correctness     = f"{tool_results['param_toggle_correctness']:.3f}",
            all_on_details               = tool_results.get("all_on_details", ""),
            min_details                  = tool_results.get("min_details", ""),
            split_correctness            = f"{tool_results['split_correctness']:.3f}",
            address_coverage_ok          = tool_results["address_coverage_ok"],
            per_combo_split              = self._format_split_table(tool_results["per_combo_split"]),
            tiein_correctness            = f"{tool_results['tiein_correctness']:.3f}",
            tiein_details                = tool_results.get("tiein_details", ""),
            expected_families            = ", ".join(self.expected_families),
            built_families               = ", ".join(tool_results["built_families"]),
            mem_type_full_coverage       = f"{tool_results['mem_type_full_coverage']:.3f}",
            interface_spec_consistent    = tool_results["interface_spec_consistent"],
            interface_spec_consistency   = f"{tool_results['interface_spec_consistency']:.3f}",
            spec_violations              = self._format_violations(tool_results["spec_violations"]),
            total_linted                 = tool_results["total_linted"],
            clean_count                  = tool_results["clean_count"],
            lint_clean_rate              = f"{tool_results['lint_clean_rate']:.3f}",
            lint_warnings_summary        = tool_results.get("lint_warnings_summary", "N/A"),
            total_skill_docs             = tool_results["total_skill_docs"],
            skill_doc_appendix           = self._format_skill_docs(tool_results["skill_doc_contents"]),
        )

        # ── Step 4: LLM 解读 + 综合打分 ──────────────────────────────────────
        response = get_response_from_llm(
            msg          = prompt,
            system_msg   = SYSTEM_PROMPT,
            model        = self.model_config["model"],
            temperature  = 0.0,    # 评估要求确定性
            api_base     = self.model_config.get("api_base"),
            api_key      = self.model_config.get("api_key"),
            extra_headers = self.model_config.get("extra_headers"),
        )

        report = json.loads(response)

        # ── Step 5: 硬校验（防止 LLM 改动 Python 工具已确定的数值）────────────
        report = self._enforce_tool_values(report, tool_results, agent_steps)

        return report

    def _run_tools(self, inputs: dict) -> dict:
        """调用所有 Python eval 工具，返回客观测量结果。"""
        workspace  = inputs["workspace_dir"]
        mem_type   = inputs["memory_type"]
        combos     = inputs["param_combos"]
        bmodel_dir = inputs["behavior_model_dir"]
        kits       = inputs["expected_kits"]

        # 工具 1: pipeline 完成检查
        pipeline_complete = os.path.exists(os.path.join(workspace, "DONE.txt"))

        # 工具 2: artifact completeness
        art_result = compute_artifact_completeness(
            workspace_dir = workspace,
            memory_type   = mem_type,
            param_combos  = combos,
        )

        # 工具 3: wrapper interface match
        iface_result = check_wrapper_interface_match(
            workspace_dir      = workspace,
            memory_type        = mem_type,
            param_combos       = combos,
            behavior_model_dir = bmodel_dir,
        )

        # 工具 4: parameter toggle correctness（全开 / 全关）
        toggle_result = check_param_toggle(
            workspace_dir      = workspace,
            memory_type        = mem_type,
            all_on_combo       = inputs.get("all_on_combo", ""),
            min_combo          = inputs.get("min_combo", ""),
            behavior_model_dir = bmodel_dir,
        )

        # 工具 5: split correctness（宽度/深度非整除拆分）
        split_result = check_split_correctness(
            workspace_dir = workspace,
            memory_type   = mem_type,
            split_combos  = inputs.get("split_combos", []),
        )

        # 工具 6: lint check
        lint_result = run_lint_check(
            workspace_dir = workspace,
            memory_type   = mem_type,
            param_combos  = combos,
        )

        # 工具 7: mem_type_full_coverage（全 family 覆盖，all-or-nothing）
        all_types   = inputs.get("all_memory_types", self.expected_families)
        built_fam   = [mem_type] if pipeline_complete else []
        # 若 job 跑了 all_memory_types 中的所有 family，则覆盖率 = 1.0
        # 单次 eval 只跑一个 type；loop 会汇总多个 type 后计算综合分
        mem_full_cov = 1.0 if mem_type in all_types and pipeline_complete else 0.0

        # 工具 8: interface spec consistency（跨 family 命名一致性）
        ispec_result = check_interface_spec_consistency(
            workspace_dir     = workspace,
            memory_type_list  = inputs.get("all_memory_types", [mem_type]),
            combo_per_type    = inputs.get("combo_per_type", {mem_type: combos[0] if combos else ""}),
        )

        # tiein_correctness 来自 split_result 的 tiein_pass
        tiein_score = split_result["score"] if split_result["tiein_pass"] else 0.0
        # 若没有 split combos，tiein 无法检查，给满分（无测试用例 = 不扣分）
        if not inputs.get("split_combos"):
            tiein_score = 1.0

        # 工具 9: 收集 skill 文档内容（供 Judge LLM 直接阅读并评估中立性）
        skill_doc_result = read_skill_docs(
            workspace_dir = workspace,
            skill_dirs    = inputs.get("skill_dirs"),
        )

        return {
            "pipeline_complete":        pipeline_complete,
            "wrapper_parsable":         iface_result["wrapper_parsable"],
            "parse_errors":             iface_result.get("parse_errors", ""),
            "found_combos":             art_result["fully_complete_count"],
            "missing_combos":           [
                s["combo"] for s in art_result["per_combo_stats"] if not s["complete"]
            ],
            "per_kit":                  {
                s["combo"]: {f: f not in s["missing_files"] for f in kits}
                for s in art_result["per_combo_stats"]
            },
            "artifact_completeness":    art_result["score"],
            "total_combos":             iface_result["total_combos"],
            "zero_mismatch_count":      iface_result["zero_mismatch_count"],
            "wrapper_interface_match":  iface_result["score"],
            "per_combo_interface":      iface_result["per_combo_details"],
            "all_on_pass":              toggle_result["all_on_pass"],
            "min_pass":                 toggle_result["min_pass"],
            "param_toggle_correctness": toggle_result["score"],
            "all_on_details":           toggle_result["all_on_details"],
            "min_details":              toggle_result["min_details"],
            "split_correctness":        split_result["score"],
            "address_coverage_ok":      split_result["address_coverage_ok"],
            "per_combo_split":          split_result["per_combo_details"],
            "tiein_correctness":        tiein_score,
            "tiein_details":            "ok" if split_result["tiein_pass"] else "tie-off errors present",
            "total_linted":             lint_result["total_files_checked"],
            "clean_count":              lint_result["total_files_checked"] - lint_result["files_with_errors"],
            "lint_clean_rate":          lint_result["clean_file_ratio"],
            "lint_warnings_summary":    str(lint_result.get("details", ""))[:500],
            "built_families":              built_fam,
            "mem_type_full_coverage":      mem_full_cov,
            "interface_spec_consistent":   ispec_result["consistent"],
            "interface_spec_consistency":  ispec_result["score"],
            "spec_violations":             ispec_result["violations"],
            "skill_doc_contents":           skill_doc_result["files"],
            "total_skill_docs":            skill_doc_result["total_md_files"],
        }

    def _enforce_tool_values(self, report: dict, tool_results: dict, agent_steps: int) -> dict:
        """
        硬校验：Python 工具数值覆盖 LLM 可能偏离的数字。Ground truth 永远是 Python 层。
        """
        b  = report.setdefault("breakdown", {})
        l1 = b.setdefault("layer1", {})
        l2 = b.setdefault("layer2", {})

        l1["artifact_completeness"]    = round(tool_results["artifact_completeness"],    4)
        l1["wrapper_interface_match"]  = round(tool_results["wrapper_interface_match"],   4)
        l1["param_toggle_correctness"] = round(tool_results["param_toggle_correctness"],  4)
        l1["split_correctness"]        = round(tool_results["split_correctness"],          4)
        l1["tiein_correctness"]        = round(tool_results["tiein_correctness"],           4)
        l2["mem_type_full_coverage"]      = round(tool_results["mem_type_full_coverage"],     4)
        l2["interface_spec_consistency"]  = round(tool_results["interface_spec_consistency"], 4)
        l2["lint_clean_rate"]             = round(tool_results["lint_clean_rate"],             4)
        # vendor_agnostic_generality 是 Judge LLM 语义判断的结果，不从 Python tool 覆盖；
        # 只做边界 clamp，防止 LLM 输出越界。
        gen_score = float(l2.get("vendor_agnostic_generality", 1.0))
        l2["vendor_agnostic_generality"]  = round(max(0.0, min(1.0, gen_score)), 4)

        # 重计 score，忽略 LLM 算术
        s = (0.10 * l1["artifact_completeness"]
           + 0.20 * l1["wrapper_interface_match"]
           + 0.20 * l1["param_toggle_correctness"]
           + 0.15 * l1["split_correctness"]
           + 0.10 * l1["tiein_correctness"]
           + 0.10 * l2["mem_type_full_coverage"]
           + 0.05 * l2["interface_spec_consistency"]
           + 0.05 * l2["lint_clean_rate"]
           + 0.05 * l2["vendor_agnostic_generality"])
        report["score"]            = round(s, 4)
        report["passed_threshold"] = (report["score"] >= THRESHOLDS.get("target", 0.80))
        return report

    def _gate_fail_report(self, tool_results: dict) -> dict:
        reason = []
        if not tool_results["pipeline_complete"]:
            reason.append("pipeline did not complete (DONE.txt not found)")
        if not tool_results["wrapper_parsable"]:
            reason.append(f"wrapper parse error: {tool_results.get('parse_errors', 'unknown')}")
        return {
            "score": 0.0,
            "passed_threshold": False,
            "breakdown": {"layer0_gates": {"pipeline_complete": False}},
            "failure_analysis": [{"metric": "layer0_gate", "diagnosis": "; ".join(reason)}],
            "improvement_hints": [f"Fix gate failure first: {r}" for r in reason],
        }

    @staticmethod
    def _format_kit_table(per_kit: dict) -> str:
        lines = []
        for combo, kits in per_kit.items():
            missing = [k for k, v in kits.items() if not v]
            status = "OK" if not missing else f"MISSING: {', '.join(missing)}"
            lines.append(f"  {combo}: {status}")
        return "\n".join(lines) or "  (none produced)"

    @staticmethod
    def _format_interface_table(per_combo: list[dict]) -> str:
        lines = []
        for item in per_combo:
            if item["mismatch_count"] == 0:
                lines.append(f"  {item['combo']}: PASS")
            else:
                lines.append(f"  {item['combo']}: FAIL — {item['mismatch_details']}")
        return "\n".join(lines) or "  (no combos checked)"

    @staticmethod
    def _format_split_table(per_combo: list[dict]) -> str:
        lines = []
        for item in per_combo:
            status = "PASS" if item["pass"] else "FAIL"
            failing = [c["detail"] for c in item.get("checks", []) if not c["pass"]]
            detail = "; ".join(failing[:3]) if failing else ""
            lines.append(f"  {item['combo']}: {status}" + (f" — {detail}" if detail else ""))
        return "\n".join(lines) or "  (no split combos tested)"

    @staticmethod
    def _format_violations(violations: list[dict]) -> str:
        if not violations:
            return "  (none)"
        return "\n".join(f"  [{v['rule']}] {v['detail']}" for v in violations)

    @staticmethod
    def _format_skill_docs(files: dict[str, str]) -> str:
        """将 skill .md 文件内容格式化为 prompt 附录。"""
        if not files:
            return "(no skill documents found)"
        sections = []
        for path, content in files.items():
            sections.append(f"### {path}\n\n{content}")
        return "\n\n---\n\n".join(sections)
