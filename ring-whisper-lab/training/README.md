# 后续训练模块

这里预留训练入口；当前版本没有启动训练，也没有声称已实现增强模型。

- 模型接口：`src/ring_whisper_lab/models/base.py`
- 模型初始设计：`configs/mask_gru.toml`（STFT + 因果 CNN/GRU + 掩码）
- 已实现数据读取：`ring_whisper_lab.dataset.PairedAudioDataset`
- 导出数据：`data/exports/<time>/{train,validation,test}.jsonl`
- 模型输入只有 input 路；reference 路仅提供近似监督。
- 后续实验输出放 `runs/`，权重放 `checkpoints/`，均不提交 Git。

训练前先检查有效片段数、说话人分组，以及验证/测试是否为空。
不同场次的同一说话人不跨集合；同步短音必须在复核区间中排除。
真实双 ring 不能直接当作理想逐采样同源监督。先用对齐的幅度谱损失，
并加入同源耳语合成噪声和干净输入恒等样本，验证是否吞掉耳语辅音。
