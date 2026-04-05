#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_phase1a.sh  —  Phase 1a 启动脚本
#
# 功能：
#   1. 加载 MC2 工具链环境（module load mc2_n12/2013.12）
#   2. 将 mc 二进制路径写入环境变量 MC_PATH
#   3. 调用 Python evaluation loop
#
# 用法：
#   bash run_phase1a.sh [output_dir]
#
# 参数：
#   output_dir  : 可选，默认为 ./outputs（相对于脚本所在目录）
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${1:-${SCRIPT_DIR}/outputs}"
SKILL_DIR="${SCRIPT_DIR}/mem_gen_offline_tester"

echo "=========================================="
echo "  Phase 1a Launcher"
echo "  output_dir : ${OUTPUT_DIR}"
echo "  skill_dir  : ${SKILL_DIR}"
echo "=========================================="

# ── Step 1: Load MC2 environment ─────────────────────────────────────────────
if command -v module &>/dev/null; then
    echo "[env] module command found, loading mc2_n12/2013.12 ..."
    module load mc2_n12/2013.12 2>&1 || {
        echo "[warn] module load mc2_n12/2013.12 failed — proceeding without it"
    }
else
    echo "[warn] 'module' command not available on this shell"
    # Attempt to source the module init script directly (common locations)
    for _mod_init in /usr/share/Modules/init/bash \
                     /etc/profile.d/modules.sh \
                     /usr/local/Modules/init/bash \
                     /opt/modules/init/bash; do
        if [ -f "${_mod_init}" ]; then
            # shellcheck disable=SC1090
            source "${_mod_init}" && module load mc2_n12/2013.12 2>&1 && break || true
        fi
    done
fi

# ── Step 2: Resolve mc binary path ───────────────────────────────────────────
# Try common binary names for the MC2 compiler
MC_PATH=""
for _bin in mc2-eu mc2 mc2_n12 memory_compiler mc; do
    if _found="$(command -v "${_bin}" 2>/dev/null)"; then
        MC_PATH="${_found}"
        echo "[env] MC binary found: ${MC_PATH}"
        break
    fi
done

if [ -z "${MC_PATH}" ]; then
    echo "[warn] MC2 binary not found in PATH after module load"
    echo "[warn] Will try to locate via 'find' in common install prefixes ..."
    for _prefix in /eda /tools /opt /usr/local; do
        if _found="$(find "${_prefix}" -name 'mc2' -type f 2>/dev/null | head -1)"; then
            if [ -n "${_found}" ]; then
                MC_PATH="${_found}"
                echo "[env] Found MC binary via find: ${MC_PATH}"
                break
            fi
        fi
    done
fi

if [ -z "${MC_PATH}" ]; then
    echo "[error] Cannot locate MC2 binary. Phase 1a cannot run without it."
    echo "        Please ensure mc2_n12/2013.12 is loaded or set MC_PATH manually."
    exit 1
fi

export MC_PATH

# ── Step 3: Activate Python environment ──────────────────────────────────────
PYTHON_BIN="/opt/miniconda3/envs/agent_env/bin/python"
if [ ! -x "${PYTHON_BIN}" ]; then
    # Fallback: use whatever python3 is in PATH
    PYTHON_BIN="$(command -v python3)"
    echo "[warn] Default Python not found, using: ${PYTHON_BIN}"
fi

echo "[env] Python  : ${PYTHON_BIN}"
echo "[env] MC_PATH : ${MC_PATH}"
echo "=========================================="

# ── Step 4: Launch evaluation loop ───────────────────────────────────────────
cd "${SCRIPT_DIR}"

exec "${PYTHON_BIN}" - <<'PYEOF'
import sys, os, logging

# Make sure evaluation modules are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in dir() else os.getcwd()
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'evaluation'))

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt = "%H:%M:%S",
)

from evaluation.phase1a.loop import run_phase1a

output_dir = os.environ.get("OUTPUT_DIR", os.path.join(SCRIPT_DIR, "outputs"))
skill_dir  = os.path.join(SCRIPT_DIR, "mem_gen_offline_tester")

result = run_phase1a(
    output_dir = output_dir,
    skill_dir  = skill_dir,
)

print(f"\n✓ Phase 1a complete. Best score: {result['score']:.4f}")
print(f"  Best skill dir: {result['skill_dir']}")
PYEOF
