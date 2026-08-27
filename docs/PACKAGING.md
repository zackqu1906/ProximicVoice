# 桌面安装包

Proximic Voice 使用“轻量主安装包 + 模型按需下载”：安装包包含 UI、Ring SDK、
ASR/推理依赖和 16 kHz Opus 解码运行时，不包含 ASR 权重及约 2.5 GB 的本地 GGUF。
首次使用相应功能时，ASR 框架下载权重；本地文本模型由设置页的“下载本地模型”按钮下载。

所有可变文件都放在用户目录，应用安装目录保持只读：

- Windows：`%LOCALAPPDATA%\ProxiMic Voice`
- macOS：`~/Library/Application Support/ProxiMic Voice`
- macOS 模型缓存：`~/Library/Caches/ProxiMic Voice`

本地模型下载支持断点续传，并在启用前校验清单中的 SHA-256。Windows 与 macOS
各自下载匹配平台的 llama.cpp，GGUF 模型在两个平台共用同一份版本。

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

需要 Python 3.11、Homebrew 和 Xcode Command Line Tools：

```bash
./scripts/build-macos.sh
```

产物：`dist/ProximicVoice-0.6.0-macos-arm64.dmg`。构建脚本把 Homebrew libopus
复制进 `.app`，用户电脑不需要安装 Homebrew。

要让下载后的应用在其他 Mac 上无警告打开，构建机必须配置 Developer ID：

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Example (TEAMID)"
export APPLE_NOTARY_PROFILE="proximic-notary"
./scripts/build-macos.sh
```

未配置证书时脚本执行 ad-hoc 签名，适合本机开发验证，不等同于可公开分发的签名和公证。
应用的 `Info.plist` 已包含蓝牙和麦克风用途说明。

## 自动构建

`.github/workflows/build-installers.yml` 支持手工触发及 `v*` 标签构建两个平台。
公开发布 macOS 版本时，应在 CI 中导入签名证书和 notarytool keychain profile；默认 CI
产物仅为 ad-hoc 签名测试包。

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

在干净的 Windows 10/11 x64 和 Apple Silicon macOS 上分别验证：

1. 安装/拖入 Applications 后正常启动，蓝牙权限提示文案正确。
2. 扫描、选择 Ringo、连接、Opus 连续音频和断开后重连。
3. 首次 ASR 权重下载、重启后的缓存复用。
4. 本地模型下载中断后续传、校验完成、llama-server 启动和修改确认。
5. 数据、缓存和修改采集文件均落在用户目录，卸载不会误删用户模型/数据。
