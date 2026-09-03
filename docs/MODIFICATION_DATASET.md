# 用户修改数据采集

桌面 UI 会把修改模式的数据保存到项目根目录的 `dataset/`。匿名用户 ID
首次运行时生成并保存在应用设置中；原始文本、语音和模型返回仍可能包含用户内容，
因此该目录已加入 `.gitignore`，不应直接提交或分享。

```text
dataset/<anonymous_user_id>/<episode_id>/
├── episode.json
├── attempt_001/
│   ├── audio_raw.wav
│   ├── asr_updates.jsonl
│   ├── llm_branches.jsonl
│   └── attempt.json
```

- `audio_raw.wav` 是 `ProximitySessionController` 最终裁剪的单声道 16 kHz
  PCM16 音频；ASR 使用相同的未放大波形。
- `asr_updates.jsonl` 按发生顺序保存 partial、final、backend、model、延迟和错误。
- `llm_branches.jsonl` 同时保存 fragment/full 的原始返回、校验状态、完整候选、
  分支延迟和 winner。第一个有效候选会立即交给 UI；较慢分支继续在后台运行，结束后
  原子补写完整记录，不阻塞用户预览或确认。只有两边都尚未成功时才继续等待。
- `attempt.json` 保存目标全文、应用名、ASR 摘要、LLM 配置、候选和用户反馈事件。
- `episode.json` 汇总本次 Attempt，并在确认、取消、应用失败或中断时持久化终态。

LLM 没有返回可用编辑结果时，错误会立即保存在该 Attempt 的 `llm_error`，两条分支
各自的原始返回、校验错误和延迟仍保存在 `llm_branches.jsonl`。此时 Episode 保持
`active`，界面持续显示具体错误，并只允许用户取消，不会因提示超时而直接把
Episode 标为 abandoned。

用户在这个已知 LLM 失败状态选择取消时，Episode 结束为 cancelled。系统不再要求
用户选择取消或撤回原因；失败类型由已有的 ASR、LLM 和应用结果字段在离线处理时推导。

确认或取消后，UI 会重新读取目标控件中的真实文本。回读值与已知候选/原文不同
时，Episode 的 `manually_corrected` 为 `true`。每段音频只与自身 Attempt 配对。

训练集构建时应只用同一个 Attempt 的 `audio_raw.wav` 和 ASR final。
