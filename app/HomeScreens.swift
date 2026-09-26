// Home Screens: the launchpad-map history page in a window, with the commands
// that feed it one click away. It shows one device at a time (the Mac's
// Launchpad, the phone, the tablet), scans the chosen one, applies a proposal
// to it after asking, and for the Android devices says whether a scan can run.
//
// Every action is a `launchpad-map` command run from the repo this app was
// bundled from; the app reimplements none of them.

import AppKit
import WebKit

// MARK: - Where things are

/// The launchpad-map checkout, recorded in Info.plist by bundle.sh.
let repo: URL = {
    guard let path = Bundle.main.object(forInfoDictionaryKey: "LaunchpadMapRepo") as? String, !path.isEmpty else {
        fatalError("Info.plist has no LaunchpadMapRepo; build the app with app/bundle.sh")
    }
    return URL(fileURLWithPath: path)
}()
let tool = repo.appendingPathComponent("bin/launchpad-map")
let page = repo.appendingPathComponent("history.html")

/// The login shell's PATH. An app opened from the Dock gets a bare one, and the
/// scanner needs python3, adb, adb-reconnect and swift from the user's setup.
let shellPath: String = {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/zsh")
    p.arguments = ["-lic", "print -r -- \"PATH=$PATH\""]
    let out = Pipe()
    p.standardOutput = out
    p.standardError = FileHandle.nullDevice
    p.standardInput = FileHandle.nullDevice
    do { try p.run() } catch { return "/usr/bin:/bin:/usr/sbin:/sbin" }
    let deadline = Date().addingTimeInterval(8)
    while p.isRunning && Date() < deadline { usleep(50_000) }
    if p.isRunning { p.terminate() }
    let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    let line = text.split(separator: "\n").last(where: { $0.hasPrefix("PATH=") })
    return line.map { String($0.dropFirst(5)) } ?? "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
}()

func environment() -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = shellPath
    env["PYTHONUNBUFFERED"] = "1"  // progress lines as they happen, not at exit
    return env
}

// MARK: - Devices

struct Device {
    let id: String       // the page's #id and the command's device word
    let label: String
    let android: Bool
    var dir: URL { android ? repo.appendingPathComponent("layouts/\(id)") : repo.appendingPathComponent("layouts") }
    var scan: [String] { android ? [id, "dump", "--save"] : ["dump", "--save"] }
    func apply(_ proposal: URL) -> [String] { android ? [id, "write", proposal.path] : ["write", proposal.path] }
}

let devices = [
    Device(id: "mac", label: "Mac", android: false),
    Device(id: "phone", label: "Phone", android: true),
    Device(id: "tablet", label: "Tablet", android: true),
]

struct Proposal {
    let url: URL
    let title: String
    let note: String
}

/// Drafts in a device's snapshot directory: layouts that `write` has not applied.
func proposals(for device: Device) -> [Proposal] {
    let files = (try? FileManager.default.contentsOfDirectory(at: device.dir, includingPropertiesForKeys: nil)) ?? []
    return files.filter { $0.pathExtension == "json" && $0.lastPathComponent != "example.json" }
        .sorted { $0.lastPathComponent < $1.lastPathComponent }
        .compactMap { url in
            guard let data = try? Data(contentsOf: url),
                  let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  doc["draft"] as? Bool == true else { return nil }
            let title = (doc["title"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? url.deletingPathExtension().lastPathComponent
            return Proposal(url: url, title: title, note: doc["note"] as? String ?? "")
        }
}

struct Status: Decodable {
    let attached: Bool
    let ready: Bool
    let model: String?
    let awake: Bool?
    let locked: Bool?

    var summary: String {
        guard attached else { return "not attached" }
        let name = model.map { $0 + ": " } ?? ""
        if awake == false { return name + "screen off" }
        if locked == true { return name + "locked" }
        return name + "ready"
    }
}

// MARK: - Running commands

final class Runner {
    private var process: Process?
    var running: Bool { process?.isRunning ?? false }

    /// Run `launchpad-map args`, handing each output line to `line` and the exit
    /// status to `done`, both on the main thread.
    func start(_ args: [String], line: @escaping (String) -> Void, done: @escaping (Int32) -> Void) {
        let p = Process()
        p.executableURL = tool
        p.arguments = args
        p.currentDirectoryURL = repo
        p.environment = environment()
        p.standardInput = FileHandle.nullDevice
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        var partial = ""
        pipe.fileHandleForReading.readabilityHandler = { h in
            let data = h.availableData
            guard !data.isEmpty else { return }
            partial += String(decoding: data, as: UTF8.self)
            // A line ends at \n; a carriage return redraws it in a terminal.
            var lines = partial.components(separatedBy: CharacterSet(charactersIn: "\r\n"))
            partial = lines.removeLast()
            let shown = lines.filter { !$0.isEmpty }
            if !shown.isEmpty { DispatchQueue.main.async { shown.forEach(line) } }
        }
        p.terminationHandler = { p in
            pipe.fileHandleForReading.readabilityHandler = nil
            let rest = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            let tail = (partial + rest).split(whereSeparator: { $0 == "\n" || $0 == "\r" }).map(String.init)
            DispatchQueue.main.async {
                tail.forEach(line)
                done(p.terminationStatus)
            }
        }
        do {
            try p.run()
            process = p
        } catch {
            line("cannot run \(tool.path): \(error.localizedDescription)")
            done(127)
        }
    }

    func stop() { process?.interrupt() }
}

/// One `launchpad-map <device> status` call, off the main thread.
func fetchStatus(_ device: Device, _ done: @escaping (Status?) -> Void) {
    DispatchQueue.global(qos: .utility).async {
        let p = Process()
        p.executableURL = tool
        p.arguments = [device.id, "status"]
        p.environment = environment()
        p.standardInput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        let out = Pipe()
        p.standardOutput = out
        var status: Status?
        if (try? p.run()) != nil {
            let data = out.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            status = try? JSONDecoder().decode(Status.self, from: data)
        }
        DispatchQueue.main.async { done(status) }
    }
}

// MARK: - Window

final class Controller: NSObject, NSToolbarDelegate, NSWindowDelegate, WKNavigationDelegate {
    let window: NSWindow
    let web: WKWebView
    let picker = NSSegmentedControl(labels: devices.map(\.label), trackingMode: .selectOne, target: nil, action: nil)
    let statusLabel = NSTextField(labelWithString: "")
    let scanButton = NSButton(title: "Scan", target: nil, action: nil)
    let applyButton = NSButton(title: "Apply Proposal…", target: nil, action: nil)
    let stopButton = NSButton(title: "Stop", target: nil, action: nil)
    let progressLine = NSTextField(labelWithString: "")
    let spinner = NSProgressIndicator()
    let logToggle = NSButton(title: "Log", target: nil, action: nil)
    let log = NSTextView()
    let logScroll = NSScrollView()
    let runner = Runner()
    var statuses: [String: Status] = [:]
    var poll: Timer?

    var device: Device { devices[max(0, picker.selectedSegment)] }

    override init() {
        let config = WKWebViewConfiguration()
        // The window's own picker chooses the device; hide the page's.
        let css = ".devices { display: none !important; }"
        let js = "var s = document.createElement('style'); s.textContent = '\(css)'; document.documentElement.appendChild(s);"
        config.userContentController.addUserScript(WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: .zero, configuration: config)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        super.init()
        window.title = "Home Screens"
        window.setFrameAutosaveName("HomeScreensWindow")
        window.delegate = self
        web.navigationDelegate = self

        let toolbar = NSToolbar(identifier: "main")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        window.toolbar = toolbar
        window.toolbarStyle = .unified

        picker.target = self
        picker.action = #selector(pick)
        picker.selectedSegment = devices.firstIndex { $0.id == UserDefaults.standard.string(forKey: "device") } ?? 0
        scanButton.target = self; scanButton.action = #selector(scan)
        applyButton.target = self; applyButton.action = #selector(apply)
        stopButton.target = self; stopButton.action = #selector(stop)
        logToggle.target = self; logToggle.action = #selector(toggleLog)
        logToggle.setButtonType(.pushOnPushOff)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.font = .systemFont(ofSize: 12)

        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        progressLine.lineBreakMode = .byTruncatingTail
        progressLine.textColor = .secondaryLabelColor
        progressLine.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        progressLine.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        stopButton.isHidden = true
        stopButton.controlSize = .small
        logToggle.controlSize = .small

        log.isEditable = false
        log.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        log.textContainerInset = NSSize(width: 6, height: 6)
        log.autoresizingMask = [.width]
        logScroll.documentView = log
        logScroll.hasVerticalScroller = true
        logScroll.isHidden = true
        logScroll.heightAnchor.constraint(equalToConstant: 180).isActive = true

        let bar = NSStackView(views: [spinner, progressLine, stopButton, logToggle])
        bar.orientation = .horizontal
        bar.spacing = 8
        bar.edgeInsets = NSEdgeInsets(top: 5, left: 12, bottom: 5, right: 12)
        let stack = NSStackView(views: [web, logScroll, bar])
        stack.orientation = .vertical
        stack.spacing = 0
        stack.setHuggingPriority(.defaultLow, for: .vertical)
        for v in [web, logScroll, bar] { v.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true }
        window.contentView = stack
        progressLine.stringValue = "Ready"

        if FileManager.default.fileExists(atPath: page.path) {
            load()
        } else {
            run(["history"], what: "Rendering the history page") { _ in }
        }
        refreshControls()
        pollStatus()
        poll = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.pollStatus() }
    }

    func load() {
        var parts = URLComponents(url: page, resolvingAgainstBaseURL: false)!
        parts.fragment = device.id
        web.loadFileURL(parts.url!, allowingReadAccessTo: repo)
    }

    // MARK: actions

    @objc func pick() {
        UserDefaults.standard.set(device.id, forKey: "device")
        // The page reloads itself on a hash change and shows that device.
        web.evaluateJavaScript("location.hash = '\(device.id)'")
        refreshControls()
        pollStatus()
    }

    @objc func scan() {
        let d = device
        run(d.scan, what: "Scanning the \(d.label.lowercased())") { [weak self] ok in
            guard ok else { return }
            self?.run(["history"], what: "Rendering the history page") { _ in }
        }
    }

    @objc func apply() {
        let d = device, drafts = proposals(for: d)
        guard !drafts.isEmpty else { return }
        let menu = NSMenu()
        for p in drafts {
            let item = NSMenuItem(title: p.title + " — " + p.url.lastPathComponent, action: #selector(confirmApply(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = p.url
            item.toolTip = p.note.isEmpty ? nil : p.note
            menu.addItem(item)
        }
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: applyButton.bounds.height + 4), in: applyButton)
    }

    @objc func confirmApply(_ item: NSMenuItem) {
        guard let url = item.representedObject as? URL,
              let p = proposals(for: device).first(where: { $0.url == url }) else { return }
        let d = device
        let alert = NSAlert()
        alert.messageText = "Apply “\(p.title)” to the \(d.label.lowercased())?"
        var text = d.android
            ? "This drives the \(d.label.lowercased())'s screen: it drags icons and folders until the home screen matches the proposal. Leave the device alone until it finishes."
            : "This rewrites the Launchpad database and restarts the Dock. The database is backed up first, and the write is checked and rolled back if the Dock does not keep it."
        if !p.note.isEmpty { text += "\n\n" + p.note }
        alert.informativeText = text
        alert.addButton(withTitle: "Apply")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard response == .alertFirstButtonReturn, let self else { return }
            self.run(d.apply(url), what: "Applying \(p.title) to the \(d.label.lowercased())") { [weak self] _ in
                self?.run(["history"], what: "Rendering the history page") { _ in }
            }
        }
    }

    @objc func stop() { runner.stop() }

    @objc func toggleLog() { logScroll.isHidden = logToggle.state != .on }

    // MARK: running

    func run(_ args: [String], what: String, then: @escaping (Bool) -> Void) {
        guard !runner.running else { return }
        append("$ launchpad-map " + args.joined(separator: " "))
        progressLine.stringValue = what + "…"
        spinner.startAnimation(nil)
        stopButton.isHidden = false
        refreshControls()
        runner.start(args, line: { [weak self] line in
            self?.append(line)
            self?.progressLine.stringValue = line
        }, done: { [weak self] code in
            guard let self else { return }
            self.spinner.stopAnimation(nil)
            self.stopButton.isHidden = true
            let ok = code == 0
            self.progressLine.stringValue = ok ? what + ": done" : what + ": failed (exit \(code)); see the log"
            self.append(ok ? "✓ done" : "✗ exit \(code)")
            if !ok { self.logToggle.state = .on; self.toggleLog() }
            if args == ["history"] && ok { self.load() }
            self.refreshControls()
            then(ok)
        })
    }

    func append(_ line: String) {
        let atEnd = log.visibleRect.maxY >= log.bounds.maxY - 4
        log.textStorage?.append(NSAttributedString(string: line + "\n", attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 11, weight: .regular), .foregroundColor: NSColor.labelColor]))
        if atEnd { log.scrollToEndOfDocument(nil) }
    }

    func pollStatus() {
        for d in devices where d.android {
            fetchStatus(d) { [weak self] s in
                guard let self else { return }
                if let s { self.statuses[d.id] = s } else { self.statuses.removeValue(forKey: d.id) }
                self.refreshControls()
            }
        }
    }

    func refreshControls() {
        let d = device, busy = runner.running
        if d.android {
            let s = statuses[d.id]
            statusLabel.stringValue = s?.summary ?? "checking…"
            scanButton.isEnabled = !busy && (s?.ready ?? false)
            scanButton.toolTip = s?.ready == true ? "Scan the \(d.label.lowercased())'s home screen"
                : "A scan needs the \(d.label.lowercased()) attached, its screen on and unlocked"
        } else {
            statusLabel.stringValue = "Launchpad"
            scanButton.isEnabled = !busy
            scanButton.toolTip = "Read the Launchpad database and add a snapshot"
        }
        let drafts = proposals(for: d)
        applyButton.isEnabled = !busy && !drafts.isEmpty && (!d.android || statuses[d.id]?.ready == true)
        applyButton.toolTip = drafts.isEmpty ? "No proposal for this device" : nil
        picker.isEnabled = !busy
    }

    // MARK: toolbar

    static let pickerID = NSToolbarItem.Identifier("device")
    static let statusID = NSToolbarItem.Identifier("status")
    static let scanID = NSToolbarItem.Identifier("scan")
    static let applyID = NSToolbarItem.Identifier("apply")

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [Self.pickerID, .flexibleSpace, Self.statusID, Self.scanID, Self.applyID]
    }

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarDefaultItemIdentifiers(toolbar)
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        let item = NSToolbarItem(itemIdentifier: id)
        switch id {
        case Self.pickerID: item.view = picker; item.label = "Device"
        case Self.statusID: item.view = statusLabel; item.label = "Status"
        case Self.scanID: item.view = scanButton; item.label = "Scan"
        case Self.applyID: item.view = applyButton; item.label = "Apply"
        default: return nil
        }
        return item
    }

    // MARK: window

    func windowWillClose(_ notification: Notification) {
        runner.stop()
        NSApp.terminate(nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // Store links from the app menus open in the browser, not in this window.
        if let url = action.request.url, url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        } else {
            decisionHandler(.allow)
        }
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    var controller: Controller?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Home Screens", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)
        NSApp.mainMenu = main

        controller = Controller()
        // The first launch centres the window; later ones reopen it where it was left.
        if UserDefaults.standard.string(forKey: "NSWindow Frame HomeScreensWindow") == nil {
            controller?.window.center()
        }
        controller?.window.makeKeyAndOrderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
