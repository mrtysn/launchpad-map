// swift-tools-version: 6.1
import Foundation
import PackageDescription

// The Home Screens app builds against the shared look in project-lifecycle (MacAppStyle).
// Set PROJECT_LIFECYCLE_PACKAGE to a checkout of it to build from disk; without it the
// package is fetched from GitHub. project-lifecycle builds with it set to its own checkout.
// Without that variable (an editor's SourceKit-LSP, a plain `swift build`) the checkout project-lifecycle
// last recorded is used, which keeps a pin of the remote branch out of Package.resolved; only a machine
// that never ran project-lifecycle fetches it.
func recordedCheckout() -> String? {
    let environment = ProcessInfo.processInfo.environment
    let base = environment["XDG_CONFIG_HOME"] ?? ((environment["HOME"] ?? "") + "/.config")
    guard let text = try? String(contentsOfFile: base + "/project-lifecycle/checkout", encoding: .utf8) else { return nil }
    let path = text.trimmingCharacters(in: .whitespacesAndNewlines)
    return FileManager.default.fileExists(atPath: path + "/Package.swift") ? path : nil
}

let shared: Package.Dependency =
    (ProcessInfo.processInfo.environment["PROJECT_LIFECYCLE_PACKAGE"] ?? recordedCheckout()).map { .package(path: $0) }
    ?? .package(url: "git@github.com:mrtysn/project-lifecycle.git", branch: "main")

let package = Package(
    name: "launchpad-map",
    platforms: [.macOS(.v13)],
    dependencies: [shared],
    targets: [
        .executableTarget(
            name: "HomeScreens",
            dependencies: [
                .product(name: "MacAppBase", package: "project-lifecycle"),
                .product(name: "MacAppStyle", package: "project-lifecycle"),
            ],
            path: "app",
            exclude: ["assets", "fixtures", "snapshot.sh"]
        ),
    ],
    swiftLanguageModes: [.v5]
)
