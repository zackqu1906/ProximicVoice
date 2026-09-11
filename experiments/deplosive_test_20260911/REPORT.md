# De-plosive A/B test

同一条已分段音频分别以原始版和动态 De-plosive 版重新送入同一 ASR。
历史 ASR 文本不是人工标准答案，因此 `potential` 只表示候选改善，最终需结合试听确认。

## Summary

- Sessions: 20
- Detected regions: 62
- Mean 20–250 Hz change: -2.10 dB
- Mean 300–3000 Hz change: -0.002 dB
- ASR unchanged: 15
- ASR potential improvements: 0
- ASR potential regressions: 0
- ASR changed, needs reference: 5
- Historical-text proxy better/same/worse: 0/11/2

## Listening order

每条目录中的 `ab_original_then_deplosive.wav` 都是原始版在前、处理版在后。
`top_candidates_original_then_deplosive.wav` 按低频衰减幅度排列候选样本。

## Per-session results

| Session | Prior | Raw ASR | De-plosive ASR | Assessment | Regions | Low Δ | Speech Δ |
|---|---|---|---|---|---:|---:|---:|
| interaction_2026-09-11_16-53-27-602 | ∅ | 。 | 。 | unchanged | 14 | -7.16 dB | -0.000 dB |
| interaction_2026-09-11_16-53-07-098 | 24。 | 124。 | 124。 | unchanged | 0 | -0.15 dB | -0.010 dB |
| interaction_2026-09-11_16-52-48-628 | ∅ | 打牌电话不被。 | 打牌电话宝被。 | changed_needs_reference | 2 | -0.54 dB | -0.000 dB |
| interaction_2026-09-11_16-52-43-145 | ∅ | 不不不不不。 | 不不不不不。 | unchanged | 0 | -0.13 dB | -0.000 dB |
| interaction_2026-09-11_16-52-39-164 | ∅ | 大白点不的不不。 | 大白点不的宝哭。 | changed_needs_reference | 3 | -0.87 dB | -0.000 dB |
| interaction_2026-09-11_16-52-33-601 | 12345。 | 第二305。 | 第我搜索。 | changed_needs_reference | 2 | -3.77 dB | -0.000 dB |
| interaction_2026-09-11_16-52-24-655 | 什么时候到达不了？ | 是不是对的不了？ | 是不是对的不了？ | unchanged | 4 | -1.02 dB | -0.000 dB |
| interaction_2026-09-11_16-52-16-583 | 是不是这样的？大家都不要。 | 是不是这样图大图不了。 | 是不是这样图大图不了。 | unchanged | 4 | -0.89 dB | -0.000 dB |
| interaction_2026-09-11_16-52-07-416 | 这样。 | 吃这样的东西带图片。 | 是这样的东西带图片。 | changed_needs_reference | 4 | -1.24 dB | -0.009 dB |
| interaction_2026-09-11_16-52-02-225 | ∅ | 你不想吃错话？ | 你不想吃错话？ | unchanged | 2 | -0.24 dB | -0.004 dB |
| interaction_2026-09-11_16-51-58-924 | ∅ | 空聊。 | 空聊。 | unchanged | 0 | -0.13 dB | -0.000 dB |
| interaction_2026-09-11_16-51-50-438 | 把钱不是坏的。 | 把戒播放。 | 把戒播放。 | unchanged | 1 | -0.21 dB | -0.002 dB |
| interaction_2026-09-11_16-19-00-221 | 删掉。 | 帮我把这个东西删掉。 | 帮我把这个东西删掉。 | unchanged | 5 | -1.50 dB | -0.004 dB |
| interaction_2026-09-11_16-18-47-406 | 说一个小故事。 | 说个小故事。 | 说个小故事。 | unchanged | 4 | -1.49 dB | -0.000 dB |
| interaction_2026-09-11_16-18-25-174 | 故事。 | 说一个新故事。 | 说一个新故事。 | unchanged | 3 | -0.97 dB | -0.004 dB |
| interaction_2026-09-11_16-18-17-427 | 删掉上一句话。 | 删掉上衣服。 | 删掉上衣服。 | unchanged | 1 | -0.73 dB | -0.003 dB |
| interaction_2026-09-11_16-18-02-086 | 沙雕，上海警方。 | 删掉伤害就好。 | 删掉伤害就好。 | unchanged | 5 | -2.98 dB | -0.000 dB |
| interaction_2026-09-11_16-17-45-890 | 声调上一句话。 | 删掉声意就大。 | 删掉声意就大。 | unchanged | 2 | -6.10 dB | -0.000 dB |
| interaction_2026-09-11_16-17-36-237 | 删掉上海警方。 | 删掉啥就。 | 删点啥就。 | changed_needs_reference | 4 | -4.78 dB | -0.000 dB |
| interaction_2026-09-11_16-17-21-637 | ∅ | 。 | 。 | unchanged | 2 | -7.10 dB | +0.000 dB |
