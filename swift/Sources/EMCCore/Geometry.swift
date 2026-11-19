import ApplicationServices
import Foundation

/// RGB hover colour (0-1 components).
struct RGB: Equatable, Hashable {
    var r: Double
    var g: Double
    var b: Double
    init(_ r: Double, _ g: Double, _ b: Double) { self.r = r; self.g = g; self.b = b }
}

/// Top-left-origin rectangle in global AX/CG coordinates.
struct Rect: Equatable, Hashable {
    var x: Double
    var y: Double
    var w: Double
    var h: Double

    init(_ x: Double, _ y: Double, _ w: Double, _ h: Double) {
        self.x = x; self.y = y; self.w = w; self.h = h
    }

    func contains(_ px: Double, _ py: Double) -> Bool {
        x <= px && px <= x + w && y <= py && py <= y + h
    }
}

/// One thing EMC can put buttons on or scrim: a Mission Control window
/// thumbnail, a Spaces Bar tile, a Dock icon, or the Show Desktop tile. The
/// Python version passed dicts with these keys around interchangeably.
struct Tile {
    var title: String = ""
    var desc: String = ""
    var x: Double = 0
    var y: Double = 0
    var width: Double = 0
    var height: Double = 0
    /// AXPress focuses the window (thumbnail) or switches to the space (tile).
    var element: AXUIElement?
    var bundle: String?
    var space: Int?
    var fullscreen = false
    // Per-button copies (see MissionControlController.slot).
    var slot = 0
    var label: String?
    var hover: RGB?

    var rect: Rect { Rect(x, y, width, height) }

    var rounded: RoundedRect {
        RoundedRect(x: Int(x.rounded()), y: Int(y.rounded()),
                    w: Int(width.rounded()), h: Int(height.rounded()))
    }
}

struct RoundedRect: Equatable, Hashable {
    var x: Int
    var y: Int
    var w: Int
    var h: Int
}

/// One window in a rounded-bounds signature: (window number, x, y, w, h).
struct WindowSig: Equatable, Hashable {
    var number: Int
    var x: Int
    var y: Int
    var w: Int
    var h: Int

    var bounds: [Int] { [x, y, w, h] }
}

enum Motion {
    /// Mission Control's hover effect lifts the window under the cursor by
    /// 10-25px (more on macOS 27); real transitions move every window
    /// 50-300px per frame. Deltas at or under this are not "motion".
    static let tolerance = 30.0
    /// A lone window moving up to this far is a hover lift, not an animation.
    static let hoverLiftMax = 160.0

    /// Whether two window-bounds signatures differ MEANINGFULLY: any window
    /// appearing or vanishing counts, as does drift beyond `tol` px on two or
    /// more windows, or a single window jumping more than hoverLiftMax.
    static func moved(_ prev: [WindowSig]?, _ cur: [WindowSig]?, tol: Double = tolerance) -> Bool {
        guard let prev = prev, let cur = cur else { return false }
        if prev.count != cur.count { return true }
        var old: [Int: [Int]] = [:]
        for entry in prev { old[entry.number] = entry.bounds }
        var movedCount = 0
        for entry in cur {
            guard let bounds = old[entry.number] else { return true }
            let now = entry.bounds
            let delta = Double(zip(bounds, now).map { abs($0 - $1) }.max() ?? 0)
            if delta > tol {
                // Hover lift scales the thumbnail. A translation without any
                // size change is a pan/drag, even with only one window visible.
                let resized = abs(bounds[2] - now[2]) > 1 || abs(bounds[3] - now[3]) > 1
                if delta > hoverLiftMax || tol <= 0 || !resized { return true }
                movedCount += 1
                if movedCount >= 2 { return true }
            }
        }
        return false
    }

    /// Debug: per-window bounds deltas between two signatures.
    static func diff(_ prev: [WindowSig]?, _ cur: [WindowSig]?) -> String {
        guard let prev = prev, let cur = cur else { return "(no baseline)" }
        var old: [Int: [Int]] = [:]
        for entry in prev { old[entry.number] = entry.bounds }
        var out: [String] = []
        for entry in cur {
            if let b = old[entry.number] {
                if b != entry.bounds {
                    out.append("\(entry.number):\(zip(entry.bounds, b).map { $0 - $1 })")
                }
            } else {
                out.append("\(entry.number):new")
            }
        }
        let gone = Set(old.keys).subtracting(cur.map { $0.number })
        out += gone.map { "\($0):gone" }
        return out.isEmpty ? "(none)" : out.joined(separator: " ")
    }
}
