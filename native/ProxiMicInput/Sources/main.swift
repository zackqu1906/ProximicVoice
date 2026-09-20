import AppKit
import InputMethodKit

let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let connectionName = Bundle.main.object(forInfoDictionaryKey: "InputMethodConnectionName") as? String ?? "ProxiMicInput_Connection"
let identifier = Bundle.main.bundleIdentifier ?? "com.proximic.inputmethod.ProxiMicVoice"
let inputServer = IMKServer(name: connectionName, bundleIdentifier: identifier)
IMEService.shared.start()
application.run()
withExtendedLifetime(inputServer) {}
