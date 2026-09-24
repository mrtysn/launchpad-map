// Prints "<window id>\t<x>\t<y>\t<width>\t<height>" for the on-screen window
// whose owner name and title match the two arguments (title may be a prefix).
// Exits 1 when there is none.

import CoreGraphics
import Foundation

let args = CommandLine.arguments
guard args.count == 3 else {
    FileHandle.standardError.write("usage: window-id <owner> <title-prefix>\n".data(using: .utf8)!)
    exit(64)
}
let owner = args[1], prefix = args[2]
let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
for w in list {
    guard (w[kCGWindowOwnerName as String] as? String) == owner,
          let title = w[kCGWindowName as String] as? String, title.hasPrefix(prefix),
          let id = w[kCGWindowNumber as String] as? Int,
          let b = w[kCGWindowBounds as String] as? [String: Double] else { continue }
    print("\(id)\t\(Int(b["X"] ?? 0))\t\(Int(b["Y"] ?? 0))\t\(Int(b["Width"] ?? 0))\t\(Int(b["Height"] ?? 0))")
    exit(0)
}
exit(1)
