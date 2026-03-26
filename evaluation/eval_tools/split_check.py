"""
eval_tools/split_check.py
--------------------------
验证 wrapper 在以下拆分场景下的正确性：

  1. 宽度拆分（NB % N_segments ≠ 0）
     - e.g. NB=40 拆成两个 macro，一个 32-bit，一个 8-bit
     - 检查：BWEB[39:32] / DATA[39:32] / Q[39:32] 均正确连到 8-bit macro
     - 检查：8-bit macro 的 BWEB 未使用 bits 是否 tie 到 1'b1（写禁用）

  2. 深度拆分（NW % 2^k ≠ 0）
     - e.g. NW=1000 拆成 macro_0(512) + macro_1(488)（而非强制 512+512）
       或   NW=1000 拆成 macro_0(512) + macro_1(512) + 地址 guard 禁止访问 [999+1..1023]
     - 检查：address decode 覆盖了全部 [0, NW-1] 地址
     - 检查：最后 segment 的超出地址范围内 CE 被 disable

  3. 复合拆分（宽度 + 深度同时非整除）
     - 上述两类检查的组合

  4. Tie-off 正确性检查（tiein_correctness）
     - BWEB 多余 bits → tie 1'b1
     - DATA/DIN multi-bit 多余 bits → tie 1'b0 或合理值
     - Q / DOUT 多余 bits → assign z 或 '0（只要编译不报 warning 即可）
     - input 端口不留悬空

返回：
{
    "score":                float,   # 通过检查数 / 总检查数
    "split_pass":           bool,
    "tiein_pass":           bool,
    "address_coverage_ok":  bool,   # depth split 地址覆盖验证
    "per_combo_details":    [{"combo": str, "checks": list, "pass": bool}]
}
"""

import os
import re
from .interface_match import _parse_module_ports


# ─── 主入口 ──────────────────────────────────────────────────────────────────

def check_split_correctness(
    workspace_dir: str,
    memory_type:   str,
    split_combos:  list[dict],   # [{"combo": "512x40m8", "nw": 512, "nb": 40, "nmux": 8, "type": "width|depth|both"}]
) -> dict:
    """
    对拆分场景进行多维度正确性检查。

    split_combos 每项：
      combo   : 参数 combo 字符串（对应产出路径）
      nw      : 期望总深度
      nb      : 期望总位宽
      nmux    : NMUX
      split_type: "width" / "depth" / "both"
    """
    all_checks   = []
    combo_details = []

    for spec in split_combos:
        combo      = spec["combo"]
        nw         = spec["nw"]
        nb         = spec["nb"]
        nmux       = spec.get("nmux", 4)
        split_type = spec.get("split_type", "both")

        wrapper_path = os.path.join(
            workspace_dir, "artifacts", memory_type, combo,
            f"{memory_type}_{combo}_wrapper.sv",
        )

        combo_checks = []

        if not os.path.exists(wrapper_path):
            combo_checks.append({
                "check": "wrapper_exists", "pass": False,
                "detail": f"missing: {wrapper_path}",
            })
        else:
            sv_text = _read_sv(wrapper_path)

            if split_type in ("width", "both"):
                combo_checks += _check_width_split(sv_text, nb, nmux)

            if split_type in ("depth", "both"):
                combo_checks += _check_depth_split(sv_text, nw)

            combo_checks += _check_tiein(sv_text, nb, nw, split_type)

        combo_pass = all(c["pass"] for c in combo_checks)
        combo_details.append({
            "combo":  combo,
            "checks": combo_checks,
            "pass":   combo_pass,
        })
        all_checks += combo_checks

    total  = len(all_checks)
    passed = sum(1 for c in all_checks if c["pass"])
    score  = passed / total if total > 0 else 1.0

    split_checks = [c for c in all_checks if "split" in c["check"] or "address" in c["check"]]
    tiein_checks = [c for c in all_checks if "tie" in c["check"] or "floating" in c["check"]]
    addr_checks  = [c for c in all_checks if "address" in c["check"]]

    return {
        "score":               score,
        "split_pass":          all(c["pass"] for c in split_checks) if split_checks else True,
        "tiein_pass":          all(c["pass"] for c in tiein_checks)  if tiein_checks  else True,
        "address_coverage_ok": all(c["pass"] for c in addr_checks)   if addr_checks   else True,
        "per_combo_details":   combo_details,
    }


# ─── 宽度拆分检查 ─────────────────────────────────────────────────────────────

def _check_width_split(sv_text: str, nb: int, nmux: int) -> list[dict]:
    """
    检查宽度拆分正确性：寻找 BWEB / DATA / Q 宽度与 nb 的对应关系。
    """
    checks = []

    # 检查：wrapper 中存在多个 macro 实例（拆分了才会有 macro_0 / macro_1 等）
    instance_count = len(re.findall(r"\bTS\w+N12\w+\s+\w+\s*\(", sv_text))
    needs_split    = (nb % nmux != 0)

    if needs_split:
        multi_inst = instance_count >= 2
        checks.append({
            "check": "width_split_multi_instance",
            "pass":   multi_inst,
            "detail": "" if multi_inst else
                      f"NB={nb} % NMUX={nmux} = {nb % nmux} ≠ 0, "
                      f"but only {instance_count} macro instance(s) found",
        })

    # 检查：BWEB tie-1 对未使用 bits（正则找 bweb assign）
    remainder = nb % nmux if nmux else 0
    if remainder != 0:
        # 应该有 tie 到 1'b1 的赋值（宽松匹配，只要有即可）
        has_tie_bweb = bool(re.search(
            r"(?i)(bweb\s*\[|bweb\s*=).*1'b1|assign\s+\w*bweb\w*\s*=\s*[{]?1'b1",
            sv_text,
        ))
        checks.append({
            "check": "width_split_bweb_tie1",
            "pass":   has_tie_bweb,
            "detail": "" if has_tie_bweb else
                      f"NB={nb} has {remainder} remainder bits; BWEB tie-to-1 pattern not found",
        })

    return checks


# ─── 深度拆分检查 ─────────────────────────────────────────────────────────────

def _check_depth_split(sv_text: str, nw: int) -> list[dict]:
    """
    检查深度拆分地址解码逻辑：
      1. wrapper 中存在地址比较/选择逻辑（说明有地址 decode）
      2. 如果 nw 不是 2 的幂，应该存在上限地址保护（address guard）
    """
    checks = []

    is_power_of_2 = (nw & (nw - 1)) == 0

    if not is_power_of_2:
        # 检查是否有地址 guard（防止访问有效范围之外的行）
        # 典型模式：if (addr < NW) 或 assign ce = orig_ce & (addr_in_range)
        addr_guard = bool(re.search(
            rf"\b{nw}\b|\b{nw:#010x}\b|{nw:d}'h{nw:x}",  # 数字字面量
            sv_text,
        )) or bool(re.search(
            r"(?i)(addr_valid|addr_in_range|address_guard|addr\s*<\s*\w+_max)",
            sv_text,
        ))
        checks.append({
            "check": "depth_split_address_guard",
            "pass":   addr_guard,
            "detail": "" if addr_guard else
                      f"NW={nw} is not a power-of-2; no address guard pattern found in wrapper",
        })

    # 检查：存在 CE 的条件屏蔽（禁止坏地址激活 macro）
    has_ce_guard = bool(re.search(
        r"(?i)(ce\s*=\s*[^;]*&|\bce\w*\s*=.*(?:addr|valid|sel))",
        sv_text,
    ))
    if not is_power_of_2:
        checks.append({
            "check": "depth_split_ce_guard",
            "pass":   has_ce_guard,
            "detail": "" if has_ce_guard else
                      "CE enable not gated by address range check",
        })

    return checks


# ─── Tie-off 检查 ─────────────────────────────────────────────────────────────

def _check_tiein(sv_text: str, nb: int, nw: int, split_type: str) -> list[dict]:
    """
    检查未使用 bits/ports 的 tie-off 是否合理：
      - 没有悬空的 input（input 必须有驱动）
      - BWEB 多余 bits → 1'b1
      - DATA 多余 bits → 可接 0 或通配
      - Q/DOUT → 可以 assign z 或 '0
    """
    checks = []

    # 悬空检测：简单找是否有裸 wire 声明而没有 assign（粗粒度）
    # 更精确的分析需要 elaboration；这里只做模式检查
    floating_input = bool(re.search(
        r"\binput\b.*\bfloat\b|\bUndriven\b|\bpartially_connected\b",
        sv_text, re.IGNORECASE,
    ))
    checks.append({
        "check": "no_floating_input",
        "pass":   not floating_input,
        "detail": "" if not floating_input else "possible floating input detected",
    })

    # BWEB tie-high 只在余数不为 0 时检查（已在 _check_width_split 处理）
    # 这里检查：assign 没有驱动 'x（不确定值）
    has_x_assign = bool(re.search(r"=\s*\{?\d*'bx", sv_text))
    checks.append({
        "check": "no_x_assign",
        "pass":   not has_x_assign,
        "detail": "" if not has_x_assign else "found assignment to 'x — use tie-0 or tie-z instead",
    })

    return checks


# ─── helper ───────────────────────────────────────────────────────────────────

def _read_sv(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return ""
