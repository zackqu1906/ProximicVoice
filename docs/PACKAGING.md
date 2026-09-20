# 桌面安装包

Proximic Voice 使用“轻量主安装包 + 模型按需下载”：安装包包含 UI、Ring SDK、
ASR/推理依赖和 16 kHz Opus 解码运行时，不包含 ASR 权重及约 2.5 GB 的本地 GGUF。
首次使用相应功能时，ASR 框架下载权重；本地文本模型由设置页的“下载本地模型”按钮下载。

所有可变文件都放在用户目录，应用安装目录保持只读：

- Windows：`%LOCALAPPDATA%\ProxiMic Voice`
- macOS：`~/Library/Application Support/ProxiMic Voice`
- macOS 模型缓存：`~/Library/Caches/ProxiMic Voice`

本地模型下载会在镜像和官方地址之间自动重试、从 `.part` 断点续传，并在启用前校验
清单中的文件大小和 SHA-256。Windows 与 macOS 各自下载匹配平台的 llama.cpp，GGUF
模型在两个平台共用同一份版本。若两个地址最终都被当前网络拦截，失败信息会保留断点；
更换网络或启用能访问 Hugging Face 文件 CDN 的代理/VPN 后，再点一次即可继续。

Windows 的 GGUF 断点默认位于：

```text
%LOCALAPPDATA%\ProxiMic Voice\local-llm\models\qwen3-4b-instruct-2507-q4-k-m\Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf.part
```

不要手工删除这个文件，除非 UI 明确报告大小或 SHA-256 校验失败。

## Windows x64

先准备 CPU 运行环境（不下载本地 LLM），安装 Inno Setup 6，然后构建：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Compute cpu -SkipLocalLLM
powershell -ExecutionPolicy Bypass -File .\scripts\build-windows-installer.ps1
```

产物：`dist/installer/ProximicVoice-0.6.0-windows-x64-setup.exe`。安装器为当前用户
安装，不要求管理员权限，并创建开始菜单入口，可选桌面快捷方式。

公开分发前可通过证书存储中的代码签名证书为安装包签名：

```powershell
$env:WINDOWS_SIGNING_CERT_SHA1 = "证书指纹"
$env:WINDOWS_TIMESTAMP_URL = "http://timestamp.digicert.com" # 可省略
.\scripts\build-windows-installer.ps1
```

脚本会调用 Windows SDK 的 `signtool.exe`，完成 SHA-256 签名、时间戳和签名校验。
未配置证书时生成的安装包功能不受影响，但其他电脑可能显示 SmartScreen 来源警告。

## macOS Apple Silicon

当前产物支持 Apple Silicon（arm64）和 macOS 15 及以上。构建机需要 Xcode Command
Line Tools；脚本会把固定版本的 Python 3.11、构建工具和 libopus 安装到项目目录，不要求
Homebrew，也不会修改系统 Python：

```bash
./scripts/build-macos.sh
```

产物：`dist/ProximicVoice-0.6.0-macos-arm64.dmg`。构建脚本把自行编译的 libopus
复制进 `.app`，用户电脑不需要安装 Python、Homebrew 或其他运行库。生成 DMG 前，脚本会实际启动冻结后的
可执行文件执行 `--self-check-package`，验证内置 libopus、全部必需 QML 模块、QApplication、
控制器、ASR 导入和 QML 根窗口；启动失败会直接终止构建，详细信息写入
`.build/macos-smoke-data/logs/startup.log`。

要让下载后的应用在其他 Mac 上无警告打开，构建机必须配置 Developer ID：

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Example (TEAMID)"
export APPLE_NOTARY_PROFILE="proximic-notary"
./scripts/build-macos.sh
```

未配置证书时脚本执行 ad-hoc 签名，适合本机开发验证，不等同于可公开分发的签名和公证。
应用的 `Info.plist` 已包含蓝牙和麦克风用途说明。

### 输入法随包安装（2026-09-18）

macOS 构建先编译 Swift 输入法和 `InputMethodAdmin`，再将两者完整放入 app 的
`Contents/Helpers` 并签名。组件最低版本与主程序统一为 macOS 15；本地默认临时签名。
`--self-check-package` 会核对组件身份、可执行文件及签名，缺失时构建失败。
冻结 app 从自身路径寻找安装资源，既不引用开发者源码目录，也不依赖用户安装 Python／Xcode。

首次打开主程序，点击“输入法设置”→“安装／更新输入法”。安装器复制到当前用户的
`~/Library/Input Methods/ProxiMicInput.app`，注册并启用，但不选择输入源。更新时先手动切回
拼音或 ABC；旧组件如仍运行，会核对用户和完整路径后正常退出，安装失败则回滚。
同一窗口提供辅助功能和键盘设置入口。新 app 替换完成后也要从此入口更新系统中的输入法。
DMG 内附 `安装说明.txt`，说明连接戒指、服务配置、授权和手动切换步骤。

已有依赖环境且没有修改依赖时，可离线快速重建：

```bash
PROXIMIC_SKIP_DEPENDENCY_INSTALL=1 bash scripts/build-macos.sh
```

只更新 `.app` 并完成签名、便携性和启动检查，保留已有 DMG：

```bash
PROXIMIC_SKIP_DEPENDENCY_INSTALL=1 bash scripts/build-macos.sh --app-only
```

macOS 打包默认在最终签名前精简 10 个已验证运行库的调试和本地符号，当前依赖版本
约节省 84 MiB。工具 `tools/strip_macos_runtime.py` 对比处理前后的导出符号和动态链接
依赖，不一致即中止构建；体积明细保存在 `.build/macos-runtime-size.json`。
主可执行文件中的 Python 归档、ASR/手势模型、Opus、输入法组件及其他模块均保留。
依赖升级导致库路径变化时会明确报错，需要重新检查名单，不能静默漏掉或扩大精简范围。

构建仍从当前 `src` 收集代码与资源，不复用已安装的旧项目包。打包启动检查使用独立
IME socket，避免占用正在运行的源码主程序连接。安装器诊断可使用打包可执行文件
`--input-method verify`／`status`；`--input-method install` 与界面共用同一安装实现。
临时签名包没有 Apple 公证，其他 Mac 下载后的 Gatekeeper 放行仍需用户按系统提示完成。

### 更新后的辅助功能权限

输入法组件安装与主程序的辅助功能授权是两条独立流程。临时签名更新可能使旧授权不再匹配，
但无需每次都先删除权限：先完全退出旧进程、替换 `/Applications` 中的 app，再打开新版并
查看主界面的权限状态。重新授权后会自动检测，系统确认生效即可继续使用，无需例行重启。
若持续未生效，核对实际运行路径，必要时删除旧条目、添加新版 app；系统仍未放行时再退出重开。
不要从 DMG 或另一个同名副本继续运行。

主界面同时检测当前进程的 Accessibility 信任与按键发送权限，异常时持续显示警告。
检测在后台进行：启动和回到主界面时立即刷新，未授权时每 1 秒、已授权时每 10 秒刷新，
主窗口不在前台时仍持续检测。实际发键仍做即时检查，权限生效后下一次操作即可使用。
授权恢复不会自动重放之前失败的发送、编辑或撤销。系统授权状态的返回延迟不受程序控制；
检测恢复也不能修复临时签名变化造成的旧授权身份不匹配。
设置页提供授权入口、重新检测、显示当前 app 和复制诊断。诊断只含权限状态、进程与路径，
不含文本内容。程序不会自动清空 TCC 权限，也不会用 Accessibility 的成功结果绕过发键拒绝。

## 自动构建

`.github/workflows/build-installers.yml` 支持手工触发及 `v*` 标签构建两个平台。
GitHub Actions 手工构建在没有证书时仍可生成 ad-hoc 测试包。`v*` 标签发布则会强制
校验 Developer ID 签名和 Apple 公证配置，缺少任一 Secret 都会终止发布，避免把不能在
新 Mac 上正常通过 Gatekeeper 的包上传到 Releases。仓库需要配置：

- `APPLE_CERTIFICATE_BASE64`：Developer ID Application 的 `.p12` 文件（Base64）。
- `APPLE_CERTIFICATE_PASSWORD`：导出 `.p12` 时设置的密码。
- `APPLE_SIGNING_IDENTITY`：完整的 `Developer ID Application: ... (TEAMID)` 名称。
- `APPLE_NOTARY_APPLE_ID`、`APPLE_NOTARY_TEAM_ID`、`APPLE_NOTARY_PASSWORD`：公证用
  Apple ID、Team ID 和 app-specific password。
- `APPLE_KEYCHAIN_PASSWORD`：可选的 CI 临时钥匙串密码。

- 手工触发：构建结果保存在 Actions Artifact，适合内部验证。
- 推送 `v*` 标签：两个平台构建成功后自动创建 GitHub Release，附带 `.exe`、`.dmg`
  和 `SHA256SUMS.txt`，用户可从 Releases 页面直接下载。

发布示例：

```bash
git tag v0.6.0
git push origin v0.6.0
```

公开下载页：<https://github.com/zackqu1906/ProximicVoice/releases>

## 发布前硬件验收

在干净的 Windows 10/11 x64 和 Apple Silicon macOS 15+ 上分别验证：

1. 安装/拖入 Applications 后正常启动，蓝牙权限提示文案正确。
2. 扫描、选择 Ringo、连接、Opus 连续音频至少 15 分钟、连续说 20 次，并验证单个 BLE
   音频块丢失后后续语音仍可继续；再测试断开后重连。
3. 首次 ASR 权重下载、重启后的缓存复用。
4. 本地模型下载中断后续传、校验完成、llama-server 启动和修改确认。
5. 数据、缓存和修改采集文件均落在用户目录，卸载不会误删用户模型/数据。
6. 清空 `ARK_API_KEY`、`VOLC_ASR_API_KEY` 环境变量后，分别在设置页填写线上大模型和
   线上语音模型 Key，重启应用确认配置仍在，并各完成一次真实请求。


## 2026-09-18 本地输入法整包验证

临时签名 DMG 已生成：`ProximicVoice-0.6.0-macos-arm64.dmg`（约 217.9 MiB）。
SHA-256：`6602c913cbf0524fbcbc5340c3a020947e15a68fd925b86b47e85650b6023d1e`。输入法二进制 SHA-256：`f358d0cb17edc0a15f709548981a4bf1696270708ff50a26f5f4b7c4553f10b5`。

- 79 项 Python 安装器／打包入口／输入法状态／快捷键／语音链路回归，以及 21 项 QML 界面回归通过。
- 整包临时签名、macOS 15 部署目标和动态库依赖检查通过；最新 QML 与源码一致，打包清单包含最新安装器、行内控制器、原生快捷键模块和戒指手势资源。
- DMG 校验和通过；从只读挂载的 DMG 运行冻结 app 的独立启动检查，Opus、QML、ASR 和内置输入法安装资源均通过。
- 从该 DMG 的冻结可执行文件调用同一安装器，在本机用户目录实际安装组件，系统确认注册及启用成功，未改变选中的输入源；安装后的二进制与 DMG 内组件一致。
- 以上不等于另一台干净 Mac 的验收。本包没有 Developer ID 公证；按用户要求保留本地临时签名。

## Opus 自带运行库

`opuslib` 是 Python 绑定，依赖已列入 `ring-opus` extra、requirements.txt 和 macOS/Windows 锁定文件；macOS App 同时携带 `Contents/Frameworks/opus/libopus.0.dylib`，用户无需安装 Opus、Homebrew 或 Python。

打包版直接从自身资源目录定位动态库，不接受旧的 `PROXIMIC_OPUS_DIR` 覆盖，也不回退到开发机或用户机器上的系统 Opus；缺失时提示重新安装完整应用。打包自检使用五个真实 Opus 静音帧完成 16 kHz 单声道解码，确认得到 3200 字节 PCM。缺少库或解码失败会使自检失败，阻止继续生成 DMG。

源码首次安装 `scripts/setup-macos.sh` 会在 `.runtime/opus/lib` 缺少运行库时自动调用项目内的构建脚本，并验证实际解码。这个开发安装步骤需要编译工具和网络；DMG 使用者无需执行它。
