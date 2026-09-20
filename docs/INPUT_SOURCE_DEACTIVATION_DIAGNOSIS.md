# 拼音切入 ProxiMic 后立即停用：调用链诊断

日期：2026-09-20。实测系统：macOS 15.7.5 (24G624)，Apple Silicon。

## 结论

外部后台辅助进程调用 `TISSelectInputSource` 的路径，在本机可重复触发：前台应用的 HIToolbox 先激活目标 IMK 会话，再经 `utHideBackgroundPalettes` 停用同一个会话。输入源标识仍为 ProxiMic。约 4–5ms 是两段系统调用的执行间隔，不是本项目的等待超时；不能用删除选区校验、延长等待或忽略旧 sender 来修复这一分支。

这是本机独立 AppKit 文本框实验的直接调用栈证据，与用户故障的 activate→deactivate 顺序吻合。未把调试代码注入 Codex/微信。没有源码证据能进一步解释苹果内部选择此清理分支的判断条件，不能把函数名解读为已证明“系统把前台窗口误判成后台”。

## 用户故障时间线

系统统一日志 16:27:30.432873：ProxiMic Activate Server；30.436083：Set value for tag；30.437154：Deactivate Server。主程序源切换成功。原生准备定时器尚未执行、未读选区、未开始 ASR。当前应用 PID 444，bundle `com.openai.codex`（本机可执行程序显示为 ChatGPT）。当前应用同时更新 AppleKeyboardInputMethodsPasteboard 与 AppleKeyboardLayoutOverridePasteboard；仅这份日志的先后关系不足以证明调用原因。

## 独立复现与对照

使用自己的 NSTextView 窗口，切换时验证窗口仍在前台；离开窗口即中止。只在诊断进程里拦截 IMKInputSession 的 activate/deactivate 并记录调用栈，未修改目标程序或系统库。私有类只用于取证，不能进入产品实现。

在同一窗口交替运行 8 轮：

- 4 轮前台进程自身调用 `TISSelectInputSource`：目标会话的 `didActivate=true`，均保持激活。
- 3 轮已建立会话后，由生产后台辅助进程调用相同 API：均经过 `utHideBackgroundPalettes`，目标会话随后 `didActivate=false`。
- 第 1 轮冷启动没有捕获到会话对象，不纳入活性成功/失败统计。
- 全部取样时窗口 `isKeyWindow=true`、应用 `isActive=true`，current input source 和 NSTextInputContext selected source 都显示 ProxiMic。

第 3 轮同一个会话对象的关键栈：

```text
16:37:27.605377 activate
TSMMessagePortCallBack
  → _TSMSetInputSourceSelected +1204
  → utOpenActivateThisDocsInputMethod
  → utActivateIM4Document
  → IMKInputSessionActivate

16:37:27.609383 deactivate（间隔 4.006ms）
TSMMessagePortCallBack
  → _TSMSetInputSourceSelected +1444
  → utHideBackgroundPalettes
  → utDeactivateIMforDocument
  → DeactivateInputMethodInstance
  → IMKInputSessionDeactivate
```

这两次操作属于同一轮切换处理，且针对同一个会话对象，不是上一句话的旧取消任务。

辅助进程存活对照：切换后额外运行 RunLoop 400ms。后台清理停用于 16:39:03.079537；辅助进程退出于 03.477734，仍能复现。排除了辅助进程过早退出；该轮与前台进程对照仍呈现 inactive / active 差异。

## 修复约束

应更换“后台进程直接 TISSelectInputSource”这一入口，验证系统原生输入源选择入口，而不是继续增加等待、ABC 往返或屏蔽真实停用。不能要求第三方应用执行我们的代码；前台进程直接调用只用于因果对照，不是可发布方案。具体替代入口仍需独立验证成功率、焦点与输入源是否正确。

本次只做诊断与保存证据，未修改或安装生产组件、未构建 DMG。上一版本已删除 ABC 往返恢复，但仍不能宣称自动切换已修复。

证据：`experiments/ime_activation_20260920/paired-events.jsonl`、`keepalive-events.jsonl`。实验程序包含本机诊断路径，不参与安装包构建。完整系统日志保留于 `/private/tmp/proximic-tap-start/deactivate-162730-all.json`，不复制其他应用的系统日志进项目。

## 系统菜单替代入口实测（同日后续）

在同一独立测试窗口中，比较辅助功能选择系统输入法菜单和前台进程调用 TIS 的对照。TextInputMenuAgent 的 AXExtrasMenuBar 能读取到 ProxiMic Voice、拼音和 ABC 项；辅助进程具有辅助功能权限。

- 直接 AXPress 隐藏菜单项：辅助程序约 98–107ms 返回，动作本身不足 1ms，但实际输入源仍是拼音。不能把此值当作成功切换延迟。
- AXPress 展开菜单再选目标项：接口返回成功，实际菜单未确认展开，输入源未改变。加入展开和切换超时后约 810–822ms，仍失败。这是等待超时，不是正常操作耗时。
- 用已有权限的 Python 运行时重复、尝试菜单项 AXPick，结果相同。
- 用 AX 报告的图标位置发送原生鼠标点击也未确认切换，而且改变了指针位置。不满足无干扰要求。
- 交替的前台 TIS 对照仍可切换并保持目标会话激活。

结论：目前没有得到一次可用于评估延迟和视觉效果的成功菜单切换，不能断言辅助功能方案与直接 API 耗时相近，也不能把 AX 返回成功当成切换成功。尚未定位菜单无效的原因；不据此概括所有 Mac 的行为。本轮未将替代路径接入生产代码、未安装更新、未构建 DMG。

脱敏结果（仅自建测试窗口元数据）：`experiments/ime_activation_20260920/menu-route-observations.jsonl`。

## 上游资料核对（2026-09-20）

### 类似故障有公开先例

- Squirrel issue #1162（2026-07-26，查看时仍 Open）：报告在 macOS 26.5.2 上，外部进程 TISSelectInputSource 切入后，source ID 正确但没有输入候选；菜单栏手动切换正常，切往另一个应用再回来恢复。报告者明确表示没有直接记录 activateServer 未调用，因此其“未激活”是现象推断。与本机 macOS 15.7.5 的 activate→deactivate 直接调用栈不能等同，但支持“输入源选择成功不等于客户端可用”的判断。
  https://github.com/rime/squirrel/issues/1162
- Squirrel PR #1142（2026-06-12 已合并）处理相反方向：程序化切走未发送 deactivateServer，旧组合残留。监听输入源改变后补做 deactivateServer 清理。它不修复切入激活，不能把已合并状态当作 #1162 已解决。
  https://github.com/rime/squirrel/pull/1142
- Squirrel 当前 controller 的 deactivateServer 隐藏候选、提交组合、清除 client；没有在该方法里无条件重新调用 activateServer。
  https://raw.githubusercontent.com/rime/squirrel/master/sources/SquirrelInputController.swift

### 已存在的切换恢复实现

macism 的 InputSource.select 对 CJKV 源先 TISSelectInputSource，再 showTemporaryInputWindow。WindowUtils 创建屏幕角落 3×3 的 titled 窗口，使辅助应用成为前台，等待后退出。借真实焦点往返让原应用重新获得输入上下文；不是直接修改原应用 IMK 状态，也不是点击系统菜单。窗口及激活可能有视觉/焦点副作用，不能称为完全无感。

当前代码默认等待 150ms；README 给出的 macOS 26 实验称较短等待仍可能漏首字。这是上游经验、不是本机性能结果；150ms 也不是包含进程启动在内的总延迟。不得把它直接作为产品延迟承诺。

- https://github.com/laishulu/macism
- https://raw.githubusercontent.com/laishulu/macism/master/InputSourceManager.swift
- https://raw.githubusercontent.com/laishulu/macism/master/WindowUtils.swift

后续优先实验：在自建测试窗口复现失活，再用独立辅助窗口一次焦点往返，验证是否收到新激活、原控件/选区是否恢复、未定稿更新与定稿是否实际成功；测总耗时及闪烁/全屏副作用。如果采用，应只用于需要激活恢复的切换请求，禁止持续循环或覆盖用户主动切换前台。仍然保留当前有效输入会话的快速路径。研究阶段未实施此路径。

## 已实现焦点恢复（2026-09-20 后续）

用户确认后，将焦点恢复放入 InputMethodAdmin 独立进程。自动切换到 ProxiMic 后，以透明、无阴影、忽略鼠标的 1×1 辅助窗口取得焦点，停留 150ms，返回原 PID，等待原应用连续处于前台 50ms 后报告完成。使用完整 NSApplication 事件循环。没有调用 AX、注入系统键盘事件、操作剪贴板或切换到 ABC。辅助窗口使用 accessory 策略，不新增 Dock 图标。

主程序只允许当前辅助进程 PID 短暂占用焦点，不放宽其他应用的保护。用户切走、再次 Tap 取消或启动超时会结束这次请求。辅助进程监测父进程存活及第三方前台切换，不抢回第三方应用焦点。收到系统选中确认后仍通过原有 BEGIN ACK 开始 ASR，不直接伪造 IME 激活状态。

已有正常会话保持快路径。如果已选中但新 ping 明确未就绪，调用 refresh-selected 只恢复一次焦点；这条恢复不会覆盖用户新选择的其他输入法。每次起句最多恢复一次，不循环重试。这里只更新辅助程序和 Python 主机，无需替换当前加载的 IME app；未构建 DMG。

验证：

- 原生 source selection 8 个场景通过；Python 启动/取消/旧回复隔离、流水线和界面回归分进程通过。QML 必须单独在 QApplication 进程运行，混入 QCoreApplication 测试进程会跳过。
- 独立系统 NSTextView 连续 10 句：30 次 partial、10 次定稿、10 次撤销还原选中文本通过。请求开始至 listening 376–416ms，包含辅助程序启动与 IME 确认，不含 ASR 首包。
- 使用与主程序相同 QApplication 的 6 句补测通过，包含已就绪快路径、预先制造的已选中失活，以及恢复期间取消后下一句。快路径 81–82ms；需切换/恢复时 387–418ms。
- 初次选区测试曾发现原文减少；后续加入开始前后完整原文/选区断言。另一次测试在开始前检测到额外键盘输入，按断言中止。上述最终 10+6 句均通过，但不能据此声称所有应用、全屏/多屏场景零故障。

测试仅操作自建文本框，未自动控制 Codex；微信和 Codex 仍需用户实际 Tap 验证。保存的摘要与测试源码在 `experiments/ime_activation_20260920/focus-recovery/`；不包含其他应用文本。
