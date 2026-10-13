# 把 Firefox-Reverse 的浏览器增强能力迁移到 Chromium —— 深度分析

> 状态：分析报告（2026-08-27）。基于对 `additions/`、`patches/`、`scripts/`、`docs/` 与 agent-sidebar 全部 22 个 `.sys.mjs` 模块的代码级审读，以及对 V8 / Blink / CDP / Chromium 构建体系的上游机制核实。
>
> **后续进展**：已结合 xtrace（补丁版增强 Chromium，`../xtrace/`）的现成基建形成融合设计，见 [xtrace-merge-design.md](xtrace-merge-design.md)——本报告的路线 C 可大幅提速，xtrace 的 Mojo 传输/CSA 插桩实证直接消解 R1/R4 两大风险。
>
> 结论先行：**可行，且相当一部分资产可直接复用**。项目自身的架构分层（纯 JS 引擎层 / 特权工具层 / C++ 引擎观测层）天然决定了迁移成本呈金字塔分布——约 60% 代码近乎原样搬运，25% 换 API 家族重写，15%（引擎级观测 + 指纹 C++ 层）需要在 V8/Blink 里重做，但对应钩子点在 Chromium 里**存在且更干净**。

---

## 0. 执行摘要

| 迁移件 | 现状（Firefox 侧） | Chromium 侧对应 | 成本 |
|---|---|---|---|
| Agent 对话引擎（Loop/Router/LLM/护栏） | 纯 JS，作者已按"零 Firefox 依赖 + DI 注入"设计 | 原样复用 | 极低 |
| 侧边栏 React UI | esbuild bundle 进 omni.ja | MV3 `sidePanel` 或 fork 内 WebUI | 低 |
| 页面自动化 9 工具 | JSWindowActor + Cu.Sandbox | `chrome.scripting`(MAIN world) 或进程内 Inspector `Runtime.evaluate` | 中 |
| 网络抓包 + initiator 栈 | nsIObserver `http-on-*` 双进程拼栈 | CDP `Network` 域（initiator.stack 白送）或 URLLoaderThrottle | 中（CDP 路线反而更省） |
| signer_trace / closure_read / whitebox | SpiderMonkey Debugger API（特权 JS） | V8 Inspector：`Debugger.pause` + `Runtime.getProperties(scopeChain)` + `Profiler.takePreciseCoverage` | 中高（编程模型重写，语义可达） |
| **JSVMP 逐指令 trace** | 补丁 `js::Interpret()` 的 `ADVANCE_AND_DISPATCH` 宏 | 补丁 V8 `InterpreterAssembler::Dispatch()` 中央派发点（已核实存在，且是单点收敛，比宏展开更干净） | **高（核心重做）** |
| **WebAPI trace** | 补丁 `dom/bindings/Codegen.py` 注入 RAII | 补丁 Blink `bindings/scripts/bind_gen/` 代码生成器（已核实模板层已迁移为 Python 包） | **高（核心重做，思路 1:1）** |
| 指纹配置层（C++） | FrxFingerprintConfig hook 16 处上游文件 | **分治**：CDP `Emulation/Network.setUserAgentOverride(userAgentMetadata)` 覆盖 ~60% 字段（零补丁）；其余（hardwareConcurrency/WebGL 串/canvas/audio 噪声）少量 Blink patch | 中 |
| UA-CH（navigator.userAgentData） | 需整套新增 NavigatorUAData WebIDL 实现 | **Chromium 原生已有**，只需覆盖值（CDP userAgentMetadata 直接生效） | 消失（白赚） |
| 指纹环境管理器（env_*） | 独立 profile + 进程 + Marionette 端口 | 独立 `--user-data-dir` + `--remote-debugging-port`，启动/端口扫描逻辑原样移植 | 低 |
| run_node/run_python/fs_* | Subprocess.sys.mjs / IOUtils | Native Messaging Host（Node 常驻进程）——架构上反而更自然 | 中 |
| 跨会话 SQLite 记忆 | Sqlite.sys.mjs | better-sqlite3（native host 内） | 低 |
| 构建体系 | mach + 补丁集 + rsync additions | depot_tools + GN/ninja + 同款补丁集模式（Brave/ungoogled-chromium 先例） | 中（基建一次性） |

**三条可选路线**（§9 详述）：

1. **A｜全对等 fork**：补丁版 Chromium（V8/Blink/侧边栏全打），架构与 Firefox 版 1:1。能力最全，工程量最大。
2. **B｜扩展壳 + 原生宿主**：MV3 sidePanel + native messaging host，跑在**原版 Chrome** 上。最快可用，但引擎级观测（jsvmp/webapi trace、隐形指纹）缺失。
3. **C｜混合（推荐）**：Agent 引擎与工具壳先以扩展 + native host 落地（兼容原版 Chrome）；同时构建 chromium-reverse fork，把 V8/Blink 引擎层补丁和指纹 C++ 层做进去；引擎级工具只在 fork 进程上解锁（env 打开 fork 进程，走 CDP 端口）。**这正是 Firefox 版自己走过的路**（`docs/agent-native-capabilities.md` §2 的"混合策略"：先用在树 DevTools/BiDi 模块快速跑通，热点路径再下沉 C++）。

---

## 1. 现状盘点：四层能力与 Firefox 耦合度

### 1.1 分层

```
┌─ L1 Agent 引擎与 UI（纯 JS，~60% 代码）────────────────────┐
│  AgentLoop / ToolRouter / LlmClient / providers / Tools 声明表 │
│  AgentSession 状态机 / React 面板 / 离线分析器 tools/*.cjs      │
│  设计上零/近零 Firefox 依赖（Node 可自测即证明）              │
├─ L2 特权工具层（JS，依赖 Gecko 设施，~25% 代码）──────────┤
│  JSWindowActor 页面自动化 / nsIObserver 抓包 / nsICookieManager │
│  Subprocess.sys.mjs / IOUtils / Sqlite.sys.mjs /               │
│  SpiderMonkey Debugger API（signer/closure/coverage）          │
├─ L3 引擎级观测（C++ 补丁，~10% 代码，核心差异化）────────┤
│  JsvmpTraceCore：js::Interpret() ADVANCE_AND_DISPATCH 逐 op     │
│  WebApiTraceCore：Codegen.py 注入 RAII 到全部 WebIDL binding    │
├─ L4 指纹配置层（C++ + JS，~5% 代码）─────────────────────┤
│  FrxFingerprintConfig：16 处上游 hook（Navigator/Screen/Intl/  │
│  HTTP 头/WebGL）+ NavigatorUAData 新增 WebIDL                  │
│  EnvironmentBackend：独立 profile/进程/Marionette 端口编排      │
└────────────────────────────────────────────────────────┘
```

### 1.2 关键架构事实（决定迁移成本）

1. **数据/控制通道是引擎无关的**。整套 C++ 观测层靠"per-pid NDJSON 数据面 + ctl/dump/clear 控制文件"传输（`/tmp/firefox-reverse-jsvmp-b.ndjson.<pid>` 等），不经过 JS 引擎 API、不走网络。这套机制在 Chromium 上**原样可用**——JsvmpBackend/WebApiBackend 的尾窗读取、filter 版本号失效、mtime 过期、上限锁设计全部照搬。
2. **四层观测分层是通用方法论**，逐层平移即可：
   - 逐 opcode → 必须引擎 C++ 解释器内 + JIT 钉死；
   - WebIDL 边界 → binding 代码生成器插桩（编译期字面量命名 + RAII 拿 rval）；
   - 帧级/环境级 → 特权 Debugger 观测（有帧数/墙钟安全阀）；
   - 纯 JS hook → page_eval 注入（最便宜但可检测）。
3. **Agent 引擎的 Firefox 依赖集中在持久化与进程操作**（IOUtils/PathUtils/Services.prefs/Sqlite/Subprocess），对话循环、上下文压缩、LLM 双协议、防绕圈护栏全部纯 JS。
4. **66 工具声明表带 `_need` backend 探测**（`Tools.sys.mjs` 尾部），backend 缺席时工具自动隐藏——迁移可以增量进行，同一份声明表在原版 Chrome（子集）和 fork（全集）上都成立。

---

## 2. L1：Agent 引擎与 UI —— 直接搬运

### 2.1 可原样复用（改 import 即可）

- `ToolRouter.sys.mjs`（144 行，零依赖，作者明示设计约束）
- `AgentLoop.sys.mjs`（948 行：tool_use 循环、按模型窗口分档的上下文预算、主动压缩 + LLM 交接摘要、同参同结果熔断/连续失败/无进展护栏、视觉回喂）
- `LlmClient.sys.mjs`（727 行：OpenAI + Anthropic 双协议、SSE 流式、空闲看门狗重试；唯一 Firefox 触点是缺 `setTimeout` 时 import Timer.sys.mjs，条件分支剥掉即可）
- `providers.sys.mjs` / `ReasoningEffort.sys.mjs` / `Tools.sys.mjs` 声明表（1178 行 schema + 描述文案是重要 prompt 资产）
- `AgentSession.sys.mjs` 状态机（剥 Timer.sys.mjs / Services.obs 两处触点）
- React 面板三件套（AgentPanel / EnvironmentPane / SettingsPane）：把 `index.jsx` 的 `ChromeUtils.importESModule` 直调改为消息桥即可
- `content/tools/*.cjs` 离线分析器（dispatcher_split / disassemble / wasm_disasm / vpc_resolve 等本来就是 Node 程序）、`templates/*.js`、`skill-reverse.md` 方法论、`dev/selftest-*.mjs`

### 2.2 "引擎住哪"——三个候选（Chromium 特有的架构决策）

Firefox 版的"父进程 system ESM 单例（跨面板重载存活）"在 Chromium 侧没有直接等价物，需要选型：

| 候选 | 机制 | 优点 | 缺点 |
|---|---|---|---|
| **① Native Messaging Host（Node 常驻进程）★推荐** | 扩展 ↔ host 用 stdin/stdout JSON 消息；引擎跑在 Node 里 | 生命周期与浏览器解耦（最接近"常驻引擎"语义）；白送 child_process / fs / better-sqlite3 / 任意 npm 生态；`run_node`/`fs_*`/`env_open`/记忆全在这层天然实现 | 需要装一个本地组件（安装器多一步）；UI ↔ host 每条消息过 SW 中转 |
| ② MV3 Service Worker + offscreen document | 引擎跑在扩展 SW / offscreen 页 | 纯扩展、无本地组件 | SW ~30s idle 被杀，长会话（全自动 24×120 轮）要靠 keepalive/状态外置硬撑；SW 无 child_process，env_open/run_node 还是要 native host |
| ③ fork 内后台 WebUI（chrome://frx-engine） | 浏览器进程 JS + Mojo JS bindings，C++ Mojo 服务提供 fs/subprocess | 与 system ESM 架构 1:1；可进程内拿 `content::DevToolsAgentHost`（无调试横幅） | 只在 fork 上可用；要写 C++ Mojo 服务（fs/subprocess/env 各一个） |

**推荐**：路线 C 下选 ①（扩展 + Node host 承载引擎），fork 阶段再把"引擎级工具后端"通过 host 连接 fork 进程的 CDP 端口接上。UI 侧边栏用 MV3 `chrome.sidePanel`（Chrome 114+，每窗口一 panel，语义与 Firefox 侧栏匹配）。

### 2.3 通信桥重画

```
Firefox：React(父进程 chrome 文档) ─直调→ system ESM 单例 ─JSWindowActor→ 内容进程
Chromium：sidePanel React ─chrome.runtime→ SW 中转 ─native messaging→ Node 引擎宿主
                                     ├─chrome.scripting / chrome.debugger(CDP)→ 页面
                                     └─child_process→ fork 进程（--remote-debugging-port）/ node / python
```

`AgentSession.callTool/listTools`（为外部 MCP 直调设计的入口）在 Chromium 侧天然映射为 host 的消息 handler，frx-director-mcp 的对接面也在这里重建（连 host 的本地套接字即可）。

---

## 3. L3 核心：JSVMP 逐指令 trace → V8/Ignition

这是项目最深的技术资产，也是迁移里唯一"无现成等价、必须重做"的部分。但上游核实表明 **V8 的钩子点比 SpiderMonkey 更收敛**。

### 3.1 Firefox 现实现（参照）

- 钩 `js/src/vm/Interpreter.cpp` 的 `ADVANCE_AND_DISPATCH(N)` 宏体，每条 opcode 一跳（`scripts/apply-phase-b.py` 注入 `JSVMP_TRACE_OPCODE()`）；
- `PinInterpreterTier()` 把 Baseline/Ion 预热阈值设为 UINT32_MAX 钉死 tier-0，防热 dispatcher 逃出 hook；
- 输出 per-pid NDJSON（`{sid,pc,op,n,ln,col,imm}` + `_args/_locals/_vpc/_ret`），控制走 ctl/dump/clear 三文件；
- args/locals/闭包链 dump 依赖 `js::FrameIter` / `AbstractFramePtr::unaliasedActual/unaliasedLocal` / `EnvironmentObject::enclosingEnvironment`。

### 3.2 V8 对应钩子（已核实，2026-08 main 分支）

| Firefox 机制 | V8 对应 | 核实结果 |
|---|---|---|
| `ADVANCE_AND_DISPATCH` 宏体插桩 | **`v8/src/interpreter/interpreter-assembler.cc` 的 `InterpreterAssembler::Dispatch()`**——每个 `IGNITION_HANDLER` 生成的 handler 都以 `Dispatch()` / `Jump` / `JumpBackward` 结束；所有派发路径最终收敛到 `DispatchToBytecodeHandler` → `DispatchToBytecodeHandlerEntry` → `TailCallBytecodeDispatch` | ✅ 存在，且是**单点收敛**（比宏展开到多个 CASE 更干净）；跳转类需同时覆盖 `JumpToOffset/JumpBackward` 或直接挂在最底层的 `DispatchToBytecodeHandlerEntry` |
| `js::Interpret()` 顶部 `EnsureInit()` | 解释器入口（InterpreterEntryTrampoline builtin）或首个 handler 触发的 lazy init；控制文件轮询沿用摊销模式 | 实现细节不同，模式照搬 |
| `js::CodeName` / `PCToLineNumber` | `Bytecodes::ToString(Bytecode)` + `BytecodeArray::SourcePositionTable()`（行列映射）；操作数读取走 `BytecodeArrayIterator`（与解释器同源语义） | 直换 |
| Baseline/Ion 阈值钉死 | **tier 钉死换为编译开关**：`--jitless` 一刀切，或分别禁用 Sparkplug/Maglev/TurboFan（以目标版本 `--js-flags` 清单为准）；注意 V8 官方构建有 flag 冻结机制，fork 内可控 | ⚠️ 见风险 R2/R3 |
| `FrameIter`/`AbstractFramePtr` args/locals dump | Ignition 是**寄存器机**：帧布局走 `InterpretedFrame`/`UnoptimizedJSFrame` 的 register file + `BytecodeArray::register_count()`；函数入口（args dump）钩 InterpreterEntryTrampoline | 编程模型从"操作数栈 + nfixed"换成"寄存器窗口"，dump 语义等价 |
| `EnvironmentObject::enclosingEnvironment()` 闭包链 | `Context::previous()` / `ScopeIterator`（V8 作用域链遍历） | 直换 |
| Windows 内容沙箱挡 trace 落盘（Firefox 用 `_putenv MOZ_DISABLE_CONTENT_SANDBOX` 绕） | Chromium renderer 沙箱同样禁任意文件写。选项：(a) 环境进程 `--no-sandbox`（研究用途可接受，与 Firefox 侧做法同量级）；(b) trace 写入经 Mojo 回 browser 进程落盘（更工程化，多一层管道） | ⚠️ 见风险 R4 |

### 3.3 关键实现差异与注意点

1. **Ignition handler 是 CSA（CodeStubAssembler）生成的机器码**，在 **mksnapshot 阶段**编进 embedded builtins 快照——补丁要在构建期生效（我们本来就在全量构建，无额外负担），但意味着"打补丁 = 重跑 mksnapshot + 链接"，无法热改。
2. **热路径门控照搬**：CSA 里读一个 ExternalReference 全局 flag，单分支跳过 → 关闭时近零开销（对应 Firefox 的 `MOZ_ALWAYS_INLINE` + `gTraceEnabled` 模式）。cold path 经 ExternalReference call 进 C++。
3. **寄存机 vs 栈机**：`_vpc`（虚拟寄存器快照）语义反而更自然——直接 dump 寄存器窗口 + accumulator；`sid` 等价物用 `BytecodeArray` 指针或 script id。
4. **JSVMP 分析工具链兼容性**：`jsvmp_split_dispatcher`（Babel AST 拆派发器）、`crypto_scan` 等分析的是 **JS 源码 AST**，与引擎无关，原样可用；但 `jsvmp_trace` 产出的 NDJSON 字段（opcode 名/pc 语义）从 SM 栈机换成 V8 寄存机，`analyze-jsvmp-trace.py` 与 dispatcher 命中统计需要适配 V8 操作码语义（工作量集中在"虚拟 op 序列反推"的映射表，非重写）。
5. **控制文件机制原样保留**：`/tmp/chromium-reverse-jsvmp.ndjson.<pid>` + ctl/dump/clear，JsvmpBackend 只改路径常量与"定位内容进程 pid"的实现（Firefox 用 `currentWindowGlobal.osPid`；Chromium 侧 host 可从 CDP `Target.getTargets` / 进程列表拿 renderer pid）。

### 3.4 WASM 工具链

- `wasm_disasm`（二进制 → WAT）：离线 JS（wabt），**原样复用**。
- `wasm_probe`（import 边界观测）：V8 侧无现成运行期 import-call trace；可选 (a) CDP 无此能力，(b) patch V8 的 wasm import wrapper / `WasmImportWrapperCache`，(c) 退化为 page_eval 层 JS hook（精度降级）。列为二期项。
- 注意：若用 `--jitless` 钉 tier 且目标含 WASM，jitless 会牵连 WASM 编译路径——JSVMP 目标（纯 JS）不受影响，WASM 目标需保留 Liftoff 或走离线反汇编路线（`--wasm-interpret-all` 一类开关以目标版本为准）。

---

## 4. L3 核心：WebAPI trace → Blink bind_gen

### 4.1 Firefox 现实现（参照）

- 补丁 `dom/bindings/Codegen.py` 的 `definition_prologue`：对所有生成的 WebIDL method/getter 注入 `AutoTraceMethod/AutoTraceGetter` RAII（接口名+成员名取**编译期字符串字面量**；析构时 `args.rval()` 已设，同时拿到入参与返回值）；
- 反扰动序列化：不触发用户 getter、TypedArray 原生逐元素（≤1024）、普通对象只打 class 标签、字符串截 16KB；
- per-pid NDJSON + ctl 文件，与 JSVMP 同构。

### 4.2 Chromium 对应（已核实）

Blink 的 V8 bindings 生成器已从旧的 Jinja 模板目录迁移为 **Python 包 `third_party/blink/renderer/bindings/scripts/bind_gen/`**（"Blink-V8 bindings generator"）。hook 思路 1:1 平移：

- 在 bind_gen 生成 **attribute getter / method callback 体**的代码路径里注入同名 RAII（形如 `frx::WebApiTraceMethod _frx_trace(info, "Interface.member");`）——`v8::FunctionCallbackInfo` 的 args 与 return holder 语义和 `JSJitMethodCallArgs` 一一对应，析构时 return value 已写入 holder；
- 命名仍取**编译期字面量**（bind_gen 从 IDL 拿接口/成员名，与 Codegen.py 同理）；
- 反扰动序列化用 v8 API 重写：`v8::Value` 类型分派 + `String::Utf8Value` + `TypedArray` 直接读 backing store（**比 SM 版更干净**——v8 TypedArray 有原生数据指针，无需逐元素 `JS_GetElement`）；
- static 成员/匿名场合的排除逻辑照抄 apply-phase-b.py 的判定。

一个额外收益：Blink 的 IDL 是 WebIDL，`property-trace`（README 规划中）与 webapi_trace 共用同一注入点。

**注意**：bind_gen 是 Python 生成器、随上游演进较快，补丁建议做成"对生成器函数的字符串替换脚本"（与 `apply-fingerprint-config.py` 同款幂等替换模式），而不是大块 .patch 文件，降低 rebase 冲突面。

---

## 5. L2/L3 之间：signer_trace / closure_read / whitebox_diff → V8 Inspector

### 5.1 现实现

内容进程特权 JS 用 **SpiderMonkey Debugger API**：`new Debugger(win)` 跨 compartment、`onEnterFrame` 读 `frame.arguments`、`frame.environment.names()/getVariable()/parent` 走作用域链、`collectCoverageInfo + getOffsetsCoverage` 采分支覆盖，配 onNewGlobalObject 跨导航接管 + 帧数/墙钟安全阀。

### 5.2 Chromium 映射

SpiderMonkey Debugger 无 V8 直接等价，但 **CDP 的 Debugger/Profiler 域就是 V8 Inspector**，语义全部可达：

| SM Debugger 能力 | CDP/V8 Inspector 对应 |
|---|---|
| `onEnterFrame`（函数入口读实参） | `Debugger.setBreakpointByUrl` 打在目标函数首语句 + `Debugger.paused` 事件的 `callFrames[].functionName/location/scopeChain`；或按 `Debugger.scriptParsed` 全量铺断点（等价现在的 findScripts 预筛 + 全局接管） |
| `frame.environment`（闭包变量真值） | paused 的 `callFrames[i].scopeChain[]`（local/closure/global）+ `Runtime.getProperties` 逐层取值（`e.parent` ↔ scopeChain 数组天然有序） |
| `collectCoverageInfo/getOffsetsCoverage` | `Profiler.startPreciseCoverage` + `takePreciseCoverage`（按函数按偏移的计数，语义对齐 whitebox_diff 的浏览器真值侧） |
| `onNewGlobalObject` | `Target.autoAttachRelated`/`Runtime.executionContextCreated` 跨 iframe/worker 接管 |
| 帧数上限 + TTL 自卸 | 客户端侧等价护栏（host 里实现） |

**执行位置两种**：
- **原版 Chrome 路线**：扩展 `chrome.debugger.attach` → 顶栏出现"正在调试此浏览器"横幅（用户可见、目标站可推断），且 SW 无文件权限（需 host 中转）；
- **fork 路线（推荐）**：进程内 `content::DevToolsAgentHost::GetOrCreateFor(frame)` + 自建 client 直接收发协议消息——**无横幅、无端口暴露**，与 Firefox 版"用在树 DevTools 模块"的定位完全一致（这也是文档里 Phase-1 机制的原意）。

`js_trace`（AST 插桩 + Node 执行）、`whitebox_diff`（Node 侧 diff）、`crypto_scan` 均为纯离线 JS，原样复用。

---

## 6. L2：页面自动化 / 网络 / Cookie / 文件与子进程

| 工具族 | Firefox 机制 | Chromium 机制 | 备注 |
|---|---|---|---|
| page_eval | JSWindowActor + `Cu.Sandbox(wantXrays:false)` | `chrome.scripting.executeScript({world:"MAIN"})` 或 CDP `Runtime.evaluate` | MAIN world ≈ wantXrays:false；click/type/scroll 现有"生成 DOM 表达式走 eval"的代码**原样搬**，只换执行通道 |
| page_navigate | `browser.fixupAndLoadURIString` | `chrome.tabs.update({url})` / CDP `Page.navigate` | 直换 |
| page_screenshot | `wgp.drawSnapshot` | `chrome.tabs.captureVisibleTab`（可视区）/ CDP `Page.captureScreenshot{captureBeyondViewport}`（整页） | 整页能力反而更强 |
| hook_inject（document-start） | `content-document-global-created` observer + Sandbox | `chrome.scripting.registerContentScripts({runAt:"document_start", world:"MAIN"})` | **几乎一一对应**，MV3 注册跨进程更稳；现有 fetch/XHR/sendBeacon preset 代码原样搬 |
| net_capture/list/get | 父进程 nsIObserver + 内容侧拼 initiator 栈 | CDP `Network` 域：`requestWillBeSent` **原生带 `initiator.stack` JS 调用栈**；`getResponseBody` 可取响应体（Firefox 版尚未实现 body 捕获，这里白赚） | Firefox 版最费工的"双进程拼栈"在 CDP 是免费的；fork 内可进一步下沉 URLLoaderThrottle（对应 patches/network-analysis 的 Phase-2 下沉） |
| cookies | `Services.cookies`（nsICookieManager，含 httpOnly） | `chrome.cookies`（原生含 httpOnly）或 CDP `Storage.getCookies/Network.setCookie` | 映射干净 |
| fs_* / run_node / run_python / npm_install | Subprocess.sys.mjs + IOUtils | **Native host（Node）**：child_process + fs | PATH 探测那套（zsh -lic、nvm/pyenv、注册表）逻辑可移植；Node 里反而简单 |
| 记忆/笔记 | Sqlite.sys.mjs / IOUtils NDJSON | better-sqlite3 / Node fs | 直换 |

---

## 7. L4：指纹配置层与环境管理器

### 7.1 指纹：Chromium 侧"三档"实现策略

**第一档：CDP override（零补丁，覆盖 ~60% 字段）**

CDP 对应域在引擎层生效（非 monkey patch，页面 JS 反射不出去，与 Frx 的对抗等级同类）：

- `Network.setUserAgentOverride{userAgent, acceptLanguage, platform, userAgentMetadata}` —— 一次性覆盖 **navigator.userAgent/platform/language(s)、HTTP UA 头、Accept-Language、全套 Sec-CH-UA* 请求头、navigator.userAgentData（brands/高熵值）**。Firefox 版为此写了 900 行 NavigatorUAData + 8 个头注入；Chromium 上这是一个 CDP 调用。
- `Emulation.setTimezoneOverride` / `setLocaleOverride` —— Intl 时区/locale（跨平台，比 TZ 环境变量可靠，Windows 也生效）。
- `Emulation.setDeviceMetricsOverride` —— screen 指标/DPR。

**第二档：启动开关（零补丁）**

`--user-agent`、`--accept-lang`、`--lang`、`--force-device-scale-factor`、`--use-angle=swiftshader`（WebGL renderer 换成固定 SwiftShader 字符串）、`--webrtc-ip-handling-policy`/`--force-webrtc-ip-handling-policy`（WebRTC IP 泄漏面）、`--window-size`、`--user-data-dir`。

**第三档：Blink/content patch（必须改码的字段）**

| 字段 | hook 点 | 先例 |
|---|---|---|
| navigator.hardwareConcurrency | `third_party/blink/renderer/core/frame/navigator_concurrent_hardware.cc` | Brave 在其 fork 里限流，可参考 |
| navigator.deviceMemory | `core/frame/`（DeviceMemory） | 同上 |
| WebGL 任意 UNMASKED_VENDOR/RENDERER | `modules/webgl/webgl_rendering_context_base.cc` getParameter / `gpu/config/gpu_info_collector` | 各指纹浏览器通用改法 |
| canvas toDataURL/getImageData 确定性噪声 | `html/canvas/canvas_rendering_context_2d.cc` 等 | **Brave farbling 补丁可直接参考**（canvas/audio/键盘/字体噪声全套在 Brave fork 里） |
| 字体枚举 | Blink font list | Brave 同款 |
| webdriver=false（开自动化时） | Blink NavigatorAutomationInformation | fork 内可强制 |
| Sec-CH-UA* 离线伪造（不走 CDP 时） | `content/common/user_agent.cc` + client hints | 第一档已覆盖，此为 fork 常驻版 |

**结构一致性（重要）**：Firefox 专有字段（oscpu/buildID/productSub/appName 等）在 Chromium schema 中**整组删除**——这是项目自己立的"身份与内核能力一致"原则（v0.22.4 收敛 Firefox-only 指纹）的反向应用。派生一致性规则（languages[0] → navigatorLanguage → intlLocale → httpAcceptLanguage q 值）**原样保留**，FrxFingerprintConfig 的字段分组/优先级/缓存设计也可平移（启动读一次 + mutex 缓存 → Blink 侧等价：renderer 初始化时读环境变量 JSON，getter 前置覆盖）。

**迁移净效果**：Chromium 上 navigator.userAgentData 原生存在，指纹层实现面**显著缩小**；且 Chromium 是指纹浏览器市场主流（目标站对 Chromium 指纹兼容性最好），"环境与真实用户分布一致"比 Gecko 伪装 Chrome 更自然。

### 7.2 环境管理器

`EnvironmentBackend`（3149 行）的移植地图：

| 机制 | Firefox | Chromium |
|---|---|---|
| 隔离单元 | `-no-remote -profile <dir>` | `--user-data-dir=<dir> --no-first-run --no-default-browser-check` |
| 自动化端口 | `-marionette` + `marionette.port` pref（2828 起扫描 + 真实 bind 探测） | `--remote-debugging-port=<port>`（**端口扫描/绑定探测逻辑原样移植**）；就绪探针从"连端口等首字节"换成 `GET http://127.0.0.1:<port>/json/version`（更规范） |
| 指纹/trace 注入 | 子进程环境变量 `MOZ_FRX_*`（C++ 层消费） | 同款模式：`FRX_FINGERPRINT_JSON` / `FRX_JSVMP_TRACE` 等，fork 的 Blink/V8 补丁在启动期读取；trace 期进程加 `--no-sandbox --js-flags=...` |
| prefs 注入 | profile/user.js（AUTOMATION_STEALTH_PREFS + webgl/audio/webrtc/TLS 映射） | `<user-data-dir>/Default/Preferences`（JSON）+ 启动开关组合 |
| 子进程启动 | Subprocess.call + stdout drain | host 的 child_process（逻辑照搬） |
| macOS 进程名伪装 | `/bin/zsh -lc 'exec -a "$label" ...'` | **同款原样复用** |
| 状态机/manifest/目录布局 | `~/.firefox-reverse/environments/<id>/...` | 目录结构原样；数据模型兼容旧环境导入（fingerprint.json 做 Firefox→Chromium 字段裁剪转换） |

`env_open` 启动 fork 的 chromium-reverse 二进制（定位方式从 `Services.dirsvc.get("XREExeF")` 换为 host 已知的安装路径）；MCP 侧 `FRX_ENV_ID` 绑定机制平移。

---

## 8. 构建体系与补丁维护

| 维度 | Firefox 现状 | Chromium |
|---|---|---|
| 取源 | `git clone --depth 1 mozilla-firefox/firefox`（~5GB） | `fetch --no-history chromium` + `gclient sync`（~30-40GB、首次数小时） |
| 构建 | `./mach build/package` | `gn gen out/...` + `autoninja chrome`；正式参数 `is_official_build=true symbol_level=0` |
| 单次构建时长 | ~12-26 分钟（CI） | 16 核约 1.5-4 小时（首次），增量快 |
| 补丁模式 | patches/（git apply）+ additions/（rsync）+ 幂等替换脚本 | **同款模式完全适用**；业界先例：Brave（chromium_src 覆盖层）、ungoogled-chromium（纯补丁集）、Vanadium。建议沿用"additions 放新增 C++/JS、apply-*.py 做幂等字符串替换"的既有约定 |
| 交叉编译 | Linux 构建机出 5 平台 | `target_os`/`target_cpu` GN 参数；Linux→Windows 成熟；Linux→macOS 需 macOS SDK（Brave CI 有先例）；或各平台原生构建机 |
| 上游节奏 | 锁 153.0a1 nightly，不追上游 | **建议锁 stable 分支**（约 4 周一个 major，锁 branch-heads）；季度 rebase。经验预判：V8 interpreter-assembler / Blink bind_gen 属慢变区，补丁冲突面小；**侧边栏/浏览器 UI 属快变区**（与 Firefox 版 agent-ui 补丁同样脆弱）——这是选择扩展壳承载 UI 的第二个理由 |
| 品牌 | browser/branding 替换 + installer 补丁 | Chromium 无 Chrome 商标资源（默认 Chromium 品牌），替换 `chrome/app/theme/chromium/` + `chrome/app/settings_strings` 一类即可，比 Firefox 侧简单 |
| 法务 | MPL-2.0 衍生 | Chromium 为 BSD-3 等 permissive 许可，衍生发行合规负担更小（不含 Google API key 即可，构建时去掉 google_api_key） |

---

## 9. 路线对比与推荐

### 路线 A：全对等 fork
V8 trace + Blink trace + 指纹 C++ 层 + 侧边栏 WebUI 全部打进 Chromium。能力 100% 对等，架构 1:1（后台 WebUI 引擎 + Mojo 服务）。代价：Mojo C++ 服务 + WebUI + side panel UI 补丁三块新工程；UI 补丁随上游 churn。

### 路线 B：扩展壳（原版 Chrome 可用）
MV3 sidePanel + SW + native host。引擎/UI/大部分工具可用（page/net/cookies/fs/run_node/env 全通，signer/closure/coverage 走 chrome.debugger 带横幅，jsvmp/webapi trace 与 C++ 指纹层缺失）。**一两天内可出 demo，一周可内测**，且跑在任何用户的 Chrome 上——分发优势巨大。

### 路线 C：混合（推荐）★
1. **C1（快赢）**：路线 B 落地——引擎进 native host，工具走 chrome.* + chrome.debugger；指纹用 CDP override + 启动开关（对研究用途已够）；env_* 管理 fork 之前的 stock Chromium 进程。
2. **C2（fork 核心）**：起 chromium-reverse 仓库（补丁集模式），按序落地：V8 Dispatch 钩子（jsvmp trace）→ bind_gen 注入（webapi trace）→ 指纹 C++ 层（第三档字段）→ 进程内 DevToolsAgentHost 桥（无横幅 signer/closure/coverage）。
3. **C3（合流）**：host 的引擎级工具后端检测目标进程是否 fork（CDP `Browser.getVersion`/自定义域握手）：是 → 解锁 jsvmp_*/webapi_* 与全量指纹；否 → 降级为扩展级工具集（`_need` 机制天然支持这个渐进暴露）。
4. **C4（可选）**：fork 侧把 FRX 能力注册为**自定义 CDP 域**（如 `FRX.setFingerprint`/`FRX.jsvmpTrace`），替代 ctl 文件跨进程通道——host 无需文件权限即可驱动（需验证 chrome.debugger 对自定义域的放行情况；进程内 agent host 路线无此问题）。

这与 Firefox 版的演进史同构：先 DevTools/BiDi 快速跑通（N0-N2），引擎层下沉后置（N4）。**风险被分摊，每阶段都有可用产物。**

---

## 10. 风险清单

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | V8 CSA 层插桩比 SM C++ 宏复杂（builtins 快照机制） | jsvmp trace 是最高成本项 | 单点收敛的 Dispatch() 已核实；先做"每 N 条采样一跳"降级版验证管道，再做全量 |
| R2 | tier 钉死与 V8 flag 冻结 | trace 期间热函数逃逸 | 启动期 `--js-flags` 锁定；fork 关闭 flag 冻结；JSVMP 目标本就长跑解释器友好 |
| R3 | `--jitless` 牵连 WASM | WASM 目标无法同时 trace | JSVMP/WASM 目标分进程处理；WASM 走离线反汇编主路线 |
| R4 | renderer 沙箱禁文件写 | trace 落盘失败 | 环境进程 `--no-sandbox`（对齐 Firefox 侧禁内容沙箱的既有决策）；产品化再换 Mojo drain |
| R5 | MV3 SW 生命周期杀死长会话 | 全自动 24×120 轮中断 | 引擎宿主放 native host（Node）从根上规避 |
| R6 | chrome.debugger 横幅 + 单调试器独占 | 原版 Chrome 上观测可见 | fork 内进程内 DevToolsAgentHost 无横幅；原版上仅作可见降级模式 |
| R7 | bind_gen 生成器上游 churn | webapi trace 补丁冲突 | 幂等替换脚本 + 季度 rebase；生成器接口相对稳定 |
| R8 | Chromium 构建基建门槛（磁盘/时长/SDK） | CI 成本上升 | 与 Firefox 同期 5 平台发布经验平移；先出单平台（mac arm64）验证 |
| R9 | 双内核维护成本（Firefox 版继续演进） | 人力分摊 | 路线 C 分阶段；L1 引擎层两端共享同一份代码（建议抽 monorepo 共享包） |
| R10 | SM→V8 操作码语义差异影响虚拟 op 反推 | 分析工具链需适配 | 集中在 NDJSON 后处理映射表；dispatcher 检测（热点排序）逻辑不变 |

---

## 11. 工作量估算（粗粒度，单人当量）

| 阶段 | 内容 | 估算 |
|---|---|---|
| C1 | 引擎进 native host + MV3 面板 + page/net/cookies/fs/run_* 工具 + CDP 指纹 override + env_* 管理原版 Chromium | 2-4 周 |
| C2a | chromium-reverse 基建（fetch/gn/打包/品牌/CI 单平台）+ 补丁框架 | 1-2 周 |
| C2b | V8 Dispatch 钩子 + JIT 钉死 + NDJSON/ctl 管道 + JsvmpBackend 接线 | 3-5 周（含 mksnapshot 调试与采样降级版） |
| C2c | bind_gen 注入 WebAPI trace | 1-2 周 |
| C2d | 指纹第三档字段（WebGL 串/hardwareConcurrency/canvas/audio/字体） | 2-3 周 |
| C2e | 进程内 DevToolsAgentHost 桥（signer/closure/coverage 无横幅版） | 2 周 |
| C3 | fork 探测 + 工具分级解锁 + 分析工具链 V8 适配 | 2 周 |
| C4 | 多平台发布（win/linux/arm64 交叉或原生） | 1-2 周 |

合计约 **3-4 个月**单人当量到路线 C 完整形态；C1 两端（Firefox/Chromium）共享 L1 代码库。

---

## 12. 结论

1. **迁移可行性高**：项目的分层设计（纯 JS 引擎、引擎无关的文件通道、DI 注入的后端）本身就是为这种移植留的口子；66 工具里 ~45 个在原版 Chrome + native host 上就能跑。
2. **核心差异化能力在 V8/Blink 都有干净的落点**：V8 `InterpreterAssembler::Dispatch()` 单点收敛（优于 SM 宏展开）；Blink bind_gen 与 Firefox Codegen.py 同构（编译期字面量 + RAII 拿 rval 的设计原样平移）。
3. **指纹层在 Chromium 上反而变小**：UA-CH/Sec-CH 头一个 CDP override 全覆盖，Firefox 版最重的 NavigatorUAData 实现整块消失；剩余必改字段有 Brave farbling 等成熟先例。
4. **最大的新工程不是引擎观测，而是"引擎宿主"选型**——native messaging host 承载引擎是架构上最接近"父进程常驻引擎"的方案，同时天然解决 fs/subprocess/SQLite。
5. **推荐路线 C（混合）**：扩展壳先行（原版 Chrome 可用、快赢），fork 承接引擎级能力（jsvmp/webapi trace、C++ 指纹、无横幅观测），两端共享同一份 L1 引擎代码——这正是 Firefox 版从 DevTools 混合策略走到 C++ 下沉的既定演进路径在 Chromium 上的重演。
