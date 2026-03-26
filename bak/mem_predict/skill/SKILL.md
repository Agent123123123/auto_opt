---
name: mem-predict
description: >
  SRAM PPA 预测模型构建 skill。指导 agent 从零为任意 foundry/process 构建高精度
  PPA 预测器（<8% worst-case error），使用少量 MC 采样 + Ridge 回归。

  命中以下任意场景即加载：
  - 用户提到 SRAM predictor、mem_predict、PPA 预测、内存性能预测；
  - 提到训练 PPA 模型、构建 SRAM 回归模型、fitting pipeline；
  - 需要预测 SRAM 面积/时序/功耗而不运行 MC 编译器；
  - 提到 DATASHEET 解析、Group A/B 验证、采样策略；
  - 需要为 sram_selection 提供 PPA 数据源。
---

# SRAM PPA Predictor Skill (mem_predict)

> 本文档是 mem_predictor_offline_tester 优化产出的最佳版本。
> 完整的 skill 文档请参见各子文件。

## Skill 文件结构

```
mem_predict/
├── SKILL.md                    ← 本文件（入口 + 触发规则）
├── 01_workflow.md              ← 端到端 8 步 pipeline
├── 02_sampling_strategy.md     ← 采样策略（Group A/B 划分）
├── 03_fitting_algorithm.md     ← Ridge 回归 + 特征工程
├── 04_pitfalls.md              ← 10 条关键教训
└── ref_code/
    ├── parse_ds.py             ← DATASHEET 解析参考实现
    ├── fit_pipeline.py         ← 完整拟合 pipeline 参考
    └── predictor.py            ← 推理 API 参考
```

## 关键指标（TSMC 12nm 验证）

| 指标 | 值 |
|------|-----|
| 支持 memory type | 14 种 |
| 采样密度 | ~6.3% per dimension |
| 整体 worst-case error | 7.26% |
| 验证指标通过率 | 1803/1803 PASS (<15%) |
| 典型 avg MAPE | 0.02% – 1.15% |

## 与其他 Skill 的关系

- **上游**：mem_gen（提供 MC 编译器封装和 DATASHEET 数据）
- **下游**：mem_selection（调用 predictor API 获取 PPA 数据做选型）
- **同层**：mem_replace（无直接关系）
