# PR #21 验收证据

本目录保存 2026-10-11 对 PR #21 的可审阅证据。原始验证在隔离副本和独立 Firefox profile 中进行；本目录不包含 Firefox ZIP、profile、API key 或个人路径。

## 文件

- `native-report.json`：Windows 官方 v0.26.0 package 的 8 项原生 sidebar 验收、11 条 synthetic 请求和两次浏览器退出码。
- `settings.png`：真实 Firefox sidebar 设置界面截图。
- `selftest-windows-node23.log`：Windows Node 23.9.0 的 25 项 Node selftest，每个进程均记录退出码 0。
- `selftest-wsl-linux-node23.log`：WSL Ubuntu 的完整 `scripts/selftest-agent-tools.sh` 输出，包含 25 项 Node selftest、19 项 Python branding tests 和 startup diagnostics；退出码为 0。
- `libuv-exit-matrix.json`：Node 23.9.0/24.19.0、四种结束路径、每种三次对照矩阵。
- `libuv-node23-original.log` / `libuv-node23-graceful.log`：原始强制退出路径和修复后自然退出路径的代表性日志。
- `native-firefox-1.log` / `native-firefox-2.log`：两次真实 Firefox native run 的诊断日志。日志中的 graphics、Remote Settings 和 locale 警告来自隔离 headless runtime；两次进程均正常退出。

## 复现入口

完整命令和限制见 [`docs/global-system-prompt-validation.md`](../../global-system-prompt-validation.md)。原生脚本会校验官方 ZIP SHA256，并只修改新建的解压副本与 profile：

```powershell
npm --prefix additions/browser/components/agent-sidebar ci --no-audit --no-fund
npm --prefix additions/browser/components/agent-sidebar run build
python scripts/verify-global-system-prompt.py --package <v0.26.0-windows-x86_64.zip> --package-sha256 0557526d18bf59df283ed2629b7d6ff4a601e168c0f6d6befb99ac1c54d7f50d --output <output-dir>
```

原生验证使用本机 HTTP mock，不发送真实模型请求。测试退出前显式关闭测试创建的 ledger SQLite connection，以避免独立的 `memory.sqlite` shutdown blocker；这只是 fixture cleanup，不是本 PR 的修复范围。
