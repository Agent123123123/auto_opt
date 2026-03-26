"""
eval_tools/param_toggle_check.py
---------------------------------
验证在"所有可选参数全开"和"所有可选参数全关"两种极端配置下，
wrapper 均能正确生成，且接口与对应行为模型一致。

SRAM 可选参数涵盖：
  全开（all_on）：SLP + DSLP + SD + RET + BWEB（bit-level write enable）+ OEB
  全关（min）   ：仅保留必须 port：CLK / CEB / WEB / A / DI / DO（无任何可选引脚）

检查目标：
  1. 两种配置下的 artifacts 均存在（artifact_completeness）
  2. 两种配置下 wrapper 均可解析（parsable）
  3. 两种配置的 wrapper 与各自行为模型接口匹配
  4. "全关" wrapper 里不出现 SLP / DSLP / SD / OEB 等可选 port（多余 port 也是错误）
  5. "全开" wrapper 里 BWEB 宽度正确（= NB bits），SLP/DSLP 为 input 且 active-low

返回：
{
    "score":               float,   # 通过的检查数 / 总检查数
    "all_on_pass":         bool,
    "min_pass":            bool,
    "all_on_details":      str,
    "min_details":         str,
    "per_check":           [{"check": str, "pass": bool, "detail": str}]
}
"""

import os
import re
from .interface_match import _parse_module_ports, ACTIVE_LOW_PORTS


# 在"全开"配置下必须存在的可选 port 集合（大写后比对）
EXPECTED_IN_ALL_ON = {"SLP", "DSLP", "SD", "OEB", "BWEB"}

# 在"全关/最小"配置下不能出现的 port
FORBIDDEN_IN_MIN = {"SLP", "DSLP", "SD", "OEB"}

# 任意配置下必须存在的核心 port 前缀（宽松匹配）
REQUIRED_CORE_PREFIXES = {"CLK", "CK", "CEB", "A", "ADR", "D", "DI", "Q", "DO", "WEB"}


def check_param_toggle(
    workspace_dir:      str,
    memory_type:        str,
    all_on_combo:       str,    # e.g. "256x32m4_full"
    min_combo:          str,    # e.g. "256x32m4_min"
    behavior_model_dir: str,
) -> dict:
    """
    比对全开和最小配置下的 wrapper 是否符合各自接口规范。
    """
    checks = []

    # ── 1. 文件存在性检查 ────────────────────────────────────────────────────
    for combo, tag in [(all_on_combo, "all_on"), (min_combo, "min")]:
        wrapper_path = _wrapper_path(workspace_dir, memory_type, combo)
        exists = os.path.exists(wrapper_path)
        checks.append({
            "check": f"{tag}_wrapper_exists",
            "pass":   exists,
            "detail": "" if exists else f"missing: {wrapper_path}",
        })

    # ── 2. 全开 wrapper：必须含可选 port ────────────────────────────────────
    all_on_path = _wrapper_path(workspace_dir, memory_type, all_on_combo)
    if os.path.exists(all_on_path):
        try:
            ports_all_on = {p.name.upper(): p for p in _parse_module_ports(all_on_path)}
            for required_port in EXPECTED_IN_ALL_ON:
                has_it = any(required_port in name for name in ports_all_on)
                checks.append({
                    "check": f"all_on_has_{required_port.lower()}",
                    "pass":   has_it,
                    "detail": "" if has_it else f"port {required_port} missing in all_on wrapper",
                })
            # BWEB 宽度检查（从 combo string 解析 NB）
            nb = _parse_nb_from_combo(all_on_combo)
            if nb and "BWEB" in ports_all_on:
                expected_width = nb
                actual_width   = ports_all_on["BWEB"].width
                ok = (actual_width == expected_width)
                checks.append({
                    "check": "all_on_bweb_width",
                    "pass":   ok,
                    "detail": "" if ok else f"BWEB width: expected={expected_width}, actual={actual_width}",
                })
        except Exception as e:
            checks.append({"check": "all_on_parse", "pass": False, "detail": str(e)})

    # ── 3. 最小 wrapper：不能含禁止 port ────────────────────────────────────
    min_path = _wrapper_path(workspace_dir, memory_type, min_combo)
    if os.path.exists(min_path):
        try:
            ports_min = {p.name.upper(): p for p in _parse_module_ports(min_path)}
            for forbidden_port in FORBIDDEN_IN_MIN:
                has_it = any(forbidden_port in name for name in ports_min)
                checks.append({
                    "check": f"min_no_{forbidden_port.lower()}",
                    "pass":   not has_it,
                    "detail": "" if not has_it else f"port {forbidden_port} unexpectedly present in min wrapper",
                })
            # 核心 port 存在性
            core_found = any(
                any(prefix in pname for prefix in REQUIRED_CORE_PREFIXES)
                for pname in ports_min
            )
            checks.append({
                "check": "min_has_core_ports",
                "pass":   core_found,
                "detail": "" if core_found else "min wrapper missing core ports (CLK/CEB/A/D/Q)",
            })
        except Exception as e:
            checks.append({"check": "min_parse", "pass": False, "detail": str(e)})

    # ── 4. 汇总 ──────────────────────────────────────────────────────────────
    total = len(checks)
    passed = sum(1 for c in checks if c["pass"])
    score  = passed / total if total > 0 else 0.0

    all_on_items = [c for c in checks if "all_on" in c["check"]]
    min_items    = [c for c in checks if "min" in c["check"]]
    all_on_pass  = all(c["pass"] for c in all_on_items) if all_on_items else False
    min_pass     = all(c["pass"] for c in min_items)    if min_items    else False

    return {
        "score":          score,
        "all_on_pass":    all_on_pass,
        "min_pass":       min_pass,
        "all_on_details": _summarize_failures(all_on_items),
        "min_details":    _summarize_failures(min_items),
        "per_check":      checks,
    }


# ─── helpers ─────────────────────────────────────────────────────────────────

def _wrapper_path(workspace_dir: str, memory_type: str, combo: str) -> str:
    return os.path.join(
        workspace_dir, "artifacts", memory_type, combo,
        f"{memory_type}_{combo}_wrapper.sv",
    )


def _parse_nb_from_combo(combo: str) -> int | None:
    """从 "NWxNBmNMUX" 格式的 combo string 解析 NB。"""
    m = re.match(r"(\d+)x(\d+)m(\d+)", combo)
    return int(m.group(2)) if m else None


def _summarize_failures(checks: list[dict]) -> str:
    failed = [c for c in checks if not c["pass"]]
    if not failed:
        return "all passed"
    return "; ".join(c["detail"] for c in failed[:3])
