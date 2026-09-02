# Proximic Voice macOS 实机验证与修复清单

本文档用于在真实 Apple Silicon Mac 上验证并修复 Proximic Voice。Windows 上的测试只能排除通用代码回归，不能替代 CoreBluetooth、应用包、权限、Qt Cocoa 平台插件及原生动态库的实机验证。

## Agent 工作约束

- 不上传 GitHub，不创建 Pull Request，不发布 Release。
- 除非用户明确要求，不构建 DMG 或安装包。
- 先复现、保存证据，再修改代码；一次尽量只修复一类问题。
- 不删除用户数据、模型缓存或系统权限记录。需要清理时先说明影响并取得用户同意。
- 保留工作区中已有修改，不覆盖无关改动。
- 每次修改后至少运行相关测试；能运行完整测试集时再运行完整测试集。
- 最终报告必须列出：复现步骤、根因、修改文件、验证结果、仍未验证的项目。

## 1. 记录测试环境

先记录以下信息，后续日志必须能对应到同一台机器和同一份代码：

- [ ] Mac 型号与芯片：Apple Silicon 型号（M1/M2/M3/M4 等）。
- [ ] macOS 版本。
- [ ] Python 版本和架构。
- [ ] PySide6、PyInstaller、torch、torchaudio、bleak、funasr 版本。
- [ ] 当前 Git commit，以及工作区是否存在未提交修改。
- [ ] 测试对象是源码运行还是 `/Applications/Proximic Voice.app`。

建议执行：

```bash
uname -a
sw_vers
uname -m
python3 --version
python3 -c 'import platform; print(platform.platform(), platform.machine())'
python3 -c 'import PySide6, torch, torchaudio, bleak, funasr; print("PySide6", PySide6.__version__); print("torch", torch.__version__); print("torchaudio", torchaudio.__version__); print("bleak", bleak.__version__ if hasattr(bleak, "__version__") else "unknown"); print("funasr", funasr.__version__)'
git status --short
git rev-parse HEAD
```

如果命令失败，不要立刻安装或升级依赖；先记录缺失项，并检查项目已有的 macOS 安装说明和锁定版本。

## 2. 先区分源码问题与应用包问题

同一个场景分别记录“源码运行”和“已安装应用”的结果：

| 场景 | 源码运行 | 已安装 `.app` | 判断方向 |
|---|---|---|---|
| 两边都失败 | 待填写 | 待填写 | 业务逻辑、平台 API、线程或权限问题 |
| 仅 `.app` 失败 | 待填写 | 待填写 | PyInstaller 收集、资源路径、动态库、签名或沙盒式权限问题 |
| 仅源码失败 | 待填写 | 待填写 | Python 环境、依赖版本或工作目录问题 |

不要只双击 `.app` 后根据一个报错修改业务逻辑。首先确认同一功能在源码模式是否正常。

## 3. 启动与单实例检查

### 检查步骤

- [ ] 启动后只出现一个主窗口。
- [ ] Dock 中只出现一个应用实例。
- [ ] 打开设备选择器、扫描和连接设备时，不出现第二个相同主窗口。
- [ ] 加载 ASR 模型时，不出现第二个相同主窗口。
- [ ] 关闭主窗口后进程能够退出，不残留后台实例。
- [ ] 再次启动应用能够正常显示主窗口。

观察进程：

```bash
pgrep -afil 'ProximicVoice|Proximic Voice'
```

### 如果出现第二个主窗口

收集以下证据：

- 第二个窗口出现的精确时机：扫描开始、连接成功、模型加载还是开始识别。
- 出现前后两次 `pgrep` 输出。
- 启动日志完整内容。
- 每个相关进程的父进程：

```bash
ps -axo pid,ppid,lstart,command | grep -E 'ProximicVoice|Proximic Voice' | grep -v grep
```

重点检查：

- `packaging/launcher.py` 是否在导入 UI/模型前调用 `multiprocessing.freeze_support()`。
- 子进程是否重新进入 `proximic_ring.ui.main.main()`。
- 是否有第三方库在连接或加载模型时使用 `multiprocessing`/`spawn`。
- 不要仅靠隐藏第二个窗口掩盖子进程入口错误；否则真正的工作子进程可能被一起终止。

## 4. 启动日志与 UI 日志

### UI 日志验收

- [ ] 应用打开后，“运行日志”不一直停留在“尚未启动”。
- [ ] 点击扫描后立即显示“开始扫描附近的蓝牙设备”。
- [ ] 选择设备后显示连接、验证、模型加载和模型就绪信息。
- [ ] 日志可以显示中文、英文、路径和异常信息。
- [ ] 日志区域会滚动到最新一行。
- [ ] 清空按钮有效，清空后新日志仍能继续出现。
- [ ] 窗口缩放后日志不消失、不被裁掉、不产生异常的横向宽度。

重点确认 QML 中 `logArea.text` 与 `appController.logText` 一致。若控制器已有日志但界面没有显示，检查 QML binding/`Connections`、Qt 主线程事件循环和控件尺寸；若两者都没有日志，继续向上检查运行时回调是否仍在使用 `print()`。

### 持久化启动日志

默认位置：

```text
~/Library/Application Support/ProxiMic Voice/logs/startup.log
```

检查：

- [ ] 文件可以创建并持续追加。
- [ ] 包含 `runtime environment ready`、`UI modules imported` 和 `QML root window ready`。
- [ ] 崩溃或启动失败时包含完整 traceback，而不只是最后一行错误。
- [ ] 日志路径不指向应用包内的只读目录。

查看最近内容：

```bash
tail -n 300 "$HOME/Library/Application Support/ProxiMic Voice/logs/startup.log"
```

## 5. 应用包资源路径

仅在验证已安装 `.app` 时检查：

- [ ] `funasr/version.txt` 存在于应用实际运行所使用的资源目录。
- [ ] `proximic_ring` 的 QML、SVG、检测模型等数据文件存在。
- [ ] `third_party/streaming-sensevoice` 存在。
- [ ] `third_party/Fun-ASR/model.py`、`ctc.py` 和 `tools` 存在。
- [ ] Opus 动态库存在，且架构为 `arm64`。
- [ ] 运行时资源解析使用 `resource_root()`，可写数据使用 `app_data_root()`；不能把模型缓存或日志写入 `.app/Contents`。

建议检查：

```bash
find '/Applications/Proximic Voice.app/Contents' -path '*funasr/version.txt' -print
find '/Applications/Proximic Voice.app/Contents' -path '*third_party/Fun-ASR/model.py' -print
find '/Applications/Proximic Voice.app/Contents' -path '*third_party/streaming-sensevoice*' -print | head
find '/Applications/Proximic Voice.app/Contents' -iname '*opus*.dylib' -print
```

对找到的动态库执行 `file <路径>`，确认不是仅有 `x86_64` 架构。

## 6. macOS 权限

在“系统设置 → 隐私与安全性”中记录：

- [ ] 蓝牙权限中存在 Proximic Voice，且已允许。
- [ ] 麦克风权限状态已记录。虽然音频来自 Ring，仍需确认应用实际调用链是否触发该权限。
- [ ] 辅助功能、输入监控权限状态已记录。

macOS 通过 Quartz 键盘事件听写或修改当前文本框，需要在“系统设置 → 隐私与安全性 → 辅助功能”中允许 Proximic Voice；源码启动时允许 Terminal/Python。编辑预览支持全局 `Enter` 确认和 `Esc` 取消；右 Alt 按住说话仍是 Windows 专用能力。

如果权限弹窗从未出现或拒绝后无法恢复，先记录 `Info.plist` 中的用途说明，不要直接重置整台机器的隐私数据库。

## 7. 蓝牙扫描

### 基本流程

- [ ] 打开设备列表后开始扫描。
- [ ] 能发现 Ringo，并显示名称、CoreBluetooth identifier 和 RSSI。
- [ ] 清空搜索词后能看到其他附近 BLE 设备。
- [ ] 默认搜索词只过滤显示，不影响底层发现。
- [ ] 扫描过程中取消选择器不会导致主界面状态错乱。
- [ ] 手动重新扫描可以再次发现设备。
- [ ] 未发现设备和蓝牙权限失败时，UI 给出可理解的错误。

### 重点排查

- macOS 的 BLE identifier 通常不是 Windows MAC 地址，不能使用 Windows 地址格式假设。
- `BLEDevice` 对象可能属于创建它的 asyncio event loop；不要跨扫描线程/loop 直接复用。
- 连接流程应使用持久化 identifier 在长生命周期运行 loop 中重新解析设备。
- 不要为了“实时刷新”无间隔重复扫描；CoreBluetooth 可能产生取消、重入或权限相关错误。

## 8. 设备连接、断开和重连

每一步都记录 UI 状态、UI 日志和进程数量：

- [ ] 选择 Ringo 后设备选择器关闭。
- [ ] 连接阶段 UI 不冻结，窗口仍可移动和重绘。
- [ ] 连接成功后保持 MIC 关闭，直到模型加载完成。
- [ ] 模型加载完成后设备进入“准备就绪/识别暂停”状态。
- [ ] 连接成功不会创建第二个 UI。
- [ ] 点击断开后 Ring 麦克风和 BLE 连接都释放。
- [ ] 断开过程中不会永久卡在“正在断开”。
- [ ] 手动重新连接成功。
- [ ] 关闭并重启应用后，可以重新扫描和连接。
- [ ] Ring 关机或超出范围后，应用显示断开错误且不会无限自动重连。

至少重复“连接 → 断开 → 重连”三次，以发现 event loop、CoreBluetooth delegate 或资源未释放问题。

## 9. ASR 模型与识别流程

分别验证项目允许用户选择的后端，不要只测试默认后端。

### streaming_sensevoice

- [ ] 模型能够加载。
- [ ] UI 显示“正在加载语音模型”和“语音模型已就绪”。
- [ ] 开启识别后，靠近说话能够出现 partial/final 结果。
- [ ] 暂停识别后不再处理新语音，但设备保持连接。
- [ ] 再次开启后能够继续识别。

### funasr_nano

- [ ] 导入 `funasr` 时不再缺少 `version.txt`。
- [ ] `third_party/Fun-ASR` 路径能够找到 `model.py`。
- [ ] 模型能够加载并返回结果。
- [ ] 热词设置能够传入后端。
- [ ] 第二次连接可以复用已加载模型，不出现第二个主窗口。

### 通用稳定性

- [ ] 模型加载期间 UI 持续响应。
- [ ] ASR 异常会显示到 UI 日志。
- [ ] 连续识别 10 次不崩溃、不丢失后续日志。
- [ ] 连续传输至少 15 分钟；单个 Opus 块丢失时允许该块静音，但后续块必须继续解码，
  不能因等待缺失序号而永久停流。
- [ ] 音频流期间不启动周期性电量查询控制写入；连接时仍能读取一次电量。
- [ ] 识别中断开设备后，不继续向已关闭对象发送结果。

若仍出现“说几句话后断开”，立即保存以下两类证据，以区分 CoreBluetooth 物理断链和
仅 PCM 停流：

```bash
tail -n 300 "$HOME/Library/Application Support/ProxiMic Voice/logs/startup.log"
ls -lt "$HOME/Library/Logs/DiagnosticReports" | head
./.runtime/venv/bin/python tools/diagnose_macos_audio.py
```

日志中的 `Ring BLE disconnected unexpectedly` 表示底层连接确实断开；只有
`PCM STREAM STALLED` 则表示 BLE 仍连接、音频块重组或解码没有继续推进。
新版诊断日志还会输出两行 `[DISCONNECT]`：第一行给出编码、收到的音频时长、最后
PCM 间隔、RMS/峰值/静音/削波比例和 WAV 路径；第二行给出固件、MTU、MIC 包、缺失
分片、帧序号间隙及丢块计数。必须完整保留这两行。

识别结果不正确时，先运行上述诊断脚本并用输出的 `open <WAV路径>` 命令试听：

- WAV 已经断续、变速、失真或缺字：先查 BLE、Opus/ADPCM 解码、固件和麦克风增益。
- WAV 清晰完整但识别错误：再查 ASR 后端、语言、热词和语句起止裁剪。
- 断开时间紧跟耗时很高的 `[ASR TIMING]`：查 CPU 饱和导致的 CoreBluetooth 调度饥饿。
- WAV 在断开点整齐结束且日志明确 `physically lost`：不是 QML 日志或界面造成的断开。

## 10. UI 与 macOS 原生行为

- [ ] 中文字体完整，无方框或透明文字。
- [ ] 日志使用可用的等宽字体（macOS 默认应为 Menlo）。
- [ ] 浅色/深色系统外观下文字与背景仍有足够对比度。
- [ ] 主窗口、设备选择器和对话框尺寸正常。
- [ ] 把焦点切到外部文本框后，识别悬浮窗仍保持可见且不抢键盘焦点。
- [ ] 听写结果写入开始说话时锁定的外部文本框。
- [ ] 编辑模式能读取外部文本、显示预览，并可用 `Enter` 确认或 `Esc` 取消。
- [ ] Retina 缩放下没有重叠、裁切或模糊到不可读。
- [ ] 标题栏关闭按钮能够真正退出应用，或其行为与 UI 文案一致。
- [ ] Dock 图标、应用名称和菜单栏名称正确。
- [ ] 系统托盘/菜单栏图标不可用时，不影响主窗口退出。

建议至少测试两种窗口尺寸，并切换一次系统深色/浅色外观。

## 11. 退出和异常恢复

- [ ] 未连接时退出正常。
- [ ] 扫描中退出正常。
- [ ] 连接中退出正常。
- [ ] 模型加载中退出正常。
- [ ] 正在识别时退出正常。
- [ ] 退出后 `pgrep` 不再发现应用进程。
- [ ] 再次启动后设置仍能读取，但不会错误地宣称已经连接。
- [ ] 上次异常退出不会导致永久无法扫描或连接。

如果存在崩溃，同时检查：

```text
~/Library/Logs/DiagnosticReports/
```

只收集与 Proximic Voice、Python 或 Qt 相关且时间匹配的报告，避免混入其他应用日志。

## 12. 修改后的最低验证要求

每修复一个问题，至少完成：

- [ ] 原始复现步骤现在通过。
- [ ] 相邻生命周期步骤通过，例如修复连接后还要验证断开和重连。
- [ ] 新增一个能在 CI/Windows 上运行的回归测试，或说明为什么必须是 macOS 专用测试。
- [ ] 运行相关 pytest 文件。
- [ ] 运行 `git diff --check`。
- [ ] 检查 `git diff`，确认没有无关文件和生成物。
- [ ] 未上传 GitHub，未构建用户未要求的安装包。

## 13. 问题记录模板

每个问题单独填写：

```text
问题标题：
测试对象：源码 / 已安装 .app
Mac 与 macOS 版本：
代码 commit：
复现频率：必现 / 偶现（次数）

复现步骤：
1.
2.
3.

预期结果：
实际结果：
第二个窗口出现时的进程树：
UI 日志：
startup.log：
DiagnosticReports：

初步根因：
修改文件：
新增/修改测试：
实机复测结果：
仍未验证：
```

## 可直接交给 macOS Agent 的任务提示词

```text
请按照 docs/MACOS_VALIDATION_CHECKLIST.md 在这台真实 macOS 机器上验证 Proximic Voice。

先记录环境并复现，不要根据 Windows 行为猜测。优先检查：
1. UI 运行日志是否实时显示；
2. 连接设备或加载模型后是否出现第二个相同主窗口；
3. CoreBluetooth 扫描、连接、断开和重连；
4. streaming_sensevoice 与 funasr_nano 的加载和识别；
5. 退出后是否残留进程。

对发现的问题直接修改本地代码，并添加适当的回归测试。不要上传 GitHub，不要创建 PR，不要发布 Release。除非我明确要求，否则不要构建 DMG 或安装包。保留已有修改，不处理无关问题。最终逐项报告复现证据、根因、修改文件、测试结果以及仍需人工验证的项目。
```
