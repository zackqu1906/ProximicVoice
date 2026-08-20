# Proximic Voice

Proximic Voice 是面向 Ringo 可穿戴设备的近场实时语音输入工具。它使用 ProxiMic
两阶段模型判断用户是否靠近说话，把音频实时发送给可替换的 ASR 后端，并可将最终
文本注入 Windows 中当前获得焦点的输入框。

当前版本提供可直接运行的 Windows 桌面体验，以及 Apple Silicon Mac 的开发运行路径，
并支持算法验证与后续产品化扩展。

## 功能

- Ringo BLE 连接、默认 PCM 音频传输、实时音频流监控与明确的断线提示。
- ProxiMic Stage1 + CNN Stage2 近场触发，不依赖传统 VAD 作为起止条件。
- streaming-sensevoice 与 Fun-ASR-Nano 两种流式识别后端。
- 设备连接、语音识别开关和设备断开三个独立状态。
- 可选择、复制和编辑的识别文本区。
- “听写 / 指令”双模式；可将 ASR final 交给 OpenAI-compatible 大模型整理或生成最终文本。
- Windows Unicode 文本注入，不占用剪贴板。
- 右 `Alt` 按住说话。
- PySide6 + Qt Quick/QML 桌面界面。

## 系统要求

- Windows 10/11，或 Apple Silicon Mac（macOS 13+）。
- Windows 安装不要求预先安装 Python；Mac 需要 Python 3.11。
- 支持蓝牙的电脑和 Ringo 设备。
- 首次安装依赖、本地 LLM 包和首次下载 ASR 模型时需要联网。
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

安装过程中还会自动下载并校验本地 LLM 包：官方 llama.cpp Windows x64 运行时约 18 MB，
`Qwen3-4B-Instruct-2507` 的 `Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf`
约 2.5 GB。二者安装到
`.runtime/local-llm/`，与 `.runtime/venv/` 并列；重建虚拟环境不会重复下载模型。
下载支持断点续传，完成后必须匹配包清单中的 SHA-256。磁盘空间不足时，可以在安装前
指定其他盘：

```powershell
$env:PROXIMIC_LLM_HOME = "D:\ProximicVoiceModels"
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

仅开发调试且明确不需要本地模型时可加 `-SkipLocalLLM`，标准用户安装不应跳过。

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
文字注入和右 `Alt` 全局按住说话在 Mac 上会自动关闭，不影响其余识别链路。
Mac 安装脚本使用原生 Apple Silicon PyTorch，但本项目当前尚未开放 MPS ASR 加速，
所以“运行设备”只显示 CPU。

`.runtime/`、`.cache/` 都位于项目目录且不会提交到 Git。项目放在哪个盘，
运行时、依赖和模型缓存就位于哪个盘。

## 界面使用

1. 检查 ProxiMic 模型、Stage1 threshold、ASR 后端以及本地 llama-server/GGUF 路径。
2. 点击“选择并连接设备”，应用会实时发现附近的 BLE 设备。
3. 搜索框默认填写 `Ringo`，用于缩小显示范围；可以修改搜索词，也可以清空后查看全部设备。
4. 在列表中确认设备名称和标识，点击对应设备右侧的“连接”。程序不会自动选择设备。
5. 应用先连接设备并验证真实 PCM 音频，再依次加载检测模型和语音模型；界面会显示当前阶段。
6. 把光标放到任意外部文本框；使用“输入到光标”或“修改当前文本”，全局快捷键为 `Alt+1` / `Alt+2`，按住右 `Alt` 说话。
7. 出现“准备就绪”后，点击“开启语音识别”，开始 ProxiMic 自动近场监听。
8. “暂停语音识别”只暂停检测和 ASR，Ring 仍保持连接。
9. 需要释放硬件时单独点击“断开设备”。
10. 关闭主窗口会退出程序；终端中的 `Ctrl+C` 也会触发退出。

### 大模型文本处理

桌面应用可在设置中选择安装阶段准备的本地 GGUF，或通过火山方舟调用在线模型。输入模式会
删除口语填充、修正明显识别错误和标点，然后把结果注入说话开始时获得焦点的外部文本框。
LLM 请求在独立线程执行，不阻塞 Ring、ProxiMic 或 ASR。

选择“火山方舟（在线）”时，默认 Base URL 为
`https://ark.cn-beijing.volces.com/api/v3`，默认模型为
`doubao-seed-2-0-lite-260215`，也可在模型下拉框选择
`deepseek-v4-flash-260425`。两者复用相同的听写提示词、修改 JSON schema 和确定性执行器，
并统一通过方舟非流式 `/responses` 接口调用。API Key 只从 `ARK_API_KEY` 环境变量读取，应用设置中只保存
环境变量名、Base URL 和模型 ID，不保存 Key。启动应用前可在 PowerShell 中设置：

```powershell
$env:ARK_API_KEY = "<your-ark-api-key>"
```

模型 ID 可在设置中修改为当前方舟账号已开通的其他模型；不存在或无权访问的模型会返回
`InvalidEndpointOrModel.NotFound`。切回本地模式后仍会自动启动 `llama-server`，不会调用
线上 API。

修改模式读取当前外部文本框全文，将下一段语音作为编辑指令。删除、替换和前后插入时，
本地模型只生成短 JSON 操作计划，由 Python 确定性执行；只有润色、翻译、扩写、重排等
生成式任务才由模型返回全文。最终候选不会立即写回，而是进入后台会话记录；悬浮窗只
持续显示最后识别的修改指令和操作选项：

- `Enter`：确认，将候选应用到已锁定的原文本框。
- `Esc`：取消，不修改外部文本。
- 再次按住右 `Alt`：保留同一个目标和原文，重新说一次修改指令。

这三个动作与输入/修改模式都是设备无关的交互事件，后续 Ring 手势直接映射到相同动作，
不需要修改 ASR、提示词或桌面文本适配层。主窗口不再保存一份可编辑草稿，只记录原始 ASR、
LLM 结果、目标窗口以及最终是否应用。

选择本地模式时，应用首帧显示后会在后台启动模型，并用极短请求分别预热输入和修改提示词。
llama.cpp 显式启用 prompt cache，因此第一次真实口述通常不再承担模型加载和固定提示词
预填充开销。选择火山方舟时不做预热，收到最终 ASR 后直接请求所选线上模型。

不启动 Ring、ASR 和 UI，也可以直接测试同一套提示词与大模型请求流程。交互模式可选择
本地 Qwen、豆包、DeepSeek，或把同一份听写/指令输入同时交给三者比较；选择本地时会
自动启动 `llama-server`，后续测试直接复用该本地服务：

```powershell
python .\tools\test_llm.py
```

程序随后会逐次询问“指令 / 听写”类型。指令模式会先读取待修改全文，再读取修改要求，
最后显示各模型的原始 tool arguments、确定性执行后的完整文本、耗时及文本差异。
本地编辑请求通过 `tool_choice` 强制调用 `submit_text_edit_plan`；普通 content JSON
会被拒绝，因此成功结果一定来自 llama.cpp 返回的 `message.tool_calls`。
独立测试工具的本地 Qwen 默认超时为 180 秒，仍可通过 `--timeout` 覆盖。
也可以执行单次三模型比较：

```powershell
$env:ARK_API_KEY = "<your-ark-api-key>"
python .\tools\test_llm.py --provider compare --mode edit `
  --target-text "会议安排在周四。" --text "把周四改成周五"
```

单次本地测试：

```powershell
python .\tools\test_llm.py --provider local --mode dictation --text "明天下午三点开会"
```

线上 OpenAI 单次测试仍作为开发诊断能力保留，并可显示实际提示词：

```powershell
$env:OPENAI_API_KEY = "<your-key>"
python .\tools\test_llm.py --provider openai --mode edit --target-text "原文" --text "改正式一点" --show-prompt
```

OpenAI 默认模型为 `gpt-5.6-luna`；可通过 `--model` 或 `OPENAI_MODEL` 修改。API Key
仅从指定环境变量读取，不会由测试程序输出或保存。本地模式默认读取安装目录
`.runtime/local-llm/`；仍可通过 `--local-model-path`、`--local-server-path` 或相应环境
变量覆盖，方便开发者测试其他 OpenAI-compatible 本地服务和 GGUF。

“Ring 音频编码”默认使用 PCM，使 ProxiMic 收到的波形与近点模型训练和阈值校准时保持
一致。ADPCM 能减少 BLE 传输包数量，但有损压缩可能改变 Stage2 分数分布；Opus 带宽
更低，但要求系统存在 libopus 运行库。

“运行设备”使用下拉选择：始终提供 CPU，并自动列出当前 PyTorch 能识别的 NVIDIA GPU
及其名称。用户不需要手动填写 `cuda:0`；如果没有显示 GPU，请安装 CUDA 版 PyTorch
并重启应用。如果 Windows 检测到 NVIDIA 显卡、但当前安装的是 CPU 版 PyTorch，设置区
会显示“安装 NVIDIA GPU 加速”按钮；确认后应用退出，由独立脚本完成安装、验证并重启。
macOS 当前保持使用 CPU，不会显示该 Windows 专用安装按钮。

扫描本身不会按名称过滤，搜索框只控制列表中显示的项目。设备首次出现后位置保持不变，
后续扫描只更新已有信息并把新设备追加到末尾，避免实时刷新导致列表反复重排。Windows
通常显示 MAC 地址，macOS 通常显示 CoreBluetooth UUID。点击连接后还会检查设备是否提供项目需要的 NUS 服务，普通 BLE
设备连接失败时会给出错误，不会被当作 Ring 使用。

用户点击列表中的设备后，应用会直接复用该次扫描得到的 BLE 设备对象进入连接，不会再做
一次固定 8 秒扫描。这也避免了新设备使用轮换地址时出现“列表可见、二次扫描却匹配不到”
的问题；若扫描对象已经失效，界面会报告连接失败并自动断开，重新打开设备列表即可刷新。

### 容易混淆的运行状态

| UI 状态 | Ring 音频连接 | ProxiMic 近场检测 | ASR 语音识别 |
| --- | --- | --- | --- |
| 正在连接设备 / 正在验证设备音频 | 建立并验证中 | 未加载 | 未加载 |
| 正在加载检测模型 / 正在加载语音模型 | 已验证并保持 | 加载中或已加载 | 未加载或加载中 |
| 设备已连接，识别暂停 | 保持 | 不运行 | 不运行 |
| 自动监听中 | 保持 | 持续运行 Stage1 + Stage2 | 仅在 Stage2 判定为近场语音后启动 |
| 按住右 `Alt` | 保持 | 仍运行 | 在“语音识别已开启”时强制开始或保持当前会话 |
| 设备已断开 | 关闭 | 不运行 | 不运行 |

- “开启语音识别”实际是开启 **ProxiMic 自动监听**，并不是把全部声音持续交给 ASR。
- “暂停语音识别”会同时暂停 ProxiMic 和 ASR，但不会断开 Ring；恢复时不需要重新连接设备。
- 只有 BLE、NUS 服务和真实 PCM 音频都验证成功后，应用才会加载 ProxiMic 和 ASR 模型；连接失败不会白白等待模型加载。
- UI 显示“设备已连接”代表已经收到真实 PCM，不只是 BLE 底层报告建链成功。
- 音频流中断时 watchdog 会立即关闭并断开设备，不会重启麦克风或自动重连。排查设备后请点击“重新连接设备”，也可以选择其他设备。
- 主动点击“断开设备”会优先停止音频与 BLE；正在识别的未完成语句会被丢弃，不会在断开后继续输出文本。
- 设备与后台模型任务分别显示状态：BLE/MIC 释放后立即显示“设备已断开”；若第三方模型调用尚未返回，界面会继续提示“正在结束后台初始化”。连接或音频验证失败时也会自动断开，不需要再点一次断开按钮。
- 同一应用运行期间会保留最近使用的 ASR 模型。相同后端、模型、语言和 CPU/GPU 配置再次连接时直接复用，不再重复执行 Fun-ASR 的 remote code、网络构建和权重加载；切换相关配置或完全退出应用后，下次仍需加载一次。
- 首次验证真实 PCM 后，应用会暂时发送 `MIC OFF`，在保持 BLE/NUS 连接的情况下加载模型，避免 Fun-ASR/PyTorch 初始化影响音频回调。模型就绪后重新发送 `MIC ON`，必须收到一帧新的 PCM 才会进入识别并启用 watchdog。
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
| `.runtime/local-llm/` | llama.cpp 运行时和默认 GGUF 模型 | 否 |
| `.cache/modelscope/` | SenseVoice、Fun-ASR-Nano 等模型权重 | 否 |

`third_party/` 中有模型实现源码，不等于已经安装运行依赖，也不包含数 GB 的模型权重。
三者用途不同，并不是重复安装。

本地 LLM 的下载源、文件名、版本、模型别名、上下文长度和 SHA-256 由
`src/proximic_ring/assets/local_llm_catalog.json` 描述。`scripts/install-local-llm.ps1`
只消费该清单，不依赖 Qwen 文件名；以后增加其他 GGUF 时添加清单项并指定 `-ModelId`。
安装器会写入 `installation.json`，下次启动时应用自动读取新模型的路径、别名、上下文和
推理参数，ASR、提示词、UI 和 OpenAI-compatible HTTP 调用层都不需要修改。例如：

```powershell
.\scripts\install-local-llm.ps1 -ModelId "清单中的另一个模型ID"
```

受限网络可以在运行安装器前设置标准的 `HF_ENDPOINT`，或在清单的 `downloadUrls` 中增加
内部镜像；无论使用哪个下载源，文件都必须通过清单记录的同一个 SHA-256。

标准安装始终把模型下载到受管目录，不会扫描或引用用户机器上已有的模型。开发者明确要
导入现有 GGUF 时，才可以主动传入以下可选参数：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 `
  -ExistingLocalModelPath "E:\ModelDownloads\Qwen3\Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
```

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
