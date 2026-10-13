# Agent 工作规范 — firefox-reverse / frx 逆向工作流

## 工具分工铁律

### jsvmp 运行时代码/数据的追踪获取，一律交给 worker agent（director 不亲自磨）

**规则**：凡是要从运行中的页面捕获 JSVMP 运行时真值的工作——jsvmp_trace 的 start/dump/clear 循环、ctl 复活序列、dump 记录的搜索与验证、按特征（如 arr[196]/arr[410]）筛表、换 col/pc 重试——**必须委派给 worker agent**（frx-director 的 `agent_start`），director 只负责：定目标、写清任务书（含已知机制与防兔子洞纪律）、`agent_poll`/`agent_wait_for_stop` 观察、`agent_read` 评判结论并决策下一步。

**为什么（实证）**：
- dump 触发窗口只有 ~10 秒（C++ 侧 1M 记录硬限），director 每轮对话往返 3-4 个工具调用必然错过窗口；worker 在浏览器侧连续执行无此延迟——实测 director 手动 N 轮全空、worker **Round 1 命中**（2026-08-29 会话，tid tmtd5mat08tcez）。
- worker 还能顺手修正方法论（如发现"dump 记录在新窗口开头而非尾部"），这类迭代在 director 层成本极高。
- 成本拆分：机械循环交给便宜 worker，token 与注意力留给方向决策。

**任务书模板要点**（agent_start 的 task 必须自包含）：
1. 背景机制：ctl/dump 文件路径与格式、复活序列（ctl=0 → fetch 消化 → ctl=1 → 立即 fetch → 搜**最后 200MB 全量**而非尾部）
2. 已知目标与候选（col/pc 清单，标注"已试勿重试"）
3. 成功标准（可机检，如 "_locals 记录存在且 env 含 ≥150 项字符串数组"）
4. 产出落盘路径（work/*.json）
5. 防兔子洞纪律（轮数上限、单组合只试一次、大文件只读尾部）

**例外**：只读型操作（jsvmp_query 单次查询、net_list/net_get、page_info、closure_read 单次武装）director 可直接做；连续的"触发→读→改配置→再触发"循环型捕捉必须委派。

## 追踪器机制速查（JsvmpTraceCore.cpp 源码级，2026-08-29 确认）
- op 流: /tmp/firefox-reverse-jsvmp-b.ndjson.<内容进程pid>；dump 配置: /tmp/firefox-reverse-jsvmp.dump.<pid>（行格式 `locals col=N pc=N env=1 depth=8 maxarr=2000 limit=N`）；ctl: `0\n`=停+清限锁，`1\n<filter>\n`=开+计数清零+重钉解释器
- 三大死因: ①1M 硬限后必须 ctl=0→1 循环复活 ②无 ctl 进程里已 JIT 的函数永不回解释器 ③页面重载常换内容进程 pid（可向全部 pid 广播 ctl/dump）
- ctl 有 300 秒保鲜期（mtime 过旧自动失效），长循环需周期性 touch

## ★clear 复活链与 dump 通配（2026-09-07 源码级实测，比 ctl 0→1 更强）
- **writer 卡死死锁**：1M 限停后需要 op 进解释器才 poll ctl——页面静止时 ctl 永不被读。**clear 文件是设计好的复活通道**：`/tmp/firefox-reverse-jsvmp.clear.<pid>` 存在即被消费（删文件）→ truncate ndjson + 计数清零 + 限锁解除 + 恢复采集（保持解释器钉死）。
- **完整复活序列（实测一击必中）**：对目标 pid ①ctl 写 `0\n` → ②写 clear 文件（空文件即可）→ ③ctl 写 `1\n<filter>\n` + 写 dump spec → ④立即导航/触发（产生 op → poll 消费 clear → 全套生效）。97964 实测：75MB 冻结 → 复活爆涨 479MB + `_locals` 全量输出。
- **dump spec 关键语义（源码 DumpKV）**：`col=` **省略即=0=通配所有列**（gDumpLocalsCol=0 = any column）——不必猜 col！`pc=0`=函数入口；`skip=N`=跳过前 N 次触发（拿后段）；**spec 内容不变则不重新应用**（计数不清零）——每轮微调（如 skip+1）强制重置。
- **记录 key 带下划线**：`_locals`/`_args`/`_ret`/`_vpc`（搜 "locals" 全漏）。_locals 入口帧的 locals 多为 undefined（执行中段才有值）；_ret 抓小返回值；_vpc 形态 `{l,a}` 可能空。
- **坐标系**：op 流记录带 `ln`/`col`（eval 行内坐标，每导航轮源码轮换 col 变）；dump spec 的 col 匹配**不区分 ln**。P 类滚动函数的 col 每轮漂移——通配+skip 比 col 定向稳。
- 本轮锚定法（拿当前轮 col）：page_eval 包 `Document.prototype.cookie` setter 抓写入栈（`fn@...line 6 > eval:N:col`）→ 立即用于 spec（同轮有效，跨轮失效）。
