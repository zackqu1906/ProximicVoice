# Windows 实时语音输入（安全原型）

当前桌面输出采用“实时预览、最终提交”的方案：

1. 流式 ASR 的 `partial` 结果实时显示在屏幕底部悬浮窗。
2. 悬浮窗不获取键盘焦点，不影响当前正在编辑的应用。
3. 一轮识别结束后，仅将 `final` 结果输入到当前焦点输入框。
4. 文本通过 Windows Unicode 键盘事件输入，不读取或覆盖剪贴板。

该功能默认关闭，因此不会改变已有的检测和 ASR 命令。

## 运行

先把光标放在记事本、浏览器、聊天软件或编辑器的输入框中，再靠近戒指说话：

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --asr streaming_sensevoice `
  --streaming-sensevoice-repo ".\third_party\streaming-sensevoice" `
  --asr-device cuda:0 `
  --asr-language zh `
  --desktop-output `
  --push-to-talk
```

自动靠近检测保持不变。启用 `--push-to-talk` 后，在任意应用中按住
`Ctrl+Alt+Space` 会强制开始或保持当前语音会话；松开按键后，结束权重新交给
ProxiMic 的连续 reject、Stage1 inactivity 和最大时长逻辑。快捷键不会创建第二套
ASR，也不会关闭自动识别。

自动触发仍使用 `--asr-pre-roll` 补偿 CNN 判断延迟；按键触发是即时的，只从按键被
检测到的当前音频块开始，不会把按键前的 1 秒环境声音带入结果。

如果同时运行多个 ASR，必须明确哪一个结果用于输入，避免一段话提交两次：

```powershell
... --asr streaming_sensevoice --asr volcengine `
  --desktop-output --desktop-output-backend volcengine
```

## 当前边界

- 只有 `final` 会写入输入框；`partial` 仅用于实时预览，避免流式回改导致重复或误删。
- 目前支持 Windows 普通输入控件。非管理员进程不能向“以管理员身份运行”的应用注入输入。
- 少数游戏、密码框、远程桌面或自绘控件会主动拒绝模拟输入。
- 悬浮窗只是轻量原型，不包含快捷键、历史记录和 AI 编辑功能。

下一阶段若要求文字在输入框内部随 `partial` 实时修订，应接入 Windows TSF/IME composition，而不是连续粘贴 partial 文本。
