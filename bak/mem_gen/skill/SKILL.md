---
name: mem-gen
description: >
  Memory Generator 构建 skill。指导 agent 从零为任意 foundry/process 构建一个
  完整的 Memory Generator CLI 工具，包括编译器封装、命名解析、RTL 自动拼接、
  验证框架。

  命中以下任意场景即加载：
  - 用户提到 memory generator、mem_gen、内存生成器、SRAM 编译器封装；
  - 提到 Memory Compiler wrapper、MC 封装、mem compiler CLI；
  - 需要将 SRAM 编译器的命令行调用封装为可编程接口；
  - 需要自动 tiling / stitching（深度或宽度超出单颗 macro 限制）；
  - 提到 foundry naming convention 解析、TSMC/Samsung/Intel macro 命名；
  - 需要批量生成 SRAM macro（VERILOG + LEF + DATASHEET + GDS）；
  - 需要自动生成 RTL wrapper（tile_wrapper + top_wrapper + filelist）。
---

# Memory Generator Skill (mem_gen)

> 本文档是 mem_gen_offline_tester 优化产出的最佳版本。
> 完整的 skill 文档请参见各子文件。

## Skill 文件结构

```
mem_gen/
├── SKILL.md                         ← 本文件（入口 + 触发规则）
├── 01_investigation.md              ← MC 安装调研方法论
├── 02_name_convention.md            ← Foundry 命名规则解析
├── 03_wrapper_design.md             ← 编译器封装架构
├── 04_tiling_and_stitching.md       ← RTL 拼接算法
├── 05_cli_and_packaging.md          ← CLI 设计与打包
├── 06_validation.md                 ← 验证策略
├── 07_pitfalls.md                   ← 关键教训
├── 08_decision_log.md               ← 架构决策记录
└── ref_code/
    ├── name_parser_skeleton.py      ← 命名解析参考实现
    └── tiling_engine_skeleton.py    ← 拼接引擎参考实现
```

## 适用范围

- 任意 foundry（TSMC、Samsung、Intel、SMIC）
- 任意 process node（7nm、12nm、16nm、28nm）
- 6 种 memory family（spsram、dpsram、1prf、2prf、uhd1prf、uhd2prf）
- 支持 tiling 到 32×32 tile 阵列

## 与其他 Skill 的关系

- **上游**：无（最上游组件，直接操作 MC 编译器）
- **下游**：mem_predict（使用 mem_gen 产出的 DATASHEET 训练模型）
- **同层**：mem_selection（使用 predictor 结果做选型）、mem_replace（使用选定 macro 做 RTL 替换）
