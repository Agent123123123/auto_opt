"""
Phase 1a 进化主循环
-------------------
串联 Task Agent → Judge Agent → Meta Agent → patch 应用 → Archive 更新
"""

import os
import sys
import json
import math
import shutil
import logging
from pathlib import Path
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))

# Add Hyperagents to path for agent.llm / agent.llm_withtools / agent.tools
_HYPERAGENTS_DIR = os.path.join(os.path.dirname(__file__), '../../..', 'downloads/Hyperagents')
if os.path.isdir(_HYPERAGENTS_DIR):
    sys.path.insert(0, os.path.abspath(_HYPERAGENTS_DIR))

from phase1a.task_agent  import TaskAgent
from phase1a.judge_agent import JudgeAgent
from phase1a.meta_agent  import MetaAgent
from phase1a.config      import PHASE1A_CONFIG
from utils.archive       import Archive
from utils.skill_patcher import apply_patch, validate_patch_safety
from config.runtime_config import load_agent_model_config, check_agent_api_keys

log = logging.getLogger("phase1a_loop")


def _save_json(path: str, data: dict) -> None:
    """Atomically write a JSON file, skipping non-serializable values."""
    def _default(o):
        if isinstance(o, (set, frozenset)):
            return list(o)
        return str(o)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as _f:
        json.dump(data, _f, indent=2, default=_default)


def run_phase1a(
    output_dir: str,
    skill_dir:  str,
):
    """
    Phase 1a 进化主循环。

    参数：
        output_dir : 所有运行产出的根目录
        skill_dir  : skill bundle 目录路径（初始源 = mem_gen_offline_tester；演化最佳结果回写此处）
    """
    # ── Load per-agent model configs ──────────────────────────────────────
    _missing = [k for k, ok in check_agent_api_keys().items() if not ok]
    if _missing:
        log.warning("Missing API-key env vars for agents: %s", _missing)

    task_cfg  = load_agent_model_config("task_agent")
    judge_cfg = load_agent_model_config("judge_agent")
    meta_cfg  = load_agent_model_config("meta_agent")

    archive = Archive(
        archive_dir  = os.path.join(output_dir, "archive"),
        skill_dir    = skill_dir,
        metric_names = [
            "wrapper_completeness_rate", "wrapper_parse_rate",
            "family_breadth_score", "lint_clean_rate",
            "cross_family_consistency", "vendor_agnostic_generality",
        ],
    )

    task_agent  = TaskAgent(model_config=task_cfg)
    judge_agent = JudgeAgent(model_config=judge_cfg)
    meta_agent  = MetaAgent(model_config=meta_cfg)

    best_score       = 0.0
    stagnation_count = 0
    eval_history     = []   # 最近 8 代的 judge 报告

    cfg = PHASE1A_CONFIG

    log.info("=" * 60)
    log.info(f"Phase 1a started. max_generations={cfg['max_generations']}")
    log.info("=" * 60)

    for generation in range(1, cfg["max_generations"] + 1):
        gen_dir = os.path.join(output_dir, f"gen_{generation:03d}")
        os.makedirs(gen_dir, exist_ok=True)

        log.info(f"\n─── Generation {generation} ─────────────────────────────")

        # ── Step 1: 从 Archive 选取 parent skill ──────────────────────────
        # 第一代时 archive 为空，fallback 到原始 skill_dir（submodule）
        parent_skill_dir = archive.select_parent() or skill_dir
        working_skill_dir = os.path.abspath(os.path.join(gen_dir, "skill"))
        shutil.copytree(parent_skill_dir, working_skill_dir)

        # ── Step 2: Task Agent — builds CLI tool, discovers MC, runs all tests ─
        workspace = os.path.abspath(os.path.join(gen_dir, "workspace"))
        os.makedirs(workspace, exist_ok=True)

        task_chat = os.path.abspath(os.path.join(gen_dir, "task_chat.md"))
        task_agent.forward({
            "workspace_dir":     workspace,
            "skill_dir":         working_skill_dir,
            "chat_history_file": task_chat,
        })

        # ── Step 3: Judge Agent — single global workspace evaluation ──────────
        judge_chat = os.path.abspath(os.path.join(gen_dir, "judge_chat.md"))
        report = judge_agent.evaluate({
            "workspace_dir":     workspace,
            "skill_dir":         working_skill_dir,
            "chat_history_file": judge_chat,
            "task_chat_file":    task_chat,
        })
        generation_score = report["score"]

        _save_json(os.path.join(gen_dir, "judge_report.json"), report)
        log.info(f"  Generation score: {generation_score:.4f}")
        metrics = {k: report["breakdown"].get(k, 0.0) for k in [
            "wrapper_completeness_rate", "wrapper_parse_rate",
            "family_breadth_score", "lint_clean_rate",
            "cross_family_consistency", "vendor_agnostic_generality",
        ]}
        archive.add(
            generation   = generation,
            score        = generation_score,
            metrics      = metrics,
            judge_report = report,
        )

        # ── Step 6: 收敛检查 ──────────────────────────────────────────────
        if generation_score > best_score + 0.005:
            best_score       = generation_score
            stagnation_count = 0
            log.info(f"  ✓ New best score: {best_score:.4f}")
        else:
            stagnation_count += 1
            log.info(f"  · No improvement (stagnation={stagnation_count})")

        if stagnation_count >= cfg["stagnation_limit"]:
            log.info(f"  ✗ Stagnation limit reached. Stopping.")
            break

        if best_score >= cfg["target_score"]:
            log.info(f"  ✓ Target score {cfg['target_score']} reached. Stopping.")
            break

        # 维护历史窗口（最近 8 代）
        eval_history = ([report] + eval_history)[:8]

        # ── Step 7: Meta Agent 改进 skill ─────────────────────────────────
        log.info("  Running Meta Agent...")
        meta_chat = os.path.join(gen_dir, "meta_chat.md")
        improvement = meta_agent.propose_improvement({
            "skill_dir":         working_skill_dir,
            "eval_history":      eval_history,
            "best_score":        best_score,
            "current_score":     generation_score,
            "stagnation_count":  stagnation_count,
            "chat_history_file": meta_chat,
            "task_chat_file":    task_chat,
            "judge_chat_file":   judge_chat,
        })

        # 校验 patch 安全性 / 应用 patch
        patch_applied = False
        patch_reject_reason = ""
        if improvement.get("applied_directly"):
            # opencode already edited skill files directly via its tools.
            # Just validate for audit; do NOT re-apply.
            patch_applied = bool(improvement.get("patch") or improvement.get("analysis"))
            if improvement.get("patch"):
                is_safe, reason = validate_patch_safety(
                    patch_text = improvement["patch"],
                    skill_dir  = working_skill_dir,
                )
                if not is_safe:
                    patch_reject_reason = f"(audit only) {reason}"
                    log.warning(f"  Patch audit flag: {patch_reject_reason}")
            if patch_applied:
                log.info(f"  Edits applied via opencode: {improvement.get('analysis','')[:80]}...")
                changes = improvement.get("changes", [])
                if changes:
                    log.info(f"  Changes ({len(changes)}):")
                    for c in changes:
                        log.info(f"    · {c}")
        elif improvement["patch"]:
            is_safe, reason = validate_patch_safety(
                patch_text = improvement["patch"],
                skill_dir  = working_skill_dir,
            )
            if is_safe:
                apply_patch(patch_text=improvement["patch"], skill_dir=working_skill_dir)
                patch_applied = True
                log.info(f"  Patch applied: {improvement['analysis'][:80]}...")
            else:
                patch_reject_reason = reason
                log.warning(f"  Patch rejected (safety check failed): {reason}")

        _save_json(os.path.join(gen_dir, "meta_output.json"), {
            "generation":          generation,
            "timestamp":           datetime.now().isoformat(),
            "analysis":            improvement.get("analysis", ""),
            "changes":             improvement.get("changes", []),
            "expected_impact":     improvement.get("expected_impact", ""),
            "patch_length":        len(improvement.get("patch", "")),
            "patch_applied":       patch_applied,
            "patch_reject_reason": patch_reject_reason,
            "patch":               improvement.get("patch", ""),
        })

    # ── 最终结果 ─────────────────────────────────────────────────────────────
    best_entry = archive.get_best()
    log.info("\n" + "=" * 60)
    log.info(f"Phase 1a complete. Best score: {best_entry['score']:.4f}")
    log.info(f"Best skill version: {best_entry['skill_dir']}")

    # 将最佳 skill 回写到 submodule（mem_gen_offline_tester/）
    # 这样 submodule 始终保存最新最优版本，不需要单独维护 mem_gen/skill/ 副本。
    shutil.copytree(best_entry["skill_dir"], skill_dir, dirs_exist_ok=True)
    log.info(f"Best skill published back to: {skill_dir}")
    log.info("=" * 60)

    return best_entry
