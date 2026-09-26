// Home Screens: the launchpad-map history page in a window, with every command
// that feeds it one click away. It shows one device at a time (the Mac's
// Launchpad, the phone, the tablet) and, for the chosen one: scans it, applies
// a proposal after showing its dry run, refreshes usage stats, builds a
// proposal from a reorg plan, runs single home-screen operations, and records
// showcase decisions made from the page.
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
let historyPage = repo.appendingPathComponent("history.html")
let showcasePage = repo.appendingPathComponent("showcase.html")

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
    var name: String { label.lowercased() }
    var dir: URL { android ? repo.appendingPathComponent("layouts/\(id)") : repo.appendingPathComponent("layouts") }
    /// `launchpad-map` arguments for this device: the Android ones take its word first.
    func cmd(_ args: String...) -> [String] { android ? [id] + args : args }
    func cmd(_ args: [String]) -> [String] { android ? [id] + args : args }
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

/// Where a new proposal goes: proposal.json, else the first free proposal-N.json.
func nextProposalFile(in dir: URL) -> URL {
    let fm = FileManager.default
    let first = dir.appendingPathComponent("proposal.json")
    if !fm.fileExists(atPath: first.path) { return first }
    var n = 2
    while fm.fileExists(atPath: dir.appendingPathComponent("proposal-\(n).json").path) { n += 1 }
    return dir.appendingPathComponent("proposal-\(n).json")
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

/// Split an operation typed by hand into arguments; quotes group words.
func splitArgs(_ text: String) -> [String] {
    var out: [String] = [], cur = "", quote: Character? = nil, started = false
    for ch in text {
        if let q = quote {
            if ch == q { quote = nil } else { cur.append(ch) }
        } else if ch == "\"" || ch == "'" {
            quote = ch; started = true
        } else if ch.isWhitespace {
            if started || !cur.isEmpty { out.append(cur); cur = ""; started = false }
        } else {
            cur.append(ch)
        }
    }
    if started || !cur.isEmpty { out.append(cur) }
    return out
}

// MARK: - Running commands

/// One `launchpad-map` command. Output lines go to `line` on the main thread;
/// with `stdout` set, standard output goes to that file instead.
final class Job {
    let args: [String]
    let what: String
    let stdout: URL?
    let then: (Bool, [String]) -> Void
    var lines: [String] = []
    init(_ args: [String], _ what: String, stdout: URL? = nil, then: @escaping (Bool, [String]) -> Void = { _, _ in }) {
        self.args = args; self.what = what; self.stdout = stdout; self.then = then
    }
}

final class Runner {
    private(set) var current: Job?
    private var process: Process?
    private var queue: [Job] = []
    var busy: Bool { current != nil }
    var onLine: (Job, String) -> Void = { _, _ in }
    var onStart: (Job) -> Void = { _ in }
    var onDone: (Job, Int32) -> Void = { _, _ in }

    /// Run after whatever is running and queued already.
    func enqueue(_ job: Job) {
        queue.append(job)
        if current == nil { next() }
    }

    func queued(_ args: [String]) -> Bool { queue.contains { $0.args == args } || current?.args == args }

    private func next() {
        guard current == nil, !queue.isEmpty else { return }
        let job = queue.removeFirst()
        current = job
        onStart(job)
        let p = Process()
        p.executableURL = tool
        p.arguments = job.args
        p.currentDirectoryURL = repo
        p.environment = environment()
        p.standardInput = FileHandle.nullDevice
        let pipe = Pipe()
        var outFile: FileHandle?
        if let url = job.stdout {
            FileManager.default.createFile(atPath: url.path, contents: nil)
            outFile = try? FileHandle(forWritingTo: url)
            p.standardOutput = outFile ?? pipe
        } else {
            p.standardOutput = pipe
        }
        p.standardError = pipe
        var partial = ""
        let emit: ([String]) -> Void = { [weak self] ls in
            DispatchQueue.main.async { ls.forEach { job.lines.append($0); self?.onLine(job, $0) } }
        }
        pipe.fileHandleForReading.readabilityHandler = { h in
            let data = h.availableData
            guard !data.isEmpty else { return }
            partial += String(decoding: data, as: UTF8.self)
            // A line ends at \n; a carriage return redraws it in a terminal.
            var ls = partial.components(separatedBy: CharacterSet(charactersIn: "\r\n"))
            partial = ls.removeLast()
            emit(ls.filter { !$0.isEmpty })
        }
        p.terminationHandler = { [weak self] p in
            pipe.fileHandleForReading.readabilityHandler = nil
            try? outFile?.close()
            let rest = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            let tail = (partial + rest).split(whereSeparator: { $0 == "\n" || $0 == "\r" }).map(String.init)
            DispatchQueue.main.async {
                tail.forEach { job.lines.append($0); self?.onLine(job, $0) }
                self?.finish(job, p.terminationStatus)
            }
        }
        do {
            try p.run()
            process = p
        } catch {
            onLine(job, "cannot run \(tool.path): \(error.localizedDescription)")
            finish(job, 127)
        }
    }

    private func finish(_ job: Job, _ code: Int32) {
        current = nil
        process = nil
        onDone(job, code)
        job.then(code == 0, job.lines)
        next()
    }

    /// Interrupt the running command and drop everything queued after it.
    func stop() {
        queue.removeAll()
        process?.interrupt()
    }
}

/// A command that runs beside the queue and says nothing: pause, resume and
/// stop reach a running `write` this way.
func runAside(_ args: [String]) {
    let p = Process()
    p.executableURL = tool
    p.arguments = args
    p.environment = environment()
    p.standardInput = FileHandle.nullDevice
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    try? p.run()
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

final class Controller: NSObject, NSToolbarDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    let window: NSWindow
    let web: WKWebView
    let picker = NSSegmentedControl(labels: devices.map(\.label), trackingMode: .selectOne, target: nil, action: nil)
    let statusLabel = NSTextField(labelWithString: "")
    let scanButton = NSButton(title: "Scan", target: nil, action: nil)
    let applyButton = NSButton(title: "Apply Proposal…", target: nil, action: nil)
    let moreButton = NSButton(title: "More", target: nil, action: nil)
    let stopButton = NSButton(title: "Stop", target: nil, action: nil)
    let pauseButton = NSButton(title: "Pause", target: nil, action: nil)
    let resumeButton = NSButton(title: "Resume", target: nil, action: nil)
    let progressLine = NSTextField(labelWithString: "")
    let spinner = NSProgressIndicator()
    let logToggle = NSButton(title: "Log", target: nil, action: nil)
    let log = NSTextView()
    let logScroll = NSScrollView()
    let runner = Runner()
    var statuses: [String: Status] = [:]
    var poll: Timer?
    var rebuild: Timer?
    var showingShowcase = false

    var device: Device { devices[max(0, picker.selectedSegment)] }
    var ready: Bool { !device.android || statuses[device.id]?.ready == true }

    override init() {
        let config = WKWebViewConfiguration()
        // The window's own picker chooses the device; hide the page's.
        let js = "var s = document.createElement('style'); s.textContent = '.devices { display: none !important; }';"
            + " document.documentElement.appendChild(s);"
        config.userContentController.addUserScript(WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: .zero, configuration: config)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        super.init()
        config.userContentController.add(self, name: "homeScreens")
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
        for (b, a) in [(scanButton, #selector(scan)), (applyButton, #selector(apply)), (moreButton, #selector(more)),
                       (stopButton, #selector(stop)), (pauseButton, #selector(pause)), (resumeButton, #selector(resume)),
                       (logToggle, #selector(toggleLog))] {
            b.target = self; b.action = a
        }
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
        for b in [stopButton, pauseButton, resumeButton, logToggle] { b.controlSize = .small }
        for b in [stopButton, pauseButton, resumeButton] { b.isHidden = true }

        log.isEditable = false
        log.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        log.textContainerInset = NSSize(width: 6, height: 6)
        log.autoresizingMask = [.width]
        logScroll.documentView = log
        logScroll.hasVerticalScroller = true
        logScroll.isHidden = true
        logScroll.heightAnchor.constraint(equalToConstant: 180).isActive = true

        let bar = NSStackView(views: [spinner, progressLine, pauseButton, resumeButton, stopButton, logToggle])
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

        runner.onStart = { [weak self] job in
            guard let self else { return }
            self.append("$ launchpad-map " + job.args.joined(separator: " "))
            self.progressLine.stringValue = job.what + "…"
            self.spinner.startAnimation(nil)
            self.stopButton.isHidden = false
            let write = self.device.android && job.args.count > 1 && job.args[1] == "write" && !job.args.contains("--dry-run")
            self.pauseButton.isHidden = !write
            self.resumeButton.isHidden = !write
            self.refreshControls()
        }
        runner.onLine = { [weak self] _, line in
            self?.append(line)
            self?.progressLine.stringValue = line
        }
        runner.onDone = { [weak self] job, code in
            guard let self else { return }
            let ok = code == 0
            self.append(ok ? "✓ done" : "✗ exit \(code)")
            self.progressLine.stringValue = ok ? job.what + ": done" : job.what + ": failed (exit \(code)); see the log"
            if !ok { self.logToggle.state = .on; self.toggleLog() }
            if !self.runner.busy {
                self.spinner.stopAnimation(nil)
                for b in [self.stopButton, self.pauseButton, self.resumeButton] { b.isHidden = true }
            }
            self.refreshControls()
        }

        if FileManager.default.fileExists(atPath: historyPage.path) {
            load()
        } else {
            render()
        }
        refreshControls()
        pollStatus()
        poll = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.pollStatus() }
    }

    func load() {
        showingShowcase = false
        var parts = URLComponents(url: historyPage, resolvingAgainstBaseURL: false)!
        parts.fragment = device.id
        web.loadFileURL(parts.url!, allowingReadAccessTo: repo)
    }

    /// Re-render the history page, then show it.
    func render() {
        runner.enqueue(Job(["history"], "Rendering the history page") { [weak self] ok, _ in if ok { self?.load() } })
    }

    // MARK: device and scan

    @objc func pick() {
        UserDefaults.standard.set(device.id, forKey: "device")
        if showingShowcase {
            load()
        } else {
            // The page reloads itself on a hash change and shows that device.
            web.evaluateJavaScript("location.hash = '\(device.id)'")
        }
        refreshControls()
        pollStatus()
    }

    @objc func scan() {
        let d = device
        runner.enqueue(Job(d.cmd("dump", "--save"), "Scanning the \(d.name)") { [weak self] ok, _ in
            if ok { self?.render() }
        })
    }

    // MARK: apply, with its dry run first

    @objc func apply() {
        let drafts = proposals(for: device)
        guard !drafts.isEmpty else { return }
        let menu = NSMenu()
        for p in drafts {
            let item = NSMenuItem(title: p.title + " — " + p.url.lastPathComponent, action: #selector(dryRun(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = p.url
            item.toolTip = p.note.isEmpty ? nil : p.note
            menu.addItem(item)
        }
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: applyButton.bounds.height + 4), in: applyButton)
    }

    @objc func dryRun(_ item: NSMenuItem) {
        guard let url = item.representedObject as? URL,
              let p = proposals(for: device).first(where: { $0.url == url }) else { return }
        let d = device
        runner.enqueue(Job(d.cmd("write", url.path, "--dry-run"), "Planning \(p.title)") { [weak self] ok, lines in
            guard ok, let self else { return }
            self.confirmApply(p, on: d, plan: lines)
        })
    }

    func confirmApply(_ p: Proposal, on d: Device, plan: [String]) {
        let alert = NSAlert()
        alert.messageText = "Apply “\(p.title)” to the \(d.name)?"
        var text = d.android
            ? "This drives the \(d.name)'s screen: it drags icons and folders until the home screen matches the proposal. Leave the device alone until it finishes; Pause and Stop sit in the bar below."
            : "This rewrites the Launchpad database and restarts the Dock. The database is backed up first, and the write is checked and rolled back if the Dock does not keep it."
        if !p.note.isEmpty { text += "\n\n" + p.note }
        alert.informativeText = text + "\n\nThe plan, from a dry run:"
        let view = NSTextView(frame: NSRect(x: 0, y: 0, width: 620, height: 280))
        view.isEditable = false
        view.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        view.string = plan.joined(separator: "\n")
        let scroll = NSScrollView(frame: view.frame)
        scroll.documentView = view
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        alert.accessoryView = scroll
        alert.addButton(withTitle: "Apply")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard response == .alertFirstButtonReturn, let self else { return }
            self.runner.enqueue(Job(d.cmd("write", p.url.path), "Applying \(p.title) to the \(d.name)") { [weak self] _, _ in
                self?.render()
            })
        }
    }

    // MARK: the More menu

    @objc func more() {
        let d = device, menu = NSMenu()
        menu.autoenablesItems = false
        func add(_ title: String, _ action: Selector, enabled: Bool = true, tag: Int = 0, to m: NSMenu? = nil) {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            item.isEnabled = enabled
            item.tag = tag
            (m ?? menu).addItem(item)
        }
        if d.android {
            let attached = statuses[d.id]?.attached == true
            add("Refresh Usage Stats", #selector(refreshUsage), enabled: attached)
            add("New Proposal from Reorg Plan…", #selector(newProposal))
            menu.addItem(.separator())
            let ops = NSMenu()
            ops.autoenablesItems = false
            add("Peek", #selector(op(_:)), enabled: ready, tag: 1, to: ops)
            add("Go to Page…", #selector(op(_:)), enabled: ready, tag: 2, to: ops)
            add("Home", #selector(op(_:)), enabled: ready, tag: 3, to: ops)
            add("Back", #selector(op(_:)), enabled: ready, tag: 4, to: ops)
            ops.addItem(.separator())
            add("Lock Layout", #selector(op(_:)), enabled: ready, tag: 5, to: ops)
            add("Unlock Layout", #selector(op(_:)), enabled: ready, tag: 6, to: ops)
            ops.addItem(.separator())
            add("Run Operation…", #selector(op(_:)), enabled: ready, tag: 7, to: ops)
            let item = NSMenuItem(title: "Home Screen Controls", action: nil, keyEquivalent: "")
            item.submenu = ops
            menu.addItem(item)
        } else {
            add(showingShowcase ? "Back to History" : "Preview Showcase", #selector(previewShowcase))
            add("Rebuild Showcase", #selector(rebuildShowcase))
        }
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: moreButton.bounds.height + 4), in: moreButton)
    }

    @objc func refreshUsage() {
        let d = device
        runner.enqueue(Job(d.cmd("usage", "--save"), "Reading the \(d.name)'s usage stats") { [weak self] ok, _ in
            if ok { self?.render() }
        })
    }

    @objc func newProposal() {
        let d = device
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.json]
        panel.directoryURL = d.dir
        panel.message = "A reorg plan: the new folders to make and what goes in them"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let plan = panel.url, let self else { return }
            DispatchQueue.main.async {
                guard let fields = self.ask("New proposal from \(plan.lastPathComponent)",
                                            "It is placed on the \(d.name)'s newest snapshot and saved as a draft.",
                                            ["Title", "Note"], ["", ""]) else { return }
                let out = nextProposalFile(in: d.dir)
                var args = d.cmd("reorg", plan.path)
                if !fields[0].isEmpty { args += ["--title", fields[0]] }
                if !fields[1].isEmpty { args += ["--note", fields[1]] }
                self.runner.enqueue(Job(args, "Building \(out.lastPathComponent)", stdout: out) { [weak self] ok, _ in
                    if ok { self?.render() } else { try? FileManager.default.removeItem(at: out) }
                })
            }
        }
    }

    @objc func op(_ item: NSMenuItem) {
        let d = device
        var args: [String]
        switch item.tag {
        case 1: args = ["peek"]
        case 2:
            guard let f = ask("Go to page", "Turns the \(d.name) to that home page.", ["Page"], ["1"]),
                  Int(f[0]) != nil else { return }
            args = ["go", f[0]]
        case 3: args = ["home"]
        case 4: args = ["back"]
        case 5: args = ["lock", "on"]
        case 6: args = ["lock", "off"]
        default:
            guard let f = ask("Run an operation", "One `op` step, as on the command line, e.g.\n"
                              + "into office Slack Zoom\nmove-folder travel --to 2\ngroup 3 \"new folder\" Maps Weather",
                              ["Operation"], [""]),
                  !f[0].trimmingCharacters(in: .whitespaces).isEmpty else { return }
            args = splitArgs(f[0])
        }
        runner.enqueue(Job(d.cmd(["op"] + args), "\(d.label): op \(args.joined(separator: " "))") { [weak self] ok, _ in
            // An edit updates the record the page does not show; a look changes nothing.
            if ok, !["peek", "go", "home", "back", "lock"].contains(args[0]) { self?.render() }
        })
    }

    @objc func previewShowcase() {
        if showingShowcase { load(); return }
        let show = { [weak self] in
            self?.showingShowcase = true
            self?.web.loadFileURL(showcasePage, allowingReadAccessTo: repo)
        }
        if FileManager.default.fileExists(atPath: showcasePage.path) {
            show()
        } else {
            runner.enqueue(Job(["showcase"], "Building the showcase") { ok, _ in if ok { show() } })
        }
    }

    @objc func rebuildShowcase() {
        runner.enqueue(Job(["showcase"], "Building the showcase") { [weak self] ok, _ in
            if ok, self?.showingShowcase == true { self?.web.reload() }
        })
    }

    // MARK: showcase decisions from the page

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let app = body["review"] as? String,
              let show = body["show"] as? Bool else { return }
        var reason = ""
        if !show {
            guard let f = ask("Hide “\(app)” from the showcase?",
                              "Only apps that would expose you are hidden. The reason stays in showcase.json and never reaches the page.",
                              ["Reason"], [""]),
                  !f[0].trimmingCharacters(in: .whitespaces).isEmpty else { return }
            reason = f[0].trimmingCharacters(in: .whitespaces)
        }
        let args = show ? ["review", "--show", app] : ["review", "--hide", reason, app]
        runner.enqueue(Job(args, show ? "Showing \(app)" : "Hiding \(app)") { [weak self] ok, _ in
            guard ok, let self else { return }
            let payload = (try? JSONSerialization.data(withJSONObject: [app, show, reason])).map { String(decoding: $0, as: UTF8.self) } ?? "[]"
            self.web.evaluateJavaScript("hsReviewed(...\(payload))")
            self.scheduleRebuild()
        })
    }

    /// Rebuild the saved pages a few seconds after the last decision, so a run
    /// of decisions costs one rebuild. The page already shows them.
    func scheduleRebuild() {
        rebuild?.invalidate()
        rebuild = Timer.scheduledTimer(withTimeInterval: 4, repeats: false) { [weak self] _ in
            guard let self else { return }
            if !self.runner.queued(["history"]) { self.runner.enqueue(Job(["history"], "Saving the history page")) }
            if !self.runner.queued(["showcase"]) { self.runner.enqueue(Job(["showcase"], "Saving the showcase")) }
        }
    }

    // MARK: running

    @objc func stop() {
        // A write on the phone or tablet stops between operations, cleanly.
        if let job = runner.current, device.android, job.args.count > 1, job.args[1] == "write", !job.args.contains("--dry-run") {
            runAside(device.cmd("op", "stop"))
            progressLine.stringValue = "Stopping after the current operation…"
        } else {
            runner.stop()
        }
    }

    @objc func pause() { runAside(device.cmd("op", "pause")); progressLine.stringValue = "Pausing after the current operation…" }
    @objc func resume() { runAside(device.cmd("op", "resume")); progressLine.stringValue = "Resuming…" }

    @objc func toggleLog() { logScroll.isHidden = logToggle.state != .on }

    func append(_ line: String) {
        let atEnd = log.visibleRect.maxY >= log.bounds.maxY - 4
        log.textStorage?.append(NSAttributedString(string: line + "\n", attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 11, weight: .regular), .foregroundColor: NSColor.labelColor]))
        if atEnd { log.scrollToEndOfDocument(nil) }
    }

    /// A small form: one text field per label. nil when cancelled.
    func ask(_ title: String, _ info: String, _ labels: [String], _ values: [String]) -> [String]? {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = info
        let fields = zip(labels, values).map { label, value -> NSTextField in
            let f = NSTextField(string: value)
            f.placeholderString = label
            f.frame = NSRect(x: 0, y: 0, width: 360, height: 24)
            return f
        }
        let stack = NSStackView(views: fields)
        stack.orientation = .vertical
        stack.spacing = 8
        stack.frame = NSRect(x: 0, y: 0, width: 360, height: CGFloat(fields.count) * 32)
        alert.accessoryView = stack
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = fields.first
        return alert.runModal() == .alertFirstButtonReturn ? fields.map(\.stringValue) : nil
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
        let d = device, busy = runner.busy
        if d.android {
            let s = statuses[d.id]
            statusLabel.stringValue = s?.summary ?? "checking…"
            scanButton.toolTip = s?.ready == true ? "Scan the \(d.name)'s home screen"
                : "A scan needs the \(d.name) attached, its screen on and unlocked"
        } else {
            statusLabel.stringValue = showingShowcase ? "Showcase preview" : "Launchpad"
            scanButton.toolTip = "Read the Launchpad database and add a snapshot"
        }
        scanButton.isEnabled = !busy && ready
        let drafts = proposals(for: d)
        applyButton.isEnabled = !busy && !drafts.isEmpty && ready
        applyButton.toolTip = drafts.isEmpty ? "No proposal for this device" : nil
        moreButton.isEnabled = !busy
        picker.isEnabled = !busy
    }

    // MARK: toolbar

    static let pickerID = NSToolbarItem.Identifier("device")
    static let statusID = NSToolbarItem.Identifier("status")
    static let scanID = NSToolbarItem.Identifier("scan")
    static let applyID = NSToolbarItem.Identifier("apply")
    static let moreID = NSToolbarItem.Identifier("more")

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [Self.pickerID, .flexibleSpace, Self.statusID, Self.scanID, Self.applyID, Self.moreID]
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
        case Self.moreID: item.view = moreButton; item.label = "More"
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
        // Store links from the page's app menus open in the browser, not in this window.
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
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
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
