# ProxiMic Voice 客户界面

桌面应用采用 PySide6 + Qt Quick/QML。CLI 继续用于调试和实验，UI 直接调用同一套
Ring、ProxiMic、ASR、按住说话和文本注入模块，不启动或解析额外的终端进程。

## 安装

在已经激活的项目环境中安装 UI 和运行依赖：

```powershell
python -m pip install -e ".[ring,asr-streaming-sensevoice,ui]"
```

如果需要在 UI 中使用 Fun-ASR-Nano，再安装其模型依赖：

```powershell
python -m pip install -e ".[asr-funasr-nano]"
```

## 启动

```powershell
proximic-ring-ui
```

也可以从源码入口启动：

```powershell
python -m proximic_ring.ui
```

首次启动时检查以下设置：

- Ring 设备名称，默认 `Ringo`。
- ProxiMic 模型路径和 Stage1 threshold，当前训练模型建议从 `0.005` 开始。
- ASR 后端、模型和运行设备。
- `streaming-sensevoice` 默认使用 `third_party/streaming-sensevoice` submodule。
- 选择 `funasr_nano` 时默认使用 `third_party/Fun-ASR`；目录必须包含 `model.py`。ASR 模型
  留空时会优先使用 `pretrained_models/Fun-ASR-Nano-2512` 本地 checkpoint。
- 是否启用最终文本注入和 `Ctrl+Alt+Space` 按住说话。

先点击“连接设备”，模型与 Ring 只初始化一次。设备连接后，“开启/暂停语音识别”只
控制检测与 ASR，不会断开 Ring；需要释放设备时单独点击“断开设备”。设备保持连接时
设置会暂时锁定，断开后可以修改。

最终识别结果会逐段累积到可选择、复制和编辑的文本框中。若焦点位于 ProxiMic 自己的
窗口，桌面注入会自动跳过，避免同一段文本在编辑器中重复；焦点位于其他应用时仍会将
最终文本注入当前输入框。

关闭主窗口会退出程序并释放设备，不再隐藏为不可见的后台进程。终端中的 `Ctrl+C` 也
会触发同一退出流程；若设备清理异常，最多等待约 5 秒。

## 当前范围

- 已有：设备/识别分离控制、模型配置、状态展示、实时字幕、可编辑文本、日志、系统
  托盘、配置持久化、桌面输入和按住说话。
- 尚未加入：文件选择器、可编辑快捷键、历史记录、润色/扩写、自动升级和安装包。

配置通过 Qt `QSettings` 保存。QML 只负责展示和用户交互；识别链路继续运行在后台
Python 线程，所有 UI 更新通过 Qt queued signal 回到主线程。
