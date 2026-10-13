import AppKit
import WebKit

/// Renders the window offscreen from fixture data (app/fixtures), once in each appearance, to
/// `<path minus .png>-light.png` and `-dark.png`. The page is built by lib/render.py from the fixture
/// layouts; no device is asked and no launchpad-map command runs.
@MainActor
func runSnapshot(to path: String) async -> Int32 {
    let fixtures = repo.appendingPathComponent("app/fixtures/layouts")
    dataRoot = fixtures
    let out = URL(fileURLWithPath: path)
    let outDir = out.deletingLastPathComponent()
    try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
    let tablet = fixtures.appendingPathComponent("tablet")
    let page = outDir.appendingPathComponent("snapshot-page.html")

    let render = Process()
    render.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    render.arguments = ["python3", repo.appendingPathComponent("lib/render.py").path,
                        "--layout", tablet.appendingPathComponent("2026-01-10-0900.json").path,
                        "--layout", tablet.appendingPathComponent("2026-01-12-1800.json").path,
                        "--layout", tablet.appendingPathComponent("proposal.json").path,
                        "--icons", tablet.appendingPathComponent("icons.json").path,
                        "--name", "Home screen", "--out", page.path]
    render.environment = environment()
    render.standardError = FileHandle.nullDevice
    guard (try? render.run()) != nil else { print("snapshot: cannot run python3"); return 1 }
    render.waitUntilExit()
    guard render.terminationStatus == 0 else { print("snapshot: lib/render.py failed"); return 1 }

    let controller = Controller(offline: true, page: page)
    let w = controller.window
    w.setFrameAutosaveName("")
    w.setContentSize(NSSize(width: 1400, height: 900))
    controller.picker.selectedSegment = 2
    let status = #"{"attached":true,"ready":false,"model":"Redmi Pad 2","awake":false,"locked":false}"#
    controller.statuses["tablet"] = try? JSONDecoder().decode(Status.self, from: Data(status.utf8))
    controller.refreshControls()

    while controller.web.isLoading { try? await Task.sleep(nanoseconds: 100_000_000) }
    try? await Task.sleep(nanoseconds: 1_000_000_000)

    let stem = out.deletingPathExtension().path
    for scenario in Scenario.allCases {
        scenario.apply(to: controller)
        for (name, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
            w.appearance = NSAppearance(named: appearance)
            w.contentView?.layoutSubtreeIfNeeded()
            try? await Task.sleep(nanoseconds: 800_000_000)
            guard let png = await capture(controller) else { print("snapshot: nothing drawn"); return 1 }
            let file = URL(fileURLWithPath: "\(stem)\(scenario.suffix)-\(name).png")
            do { try png.write(to: file) } catch { print("snapshot: \(error)"); return 1 }
            print("snapshot: wrote \(file.path)")
        }
    }
    return 0
}

/// The window's frame view (title bar included) with the web page drawn over its blank area: a web
/// view does not appear in a plain view snapshot.
@MainActor
private func capture(_ controller: Controller) async -> Data? {
    guard let frameView = controller.window.contentView?.superview,
          let rep = frameView.bitmapImageRepForCachingDisplay(in: frameView.bounds) else { return nil }
    frameView.cacheDisplay(in: frameView.bounds, to: rep)
    let base = NSImage(size: frameView.bounds.size)
    base.addRepresentation(rep)
    let web = controller.web
    let shot: NSImage? = await withCheckedContinuation { cont in
        web.takeSnapshot(with: nil) { image, _ in cont.resume(returning: image) }
    }
    let webRect = frameView.convert(web.bounds, from: web)
    let size = frameView.bounds.size
    let scale = max(1, CGFloat(rep.pixelsWide) / size.width)
    guard let out = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale),
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                     colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
    out.size = size
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: out)
    base.draw(in: NSRect(origin: .zero, size: size))
    shot?.draw(in: webRect)
    NSGraphicsContext.restoreGraphicsState()
    return out.representation(using: .png, properties: [:])
}

/// The footer and command states the window shows besides "Ready", set directly with stub text: no
/// command runs and nothing is read from a device.
@MainActor
enum Scenario: CaseIterable {
    case ready, running, failed, failedLong

    var suffix: String {
        switch self {
        case .ready: return ""
        case .running: return "-running"
        case .failed: return "-error"
        case .failedLong: return "-error-long"
        }
    }

    func apply(to c: Controller) {
        let stub = ["$ launchpad-map tablet write proposal.json", "page 1: moved 3 icons", "page 2: moved 5 icons"]
        let stopped = [c.stopButton, c.pauseButton, c.resumeButton]
        switch self {
        case .ready:
            c.footer.show("Ready", announce: false)
        case .running:
            c.logToggle.state = .on
            c.toggleLog()
            c.log.string = stub.joined(separator: "\n")
            for b in stopped { b.isHidden = false }
            c.spinner.startAnimation(nil)
            c.footer.show("page 2: moved 5 icons, then 4 icons from the Games folder to page 3 and the dock, waiting for the screen to settle before the next operation", announce: false)
        case .failed:
            for b in stopped { b.isHidden = true }
            c.spinner.stopAnimation(nil)
            c.log.string = (stub + ["error: device offline", "✗ exit 1"]).joined(separator: "\n")
            c.footer.show("Scanning the Redmi Pad 2: failed (exit 1); see the log", error: true)
        case .failedLong:
            c.window.setContentSize(NSSize(width: 900, height: 900))
            c.footer.show("Applying the proposal on the Redmi Pad 2: failed (exit 1); see the log, the device went offline after page 2 and the remaining operations were not run: operations 7 to 12 of 12 never started", error: true)
        }
        c.refreshControls()
    }
}
