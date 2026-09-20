import Foundation

/// Keep the transaction's full reason intact; summarize only its presentation.
struct ActionPanelPresentation {
    let title: String
    let detail: String?
    let isError: Bool

    init(phase: CompositionSession.Phase, error: String, empty: Bool = false,
         editRequested: Bool = false, readableRange: Bool = false) {
        let reason = error.trimmingCharacters(in: .whitespacesAndNewlines)
        if !reason.isEmpty {
            title = Self.summary(reason)
            detail = reason
            isError = phase == .error || !(phase == .interrupted && Self.normalInterruptions.contains(reason))
            return
        }
        detail = nil
        isError = phase == .error
        switch phase {
        case .listening: title = empty ? "听写 · 可以说话" : "听写中"
        case .finishing: title = editRequested ? "编辑 · 正在收尾" : "听写 · 正在收尾"
        case .dictated: title = empty ? "未识别到语音" : "已听写"
        case .editing: title = "正在编辑…"
        case .edited: title = readableRange ? "已编辑可读原文" : "已编辑"
        case .undone: title = "已撤销"
        case .interrupted: title = "本句已结束"
        case .error: title = "操作失败：未返回具体原因"
        }
    }

    private static let normalInterruptions: Set<String> = [
        "鼠标操作已接管本句", "键盘输入已接管本句", "已切换输入框",
        "输入法已重新激活", "已切换输入法或输入框", "输入会话已关闭",
        "应用结束了当前输入组合", "主程序已结束本句", "开始了新一句"
    ]

    static func summary(_ reason: String) -> String {
        // Match specific failures before more general ones. Never claim a
        // particular root cause when the client only reports alternatives.
        let replacements: [(String, String)] = [
            ("按键控制权限", "按键控制权限未生效"),
            ("辅助功能权限", "辅助功能权限未生效"),
            ("输入框未确认编辑结果或末尾光标", "编辑结果或光标未确认"),
            ("输入框未确认本句选取或撤销结果", "选取或撤销结果未确认"),
            ("输入框未确认选区或替换结果", "选区或替换结果未确认"),
            ("输入框未确认本次文字更新", "输入框未确认文字更新"),
            ("输入框尚未确认本句撤销", "输入框未确认撤销结果"),
            ("本句文字或光标已变化", "文字或光标已变，无法撤销"),
            ("光标或选区已变化", "光标或选区已改变"),
            ("输入框内容已变化", "输入框内容已改变"),
            ("输入框原文已变化", "原文已改变，未替换"),
            ("当前正文在读取期间发生变化", "原文读取时发生变化"),
            ("当前选区未包含刚才的口述指令", "选区未包含本句编辑指令"),
            ("未定稿文字已由应用修改", "未定稿文字被应用修改"),
            ("输入法连接已断开", "输入法连接已断开"),
            ("输入法交接未完成", "输入框尚未就绪"),
            ("输入法未确认定稿", "输入法未确认定稿"),
            ("输入框未提供完整上下文", "缺少完整原文，无法撤销"),
            ("输入框未提供原选中文字", "无法读取原选中文字"),
            ("当前输入框无法校验本句文字", "无法校验本句，暂不能撤销"),
            ("恢复听写的光标仍未稳定", "恢复听写后光标未稳定"),
            ("请在文字还有下划线时右滑", "请在未定稿时转编辑")
        ]
        if let item = replacements.first(where: { reason.contains($0.0) }) { return item.1 }
        let line = reason.components(separatedBy: .newlines).first ?? reason
        let sentence = line.components(separatedBy: "。").first ?? line
        let result = sentence.trimmingCharacters(in: .whitespacesAndNewlines)
        return result.count > 24 ? String(result.prefix(23)) + "…" : result
    }
}
