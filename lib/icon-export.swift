// Reads "<bundleid>\t<app name>" pairs on stdin, one per line (the name is
// optional and used as a fallback when the bundle id is not registered with
// LaunchServices).
// Writes "<bundleid>\t<base64 png>" on stdout for each one that resolves.
// Unresolvable entries produce "<bundleid>\tMISS".

import AppKit

let px = Int(ProcessInfo.processInfo.environment["ICON_PX"] ?? "96") ?? 96
let ws = NSWorkspace.shared

func png(for icon: NSImage, px: Int) -> Data? {
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
    rep.size = NSSize(width: px, height: px)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    icon.draw(in: NSRect(x: 0, y: 0, width: px, height: px),
              from: .zero, operation: .sourceOver, fraction: 1.0)
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [.compressionFactor: 0.9])
}

/// Look for "<name>.app" in the usual places, for apps LaunchServices has not
/// registered under the bundle id Launchpad recorded.
func searchByName(_ name: String) -> String? {
    let roots = ["/Applications", "\(NSHomeDirectory())/Applications",
                 "/System/Applications", "/System/Applications/Utilities",
                 "/Applications/Utilities"]
    let fm = FileManager.default
    for root in roots {
        let direct = "\(root)/\(name).app"
        if fm.fileExists(atPath: direct) { return direct }
        // One level down, e.g. /Applications/Etterna/Etterna.app
        guard let entries = try? fm.contentsOfDirectory(atPath: root) else { continue }
        for entry in entries where !entry.hasSuffix(".app") {
            let nested = "\(root)/\(entry)/\(name).app"
            if fm.fileExists(atPath: nested) { return nested }
        }
    }
    return nil
}

while let line = readLine(strippingNewline: true) {
    let fields = line.components(separatedBy: "\t")
    let bid = fields.first?.trimmingCharacters(in: .whitespaces) ?? ""
    if bid.isEmpty { continue }
    let name = fields.count > 1 ? fields[1] : ""

    var path = ws.urlForApplication(withBundleIdentifier: bid)?.path
    if path == nil, !name.isEmpty { path = searchByName(name) }

    guard let resolved = path, let data = png(for: ws.icon(forFile: resolved), px: px)
    else { print("\(bid)\tMISS"); continue }
    print("\(bid)\t\(data.base64EncodedString())")
}
