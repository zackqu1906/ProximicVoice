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
└── attempt_002/
```

- `audio_raw.wav` 是 `ProximitySessionController` 最终裁剪的单声道 16 kHz
  PCM16 音频；ASR 使用相同的未放大波形。
- `asr_updates.jsonl` 按发生顺序保存 partial、final、backend、model、延迟和错误。
- `llm_branches.jsonl` 同时保存 fragment/full 的原始返回、校验状态、完整候选、
  分支延迟和 winner。为了获得完整训练记录，采集路径会等待两条并行分支完成。
- `attempt.json` 保存目标全文、应用名、ASR 摘要、LLM 配置、候选和用户反馈事件。
- `episode.json` 汇总重说产生的 Attempt，并在确认、取消、应用失败或中断时持久化终态。

确认或取消后，UI 会重新读取目标控件中的真实文本。回读值与已知候选/原文不同
时，Episode 的 `manually_corrected` 为 `true`。`retry` 只结束当前 Attempt，下一段
语音会创建同一 Episode 下的新 Attempt；音频不会跨 Attempt 重新配对。

训练集构建时应只用同一个 Attempt 的 `audio_raw.wav` 和 ASR final。重说后的最终文本
可以作为 Episode 监督目标或偏好 chosen，但不得直接标注到重说前的音频上。
