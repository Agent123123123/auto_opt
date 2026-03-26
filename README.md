# mem_solution

**完整的 SRAM 基础设施解决方案** — 从编译器封装到 PPA 预测到选型到 RTL 替换，4 个 AI Agent Skill 协同完成。

## 架构总览

```
                         mem_solution
    ┌──────────────────────────────────────────────────┐
    │                                                  │
    │   ┌──────────────┐      ┌───────────────────┐    │
    │   │  mem_gen      │─────→│  mem_predict       │    │
    │   │  (Skill)      │  DS  │  (Skill)           │    │
    │   └──────────────┘      └────────┬──────────┘    │
    │          ▲                        │ PPA API       │
    │          │                        ▼              │
    │   ┌──────┴───────┐      ┌───────────────────┐    │
    │   │ mem_gen_      │      │  mem_selection     │    │
    │   │ offline_tester│      │  (Skill)           │    │
    │   │ (Optimizer)   │      └────────┬──────────┘    │
    │   └──────────────┘               │ macro name    │
    │                                   ▼              │
    │   ┌──────────────┐      ┌───────────────────┐    │
    │   │ mem_predictor_│      │  mem_replace       │    │
    │   │ offline_tester│      │  (Skill)           │    │
    │   │ (Optimizer)   │      └───────────────────┘    │
    │   └──────────────┘                               │
    └──────────────────────────────────────────────────┘
```

## 概念说明

```
offline_tester (优化器 skill)          最优产出
         │                                │
         │  指导 agent 构建               │  包含
         ▼                                ▼
  如何从零建立一个                 CLI 工具代码 (src/)
  完整工具的 skill 文档    ──→    +
                                  配套 skill 文档 (skill/)
```

- `mem_gen_offline_tester`：**优化器 skill**，告诉 agent 如何构建 mem_gen 工具
- `mem_predictor_offline_tester`：**优化器 skill**，告诉 agent 如何构建 mem_predict 工具
- `mem_gen`、`mem_predict`：两项优化的**最佳产出**（CLI 代码 + 配套 skill）
- `mem_select`、`mem_replace`：**独立 skill**（无配套 CLI，直接指导 agent 操作）

## 目录结构

```
mem_solution/
├── README.md                                ← 本文件
│
│  ── 最优产出（训练结果） ──────────────────────────────────────────
│
├── mem_gen/                                 ← 🟢 mem_gen 最优产出
│   ├── skill/                               │   配套 skill：指导 agent 使用 mem_gen 工具
│   │   ├── SKILL.md                         │   触发规则 + 使用说明
│   │   ├── 01_investigation.md              │   MC 安装调研（优化后）
│   │   ├── 02_name_convention.md            │   Foundry 命名解析
│   │   ├── 03_wrapper_design.md             │   编译器封装
│   │   ├── 04_tiling_and_stitching.md       │   RTL 拼接算法
│   │   ├── 05_cli_and_packaging.md          │   CLI 设计
│   │   ├── 06_validation.md                 │   验证策略
│   │   ├── 07_pitfalls.md                   │   关键教训（经优化增强）
│   │   └── 08_decision_log.md               │   架构决策
│   └── src/                                 │   CLI 工具实现代码（最优版本）
│       └── (memgen Python package)
│
├── mem_predict/                             ← 🟢 mem_predict 最优产出
│   ├── skill/                               │   配套 skill：指导 agent 使用 mem_predict 工具
│   │   ├── SKILL.md
│   │   ├── 01_workflow.md                   │   8 步 pipeline（优化后）
│   │   ├── 02_sampling_strategy.md          │   采样策略
│   │   ├── 03_fitting_algorithm.md          │   Ridge 回归
│   │   └── 04_pitfalls.md                   │   关键教训（经优化增强）
│   └── src/                                 │   CLI 工具实现代码（最优版本）
│       └── (mem_predict Python package)
│
│  ── 独立 Skill（无 CLI）────────────────────────────────────────────
│
├── mem_select/                              ← 🟢 独立 skill（SRAM 选型）
│   └── SKILL.md                             │   约束计算 → Pareto → 决策
│
├── mem_replace/                             ← 🟢 独立 skill（RTL 替换）
│   └── SKILL.md                             │   行为模型 → SRAM macro wrapper
│
│  ── 优化器（训练数据）──────────────────────────────────────────────
│
├── mem_gen_offline_tester/                  ← 🔧 submodule
│   └── (优化器 skill：如何构建 mem_gen)      │   进化目标：提升 mem_gen 产出质量
│
├── mem_predictor_offline_tester/            ← 🔧 submodule
│   └── (优化器 skill：如何构建 mem_predict) │   进化目标：提升 predictor 精度
│
│  ── 评估与文档 ──────────────────────────────────────────────────────
│
├── evaluation/                              ← 📊 评估框架（各组件 + 端到端）
│   ├── eval_mem_gen.py
│   ├── eval_mem_predict.py
│   ├── eval_mem_select.py
│   ├── eval_mem_replace.py
│   ├── eval_e2e.py
│   └── test_fixtures/
│
└── docs/
    └── optimization_plan.md                 ← 📋 完整优化方案
```

## 四个组成部分的关系

| 组件 | 类型 | 输入 | 核心产出 |
|------|------|------|---------|
| **mem_gen** | CLI + skill | Foundry MC 路径 | SRAM macro 全套产出 (VERILOG/LEF/DS/GDS) + RTL wrapper |
| **mem_predict** | CLI + skill | DATASHEET 文件 | PPA 预测 API (area/timing/power，<8% error) |
| **mem_select** | 独立 skill | 设计约束 + predictor API | 选型报告 + 首选 macro 名 |
| **mem_replace** | 独立 skill | macro 名 + RTL 源码 | 修改后的 RTL + wrapper 文件 |

**数据流链**：`mem_gen → mem_predict → mem_select → mem_replace`

## 训练结果的更新方式

```
优化循环：
  mem_gen_offline_tester (submodule) ──Hyperagents进化──→ 更新 mem_gen/skill/ + mem_gen/src/
  mem_predictor_offline_tester (submodule) ──→ 更新 mem_predict/skill/ + mem_predict/src/

独立优化：
  evaluation/eval_mem_select.py ──→ 更新 mem_select/SKILL.md
  evaluation/eval_mem_replace.py ──→ 更新 mem_replace/SKILL.md
```

## 快速开始

```bash
# 克隆（含子模块）
git clone --recursive https://github.com/Agent123123123/mem_solution.git

# 初始化最优 skill 内容（从 offline_tester 复制初始版本）
cp mem_gen_offline_tester/0*.md mem_gen/skill/
cp mem_predictor_offline_tester/0*.md mem_predict/skill/
```

## 优化方案

详见 [docs/optimization_plan.md](docs/optimization_plan.md)。

核心策略：**按耦合度分组优化**
- Phase 1：mem_gen + mem_predict 联合优化（强耦合：共享命名规则/参数空间）
- Phase 2：mem_select 独立优化
- Phase 3：mem_replace 独立优化（可与 Phase 2 并行）
- Phase 4：端到端联调验证
