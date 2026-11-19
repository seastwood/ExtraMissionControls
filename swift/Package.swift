// swift-tools-version:5.9
// Extra Mission Controls — native Swift port of the PyObjC app.
//
//   swift build                      # debug build
//   swift test                       # headless logic tests (no Mission Control)
//   ./scripts/build_app.sh           # signed .app + DMG in swift/dist
//   open Package.swift               # work on it in Xcode
import PackageDescription

let package = Package(
    name: "ExtraMissionControls",
    platforms: [.macOS(.v13)],
    dependencies: [
        // In-app updates. Only switched on when the build embeds a Sparkle
        // public key (see swift/README.md → Releases).
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.6.0"),
    ],
    targets: [
        // Everything lives in the library so the test target can reach it.
        .target(
            name: "EMCCore",
            dependencies: [.product(name: "Sparkle", package: "Sparkle")],
            path: "Sources/EMCCore",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("ApplicationServices"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("ServiceManagement"),
            ]
        ),
        .executableTarget(
            name: "ExtraMissionControls",
            dependencies: ["EMCCore"],
            path: "Sources/ExtraMissionControls",
            // The .app embeds Sparkle.framework in Contents/Frameworks.
            linkerSettings: [.unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"])]
        ),
        .testTarget(
            name: "EMCCoreTests",
            dependencies: ["EMCCore"],
            path: "Tests/EMCCoreTests"
        ),
    ]
)
