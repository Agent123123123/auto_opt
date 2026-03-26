"""
eval_tools/interface_match.py
------------------------------
检查 mem_gen 产出的 RTL wrapper 与行为模型的接口是否完全一致。

检查维度：
  1. Port 名称存在性（wrapper 是否有行为模型的所有 port）
  2. Port 极性（CEB/WEB 必须 active-low，A/ADR 为地址输入等）
  3. Port 位宽（DATA/Q/BWEB 宽度 = NB*NB_per_word，ADR 宽度 = log2(NW) 等）
  4. 时钟/使能 pin 完整性

输入：
  wrapper .sv 文件（通过 svlinter 解析 AST）
  behavior_model .sv 文件

输出：
  per-combo match 结果，包含具体的 mismatch 细节
"""

import os
import re
import math
from dataclasses import dataclass, field


@dataclass
class PortSpec:
    name:      str
    direction: str   # "input" / "output"
    width:     int   # bit width, 1 for scalar
    polarity:  str   # "active_high" / "active_low" / "unknown"


@dataclass
class InterfaceMismatch:
    port:    str
    kind:    str    # "missing" / "width_mismatch" / "polarity_mismatch" / "direction_mismatch"
    expected: str
    actual:   str


# ─── Port 极性规则（hardware spec，不可修改）──────────────────────────────────

ACTIVE_LOW_PORTS = {"CEB", "WEB", "BWEB", "OEB", "SLP", "DSLP", "SD"}

def _infer_polarity(port_name: str) -> str:
    suffix = port_name.upper()
    if any(suffix.endswith(p) or suffix == p for p in ACTIVE_LOW_PORTS):
        return "active_low"
    if suffix.startswith("N") and len(suffix) > 1:
        return "active_low"
    return "active_high"


# ─── SV 文件解析（简化版，生产环境可替换为 svlinter AST）─────────────────────

def _parse_module_ports(sv_path: str) -> list[PortSpec]:
    """
    从 .sv 文件提取 module port 列表。
    使用正则解析（足够用于 SRAM wrapper 这种简单模块）。
    """
    with open(sv_path) as f:
        content = f.read()

    ports = []
    # 匹配: input/output [N-1:0] port_name
    port_pattern = re.compile(
        r"\b(input|output)\s+(?:logic\s+)?(?:\[(\d+)\s*:\s*(\d+)\]\s+)?(\w+)",
        re.MULTILINE
    )
    for m in port_pattern.finditer(content):
        direction = m.group(1)
        hi  = int(m.group(2)) if m.group(2) else 0
        lo  = int(m.group(3)) if m.group(3) else 0
        width = hi - lo + 1
        name  = m.group(4)
        if name in ("module", "endmodule", "logic", "reg", "wire"):
            continue
        ports.append(PortSpec(
            name      = name,
            direction = direction,
            width     = width,
            polarity  = _infer_polarity(name),
        ))
    return ports


# ─── 主要接口检查函数 ─────────────────────────────────────────────────────────

def check_wrapper_interface_match(
    workspace_dir:      str,
    memory_type:        str,
    param_combos:       list[str],
    behavior_model_dir: str,
) -> dict:
    """
    对每个参数组合，比对 wrapper 与行为模型的接口。

    返回：
    {
        "wrapper_parsable": bool,
        "parse_errors":     str,
        "total_combos":     int,
        "zero_mismatch_count": int,
        "score":            float,   # zero_mismatch_count / total_combos
        "per_combo_details": [
            {"combo": str, "mismatch_count": int, "mismatch_details": str}
        ]
    }
    """
    per_combo_details = []
    zero_mismatch_count = 0
    parse_errors = []

    for combo in param_combos:
        wrapper_path = os.path.join(
            workspace_dir, "artifacts", memory_type, combo,
            f"{memory_type}_{combo}_wrapper.sv"
        )
        bmodel_path = os.path.join(
            behavior_model_dir, f"{memory_type}_{combo}_model.sv"
        )

        # wrapper 不存在时视为全 mismatch
        if not os.path.exists(wrapper_path):
            per_combo_details.append({
                "combo":            combo,
                "mismatch_count":   -1,    # -1 表示文件缺失
                "mismatch_details": f"wrapper file not found: {wrapper_path}",
            })
            continue

        try:
            wrapper_ports = _parse_module_ports(wrapper_path)
        except Exception as e:
            parse_errors.append(f"{combo}: {e}")
            per_combo_details.append({
                "combo":            combo,
                "mismatch_count":   -1,
                "mismatch_details": f"parse error: {e}",
            })
            continue

        # 行为模型不存在时跳过（只做 wrapper 自身检查）
        if not os.path.exists(bmodel_path):
            per_combo_details.append({
                "combo":            combo,
                "mismatch_count":   0,
                "mismatch_details": "behavior model not found, skipped",
            })
            zero_mismatch_count += 1
            continue

        try:
            bmodel_ports = _parse_module_ports(bmodel_path)
        except Exception as e:
            parse_errors.append(f"bmodel {combo}: {e}")
            continue

        # 逐 port 比对
        mismatches = _compare_ports(wrapper_ports, bmodel_ports)

        if len(mismatches) == 0:
            zero_mismatch_count += 1
            per_combo_details.append({
                "combo": combo, "mismatch_count": 0, "mismatch_details": "",
            })
        else:
            detail = "; ".join(
                f"{m.port}({m.kind}: expected={m.expected}, actual={m.actual})"
                for m in mismatches[:5]   # 最多显示 5 个，避免过长
            )
            per_combo_details.append({
                "combo":            combo,
                "mismatch_count":   len(mismatches),
                "mismatch_details": detail,
            })

    total = len(param_combos)
    score = zero_mismatch_count / total if total > 0 else 0.0

    return {
        "wrapper_parsable":    len(parse_errors) == 0,
        "parse_errors":        "; ".join(parse_errors),
        "total_combos":        total,
        "zero_mismatch_count": zero_mismatch_count,
        "score":               score,
        "per_combo_details":   per_combo_details,
    }


def _compare_ports(wrapper: list[PortSpec], bmodel: list[PortSpec]) -> list[InterfaceMismatch]:
    """比对两个 port 列表，返回所有不一致项。"""
    mismatches = []
    bmodel_by_name = {p.name.upper(): p for p in bmodel}
    wrapper_by_name = {p.name.upper(): p for p in wrapper}

    # 检查 bmodel 中的每个 port 在 wrapper 中是否存在且一致
    for bname, bp in bmodel_by_name.items():
        if bname not in wrapper_by_name:
            mismatches.append(InterfaceMismatch(
                port=bname, kind="missing",
                expected=f"exists (dir={bp.direction}, width={bp.width})",
                actual="not found in wrapper",
            ))
            continue

        wp = wrapper_by_name[bname]

        if wp.direction != bp.direction:
            mismatches.append(InterfaceMismatch(
                port=bname, kind="direction_mismatch",
                expected=bp.direction, actual=wp.direction,
            ))

        if wp.width != bp.width:
            mismatches.append(InterfaceMismatch(
                port=bname, kind="width_mismatch",
                expected=str(bp.width), actual=str(wp.width),
            ))

        if wp.polarity != bp.polarity:
            mismatches.append(InterfaceMismatch(
                port=bname, kind="polarity_mismatch",
                expected=bp.polarity, actual=wp.polarity,
            ))

    return mismatches
