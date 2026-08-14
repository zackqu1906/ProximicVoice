# Proximic Voice

Proximic Voice 是面向 Ringo 可穿戴设备的近场实时语音输入工具。它使用 ProxiMic
两阶段模型判断用户是否靠近说话，把音频实时发送给可替换的 ASR 后端，并可将最终
文本注入 Windows 中当前获得焦点的输入框。

当前版本适合课程演示、算法验证和后续产品化开发。

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

- Windows 10/11。
- Python 3.11（64 位）。
- 支持蓝牙的电脑和 Ringo 设备。
- 首次下载 ASR 模型时需要联网。
- CPU 可以完成首次验证；有兼容 CUDA 环境时可在 UI 中改为 `cuda:0`。

Fun-ASR-Nano 的模型权重约 2 GB，不包含在 Git 仓库中。第一次使用该后端时会由
模型库下载到本机缓存。

## 最快安装方式

```powershell
git clone <你的仓库地址>
cd ProximicVoice
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

安装完成后启动：

```powershell
.\scripts\start-ui.cmd
```

也可以在已激活的环境中直接运行：

```powershell
python -m proximic_ring.ui
```

## 界面使用

1. 检查 Ring 名称、ProxiMic 模型、Stage1 threshold 和 ASR 后端。
2. 点击“连接设备”，加载模型并建立 Ring 连接。
3. 点击“开启语音识别”开始自动近场识别。
4. “暂停语音识别”只暂停检测和 ASR，Ring 仍保持连接。
5. 需要释放硬件时单独点击“断开设备”。
6. 关闭主窗口会退出程序；终端中的 `Ctrl+C` 也会触发退出。

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

UI 会自动使用这两个目录，不需要老师另行克隆。第三方模型权重不会提交到本仓库，来源和许可证说明见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

## 手动安装

如果不使用脚本：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[ring,asr-streaming-sensevoice,asr-funasr-nano,ui]"
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
  GitHub Private 仓库进行课程评审。

更详细的使用说明见 [`docs/CUSTOMER_UI.md`](docs/CUSTOMER_UI.md) 和
[`docs/ASR_BACKENDS.md`](docs/ASR_BACKENDS.md)。
