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
from phase1a.config      import (
    PHASE1A_CONFIG, EVAL_COMBOS, EXPECTED_FAMILIES,
    PARAM_TOGGLE_COMBOS, SPLIT_COMBOS, REPRESENTATIVE_COMBO,
)
from utils.archive       import Archive
from utils.skill_patcher import apply_patch, validate_patch_safety
from config.runtime_config import load_agent_model_config, check_agent_api_keys

log = logging.getLogger("phase1a_loop")

# ──────────────────────────────────────────────────────────────────────────────
# 测试集定义（固定，非进化目标）
# ──────────────────────────────────────────────────────────────────────────────

TEST_CASES = {
    # Quick Eval（每代必跑）
    "quick": [
        {
            "memory_type":       "spsram_ulvt",
            "param_combos":      EVAL_COMBOS["spsram_ulvt"],
            "behavior_model_dir": "test_fixtures/behavior_models/spsram_ulvt/",
            "all_on_combo":      PARAM_TOGGLE_COMBOS["spsram_ulvt"]["all_on"],
            "min_combo":         PARAM_TOGGLE_COMBOS["spsram_ulvt"]["min"],
            "split_combos":      SPLIT_COMBOS.get("spsram_ulvt", [])[:2],   # quick：只跑前两个
            "all_memory_types":  EXPECTED_FAMILIES,
            "combo_per_type":    REPRESENTATIVE_COMBO,
        },
        {
            "memory_type":       "2prf_lvt",
            "param_combos":      EVAL_COMBOS["2prf_lvt"],
            "behavior_model_dir": "test_fixtures/behavior_models/2prf_lvt/",
            "all_on_combo":      PARAM_TOGGLE_COMBOS["2prf_lvt"]["all_on"],
            "min_combo":         PARAM_TOGGLE_COMBOS["2prf_lvt"]["min"],
            "split_combos":      [],  # 2prf quick eval 不测拆分
            "all_memory_types":  EXPECTED_FAMILIES,
            "combo_per_type":    REPRESENTATIVE_COMBO,
        },
    ],
    # Full Eval（通过初筛才跑）
    "full": [
        {
            "memory_type":       "uhd1prf_svt",
            "param_combos":      EVAL_COMBOS["uhd1prf_svt"],
            "behavior_model_dir": "test_fixtures/behavior_models/uhd1prf_svt/",
            "all_on_combo":      "",    # uhd1prf 暂无 toggle 定义
            "min_combo":         "",
            "split_combos":      SPLIT_COMBOS.get("spsram_ulvt", []),  # 用 spsram 拆分组合复测
            "all_memory_types":  EXPECTED_FAMILIES,
            "combo_per_type":    REPRESENTATIVE_COMBO,
        },
        {
            "memory_type":       "dpsram_lvt",
            "param_combos":      EVAL_COMBOS["dpsram_lvt"],
            "behavior_model_dir": "test_fixtures/behavior_models/dpsram_lvt/",
            "all_on_combo":      "",
            "min_combo":         "",
            "split_combos":      [],
            "all_memory_types":  EXPECTED_FAMILIES,
            "combo_per_type":    REPRESENTATIVE_COMBO,
        },
    ],
}

QUICK_EVAL_WEIGHT = PHASE1A_CONFIG["staged_eval_fraction"]  # e.g. 0.6
FULL_EVAL_WEIGHT  = 1.0 - QUICK_EVAL_WEIGHT


def run_phase1a(
    mc_path:    str,
    output_dir: str,
    skill_dir:  str,
):
    """
    Phase 1a 进化主循环。

    参数：
        mc_path    : MC 编译器路径（或 mock_mc.py 路径）
        output_dir : 所有运行产出的根目录
        skill_dir  : skill bundle 目录路径（初始源 = mem_gen_offline_tester；演化最佳结果回写此处）

    每个 Agent 所用的模型在 config/runtime_config.yaml 中独立配置。
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
            "artifact_completeness", "wrapper_interface_match",
            "param_toggle_correctness", "split_correctness",
            "tiein_correctness", "mem_type_full_coverage",
            "interface_spec_consistency", "lint_clean_rate",
        ],
    )

    task_agent  = TaskAgent(model_config=task_cfg)
    judge_agent = JudgeAgent(model_config=judge_cfg, expected_families=EXPECTED_FAMILIES)
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
        working_skill_dir = os.path.join(gen_dir, "skill")
        shutil.copytree(parent_skill_dir, working_skill_dir)

        # ── Step 2: Task Agent 执行（Quick Eval 测试集）──────────────────
        quick_scores    = []
        quick_reports   = []

        for tc in TEST_CASES["quick"]:
            workspace = os.path.join(gen_dir, "workspace", tc["memory_type"])
            os.makedirs(workspace, exist_ok=True)

            _, history = task_agent.forward({
                "memory_type":   tc["memory_type"],
                "mc_path":       mc_path,
                "workspace_dir": workspace,
                "param_combos":  tc["param_combos"],
                "skill_dir":     working_skill_dir,
            })
            agent_steps = len(history)

            # ── Step 3: Judge Agent 评估 ──────────────────────────────────
            report = judge_agent.evaluate({
                "workspace_dir":      workspace,
                "memory_type":        tc["memory_type"],
                "param_combos":       tc["param_combos"],
                "behavior_model_dir": tc["behavior_model_dir"],
                "expected_kits":      ["sv", "ds"],
                "agent_steps":        agent_steps,
            })

            log.info(f"  Quick eval [{tc['memory_type']}]: score={report['score']:.4f}")
            quick_scores.append(report["score"])
            quick_reports.append(report)

        quick_avg = sum(quick_scores) / len(quick_scores)

        # ── Step 4: Full Eval（仅当 quick_avg 超过初筛线）───────────────
        full_avg     = 0.0
        full_reports = []

        if quick_avg >= cfg.get("quick_threshold", 0.50):
            for tc in TEST_CASES["full"]:
                workspace = os.path.join(gen_dir, "workspace", tc["memory_type"])
                os.makedirs(workspace, exist_ok=True)

                _, history = task_agent.forward({
                    "memory_type":   tc["memory_type"],
                    "mc_path":       mc_path,
                    "workspace_dir": workspace,
                    "param_combos":  tc["param_combos"],
                    "skill_dir":     working_skill_dir,
                })

                report = judge_agent.evaluate({
                    "workspace_dir":      workspace,
                    "memory_type":        tc["memory_type"],
                    "param_combos":       tc["param_combos"],
                    "behavior_model_dir": tc["behavior_model_dir"],
                    "expected_kits":      ["sv", "ds"],
                    "agent_steps":        len(history),
                })

                log.info(f"  Full eval  [{tc['memory_type']}]: score={report['score']:.4f}")
                full_reports.append(report)

            full_avg = sum(r["score"] for r in full_reports) / len(full_reports)
        else:
            log.info(f"  Skipping full eval (quick_avg={quick_avg:.4f} < threshold)")

        # ── Step 5: 综合评分，更新 Archive ────────────────────────────────
        generation_score = (QUICK_EVAL_WEIGHT * quick_avg
                          + FULL_EVAL_WEIGHT  * full_avg)

        log.info(f"  Generation score: {generation_score:.4f} "
                 f"(quick={quick_avg:.4f}, full={full_avg:.4f})")

        all_reports = quick_reports + full_reports
        archive.add_entry(
            skill_dir = working_skill_dir,
            score     = generation_score,
            reports   = all_reports,
            generation = generation,
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
        eval_history = (all_reports + eval_history)[:8]

        # ── Step 7: Meta Agent 改进 skill ─────────────────────────────────
        log.info("  Running Meta Agent...")
        improvement = meta_agent.propose_improvement({
            "skill_dir":        working_skill_dir,
            "eval_history":     eval_history,
            "best_score":       best_score,
            "current_score":    generation_score,
            "stagnation_count": stagnation_count,
        })

        # 校验 patch 安全性（确保 patch 路径不逃出 working_skill_dir）
        if improvement["patch"]:
            is_safe, reason = validate_patch_safety(
                patch_text = improvement["patch"],
                skill_dir  = working_skill_dir,
            )
            if is_safe:
                apply_patch(patch_text=improvement["patch"], skill_dir=working_skill_dir)
                log.info(f"  Patch applied: {improvement['analysis'][:80]}...")
            else:
                log.warning(f"  Patch rejected (safety check failed): {reason}")

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
