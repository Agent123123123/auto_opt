"""
eval_tools/compute_metrics.py
------------------------------
计算 mem_gen Task Agent 产出的 artifact 完整性。

"完整性"定义：
  对于每个期望的 (memory_type, param_combo) 组合，
  以下文件是否全部存在：
    - <combo>/<mem_type>_<combo>_wrapper.sv
    - <combo>/<mem_type>_<combo>_wrapper.f       (filelist)
    - <combo>/<mem_type>_<combo>_model.sv        (behavior model)
    - <combo>/DONE.txt                         (完成标志)

返回 artifact_completeness_score = fully_complete_combos / total_expected_combos

同时返回：
  - missing_per_combo: 每个 combo 缺失了哪些文件
  - fully_complete_count / partial_count / zero_count
"""

import os


REQUIRED_SUFFIXES = [
    "_wrapper.sv",
    "_wrapper.f",
    "_model.sv",
    "DONE.txt",          # 不带前缀
]


def compute_artifact_completeness(
    workspace_dir: str,
    memory_type:   str,
    param_combos:  list[str],
) -> dict:
    """
    检查所有参数组合的产出文件完整性。

    返回：
    {
        "total_combos":        int,
        "fully_complete_count": int,
        "partial_count":       int,
        "zero_count":          int,
        "score":               float,   # fully_complete_count / total_combos
        "per_combo_stats":     [
            {"combo": str, "missing_files": [str], "complete": bool}
        ]
    }
    """
    artifacts_root = os.path.join(workspace_dir, "artifacts", memory_type)

    per_combo_stats   = []
    fully_complete    = 0
    partial           = 0
    zero              = 0

    for combo in param_combos:
        combo_dir = os.path.join(artifacts_root, combo)
        missing   = []

        for suffix in REQUIRED_SUFFIXES:
            if suffix == "DONE.txt":
                fpath = os.path.join(combo_dir, "DONE.txt")
            else:
                fpath = os.path.join(combo_dir, f"{memory_type}_{combo}{suffix}")

            if not os.path.exists(fpath):
                missing.append(os.path.basename(fpath))

        found = len(REQUIRED_SUFFIXES) - len(missing)
        complete = (len(missing) == 0)

        if complete:
            fully_complete += 1
        elif found == 0:
            zero += 1
        else:
            partial += 1

        per_combo_stats.append({
            "combo":         combo,
            "missing_files": missing,
            "complete":      complete,
        })

    total  = len(param_combos)
    score  = fully_complete / total if total > 0 else 0.0

    return {
        "total_combos":         total,
        "fully_complete_count": fully_complete,
        "partial_count":        partial,
        "zero_count":           zero,
        "score":                score,
        "per_combo_stats":      per_combo_stats,
    }
