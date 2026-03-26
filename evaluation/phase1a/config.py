"""
Phase 1a 配置：mem_gen 独立优化
"""

# ─── 演化超参 ─────────────────────────────────────────────────────────────────

PHASE1A_CONFIG = {
    # 最大演化代数
    "max_generations":    40,

    # 连续 N 代无进步则视为停滞（触发 meta agent 激进变异）
    "stagnation_limit":   8,

    # 达到此分数即认为收敛，提前终止
    "target_score":       0.80,

    # quick eval 分数占总分权重（staged_eval_fraction）
    "staged_eval_fraction": 0.6,

    # quick eval 平均分达到此阈值才触发 full eval
    "quick_threshold":    0.50,

    # judge 和 meta 使用的历史窗口（最近 N 代）
    "history_window":     8,

    # Task Agent 最大步数
    "task_agent_max_steps": 40,

    # skill bundle 目录（meta agent 只能修改这里的文件）
    # 直接使用 submodule 作为演化源；最佳结果也回写到此处。
    "skill_dir": "mem_gen_offline_tester",

    # workspace 临时目录前缀
    "workspace_prefix":   "/tmp/mem_gen_eval",
}

# ─── 每种 memory type 的标准 eval 参数组合 ──────────────────────────────────
#
# 格式：f"{NW}x{NB}m{NB_per_word}"，与 MC compiler 命名约定一致。

EVAL_COMBOS = {
    # Quick eval（少量 combo，速度优先）
    "spsram_ulvt": [
        "256x32m4",
        "1024x64m8",
    ],
    "2prf_lvt": [
        "256x32m4",
        "1024x64m8",
    ],
    # Full eval（覆盖边界情况）
    "uhd1prf_svt": [
        "128x16m2",
        "256x32m4",
        "512x32m4",
        "1024x64m8",
        "2048x128m8",
        "4096x64m8",
    ],
    # Full eval — dpsram（验证 mem_type_full_coverage）
    "dpsram_lvt": [
        "256x32m4",
        "512x64m8",
    ],
}

# ─── 参数全开 / 全关 combo（param_toggle 检查）──────────────────────────────
#
# 每个 family 各定义两个极端配置 combo。
# "_full" 后缀 ≡ 含 SLP/DSLP/SD/OEB/BWEB；"_min" 后缀 ≡ 仅核心 port。

PARAM_TOGGLE_COMBOS = {
    "spsram_ulvt": {
        "all_on": "256x32m4_full",   # SLP+DSLP+SD+OEB+BWEB 全开
        "min":    "256x32m4_min",    # 仅 CLK/CEB/WEB/A/DI/DO
    },
    "2prf_lvt": {
        "all_on": "256x32m4_full",
        "min":    "256x32m4_min",
    },
}

# ─── 拆分边界测试 combo（split_check 检查）──────────────────────────────────
#
# 每项是一个 dict，包含 combo string、实际 NW/NB/NMUX，以及 split_type。
# split_type: "width" | "depth" | "both"

SPLIT_COMBOS = {
    "spsram_ulvt": [
        # 宽度拆分：NB=40，不被8整除（余0不算，用 nmux=8 测 40%8=0，改用 nmux=3 测 40%3=1）
        {"combo": "512x40m8",  "nw": 512,  "nb": 40,  "nmux": 8,  "split_type": "width"},
        # 深度拆分：NW=1000，不是2的幂
        {"combo": "1000x32m4", "nw": 1000, "nb": 32,  "nmux": 4,  "split_type": "depth"},
        # 复合拆分：NW=1500, NB=48 均非整除
        {"combo": "1500x48m8", "nw": 1500, "nb": 48,  "nmux": 8,  "split_type": "both"},
        # 宽度余数测试：NB=36，nmux=8 → 余4 bits
        {"combo": "256x36m8",  "nw": 256,  "nb": 36,  "nmux": 8,  "split_type": "width"},
    ],
}

# ─── 期望 Judge 覆盖的全部 memory family ─────────────────────────────────────

EXPECTED_FAMILIES = ["spsram_ulvt", "2prf_lvt", "uhd1prf_svt", "dpsram_lvt"]

# ─── 代表性 combo（用于 interface_spec_consistency 跨 family 检查）──────────

REPRESENTATIVE_COMBO = {
    "spsram_ulvt": "256x32m4",
    "2prf_lvt":    "256x32m4",
    "uhd1prf_svt": "256x32m4",
    "dpsram_lvt":  "256x32m4",
}

# ─── Behavior model 目录（固定，不随 skill 变化）────────────────────────────

BEHAVIOR_MODEL_DIR = "mem_gen_offline_tester/reference_models"

# ─── MC compiler 路径（测试环境） ───────────────────────────────────────────

MC_PATH = "mem_gen_offline_tester/mock_mc"
