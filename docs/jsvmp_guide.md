# JSVMP Trace 工具链路与使用指南

> 本文聚焦 **jsvmp_trace / jsvmp_query / jsvmp_status / closure_read** 这一组工具的
> **调用链路、运行期开关原理、关键成功要素、以及"抓不到"时的排错根因**。
> 关于完整逆向工作流（dump → 离线 AST 分析 → 算法还原），见 `jsvmp-reverse-workflow.md`，两文互补。

---

## 一、工具定位与底层原理

JSVMP 普遍内置自校验（`func.toString()` hash、`length`、`performance.now()` 时序、
`Error.stack` 检测）。任何在 **JS 层** 改代码或挂 Debugger 的方案都会触发反爬：

- `Debugger.onStep` → 丢弃 JIT、性能降 10–100×、时序必塌方
- `TraceLogger` / `GeckoProfiler` → 颗粒度太粗（function 级 / 1ms 采样）
- `SetExecuteHook` → SpiderMonkey 35 已删；Frida/QBDI → 不懂 JS 语义

**核心选择**：直接 patch SpiderMonkey 解释器内核
（`js/src/vm/Interpreter.cpp` 的 `ADVANCE_AND_DISPATCH` 宏），在 C++ 层逐 opcode 记录。
对页面 JS 完全透明，不会被自校验发现。

### 热路径零开销

hot path 内只有一个 `if (!gTraceEnabled) return;` 分支，flag off 时近零开销、JIT 友好。
开启后逐 opcode 记录 `sid + pc + op + opname + line + col`（带内联整数立即数 `imm`）。

### 钉死解释器层（PinInterpreterTier）—— 抓热 dispatcher 的关键

JS 函数跑约 10 次就被 Baseline/Ion JIT 接管，**逃出 `js::Interpret`**（hook 所在层），
导致抓不到热函数。start 时把 tier-up 阈值设为 `UINT32_MAX` → 永远停在 tier-0 → hook 必中。
stop 时还原阈值（并落盘 pref，防浏览器强杀丢状态）。

### 运行期开关（无需重启）

`MaybePollControlFile()` 在每次进解释器时摊销轮询（每 256 次真正读一次文件）。
控制文件 `/tmp/firefox-reverse-jsvmp.ctl.<pid>`：

- 首字节 `1` / `0` → 开 / 关
- 第二行写 script filename 子串 → 运行期变更 filter（`gFilterVersion` 让各线程缓存失效）
- `1` 但 mtime > 300s → 视为关闭（auto-expire，防忘了 stop）
- `.clear.<pid` 文件 → 消费即删，截断文件 + 重置计数但保持开启（丢页面加载噪声）
- `.dump.<pid` 文件 → 运行期开 locals / env / vpc / ret / args

### 落盘与 pid 分文件

trace 落 `<traceDir>/firefox-reverse-jsvmp-b.ndjson.<pid>`，**按内容进程 pid 分文件**。
父进程 chrome JS 只产生 `resource://` 噪声，必须读**网页内容进程**那份。

### 六种 dump 能力

| 模式 | 触发 | 用途 |
|------|------|------|
| `args` | `pc=0` 函数入口 | dump 实参 |
| `locals`(+`env=1`) | 指定 `(col,pc)` | 深序列化局部变量 + **walk 闭包环境链**（拿 xor key 等运行时常量对象） |
| `vpc` | 派发循环头 `(col,pc)` | 每命中一次轻量快照「帧数值寄存器」，离线找虚拟 pc 槽 |
| `ret` | `RetRval` / `Return` | dump 返回值（递归 VM 最外层返回值常是最终签名串，`skip` 跳到最后一次） |

---

## 二、工具调用链路（agent-sidebar API）

四个工具经 `JsvmpBackend.sys.mjs` 实现，依赖 `page` backend 在场。

### jsvmp_status（排错第一步，永远先看它）

返回关键诊断字段：
- `contentPid` —— 当前标签页内容进程 pid
- `readingContentProcess` —— 是否读到网页内容进程（应为 `true`）
- `traceFilePath` / `controlFilePath` —— 实际落盘路径
- `diag.jsvmpFilesInDir` —— `/tmp` 下到底生成了哪些 jsvmp 文件、pid 对不对
- `diag.enabled` / `diag.filter` —— C++ 侧当前开关与 filter 状态

> 若 `diag.jsvmpFilesInDir` 里**没有目标 pid 的文件** = C++ 没写 = start 没生效或脚本名不匹配。

### jsvmp_trace(action:'start', scriptUrl:'<子串>')

- 内部从 URL 取**文件名子串**作 filter（仅匹配 filename，不认完整 URL/路径）
- **不带 scriptUrl 会被直接拒绝**（返回错误，不会全量 trace）
- 返回里 `filter` 字段确认它解析成了期望的文件名

### page_navigate(目标页) —— 重载！

start 关 JIT 后，**已加载并 JIT 过的旧代码仍驻留 tier-1/2，不进 `js::Interpret`**，
所以一条 op 都抓不到。**必须重载让脚本在解释器下"冷"重跑**。

### 触发目标操作

交互 / fetch / 翻页——让目标混淆函数在解释器下执行。

### jsvmp_query(limit:200)

- 默认读**文件尾部**最新记录（旧实现读头会反复返回页面加载早期噪声）
- `count=0` → 链路某步失败；`count>0` 但全是 `resource://` → pid 配错

### jsvmp_trace(action:'stop')

用完手动 stop 恢复 JIT。约 300s 未操作会 auto-expire 自动关闭。

### closure_read（补运行时常量）

专治"**dispatcher / 解码后字节码 / S-box / 常量池是闭包变量，`window.` 取不到、
page_eval 够不到**"——JSVMP 逆向反复撞的根因。
- `closure_read(action:'start', scriptUrl:'...', fn:'dispatcher子串')` → 触发 → `query`
- 先不传 `varNames` 拿变量目录，再带正则深序列化

---

## 三、标准 6 步工作流

```
① jsvmp_trace(start, scriptUrl="目标bundle.js文件名子串")
     → 强制解释器模式 + 写 ctl.<pid>。务必带 scriptUrl！无 filter=被拒绝
② page_navigate(目标页URL)
     → 让已加载脚本在解释器下"冷"重跑（已 JIT 旧代码需 reload 才落回 tier-0）
③ 触发目标操作（交互 / fetch / 翻页）
④ 可选：jsvmp_trace(dump, actions=["locals"], env:true, col=<dispatcher列>)
     → 拿闭包常量对象（xor key / 常量池 / 解码后字节码数组）
⑤ jsvmp_query(limit=200)  ← 默认读【尾部】最新记录
⑥ jsvmp_trace(stop)  ← ~5min 自动关，用完手动 stop 恢复 JIT
```

> 与静态三件套互补：`jsvmp_split_dispatcher`（拆派发器）→
> `jsvmp_disassemble`（反汇编）→ `crypto_scan`（一眼判型）。
> 详见 `jsvmp-reverse-workflow.md`。

---

## 四、"抓不到 jsvmp" 排错根因（按命中率排序）

### 1. 没重载目标页 —— 最常见
start 关 JIT 后，已 JIT 旧代码不进 hook 层，一条 op 都无。→ **start 后必须 page_navigate 重载**。

### 2. scriptUrl 传错形态
- 传完整 URL → 被截成文件名，OK；但动态 chunk（如 `abc~def~vendors.1234.js`）
  子串必须能匹配文件名里的稳定片段。
- 不带 scriptUrl → 直接被拒绝（返回错误，不是全量 trace）。
- 先用 page_eval / 网络面板确认目标混淆 bundle 真实文件名，再传其**文件名子串**。

### 3. 读错 pid / 多标签页串扰
query 默认按当前标签页内容进程 pid 选文件。若 start 后切了标签页，或目标跑在
worker / iframe，trace 落到另一 pid 文件，你会读到父进程噪声或空。
→ 用 `jsvmp_status` 核对 `contentPid` 与 `diag.jsvmpFilesInDir` 的 pid 是否一致；
重载并聚焦目标页。

### 4. query 读文件头（旧习惯）
旧实现读头会一直返回页面加载早期记录。现在读尾部。**自己写脚本读文件时记得读尾部**，非首行。

### 5. 忘了 stop / 5 分钟自动过期
start 后约 300s auto-expire。中间停顿太久再触发，trace 已关，无记录。→ 重新 start + 重载。

### 6. content sandbox 拦写 /tmp（来自实战踩坑）
mac/Linux firefox content process sandbox 默认禁止写 `/tmp`、`$HOME`，
症状：trace 文件只有父进程 meta 行，content process 全 0 字节。
→ 启动加 `MOZ_DISABLE_CONTENT_SANDBOX=1`（debug 用）；或生产版 patch sandbox profile。

### 7. 多 firefox 实例同 profile 互锁
`-no-remote -P xxx` 启动弹 "Choose User Profile" → 旧进程没杀干净。
→ 启动前 `pkill -9 -x firefox`（注意 mac 进程名是 `firefox` 不是 `firefox-bin`）；
**不要用 `pkill -f`**，会匹配到 ssh 命令行 self-kill（exit 255）。

### 8. 构建未带上 jsvmp patch
最直白验证：start 后重载一个**普通非混淆页面**并触发任意 JS，
`jsvmp_query` 是否看到页面脚本 opcode。连普通页面都空 = C++ patch 没编进去或 ctl 路径不对
（Windows 上是 `TEMP` 目录，需与 C++ 侧 `getenv("TEMP")` 同源）。

---

## 五、关键成功要素速查表

| 痛点 | 正确做法 |
|------|----------|
| 抓不到热 dispatcher | start 后**务必 reload**，让代码冷加载回 tier-0 |
| 读不到数据 | query 用**默认尾窗**（最新触发），不是文件头 |
| 运行时常量取不到 | `dump` + `env:true` + `col`=dispatcher 列（walk 闭包链）；或 `closure_read` |
| 页面被卡死 | 必须带 `scriptUrl`（目标脚本**文件名子串**，非完整 URL）；无 filter 被拒绝 |
| 多标签页串扰 | ctl/dump/clear 全按**内容进程 pid** 分文件，先聚焦目标页 |
| 大数组截断 | dump 时调 `maxarr`（字符串池等），`depth` 控序列化深度 |
| 忘了 stop | 300s 自动过期 + 用完手动 stop 恢复 JIT（否则全局拖慢） |
| content 写不出文件 | 启动加 `MOZ_DISABLE_CONTENT_SANDBOX=1` |
| 多实例锁 profile | `pkill -9 -x firefox` 清旧进程，勿用 `pkill -f` |

---

## 六、排错 SOP（照着走）

```
① jsvmp_status
   → 看 contentPid / readingContentProcess / diag.jsvmpFilesInDir
   → 若 jsvmpFilesInDir 无目标 pid 文件 = C++ 没写 = start 未生效或脚本名不匹配

② jsvmp_trace(action:'start', scriptUrl:'<混淆bundle文件名子串>')
   → 注意返回里 filter 字段，确认解析成期望文件名

③ page_navigate(目标页)   ← 重载！不重载=旧 JIT 代码逃出 hook

④ 触发目标操作（点按钮/发请求/翻页）

⑤ jsvmp_query(limit:200)
   → count=0？回看 ②③④；count>0 但都是 resource:// = pid 配错（重载/聚焦正确标签页）

⑥ jsvmp_trace(action:'stop')  ← 用完恢复 JIT
```

---

## 七、抓到 dispatcher 后怎么拿常量

光有 opcode 流不够，运行时常量（xor key、S-box、解码后字节码数组）在闭包里：
- `jsvmp_trace(action:'dump', actions:['locals'], env:true, col:<dispatcher列>)` + 触发 → `query` 读 `_locals` 行
- 或 `closure_read(action:'start', scriptUrl:'...', fn:'dispatcher子串')` → 触发 → `query`
  （先不传 varNames 拿变量目录，再带正则深序列化）

配合 `webapi_trace`（C++ 引擎层，JS 不可检测）看签名器读了哪些
`querySelector` / `getAttribute` / `navigator`、写了哪些 `XHR.setRequestHeader`，
定位"输入从哪来"；其 `env` 模式落的 `fingerprint-env.ndjson` 可被 `whitebox_diff` 捞作真值。
