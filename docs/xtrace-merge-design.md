# xtrace × firefox-reverse：差异分析与"更强版 Chromium"融合设计

> 分析日期：2026-08-27。参照 [xtrace/XTRACE-REPORT.md](../../xtrace/XTRACE-REPORT.md)（已实物核验：4 补丁 26,939 行、0001 触及 95 文件、0002 触及 126 文件、Mojo 接口仅 Log/LogBatch/LogAsset、0002 在 interpreter-generator.cc 只钩位运算家族而非全量派发）、[chromium-migration-analysis.md](chromium-migration-analysis.md)（本仓迁移分析）。
>
> 一句话结论：**xtrace 和 firefox-reverse 在同一问题的两层上各自做到了极致，且几乎不重叠**——xtrace 是"被动示波器"（V8/Blink 内建 API 边界的语义级家族 trace，基建极其讲究），firefox-reverse 是"主动工作台"（逐字节码派发 trace + 全套 Agent 产品）。把两者叠在一个 Chromium 上，得到的能力不是相加而是相乘：**采集层证明"发生了什么"，分析层还原"算法是什么"**。

---

## 1. 定位差异：示波器 vs 工程师

| | xtrace | firefox-reverse |
|---|---|---|
| 本质 | 内建于补丁版 Chromium 的**被动运行时追踪器** | 内建于补丁版 Firefox 的**AI 逆向 Agent + 指纹环境管理器** |
| 产出 | 一份结构化 NDJSON（schema v1/v2，九家族证据） | 可脱离浏览器运行的 Node/Python 算法 + 隔离指纹环境 |
| 使用方 | 人 + Python CLI/Electron GUI 做**离线**分析 | 内置 Agent **在线**全流程自动推进（66 工具循环） |
| 交互性 | 启动期开关（switch/env），跑起来只观测 | 运行期 ctl/dump/clear 控制文件，秒级启停/换 filter |
| 反检测 | 只**记录**指纹 API 读取，不改值 | **伪装**指纹（C++ 层覆盖）+ 自动化痕迹隐藏 |
| 分发 | 本机四步流水线自建（revision 锁定 151.0.7917.0） | 五平台安装包 Release 分发（锁 153.0a1） |

## 2. 引擎层钩子：正交的两层

这是全部分析里最重要的发现——**两者在 V8/引擎里钩的是不同的层，合并后覆盖 JSVMP 逆向的两半**：

### 2.1 xtrace：内建 API 边界（语义层，"VMP 用了什么原语"）

- **Torque/CSA 层钩 V8 内建函数**（~120 文件）：Array/String/TypedArray/Promise/RegExp/DataView/Reflect/Object/Proxy 等，调用点带 `API 名 + shape + 实参值`；
- **解释器内选择性钩**：`interpreter-generator.cc` 只钩位运算 handler（`Bitwise.and/or/xor/Shift.*`，带 left/right/result）——服务于 `int_bitwise` 家族，**不做全量派发 trace**；
- **动态派发点**：arm64 手写汇编 trampoline 钩 `Reflect.apply / Function.prototype.call/apply / Reflect.construct`（xtrace 自己的分析也承认汇编层最脆弱，建议弃用）；
- **九个 JSVMP hook 家族**：base64 · text_codec · byte_buffer · dynamic_dispatch · proxy_trap · hash_crypto · int_bitwise · anti_debug_timing · source_probe；
- **WASM JS-API 边界**（wasm-js.cc 纯 C++ 回调）；
- **关键免疫性：对 JIT 分层免疫**。无论调用方函数跑到 Sparkplug/Maglev/TurboFan 哪一层，JS 调用内建函数永远进入 builtin 代码——钩子不会因 tier-up 丢失。**无需 JIT 钉死**。

### 2.2 firefox-reverse：字节码派发（结构层，"虚拟 PC 走到哪"）

- 钩 `js::Interpret()` 的 `ADVANCE_AND_DISPATCH` 宏——**每条字节码**都记录 `{sid,pc,op,ln,col,imm}`；
- `_vpc/_args/_locals/_ret` dump 重建 JSVMP 的**虚拟程序计数器**与派发器状态；
- 代价一：海量输出（需要 limit 锁防 4GB 死循环）；代价二：**必须 JIT 钉死**（把 Baseline/Ion 预热阈值设 UINT32_MAX），否则热 dispatcher 逃出解释器、trace 断流；
- 配套离线链：dispatcher 热点识别 → `dispatcher_split.js`（Babel AST 拆 handler）→ `disassemble.js`/`vpc_resolve.js`（字节码→伪汇编）→ LLM 命名 → 纯算重写。

### 2.3 为什么两层都要

JSVMP 逆向的两个正交问题：

| 问题 | 单靠 xtrace | 单靠 firefox-reverse | 合并 |
|---|---|---|---|
| "VMP 解释器调用了哪些原生原语"（hash/base64/位运算/字节读写，带真值） | ✅ 家族钩子直出 | 🟡 只能在字节码流里看到 Call 类 opcode，无值 | ✅✅ |
| "虚拟程序执行到哪一步"（vpc 重建、控制流、handler 边界） | ❌ | ✅ 派发 trace 直出 | ✅✅ |
| "签名输入→运算→输出的关联" | 🟡 provenance 链（handler_arg/register/handler_return 引用） | 🟡 dump 拼接 | ✅ provenance × vpc 对齐 |
| "哪个 WebAPI 被读了什么"（补环境清单） | ✅ curated Blink 钩子（~100 文件） | ✅ codegen 全量 WebIDL | ✅✅ 全量 + 深度 |

## 3. 基建对比：xtrace 有五项 frx 迁移时正好需要的底座

对照 [chromium-migration-analysis.md](chromium-migration-analysis.md) §3-§4 的迁移风险清单，xtrace 已经把其中最贵的几项趟过了：

| 基建项 | xtrace 做法 | frx 现状 / 迁移痛点 | 结论 |
|---|---|---|---|
| **渲染器→落盘通道** | **沙箱保持开启**，事件经 `blink.mojom.XTraceHost` Mojo IPC 传出，浏览器进程持有文件句柄落盘（`MayBlock` 线程池 + `SKIP_ON_SHUTDOWN`） | renderer 直接写 `/tmp` per-pid 文件，**Windows 需 `_putenv(MOZ_DISABLE_CONTENT_SANDBOX)` 禁内容沙箱**；迁移报告 R4 风险项 | **直接采用 xtrace 方案**，frx 的 per-pid NDJSON 语义在上层保留 |
| **跨进程全序** | 落盘前盖 `global_seq`（浏览器进程统一编号） | 无（per-pid 文件天然无全序） | 采 xtrace |
| **V8↔Blink 解耦协议** | V8 补丁只认 `isolate->event_logger()` + embedder slot 暗号 `0x5854524143450`，零 Blink 头文件依赖，两补丁独立审查 | 单树（SpiderMonkey 无此问题） | 采 xtrace（V8 独立 git 树本来就是 Chromium 结构） |
| **生成代码里插桩的可行性证明** | Torque `.tq` 调 `extern runtime XTraceVmpRuntime` / CSA `CallRuntime` / **解释器 handler 内 CallRuntime（位运算家族）**——全都是"生成代码→运行期 C++"通道 | 迁移报告 §3.2 提议钩 `InterpreterAssembler::Dispatch()`，当时标注 CSA 层插桩是最高成本项（R1 风险） | **xtrace 已实证 CSA 层 CallRuntime 模式可 work**——frx 全量派发 trace 的落地机制被它验证了一半 |
| **值捕获分级与资产** | `capture-values = full/summary/args-only`、`capture-assets`、sha1 引用去重、`--xtrace-max-body-bytes` 限额 | 字符串 16KB 截断 + TypedArray 1024 上限（写死） | 采 xtrace 的分级配置 |
| **因果结构（schema v2）** | `XTraceActivationScope` 因果栈：事件带 `call_id/parent_id/depth`，成对 call/return，网络事件标 `external` | 无 | 采 xtrace |
| **补丁工程** | 三态 preflight（apply/applied/3way）、component build 秒级增量、solink 局部重链、debug/release 双出 | 幂等字符串替换脚本 + mach 增量；五平台交叉发布流水线 | 各取所长：xtrace 的迭代内环 + frx 的发布外环 |
| **可验证性** | `validate_trace.py` 要求九家族**必须带证据**（值/引用/结果），配套自包含测试页 | JS 层 selftest（工具路由/环境管理），无 trace 质量校验 | 采 xtrace（引擎补丁的 CI 只能靠它） |

反向地，xtrace 缺的正是 frx 的全部产品层：Agent 引擎（Loop/Router/LLM/护栏/记忆/Skills）、66 工具、侧边栏 UI、环境管理器（profile/进程/端口编排）、指纹伪装层、离线白盒工具链、MCP director 对接。**xtrace 到目前为止的"消费端"是人肉 Python 脚本；frx 的消费端是一个会自己干活的 Agent。**

## 4. 融合设计：chromium-reverse（工作代号）

### 4.1 总架构

```
┌────────────────────────── chromium-reverse（单一补丁集，锁 xtrace 同款 revision 流程）──────────────────────────┐
│                                                                                                                    │
│  采集层（引擎补丁，常驻插桩 + 运行期分级开关）                                                                       │
│  ├─ 传输底座（承 xtrace 0001）: XTraceHost Mojo 通道 · 沙箱保持 · global_seq · NDJSON schema v2 · 因果栈            │
│  │   + 新增控制面: XTraceControl（浏览器进程读开关/env 兜底 + CDP 自定义域 XTrace.* 运行期切换）                     │
│  ├─ V8 家族钩子（承 xtrace 0002）: Torque/CSA 内建钩子 · 九家族带值 · WASM 边界                                      │
│  │   改造: 弃 arm64 汇编层，动态派发点改 CSA/Torque 实现（跨架构、可维护）                                            │
│  ├─ V8 全量派发 trace（新，承 frx 经验）: InterpreterAssembler::Dispatch() 单点 + 门控 fast path                    │
│  │   + JIT 钉死联动（开启派发 trace 时 --js-flags 钉 tier）→ vpc 重建管线                                            │
│  ├─ Blink 全量 WebIDL trace（新，承 frx Codegen.py 理念）: bind_gen 生成器注入 RAII（自动覆盖全部接口）               │
│  │   + xtrace curated 深钩保留（crypto/storage/network 等高价值文件带 body/assets）                                  │
│  └─ 指纹配置层（新，承 frx FrxFingerprintConfig 理念）: 启动期 JSON 覆盖 Navigator/Screen/Intl/HTTP/WebGL           │
│      （分档：CDP override 覆盖大半 + Blink patch 补 hardwareConcurrency/WebGL 任意串/canvas/audio 噪声）              │
│                                                                                                                    │
│  暴露面: --remote-debugging-port (CDP 全域 + 自定义 XTrace.* 域) · trace.ndjson + assets/ 落盘                       │
└────────────────────────────────────────────────────────────────────────────────────────┘
                    ▲ CDP/文件                                    ▲ 读写 NDJSON/控制
┌──────────────────┴──────────────────────────────┐ ┌───────────┴──────────────┐
│  Agent 层（承 frx L1，原样搬运）                  │ │ frx-director-mcp 对接     │
│  native messaging host(Node) 承载引擎:           │ │ （控制面 = CDP XTrace.*  │
│  AgentLoop/ToolRouter/LlmClient/记忆/Skills      │ │  + host 消息入口）        │
│  MV3 sidePanel UI · 66+ 工具 · env_* 环境管理    │ └──────────────────────────┘
│  （每环境 = 独立 --user-data-dir + CDP 端口       │
│   + 独立指纹 JSON + 独立 trace 配置）             │
└─────────────────────────────────────────────────┘
```

### 4.2 采集层合并细节

**补丁序列（V8 独立树协议沿用 xtrace）**：

| # | 来源 | 内容 | 改造点 |
|---|---|---|---|
| 0001 | xtrace | 原生日志通路（Mojo + host + mojom + Blink 深钩） | 接口加 `SetConfig(string json)` 控制方法；全局 gate 沿用"插桩常驻、开关决定记录" |
| 0002 | xtrace | V8 家族钩子（Torque/CSA/runtime/门闸暗号/WASM 边界/provenance） | **删 arm64 汇编 trampoline**，`Reflect.apply/call/apply/construct` 四个派发点改 CSA 层（xtrace 文档自己建议的方向）；`Runtime_XTrace*` 三个函数保留 |
| 0003/0004 | xtrace | 因果 schema v2 | 原样 |
| 0005 | **新（frx 经验）** | Ignition 全量派发 trace：钩 `InterpreterAssembler::Dispatch()` + `Jump/JumpBackward`（或收敛点 `DispatchToBytecodeHandlerEntry`），门控单分支读全局 flag，cold path CallRuntime 进 C++ 落 `{sid,pc,op,ln,col}` | 事件走 event_logger 新前缀 `xtrace:ignition:`；与 0002 门闸协议一致；开启时联动 JIT 钉死（host 侧重启进程加 `--js-flags`，避免 V8 flag 冻结问题）；限流/上限锁照搬 frx |
| 0006 | **新（frx 理念）** | bind_gen 生成器注入 RAII（全量 WebIDL method/getter） | 命名编译期字面量、析构拿 rval、反扰动序列化（TypedArray 用 v8 原生数据指针，比 SM 版更干净）；以幂等替换脚本维护（frx apply-fingerprint-config.py 模式），降低 bind_gen 上游 churn 的冲突面 |
| 0007 | **新（frx 理念）** | 指纹配置层：启动期读 `FRX_FINGERPRINT_JSON` | 第一档 CDP override（UA/UA-CH/时区/locale/screen——由 host 在 CDP 会话上应用）；第二档启动开关；第三档 Blink patch（hardwareConcurrency/WebGL 任意串/canvas/audio 噪声/字体，参考 Brave farbling） |

**采集分档（合并 xtrace categories 与 frx 交互控制）**：

```
常开低耗（默认）: 家族钩子（xtrace，对 tier 免疫） + 全量 WebIDL（frx codegen，门控关闭时近零开销）
按需开启（Agent 一键）: 派发 trace（frx，进程重启钉 tier） + dump 系列（args/locals/vpc/ret）
运行期可切: categories/filter/values 分级（xtrace 配置面经 XTraceControl 下发，frx ctl 理念）
```

### 4.3 Agent 层：66 工具的升级与新增

frx 工具映射照 [chromium-migration-analysis.md](chromium-migration-analysis.md) §6 执行；xtrace 数据面给工具层带来的直接升级：

| 升级 | 旧实现（frx/Firefox） | 新实现（合并版） |
|---|---|---|
| `net_capture` | nsIObserver 双进程拼 initiator 栈 | 引擎内 browser-network 事件直接带 global_seq + external 因果标记，零拼装 |
| `scripts_capture_all` | page_eval 枚举 + fetch 抓源码 | 引擎 `source_probe/dynamic-code` 资产通道自动落全部脚本（含 eval/new Function 源文本），资产 sha1 去重 |
| `crypto_scan` | 离线 JS 常量扫描 | 叠加 `hash_crypto` 家族命中（SubtleCrypto 明文输入/输出 + 调用栈），**在线直出**加密材料 |
| `signer_trace` | SM Debugger onEnterFrame | 三路：家族钩子带值 + 因果栈定位 + CDP Debugger 域（无横幅进程内 agent host） |
| `whitebox_diff` | collectCoverageInfo | `Profiler.takePreciseCoverage` + 家族事件对齐 |
| **新增 `vpc_reconstruct`** | （离线 Python 手动） | 派发 trace NDJSON → dispatcher 识别（热点排序）→ handler 拆分 → 伪汇编，Agent 全自动串联（frx tools/*.cjs 原样复用） |
| **新增 `trace_query`** | jsvmp_query/webapi_query 各自为政 | 统一查询：by category / family / api / causality(call_id,depth) / provenance 引用链 |
| **新增 capture 质量自检** | 无 | Agent 收 trace 后先跑 validate（九家族证据要求），不合格自动调参重采——把 xtrace 的可验证性变成 Agent 的自纠错回路 |

### 4.4 环境管理器合并

`env_*` 在合并版里升级为**一键生成"带指纹伪装 + 带采集配置"的隔离实例**：

```
env_open(id) →
  chromium-reverse --user-data-dir=<env>/profile
    --remote-debugging-port=<n>            ← 自动化 + XTrace.* 控制域
    --xtrace-file=<env>/traces/trace.ndjson ← 每环境独立 trace
    --xtrace-categories=reverse,fingerprint
    FRX_FINGERPRINT_JSON=<env>/fingerprint.json
    [--js-flags=... 仅当该环境开了派发 trace]
```

端口扫描/真实 bind 探测/进程状态机/macOS `exec -a` 伪装——EnvironmentBackend 逻辑原样移植（frx → native host）。

## 5. 需要新做的 Delta（不在任何一方现成代码里）

1. **XTraceControl 运行期控制通道**：mojom `SetConfig` + CDP 自定义域 `XTrace.*`（frx ctl 理念 × xtrace 配置面）。CDP 域同时是 MCP director 的控制面。
2. **Ignition 全量派发 trace（0005）**：xtrace 验证了 handler 内 CallRuntime 可行，但全量派发仍需 Dispatch() 单点 + 性能验证（先做 1/N 采样版验证管道）。这是唯一的高风险新工程。
3. **bind_gen RAII 注入（0006）**：机制同 frx Codegen.py，落点在 Python 生成器。
4. **动态派发点去汇编化**：Reflect.apply/call/apply/construct 四点改 CSA（xtrace 自己的演进方向）。
5. **指纹第三档 Blink patch（0007）**：WebGL 任意串/hardwareConcurrency/canvas/audio 噪声（Brave farbling 参考）。
6. **Agent 层接线**：frx L1 进 native host + CDP 后端替换（迁移报告 §2/§6 已规划）。

## 6. 分阶段路线（修订版，替代迁移报告的 C2 细化）

| 阶段 | 内容 | 依赖 | 估算 |
|---|---|---|---|
| M0 | 以 xtrace 流水线起 repo（bootstrap/apply/gn/build 原样）→ 本机出增强 Chromium，跑通九家族验证 | 无（xtrace 现成） | 1-2 天（首次构建数小时） |
| M1 | frx L1 引擎进 native host + MV3 面板 + CDP 后端工具族（page/net/cookies/fs/run_*） | M0 产物可当普通 Chromium 用 | 2-4 周 |
| M2 | XTraceControl（mojom SetConfig + CDP XTrace.* 域）+ host 侧 trace 消费工具（trace_query/资产/crypto_scan 升级/质量自检） | M1 | 1-2 周 |
| M3 | bind_gen RAII 全量 WebIDL（0006）+ xtrace curated 深钩去重 | M2 | 1-2 周 |
| M4 | Ignition 派发 trace（0005，先采样版后全量）+ vpc_reconstruct 管线 + JIT 钉死联动 | M2 | 3-5 周 ★关键路径 |
| M5 | 指纹层（CDP override 档 + Blink patch 档）+ env_* 合并 | M2 | 2-3 周 |
| M6 | 动态派发点 CSA 化、arm64 汇编层退役、多平台发布（frx 发布流水线平移） | M4/M5 | 2 周 |

合计 ~3 个月内到全功能形态；M1 结束即有"Agent + 原版可用的增强浏览器"的可用产物。

## 7. 决策点与风险

| # | 决策/风险 | 建议 |
|---|---|---|
| D1 | 基线 revision：xtrace 锁 151.0.7917.0（2026-06-27 main） | 沿用，与 xtrace 补丁零冲突起步；季度按 xtrace §12.2 流程 rebase（3-way preflight + 长分支 format-patch） |
| R1 | Ignition 全量派发 trace 性能（最高风险） | xtrace 已证 handler 内 CallRuntime 可行；Dispatch() 单点 + 全局 flag 门控 + 1/N 采样降级；release 构建（xtrace 实测 debug 慢一个数量级，frx 钉 tier 后更甚） |
| R2 | tier 钉死 vs V8 flag 冻结 | 派发 trace 按需开 + 进程重启时带 `--js-flags`；家族钩子不受 tier 影响、常开 |
| R3 | bind_gen 上游 churn | 幂等替换脚本 + 每季度随 D1 一并 rebase |
| R4 | debug(组件化,迭代) vs release(分发) 双构建并存 | xtrace 已有全套脚本，直接沿用 |
| R5 | 双仓协同（frx Firefox 线继续演进） | L1 引擎层抽共享包；采集层补丁两边独立、schema 对齐（frx NDJSON 行类型并入 xtrace schema v2 字段族） |
| R6 | trace 体积（派发 trace 海量） | xtrace 分级捕获 + frx 上限锁/clear 机制合并；vpc 类事件单独 category 默认关 |

## 8. 结论

1. **两项目能力几乎零重叠**：xtrace 把"采集正确的数据"工程化到极致（沙箱保留的 Mojo 通路、九家族证据、因果 schema、可验证闭环）；frx 把"用数据还原算法"工程化到极致（Agent 全流程、vpc 重建、环境隔离、指纹伪装、白盒工具链）。
2. **xtrace 直接消解了 frx→Chromium 迁移的两大最贵风险**：渲染器沙箱落盘（R4，Mojo 方案现成）与"生成代码里能不能插桩"（R1，CSA CallRuntime 已实证）。
3. **frx 补上 xtrace 的全部缺口**：交互性（运行期控制）、自动化（Agent）、伪装（指纹层）、隔离（环境管理）、以及 vpc 这一层 xtrace 没有的结构化 trace。
4. 融合的关键路径只有一条新工程：**0005 Ignition 全量派发 trace**（M4）。其余都是"搬运 + 接线"。
5. 按此路线，**M1（约一个月内）即可得到"原版 Chrome 可用 + 增强 Chromium 可选"的 Agent 产品，M4 后得到当前世上覆盖最全的 JSVMP 分析浏览器**：家族语义 × 虚拟 PC × 因果链 × 主动 Agent × 指纹环境，五层闭环。
