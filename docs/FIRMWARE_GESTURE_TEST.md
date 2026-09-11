# 固件端手势测试

这个独立终端程序只接收 Ring 固件的 Swipe 分类与触发结果。它不启动麦克风、
电脑端 IMU 模型、ASR、LLM，也不调用主程序的语音动作或数据关联按钮。
本次补充的是项目内置的 `src/ring_python_sdk`，无需改用旁边下载的 SDK 目录。

## 启动

先在 Proximic Voice 主界面和其他 BLE 工具中断开这枚戒指，让测试程序单独连接。
在项目根目录执行：

```bash
# macOS：默认持续运行，Ctrl+C 停止并保存
./scripts/test-firmware-gestures.sh

# 查看附近设备（只扫描），然后用输出中的 MAC 或 macOS UUID 指定戒指
./scripts/test-firmware-gestures.sh --scan
./scripts/test-firmware-gestures.sh --selector "你的设备 MAC 或 UUID"

# 限时 60 秒
./scripts/test-firmware-gestures.sh --duration 60

# 同时打印每次分类，包括 empty；默认只打印触发和每 5 秒的状态汇总
./scripts/test-firmware-gestures.sh --show-events
```

Windows 使用 `scripts\test-firmware-gestures.cmd`，参数相同。也可以直接运行：

```powershell
.\.runtime\venv\Scripts\python.exe -u .\tools\test_firmware_gestures.py --duration 60
```

`--name Ringo` 是默认名称关键词。多个戒指同时出现时，建议用 `--selector` 指定
MAC/UUID；扫描列表的顺序可能变化，不建议跨次扫描依赖数字序号。

## 看什么

- `EVENT`：固件每次分类的结果，可能包含 `empty`。出现非空 EVENT 不代表已经触发。
- `TRIGGER`：固件做完触发判断后上报的结果。终端显示序号、中文/英文手势名、
  类别 ID、协议版本、置信度、设备时间和距上次触发的主机接收间隔。
- 程序不增加阈值、不做二次防抖、不去掉重复包，保留固件上报的行为供测试。
  汇总中的 TRIGGER 数是收到的包数，不等于正确识别次数。
- 每 5 秒输出 EVENT/非空 EVENT/TRIGGER 数，以及无效包、序号缺口、重复包、
  乱序包计数。序号缺口只是接收侧诊断，不能单独证明蓝牙丢包的原因。
- 启动时读取固件版本，收到包后再明确显示 V1 或 V2。发送 START 没有应答确认；
  一直没有结果时，请检查固件支持、连接状态，并尝试做手势。
- 启动时还检查 INFO 的 Swipe 组件。若设备明确声明 `present=0` 或 `model=none`，
  程序提示当前固件不提供板端手势模型、保存摘要并退出，不再循环输出零计数。
  没有返回 INFO 或旧版表中缺少 Swipe 项时，能力为未知，仍允许尝试旧协议。
- 蓝牙断开会结束本次测试并保存记录；不会自动重连把两次测试混在一起。

V2 为 12 分类，包含 11 个有效手势：

| ID | 名称 | 中文 |
|---|---|---|
| 0 | empty | 无手势，只有分类结果 |
| 1 | swipe-up | 上滑 |
| 2 | swipe-down | 下滑 |
| 3 | swipe-left | 左滑 |
| 4 | swipe-right | 右滑 |
| 5 | swipe-tap | 点击 |
| 6 | snap | 响指 |
| 7 | clench | 握拳 |
| 8 | index-pinch | 食指捏合 |
| 9 | middle-pinch | 中指捏合 |
| 12 | circle-clockwise | 顺时针画圈 |
| 13 | circle-counterclockwise | 逆时针画圈 |

SDK 来源文档注明普通 v1 固件从 1.2.68 起支持 Density V2；实际能力以设备固件为准。
版本号达到该范围也不能代替能力检查，需使用适配硬件且包含 Swipe 模型的固件构建。
ID 10/11 当前不在 V2 输出中，ID 12/13 **不是概率数组下标**。
旧版协议只有 7 类（含 empty），上报的是有符号 `scores`；其 `confidence` 显示
`N/A`，CSV 留空，不转换成看似真实的概率。

建议先静止或正常打字观察误触，再分别重复每个手势，比较漏触、误分类与连续触发。
这里的触发间隔不是端到端识别延迟；没有人工动作时间或真值标注时，程序不计算准确率。

## 保存记录

默认保存到 `data/firmware_gestures/<时间>/`：

- `events.csv`：全部 EVENT/TRIGGER，UTF-8 BOM，含接收 UTC 时间、经过秒数、
  手势名、类别、置信度、设备 uptime、V2 中心时间和 event mass、各类概率/旧版分数。
  TRIGGER 额外记录触发序号，以及主机和设备时间计算的触发间隔。
- `events.summary.json`：固件版本、实际协议、结束原因、各手势次数及 SDK 接收统计。
- `sdk/`：SDK 自己保存的原始分类/触发 CSV、性能统计等采集文件。

用 `--csv /path/to/my-test.csv` 指定输出位置；摘要为 `my-test.summary.json`，
已有 CSV/摘要不会被覆盖。Ctrl+C 会停止 Swipe、断开并写出摘要；触发时和状态输出时
刷新 CSV。被强制杀死进程或断电仍可能来不及保存末尾数据和摘要。

## 不连接设备的检查

```bash
./scripts/test-firmware-gestures.sh --dry-run
./scripts/test-firmware-gestures.sh --demo
```

`--dry-run` 不写文件。`--demo` 用合成 V2 数据包走同一解析、回调和记录链路，
演示全部 11 个手势；结果标记为 `source=demo`，不代表真实设备识别效果。

## SDK 接口

```python
from ring_python_sdk import RingSession
from ring_python_sdk.swipe import SwipeResult

def on_trigger(result: SwipeResult):
    print(result.name, result.confidence, result.uptime_ms)

# 在既有已连接 session 上调用；这里不需要 imu_on() 或 GestureRecognizer。
await session.swipe_on(on_trigger=on_trigger, on_event=None)
# ...
await session.swipe_off()
```

`on_event` 接收分类，`on_trigger` 接收触发，均为 `SwipeResult`：
`protocol_version`、`kind`、`seq`、`class_id`、`name`、`uptime_ms`、
`scores`、`probabilities`、`confidence`、`center_uptime_ms`、`event_mass`。
V2 的概率按 `(0,1,2,3,4,5,6,7,8,9,12,13)` 排列。

回调在 BLE 通知路径同步执行，耗时业务应投递到自己的队列。回调异常计入
`callback_error_count` 并写入 SDK 日志，不中断其他包的解析。重复调用 `swipe_on()`
不会替换正在使用的回调；先 `swipe_off()` 再开始新一轮。
`print_events`、`print_triggers`、`print_profile` 只控制日志，不改变回调或 CSV。
现有无参数 `swipe_on()` 的默认行为保持兼容。

V2 协议解析和常量来自本地提供的 `ring-python-sdk-feat-ringo-gestures-sdk`。
统一回调、生命周期清理和测试程序在项目内补充，尚未接入主界面动作。
