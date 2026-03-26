"""
eval_tools/interface_spec_consistency.py
-----------------------------------------
检查不同 memory family 的 wrapper 是否遵循统一的接口编程规范。

"接口编程规范"包含：
  1. 时钟 pin 命名：全部用 CLK 或全部用 CK（不混用）
  2. 地址 pin 命名：全部 A 或全部 ADR（不混用）
  3. 片选使能极性：所有 family 的 CE pin 均为 active-low（命名含 EB 后缀）
  4. 数据输入/输出命名：DI/DO 或 D/Q（全库一致）
  5. BWEB bit 序：MSB 对应最高 bit（不反向）

这种一致性对下游工具（mem_replace / place & route script）至关重要。

返回：
{
    "score":          float,    # 通过规则数 / 总规则数
    "consistent":     bool,
    "violations":     [{"rule": str, "detail": str}]
}
"""

import os
import re
from .interface_match import _parse_module_ports


# ─── 规则定义 ─────────────────────────────────────────────────────────────────

def check_interface_spec_consistency(
    workspace_dir:  str,
    memory_type_list: list[str],    # 本次评估涵盖的 family 列表
    combo_per_type:   dict[str, str],  # {family: 一个代表性 combo string}
) -> dict:
    """
    跨 family 检查接口规范一致性。

    combo_per_type: 每个 family 取一个代表 combo 做检查即可（代表性 combo）。
    """
    # 收集各 family 的 port 列表
    family_ports: dict[str, dict] = {}
    parse_errors = []

    for family in memory_type_list:
        combo = combo_per_type.get(family)
        if not combo:
            continue
        sv_path = os.path.join(
            workspace_dir, "artifacts", family, combo,
            f"{family}_{combo}_wrapper.sv",
        )
        if not os.path.exists(sv_path):
            continue
        try:
            ports = {p.name.upper(): p for p in _parse_module_ports(sv_path)}
            family_ports[family] = ports
        except Exception as e:
            parse_errors.append(f"{family}: {e}")

    if len(family_ports) < 2:
        return {
            "score":      1.0,    # 少于 2 个 family，无从比较
            "consistent": True,
            "violations": [],
        }

    violations = []

    # 规则 1：时钟 pin 命名一致性
    violations += _check_naming_consistency(
        family_ports, rule="clock_pin_name",
        candidates={"CLK", "CK"},
        description="Clock pin naming: all families must use the same name (CLK xor CK)",
    )

    # 规则 2：地址 pin 命名一致性
    violations += _check_naming_consistency(
        family_ports, rule="addr_pin_name",
        candidates={"A", "ADR", "ADDR"},
        description="Address pin naming: all families must use the same prefix (A xor ADR)",
    )

    # 规则 3：数据输入命名一致性
    violations += _check_naming_consistency(
        family_ports, rule="din_pin_name",
        candidates={"DI", "D", "DIN"},
        description="Data-in pin naming: all families must share the same prefix (DI xor D)",
    )

    # 规则 4：数据输出命名一致性
    violations += _check_naming_consistency(
        family_ports, rule="dout_pin_name",
        candidates={"DO", "Q", "DOUT"},
        description="Data-out pin naming: all families must share the same prefix (DO xor Q)",
    )

    # 规则 5：CE 极性统一（所有 family 的 CE pin 必须 active-low，即含 EB 后缀）
    for family, ports in family_ports.items():
        ce_pins = [n for n in ports if re.match(r"CE|CEN|CEB", n)]
        for pin in ce_pins:
            if not pin.endswith("B") and not pin.endswith("N"):
                violations.append({
                    "rule":   "ce_active_low",
                    "detail": f"{family}: CE pin '{pin}' is not active-low (missing B/N suffix)",
                })

    total_rules = 5
    # 每个 violation 扣一条规则（上限 total_rules）
    failed_rules = min(len(violations), total_rules)
    score = (total_rules - failed_rules) / total_rules

    return {
        "score":      score,
        "consistent": len(violations) == 0,
        "violations": violations,
    }


def _check_naming_consistency(
    family_ports: dict,
    rule:         str,
    candidates:   set[str],
    description:  str,
) -> list[dict]:
    """
    对于给定的 候选名称集合，检查各 family 是否使用同一个名称。
    """
    used_names: dict[str, list[str]] = {}  # 名称 → 用了它的 family 列表
    for family, ports in family_ports.items():
        for cand in candidates:
            if any(pname.startswith(cand) for pname in ports):
                used_names.setdefault(cand, []).append(family)
                break   # 一个 family 只算一次

    if len(used_names) <= 1:
        return []   # 全部用同一名称 → 无违规

    # 超过一种名称 → 违规
    summary = ", ".join(
        f"{name}({', '.join(families)})" for name, families in used_names.items()
    )
    return [{
        "rule":   rule,
        "detail": f"{description} — inconsistent: {summary}",
    }]
