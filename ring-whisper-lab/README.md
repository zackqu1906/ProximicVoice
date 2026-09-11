# Ring Whisper Lab

独立的双 ring 耳语采集、对齐、试听和训练数据管理项目。当前在 ProximicVoice
工作区的 `ring-whisper-lab/` 下，有自己的包、启动入口、测试与依赖声明，
可整体移动到其他目录。除可选复用 Python/libopus 运行环境外，不依赖主项目的
近场检测、ASR、自动分段或界面。

## 启动

在当前 Mac 上双击 **`启动采集.command`**，或在父工作区执行：

```sh
.runtime/venv/bin/python ring-whisper-lab/run.py gui
```

启动器优先使用本项目 `.venv`，否则复用父目录已安装依赖的 `.runtime/venv`。
采集文件默认写入本项目 `data/`，界面底部显示保存位置。其他目录可通过启动参数
`--data-dir /绝对路径` 指定（放在 `gui` 子命令之前）。

独立安装（Python 3.11+）：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python run.py gui
```

macOS 的 Opus 解码需要 libopus（可用 `brew install opus`）。启动器会优先
发现本项目或父工作区 `.runtime/opus/lib` 中已有的库。其他位置可设置
`PROXIMIC_OPUS_DIR`。Windows 使用 `.venv\Scripts\python.exe`，并安装对应
平台的 libopus DLL；环境变量指向 DLL 所在目录。

首次蓝牙连接需系统授权。采集前先在主应用断开选中的 ring，避免同一设备被
另一客户端占用。试听使用默认输出，提示音优先选择内置/MacBook 扬声器，界面显示实际输出设备；
无匹配时使用系统默认输出。不修改系统音量，请确保不是耳机、扬声器未静音且不会让麦克风削波。

## 一次采集的流程

1. 点击“扫描设备”，选择两只不同的 Ring：**A 裸麦、B 戴防喷罩**，点击“连接两只设备”。
   名称含 Ring 的设备排在前面；连接时验证 Ringo NUS 服务。
2. 点击“开始录音”。等两路均收到音频后，电脑自动播放开始提示音及一小段静音缓冲。
   **看到“请说话”后再开口**，建议每条10–60秒。
3. 说完点击“结束并保存”，保持安静。电脑先播放结束提示音，录完后才停止两路麦克风。
4. 保存后自动根据首尾提示音对齐、校正线性漂移、裁掉标记，保留中间的完整说话区间。
   点击列表中的任意一条，直接听 A、B，或左右声道对照。
   波形上轨为 A、下轨为 B，切换声道尽量保留播放位置。

**不需要填写采集表单、手动对齐或人工复查，没有训练导出操作。**
历史已保存但未对齐的录音也会在启动时自动处理。对齐过程不阻塞下一条录音。
录音中会禁用普通试听，只允许同步提示音；每条最长10分钟。超时/断连自动停止时可能缺结束标记，
这种录音只保留原音，不猜测裁剪。关闭窗口会先完成结束提示音、保存和处理。

对齐质量有疑问时仍可试听，并显示简短提示；短到无法对齐的录音会明确标为原音，
分别听 A/B，不能假装成同步立体声。自动对齐失败不会丢弃录音或弹出复核流程。
提示音是不同的三段短扫频（开始/结束不会混淆），自动播放，不需要手动触发。
保持扬声器和两个麦克风相对位置固定、尽量等距；高音量不是精度保证，避免削波。
若漏录、静音、相关性不足或多个相近匹配，显示失败并保留原音，绝不退回语音猜测裁剪。

采集不经过主项目的 **VAD、reject 截断、额外增益或自动音量归一化**。
设备内部固件默认增益未经读回，不应将其误认为零增益。
若交换实物，重新选择裸 Ring 为 A、戴罩 Ring 为 B。

## 对齐的含义和限制

同时发 MIC ON 并不保证采样同步。每只 ring 有独立客户端、分包组装器和
Opus解码器。保存的设备 uptime 与主机接收时间用作诊断，不直接当作共同ADC时钟。

新录音的 `metadata.sync_protocol` 为 `chirp_bookends_v1`。在每路录音的首/尾12秒内，
分别用已知的开始/结束信号进行归一化匹配，再定位各标记的三个子脉冲。
利用两路对应的六对子脉冲位置拟合常量偏移和线性漂移：

```text
reference_sample = input_sample * (1 + drift_ppm / 1_000_000) + offset_samples
```

正偏移表示对应的声音在参考文件中具有更大的采样下标。裁剪起点为开始标记结束后至少0.5秒，
终点为结束标记开始前至少0.15秒，取两路均避开标记的共同区间。
提示音播放文件额外带前后静音，UI 等待开始音缓冲结束才显示“请说话”。
**中间不做VAD、不删除停顿，不自动判断耳语是否是语音。** 只对参考路做线性插值；
输入保持原样裁出，不做频响/增益补偿，也不改变极性。输出为16kHz PCM16 WAV。

检查包含标记相关性、匹配唯一性、顺序、子脉冲拟合残差、漂移、削波和传输完整性。
拟合残差超过0.5ms会提示，任一子脉冲残差超过2ms或漂移超过5000ppm则拒绝裁剪。
**这些是初步工程检查：首尾标记不能独立验证中间的时间误差，也不保证整段0.5ms精度。**
阈值尚需真实扬声器、房间和双 Ring 数据验证，明显混响可能需要更长的首尾缓冲。

旧录音没有提示音，仍保留原先的400–6000Hz语音互相关对齐方式，并在界面明确标为旧版。
不会给旧录音补造提示音，也不会自动覆盖历史版本。

线性时钟校正解决不了间歇采样速率变化和丢失的信息。发现丢帧时按块长估计补零，
记录准确的占位区间和估计标志，整条录音默认排除训练。原始解码回调和原包都保留，
以后可以重新解码、重建时间轴或升级为分段漂移校正。

## 每条录音保存什么

```text
data/<take_id>/
  record.json                   # 设备角色、状态、统计与对齐版本；保留旧版复核字段
  markers.jsonl                 # 提示音播放请求/结束事件（不是声学采样时间戳）
  raw/
    input.wav                   # 按帧序号整理的裸麦音频，丢帧占位另有标记
    reference.wav               # 参考麦音频
    input.capture.wav           # 解码回调到达顺序；逐块更新WAV头
    reference.capture.wav
    input.frames.jsonl          # 帧序号、PCM位置、主机时刻、设备uptime
    reference.frames.jsonl
    input.quality.json          # 顺序时间轴、丢帧区间、错误、音量/削波统计
    reference.quality.json
    input.notifications.bin     # 原始通知：uint64主机ns + uint32长度 + 包内容，小端
    reference.notifications.bin
  aligned_<revision>/
    input.wav                   # 新版为已去除首尾标记的连续说话区间
    reference.wav               # 与 input 等长且已校正时钟漂移
    stereo.wav                  # 左输入、右参考
    alignment.json              # 算法、首尾标记位置、拟合、裁剪采样范围和限制
```

每次对齐产生新版本，不覆盖原始音频或历史对齐版本。试听无需任何人工接受标记。
任何一路中断或超过5秒不来音频，会停止两路并保存为 interrupted，防止假装成完整对。
正常停止会留350ms等待在途通知；此前已丢失的包不会凭空恢复。
程序意外退出时，已写入的 `.capture.wav`、包文件和JSONL仍在；它们可用于后续恢复，
当前界面不会自动把未完成录音当成已完成采集。

## 训练数据与模型预留

简化界面只负责录音与试听；不会把“自动对齐成功”误标成人工接受或干净参考。
说话人保存为 `unknown`、语音类型为 `unspecified`，设备 ID、采集场次、音频与对齐信息
仍完整保留。正式训练前再补充说话人/语音类型等标注，以便正确划分训练和验证数据。

以下清单导出是保留的命令行开发接口，不是日常采集步骤。它仍使用原有的保守过滤：
仅导出非演示、完整、A裸/B罩、已标注语音类型、人工接受且自动质量通过的条目。
因此新简化界面的未标注录音不会被该接口直接导出；后续搭建训练流程时再制定筛选策略。

导出位于 `data/exports/<time>/`，包含 `train.jsonl`、`validation.jsonl`、
`test.jsonl` 和导出报告。清单的路径相对于数据根目录，可随整个 `data/` 搬走。
默认每4秒一个片段，不复制音频，通过采样下标读取。句子级转写明确标为
`take_transcript`，不会错误地当成每个4秒片段的转写。

按说话人哈希分配约80/10/10，同一人所有场次保持同一集合。人数少时可能有
空集合，导出报告会提醒；正式训练前需收集独立验证和测试说话人。

```python
from pathlib import Path
from ring_whisper_lab.dataset import PairedAudioDataset

data = PairedAudioDataset(Path('data/exports/<time>/train.jsonl'), Path('data'))
pair = data[0]  # input/reference: float32 mono arrays; metadata: sample interval and provenance
```

模型接口放在 `src/ring_whisper_lab/models/base.py`，初始设计在
`configs/mask_gru.toml`，后续训练说明在 `training/README.md`。
当前版本完成采集与数据处理，未实现/训练增强模型。采集不需要安装torch；
未来可安装 `.[train]`。

## 无设备演示和验证

命令行 `demo` 会生成带低频扰动、已知延迟和漂移的双路测试信号，
并自动对齐。它不是真实耳语，始终标为demo，不能导入真实训练清单。

```sh
.venv/bin/python run.py demo
.venv/bin/python run.py scan
.venv/bin/python run.py align data/<take_id>
.venv/bin/python run.py export
.venv/bin/python -m pytest -q
```

测试覆盖时差/漂移、静音和无关信号、原始文件不变、自动对齐与默认试听、失败时原音回退、
帧乱序/丢帧/回绕、双设备独立采集、部分连接失败清理及训练数据导出。模拟测试不能替代两只真实
ring同时录音的蓝牙带宽、固件编码、同步精度和耳语听感验收。
