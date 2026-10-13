import AppKit

// MARK: - Self-test

/// `HOME_SCREENS_SELFTEST=1` builds the window over a throwaway checkout in a temporary directory:
/// a `bin/launchpad-map` stub that logs each call and answers from fixtures, a few layouts and two
/// small pages. It presses every control and checks the effect. The real `launchpad-map`, the real
/// layouts, the Launchpad database, the Dock and any device are never touched: every path the app
/// builds from the checkout points into the temporary directory, and the run stops before the first
/// press if one does not.
enum SelfTest {
    /// The stub: logs `calls.log`, answers `status`, writes the pages `history` and `showcase` make,
    /// and holds `dump` and a non-dry `write` while their `hold-*` file exists, as a slow command.
    static let stub = """
    #!/bin/zsh
    DIR="${0:A:h:h}"
    trap 'exit 130' INT
    print -r -- "$*" >> "$DIR/calls.log"
    wait_while_held() { for _ in {1..300}; do [[ -e "$DIR/$1" ]] || return 0; sleep 0.1; done }
    dev=()
    [[ "$1" == phone || "$1" == tablet ]] && { dev=("$1"); shift; }
    case "$1" in
      status)
        if [[ "$dev" == phone ]]; then print '{"attached":true,"ready":true,"model":"Fixture Phone","awake":true,"locked":false}'
        else print '{"attached":true,"ready":false,"model":"Fixture Tablet","awake":false,"locked":false}'; fi ;;
      history) print '<html><body>history</body></html>' > "$DIR/history.html"; print "history written" ;;
      showcase) print '<html><body>showcase</body></html>' > "$DIR/showcase.html"; print "showcase written" ;;
      dump) print "scanning"; wait_while_held hold-dump; print "snapshot saved" ;;
      write)
        if [[ " $* " == *" --dry-run "* ]]; then print "plan: move 2 icons"; print "plan: group 3 apps"
        else print "writing"; wait_while_held hold-write; print "applied"; fi ;;
      reorg) print '{"title":"From plan","draft":true,"pages":[]}' ;;
      op)
        case "$2" in
          stop) rm -f "$DIR/hold-write" ;;
          fail) print -u2 "error: fixture failure"; exit 1 ;;
          *) print "op $2 done" ;;
        esac ;;
      usage|review) print "$1 done" ;;
      *) print -u2 "unknown command $1"; exit 2 ;;
    esac
    """

    /// The throwaway checkout: stub tool, a draft proposal for the Mac and the phone (none for the
    /// tablet), a reorg plan, and a history page, so nothing needs a first render.
    static func makeCheckout(in dir: URL) throws {
        let fm = FileManager.default
        for sub in ["bin", "layouts/phone", "layouts/tablet"] {
            try fm.createDirectory(at: dir.appendingPathComponent(sub), withIntermediateDirectories: true)
        }
        let toolFile = dir.appendingPathComponent("bin/launchpad-map")
        try stub.write(to: toolFile, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: toolFile.path)
        let draft = #"{"title":"Fixture proposal","note":"Fixture note","draft":true,"pages":[]}"#
        try draft.write(to: dir.appendingPathComponent("layouts/proposal.json"), atomically: true, encoding: .utf8)
        try draft.write(to: dir.appendingPathComponent("layouts/phone/proposal.json"), atomically: true, encoding: .utf8)
        try #"{"folders":[]}"#.write(to: dir.appendingPathComponent("reorg-plan.json"), atomically: true, encoding: .utf8)
        try "<html><body>history</body></html>".write(to: historyPageIn(dir), atomically: true, encoding: .utf8)
    }

    static func historyPageIn(_ dir: URL) -> URL { dir.appendingPathComponent("history.html") }
}

@MainActor
final class SelfTestRun {
    let dir: URL
    let controller: Controller
    var failures: [String] = []
    private var asked: [(title: String, labels: [String])] = []
    private var formAnswer: [String]? = [""]
    private var planAnswer = false
    private var plans: [[String]] = []
    private var fileAnswer: URL?

    init(dir: URL) {
        self.dir = dir
        controller = Controller(offline: true, page: SelfTest.historyPageIn(dir), autosave: "")
        controller.answerForm = { [unowned self] title, _, labels, _ in
            asked.append((title, labels))
            return formAnswer
        }
        controller.answerPlan = { [unowned self] _, _, plan in
            plans.append(plan)
            return planAnswer
        }
        controller.answerFile = { [unowned self] _ in fileAnswer }
    }

    func check(_ ok: Bool, _ what: String) { if !ok { failures.append(what) } }

    var calls: [String] {
        let text = (try? String(contentsOf: dir.appendingPathComponent("calls.log"), encoding: .utf8)) ?? ""
        return text.split(separator: "\n").map(String.init).filter { !$0.hasSuffix(" status") }
    }

    func hold(_ name: String, _ on: Bool) {
        let url = dir.appendingPathComponent(name)
        if on { FileManager.default.createFile(atPath: url.path, contents: nil) } else { try? FileManager.default.removeItem(at: url) }
    }

    @discardableResult
    func wait(_ seconds: Double = 10, _ condition: () -> Bool) async -> Bool {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    /// Everything queued has finished, and the page is done loading.
    func idle() async {
        let c = controller
        let done = await wait { !c.runner.busy && !c.web.isLoading }
        check(done, "commands finished within 10s (still running: \(c.runner.current?.args.joined(separator: " ") ?? "none"))")
    }

    func select(_ index: Int) {
        controller.picker.selectedSegment = index
        controller.pick()
    }

    /// Presses a menu item the way the menu would.
    func choose(_ title: String, in menu: NSMenu?) {
        guard let item = menu?.items.first(where: { $0.title == title }), item.isEnabled, let action = item.action else {
            failures.append("menu has no enabled item \"\(title)\"")
            return
        }
        _ = (item.target as AnyObject).perform(action, with: item)
    }

    func run() async {
        let c = controller
        let defaults = UserDefaults.standard
        let savedDevice = defaults.object(forKey: "device")
        defer { if let savedDevice { defaults.set(savedDevice, forKey: "device") } else { defaults.removeObject(forKey: "device") } }

        // Window and first state
        check(c.window.title == "Home Screens", "window title is Home Screens, got \(c.window.title)")
        check(c.band.title == "Home Screens", "band title is Home Screens")
        check(c.footer.message == "Ready", "footer opens with Ready, got \(c.footer.message)")
        check(c.footer.resting == "Ready", "footer has a resting message")
        check(c.picker.segmentCount == 3 && c.picker.label(forSegment: 0) == "Mac" && c.picker.label(forSegment: 2) == "Tablet",
              "the picker offers Mac, Phone, Tablet")
        check(c.web.url?.lastPathComponent == "history.html", "the fixture history page is loaded")
        await idle()

        // The stub answers status; the app decodes it
        var fetched: [String: Status] = [:]
        for d in devices where d.android {
            await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
                fetchStatus(d) { status in
                    if let status { fetched[d.id] = status }
                    done.resume()
                }
            }
        }
        check(fetched["phone"]?.summary == "Fixture Phone: ready", "phone status decodes, got \(fetched["phone"]?.summary ?? "nil")")
        check(fetched["tablet"]?.summary == "Fixture Tablet: screen off", "tablet status decodes, got \(fetched["tablet"]?.summary ?? "nil")")
        c.statuses = fetched

        // Device picker
        select(0)
        check(defaults.string(forKey: "device") == "mac", "picking Mac remembers it")
        check(c.band.stateText == "Launchpad", "Mac state reads Launchpad, got \(c.band.stateText)")
        check(c.scanButton.isEnabled && c.applyButton.isEnabled && c.moreButton.isEnabled && c.picker.isEnabled,
              "Mac: Scan, Apply Proposal and More are enabled")
        check([c.stopButton, c.pauseButton, c.resumeButton].allSatisfy(\.isHidden), "Stop, Pause and Resume start hidden")
        select(1)
        check(defaults.string(forKey: "device") == "phone", "picking Phone remembers it")
        check(c.band.stateText == "Fixture Phone: ready", "phone state shows its status, got \(c.band.stateText)")
        check(c.scanButton.isEnabled && c.applyButton.isEnabled, "phone: Scan and Apply Proposal are enabled")
        select(2)
        check(c.band.stateText == "Fixture Tablet: screen off", "tablet state shows its status, got \(c.band.stateText)")
        check(!c.scanButton.isEnabled, "tablet: Scan is disabled while the screen is off")
        check(c.scanButton.toolTip?.contains("needs the tablet attached") == true, "tablet: Scan says why it is disabled")
        check(!c.applyButton.isEnabled && c.applyButton.toolTip == "No proposal for this device", "tablet: Apply Proposal is disabled with no draft")
        check(c.applyMenu() == nil, "tablet has no apply menu")

        // Scan: Mac, then phone. Each runs dump --save and re-renders the history page.
        select(0)
        c.scanButton.performClick(nil)
        check(c.runner.busy && c.stopButton.isHidden == false, "Scan runs and shows Stop")
        check(!c.scanButton.isEnabled && !c.applyButton.isEnabled && !c.moreButton.isEnabled && !c.picker.isEnabled,
              "controls are disabled while a command runs")
        await idle()
        check(calls == ["dump --save", "history"], "Mac scan runs dump --save then history, got \(calls)")
        check(c.footer.message == "Rendering the history page: done" && !c.footer.isError, "footer reports the finished run, got \(c.footer.message)")
        check(c.log.string.contains("$ launchpad-map dump --save") && c.log.string.contains("✓ done"), "the log records the command and its result")
        check([c.stopButton, c.pauseButton, c.resumeButton].allSatisfy(\.isHidden), "run controls hide when the queue is empty")
        check(c.scanButton.isEnabled && c.picker.isEnabled, "controls come back after the run")
        check(c.web.url?.fragment == "mac", "the page reloads on the Mac, url \(c.web.url?.absoluteString ?? "nil")")
        select(1)
        c.scanButton.performClick(nil)
        await idle()
        check(calls.suffix(2) == ["phone dump --save", "history"], "phone scan runs phone dump --save then history")
        check(c.web.url?.fragment == "phone", "the page reloads on the phone")

        // A failing command: footer error, log shown, controls back
        formAnswer = ["fail"]
        c.logToggle.state = .off
        c.toggleLog()
        choose("Run Operation…", in: c.moreMenu().items.first(where: { $0.title == "Home Screen Controls" })?.submenu)
        await idle()
        check(calls.last == "phone op fail", "typed operation runs phone op fail, got \(calls.last ?? "nil")")
        check(c.footer.isError && c.footer.message.contains("failed (exit 1)"), "failure shows an error in the footer, got \(c.footer.message)")
        check(c.log.string.contains("error: fixture failure") && c.log.string.contains("✗ exit 1"), "the log holds the error output and exit status")
        check(!c.logScroll.isHidden && !c.logLine.isHidden && c.logToggle.state == .on, "a failure opens the log")
        check(c.scanButton.isEnabled && c.moreButton.isEnabled, "controls come back after a failure")

        // Log toggle
        c.logToggle.performClick(nil)
        check(c.logScroll.isHidden && c.logLine.isHidden, "Log hides the log")
        c.logToggle.performClick(nil)
        check(!c.logScroll.isHidden && !c.logLine.isHidden, "Log shows the log")

        // Apply Proposal: the menu, the dry run, Cancel, then Apply (Mac)
        select(0)
        let applyMenu = c.applyMenu()
        check(applyMenu?.items.count == 1 && applyMenu?.items.first?.title == "Fixture proposal — proposal.json",
              "Apply lists the Mac's draft, got \(applyMenu?.items.map(\.title) ?? [])")
        check(applyMenu?.items.first?.toolTip == "Fixture note", "the draft's note is its tooltip")
        let before = calls.count
        planAnswer = false
        choose("Fixture proposal — proposal.json", in: applyMenu)
        await idle()
        check(Array(calls.dropFirst(before)).count == 1 && calls.last?.hasSuffix("proposal.json --dry-run") == true && calls.last?.hasPrefix("write ") == true,
              "choosing a draft runs only its dry run, got \(Array(calls.dropFirst(before)))")
        check(plans.last == ["plan: move 2 icons", "plan: group 3 apps"], "the confirmation shows the dry-run plan, got \(plans.last ?? [])")
        planAnswer = true
        choose("Fixture proposal — proposal.json", in: c.applyMenu())
        await idle()
        let afterApply = Array(calls.dropFirst(before + 1))
        check(afterApply.count == 3 && afterApply[0].hasSuffix("proposal.json --dry-run") && afterApply[1].hasPrefix("write ")
              && !afterApply[1].contains("--dry-run") && afterApply[2] == "history",
              "Apply runs the dry run, the write, then history, got \(afterApply)")

        // Apply on the phone, held mid-write: Pause, Resume, Stop
        select(1)
        hold("hold-write", true)
        choose("Fixture proposal — proposal.json", in: c.applyMenu())
        let writing = await wait { c.runner.current?.args.contains("--dry-run") == false }
        check(writing, "the phone write starts")
        check(!c.pauseButton.isHidden && !c.resumeButton.isHidden && !c.stopButton.isHidden, "a phone write shows Pause, Resume and Stop")
        check(!c.scanButton.isEnabled && !c.applyButton.isEnabled && !c.moreButton.isEnabled && !c.picker.isEnabled, "controls are disabled during the write")
        c.pauseButton.performClick(nil)
        check(c.footer.message == "Pausing after the current operation…", "Pause says so in the footer")
        await wait { self.calls.contains("phone op pause") }
        check(calls.contains("phone op pause"), "Pause sends phone op pause")
        c.resumeButton.performClick(nil)
        check(c.footer.message == "Resuming…", "Resume says so in the footer")
        await wait { self.calls.contains("phone op resume") }
        check(calls.contains("phone op resume"), "Resume sends phone op resume")
        c.stopButton.performClick(nil)
        check(c.footer.message == "Stopping after the current operation…", "Stop says so in the footer")
        await idle()
        check(calls.contains("phone op stop"), "Stop on a phone write sends phone op stop")
        check([c.stopButton, c.pauseButton, c.resumeButton].allSatisfy(\.isHidden), "run controls hide after the write ends")
        hold("hold-write", false)

        // Stop on a Mac command interrupts it
        select(0)
        hold("hold-dump", true)
        c.scanButton.performClick(nil)
        await wait { self.calls.last == "dump --save" }
        check(!c.stopButton.isHidden && c.pauseButton.isHidden && c.resumeButton.isHidden, "a Mac scan shows Stop only")
        c.stopButton.performClick(nil)
        await idle()
        check(c.footer.isError && c.footer.message.contains("failed"), "a stopped scan reports a failure, got \(c.footer.message)")
        check(calls.last == "dump --save", "a stopped scan does not re-render, got \(calls.suffix(2))")
        hold("hold-dump", false)

        // More menu, Mac: showcase
        let macMore = c.moreMenu()
        check(macMore.items.map(\.title) == ["Preview Showcase", "Rebuild Showcase"] && macMore.items.allSatisfy(\.isEnabled),
              "Mac More offers the showcase, got \(macMore.items.map(\.title))")
        let showcaseRuns = { self.calls.filter { $0 == "showcase" }.count }
        choose("Preview Showcase", in: macMore)
        await idle()
        check(showcaseRuns() == 1 && c.showingShowcase, "Preview Showcase builds the missing showcase and shows it")
        check(c.band.stateText == "Showcase preview", "the band says Showcase preview, got \(c.band.stateText)")
        check(c.moreMenu().items.first?.title == "Back to History", "More offers Back to History while previewing")
        choose("Back to History", in: c.moreMenu())
        await idle()
        check(!c.showingShowcase && c.web.url?.lastPathComponent == "history.html", "Back to History shows the history page")
        choose("Preview Showcase", in: c.moreMenu())
        await idle()
        check(showcaseRuns() == 1 && c.showingShowcase, "a second preview reuses the built showcase")
        choose("Rebuild Showcase", in: c.moreMenu())
        await idle()
        check(showcaseRuns() == 2, "Rebuild Showcase runs showcase again")
        choose("Back to History", in: c.moreMenu())
        await idle()

        // More menu, phone: usage, new proposal, home screen controls
        select(1)
        let phoneMore = c.moreMenu()
        check(phoneMore.items.map(\.title).prefix(2) == ["Refresh Usage Stats", "New Proposal from Reorg Plan…"], "phone More lists usage and reorg")
        let controls = phoneMore.items.first(where: { $0.title == "Home Screen Controls" })?.submenu
        check(controls?.items.filter { !$0.isSeparatorItem }.map(\.title)
              == ["Peek", "Go to Page…", "Home", "Back", "Lock Layout", "Unlock Layout", "Run Operation…"],
              "Home Screen Controls lists every operation, got \(controls?.items.map(\.title) ?? [])")
        choose("Refresh Usage Stats", in: phoneMore)
        await idle()
        check(calls.suffix(2) == ["phone usage --save", "history"], "Refresh Usage Stats runs usage --save then history")

        let controlCalls: [(String, [String], String)] = [
            ("Peek", [""], "phone op peek"), ("Go to Page…", ["3"], "phone op go 3"), ("Home", [""], "phone op home"),
            ("Back", [""], "phone op back"), ("Lock Layout", [""], "phone op lock on"), ("Unlock Layout", [""], "phone op lock off"),
            ("Run Operation…", ["into office Slack \"Zoom Pro\""], "phone op into office Slack Zoom Pro"),
        ]
        for (title, answer, expected) in controlCalls {
            formAnswer = answer
            let n = calls.count
            choose(title, in: controls)
            await idle()
            // A look changes nothing; an edit re-renders the history page.
            let edits = expected.contains(" into ")
            check(Array(calls.dropFirst(n)) == (edits ? [expected, "history"] : [expected]),
                  "\(title) runs \(expected), got \(Array(calls.dropFirst(n)))")
        }
        for (title, answer) in [("Go to Page…", ["x"]), ("Run Operation…", ["  "])] {
            formAnswer = answer
            let n = calls.count
            choose(title, in: controls)
            await idle()
            check(calls.count == n, "\(title) with \(answer) runs nothing")
        }
        formAnswer = nil
        let cancelled = calls.count
        choose("Go to Page…", in: controls)
        await idle()
        check(calls.count == cancelled, "cancelling a form runs nothing")

        // New proposal from a reorg plan
        let plan = dir.appendingPathComponent("reorg-plan.json")
        fileAnswer = nil
        choose("New Proposal from Reorg Plan…", in: c.moreMenu())
        await idle()
        check(calls.count == cancelled, "cancelling the file picker runs nothing")
        fileAnswer = plan
        formAnswer = ["My title", "My note"]
        choose("New Proposal from Reorg Plan…", in: c.moreMenu())
        await idle()
        check(calls.contains("phone reorg \(plan.path) --title My title --note My note"), "the reorg runs with the title and note")
        let saved = try? String(contentsOf: dir.appendingPathComponent("layouts/phone/proposal-2.json"), encoding: .utf8)
        check(saved?.contains("From plan") == true, "the reorg output is saved as proposal-2.json")
        check(proposals(for: devices[1]).count == 2 && c.applyMenu()?.items.count == 2, "the new draft shows in Apply Proposal")
        check(c.log.string.contains("$ launchpad-map phone reorg"), "the log shows the reorg command")

        // Tablet: nothing is ready, so the operations are disabled but usage stats need only attachment
        select(2)
        let tabletMore = c.moreMenu()
        check(tabletMore.items.first(where: { $0.title == "Refresh Usage Stats" })?.isEnabled == true, "tablet: Refresh Usage Stats needs only an attached device")
        let tabletControls = tabletMore.items.first(where: { $0.title == "Home Screen Controls" })?.submenu
        check(tabletControls?.items.filter { !$0.isSeparatorItem }.allSatisfy { !$0.isEnabled } == true, "tablet: operations are disabled until the screen is on")

        // Showcase decisions from the page
        select(0)
        let reviews = { self.calls.filter { $0.hasPrefix("review") } }
        c.review("Fixture App", show: true)
        await idle()
        check(reviews() == ["review --show Fixture App"], "showing an app runs review --show, got \(reviews())")
        formAnswer = ["private"]
        c.review("Fixture App", show: false)
        await idle()
        check(reviews().last == "review --hide private Fixture App", "hiding an app runs review --hide with the reason, got \(reviews())")
        check(asked.last?.title.contains("Hide “Fixture App”") == true && asked.last?.labels == ["Reason"], "hiding asks for a reason")
        formAnswer = ["   "]
        let reviewCount = reviews().count
        c.review("Other App", show: false)
        await idle()
        check(reviews().count == reviewCount, "hiding with a blank reason runs nothing")
        let historyRuns = calls.filter { $0 == "history" }.count
        let showcaseBuilt = showcaseRuns()
        check(c.rebuild?.isValid == true, "decisions schedule a rebuild")
        c.rebuild?.fire()
        await idle()
        check(calls.filter { $0 == "history" }.count == historyRuns + 1 && showcaseRuns() == showcaseBuilt + 1,
              "the scheduled rebuild saves the history page and the showcase once")

        // Helpers the commands rely on
        check(splitArgs("group 3 \"new folder\" 'a b' c") == ["group", "3", "new folder", "a b", "c"], "splitArgs groups quoted words")
        check(nextProposalFile(in: devices[2].dir).lastPathComponent == "proposal.json", "an empty folder gets proposal.json")
        check(nextProposalFile(in: devices[1].dir).lastPathComponent == "proposal-3.json", "a folder with two drafts gets proposal-3.json")

        c.rebuild?.invalidate()
        c.runner.stop()
    }
}

extension AppDelegate {
    /// Returns the exit status of the run: 0 when every check passed.
    func runSelfTest() async -> Int32 {
        let fm = FileManager.default
        let dir = fm.temporaryDirectory.appendingPathComponent("home-screens-selftest-\(UUID().uuidString)")
        defer { try? fm.removeItem(at: dir) }
        do { try SelfTest.makeCheckout(in: dir) } catch {
            print("self-test: cannot build the fixture checkout: \(error.localizedDescription)")
            return 1
        }
        repoOverride = dir
        pathOverride = "/usr/bin:/bin"
        dataRoot = dir.appendingPathComponent("layouts")
        defer { repoOverride = nil }

        // Nothing may point outside the throwaway checkout.
        let root = dir.path
        let inside = [repo, tool, historyPage, showcasePage, dataRoot].allSatisfy { $0.path.hasPrefix(root) }
            && devices.allSatisfy { $0.dir.path.hasPrefix(root) }
        guard inside else {
            print("self-test: refusing to run: a path of the app points outside the fixture checkout")
            return 1
        }

        let defaults = UserDefaults.standard
        let savedDevice = defaults.object(forKey: "device")
        defaults.set("mac", forKey: "device")
        let run = SelfTestRun(dir: dir)
        controller = run.controller
        await run.run()
        if let savedDevice { defaults.set(savedDevice, forKey: "device") } else { defaults.removeObject(forKey: "device") }
        for failure in run.failures { print("self-test: \(failure)") }
        print(run.failures.isEmpty ? "self-test: every check passed" : "self-test: \(run.failures.count) failed")
        return run.failures.isEmpty ? 0 : 1
    }
}
