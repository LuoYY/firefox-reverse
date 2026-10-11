# macOS 启动排查

适用于 [Issue #18](https://github.com/WhiteNightShadow/firefox-reverse/issues/18) 一类启动反馈。报告者已补充 v0.25.0、M1 Max、双击启动失败而直接执行带 `-no-remote` 可以启动。仍缺同一可执行文件/同一 profile 的对照诊断，不能据此认定 macOS 27 不兼容，也不能宣称已经修复其根因。

## 先区分错误

- “Firefox 配置文件无法加载/可能丢失或不可访问”通常来自 Firefox 的 **profile 用户数据目录**。它不是 `fingerprint.json`，创建一个同名 JSON 文件不能代替目录。
- 指纹配置不可读或格式错误：本轮 C++ 会记录 Invalid 并停用覆盖，环境管理器会显示保存/打开错误；不要把它和 profile 缺失混为一谈。
- `application.ini`、XUL 缺失或签名验证失败：属于应用包/安装完整性问题。

## 只读检查

在仓库目录运行（需要 Python 3）：

```sh
python3 scripts/diagnose-macos-startup.py \
  --app "/Applications/Firefox Reverse.app" \
  --output /tmp/firefox-reverse-startup.json
```

工具只读取系统版本、架构、应用元数据、签名状态、profile 注册项及目录可访问性，不读取 cookies、登录信息、模型 Key 或完整偏好设置，不删除锁或修改 profile。只在指定 `--output` 时写诊断报告；分享前仍请检查个人路径。

若诊断提示安装默认项指向不存在的目录，可完整退出 Firefox Reverse 后打开原生配置管理器，手动选择仍存在的 profile：

```sh
open -n -a "/Applications/Firefox Reverse.app" --args -new-instance -P
```

不要直接删除 `profiles.ini`、`installs.ini`、整个 Firefox 数据目录或正在使用的锁。创建新 profile 会得到独立空环境，不会把旧书签、cookie 自动搬进去。

当前锁定的 Firefox 153 基线已忽略 `-no-remote`；有效的独立实例参数是 `-new-instance`，它不绕过 profile 锁，不修复丢失数据。不能仅凭增加 `-no-remote` 后成功就断定实例冲突，直接命令启动与 Finder 的环境也可能不同。

v0.26.0 对未显式指定 profile 的交互启动增加有限恢复入口：仅当默认目录确认不存在或不是目录，才显示原生 Profile Manager 由用户选择；权限探测异常、无头启动、reset、显式 `-profile` 和重启路径保持原行为，不自动创建空 profile。

反馈时请附：Firefox-Reverse 版本及包文件名、Intel/Apple Silicon、`sw_vers` 输出、完整弹窗截图、启动方式（双击/MCP/环境管理）及脱敏诊断结果。本轮本地开发机为 macOS 26.7.1 ARM64；其他系统结果需对应机器验证。
