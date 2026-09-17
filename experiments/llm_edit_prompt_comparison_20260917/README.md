# 编辑提示词比较（2026-09-17）

本次采用 `variants.py` 中的 `selected_v3`，已静态写入
`src/proximic_ring/text_processing/prompts.py`。它将整体润色与局部纠错的范围规则分开，
明确“润色一下”是完整要求，且有可改善的措辞时不能仅换标点。
同时保留释字、拼写、大小写、局部空格和歧义目标保护规则。
测试证明这条润色失败案例可以完成修改；没有证明所有编辑都正确，也没有找到所谓完美提示词。

## 方法与范围

- 使用设置页已配置的火山方舟 `deepseek-v4-flash-260425`，走应用的 `/responses` 请求、
  工具参数解析和文本编辑执行函数。没有改模型、输出预算或请求参数。
- 用户明确允许将原文“我准备开车去。根本就没有这种开法。”与指令“润色一下这段话。”
  重放到原配置服务。其余都是合成样例；没有批量读取或上传语音历史。
- 凭据只用于原服务认证，保留在内存；报告没有 API Key。
- 每个提示词通常分别测片段替换和全文返回，单分支最多请求一次，与应用并行分支一致。
  `race` 额外调用生产的双协议选择流程，保存两路输出与胜出分支；测试不写入桌面文本框。
- `cases.json` 包含 16 个调优例子、12 个独立例子；后者首次运行前未用于调优。
  16 个已有纠错例子复用上一轮 `llm_correction_prompts_20260917/cases.json`。
- 自动检查包括逐字结果、指定范围、保留字段、格式、字数及是否发生变化。
  开放式改写另做逐条语义审阅；自动检查通过不等于语义正确。
  原始检查结果完整保留，没有为了提高分数删除失败或修改检查阈值。

## 比较过程

`baseline_prompts.json` 是修改前快照。其余候选都冻结在 `variants.py`。
不同阶段的题集和重复次数不同，以下数字不能直接作为各提示词准确率排名。

| 提示词 | 记录 | 自动检查结果 | 影响选择的观察 |
| --- | --- | --- | --- |
| baseline | `synthetic_screen_results.json`、`reported_polish_results.json` | 合成 26/30，真实案例 3/4 | 出现原文不变、列表未分行；局部纠错规则过强是此次提示词调整的依据 |
| scope_prefix | 同上 | 合成 28/30，真实案例 4/4 | 真实案例虽有变化，但多次把“没有这种开法”改成“不是这种开法”；还有中英文空格变化 |
| balanced | 同上 | 合成 28/30，真实案例 4/4 | 仍出现否定含义漂移，扩写长度与事实控制不足 |
| balanced_v2 | `refined_screen_results.json` | 31/32 | 高分仍掩盖“可能”改成“初步定为”、扩写补造安排等问题 |
| concise | `concise_screen_results.json`、`concise_holdout_results.json` | 调优 30/32，独立 23/24 | 原例两路都保留否定含义并润色；仍有扩写长度不足和翻译时间关系偏移 |
| selected | `production_*_results.json` | 纠错 32/32，独立 23/24，专项 11/16，双协议 3/3 | 补清明确修改事实的优先级、时间关系；真实案例单分支仍有一次原文不变，双协议有两次只换标点 |
| selected_v2 | `final_correction_results.json`、`final_polish_race_results.json` | 纠错 31/32，润色双协议 9/9 | 改善仅换标点问题；出现一次无英文目标却强行替换，因而再补强无匹配规则 |
| selected_v3（采用） | `accepted_*_results.json` | 见下表 | 保留润色改善，补强拼写的目标定位；仍有歧义范围误改，不能保证全对 |

`production` 是运行时读取生产常量的标签，不代表这些历史文件都使用当前版本。
每份报告的 `prompt_hashes` 标识当时版本：`production_*` 对应 selected，
`final_*` 对应 selected_v2，`accepted_*` 对应当前 selected_v3。

## 当前版本的验证

| 验证 | 结果 | 记录 |
| --- | --- | --- |
| 16 条已有口述纠错 × 两种协议 | **31/32** 严格逐字检查通过 | `accepted_correction_results.json` |
| 无对应英文目标，三次重复 × 两种协议 | **6/6** 保持原文 | `accepted_no_match_results.json` |
| 真实失败例、普通润色、仅润色中间句，各重复三次；生产双协议流程 | **9/9** 有修改并通过自动范围检查 | `accepted_polish_race_results.json` |
| 请求构造、工具协议、文本替换等现有代码测试 | **56 passed** | 下方命令 |

真实原例的三次最终输出分别是：

1. “我准备开车去。根本没有这种开法。”
2. “我打算开车去。根本没有这种开法。”
3. “我准备开车去，但根本没有这种开法。”

前两次保留原句关系并略去赘词；第三次自行添加了转折连接词。
三次都完成了编辑，但第三次不满足最严格的“不推断两句关系”要求。
原句缺少背景，输出通常只是轻度润色，不能把这些结果描述为全面改写或语义全部通过。
普通口语段落的赘词明显减少；仅润色中间句的三次输出均保留了两侧编号、金额和日期原文。

32 项纠错中唯一失败是：
原文“第一个界面不错，第二个界面也不错”，只说“街道的街，面包的面”而未指定位置，
片段分支把两处都改成了“街面”，全文分支正确保持原文。
生产双协议会拒绝无变化结果，因此不能依靠正确的无变化分支去覆盖另一路误改。
这一残余风险明确保留在报告中；本次没有增加额外模型验证或新的拦截逻辑。

## 自动检查与语义审阅的区别

- 独立集的“星期一”被关键词检查误判为缺少“周一”，语义正确；原始 23/24 不改写成 24/24。
- “大约/约”“下个季度/下一季度”等也可能被字面检查漏认。
- 扩写即使满足长度与关键词，仍可能新增资源配置、准备要求等原文未给的安排；不能视为正确。
- 英译 `on Friday` 与 `by Friday` 的时间关系不同，关键词 Friday 检查无法识别这种错误。
- 最终仅补强了润色执行和拼写定位，未全面复测最终版本的扩写、翻译；较早候选暴露的相关限制
  仍视为未解决。12 条独立集是在 concise 和 selected 阶段运行，不冒充最终版本结果。
- 即使规则和样例写明，模型仍会波动。一次或几次通过不能证明不会再出错。

## 变更边界与延迟

运行代码只调整两种编辑协议共享的提示词及返回格式说明；分类、听写、ASR、撤销关联、
写入和历史记录流程均未在本次改动。测试中原来的硬编码提示词片段断言改为核对所选的完整
生产提示词，继续验证正确协议及工具参数。没有新增生产模型请求、候选选择或重试次数。

最终片段提示词 1637 字符、全文提示词 1481 字符（原来分别 2128、1934），字符数不是 token 数。
测试有服务耗时波动和较长尾延迟，不能据此承诺响应变快。`race` 的实验耗时包含收齐两路日志，
与 UI 取到首个有效候选的耗时不同，可另看 `branch_traces` 内胜出分支耗时。

当前静态提示词 SHA-256：

```text
fragment 42c62fed0db277f8b0df2d693eecc4fc8944606bb89abc43dfd5b5bdcde0619c
full     5b883ffec8a8711e4413da7bdd3f34af3f74ddd712fb2c19460ebf52a34c9571
```

## 复现

从项目根目录运行。线上命令会实际调用设置页配置的模型并产生服务用量；普通 pytest 不调用线上模型。
结果请用新的文件名，保留本次原始记录。

```sh
.runtime/venv/bin/python experiments/llm_edit_prompt_comparison_20260917/evaluate.py --variants production --suite corrections --output /tmp/edit-corrections-new.json
.runtime/venv/bin/python experiments/llm_edit_prompt_comparison_20260917/evaluate.py --variants production --suite holdout --output /tmp/edit-holdout-new.json
.runtime/venv/bin/python experiments/llm_edit_prompt_comparison_20260917/evaluate.py --variants production --case reported_polish --branches race --repeat 3 --output /tmp/edit-polish-new.json
PYTHONPATH=src .runtime/venv/bin/python -m pytest -o addopts='' -q --tb=short tests/test_text_processing.py tests/test_edit_operations.py tests/test_llm_tool.py
```

运行中的应用需要重启以加载更新后的静态提示词。
