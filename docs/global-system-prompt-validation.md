# 全局 system 提示词原生验证

## 最终安装包验收入口

v0.26.1 新增跨平台 `--app` 模式，直接测试从安装包解压的应用，不覆盖 `omni.ja` 或其他资源。先通过 `prepare-native-candidate.py` 核对下载包 SHA256、架构、品牌、BuildID、SourceStamp 及模块字节，并将固定 `source_commit` 经 lockfile 重新构建的 sidebar bundle 与包内资源核对，再运行：

```sh
python3 scripts/verify-global-system-prompt.py \
  --app "$FRX_TEST_BINARY" \
  --source-commit "$SOURCE_COMMIT" \
  --build-id "$EXPECTED_BUILD_ID" \
  --bundle-sha256 "$FRX_TEST_BUNDLE_SHA" \
  --output native-report/global-system-prompt
```

`--app` 接受 macOS `.app` 目录或 Windows/Linux 可执行文件。运行前后核对 AgentSession、ConfigStore、CSS、bundle 和 XUL；校验不符直接失败，不把仓库文件覆盖进去“修复”候选包。独立临时 profile 只使用合成配置和本机模型服务；验收包含原文保存、完整重启、模型/会话切换、历史会话、运行中修改的回合边界、真实自动续跑与清空恢复。现有指纹、页面/Agent、Windows 启动与 Ledger 回归继续保留。

## v0.26.1 最终包结果

2026-10-12 完成五目标六包构建与原生验收。运行时代码提交为 `906578f07e22f94151edf793279203302ba484a7`，BuildID 为 `20261012042610`；[原生 CI 38208742667](https://github.com/WhiteNightShadow/firefox-reverse/actions/runs/38208742667) 全部通过，各平台原始报告可在该 run 的 artifacts 中查看。Mac ARM64 在本地 macOS 26.7.1 验证；Intel Mac 使用 `macos-15-intel` 原生 runner，本地 Rosetta 结果仅作补充。

| 平台 | 指纹检查 | 页面/Agent 检查 | 提示词场景 / 请求 |
| --- | ---: | ---: | ---: |
| macOS ARM64 | 213 | 33 | 8 / 11 |
| macOS Intel | 197 | 33 | 8 / 11 |
| Windows x86_64 | 212 | 33 | 8 / 11 |
| Linux x86_64 | 182 | 33 | 8 / 11 |
| Linux ARM64 | 182 | 33 | 8 / 11 |

每个平台均确认最终包资源未在测试前后被替换，两次正常浏览器退出码均为 0；保存、重启、切换模型/会话、运行中修改、自动续跑及清空恢复全部通过。源码与所有包内的关键资源 SHA256 一致：

| 资源 | SHA256 |
| --- | --- |
| AgentSession | `d189af050255e5c42b61b7150122c52ff718fed19faf1d1c8637a22c9da9b7a7` |
| ConfigStore | `308417d1ab506881f7792273232caec90c90aad0a5a7dff57b0ecfea4f781c9f` |
| CSS | `e4c9955f8c1295d148182ff98ca5168a02e8fc2e2ada3756a92881ab895318e1` |
| Sidebar bundle | `5a623a827fbef18f7238666738ebbcdf684015d8f2d583ffea29341ebe711e62` |

Mac 另通过 5 类隔离 profile 启动边界、7 项 v0.26.0 到 v0.26.1 升级检查，覆盖旧模型配置、会话、书签、cookie/localStorage 保留、默认无附加提示词及另一 profile 的存储隔离。Windows 另通过 6 次截图/正常退出及 Ledger 回归；安装程序中的核心载荷与已实测 ZIP 一致，但未把静态载荷比较当作安装向导实测。

边界：使用本地合成模型服务，不是付费供应商线上联调；Intel/Linux 无头 runner 的 GPU/MSAA 未验证，Linux 缺失 Arial/Times 字体用例未验证；macOS 27 报告者的启动根因仍未确认。macOS 包为 ad-hoc 签名，非 Apple Developer ID 公证。Release 只附安装/便携包，不附测试日志或 JSON。

下文为作者 PR 阶段的历史资源覆盖测试，不是 v0.26.1 最终包报告；最终包以本节固定源码、资源哈希和 CI 结果为准。

## PR 阶段历史验收

2026-10-11 验证 PR #21。已合并当前 main `208b83cfac4347fe2e7d2423ff78cb172c0287a6`，其中相对 v0.26.0 的差异仅为发布文档。

## 验证环境与范围

- Windows x86_64 原生 Firefox，使用官方 `firefox-reverse-v0.26.0-windows-x86_64.zip`。
- ZIP SHA256：`0557526d18bf59df283ed2629b7d6ff4a601e168c0f6d6befb99ac1c54d7f50d`。
- Firefox `153.0a1`，BuildID `20261011033023`，包内 SourceStamp `bc338bafe690cb362b2d085a263609395f64937c`。
- 原始 `xul.dll` SHA256：`73e555a9fcfacd1f2cbbdb9a1f409d467f84057973e6625bd19a27ea3a6f4b21`。
- 只在解压副本中替换 PR 的 `AgentSession.sys.mjs`、`ConfigStore.sys.mjs`、侧栏 bundle 和 CSS；未重编译 Gecko。
- 每次创建独立 profile；API Key、模型、消息、提示词均为合成测试数据。LLM 服务为本机 HTTP mock，Firefox、React、prefs、AgentSession、AgentLoop 和工具 dispatch 均为真实实现。

## 可复现命令

安装当前 lockfile 的依赖并构建侧栏：

```sh
npm --prefix additions/browser/components/agent-sidebar ci --no-audit --no-fund
npm --prefix additions/browser/components/agent-sidebar run build
```

Windows 下载官方 ZIP 并运行原生验收：

```powershell
gh release download v0.26.0 --repo WhiteNightShadow/firefox-reverse --pattern firefox-reverse-v0.26.0-windows-x86_64.zip --dir work/download
python scripts/verify-global-system-prompt.py --package work/download/firefox-reverse-v0.26.0-windows-x86_64.zip --package-sha256 0557526d18bf59df283ed2629b7d6ff4a601e168c0f6d6befb99ac1c54d7f50d --output work/global-system-prompt-native
```

命令退出码为 0，输出目录包含 JSON 报告、截图和浏览器日志。脚本在该目录中新建安装副本及 profile，不访问个人浏览器配置。

完整轻量套件在 POSIX 环境运行（本次为 WSL Ubuntu、Node 23.9.0、Python 3.14.4、LF source checkout）：

```sh
bash scripts/selftest-agent-tools.sh
```

结果：bundle 构建、25 个 Node selftest、branding asset 检查、19 个 Python branding test 和 startup diagnostics 检查全部通过，整个脚本退出码 0。Windows Node 23.9.0 的 25 个 Node selftest 也全部退出 0。

## 可审阅证据

- 版本化证据目录：[`docs/verification/global-system-prompt-20261011/`](verification/global-system-prompt-20261011/)
- 原生报告：[`native-report.json`](verification/global-system-prompt-20261011/native-report.json)
- 原生设置截图：[`settings.png`](verification/global-system-prompt-20261011/settings.png)
- 完整回归和退出诊断日志：[`README.md`](verification/global-system-prompt-20261011/README.md)

## 原生验收结果

脚本通过 Marionette 操作 `SidebarController.show("viewAgentSidebar")` 中的真实 React 设置界面，并检查实际发送到本机端点的请求。

| 场景 | 结果 |
| --- | --- |
| UI 输入多行提示词、点击保存、关闭再打开设置 | 原文含首尾空白和 Unicode 均保存在 prefs，表单完整恢复 |
| 从侧栏发送消息 | 内置 system 后追加自定义正文，每条请求仅一份 |
| 完全退出再启动同一测试 profile | 表单和实际请求都继续使用已保存的提示词 |
| 在 UI 切换模型配置并新建会话 | 两个模型与不同会话共享该 profile 的提示词 |
| 从历史 UI 重新打开旧会话 | 使用当前 profile 提示词，不恢复旧会话的设置快照 |
| HTTP 请求进行中通过 UI 修改并保存 | 本轮后续真实工具调用和请求继续使用旧值；下一轮才使用新值 |
| 真实 AgentLoop 达到 `maxRounds` 后自动续跑 | 发生一次 checkpoint；三条实际请求中的 system 完全一致，未重复追加 |
| 在 UI 清空并保存 | 下一条请求的内置 system 与追加前逐字相同 |

最终共检查 11 条实际请求，两次浏览器退出码均为 0。自动续跑用真实 `page_info` 工具和 `maxRounds: 1` 触发，未 mock AgentSession 或 AgentLoop。其余对话通过侧栏发送按钮触发。

按现有 `verify-windows-package.py` 的 native 测试约定，退出前显式关闭测试打开的 ledger SQLite 连接；否则 `memory.sqlite` 会阻止 shutdown。此为测试 fixture 收尾步骤，不宣称本 PR 修复了该独立的 ledger 生命周期问题。脚本明确执行此收尾步骤，未把强制终止计为正常退出。

## Windows libuv 退出异常定位

使用不包含 PR 功能的 main source archive 对照试验，每种变体各跑三次：

| selftest-providers 结束方式 | Windows Node 23.9.0 | Windows Node 24.19.0 |
| --- | --- | --- |
| 原始 `server.close(); process.exit(0)` | 3/3 打印 `ALL PASS` 后断言，退出 `3221226505`（`0xC0000409`） | 本次 3/3 退出 0 |
| 等待 server close，但仍 `process.exit(0)` | 3/3 同一断言 | 本次 3/3 退出 0 |
| 原始 close，将结束改为 `process.exitCode = 0` | 3/3 退出 0 | 3/3 退出 0 |
| 等待 close，设置 `process.exitCode` | 3/3 退出 0 | 3/3 退出 0 |

异常输出为 `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING), file .../win/async.c`。这些对照将触发条件定位到测试的强制退出路径，而非 prompt 功能；没有推断未经调试的 libuv 内部根因。当前修复等待 HTTP server 关闭，并设置 `process.exitCode`，让异步句柄正常结束，失败仍保持非零退出码。

重现原始路径可以在单独导出的 main 目录中运行 `node additions/browser/components/agent-sidebar/dev/selftest-providers.mjs`。修复后的同一命令及完整回归均正常结束。Node 24.19.0 的原始脚本本次未重现，因此没有将先前观察到的退出异常标为跨版本必现。

未验证 macOS/Linux 原生浏览器 UI、外部真实模型服务或完整 Gecko 重编译。本验证结论仅覆盖 Windows 官方 v0.26.0 runtime 上的本 PR JavaScript/UI 资源。
