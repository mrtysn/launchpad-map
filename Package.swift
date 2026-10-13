// swift-tools-version: 6.1
import Foundation
import PackageDescription

// The Home Screens app builds against the shared look in project-lifecycle (MacAppStyle).
// Set PROJECT_LIFECYCLE_PACKAGE to a checkout of it to build from disk; without it the
// package is fetched from GitHub. app/bundle.sh sets it when a sibling checkout exists.
let shared: Package.Dependency =
    ProcessInfo.processInfo.environment["PROJECT_LIFECYCLE_PACKAGE"].map { .package(path: $0) }
    ?? .package(url: "git@github.com:mrtysn/project-lifecycle.git", branch: "main")

let package = Package(
    name: "launchpad-map",
    platforms: [.macOS(.v13)],
    dependencies: [shared],
    targets: [
        .executableTarget(
            name: "HomeScreens",
            dependencies: [.product(name: "MacAppStyle", package: "project-lifecycle")],
            path: "app",
            exclude: ["assets", "bundle.sh", "fixtures"]
        ),
    ],
    swiftLanguageModes: [.v5]
)
