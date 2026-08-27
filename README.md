# ProxiMic Voice

桌面安装包发布在 [GitHub Releases](https://github.com/zackqu1906/ProximicVoice/releases)。
Windows 用户下载 `.exe`，Apple Silicon macOS 用户下载 `.dmg`；首次使用本地模型时
应用会按需下载模型文件。

ProxiMic Voice 是面向 Ringo 可穿戴设备的近场语音输入与语音编辑桌面应用。
它持续接收 Ring 麦克风音频，用 ProxiMic 两阶段模型判断“是否有人贴近设备说话”，
只把命中的语音片段交给 ASR，并在 Windows 中完成跨应用听写、文本修改和确认写回。

项目同时保留了完整的命令行、数据采集、模型训练和多 ASR 对比能力，既可以作为桌面产品使用，
也可以作为近场、低声和耳语识别实验平台继续开发。

## 下载、生成并安装桌面安装包

仓库源码 ZIP 和桌面安装包是两种不同的文件：

- 只想安装使用：进入 [GitHub Releases](https://github.com/zackqu1906/ProximicVoice/releases)，
  Windows 下载 `*-windows-x64-setup.exe`，Apple Silicon macOS 下载 `*-macos-arm64.dmg`。
- 从仓库首页选择 **Code → Download ZIP**：下载的是源码，不包含 `dist/` 安装包目录；
  解压后需要按下面的步骤在对应系统上生成安装包。

安装包不包含 ASR 权重和约 2.5 GB 的本地文本模型。相应功能首次使用时会联网下载，
以后从用户缓存目录复用。

### Windows 10/11 x64：从源码生成 `.exe`

1. 解压源码 ZIP，安装 [Inno Setup 6](https://jrsoftware.org/isdl.php)。
2. 在解压后的项目根目录打开 PowerShell。
3. 首次构建时执行：

```powershell
cd C:\你的路径\ProximicVoice
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Compute cpu -SkipLocalLLM
powershell -ExecutionPolicy Bypass -File .\scripts\build-windows-installer.ps1
```

构建完成后，安装包位于：

```text
dist\installer\ProximicVoice-0.6.0-windows-x64-setup.exe
```

双击 `.exe`，按安装向导完成安装。当前 Demo 尚未使用商业代码签名证书，SmartScreen
可能显示“发布者未知”；确认文件来自本仓库后，可选择 **更多信息 → 仍要运行**。

后续只修改 Python、QML、提示词或其他业务代码时，关闭正在运行的 ProxiMic Voice，
然后使用快速重建命令：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-windows-installer.ps1 -SkipDependencyInstall
```

新的安装包会覆盖 `dist\installer\` 中的同名文件。如果修改了 `pyproject.toml`、
依赖锁文件或新增依赖，应先重新执行 `setup.ps1 -Compute cpu -SkipLocalLLM`，再构建安装包。

### Apple Silicon macOS 12+：从源码生成 `.dmg`

当前只提供 Apple Silicon（M1/M2/M3/M4 等 arm64）构建，不支持 Intel Mac。
构建机需要联网，并安装 Xcode Command Line Tools、Homebrew 和 Python 3.11：

```bash
xcode-select --install
brew install python@3.11
```

解压源码 ZIP，在“终端”进入项目根目录并执行：

```bash
cd /你的路径/ProximicVoice
bash ./scripts/build-macos.sh
```

构建完成后可找到：

```text
dist/ProximicVoice-0.6.0-macos-arm64.dmg
```

双击 `.dmg`，把 **Proximic Voice.app** 拖入 **Applications**。当前 Demo 使用 ad-hoc
签名；如果 macOS 提示无法验证开发者，请在 Finder 中按住 Control 点击应用并选择
**打开**，或进入 **系统设置 → 隐私与安全性 → 仍要打开**。首次启动时允许蓝牙和麦克风权限。

如果应用图标出现后立即退出，新版本会弹出启动错误并把完整诊断写到：

```text
~/Library/Application Support/ProxiMic Voice/logs/startup.log
```

也可以在“终端”直接启动以复现，并把上述日志发给开发者：

```bash
"/Applications/Proximic Voice.app/Contents/MacOS/ProximicVoice"
```

macOS 可以运行 Ring、ProxiMic、ASR 和桌面 UI；Windows 专用的全局快捷键、跨应用
文本读取和写回目前不在 macOS 上提供。

## 当前可以做什么

- 发现并连接 Ringo BLE 设备，验证 NUS 服务和真实麦克风 PCM 数据。
- 使用 ProxiMic Stage1 + CNN Stage2 检测近场说话，不把全部环境声音持续发送给 ASR。
- 在桌面界面中选择三种 ASR：
  - `streaming_sensevoice`：本地 Streaming SenseVoice。
  - `funasr_nano`：本地 Fun-ASR-Nano-2512，支持用户热词。
  - `volcengine`：在线豆包 Seed-ASR 流式识别。
- 实时显示 ASR partial，结束后使用 final 结果进入文本处理。
- ASR 直接使用 Controller 裁剪后的原始 16 kHz 音频，不额外放大。
- 提供“输入到光标”和“修改当前文本”两种工作模式。
- 使用本地 Qwen3-4B-Instruct-2507 或火山方舟上的豆包/DeepSeek 处理文本。
- 修改时向所选模型并行发送片段替换和完整文本两套 prompt，采用最先通过校验的结果。
- 片段协议由 Python 完成单处或全量匹配替换；完整文本协议直接生成完整候选。
- 修改结果先等待确认，`Enter` 应用、`Esc` 取消、右 `Alt` 重新说。
- 明确的“删除全文”会提示即将清空文本框，确认后才执行。
- Windows 使用 Unicode 键盘注入，不用剪贴板写入最终文本。
- 保存 Ring 麦克风连续 WAV，便于回听和后续 ASR 评测。
- 提供 CLI，用同一段音频并行比较多个 ASR，或采集、训练新的 ProxiMic 模型。

## 整体流程

```text
Ringo BLE 麦克风
        │
        ├── 保存连续录音：data/session/<时间>/ring_audio.wav
        │
        ▼
16 kHz / 单声道 PCM
        │
        ▼
ProxiMic Stage1 + Stage2
        │  只放行近场会话
        ▼
ASR partial / final
        │
        ├── 输入到光标
        │      ASR final →（可选：听写 prompt → LLM）→ Unicode 注入
        │
        └── 修改当前文本
               锁定外部文本框并读取全文
               → ASR 修改指令
               → 并行竞速两套 edit prompt + tool schema
               ├── 片段：original_text + modified_text → Python 替换
               └── 全文：modified_text → 完整候选
               → 采用最先返回且通过校验的结果
               → 较慢分支在后台完成并补写采集记录
               → Enter / Esc 确认
               → 写回原文本框
```

Ring、ProxiMic、ASR、LLM 和桌面写入彼此解耦。更换 ASR 或 LLM 不需要修改检测模型；
修改链路会让同一个已选模型并行尝试片段和全文协议，谁先返回有效结果就使用谁的候选。

## 当前边界

- 完整的跨应用输入、文本读取、全局快捷键和修改确认目前只支持 Windows。
- macOS 可以运行 Ring、ProxiMic、ASR 和桌面 UI，但不支持 Windows 全局快捷键与跨应用文字注入。
- 当前没有内置降噪；Ring PCM 会直接进入 ProxiMic 和 ASR。
- Fun-ASR 热词属于识别提示，不是强制字典。过多、过短或常见的热词可能错误吸附无关语音。
- 当前保存的是每次 Ring 麦克风连接期间的连续 WAV，不会自动把每条 ASR 语句单独导出并标注。
- 项目重点包含低声/耳语场景，但通用 ASR 对耳语的准确率仍需要用真实 Ring 数据评测和优化。

## Windows 快速开始

### 1. 安装

```powershell
git clone <你的仓库地址>
cd ProximicVoice
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

安装器会询问 CPU 或 NVIDIA GPU 模式，并完成以下工作：

- 下载并校验项目专用 CPython 3.11.9。
- 创建 `.runtime/venv/`，不依赖系统 Python、Conda 或 Anaconda。
- 安装锁定版本的 PyTorch、PySide6、Ring 和 ASR 依赖。
- 下载 llama.cpp Windows CPU 运行时。
- 下载约 2.5 GB 的 `Qwen3-4B-Instruct-2507-Q4_K_M.gguf`。
- 导入关键原生库并加载 UI，全部通过后才报告安装完成。

无人值守安装可以明确指定计算设备：

```powershell
# 通用 CPU
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Compute cpu

# NVIDIA GPU / CUDA 12.8
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Compute cuda
```

只做 ASR 开发、不安装本地 LLM：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -SkipLocalLLM
```

本地 LLM 默认位于 `.runtime/local-llm/`。磁盘空间不足时，可以在首次安装前指定其他位置：

```powershell
$env:PROXIMIC_LLM_HOME = "D:\ProximicVoiceModels"
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

### 2. 启动

```powershell
.\scripts\start-ui.cmd
```

### 3. 使用

1. 在设置区选择 ASR 后端、语言和 CPU/GPU。
2. 点击“选择并连接设备”，在实时扫描列表中选择目标 Ringo。
3. 应用会依次完成 BLE 连接、ProxiMic/ASR 加载，再开启 Opus 音频并验证解码后的 PCM。
4. 点击“开启语音识别”。这会启动自动近场监听，不会断开或重连 Ring。
5. 把光标放进其他应用的文本框。
6. 使用 `Alt+1` 选择“输入到光标”，或使用 `Alt+2` 选择“修改当前文本”。
7. 修改预览被重说或取消后，可在右侧提示出现的 10 秒内按 `Alt+A` 标记
   语音识别错误、`Alt+L` 标记大模型理解错误，或按 `Alt+O` 标记其他原因；不选择
   就不会给该 Attempt 添加原因标签。
8. 输入模式可通过按钮选择是否再由文本 LLM 整理；按住右 `Alt` 说话，松开后等待处理完成。
9. 修改模式出现确认状态后，按 `Enter` 应用或按 `Esc` 取消。

如果大模型没有返回可用的编辑结果，悬浮窗会持续显示具体错误，只允许取消或重说，
不会修改原文本。重说会继续当前 Episode，并自动把失败 Attempt 标记为
`llm_error`；下一次成功确认后再结束整个 Episode。

“暂停语音识别”只暂停 ProxiMic 和 ASR，Ring 仍保持连接；“断开设备”才会释放麦克风和 BLE。

## 两种文本模式

### 输入到光标

输入模式默认会把 ASR final 交给所选文本大模型进行听写整理，再注入说话开始时锁定的外部输入框。
点击“输入模式 LLM 整理”按钮将其关闭后，ASR final 会直接注入，不再进入文本模型队列；这尤其适合
本身已经具备文本生成与整理能力的 Fun-ASR-Nano。

如果大模型不可用，输入链路会保留 ASR 原文作为回退，不让语音输入完全失效。
需要观察纯 ASR 输出时也可以直接关闭该按钮。修改模式不受此按钮影响，始终使用文本 LLM。

### 修改当前文本

程序会读取当前外部文本框的完整内容，最多接受 5000 个字符，再把下一段语音作为修改要求。

桌面应用会并行竞速以下两套协议。片段替换协议为：

```json
{
  "original_text": "需要修改的位置附近文本",
  "modified_text": "修改后的对应片段"
}
```

`original_text` 必须逐字来自待修改文本。模型希望修改全部重复项时返回重复的短片段，Python
会替换所有匹配；只修改其中一处时，模型加入足够上下文使片段唯一。整体改写或相距很远的
多处修改可以使用全文片段。

完整文本协议为：

```json
{
  "modified_text": "修改后的完整文本"
}
```

完整文本协议不需要 `original_text`，因为其结果直接作为完整候选，不执行片段定位。

安全策略：

- 所有编辑请求必须真正调用 `submit_text_edit`，普通 content JSON 不算成功。
- 默认 function arguments 可以是 `dict` 或 JSON 字符串，但只能包含片段协议的两个字段。
- `original_text` 不存在时校验器拒绝结果并自动重试一次；出现多次时视为模型选择替换全部。
- 任意编辑失败会自动重试一次，并把每次 arguments 记录到测试输出。
- 两套协议同时请求；一路失败时继续等待另一路，两路都失败才报告本次修改失败。
- 任一路先通过校验就立即显示候选；另一条分支只在后台补全采集记录，不阻塞 UI。
- 候选生成后不会立刻覆盖外部文本，必须由用户确认。
- 片段协议只有选中完整原文并返回空片段时才会清空；全文协议返回空 `modified_text` 时也会进入清空确认。

## 文本大模型

桌面 UI 提供以下选择：

| 来源 | 默认模型 | 接口 | 说明 |
| --- | --- | --- | --- |
| 本地 | `Qwen3-4B-Instruct-2507` Q4_K_M | llama.cpp `/chat/completions` | Instruct 模式、thinking 关闭、强制本地 tool calling |
| 火山方舟 | `doubao-seed-2-0-lite-260215` | 方舟 `/responses` | 显式关闭 thinking，支持 function tools |
| 火山方舟 | `deepseek-v4-flash-260425` | 方舟 `/responses` | 与豆包复用相同 prompt、schema 和校验器 |

本地模型在 UI 首帧出现后自动启动并预热固定 prompt。模型加载完成只表示权重已进入内存，
实际生成速度仍取决于 CPU/GPU、输出 token 数和是否触发重试。
本地 llama-server 使用两个并发 slot 运行竞速请求；云端会同时发出两次 API 请求，因此编辑模式
通常会消耗两次请求的推理资源，即使较慢一路的结果最终被忽略。

火山方舟 API Key 只从环境变量读取，不写入 QSettings：

```powershell
$env:ARK_API_KEY = "<your-ark-api-key>"
.\scripts\start-ui.cmd
```

UI 默认方舟地址为 `https://ark.cn-beijing.volces.com/api/v3`。模型 ID 可以在高级配置中修改，
但必须是当前方舟账号已经开通的模型。

## ASR 后端

### 桌面 UI 后端

| 后端 | 本地/在线 | 实时方式 | 默认模型 | 备注 |
| --- | --- | --- | --- | --- |
| `streaming_sensevoice` | 本地 | 流式/结束重解码 | `iic/SenseVoiceSmall` | 默认 UI 后端 |
| `funasr_nano` | 本地 | 累积伪流式，默认每 720ms 重解码 | `Fun-ASR-Nano-2512` | 支持 UI 热词和 final 重解码 |
| `volcengine` | 在线 | 原生 WebSocket，默认 200ms PCM 包 | Seed-ASR | 网络请求不阻塞 Ring 接收线程 |

Fun-ASR-Nano 第一次使用时会下载模型权重到项目缓存；以后启动仍需要从磁盘加载到内存，
但不会重复下载完整模型。

使用在线 Seed-ASR 前设置独立的语音服务 Key：

```powershell
$env:VOLC_ASR_API_KEY = "<your-doubao-speech-app-key>"
.\scripts\start-ui.cmd
```

`VOLC_ASR_API_KEY` 属于豆包语音识别服务，与文本大模型使用的 `ARK_API_KEY` 不是同一个 Key。

### Fun-ASR 热词

选择 `funasr_nano` 后，可以在设置区按行填写识别热词。程序也接受中文/英文逗号和分号，
保存时会自动去除空项并去重，重新连接后生效。

建议只加入人名、品牌名和专业名词等少量强相关词。不要把大量常见词、单字或所有编辑动词
长期作为全局热词，否则模型可能把声学上不确定的内容错误吸附到热词。

### CLI 额外后端

命令行还保留以下开发适配器：

- `sensevoice`：批量 SenseVoice。
- `whisper`：本地 faster-whisper。
- `http`：上传 multipart WAV、读取 JSON 文本的通用远端适配器。

列出当前可发现的后端：

```powershell
.\.runtime\venv\Scripts\python.exe -m proximic_ring asr-backends
```

详细参数和新增后端方法见 [docs/ASR_BACKENDS.md](docs/ASR_BACKENDS.md)。

## 音频编码与录音保存

桌面产品默认使用 `Opus` 传输，降低 Windows BLE 链路负载；SDK 解码后仍向模型提供
16 kHz、单声道 PCM。采集训练集时可显式选择 `PCM` 保留原始波形分布。

| 编码 | 特点 |
| --- | --- |
| PCM | 适合训练数据采集；质量和模型一致，BLE 带宽占用最高 |
| ADPCM | 带宽较低，但有损压缩可能改变 Stage2 分数和 ASR 输入 |
| Opus | 桌面产品默认；带宽更低，需要系统存在可用的 libopus 运行库 |

SDK 会保存每次麦克风会话的解码后连续录音：

```text
data/session/20260820_154312/
├── ring_audio.wav
├── ring_button.csv
└── ring_raise_to_wake.csv
```

`ring_audio.wav` 是 16 kHz、单声道 PCM WAV。目录名是会话开始时间。当前不会按每次
`[ASR] START → END` 自动生成独立语句 WAV。

## macOS（Apple Silicon）

macOS 使用项目内独立 Python 环境和缓存：

```bash
brew install python@3.11
./scripts/setup-macos.sh
./scripts/start-ui.sh
```

也可以指定 Python 3.11：

```bash
PROXIMIC_PYTHON=/opt/homebrew/bin/python3.11 ./scripts/setup-macos.sh
```

首次连接 Ring 时，需要允许 Terminal 或 Python 使用蓝牙。当前 macOS 路径使用 CPU，
支持 Ring、ProxiMic、ASR 和界面验证，但不提供 Windows 的右 Alt 全局控制、外部文本读取和注入。
Windows 默认本地 LLM 包中的 `llama-server.exe` 也不能直接用于 macOS。

## 命令行与实验

CLI 入口：

```powershell
.\.runtime\venv\Scripts\python.exe -m proximic_ring --help
```

可用命令：

| 命令 | 作用 |
| --- | --- |
| `ring` | 读取 Ringo 实时音频 |
| `record` | 仅录制 Ring 连续 WAV，不加载近点模型、ASR 或 LLM |
| `wav` | 重放 16 kHz PCM16 WAV |
| `mic` | 使用普通系统麦克风做基线 |
| `asr-backends` | 列出 ASR 适配器 |
| `collect` | 采集 near/far/artifact 数据集 |
| `train` | 训练新的近场二分类模型 |

只录制一段 Ring 麦克风音频：

```powershell
.\.runtime\venv\Scripts\python.exe -m proximic_ring record --duration 20
```

录音会保存到 `data/session/<时间>/ring_audio.wav`。这条路径不会构建 ProxiMic、ASR 或 LLM。

同一段 Ring 音频可以同时送给多个 ASR：

```powershell
.\.runtime\venv\Scripts\python.exe -m proximic_ring ring `
  --model .\src\proximic_ring\assets\ringo-near-v1.model `
  --stage1-threshold 0.005 `
  --asr streaming_sensevoice `
  --asr volcengine `
  --asr-model streaming_sensevoice=iic/SenseVoiceSmall `
  --asr-model volcengine=seedasr-streaming `
  --streaming-sensevoice-repo .\third_party\streaming-sensevoice `
  --asr-language zh
```

跳过 ProxiMic、直接比较原始 Ring 音频的 ASR 基线：

```powershell
.\.runtime\venv\Scripts\python.exe -m proximic_ring ring `
  --disable-proximic-detector `
  --asr funasr_nano `
  --funasr-nano-repo .\third_party\Fun-ASR `
  --asr-device cuda:0 `
  --asr-language zh
```

数据采集、训练和验证说明见：

- [docs/DATASET_TRAINING.md](docs/DATASET_TRAINING.md)
- [docs/VALIDATION.md](docs/VALIDATION.md)
- [docs/ASR_INTEGRATION.md](docs/ASR_INTEGRATION.md)

## 独立测试 LLM

不连接 Ring 和 ASR，也可以测试片段替换与完整文本两套 prompt、tool schema 和结果校验：

```powershell
.\.runtime\venv\Scripts\python.exe .\tools\test_llm.py
```

交互模式可选择本地 Qwen、豆包、DeepSeek 或三模型同时比较。编辑测试会让每个模型并行运行
片段替换和完整文本协议，标出竞速胜者；选择三模型时共运行 6 次。所有 arguments、最终文本
和耗时都会集中展示。

单次三模型编辑比较：

```powershell
$env:ARK_API_KEY = "<your-ark-api-key>"
.\.runtime\venv\Scripts\python.exe .\tools\test_llm.py `
  --provider compare `
  --mode edit `
  --target-text "会议安排在周四。" `
  --text "把周四改成周五"
```

独立测试工具的本地默认超时是 180 秒；桌面 UI 的请求超时可以在设置中调整，范围为 1～300 秒。

## 开发安装

正式 Windows 安装应优先使用 `scripts/setup.ps1`。已有兼容 Python 3.11 环境的开发者也可以：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -c requirements-windows.lock -e ".[ring-opus,asr-streaming-sensevoice,asr-funasr-nano,asr-volcengine,ui,dev]"
python -m proximic_ring.ui
```

关键运行目录：

| 目录 | 内容 | 提交到 Git |
| --- | --- | --- |
| `third_party/` | 已验证的第三方 ASR 源码快照 | 是 |
| `.runtime/venv/` | 项目 Python 和依赖 | 否 |
| `.runtime/local-llm/` | llama.cpp 与默认 GGUF | 否 |
| `.cache/modelscope/` | ModelScope ASR 权重 | 否 |
| `.cache/huggingface/` | Hugging Face 缓存 | 否 |
| `data/session/` | Ring 连续录音和事件 CSV | 否 |

第三方源码、Python 依赖和模型权重是三类不同内容。`third_party/` 中存在模型实现，
不代表权重已经下载，也不代表运行依赖已经安装。

## 项目结构

```text
src/proximic_ring/
├── audio/                 Ring、WAV 和系统麦克风输入
├── asr/                   会话控制、worker 和可插拔 ASR 后端
├── assets/                ProxiMic 模型、DSP 参数和本地 LLM 清单
├── text_processing/       prompts、tool schema、LLM 调用和编辑结果校验
├── ui/                    PySide6 控制器和 Qt Quick/QML 界面
├── detector.py            ProxiMic 两阶段检测
├── desktop_target.py      Windows 外部文本读取与替换
├── desktop_output.py      Unicode 键盘注入
├── collect.py             数据采集
└── train.py               近场模型训练

src/ring_python_sdk/       当前集成的 Ringo SDK snapshot
third_party/               已验证的外部 ASR 源码快照
tools/                     LLM、增益、设备和现场诊断工具
tests/                     自动化测试
docs/                      详细设计、接入与验证文档
experiments/               可复现实验脚本和结果
scripts/                   安装、GPU 切换和启动脚本
```

## 测试

```powershell
.\.runtime\venv\Scripts\python.exe -m pytest
```

自动化测试覆盖：

- PCM 解码、ProxiMic 时序和会话边界。
- ASR 解耦、缓存、流式更新与火山 WebSocket 协议。
- 本地 LLM 安装清单、tool calling、片段替换与完整文本结果校验。
- Windows 文本注入、目标替换和合法清空。
- UI 设置、热词和完整听写/修改确认流程。

硬件、真实模型下载、GPU 和在线服务仍需要在目标机器上单独验证。

## 常见问题

### 首次启动为什么慢？

首次使用 ASR 可能需要下载权重。以后不会重新下载完整模型，但每次全新进程仍要把权重从磁盘
加载到内存。本地 Qwen 会在 UI 出现后预热；CPU 生成速度明显慢于云端模型和 GPU。

### 日志显示 BLE 已连接，为什么 UI 还在连接？

BLE/NUS 建链完成后 UI 会显示设备已连接；模型加载完成并收到真实 PCM 后才会进入“准备就绪”。

### 热词越多越好吗？

不是。热词会提高相关词的先验，也可能把不确定音频吸附为热词。优先使用少量、长且明确的专有词。

### 为什么修改没有立即写回？

这是安全设计。LLM 先生成完整候选，程序校验后等待用户确认，才重新激活原文本框并覆盖全文。

### 为什么“删除全文”得到 0 个字符？

合法删除全文的候选就是空字符串。程序会进入明确的清空确认状态；只有按 `Enter` 后才会执行。

### 安装损坏怎么重建？

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Recreate
```

## 隐私、密钥与发布

- `data/`、`datasets/`、训练输出、缓存和模型权重已通过 `.gitignore` 排除。
- 不要提交录音、API Key、`.env` 或带个人信息的日志。
- API Key 只从环境变量读取，界面只保存环境变量名称。
- 本地 LLM 的文件、来源、上下文、推理模式和 SHA-256 记录在
  `src/proximic_ring/assets/local_llm_catalog.json`。
- 第三方来源和许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 当前仓库尚未为第一方代码选择公开许可证；公开发布前需要由项目所有者确认。
- `src/ring_python_sdk/` 的再分发权限需要由设备/SDK 提供方确认。

## 延伸文档

- [桌面界面使用说明](docs/CUSTOMER_UI.md)
- [ASR 后端与对比方法](docs/ASR_BACKENDS.md)
- [ASR 接入架构](docs/ASR_INTEGRATION.md)
- [用户修改数据采集](docs/MODIFICATION_DATASET.md)
- [Windows/macOS 安装包构建](docs/PACKAGING.md)
- [桌面文本输入设计](docs/DESKTOP_INPUT.md)
- [Ring 接入说明](docs/RING_INTEGRATION.md)
- [数据采集与训练](docs/DATASET_TRAINING.md)
- [验证清单](docs/VALIDATION.md)
- [移植说明](docs/PORTING_NOTES.md)
