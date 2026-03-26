"""
eval_tools/lint_check.py
------------------------
对 Task Agent 生成的所有 .sv wrapper 文件执行 svlinter 静态检查。

这里只关心 wrapper 文件（不检查行为模型）。
"""

import os
import subprocess
import re
from typing import NamedTuple


class LintResult(NamedTuple):
    warning_count: int
    error_count:   int
    blocked_files: list   # 有 error 的文件列表
    raw_output:    str


def run_lint_check(workspace_dir: str, memory_type: str, param_combos: list[str]) -> dict:
    """
    对所有产出的 wrapper .sv 文件运行 svlinter。

    返回：
    {
        "total_files_checked":  int,
        "files_with_errors":    int,
        "total_warnings":       int,
        "total_errors":         int,
        "clean_file_ratio":     float,  # (checked - with_errors) / checked
        "details":              [{"file": str, "warnings": int, "errors": int}]
    }
    """
    artifacts_root = os.path.join(workspace_dir, "artifacts", memory_type)
    sv_files = []

    for combo in param_combos:
        wrapper = os.path.join(artifacts_root, combo, f"{memory_type}_{combo}_wrapper.sv")
        if os.path.exists(wrapper):
            sv_files.append(wrapper)

    if not sv_files:
        return {
            "total_files_checked": 0,
            "files_with_errors":   0,
            "total_warnings":      0,
            "total_errors":        0,
            "clean_file_ratio":    1.0,
            "details":             [],
        }

    details          = []
    total_warnings   = 0
    total_errors     = 0
    files_with_errors = 0

    for sv_file in sv_files:
        result = _lint_one_file(sv_file)
        total_warnings   += result.warning_count
        total_errors     += result.error_count
        if result.error_count > 0:
            files_with_errors += 1
        details.append({
            "file":     os.path.basename(sv_file),
            "warnings": result.warning_count,
            "errors":   result.error_count,
        })

    checked = len(sv_files)
    clean_ratio = (checked - files_with_errors) / checked if checked > 0 else 1.0

    return {
        "total_files_checked": checked,
        "files_with_errors":   files_with_errors,
        "total_warnings":      total_warnings,
        "total_errors":        total_errors,
        "clean_file_ratio":    clean_ratio,
        "details":             details,
    }


def _lint_one_file(sv_path: str) -> LintResult:
    """对单个 .sv 文件运行 svlinter，解析其输出。"""
    try:
        proc = subprocess.run(
            ["svlint", sv_path],
            capture_output=True, text=True, timeout=30,
        )
        output = proc.stdout + proc.stderr
    except FileNotFoundError:
        # svlint 未安装，退化为 0 警告（不阻断流程）
        return LintResult(0, 0, [], "svlint not found, skipped")
    except subprocess.TimeoutExpired:
        return LintResult(0, 1, [sv_path], "lint timeout")

    warnings = len(re.findall(r"(?i)\bwarning\b", output))
    errors   = len(re.findall(r"(?i)\berror\b",   output))
    blocked  = [sv_path] if errors > 0 else []
    return LintResult(warnings, errors, blocked, output)
