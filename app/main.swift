import AppKit

// MARK: - App

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var controller: Controller?

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let snapshotPath {
            Task { @MainActor in exit(await runSnapshot(to: snapshotPath)) }
            return
        }
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Home Screens", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)
        NSApp.mainMenu = main

        controller = Controller()
        controller?.window.makeKeyAndOrderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

/// HOME_SCREENS_SNAPSHOT=out.png, or the argument `--snapshot out.png` (which survives `open --args`), renders the window offscreen from fixture data, once light and once dark
/// (out-light.png, out-dark.png), and exits. It reads no device and runs no launchpad-map command.
let snapshotPath: String? = {
    let args = CommandLine.arguments
    if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count { return args[i + 1] }
    return ProcessInfo.processInfo.environment["HOME_SCREENS_SNAPSHOT"]
}()

let app = NSApplication.shared
let delegate = MainActor.assumeIsolated { AppDelegate() }
app.delegate = delegate
app.setActivationPolicy(snapshotPath == nil ? .regular : .prohibited)
app.run()
