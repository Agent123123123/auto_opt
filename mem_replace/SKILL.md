---
name: mem-replace
description: >
  Memory 替换 skill。指导 agent 将 RTL 模块内的 memory 行为模型（或留白的 memory 实例化占位符）
  替换成实际的 SRAM macro 实例化（通过 wrapper），同时处理接口适配、BWEB 的取舍、
  generate-for 展开等工程问题。

  命中以下任意一条场景即必须加载：
  - 用户提到 memory 替换、mem replace、把行为模型换成真实 macro；
  - 提到 ARM functional model、RAM wrapper、behavioral SRAM、memory stub；
  - 需要把 TS5N12 / TS1N12 / TS6N12 / TS3N12 等 TSMC macro 包在 wrapper 里；
  - 模块中有 `$readmemh`、`reg [...] mem [...]`、`integer i; always @`、或带
    functional simulation model 注释的 SRAM 模块；
  - 需要为 RTL Block 创建 memory wrapper 文件（*_wrapper.sv）；
  - 需要分析 ARM wrapper 文件中的 BWEB、generate-for、或 power-pin 接口问题。
---

# Mem-Replace Skill

本 skill 指导 agent 完成一次完整的 memory 替换流程，输出可综合的、符合 DFT/PD 要求的 RTL。
流程分为五个阶段，**按顺序执行，不得跳过任何阶段**。

---

## 第一阶段：盘点现有 memory 模型

### 1.1 识别目标文目录

Memory 行为模型和 wrapper 通常位于：
- `rtl/models/rams/` — ARM 处理器风格的 wrapper 文件（每个 SRAM 一个 `.sv` 文件）
- `rtl/models/sram_wrappers/` — TSMC macro wrapper（可能尚未存在，需要创建）
- `rtl/sram/` 或 `rtl/memories/` — 其他项目的 memory 目录

### 1.2 列出所有待替换文件

```bash
# 查找直接实例化 TSMC macro 的文件（名称以 TS 开头的模块）
grep -rln "TS5N12\|TS1N12\|TS6N12\|TS3N12" <rams_dir>/ --include="*.sv"

# 或查找含 behavioral model 特征的文件
grep -rln "\$readmemh\|reg \[.*\] mem \[" <ram_model_dir>/ --include="*.sv"
```

### 1.3 提取每个文件中的 TSMC macro 名称和实例数量

```bash
grep -rn "TS5N12\|TS1N12\|TS6N12" <rams_dir>/ --include="*.sv" \
  | grep -v "^\s*//" \
  | sed 's/.*\(TS[A-Z0-9]*\).*/\1/' | sort | uniq -c
```

输出格式为：**数量 + 完整 macro 名称**。建立一张表：

| ARM wrapper 文件 | TSMC macro 名称 | 实例数量 |
|---|---|---|
| `herculesae_ls_tag_ram.sv` | TS5N12FFCLLL1ULVTA128X98M2W | 12 |
| … | … | … |

---

## 第二阶段：解析 macro 参数 & 识别 family

### 2.1 TSMC macro 命名解码

TSMC N12FFC LL macro 命名格式：

```
TS<comp_no>N12<process>LL<vt><bitcell><words>X<bits>M<mux>[W|F]SHO
      │              │   │   │         │      │   │
      │              │   │   │         │      │   └─ option suffix (W/S/H/O = NonBWEB/NonSLP/NonDSLP/NonSD)
      │              │   │   │         │      └───── mux factor
      │              │   │   │         └──────────── depth（行数）× width（位宽）
      │              │   │   └────────────────────── bitcell (a/b/d)
      │              │   └────────────────────────── VT (ULVT=u, LVT=l, SVT=s)
      │              └────────────────────────────── process (FFC LL)
      └───────────────────────────────────────────── compiler no. → 决定 family
```

**Family 对照表（TSMC N12FFC LL）：**

| comp_no | bitcell | Family | 类型 | addr 宽度公式 |
|---------|---------|--------|------|--------------|
| `1` | `a` | spsram | Single-Port SRAM (ULVT/LVT) | ⌈log₂(depth)⌉ |
| `5` | `a` | **l1cache** | Cache SRAM (ULVT) — *memgen 不支持* | ⌈log₂(depth)⌉ |
| `1` | `b` | shdspsbsram | Shared DSPB SRAM (LVT, `SB` in name) — *memgen 不支持* | ⌈log₂(depth)⌉ |
| `5` | `b` | — | (目前未见) | — |
| `6` | `a/b` | 2prf | 2-Port Register File | ⌈log₂(depth)⌉ |
| `n` | — | 1prf | 1-Port Register File | ⌈log₂(depth)⌉ |

> **⚠️ 重要**：comp_no=5（l1cache）和 shdspsbsram (`TS1N12...SB...`) 两种 family
> **memgen 均不支持**，必须手写 wrapper。

**识别 l1cache vs spsram（同为 comp_no=1）的方法：**
- macro 名中含 `L1ULVT` 或前缀 `TS5N12` → l1cache  
- macro 名含 `ULVTA` + comp_no=1 → spsram ULVT  
- macro 名含 `SBLVTD` 或 `SBSVTD` → shdspsbsram (shared dual-port SP SRAM)

### 2.2 提取关键参数

对每个 macro 名称解析：

| 参数 | 读取方法 |
|------|---------|
| depth | 名称中 `X` 前的数字，如 `256X94` → depth=256 |
| width | 名称中 `X` 后、`M` 前的数字，如 `256X94` → width=94 |
| mux | 名称中 `M` 后面的数字，如 `M2W` → mux=2 |
| addr_bits | ⌈log₂(depth)⌉，如 depth=256 → addr[7:0] |
| has_bweb | macro 名称 **末尾不含 `W`** 则有 BWEB（大写 W = NonBWEB） |
| has_pudelay | spsram/shdspsbsram/l1cache 均有；2prf 通常无 |

---

## 第三阶段：创建 TSMC macro wrapper 文件

每个唯一的 TSMC macro 对应一个 `*_wrapper.sv` 文件，存放在 `rtl/models/sram_wrappers/`。

### 3.1 三条铁律（必须严格遵守）

> **Rule 1 — BWEB 处理**：不需要部分写（partial write）的 SRAM，wrapper 内部把 BWEB
> 全部接常数 `0`（Active Low，即全部 enable 写入），不暴露 bweb 端口。
> 确认需要部分写的 SRAM，才将 bweb 作为 input 端口暴露给外部。
>
> **Rule 2 — 必须使用 wrapper**：上层 RTL 只能实例化 `*_wrapper` 模块，
> **绝对不能直接实例化** TS5N12/TS1N12/TS6N12 等 TSMC macro。
>
> **Rule 3 — 禁止 generate-for 包裹 SRAM 实例化**：
> - 若 generate parameter 是**编译期常量**（如 `L3_WAYS=8`、`EN_W=4`），必须手工展开为显式实例。
> - 若 generate parameter 是**可变参数**（如 `SFRAM_EN_W` 由顶层传入），可保留 generate-for，
>   但需确认 DFT 工具能正确识别（需与 DFT 工程师确认）。

### 3.2 wrapper 接口约定

**统一原则：wrapper 对外使用 active-high 信号命名约定。**

| wrapper 端口 | 含义 | 内部连接 |
|---|---|---|
| `clk` | 时钟 | → `CLK` |
| `cen` | 片选，active-high | `CEB = ~cen` |
| `wen` | 写使能，active-high | `WEB = ~wen` |
| `addr[N-1:0]` | 地址 | → `A` |
| `din[W-1:0]` | 写数据 | → `D` |
| `dout[W-1:0]` | 读数据 | ← `Q` |
| `bweb[W-1:0]` | 字节写使能（仅 partial-write SRAM 暴露） | → `BWEB` |
| `pudelay` | 电源延迟输出（spsram/shdspsbsram/l1cache 暴露） | ← `PUDELAY` |

### 3.3 按 family 的隐藏 pin 处理

**l1cache (TS5N12)：**
```systemverilog
// MCR/MCW: tie to 2'b00（不使用功耗缩减模式）
.MCR(2'b00), .MCW(2'b00),
// BWEB: tie to 0（全部 enable 写入）—— 除非是 partial-write SRAM
.BWEB({WIDTH{1'b0}}),
```

**spsram ULVT (TS1N12...ULVTA...)：**
```systemverilog
// 功耗管理 pin 全部关闭
.SLP(1'b0), .DSLP(1'b0), .SD(1'b0),
// 时序裕量默认值（与 PD 对齐）
.RTSEL(2'b01), .WTSEL(2'b01),
// BWEB: tie to 0（除非 partial-write）
.BWEB({WIDTH{1'b0}}),
// PUDELAY: 通常悬空或由上层接收
```

**shdspsbsram LVT (TS1N12...SBLVTD...)：**
```systemverilog
// 与 spsram ULVT 相同处理方式
.SLP(1'b0), .DSLP(1'b0), .SD(1'b0),
.RTSEL(2'b01), .WTSEL(2'b01),
.BWEB({WIDTH{1'b0}}),
```

**2prf (TS6N12, 双端口 RF)：**
```systemverilog
// 读写分开时钟
.CLKw(clkw), .CLKr(clkr),
// BWEB: 通常 partial-write，保留 bweb 端口
// SLP/DSLP/SD: tie to 0
.SLP(1'b0), .DSLP(1'b0), .SD(1'b0),
// RCT/WCT（timing selection）— 查 compiler datasheet 确认默认值
```

### 3.4 wrapper 文件模板（spsram ULVT 示例）

```systemverilog
// SRAM wrapper: ts1n12ffcllulvta256x72m4swsho
// Family: spsram ULVT | Depth: 256 | Width: 72 | Mux: 4
// BWEB: tied 0 (no partial write)
module ts1n12ffcllulvta256x72m4swsho_wrapper (
    input  logic        clk,
    input  logic        cen,      // chip enable, active-high
    input  logic        wen,      // write enable, active-high
    input  logic [7:0]  addr,
    input  logic [71:0] din,
    output logic [71:0] dout,
    output logic        pudelay
);

TS1N12FFCLLULVTA256X72M4SWSHO u_macro (
    .CLK    (clk),
    .CEB    (~cen),
    .WEB    (~wen),
    .A      (addr),
    .D      (din),
    .BWEB   (72'b0),
    .SLP    (1'b0),
    .DSLP   (1'b0),
    .SD     (1'b0),
    .RTSEL  (2'b01),
    .WTSEL  (2'b01),
    .Q      (dout),
    .PUDELAY(pudelay)
);

endmodule
```

### 3.5 wrapper 文件命名规则

```
<macro_name_lowercase>_wrapper.sv

例：
  TS5N12FFCLLL1ULVTA256X94M2W   → ts5n12ffclll1ulvta256x94m2w_wrapper.sv
  TS1N12FFCLLULVTA4096X72M4SWSHO → ts1n12ffcllulvta4096x72m4swsho_wrapper.sv
```

---

## 第四阶段：修改 ARM wrapper 文件

### 4.1 逐文件分析与替换步骤

对每个含有直接 TSMC macro 实例的 ARM wrapper 文件：

**Step A：读取整个文件，找出：**
1. TSMC macro 实例化语句（`.CEB(...)`, `.WEB(...)` 等 active-low 接口）
2. BWEB 连接逻辑（是否有 per-bit BWEB 赋值？）
3. generate-for 语句包裹的 SRAM 实例

**Step B：判断 BWEB 策略（Rule 1）**
- 检查 BWEB 连接：若全接 `{W{1'b0}}`（全 enable）→ 非 partial-write → wrapper 无 bweb 端口
- 若 BWEB 接入实际数据信号 → partial-write → wrapper 暴露 bweb 端口

**Step C：判断 generate-for 策略（Rule 3）**
- 找到 generate block 中的 loop parameter，追溯其来源
- 若为 `localparam` 或 `parameter` 且已在当前文件中赋值为常量 → **展开为显式实例**
- 若为 propagated parameter（由更上层传入）且取值不固定 → **保留 generate-for**，注释说明

**Step D：替换 macro 实例化**

```systemverilog
// 替换前（直接 TSMC macro）
TS5N12FFCLLL1ULVTA128X98M2W u_tag0 (
    .CLK(clk_i),
    .CEB(~ce_i),
    .WEB(~we_i),
    .A(addr_i),
    .D(wdata_i),
    .BWEB('0),
    .MCR(2'b00),
    .MCW(2'b00),
    .Q(rdata_tag0)
);

// 替换后（使用 wrapper，active-high 接口）
ts5n12ffclll1ulvta128x98m2w_wrapper u_tag0 (
    .clk    (clk_i),
    .cen    (ce_i),
    .wen    (we_i),
    .addr   (addr_i),
    .din    (wdata_i),
    .dout   (rdata_tag0),
    .pudelay()          // pudelay 通常悬空，除非需要连到上层
);
```

### 4.2 generate-for 展开示例

```systemverilog
// 替换前（常量参数 produce 的 generate-for — Rule 3 要求展开）
localparam EN_W = 4;
genvar i;
generate
    for (i = 0; i < EN_W; i++) begin : g_data
        TS1N12FFCLLSBSVTD256X144M4SWSHO u_data (
            .CLK(clk), .CEB(~cen[i]), .WEB(~wen[i]),
            .A(addr), .D(wdata), .BWEB('0),
            .SLP(1'b0), .DSLP(1'b0), .SD(1'b0),
            .Q(rdata[i])
        );
    end
endgenerate

// 替换后（显式展开 4 个实例）
ts1n12ffcllsbsvtd256x144m4swsho_wrapper u_data_0 (.clk(clk), .cen(cen[0]), .wen(wen[0]), .addr(addr), .din(wdata), .dout(rdata[0]), .pudelay());
ts1n12ffcllsbsvtd256x144m4swsho_wrapper u_data_1 (.clk(clk), .cen(cen[1]), .wen(wen[1]), .addr(addr), .din(wdata), .dout(rdata[1]), .pudelay());
ts1n12ffcllsbsvtd256x144m4swsho_wrapper u_data_2 (.clk(clk), .cen(cen[2]), .wen(wen[2]), .addr(addr), .din(wdata), .dout(rdata[2]), .pudelay());
ts1n12ffcllsbsvtd256x144m4swsho_wrapper u_data_3 (.clk(clk), .cen(cen[3]), .wen(wen[3]), .addr(addr), .din(wdata), .dout(rdata[3]), .pudelay());
```

### 4.3 特殊场景：条件实例化（RAM 数量由参数决定）

```systemverilog
// 场景：L3_CACHE_PARTIAL 参数控制某些 SRAM 是否实际使用
// 原始：generate 展开 6 个实例，后 2 个受 L3_CACHE_PARTIAL 控制
// 处理：展开全部实例，条件通过 cen 信号门控

// instance 6 & 7（仅 PARTIAL 模式下有效）
ts5n12ffclll1ulvta512x76m2w_wrapper u_tag_6 (
    .clk  (clk),
    .cen  (L3_CACHE_PARTIAL ? tagram_ce[6] : 1'b0),  // 非 PARTIAL 模式永远不使能
    .wen  (tagram_we[6]),
    .addr (tagram_addr[6]),
    .din  (tagram_wdata[6]),
    .dout (tagram_q_6),
    .pudelay()
);
```

---

## 第五阶段：验证

### 5.1 语法检查

```bash
# 用 VCS 或 Xrun 检查语法（不需要跑仿真）
vcs -sverilog -compile <file>.sv <wrapper>.sv
# 或
xrun -compile -sv <file>.sv <wrapper>.sv
```

### 5.2 完成度验证

```bash
# 确认没有文件还在直接例化 TSMC macro
grep -rn "TS5N12\|TS1N12\|TS6N12" <rams_dir>/ --include="*.sv" \
  | grep -v "//.*TS" \
  | grep -v "_wrapper\.sv:"  # wrapper 文件内部的实例化是合法的
```

预期输出：**只有 `*_wrapper.sv` 文件中有 TSMC macro 名称，其他所有 RAM wrapper 文件零匹配。**

### 5.3 wrapper 文件完整性检查

```bash
# 确认每个 SRAM macro 都有对应的 wrapper 文件
ls rtl/models/sram_wrappers/*_wrapper.sv
```

---

## 附录 A：TSMC N12FFC LL Family 端口速查

### spsram (TS1N12...ULVTA...)

| 端口 | 方向 | 说明 | wrapper 处理 |
|------|------|------|-------------|
| CLK | in | 时钟 | 透传 |
| CEB | in | 片选，active-LOW | `~cen` |
| WEB | in | 写使能，active-LOW | `~wen` |
| A[N-1:0] | in | 地址 | 透传 |
| D[W-1:0] | in | 写数据 | 透传 |
| BWEB[W-1:0] | in | 字节写使能，active-LOW | 全 0（非 partial-write） |
| SLP | in | Sleep mode | 接 0 |
| DSLP | in | Deep sleep | 接 0 |
| SD | in | Shutdown | 接 0 |
| RTSEL[1:0] | in | 读 timing 选择 | 接 2'b01 |
| WTSEL[1:0] | in | 写 timing 选择 | 接 2'b01 |
| Q[W-1:0] | out | 读数据 | 透传 |
| PUDELAY | out | 上电延迟状态 | 暴露给上层或悬空 |

### l1cache (TS5N12...L1ULVTA...)

| 端口 | 方向 | 说明 | wrapper 处理 |
|------|------|------|-------------|
| CLK | in | 时钟 | 透传 |
| CEB | in | 片选，active-LOW | `~cen` |
| WEB | in | 写使能，active-LOW | `~wen` |
| A[N-1:0] | in | 地址 | 透传 |
| D[W-1:0] | in | 写数据 | 透传 |
| BWEB[W-1:0] | in | 字节写使能 | 全 0（非 partial-write） |
| MCR[1:0] | in | Memory control read | 接 2'b00 |
| MCW[1:0] | in | Memory control write | 接 2'b00 |
| Q[W-1:0] | out | 读数据 | 透传 |
| PUDELAY | out | 上电延迟 | 暴露或悬空 |

### 2prf (TS6N12...)

| 端口 | 方向 | 说明 | wrapper 处理 |
|------|------|------|-------------|
| CLKw | in | 写时钟 | 透传 |
| CLKr | in | 读时钟 | 透传 |
| WEB | in | 写使能，active-LOW | `~wen` |
| WADDR[N-1:0] | in | 写地址 | 透传 |
| D[W-1:0] | in | 写数据 | 透传 |
| BWEB[W-1:0] | in | 字节写使能 | 通常暴露（RF 多为 partial-write） |
| REB | in | 读使能，active-LOW | `~ren` |
| RADDR[N-1:0] | in | 读地址 | 透传 |
| SLP/DSLP/SD | in | 功耗模式 | 接 0 |
| RCT[1:0] | in | 读 timing | 接 2'b01 |
| WCT[1:0] | in | 写 timing | 接 2'b01 |
| KP[2:0] | in | Keep power | 接 3'b001 |
| Q[W-1:0] | out | 读数据 | 透传 |

---

## 附录 B：常见错误与排查

| 错误 | 原因 | 解决方法 |
|------|------|---------|
| `port CEB: unresolved` | wrapper 暴露的是 `cen`，上层仍接 `CEB` | 检查上层 RAM wrapper 的端口名是否已切换到 active-high |
| `zero-width port` | addr 宽度计算错误（如 depth=1024 误算为 9 位） | 重新计算 ⌈log₂(depth)⌉ |
| `generate loop with constant` | Rule 3 违规 | 展开为显式实例 |
| `BWEB connected but wrapper has no bweb` | 上层仍在驱动 BWEB | 检查是否需要 partial-write；若不需要则在上层删除相关逻辑 |
| `pudelay undriven output` | wrapper 的 pudelay 悬空 | 在实例化时接空 `.pudelay()` （SystemVerilog 允许输出悬空） |
| `unsupported family` | 用 memgen 生成了不支持的 family | 手写 wrapper，参考附录 A |
