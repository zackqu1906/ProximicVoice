# Proximic Voice

Proximic Voice 是面向 Ringo 可穿戴设备的近场实时语音输入工具。它使用 ProxiMic
两阶段模型判断用户是否靠近说话，把音频实时发送给可替换的 ASR 后端，并可将最终
文本注入 Windows 中当前获得焦点的输入框。

当前版本提供可直接运行的 Windows 桌面体验，以及 Apple Silicon Mac 的开发运行路径，
并支持算法验证与后续产品化扩展。

## 功能

- Ringo BLE 连接、PCM 音频流与自动重连错误提示。
- ProxiMic Stage1 + CNN Stage2 近场触发，不依赖传统 VAD 作为起止条件。
- streaming-sensevoice 与 Fun-ASR-Nano 两种流式识别后端。
- 设备连接、语音识别开关和设备断开三个独立状态。
- 可选择、复制和编辑的识别文本区。
- Windows Unicode 文本注入，不占用剪贴板。
- `Ctrl+Alt+Space` 按住说话。
- PySide6 + Qt Quick/QML 桌面界面。

## 系统要求

- Windows 10/11，或 Apple Silicon Mac（macOS 13+）。
- Windows 安装不要求预先安装 Python；Mac 需要 Python 3.11。
- 支持蓝牙的电脑和 Ringo 设备。
- 首次安装依赖和首次下载 ASR 模型时需要联网。
- Windows 安装时可选择兼容性更好的 CPU 模式，或经过验证的 NVIDIA GPU 模式。
  Apple Silicon macOS 当前使用 CPU。

Fun-ASR-Nano 的模型权重约 2 GB，不包含在 Git 仓库中。第一次使用该后端时会由
模型库下载到本机缓存。

## Windows 安装

```powershell
git clone <你的仓库地址>
cd ProximicVoice
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

安装脚本会显示 CPU/GPU 选择。CPU 适用于所有支持的 Windows 电脑；检测到 NVIDIA
显卡时可以选择 GPU，脚本会直接安装 CUDA 版 PyTorch，不会先下载 CPU 版再覆盖。
无人值守或需要明确指定时使用：

```powershell
# CPU
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Compute cpu

# NVIDIA GPU（CUDA 12.8）
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Compute cuda
```

安装脚本会从 `python.org` 下载并校验固定的 64 位 CPython 3.11.9，解压到项目的
`.runtime/`，再使用它创建项目专用的 `.runtime/venv/`。脚本不会调用电脑中已有的 Python、
Conda 或 Anaconda。关键原生依赖版本由 `requirements-windows.lock` 固定，安装结束前
还会实际导入 PyTorch、PySide6 和 UI；只有自检通过才会显示安装完成。

安装完成后启动：

```powershell
.\scripts\start-ui.cmd
```

安装损坏或需要彻底重建时：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Recreate
```

在 VSCode 中开发时，通过 `Python: Select Interpreter` 选择：

```text
<项目目录>\.runtime\venv\Scripts\python.exe
```

## macOS（Apple Silicon）

Mac 使用项目内独立环境和缓存，并通过 `requirements-macos.lock` 固定关键原生依赖。
首次安装：

```bash
./scripts/setup-macos.sh
```

如果尚未安装 Python 3.11：

```bash
brew install python@3.11
```

也可以显式指定解释器：

```bash
PROXIMIC_PYTHON=/opt/homebrew/bin/python3.11 ./scripts/setup-macos.sh
```

安装完成后启动：

```bash
./scripts/start-ui.sh
```

首次连接 Ring 时，允许 Terminal 或 Python 使用蓝牙。若曾拒绝授权，请在
“系统设置 → 隐私与安全性 → 蓝牙”中重新开启。

macOS 当前支持 Ring BLE、ProxiMic 检测、三个 UI 可选 ASR 后端、字幕与编辑区。Windows Unicode
文字注入和 `Ctrl+Alt+Space` 全局按住说话在 Mac 上会自动关闭，不影响其余识别链路。
Mac 安装脚本使用原生 Apple Silicon PyTorch，但本项目当前尚未开放 MPS ASR 加速，
所以“运行设备”只显示 CPU。

`.runtime/`、`.cache/` 都位于项目目录且不会提交到 Git。项目放在哪个盘，
运行时、依赖和模型缓存就位于哪个盘。

## 界面使用

1. 检查 ProxiMic 模型、Stage1 threshold 和 ASR 后端。
2. 点击“选择并连接设备”，应用会实时发现附近的 BLE 设备。
3. 搜索框默认填写 `Ringo`，用于缩小显示范围；可以修改搜索词，也可以清空后查看全部设备。
4. 在列表中确认设备名称和标识，点击对应设备右侧的“连接”。程序不会自动选择设备。
5. 连接成功后点击“开启语音识别”，开始 ProxiMic 自动近场监听。
6. “暂停语音识别”只暂停检测和 ASR，Ring 仍保持连接。
7. 需要释放硬件时单独点击“断开设备”。
8. 关闭主窗口会退出程序；终端中的 `Ctrl+C` 也会触发退出。

“运行设备”使用下拉选择：始终提供 CPU，并自动列出当前 PyTorch 能识别的 NVIDIA GPU
及其名称。用户不需要手动填写 `cuda:0`；如果没有显示 GPU，请安装 CUDA 版 PyTorch
并重启应用。如果 Windows 检测到 NVIDIA 显卡、但当前安装的是 CPU 版 PyTorch，设置区
会显示“安装 NVIDIA GPU 加速”按钮；确认后应用退出，由独立脚本完成安装、验证并重启。
macOS 当前保持使用 CPU，不会显示该 Windows 专用安装按钮。

扫描本身不会按名称过滤，搜索框只控制列表中显示的项目。设备首次出现后位置保持不变，
后续扫描只更新已有信息并把新设备追加到末尾，避免实时刷新导致列表反复重排。Windows
通常显示 MAC 地址，macOS 通常显示 CoreBluetooth UUID。点击连接后还会检查设备是否提供项目需要的 NUS 服务，普通 BLE
设备连接失败时会给出错误，不会被当作 Ring 使用。

### 容易混淆的运行状态

| UI 状态 | Ring 音频连接 | ProxiMic 近场检测 | ASR 语音识别 |
| --- | --- | --- | --- |
| 设备已连接，识别暂停 | 保持 | 不运行 | 不运行 |
| 自动监听中 | 保持 | 持续运行 Stage1 + Stage2 | 仅在 Stage2 判定为近场语音后启动 |
| 按住 `Ctrl+Alt+Space` | 保持 | 仍运行 | 在“语音识别已开启”时强制开始或保持当前会话 |
| 设备已断开 | 关闭 | 不运行 | 不运行 |

- “开启语音识别”实际是开启 **ProxiMic 自动监听**，并不是把全部声音持续交给 ASR。
- “暂停语音识别”会同时暂停 ProxiMic 和 ASR，但不会断开 Ring；恢复时不需要重新连接设备。
- ASR 模型会在连接阶段加载到内存，因此暂停状态下也可能已经占用内存，但不会处理音频。
- SDK 日志中的 `Connected successfully` 只代表 BLE 层建链；`mic capturing -> ...` 只代表已经发送 MIC ON 并创建录音文件。UI 会在收到有效 PCM 音频后才显示设备已连接。

发布版近场模型位于：

```text
src/proximic_ring/assets/ringo-near-v1.model
```

默认 Stage1 threshold 为 `0.005`。设备、佩戴位置和环境发生明显变化时需要重新验证
阈值，必要时使用新的 Ringo 数据训练模型。

## 第三方 ASR

项目将已经验证过的第三方源码快照放在：

```text
third_party/streaming-sensevoice
third_party/Fun-ASR
```

UI 会自动使用这两个目录，用户无需另行克隆。第三方模型权重不会提交到本仓库，来源和许可证说明见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

### 源码、依赖和模型不是同一份东西

| 位置 | 内容 | 是否提交到 Git |
| --- | --- | --- |
| `third_party/` | 已验证的第三方 ASR 源码快照 | 是 |
| `.runtime/venv/` | PyTorch、PySide6、FunASR 等可执行 Python 依赖 | 否 |
| `.cache/modelscope/` | SenseVoice、Fun-ASR-Nano 等模型权重 | 否 |

`third_party/` 中有模型实现源码，不等于已经安装运行依赖，也不包含数 GB 的模型权重。
三者用途不同，并不是重复安装。

ASR 模型第一次使用时需要下载；以后启动时仍要从硬盘加载到内存，这一步可能需要几十秒，
但不等于重新下载。日志出现 `Downloading N files` 也可能只是模型库检查缓存；如果随后显示
`Loading pretrained params from ...\.cache\modelscope\...\model.pt`，使用的是本地权重。
下载中断产生的 `.incomplete` 文件会在下次运行时继续下载。

## 手动安装

正式 Windows 安装请使用 `scripts/setup.ps1`。下面的方式只供开发者在已经自行维护的
兼容环境中调试，不属于标准用户安装路径：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -c requirements-windows.lock -e ".[ring,asr-streaming-sensevoice,asr-funasr-nano,asr-volcengine,ui]"
python -m proximic_ring.ui
```

只需要较轻量的 streaming-sensevoice 后端时：

```powershell
python -m pip install -e ".[ring,asr-streaming-sensevoice,ui]"
```

## 测试

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

需要实际硬件、模型或网络的集成验证应在本机单独进行。

## 项目结构

```text
src/proximic_ring/       ProxiMic、ASR、桌面输出和 UI
src/ring_python_sdk/     项目当前使用的 Ringo SDK snapshot
third_party/             已验证的外部 ASR 源码快照（不含模型权重）
tests/                   自动化测试
docs/                    算法、ASR、UI 和设备接入文档
scripts/                 一键安装与启动脚本
experiments/             可复现实验脚本和结果图
```

## 隐私与发布说明

- `data/`、`datasets/`、训练输出和本地 ASR 权重已被 `.gitignore` 排除。
- 不要提交录音、API Key、`.env` 或带有个人信息的日志。
- 当前仓库尚未为第一方代码选择公开许可证；公开发布前请由项目所有者确定许可证。
- `src/ring_python_sdk/` 的再分发权限需要由设备/SDK 提供方确认。未确认前建议使用
  GitHub Private 仓库，避免公开分发。

更详细的使用说明见 [`docs/CUSTOMER_UI.md`](docs/CUSTOMER_UI.md) 和
[`docs/ASR_BACKENDS.md`](docs/ASR_BACKENDS.md)。
