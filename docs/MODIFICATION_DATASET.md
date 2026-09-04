# 统一交互数据采集

桌面 UI 将听写和编辑模式的数据统一保存到匿名用户目录：

```text
dataset/<anonymous_user_id>/
├── interactions/
│   └── <interaction_id>/
│       ├── record.json
│       ├── events.jsonl
│       ├── asr_updates.jsonl
│       ├── audio.wav
│       └── imu.jsonl（有 IMU 数据时）
└── associations.jsonl（有关联时）
```

每次说话只对应一个 `InteractionRecord`。`record.json` 保存 ASR 最终文本、输入模式、
非敏感 LLM 输入与输出、所有编辑分支、目标文本、应用结果、撤回/取消、人工结果和关联 ID；
`events.jsonl` 保存事件顺序。音频、ASR 更新和已经对齐到音频起点的 IMU 数据与该记录放在同一目录中。

记录还包含可直接追溯来源的模型标签：应用成功给近点和 ASR 写弱正例；应用前明确取消给
近点写反例；ASR 空结果或错误只给 ASR 写反例，不会污染近点标签；自动语音类型在应用后
写正例，用户切换类型时写入原预测和纠正结果。

编辑模式的 fragment/full 分支直接写入 `record.json` 对应的 LLM request，不再创建或维护
Episode、Attempt、`episode.json`、`attempt.json`、`audio_raw.wav` 或独立分支副本。

`associations.jsonl` 只保存关联关系：一个稳定的 `association_id`、类型、一个正例引用、
一个或多个反例引用以及完整的成员 Interaction ID。它不会复制或重组原始交互数据。
每个成员记录同时保存 `association_memberships`，可由任一 Interaction 反向找到所属关联及
其中的正/反例角色。

匿名用户 ID 首次运行时生成并保存在应用设置中。原始文本、语音和模型返回仍可能包含
用户内容，因此 `dataset/` 已加入 `.gitignore`，不应直接提交或未经处理地分享。
