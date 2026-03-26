# mem_solution 完整优化方案

## 目录

1. [总体优化策略](#1-总体优化策略)
2. [Phase 1: mem_gen + mem_predict 联合优化](#2-phase-1-mem_gen--mem_predict-联合优化)
3. [Phase 2: mem_selection 独立优化](#3-phase-2-mem_selection-独立优化)
4. [Phase 3: mem_replace 独立优化](#4-phase-3-mem_replace-独立优化)
5. [Phase 4: 端到端系统评估与联调](#5-phase-4-端到端系统评估与联调)
6. [Hyperagents 框架适配](#6-hyperagents-框架适配)
7. [实施路线图](#7-实施路线图)

---

## 1. 总体优化策略

### 1.1 为什么按耦合度分组

四个 Skill 之间的耦合关系决定了不能一刀切：

```
强耦合 ─────────────────── 弱耦合
   │                          │
   mem_gen ←──→ mem_predict   │   mem_selection   mem_replace
   │    共享命名规则          │   纯消费 API      完全独立
   │    共享参数空间          │   自洽知识体系     正交知识域
   │    采样与枚举协同        │
```

**优化原则**：
- mem_gen 与 mem_predict 虽然存在接口耦合，但各自的核心能力（gen 的产物正确性、predict 的拟合精度）相互独立，应先独立优化各自核心能力，再联合修复接口不一致
- 弱耦合/无耦合的 skill 全程独立优化（避免搜索空间爆炸和评估信号稀释）

### 1.2 优化方法论：Hyperagents 自适应进化

基于 Hyperagents (arXiv 2603.19461) 的 DGM-H 框架：

```
┌─────────────────────────────────────────────────────────┐
│                    进化主循环                            │
│                                                         │
│  1. 从 Archive 中选择 parent skill 版本                 │
│  2. Meta Agent 分析评估历史，提出 skill 改进方案         │
│  3. 生成 skill 变体（修改文档 / 参考代码 / 添加知识）    │
│  4. Task Agent 使用新 skill 执行任务                     │
│  5. 评估 Task Agent 表现                                │
│  6. 若表现提升，加入 Archive                            │
│  7. 回到 1                                              │
└─────────────────────────────────────────────────────────┘
```

### 1.3 四阶段执行序列

```
时间轴 ──────────────────────────────────────────────────→

Phase 1a: ████████████                           (mem_gen 独立优化)
Phase 1b: ████████████                           (mem_predict 独立优化，与 1a 并行)
Phase 1c:             ██████                     (1a+1b 接口联合优化)
Phase 2:                     ████████████        (mem_selection)
Phase 3:                     ████████████        (mem_replace，可与 Phase 2 并行)
Phase 4:                                  ██████ (端到端联调)
```

### 1.4 统一评估架构：LLM Judge Agent

#### 架构决策

**所有 Phase 的评估均由 LLM Judge Agent 负责，不使用纯 Python 脚本作为最终评判者。**

LLM Judge Agent 可以（也必须）调用 Python 脚本作为工具来完成客观指标的计算，但最终的评分和综合判定由 LLM 完成。

#### 统一架构图

```
进化主循环（每一代）
    │
    ├── [LLM] Meta Agent
    │       读取评估历史 → 分析失败模式 → 生成 skill 改进 patch
    │
    ├── [LLM] Task Agent
    │       加载当前 skill → 执行任务（MemBuild / MemSelect / MemReplace）
    │       产出：文件、报告、模型等
    │
    └── [LLM] Judge Agent  ← 本节定义
            读取 Task Agent 产出
            调用 Python 工具 → 计算客观数值指标
            综合所有指标 → 输出结构化评分 + 失败分析
```

#### Judge Agent 与工具的职责划分

| 职责 | 由谁完成 | 说明 |
|------|---------|------|
| 运行 Python eval 脚本（MAPE/MaxErr/lint/diff）| Python 工具 | Judge Agent 调用，结果返回给 Judge |
| 解读数值结果（是否满足阈值、反常情况分析）| LLM Judge | 客观数字→主观判断 |
| 检查报告结构完整性、决策逻辑合理性 | LLM Judge | 纯文本理解任务 |
| 跨指标综合打分 | LLM Judge | 加权汇总，处理边界情况 |
| 输出失败模式描述（供 Meta Agent 参考）| LLM Judge | 关键：驱动下一代进化方向 |

#### 统一架构的优点

1. **架构一致**：三类 Agent（Meta / Task / Judge）均使用相同的 LLM + 工具调用框架，复用同一套 `agent/llm_withtools.py`
2. **失败分析质量更高**：LLM Judge 不仅返回分数，还输出结构化的失败原因描述，直接喂给 Meta Agent 作为改进线索
3. **可扩展性**：新增评估维度时只需扩充 Judge 的 prompt 和工具集，无需修改 Python eval 脚本的返回格式
4. **跨 Phase 一致接口**：Meta Agent 对所有 Phase 的评估结果格式完全一致，简化了 meta_agent.py 的实现

#### Judge Agent 输出格式（统一约定）

```json
{
  "score": 0.73,
  "passed_threshold": true,
  "breakdown": {
    "layer0_gates": {"passed": true},
    "layer1_scores": {"metric_A": 0.82, "metric_B": 0.65, ...},
    "layer2_scores": {"metric_C": 0.77, ...}
  },
  "failure_analysis": [
    {"metric": "max_error", "value": 0.18, "threshold": 0.15,
     "diagnosis": "uhd1prf SVT 的 NW=4 segment 边界处误差偏高，可能与采样密度不足有关"}
  ],
  "improvement_hints": [
    "建议在 NW segment 边界附近增加采样点",
    "考虑对高密度 type 使用分段回归"
  ]
}
```

`improvement_hints` 字段由 Judge Agent 生成，Meta Agent 在下一代改进时优先参考。

---

## 2. Phase 1: mem_gen + mem_predict 分步优化

### 2.1 优化结构：先独立、再联合

Phase 1 分三个子阶段执行。Phase 1a 和 1b 可并行，Phase 1c 在两者均达标后启动：

```
Phase 1a: mem_gen 独立优化            Phase 1b: mem_predict 独立优化
  目标：Agent 能读取 skill 后          目标：拟合精度全面准确；
  构建出完整、正确的 memgen package；     覆盖所有参数组合
  参照 tsmc_12nm_sram 标准
               │                                  │
               └──────────────┬───────────────────┘
                              ▼
               Phase 1c: 接口一致性联合优化
                 目标：命名规则、DS 格式、参数空间枚举
                 在两个 skill 之间完全对齐
```

两者核心能力相互独立（memgen package 功能正确性 ≠ 拟合精度），独立优化可以获得更清晰的进化信号，避免两个方向的噪声相互覆盖。接口层面的耦合留给 Phase 1c 专项修复。

> **Golden Reference**：`mem_gen_offline_tester/examples/tsmc_12nm_sram` 是目前已知最好的 mem_gen 实现。
> 它由 Agent 使用 skill 文档产出，经过了实际 TSMC 12nm MC 编译器验证。
> 评估框架以此为标准答案，测试新 skill 版本能否指导 Agent 产出同等质量的 package。

---

### 2.2 Phase 1a：mem_gen 独立优化

#### 核心认知：Skill 产物是 Python Package，不是单个文件

通过分析已知最佳实现 `tsmc_12nm_sram/`，可以明确 mem_gen skill 的真正产物：

```
Skill 文档 (01-08.md + ref_code/)
    │  被 Task Agent 阅读
    ▼
Task Agent 产出：一个完整的 Python Package（如 tsmc_12nm_sram/memgen/）
    ├── wrapper.py      — MC 命名解析 + 约束校验 + config 生成 + 编译器调用
    ├── plan.py         — 纯数学 tiling plan 计算（WrapperPlan dataclass）
    ├── uhdl_emit.py    — UHDL RTL wrapper 生成（tile_wrapper + top_wrapper × 3 种接口）
    ├── generate.py     — 全流程编排（编译 + wrapper 生成）
    ├── cli.py          — CLI 入口（families / check / plan / generate / run）
    └── pyproject.toml  — 打包配置
```

因此 **评估的对象不是个别 wrapper 文件，而是 Task Agent 产出的整个 memgen package 的功能正确性**。
这与之前假设的"检查 wrapper 端口与行为模型是否匹配"有本质区别：
- 不存在"behavior model 比对"——wrapper 本身就是最终产物，macro model 由 MC 编译器生成，wrapper 通过 VComponent 直接引用
- 端口定义由 interface class（single_port / 1R1W / dual_port）和 plan 参数（exposed_width / exposed_depth）决定
- 评估重点是：package 的各模块功能是否正确、是否覆盖所有 family 和接口类型

#### 评估任务定义

```
任务名称：MemGen
任务描述：
  给定一个目标 foundry + process node 环境（含 MC 编译器路径），
  使用 mem_gen skill 从零构建一个 Memory Generator Python Package。
  Package 需覆盖该环境下所有 memory family，支持：
    1. 命名解析（从 foundry convention name → 解析出 family/VT/words/bits/mux/options）
    2. 约束校验（reject invalid combos, BIST 永久禁用）
    3. Config 生成 + MC 编译器调用
    4. Tiling plan 计算（宽度/深度超出单颗 macro 时自动拆分）
    5. RTL wrapper 生成（tile_wrapper + top_stitching_wrapper，支持 3 种接口类型）
    6. CLI 入口（subcommands）

输入：
  - MC 编译器安装目录
  - 目标 foundry / process 标识

输出：
  - 一个可 pip install 的 Python package（含 pyproject.toml）
  - 所有源码文件（wrapper.py / plan.py / uhdl_emit.py / generate.py / cli.py）
```

#### 评估架构：离线可测 + 在线验证

关键设计决策：**大部分评估不需要 MC 编译器**。

```
离线可测（纯 Python / 纯数学）：
  ├── 命名解析正确性（parse_memory_name 纯函数）
  ├── Tiling plan 正确性（build_wrapper_plan 纯数学）
  ├── Family 覆盖完整性（检查 FAMILIES dict）
  ├── 约束校验逻辑（feed invalid names → expect error）
  ├── CLI 结构（subcommands 是否齐全）
  └── 代码语法 + lint

在线验证（需要 MC 编译器或 stub model）：
  ├── 端到端 wrapper RTL 生成（需 Verilog model 文件作为 VComponent 输入）
  └── RTL elaboration（VCS/Xrun/svlinter）
```

#### 评估指标体系

**层级 0：门槛指标**（任一不通过 → 总分 0）

| 指标 | 判定条件 |
|------|----------|
| package_runnable | `pip install -e .` 成功，`memgen --help` 不崩溃 |
| code_syntax_clean | 所有 .py 文件通过 `ast.parse()` |

**层级 1：核心功能正确性**

| 指标 | 计算方式 | 权重 |
|------|---------|------|
| name_parsing_accuracy | 喂入 N 个 golden name → 解析结果与标准答案匹配率 | 20% |
| plan_correctness | 喂入 (name, width, depth) → tiling plan 与标准答案匹配率 | 20% |
| interface_type_coverage | 是否覆盖 3 种接口类型（single_port / 1R1W / dual_port） | 15% |
| family_coverage | FAMILIES dict 覆盖的 family 数 / 期望 family 数 | 10% |
| constraint_enforcement | 无效输入（bad name / BIST token / invalid mux）被正确拒绝的比例 | 10% |

**具体检查内容：**

`name_parsing_accuracy` 的 golden test cases：
- 从 `tsmc_12nm_sram/memgen/wrapper.py` 的 FAMILIES 数据提取（已知正确解析）
- 测试 family 自动识别（comp_no + bitcell → family_id）
- 测试 option token 解析（w/s/h/o/d/cp 各种组合）
- 测试 segment 解析（s/m/f / 无 segment 的 family）
- 边界：version 后缀有无、VT 变体（ulvt/lvt/svt）

`plan_correctness` 检查维度：
- h_tiles = ceil(exposed_width / child_bits)
- v_tiles = ceil(exposed_depth / child_words)
- padded_width / padded_depth 计算
- 每个 tile 的 data_bit_low/high、depth_start/end
- edge tile 的 padded_data_bits（余数位处理）
- exposed_addr_bits = ceil_log2(exposed_depth)
- row_sel_bits = ceil_log2(vertical_tiles)
- 特别关注非整除情况（如 width=40 / child_bits=16 → 3 cols, 余 8）

`interface_type_coverage`：
- 检查 uhdl_emit.py 中是否存在 3 组 TileWrapper + TopWrapper class
- single_port：CLK / CEB / WEB / A / D / Q / BWEB + SLP/DSLP/SD
- one_read_one_write：独立读写地址，REB / WEB 分开控制
- dual_port：两组完整 port（CLKA/CLKB, CEBA/CEBB, ...）

`constraint_enforcement`：
- BIST 相关 token（b / y）→ 应 raise WrapperError
- ROM family（ts3n...）→ 应被拒绝
- 不合法 comp_no + bitcell 组合 → 应 raise WrapperError
- mux 值超出 family 允许范围 → 应 raise WrapperError

**层级 2：工程质量**

| 指标 | 计算方式 | 权重 |
|------|---------|------|
| tiling_edge_handling | 非整除 width/depth 场景：BWEB pad-with-ones + Q truncation + CE gating | 10% |
| cli_completeness | 检查 subcommands 齐全度（families/check/plan/generate/run ÷ 5） | 5% |
| vendor_agnostic_generality | skill 文档中非 ref/examples 章节不出现 foundry 名称 / 制程标识 | 5% |
| code_quality | lint warning 数（pylint/ruff basic check），0 warning 得满分 | 5% |

`tiling_edge_handling` 详细检查（对应 wrapper RTL 生成）：
- 宽度边缘列：写路径 D 补 0 + BWEB 补 1；读路径 Q 截断有效 bits
- 深度边缘行：CE gating（address range guard），越界地址强制 CEB=1/REB=1
- 地址解码：priority chain（支持非 2 的幂深度），不得用简单 binary decode
- 读 mux：1-cycle delayed row_sel（Reg），确保读延迟正确

#### 综合评分公式

```python
def compute_phase1a_score(result):
    # Layer 0: gates
    if not result.package_runnable or not result.code_syntax_clean:
        return 0.0

    # Layer 1: core functionality
    s1 = 0.20 * result.name_parsing_accuracy
    s2 = 0.20 * result.plan_correctness
    s3 = 0.15 * result.interface_type_coverage
    s4 = 0.10 * result.family_coverage
    s5 = 0.10 * result.constraint_enforcement

    # Layer 2: engineering quality
    s6 = 0.10 * result.tiling_edge_handling
    s7 = 0.05 * result.cli_completeness
    s8 = 0.05 * result.vendor_agnostic_generality
    s9 = 0.05 * result.code_quality

    return s1 + s2 + s3 + s4 + s5 + s6 + s7 + s8 + s9
```

#### 测试策略：Golden Reference 对照

**不再使用静态 memory name combo 列表**。测试用例从 golden reference（`tsmc_12nm_sram`）自动提取：

```
Golden Reference 提取流程：
  1. 从 tsmc_12nm_sram/memgen/wrapper.py 提取 FAMILIES dict
     → 得到 6 个 family 的 comp_no / bitcell / compiler_version / supported_tokens
  2. 对每个 family 构造 3-5 个合法 memory name
     → parse → 得到 golden MemorySpec（family, vt, words, bits, mux, segment, options）
  3. 对每个 golden MemorySpec + 若干 (width, depth) 组合
     → build_wrapper_plan → 得到 golden WrapperPlan（tiles, addr_bits, padding 等）
  4. 构造 invalid inputs（bad name / BIST token / ROM prefix / bad mux）
     → 期望 WrapperError

Quick Eval（每代必跑）：
  - 命名解析：10 个合法 name + 5 个非法 name → 共 15 个 test case
  - Tiling plan：5 个 (name, width, depth) 组合（含 1 个非整除 width、1 个非整除 depth）
  - 结构检查：FAMILIES dict 完整性、interface class dispatch、CLI subcommands

Full Eval（quick score ≥ 0.50 后运行）：
  - 命名解析：全部 6 family × 3 VT × 2 option 变体 = 36 个 test case
  - Tiling plan：每 family 4 个 (width, depth) 组合（含边界 case）= 24 个 test case
  - 约束校验：20 个 invalid input 变体
  - RTL 生成：对 3 种 interface type 各生成 1 个 wrapper，做 svlinter/VCS elab 检查
```

#### Skill 变异约束

```
允许的修改：
  ✅ 添加 / 修改 pitfall 条目
  ✅ 改进 tiling 算法描述（边界处理、地址解码方式）
  ✅ 改进 wrapper 生成逻辑描述（接口类型 dispatch、padding 规则）
  ✅ 改进 CLI 设计指导（subcommand 结构、--help 内容）
  ✅ 添加新的约束校验规则
  ✅ 改进命名解析的描述（token 关系、family 自动识别逻辑）
  ✅ 补充 ref_code/ 中的参考实现
  ✅ 改进 06_validation.md（验证策略）
  ✅ 添加新的 family 接口说明

禁止的修改：
  ❌ 删除已有 pitfall 条目
  ❌ 修改 foundry macro 命名规则的客观事实部分（前缀含义、token 定义）
  ❌ 修改 port 极性定义（CEB/WEB active-low 是物理规格，不可变）
  ❌ 在非 reference/examples 章节中出现 foundry 名称（TSMC、Samsung Foundry、SMIC 等）
     或制程节点标识（N12FFC、N7FF、28nm 等）
  ❌ 以逐一枚举方式描述 SRAM type 覆盖范围（应改用抽象架构类别描述）
  ❌ 修改 tiling 的物理约束上限（32×32 tile 是已验证边界）
```

---

### 2.3 Phase 1b：mem_predict 独立优化（与 Phase 1a 并行）

#### 评估任务定义

```
任务名称：MemPredict
任务描述：
  给定一批 DS 文件（各参数组合的 DATASHEET），
  使用 mem_predict skill 完成 PPA 预测模型训练，
  在 Group B 验证集上达到精度目标。

输入：
  - DS 文件目录（预先生成，不依赖 Phase 1a 产出）
  - 目标精度阈值（default: MaxErr < 15%）

输出：
  - 训练好的预测模型
  - 验证报告（MAPE / MaxErr / Group B pass/fail per type）
```

#### 评估指标体系

**层级 0：门槛指标**

| 指标 | 判定条件 |
|------|----------|
| pipeline_complete | 8 步流程完成，模型文件存在 |
| group_b_not_empty | Group B 有有效预测结果（非 NaN/空） |

**层级 1：精度指标（核心）**

| 指标 | 计算公式 | 权重 |
|------|---------|------|
| group_b_pass_rate | 通过 type 数 / 总 type 数（MaxErr < threshold） | 35% |
| avg_mape | mean(各 type 的 avg MAPE)，≤5% 满分 | 30% |
| max_error | max(各 type 的 MaxErr)，≤15% 满分 | 20% |

**层级 2：鲁棒性**

| 指标 | 计算公式 | 权重 |
|------|---------|------|
| cross_type_coverage | 成功拟合的 type 种数 / 期望种数 | 10% |
| efficiency | 1 / log₂(agent 调用步数) | 5% |

#### 综合评分公式

```python
def compute_phase1b_score(result):
    if not result.pipeline_complete or not result.group_b_not_empty:
        return 0.0

    s1 = 0.35 * result.group_b_pass_rate
    s2 = 0.30 * max(0, 1 - result.avg_mape / 0.05)
    s3 = 0.20 * max(0, 1 - result.max_error / 0.15)
    s4 = 0.10 * result.cross_type_coverage
    s5 = 0.05 * (1 / math.log2(max(result.agent_steps, 2)))

    return s1 + s2 + s3 + s4 + s5
```

#### 测试集

```
复用与 Phase 1a 相同的 3 个 memory type，但 DS 文件由 mock_mc 独立预生成，
不依赖 Phase 1a 的实际产出（两者并行运行）。

  Quick Eval: spsram ULVT, 2prf LVT
  Full Eval:  uhd1prf SVT

DS 文件须覆盖完整参数空间（Group A: 训练集，Group B: 验证集）。
```

#### Skill 变异约束

```
允许的修改：
  ✅ 修改采样密度 / 策略参数
  ✅ 改进特征工程（添加/修改回归特征）
  ✅ 修改 Group A/B 划分比例
  ✅ 优化 DS 解析逻辑
  ✅ 添加 pitfall 条目

禁止的修改：
  ❌ 改变核心回归方法（Ridge 可调参，不允许替换为非线性方法）
  ❌ 删除 Group A/B 评估机制
  ❌ 修改 MAPE / MaxErr 的计算定义
```

---

### 2.4 Phase 1c：接口一致性联合优化

Phase 1a 和 1b 各自达标后，进行轻量联合调优，目标是修复两个 skill 之间的语义接口不一致。

#### 接口检查项

| 接口 | 检查内容 | 若不一致应修改 |
|------|---------|--------------|
| DS 文件格式 | mem_gen 产出 DS 能否被 mem_predict 解析器直接读取 | 修改下游（predict 解析器） |
| 参数范围枚举 | 两个 skill 对 (NW, NB, NMUX) 合法范围的认定是否一致 | 修改两者对齐 |
| macro 命名前缀 | gen 输出的 DS 文件名格式与 predict 期望格式是否一致 | 修改下游 |
| segment 边界定义 | gen 的 NW segment 划分与 predict 的采样分段是否对齐 | 修改 predict 跟随 gen |

#### 评估任务定义

```
任务名称：MemBuildIntegrated
任务描述：
  使用 Phase 1a 最优 mem_gen skill + Phase 1b 最优 mem_predict skill，
  完整执行 gen → predict pipeline（gen 的 DS 直接作为 predict 输入），
  要求全流程无人工格式转换。

评估重点：
  - DS 文件从 gen 到 predict 的零人工干预传递成功率
  - predict 在 gen 真实产出 DS 上的精度，应与预生成 DS 基线持平
```

#### 评估指标

| 指标 | 权重 | 说明 |
|------|------|------|
| pipeline_handoff_rate | 40% | gen DS → predict 无错误传递的 type 比例 |
| accuracy_delta | 40% | predict 在真实 DS 上的精度 vs 预生成 DS 精度差值（越小越好） |
| zero_intervention_rate | 20% | 全流程无人工干预的成功率 |

#### Skill 变异约束（较严格，保护已固化成果）

```
允许的修改：
  ✅ 修改 DS 文件路径约定的描述
  ✅ 修改 predict 解析器对 DS 格式的期望描述
  ✅ 统一两个 skill 对参数范围的表述

禁止的修改：
  ❌ 修改 mem_gen 的 wrapper 生成逻辑（已在 1a 固化）
  ❌ 修改 mem_predict 的回归核心算法（已在 1b 固化）
  ❌ 修改 TSMC macro 命名规则的客观事实部分
```

---

## 3. Phase 2: mem_selection 独立优化

### 3.1 评估任务定义

```
任务名称：MemSelect
任务描述：
  给定 SRAM 规格（depth × width）、目标频率、PPA 优先级，
  使用 mem_selection skill 执行选型流程，
  输出选型报告。与 golden answer 对比。

输入：
  - SRAM 规格（e.g., 1024×64）
  - 目标频率（e.g., 1GHz）
  - PPA 优先级（e.g., 性能 > 面积 > 功耗）
  - Mock Predictor 数据（JSON 格式的 PPA 表）

输出：
  - 完整选型报告（含 Timing 计算、候选集、Pareto 分析、首选决策）
```

### 3.2 评估指标体系

#### 层级 0：门槛

| 指标 | 判定条件 |
|------|---------|
| report_complete | 报告包含所有 5 个章节 |
| has_primary_choice | 给出了明确的首选 macro |

#### 层级 1：正确性指标

| 指标 | 计算公式 | 权重 |
|------|---------|------|
| timing_calc_accuracy | TCC/TCQ/MinPW 计算值与 golden 的误差 < 5% | 30% |
| candidate_coverage | 候选集是否包含 golden answer 中的所有候选 | 25% |
| pareto_correctness | Pareto 支配关系判定与 golden 一致 | 25% |

#### 层级 2：决策质量

| 指标 | 计算公式 | 权重 |
|------|---------|------|
| primary_match | 首选与 golden answer 一致 | 15% |
| reasoning_quality | 决策理由是否引用了正确的数据 | 5% |

#### 综合评分公式

```python
def compute_phase2_score(result):
    if not result.report_complete or not result.has_primary_choice:
        return 0.0

    s1 = 0.30 * result.timing_calc_accuracy
    s2 = 0.25 * result.candidate_coverage
    s3 = 0.25 * result.pareto_correctness
    s4 = 0.15 * result.primary_match
    s5 = 0.05 * result.reasoning_quality

    return s1 + s2 + s3 + s4 + s5
```

### 3.3 评估场景设计（Golden Answer 集）

| 场景 | 规格 | 频率 | 难度 | 考察重点 |
|------|------|------|------|---------|
| S1 | 256×32 | 500MHz | 低 | 基本流程正确性 |
| S2 | 1024×64 | 1GHz | 中 | Timing 约束计算 + 候选穷举 |
| S3 | 4096×128 | 1.5GHz | 高 | 需要拆分 + 乐观/保守区分类 |
| S4 | 32×16 | 800MHz | 特殊 | SRAM vs 寄存器边界判定 |
| S5 | 2048×256 | 1.2GHz | 高 | 深度 + 宽度双向拆分 + Pareto 前沿分析 |

每个场景预先准备 golden answer（由人类专家审核），包括：
- 期望的 TCC/TCQ/MinPW 数值
- 应纳入的候选 type 列表
- Pareto 前沿候选集
- 正确的首选决策

### 3.4 评估环境

```
Mock Predictor 数据格式：
{
  "spsram_ulvt": {
    "256x32_m2": {"area_um2": 1234, "tcc_ps": 450, "tcq_ps": 380, ...},
    "256x32_m4": {"area_um2": 1100, "tcc_ps": 520, "tcq_ps": 420, ...},
    ...
  },
  "2prf_lvt": { ... },
  ...
}

不需要真实 MC 编译器，评估速度 < 1 分钟/场景。
```

### 3.5 Skill 变异约束

```
允许的修改：
  ✅ 修改 Timing 余量参数（保守/乐观的百分比）
  ✅ 改进拆分搜索策略（锚点选择、停止条件）
  ✅ 修改 Pareto 分析的描述方式
  ✅ 调整 SRAM vs 寄存器的容量阈值
  ✅ 改善候选穷举的流程描述
  ✅ 添加新的决策规则/特殊情况处理

禁止的修改：
  ❌ 修改 TCC/TCQ/MinPW 的物理公式（这些是物理定义）
  ❌ 改变"必须穷举所有类型"的强制要求
  ❌ 取消 Pareto 支配分析步骤
  ❌ 削弱"必须给出明确首选"的约束
```

---

## 4. Phase 3: mem_replace 独立优化

### 4.1 评估任务定义

```
任务名称：MemReplace
任务描述：
  给定一组 ARM wrapper 文件（含直接 TSMC macro 实例化），
  使用 mem_replace skill 执行 memory 替换流程，
  输出修改后的 RTL + 新建的 wrapper 文件。

输入：
  - 1-3 个 ARM wrapper .sv 文件（测试 fixture）
  - TSMC macro 列表

输出：
  - 修改后的 ARM wrapper 文件（使用 *_wrapper 模块）
  - 新建的 TSMC macro wrapper 文件
```

### 4.2 评估指标体系

#### 层级 0：门槛

| 指标 | 判定条件 |
|------|---------|
| all_macros_replaced | 所有 TSMC macro 都被替换 |
| no_syntax_error | 输出文件无语法错误 |

#### 层级 1：正确性

| 指标 | 计算方式 | 权重 |
|------|---------|------|
| polarity_correct | CEB/WEB active-low 翻转正确 | 25% |
| bweb_handling | BWEB 处理策略与预期一致 | 20% |
| lint_clean | lint 零 warning | 20% |
| wrapper_interface | wrapper 接口完整（所有 pin 处理） | 15% |

#### 层级 2：工程质量

| 指标 | 计算方式 | 权重 |
|------|---------|------|
| generate_for_handling | 常量参数展开 / 可变参数保留 | 10% |
| naming_convention | 文件命名规则正确 | 5% |
| power_pin_handling | SLP/DSLP/SD/MCR/MCW 处理正确 | 5% |

### 4.3 测试 Fixture 设计

```
fixture_easy.sv:
  - 2 个 spsram ULVT macro，无 BWEB，无 generate-for
  - 考察：基本替换流程

fixture_medium.sv:
  - 3 个混合 family macro（spsram + 2prf），含 BWEB
  - 1 个 generate-for（常量参数，需展开）
  - 考察：多 family 处理 + BWEB 判定 + generate-for 展开

fixture_hard.sv:
  - 4 个 l1cache + shdspsbsram macro
  - 2 个 generate-for（1 常量展开 + 1 可变保留）
  - 含 partial-write BWEB
  - 考察：复杂 family + generate-for 判定 + partial-write 保留
```

### 4.4 评估环境

```
工具依赖：
  - svlinter 或 Verilator（lint check）
  - diff 工具（与 golden answer 对比）
  
不需要 MC 编译器，不需要仿真。
评估速度 < 30 秒/fixture。
```

---

## 5. Phase 4: 端到端系统评估与联调

### 5.1 为什么需要端到端评估

四个 skill 各自优化后，接口处可能存在不一致：
- mem_gen 的命名规则更新了，但 mem_selection 的候选生成还用旧规则
- mem_predict 新增了 type 支持，但 mem_selection 没有纳入该 type 候选
- mem_selection 输出的 macro 名格式，mem_replace 无法解析

### 5.2 端到端评估任务

```
任务名称：MemSolution_E2E
任务描述：
  模拟完整的 SRAM 基础设施建设 + 使用场景：
  
  Step 1: 使用 mem_gen 建立某 process 的 MC 封装
  Step 2: 使用 mem_predict 训练 PPA 预测模型
  Step 3: 给定设计约束，使用 mem_selection 选型
  Step 4: 使用 mem_replace 替换 RTL 中的 SRAM

  全流程必须连贯执行，后续步骤使用前面步骤的真实产出。

输入：
  - MC 编译器路径
  - 目标 process node
  - 设计约束（频率、规格）
  - ARM wrapper RTL 文件

输出：
  - 完整的 SRAM 基础设施
  - 选型报告
  - 替换后的 RTL
```

### 5.3 端到端评估指标

| 层级 | 指标 | 权重 | 说明 |
|------|------|------|------|
| L0 | pipeline_complete | 门槛 | 4 步全部完成 |
| L1 | interface_consistency | 30% | 步骤间数据格式/命名/语义一致 |
| L2 | per_skill_quality | 40% | 各 skill 独立评分的加权平均 |
| L3 | total_time | 15% | 全流程耗时 |
| L4 | human_intervention | 15% | 需要人工干预的次数（0 最佳） |

```python
def compute_e2e_score(result):
    if not result.pipeline_complete:
        return 0.0

    s1 = 0.30 * result.interface_consistency
    s2 = 0.40 * (
        0.30 * result.gen_score +
        0.30 * result.predict_score +
        0.20 * result.selection_score +
        0.20 * result.replace_score
    )
    s3 = 0.15 * max(0, 1 - result.total_hours / 4.0)  # 4 小时以内满分
    s4 = 0.15 * max(0, 1 - result.human_interventions / 5.0)

    return s1 + s2 + s3 + s4
```

### 5.4 接口一致性检查项

| 接口 | 检查内容 |
|------|---------|
| gen → predict | DS 文件路径和格式是否匹配 predict 的解析器 |
| predict → selection | predictor API 的输出 key 是否匹配 selection 的输入 |
| selection → replace | 选型输出的 macro 名是否能被 replace 的命名解析器识别 |
| gen → replace | gen 产出的 macro family 是否在 replace 的 family 对照表中 |

### 5.5 联调优化策略

端到端联调**不使用 Hyperagents 进化**（代价太高），而是：

```
1. 运行端到端评估
2. 识别接口断点（哪两个 skill 之间的数据不匹配）
3. 人工判定应该修改哪个 skill（一般修改下游 skill 适配上游）
4. 在对应的 Phase 中执行定向优化迭代
5. 重新运行端到端评估验证
```

---

## 6. Hyperagents 框架适配

### 6.1 可直接复用的组件

| Hyperagents 组件 | 文件 | 用途 |
|------------------|------|------|
| LLM 接口 | `agent/llm.py` | 调用 Claude/GPT 等模型 |
| 工具调用循环 | `agent/llm_withtools.py` | LLM + bash/edit 工具的 agentic loop |
| Bash 工具 | `agent/tools/bash.py` | 执行 shell 命令 |
| 文件编辑工具 | `agent/tools/edit.py` | 修改 skill 文档 |
| 基础 Agent 类 | `agent/base_agent.py` | ABC 抽象类 |
| Parent 选择 | `select_next_parent.py` | 从 archive 选择 parent |

### 6.2 需要新写的组件

| 组件 | 类型 | 新写原因 | 估算工作量 |
|------|------|---------|-----------|
| `judge_agent.py` | LLM Agent | 统一 Judge Agent，替代原有纯 Python harness+report；调用 Python 工具计算客观指标，LLM 完成综合评分和失败分析 | 中 |
| `eval_tools/compute_metrics.py` | Python 工具 | 被 Judge Agent 调用：计算 MAPE / MaxErr / pass_rate / artifact_completeness 等纯数值指标 | 中 |
| `eval_tools/lint_check.py` | Python 工具 | 被 Judge Agent 调用：对产出 SV 文件运行 svlinter，返回 warning 数量 | 小 |
| `eval_tools/diff_golden.py` | Python 工具 | 被 Judge Agent 调用：与 golden answer 做 diff，返回结构化差异摘要 | 小 |
| `skill_mutator.py` | Python | 变异约束层（限制 Meta Agent 的修改范围） | 小 |
| `mock_mc.py` | Python | MC 编译器 mock（无真实 MC 时用） | 中 |
| `test_fixtures/` | 数据 | 各 Phase 的测试数据集 + golden answers | 大 |

### 6.3 需要适配的组件

| 组件 | 适配内容 |
|------|---------|
| `generate_loop.py` | 去掉 Docker，改为本地目录操作；替换 domain 分发逻辑 |
| `meta_agent.py` | 修改 prompt（"改进 Skill 文档以提升 fitting 成功率"） |
| `task_agent.py` | 修改 prompt（"按照 Skill 文档执行 mem pipeline"） |
| `gl_utils.py` | 去掉 Docker 相关的 patch 应用，改为本地 git patch |

### 6.4 每个 Phase 的进化配置

```python
# Phase 1a: mem_gen 独立优化
phase1a_config = {
    "skill_dirs": ["mem_gen/skill/"],
    "eval_tasks": ["spsram_ulvt", "2prf_lvt"],
    "full_eval_tasks": ["uhd1prf_svt"],
    "staged_eval_fraction": 0.6,
    "max_generations": 40,
    "parent_selection": "score_prop",
    "mutation_constraints": "phase1a_constraints.yaml",
    "score_fn": "compute_phase1a_score",
}

# Phase 1b: mem_predict 独立优化（与 1a 并行）
phase1b_config = {
    "skill_dirs": ["mem_predict/skill/"],
    "eval_tasks": ["spsram_ulvt", "2prf_lvt"],
    "full_eval_tasks": ["uhd1prf_svt"],
    "staged_eval_fraction": 0.6,
    "max_generations": 40,
    "parent_selection": "score_prop",
    "mutation_constraints": "phase1b_constraints.yaml",
    "score_fn": "compute_phase1b_score",
}

# Phase 1c: 接口一致性联合优化（1a + 1b 均达标后启动）
phase1c_config = {
    "skill_dirs": ["mem_gen/skill/", "mem_predict/skill/"],
    "eval_tasks": ["spsram_ulvt", "2prf_lvt"],
    "full_eval_tasks": ["uhd1prf_svt"],
    "staged_eval_fraction": 0.5,
    "max_generations": 15,          # 轻量联合，代数较少
    "parent_selection": "score_prop",
    "mutation_constraints": "phase1c_constraints.yaml",  # 严格约束，保护已固化成果
    "score_fn": "compute_phase1c_score",
}

# Phase 2: mem_selection
phase2_config = {
    "skill_dirs": ["memory_skill/mem_selection/"],
    "eval_tasks": ["S1_easy", "S2_medium", "S3_hard"],
    "full_eval_tasks": ["S4_edge_case", "S5_complex"],
    "staged_eval_fraction": 0.5,
    "max_generations": 30,
    "parent_selection": "score_prop",
    "mutation_constraints": "phase2_constraints.yaml",
}

# Phase 3: mem_replace
phase3_config = {
    "skill_dirs": ["memory_skill/mem_replace/"],
    "eval_tasks": ["fixture_easy", "fixture_medium"],
    "full_eval_tasks": ["fixture_hard"],
    "staged_eval_fraction": 0.6,
    "max_generations": 25,
    "parent_selection": "score_prop",
    "mutation_constraints": "phase3_constraints.yaml",
}
```

---

## 7. 实施路线图

### 7.1 准备阶段（所有 Phase 的前置工作）

```
Step 0.1: 搭建评估基础设施
  ├── 准备 mock MC 编译器或获取真实 MC 访问权限
  ├── 准备各 Phase 的测试 fixture / golden answer
  ├── 实现 eval_harness + eval_report 框架
  └── 部署 LLM API（Claude / GPT-4）

Step 0.2: 适配 Hyperagents 框架
  ├── Fork Hyperagents 代码
  ├── 去掉 Docker 依赖，改为本地执行
  ├── 实现 skill_mutator（变异约束层）
  ├── 适配 generate_loop 和 meta_agent
  └── 运行冒烟测试（1 代进化，验证流程通）
```

### 7.2 Phase 1 执行计划

```
Step 1.0: 公共准备（1a/1b 共用）
  ├── 准备 mock MC 数据：预生成 3 个 type 的完整 DS 文件集
  ├── 实现 compute_phase1a_score() / compute_phase1b_score()
  └── 配置 judge_agent 的 Phase 1a/1b 专用 prompt

── Phase 1a + Phase 1b 并行执行 ──────────────────────────

Step 1a.1: mem_gen 基线评估
  ├── 用当前 mem_gen skill 跑一次完整 MemGen 评估
  └── 记录 baseline score（预期 0.3-0.5，wrapper_interface_match 是主要短板）

Step 1a.2: mem_gen 启动进化
  ├── 运行 Hyperagents 主循环（phase1a_config）
  ├── 每 5 代检查 artifact_completeness + wrapper_interface_match
  ├── 若 8 代无提升，调整 mutation_constraints（放开 tiling 边界修改）
  └── 目标：score ≥ 0.80（约 30-40 代）

Step 1b.1: mem_predict 基线评估
  ├── 用当前 mem_predict skill 跑一次完整 MemPredict 评估
  └── 记录 baseline score（预期 0.4-0.6）

Step 1b.2: mem_predict 启动进化
  ├── 运行 Hyperagents 主循环（phase1b_config）
  ├── 每 5 代检查 group_b_pass_rate + max_error
  ├── 若 8 代无提升，调整 mutation_constraints（放开特征工程修改）
  └── 目标：score ≥ 0.80（约 30-40 代）

── 1a + 1b 均达标后启动 Phase 1c ────────────────────────

Step 1c.1: 接口一致性检查
  ├── 用 1a 最优 skill 生成真实 DS 文件
  ├── 用 1b 最优 skill 直接消费该 DS 文件
  └── 记录接口断点（解析错误、格式不匹配、参数范围冲突）

Step 1c.2: 接口联合优化
  ├── 运行 Hyperagents 主循环（phase1c_config，严格约束）
  ├── 目标：pipeline_handoff_rate ≥ 0.90（约 10-15 代）
  └── 确认 accuracy_delta < 5%（predict 在真实 DS 上精度不下降）

Step 1c.3: 固化最佳 skill
  ├── 更新 mem_gen/skill/ 和 mem_predict/skill/ 目录
  └── 运行回归测试（分别跑 Phase 1a/1b 评估确认未退化）
```

### 7.3 Phase 2 & 3 执行计划

```
Step 2/3.1: 准备独立评估环境
  ├── Phase 2: 实现 mock predictor + golden answers
  ├── Phase 3: 准备 fixture .sv 文件 + lint 环境
  └── 两者可并行准备

Step 2/3.2: 独立进化（可并行执行）
  ├── Phase 2: 25-30 代，目标 score 0.85+
  ├── Phase 3: 20-25 代，目标 score 0.90+
  └── Phase 3 通常更快收敛（评估是确定性的）

Step 2/3.3: 固化最佳 skill
  ├── 更新 memory_skill/mem_selection/
  └── 更新 memory_skill/mem_replace/
```

### 7.4 Phase 4 执行计划

```
Step 4.1: 接口一致性检查
  ├── 运行 interface_consistency_check.py
  ├── 识别所有接口断点
  └── 输出需修复的接口列表

Step 4.2: 定向修复
  ├── 对每个接口问题，判定修改哪个 skill
  ├── 在对应 Phase 的进化框架中做 1-3 代定向优化
  └── 重新检查接口一致性

Step 4.3: 端到端验收
  ├── 运行完整 E2E 场景
  ├── 确认 e2e_score > 0.75
  └── 人工 review 最终的 4 个 skill 文档

Step 4.4: 版本发布
  ├── 提交所有优化后的 skill 文档
  ├── 更新 README.md 中的指标数据
  └── tag v1.0 release
```

### 7.5 收敛标准

| 指标 | Phase 1a | Phase 1b | Phase 1c | Phase 2 | Phase 3 | E2E |
|------|---------|---------|---------|---------|---------|-----|
| 目标 score | ≥ 0.80 | ≥ 0.80 | handoff ≥ 0.90 | ≥ 0.85 | ≥ 0.90 | ≥ 0.75 |
| 最大代数 | 40 | 40 | 15 | 30 | 25 | N/A |
| 连续 N 代无提升则停止 | 8 | 8 | 5 | 8 | 6 | N/A |
| 可否与其他并行 | 与 1b | 与 1a | 否 | 与 Phase 3 | 与 Phase 2 | 否 |
