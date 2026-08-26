# ProximicVoice 用户数据反馈、个性化增强与论文方案研究报告

**受众**：ProximicVoice 产品与研究开发者  
**日期**：2026-08-25  
**范围**：中文、跨应用、Ring 近场语音输入与语音编辑；下游 ASR/LLM 主要视为封闭 API，不以微调大模型为前提。  
**研究目标**：确定应收集哪些真实用户数据、怎样把数据转化为输入法能力，以及怎样组织成一项可发表的 HCI/IUI 研究。

## 直接结论

最好的方案不是收集大量原始音频后训练一个“万能中间分类器”，也不是为每个用户微调 LLM，而是构建一个**隐私分层、结果可归因、从最终用户结果学习的自适应语义输入闭环**：

1. 将当前日志升级为结构化、可重放的 interaction event store。
2. 将“用户最终保留的文本”作为最强监督；确认、取消、重说和撤销只作为有噪声的弱反馈。
3. 把反馈分别反哺三层：用户词汇/专名记忆改善 ASR；接受过的改写与偏好记忆通过检索进入 Prompt；策略价值模型选择直出、轻整理、强整理、片段编辑、全文编辑、并行竞争、预览和超时。
4. 不要求修改封闭 LLM 权重。个性化主要通过动态记忆、少量相关示例、Prompt 编译和 contextual bandit/utility router 实现。
5. 论文的核心主张应是：**基于真实结果反馈的全局适应和个性化适应，能否比固定 LLM Prompt 更快地产生用户愿意保留的文本，同时降低关键错误并保持用户声音。**

这一方向具备完整的 HCI 贡献链：Understanding People（用户如何输入、修复和适应）→ Building Systems（反馈感知的语义输入法）→ Evaluation（受控与纵向研究）。无法保证录用，但比单纯做行为分类或 Prompt 调参更有稳定的贡献结构。

## 1. 项目现状与缺口

### 1.1 已经具备的可利用触点

ProximicVoice 已有不少适合构建数据闭环的基础：

- `TextProcessingRequest`/`TextProcessingResult` 已有关联 request、session、模式、ASR 原文、目标文本、LLM 结果、延迟、错误和原始模型输出的字段（`src/proximic_ring/text_processing/model.py:94-115`）。
- ASR worker 已记录“开始接收音频”“最终推理开始”“模型结束”等阶段性时序（`src/proximic_ring/asr/streaming.py:245,291,307`）。
- 文本处理层保留每次尝试和重试输出，编辑模式支持 fragment/full race（`src/proximic_ring/text_processing/llm.py:112,279`）。
- 编辑界面明确产生 preview、confirm、cancel、retry 事件（`src/proximic_ring/ui/controller.py:1753-1869`）。
- 输入注入和编辑覆盖集中在 desktop target 边界，未来可以加入结果观察（`src/proximic_ring/desktop_target.py:132,178,185`）。
- UI 日志已有真实时间戳，但历史仅保存在内存中的可读字符串，最多 80 条，不适合分析或训练（`src/proximic_ring/ui/controller.py:1924-1942,2026-2038`）。

### 1.2 当前最关键的数据缺口

1. **缺少持久化结构化事件**：现在的自然语言日志难以可靠关联同一次 utterance 的检测、ASR、LLM、预览和最终操作。
2. **输入模式缺少最终结果**：文本注入外部应用后，系统不知道用户是否立即手改、删除或撤销。
3. **编辑确认不是最终正确性的充分证据**：用户可能赶时间而确认，取消也可能是目标窗口失效而非模型不好。
4. **没有记录候选策略和选择概率**：未来无法进行反事实学习或无偏的 off-policy evaluation。
5. **没有区分错误来源**：ASR 识别错、用户自己说错、LLM 过度改写、目标定位错和注入失败目前容易混为一类。
6. **隐私边界不明确**：跨应用全文、窗口标题和原始音频可能包含高度敏感信息，不应默认上传或长期保留。

## 2. 文献证据：用户反馈确实能改善系统，但必须谨慎解释

### 2.1 交互数据可以揭示生成模型能力

CoAuthor 记录了 63 名作者、1,445 次写作会话的按键级事件，包括请求建议、接受、拒绝、光标移动和后续编辑，说明细粒度、可重放交互日志能够同时支持行为分析和模型能力评估。[CoAuthor 项目与 CHI 2022 论文](https://coauthor.stanford.edu/)

对 ProximicVoice 的启示是：一次语音请求不应只有“输入—输出”两列，而应保存完整事件轨迹和时间戳；否则无法研究用户是如何达到最终文本的。

### 2.2 用户修正可以通过记忆改善冻结模型

TeachMe 将用户纠正保存到动态记忆，在相似问题中检索作为额外 Context，无需重新训练基础模型；论文报告在模拟反馈和真实用户实验中随反馈积累而提升。[Towards Teachable Reasoning Systems, EMNLP 2022](https://aclanthology.org/2022.emnlp-main.644/)

Meetalk 将用户上传样例和编辑反馈整理成结构、章节分配和写作风格三类数据库，再检索到 LLM 生成中；真实会议场景的用户研究显示其在完整性、相关性和信任上优于基线。[Meetalk, KnowLLM 2025](https://aclanthology.org/2025.knowllm-1.9/)

TICL 展示了少于 10 个用户样例也可以通过 trial-error-explain 的 in-context prompt 做 tuning-free 个性化，不过其主要评价依赖自动或 LLM judge，不能直接等同于真实语音输入中的用户收益。[TICL, Findings of NAACL 2025](https://aclanthology.org/2025.findings-naacl.326/)

### 2.3 用户历史可以改善 ASR，而不必重训云端 ASR

PersonaLM 利用用户/领域历史做检索增强和 ASR N-best 二次重排，在其数据集上报告 5%-8% 的 WER 相对下降。它说明用户反复纠正的姓名、术语和长尾词可以形成个性化词汇记忆，并作用在 ASR 语言建模或后处理层，而不要求修改声学模型。[PersonaLM, Findings of EMNLP 2023](https://aclanthology.org/2023.findings-emnlp.757/)

对于无法获得 N-best 的云端 ASR，仍可采用更保守的方式：维护专名/热词库，把相关词随请求送入支持 hotword 的 ASR；或用用户词汇记忆约束 LLM 的 ASR 错词修复。不能把所有“最终文本与 ASR 不同”的片段都当作 ASR 错误，因为差异也可能来自用户想法变化或 LLM 风格整理。

### 2.4 接受/拒绝可作为反馈，但非常有噪声

Nifty 将用户未选择建议视为 one-shot implicit negative feedback，并使用 classifier guidance 改善后续写作生成，证明拒绝行为有学习价值。[Enhancing AI Assisted Writing with One-Shot Implicit Negative Feedback, EMNLP 2024](https://aclanthology.org/2024.emnlp-main.705/)

但一项对真实 Human-LLM 日志的研究发现，隐式反馈对理解用户很有价值，作为学习信号时却结果混合；只按正负极性训练可能退化，反馈内容和初始请求质量都会影响结果。[User Feedback in Human-LLM Dialogues, EMNLP 2025](https://aclanthology.org/2025.emnlp-main.133/)

因此，ProximicVoice 必须采用多证据合成：最终文本差异 > 明确失败原因 > 重说/撤销 > 确认/取消。不能构造“confirm=1，cancel=0”的简单训练集。

### 2.5 Contextual bandit适合从部署反馈选择策略

已有工作用 contextual bandit 从用户行为结果持续学习语言生成策略，并在真实用户交互中展示随时间改善。[Continual Learning for Grounded Instruction Generation, TACL 2021](https://aclanthology.org/2021.tacl-1.77/)

PURPLE 进一步指出，个性化记忆的语义相关性不等于对生成真正有用，并用 contextual bandit 优化应检索哪些用户记录。[PURPLE, ACL 2026](https://aclanthology.org/2026.acl-long.1467/)

这些证据支持将 ProximicVoice 的策略选择建模为：给定当前输入、场景和用户历史，选择一个可执行策略，使用户最终成本最小、质量最高。

### 2.6 语音编辑本身是多策略问题

语音编辑研究发现用户自然使用“命令修改”和“重新说正确片段”两类方式，复杂语义编辑更适合 re-dictation，单词级删除/替换更适合 command；合并两种策略优于单独使用。[Commanding and Re-dictation, TOCHI 2020](https://doi.org/10.1145/3390889)

Toward Interactive Dictation 进一步证明开放式听写与编辑命令的切分、理解存在显著准确率—延迟权衡。[Toward Interactive Dictation, ACL 2023](https://aclanthology.org/2023.acl-long.854/)

Tap&Say 则说明触摸/位置 Context 能显著改善语音编辑目标定位和用户效率，意味着选区/光标信息可能比增加更多 Prompt 规则更有价值。[Tap&Say, CHI 2025](https://doi.org/10.1145/3706598.3713376)

## 3. 应当收集哪些用户数据

### 3.1 设计原则

1. **事件优先，内容最小化**：默认采集行为事件和派生特征，不默认保留全文/音频。
2. **统一 episode**：一次靠近触发或按键说话到最终文本稳定，视为一个 episode；所有事件使用同一 `episode_id`。
3. **双时钟**：保存 UTC/本地 wall-clock 用于跨日志关联，同时保存 monotonic elapsed time 用于延迟计算。
4. **可重放但不必可复原隐私文本**：研究模式允许保留经同意的内容；产品默认模式只保留差异统计、哈希或本地记忆。
5. **记录决策概率**：任何会用于 bandit 学习的策略选择都要保存 action、候选集合、policy version 和 propensity。
6. **反馈归因**：记录生成结果之后发生的动作，并尽量获得最终文本或失败原因。

### 3.2 核心事件表

#### A. Episode与Context

- `episode_id`, `participant_id`（随机伪匿名）, `session_id`, `request_id`
- `day_index`, `cumulative_episodes`, `local_timestamp`, `monotonic_ms`
- `mode`: dictation/edit
- `activation`: proximity/manual
- `app_category`: chat/email/document/search/code/other；不要默认保存窗口标题
- `target_available`, `selection_available`, `target_length_bucket`, `selection_length`
- `context_hash` 或仅派生特征；研究模式经同意后才保留 target text

#### B. 音频与检测派生特征

- utterance duration, pre-roll duration, detected onset/end, reject count
- RMS、peak、估计 SNR、clipping ratio、静音占比
- pause count、最长停顿、停顿位置比例、speech rate
- Stage2 activate 到 ASR start 的延迟
- audio gain、encoding、采样率、设备/固件版本
- 原始音频路径仅在明确 Research Mode、单独同意和限期保留时记录

#### C. ASR事件

- backend/model/version、hotword/profile version
- partial 文本序列或仅 partial revision count / stability curve
- final transcript、置信度、N-best（后端若提供）
- start/final/end timestamps、duration、error/reconnect reason
- ASR final 与用户确认最终文本之间的 alignment features

#### D. LLM与策略事件

- provider/model/version、prompt_id/prompt_version、tool schema version
- action candidates、selected action、selection probability
- execution: none/minimal/full/fragment/race
- context size、retrieved memory IDs、输出 token 数、timeout
- 每个候选的 raw output、validation result、latency、retry reason
- applied final candidate、diff size、关键实体/数字/否定词变化

#### E. 用户结果

- preview shown、confirm、cancel、retry、undo、manual correction
- time to first preview、review time、time to acceptable text（TTAT）
- final stable text 或经同意的局部 diff
- candidate→final 的 character/word edit distance
- correction reason（抽样询问）：ASR错 / LLM整理错 / 目标位置错 / 用户改主意 / 注入错误 / 其他
- explicit rating（只在少量抽样 episode）：满意度、是否保持“我的表达”、是否愿意以后采用同样策略

### 3.3 隐私分层

#### Level 0：默认产品遥测

仅保存延迟、长度、模型版本、动作、错误码、confirm/cancel/retry/undo、diff 大小等非内容特征。本地保存；上传需单独开关。

#### Level 1：本地个性化

在设备本地保存接受样例、用户词汇和风格偏好；云端请求只发送当前任务需要的两三个检索结果。用户可查看、删除、暂停学习。

#### Level 2：研究文本

参与研究并单独同意后，保存 ASR 文本、目标局部 Context、候选和最终文本。自动屏蔽或标记邮箱、电话、身份证号等明显敏感字段；仍需人工数据管理规范。

#### Level 3：研究音频

只对明确的研究队列开启。原始音频与文本分库存储、单独密钥、限定人员、限定保存期限；参与者可撤回。不要将常规用户的全量跨应用音频作为默认训练数据。

ACM 对涉及人的研究要求最小化伤害、保护隐私与自主权、知情同意，并遵守机构和当地伦理审查。[ACM Research Involving Human Participants Policy](https://www.acm.org/publications/policies/research-involving-human-participants-and-subjects)

## 4. 如何获得“最终用户结果”

这是方案成败的关键。当前编辑模式有明确确认，但输入模式注入后没有 final outcome。

### 4.1 受控研究：使用可完全记录的实验编辑器

第一阶段论文研究应提供一个 instrumented editor，像 CoAuthor 一样记录插入、删除、光标、选区、撤销和时间戳。它能可靠得到：

`ASR raw → LLM candidate → user final`

受控编辑器中的高质量数据用于训练/验证；跨应用 field telemetry 用于检验生态有效性。这样不必一开始在所有 Windows 应用中实现危险的全局键盘记录。

### 4.2 真实跨应用：三种低风险观察

1. **快速撤销窗口**：注入后 8-15 秒显示非打扰式撤销入口；undo 是强负反馈。
2. **下一次相关操作关联**：用户立即进入编辑模式、重说相似内容或删除上一结果，视为隐式失败，但只作为弱标签。
3. **研究模式的局部结果快照**：经同意后，在目标仍然有效且用户触发下一次操作时读取同一字段，计算与注入片段附近的局部 diff，立即丢弃无关全文。当前 `capture_text` 会 Select-All/Copy 并扰动焦点，因此产品化之前应优先实现 Windows UI Automation TextPattern/ValuePattern 或专用插件，而不是后台轮询剪贴板。

不建议安装全局 keylogger，也不建议持续复制所有前台文本框。

### 4.3 抽样询问错误原因

系统无法从 diff 自动判断用户为何修改。例如“周五→周六”可能是 ASR 错，也可能是用户改主意。建议只对 5%-10% 的负面 episode 弹出一键原因：

- 没听对；
- 整理得不对；
- 改错位置；
- 我改变了想法；
- 操作没有正确执行。

这少量高质量标签比大量模糊点击更适合训练和论文分析。

## 5. 数据如何反哺输入法

### 5.1 第一层：无需训练的即时记忆

维护三类本地 memory：

1. **Lexicon Memory**：被用户纠正并重复出现的人名、术语、缩写、常见同音错误。
2. **Style Memory**：不同 app_category 下用户接受的整理程度、句长、口语保留、礼貌和标点偏好。
3. **Repair Memory**：相似输入中失败的候选、用户最终改法和失败原因。

生成时只检索当前场景最相关的 2-3 条，编译成简短 Prompt。检索目标应是“对生成有效”，而不是只有语义相似性，这一点由 Pearl 和 PURPLE 的结果支持。[Pearl, CustomNLP4U 2024](https://aclanthology.org/2024.customnlp4u-1.16/)

### 5.2 第二层：ASR个性化

在可用能力范围内按优先级实施：

1. 从明确 ASR correction 提取 user lexicon，更新 Fun-ASR hotwords。
2. 若 ASR 提供 N-best/置信度，使用用户词汇和当前 app/topic 对 N-best 重排。
3. 若云端 ASR 不提供这些接口，在 LLM 整理 Prompt 中加入“可能的用户词汇”，但禁止模型无证据地替换普通词。
4. 原始音频和最终文字足够多时，才考虑对可控的本地 ASR 做 speaker/domain adapter；这不是第一篇论文的必要条件。

### 5.3 第三层：检索式 LLM 个性化

封闭 LLM 不需要微调。中间系统生成内部策略：

```json
{
  "execution": "fragment",
  "prompt_id": "edit_minimal_v3",
  "context_scope": "selection_plus_neighbors",
  "memory_ids": ["m_13", "m_27"],
  "timeout_ms": 4500,
  "preview": true
}
```

Prompt Compiler 再把它转成少量 token：当前任务的一句话规则、所需 Context、相关用户样例。用户 embedding 不直接进入云端 LLM；它只影响选择了哪些 token、模型、超时和交互策略。

### 5.4 第四层：策略价值模型

不要训练枚举所有人类行为的分类器。部署模型预测每个可执行 action 的预期效用：

\[
Q_\theta(x_t,u,a)=\mathbb{E}[R_t\mid x_t,u,a]
\]

其中 `x_t` 包含 ASR 文本 embedding、停顿/置信度、目标/选区、app_category 和历史；`u` 是用户状态；`a` 是候选策略。

候选策略可以是：

- raw ASR；
- minimal cleanup；
- semantic compose；
- fragment edit；
- full edit；
- fragment/full race；
- ask/preview；
- 不同 timeout 与 context scope 的组合。

模型结构：冻结的预训练文本 encoder + 数值/类别特征 MLP + 用户表示 + action 表示 + 单一 utility scorer。输出一个标量，不需要大量分类头。

个性化采用 population prior + user residual：

\[
e_u=e_{population}+\Delta e_u
\]

新用户使用全局策略；随着数据增加，只更新低维用户向量或 per-user bandit posterior，不在线微调整个 encoder，更不微调大 LLM。

### 5.5 奖励不应只是一项点击

建议奖励由多项结果构成：

\[
R=TaskSuccess-\lambda_1 TTAT-\lambda_2 ManualEdit-\lambda_3 Retry-\lambda_4 CriticalError-\lambda_5 Intrusion
\]

其中：

- TaskSuccess：最终是否保留并完成任务；
- TTAT：从说话开始到最终稳定文本；
- ManualEdit：候选到最终文本的编辑成本；
- Retry/Undo：修复成本；
- CriticalError：姓名、数字、日期、否定词等；
- Intrusion：不必要预览、打断或询问。

实际权重通过 pilot 和用户偏好校准。研究报告中同时呈现各分量，不应只报一个不透明的综合分。

## 6. 训练与部署路线

### Phase 0：研究基础设施（先做）

- 新建 `telemetry/`，使用 append-only JSONL 或 SQLite；UI 线程只发事件，由后台 writer 落盘。
- 建立 `EpisodeRecord`、`CandidateRecord`、`OutcomeRecord` 和 schema version。
- 给每个模型、Prompt、tool schema、policy 赋版本号。
- 增加 Research Mode、内容级别、音频级别、导出和删除入口。
- 输入模式增加快速撤销；研究编辑器记录最终改写。
- 不启用在线自适应，先验证日志完整性和隐私流程。

### Phase 1：数据与可解释基线

- 先收集约 1,000-3,000 个高质量 episode，覆盖不同用户和场景。
- 对负面样本抽样标注错误来源；双人编码行为与修复策略并报告一致性。
- 建立固定策略、规则 router、文本 embedding + LightGBM utility scorer 等基线。
- 用 leave-one-user-out 衡量对新用户泛化；按时间切分衡量未来 episode，禁止随机打散造成历史泄漏。

### Phase 2：动态记忆与全局适应

- 部署 Lexicon/Style/Repair Memory；比较无记忆、语义检索、utility-calibrated 检索。
- 训练全局 `Q(x,a)`，先 offline replay，再在低风险 action 中小比例随机探索。
- 每次记录 candidate set、chosen action、propensity 和 outcome，支持 inverse propensity/off-policy 分析。
- 高风险任务（数字、专名、否定、全局覆盖）不探索自动提交，只能探索模型/Prompt并强制预览。

### Phase 3：个性化适应

- 冷启动使用 population prior；10-20 次后建立检索记忆；50 次后再更新低维用户向量/bandit posterior。
- 偏好按 app_category 分层，带时间衰减，防止把聊天偏好错误迁移到邮件或搜索。
- 为用户提供“系统学到了什么”页面和删除某条记忆能力。
- 只在累积大量高质量 paired data 后，将微调本地小编辑模型作为独立后续工作。

## 7. 最有希望的论文方案

### 7.1 论文主张

**题目候选**：From Speech Logs to Personal Policies: Feedback-Adaptive Semantic Voice Input with Frozen LLMs

核心贡献：

1. 一个真实语音输入/编辑的、多层反馈 interaction dataset 与行为发现；
2. 一个无需微调 LLM、结合动态记忆与个性化策略价值模型的系统；
3. 受控和纵向证据，说明反馈适应如何影响 TTAT、错误、用户声音和使用经验。

### 7.2 研究问题

- **RQ1**：哪些可观察反馈能可靠预测用户是否接受以及最终如何修正 LLM 语音文本？
- **RQ2**：基于真实结果的全局策略适应，是否优于固定 Prompt？
- **RQ3**：个人历史是否在全局适应之上进一步降低 TTAT 和修复成本？这种收益随使用量如何变化？
- **RQ4**：用户与系统是否共同适应；个性化是否减少机器化表达，还是导致用户过度信任和声音趋同？

### 7.3 Study A：形成性纵向观察

- 约 15-20 名参与者，7-14 天；最终人数由资源和饱和度决定。
- 使用固定基线系统，覆盖聊天、邮件、文档、搜索、局部/全局编辑。
- 收集结构化 episode、最终文本、抽样原因和半结构访谈。
- 使用 thematic analysis + sequence analysis + mixed-effects descriptive models。
- 产出反馈 taxonomy、数据质量评估、设计需求和训练集。

### 7.4 Study B：受控系统评估

三个主要条件：

1. Fixed LLM：当前固定 Prompt/race；
2. Global Adaptive：共享 utility router + dynamic memory；
3. Personalized Adaptive：Global + user memory/embedding。

被试内平衡设计，任务覆盖短输入、边想边说、精确局部修改、re-dictation 和全局风格修改。样本量必须在 pilot 后按主要指标效应量做功效分析，不应把任意 N 当作保证。

主要指标：TTAT；次要指标：目标/范围准确率、关键错误、candidate→final edit distance、retry/undo、NASA-TLX、信任校准和 perceived authorship。

### 7.5 Study C：真实使用中的学习曲线

- 约 24-36 人、2-3 周作为强版本；资源不足时可缩短，但应诚实限制结论。
- 第一阶段固定策略收集个人历史；第二阶段在安全范围内随机交错 global/personalized 策略。
- 混合效应模型：固定效应包括 condition、day、task/app、interaction count；随机效应包括 participant 和 task。
- 画出 0/10/25/50/100 次交互后的 TTAT、接受率和人工修改曲线。
- 分析用户语言是否随系统变化：句长、停顿、自我修正、机器化简化、指代和命令形式。

### 7.6 必要消融与基线

- text-only vs text + pause/ASR dynamics vs text + context；
- no-memory vs semantic retrieval vs utility-calibrated retrieval；
- fixed policy vs global router vs personalized router；
- click-only reward vs final-text-grounded reward；
- current fragment/full race vs learned routing；
- oracle best-of-candidates 用来估计可提升上限，但不能作为真实部署基线。

### 7.7 最有价值的结果，即使模型增益不大

论文不应依赖“所有指标显著提高”。以下结果也有贡献：

- 证明 implicit click feedback 对复杂语音编辑不可靠，并量化何时可靠；
- 发现个性化主要改善场景/策略选择，而不是 ASR WER；
- 发现用户随系统适应改变表达，导致离线模型收益不能直接转化为 field benefit；
- 发现 personalized memory 提高速度但降低 perceived authorship，需要风险/所有权校准。

这使论文从单纯的系统 benchmark 提升为 HCI 的人—AI共同适应研究。

## 8. 威胁、替代解释与限制

1. **用户确认偏差**：接受可能表示“够用”，不代表偏好；必须结合最终文本与抽样反馈。
2. **系统与用户共同变化**：用户学会迁就系统会让指标改善，不能全部归因于模型；纵向分析需同时建模使用天数和表达变化。
3. **Context泄漏**：窗口标题和全文高度敏感；默认只保存 app category 与长度桶。
4. **反事实缺失**：只看到被选策略结果；研究期需安全随机化或 shadow candidates，并记录 propensity。
5. **模型版本漂移**：封闭 LLM 升级会改变行为；所有请求必须记录 provider/model/prompt version。
6. **自动指标不足**：LLM judge 对个性化和用户声音可能失真；主要结论必须包含真实用户结果。
7. **样本代表性**：早期参与者可能是技术熟练用户；需报告经验、输入习惯和场景分布。
8. **中文特性**：字符级编辑距离不能充分体现同音字、专名和语义；需要关键实体与语义保真标注。
9. **隐私影响行为**：知道音频/文本被记录可能改变表达；应比较不同记录级别并报告研究反应性。

## 9. 推荐的近期实现顺序

1. 先实现结构化 Event Store、schema/version、Research Mode 和删除/导出；不做训练。
2. 为输入模式增加可撤销反馈，建设 instrumented study editor；解决最终结果标签。
3. 实现本地 Lexicon/Style/Repair Memory 与两三个相关样例的 Prompt Compiler。
4. 先用规则和 LightGBM 做 utility router，验证可解释收益。
5. 收集到足够数据后，再训练共享 encoder + action/user embedding scorer。
6. 最后引入 conservative contextual bandit 和用户向量在线更新；高风险修改始终预览。

## 证据台账

| 关键主张 | 主要来源 | 置信度与适用边界 |
|---|---|---|
| 细粒度可重放交互日志可支持人机写作行为与模型能力分析 | CoAuthor, Lee et al., CHI 2022, https://coauthor.stanford.edu/ | 高；英文写作，不是语音输入 |
| 冻结模型可通过动态用户纠正记忆持续改善 | TeachMe, Dalvi Mishra et al., EMNLP 2022, https://aclanthology.org/2022.emnlp-main.644/ | 中高；QA场景，需验证迁移到文本编辑 |
| 编辑反馈和结构化记忆可改善个性化 ASR+LLM 应用 | Meetalk, Chen et al., 2025, https://aclanthology.org/2025.knowllm-1.9/ | 中；会议总结、小规模场景研究 |
| 少量样例可做 tuning-free 个性化 | TICL, Cho et al., Findings NAACL 2025, https://aclanthology.org/2025.findings-naacl.326/ | 中；自动评价占比较高 |
| 用户/领域历史检索可改善 ASR LM/N-best 重排 | PersonaLM, Mathur et al., Findings EMNLP 2023, https://aclanthology.org/2023.findings-emnlp.757/ | 高（其任务内）；依赖 ASR 接口能力 |
| 拒绝建议可作为隐式负反馈 | Nifty, Towle & Zhou, EMNLP 2024, https://aclanthology.org/2024.emnlp-main.705/ | 中高；不能推出所有拒绝都表示质量差 |
| 真实 implicit feedback 是有噪声的，简单正负学习可能退化 | Liu et al., EMNLP 2025, https://aclanthology.org/2025.emnlp-main.133/ | 高；直接支持多证据反馈设计 |
| Contextual bandit 可从真实行为结果持续改善策略 | Kojima et al., TACL 2021, https://aclanthology.org/2021.tacl-1.77/ | 中高；语言生成任务，不是输入法 |
| 记忆的语义相似性不等于对生成有用，可按效用优化检索 | PURPLE, Du et al., ACL 2026, https://aclanthology.org/2026.acl-long.1467/ | 高（离线任务）；真实用户验证仍有限 |
| 命令和 re-dictation 是自然且互补的语音编辑策略 | Ghosh et al., TOCHI 2020, https://doi.org/10.1145/3390889 | 高；直接相关 |
| 开放式听写/命令存在显著准确率—延迟权衡 | Li et al., ACL 2023, https://aclanthology.org/2023.acl-long.854/ | 高；直接相关但非 LLM field study |
| 位置/触摸 Context 可提升语音编辑定位与效率 | Tap&Say, Zhao et al., CHI 2025, https://doi.org/10.1145/3706598.3713376 | 高；智能手机触摸，需迁移为桌面选区/光标 |
| 人体研究需要知情同意、隐私、自主与伦理合规 | ACM Publications Policy, https://www.acm.org/publications/policies/research-involving-human-participants-and-subjects | 高；还需遵守所在机构与当地法规 |

## 研究停止条件与未决问题

本轮检索覆盖了语音编辑、交互日志、ASR 个性化、冻结模型记忆、隐式反馈、contextual bandit 和研究伦理。新搜索结果已经主要重复“记忆/检索/反馈学习”的既有机制，继续广搜不太可能改变推荐架构，因此停止。

仍需在实施前解决：研究伦理审批与招募渠道；目标投稿期和资源预算；Windows UI Automation 对不同应用的覆盖；云端 ASR 是否能提供 N-best/置信度/hotword；是否允许保存研究文本和音频。这些不影响先建设事件基础设施和 instrumented editor。
