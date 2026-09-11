# 电脑端手势识别测试

Ring 传输 200 Hz 六轴 IMU，电脑运行 `GestureRecognizer` 完成预处理、分类和触发。
无需戒指固件包含 Swipe 模型。这个独立终端程序不启动麦克风、ASR、LLM，
不调用语音或数据关联按钮。它和下面主程序的操作映射相互独立。

## 主程序中的操作

重启新版主程序并连接戒指，会自动加载本地电脑端模型；无需另开测试程序。

| 手势 | 对应现有操作 | 原有按键 |
| --- | --- | --- |
| 左滑、下滑 | 收听或处理时取消；出现可撤销的应用结果时撤销 | `Esc` |
| 右滑、上滑 | 转换当前语句的处理类型 | 默认 `F8`，或设置中选择的转换键 |

手势和键盘分别进入同一套操作逻辑，不模拟键盘按键，也不共用按键的按下/重复状态。
右滑和上滑沿用转换键的可用条件，不改变下一句的自动分类或 ASR 模型选择。
当前没有可执行的操作、目标窗口不符合原有要求时，手势不执行动作。轻点和响指不映射。

主程序共用原有蓝牙连接接收 200 Hz IMU，在独立线程分类；语音处理暂时挂起时，
手势仍可用于取消、撤销或转换。启用手势后，语音数据集的 IMU 记录及元数据均为
200 Hz；没有手势回调的调用方仍沿用原采样配置。手势加载或推理出错只停用手势，
不影响原有语音和按键入口。断开重连会建立新识别器，旧连接排队的手势不执行。

主程序日志保存在项目的 `logs/diagnostic.log`（也可在界面打开诊断日志目录）：

实时日志只显示简短的操作行，例如 `[手势] 左滑 → 取消`，以及状态变化时的异常
提示。周期统计、置信度、模型参数和完整事件字段只保留在诊断文件中；未分配的
轻点、响指及旧连接事件不刷实时日志。

- `GESTURE_MODEL`：实际加载的 SDK 模型路径、SHA-256、采样配置与 CPU 推理线程数。
- `GESTURE_RECOGNIZED`：每一次稳定识别的名称、置信度、设备时间，以及是否转交界面。
- `GESTURE_ACTION`：执行的操作，或 `ignored` 的具体原因，例如没有可转换结果、
  没有可取消/可见撤销的操作、目标窗口不在前台、未映射手势或旧连接事件。
- `GESTURE_STATUS`：每 5 秒记录接收样本数、推理数、非空分类数、各手势计数、
  窗口重置、时间戳抖动、队列与丢帧情况；断开时再输出最终统计。

MIC 和 IMU 同时开启时，固件包尾时间戳可能抖动。识别器只对序号连续的数据包
边界容忍最多 50 ms 的时间误差；真正的样本/包丢失、乱序或长时间停顿仍清空窗口。
SDK 模型、预处理、投票和冷却参数保持不变，原始采集时间戳也保留不改写。
云端 ASR 路径的两个本地小型 CNN 使用与独立测试相同的单线程推理，避免近场检测
和手势同时抢占 CPU 而积压手势队列；本地 ASR 后端保持原有线程配置。

## 启动

先在主程序及其他 BLE 工具中断开这枚戒指，然后在项目根目录运行：

```bash
# macOS：启动后输入 Ringo 后面的编号，例如 2CC7
# Ctrl+C 停止、断开并保存
./scripts/test-host-gestures.sh

# 直接指定编号（匹配 Ringo2CC7），不再询问
./scripts/test-host-gestures.sh --ring-id 2CC7

# 扫描后也可用完整名称、MAC 或 UUID 精确选择
./scripts/test-host-gestures.sh --scan
./scripts/test-host-gestures.sh --selector "戒指 MAC 或 macOS UUID"

# 限时测试 / 同时显示每次分类
./scripts/test-host-gestures.sh --duration 60
./scripts/test-host-gestures.sh --show-predictions
```

编号不区分大小写，`2cc7` 与 `Ringo2CC7` 都可输入。只连接完整名称匹配的戒指，
不按信号强度选择，也不把编号当扫描列表下标；找不到时直接提示，不连接其他设备。
若多台设备使用同一名称，需用 `--selector` 指定 MAC/UUID。扫描、离线重放和演示
模式不询问编号。

Windows 使用 `scripts\test-host-gestures.cmd`，参数相同，或者：

```powershell
.\.runtime\venv\Scripts\python.exe -u .\tools\test_host_gestures.py --duration 60
```

使用项目的 `.runtime/venv`。模型及配置已放在
`src/ring_python_sdk/gestures/assets/`，无需另外运行 ai-ring 或下载模型。
旧的 `test-firmware-gestures` 程序仍专门用于固件端测试。

## 手势与输出

| ID | 名称 | 含义 |
|---|---|---|
| 0 | empty | 无手势，只作为分类结果 |
| 1 | swipe-up | 上滑 |
| 2 | swipe-down | 下滑 |
| 3 | swipe-left | 左滑 |
| 4 | swipe-right | 右滑 |
| 5 | tap | 点击/捏合，共用一个类别 |
| 6 | snap | 响指 |

共 **6 个有效手势**。握拳、分指捏合、画圈不在这个电脑模型的范围内。
方向与佩戴方式和模型坐标系有关，不能直接认定始终等于屏幕方向。

- `PREDICTION`：逐窗口分类，包含 empty，始终保存，终端默认隐藏。
- `GESTURE`：稳定判断通过的手势事件，显示中文/英文名、置信度和设备时间。
- 每 5 秒显示 IMU 数量、根据设备时间估计的采样率、分类数、触发数、最近分类、
  推理队列长度、丢弃样本数、窗口重置次数和最大排队时间。
- IMU 和分类数持续增长且分类为 empty，说明模型在运行。IMU 为 0 才是没有收到输入。
  默认连续 10 秒没有 IMU 会报错结束，避免无限等待。
- 触发次数不是准确率，设备时间差和线程排队时间不是端到端识别延迟。

建议先静置、正常打字观察误触，然后每个手势重复尝试并保存记录。

## 参数

默认保留源 SDK 的算法：200 Hz、60 帧窗口、每 5 帧推理、0.10 秒稳定窗、同类投票
比例 1.0、触发后 10 帧冷却及半窗口补充。empty 不触发，不额外增加置信度门槛。

```bash
./scripts/test-host-gestures.sh --mount-angle 135 --mount-radius 0.01
./scripts/test-host-gestures.sh --step-frames 5 --stable-window 0.10 \
  --positive-ratio 1.0 --cooldown-frames 10
```

`--positive-ratio` 是同类投票比例，**不是置信度阈值**。安装杆臂以米为单位。
`--torch-threads` 默认为 1，仅这个独立进程设置 PyTorch 线程数。
IMU 固定为 raw 六轴、200 Hz、16 g、2000 dps、每包 10 帧，不使用低功耗三轴或 token。

### 对照测试漏触发

默认参数需要连续 4 次分类为同一手势才触发。模型短暂判出方向后又回到 empty，
即使最高概率较高，也可能没有 `GESTURE`；周期状态中的“最近分类”只是一瞬间的
结果，不能代表此前整段动作。可检查 `results.csv` 中的连续分类来区分这两种情况。

可以在独立测试中用连续 3 次确认做对照，继续使用同一个 SDK 模型和预处理：

```bash
# 启动后仍输入戒指编号；仅本次测试改成连续 3 次确认
./scripts/test-host-gestures.sh --stable-window 0.075
```

该命令保留默认 200 Hz、每 5 帧推理、同类比例 1.0 和冷却参数；启动时会打印实际
判定参数。直接运行不带参数的脚本仍使用 SDK 默认的 4 次确认，主程序也不受影响。
缩短确认可能增加误触，须分别观察静置/正常打字和每方向重复动作；不能仅凭触发
次数增加判断准确率改善。一直分类为 empty 的动作不会因为缩短确认而被识别。

推理在单独的 `GestureWorker` 线程串行执行。BLE 回调记录数据并入队，队列最多
200 个样本；若处理跟不上，清掉过时积压并计数，后续样本缺口使识别器重置窗口。

## 保存与重放

默认保存到 `data/host_gestures/<时间>/`，可用 `--output-dir` 指定新目录；不覆盖已有目录：

- `samples.csv`：SDK 物理坐标下的六轴值、原始整数、设备时间、样本/包序号、接收时间。
- `results.csv`：全部分类与触发、七类概率、时间和触发序号。
- `summary.json`：固件版本、模型 SHA-256、参数、各手势计数、采样/推理及丢弃统计。
- `sdk/`：SDK 自身的 IMU 等采集记录。

CSV 使用 UTF-8 BOM。退出时断开设备、处理完有限的推理积压并保存摘要；
正常结束、Ctrl+C、断连、模型错误都会走清理路径。

```bash
# 用真实模型重放之前保存的原始 IMU，不连接设备
./scripts/test-host-gestures.sh --replay data/host_gestures/某次测试/samples.csv

# 合成静止 IMU 经过真实模型，只检查链路，不伪造六种手势
./scripts/test-host-gestures.sh --demo

# 只显示配置，不加载模型、不连接、不写文件
./scripts/test-host-gestures.sh --dry-run
```

重放保留设备时间和样本顺序，以电脑能处理的速度推进，避免读取文件过快造成丢弃。
结果用 `source=live_host/replay/synthetic_host` 区分实测、重放和合成数据。

## SDK 用法

```python
from ring_python_sdk.gestures import GestureRecognizer, GestureWorker

# on_prediction(prediction, timestamp_ms) 可选，用于逐窗口诊断。
recognizer = GestureRecognizer(on_gesture=on_gesture, on_prediction=on_prediction)
worker = GestureWorker(recognizer)
worker.start()
try:
    # session 已连接；不需要 session.swipe_on()。
    await session.imu_on(gyro_hz=200, accel_hz=200, on_sample=worker.submit)
    # 保持事件循环运行，并检查 worker.error / worker.snapshot()。
finally:
    await session.disconnect()
    worker.close()
```

同步 `GestureRecognizer.on_sample()` / `feed()` 仍可用。新增 `on_prediction`、
累计 `prediction_count` 和 `reset_count` 只提供观察信息，不改变模型或投票规则。
`reset_count` 包含构造时的首次 reset，worker 的 `window_resets` 排除这一次。
回调在识别线程执行，应将 UI/耗时工作投递出去。每轮新建识别器和 worker。
来源见 `src/ring_python_sdk/gestures/NOTICE.md`。
