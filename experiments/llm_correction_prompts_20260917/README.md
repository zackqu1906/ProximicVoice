# 口述纠错提示词实测

`cases.json` 使用人工构造的例句，覆盖释字组词、否定后纠正、拆字姓氏、英文拼写、大小写、下划线、目标歧义、重复目标范围和普通听写边界。

每个带 `route` 的例子只将口述内容交给生产类型判断器；每个带 `source` 的例子分别经过生产片段替换、全文修改两条链路，并逐字比较最终文本。`legacy_*` 的孤立词案例只验证已进入编辑模式的行为，不声称没有上下文的分类器能推断孤立词的意图。

从项目根目录显式运行：

```sh
.runtime/venv/bin/python experiments/llm_correction_prompts_20260917/evaluate.py --output experiments/llm_correction_prompts_20260917/results.json
```

只验证一个案例可加 `--case explained_homophone`；该选项可重复。只验证一个阶段可加 `--phase full`（或 `fragment` / `route`）。脚本使用应用已保存的模型连接，通过实际 HTTP 请求产生模型用量；认证密钥只在内存中使用，不写入报告。不采集录音、不操作文本框、不改变应用设置，也不在普通 pytest 中联网运行。

实测模型为应用配置的 `deepseek-v4-flash-260425`，结果保留原始返回及各阶段提示词哈希：

- `initial_results.json`：第一轮 47/49，发现英文未指定大小写时，两条分支都没有保留目标原有的首字母大写。
- `second_results.json`：46/52，补充口述大小写规则后仍有波动，并发现重复目标范围、自我介绍分类问题。此轮新增的汉字转英文案例最初预期错误地添加了空格；后续已改为严格保留原文排版，旧报告不改写。
- `results.json`：进一步明确优先级、唯一定位和大小写示例后，51/52 严格匹配。分类 20/20、片段 16/16、全文 15/16。
- `final_full_results.json`：最后仅加强全文分支的原位拼接要求并复测全部 16 个编辑案例，15/16。其余两个阶段的提示词未再改变。

**已知限制：**“我们用派森处理数据。”改为 Python 时，全文分支仍返回“我们用 Python 处理数据。”，额外添加了空格；片段分支严格保留原有排版。这是实际模型未完全遵循提示词，未放宽断言或抹掉失败记录。纠错词本身正确，本次未修改编辑执行器来强制处理排版。

离线回归 `tests/test_text_processing.py`、`tests/test_edit_operations.py`、`tests/test_llm_tool.py` 共 56 项通过。多候选研究另见 `docs/LLM_EDIT_CANDIDATES_RESEARCH.md`，未改变生产输出协议。

实测只覆盖 LLM 文本阶段，不能证明实际麦克风、ASR 能正确转写每个字母。运行次数有限，成功率也不能视为长期准确率或候选置信度。
