import Foundation

struct OwnerLayer: Hashable {
    var owner: String
    var layer: Int
}

/// Processes that host Mission Control's windows: the Dock through macOS 26,
/// WindowManager on macOS 27.
let missionControlWindowOwners: Set<String> = ["Dock", "WindowManager"]

/// Counter of (owner, layer) over the Dock's/WindowManager's on-screen windows.
func ownerLayerCounts(_ raw: [CGWin]) -> [OwnerLayer: Int] {
    var counts: [OwnerLayer: Int] = [:]
    for entry in raw where missionControlWindowOwners.contains(entry.owner) {
        counts[OwnerLayer(owner: entry.owner, layer: entry.layer), default: 0] += 1
    }
    return counts
}

/// The verified macOS 13-26 rule: a layer-18 backdrop window, or several in
/// layers 18-20. One resident full-screen layer-20 window exists PERMANENTLY
/// while any fullscreen space does, so mere presence in 18-20 is NOT "MC is
/// up". Mission Control itself adds more: a layer-18 window plus another at 20.
func legacyBackdropRule(_ counts: [OwnerLayer: Int]) -> Bool {
    var inBand = 0
    for (key, n) in counts where key.owner == "Dock" && (18...20).contains(key.layer) {
        if key.layer == 18 { return true }
        inBand += n
    }
    return inBand >= 2
}

/// Decides "is Mission Control's backdrop up?" from ONE WindowServer query,
/// so the idle poll never has to wake the Dock or WindowManager.
///
/// macOS 13-26: the verified legacy rule. macOS 27 moved the overview into
/// WindowManager and its backdrop windows aren't documented, so there the rule
/// is LEARNED: while unknown, the idle poll asks the AX tree directly (and
/// records the Dock/WindowManager window counts as an idle baseline); the
/// first AX-confirmed session diffs its window counts against that baseline,
/// and the windows that appeared become the rule. The rule is checked against
/// every later confirmed session, re-learned if it misses, dropped if it fires
/// repeatedly with no Mission Control, and stored per OS build once it has held
/// twice. A learned rule also gets a slow AX fallback check.
final class BackdropRule {
    enum Mode: String { case legacy, unknown, learned }

    struct Clause: Equatable {
        var owner: String
        var layer: Int
        var minCount: Int
    }

    static let fallbackInterval = 2.0
    static let maxFalseHits = 12
    static let holdsToPersist = 2
    static let defaultsKey = "MissionControlBackdropRule"

    private(set) var mode: Mode
    private(set) var rule: [Clause] = []
    private var holds = 0
    private var falseHits = 0
    private var baseline: [OwnerLayer: Int] = [:]
    private var sample: [OwnerLayer: Int] = [:]
    private var lastFallback = 0.0
    private var ruleFired = false
    private let defaults: UserDefaults

    init(major: Int = AX.macOSMajor(), defaults: UserDefaults = .standard) {
        self.defaults = defaults
        mode = (13...26).contains(major) ? .legacy : .unknown
        if mode == .unknown { load() }
    }

    private var osBuild: String { ProcessInfo.processInfo.operatingSystemVersionString }

    private func load() {
        guard let stored = defaults.dictionary(forKey: Self.defaultsKey),
              stored["os"] as? String == osBuild,
              let raw = stored["rule"] as? [[Any]] else { return }
        var parsed: [Clause] = []
        for item in raw {
            guard item.count == 3, let owner = item[0] as? String,
                  let layer = (item[1] as? NSNumber)?.intValue,
                  let n = (item[2] as? NSNumber)?.intValue else { return }
            parsed.append(Clause(owner: owner, layer: layer, minCount: n))
        }
        if !parsed.isEmpty {
            mode = .learned
            rule = parsed
            holds = Self.holdsToPersist
        }
    }

    private func save() {
        defaults.set(["os": osBuild,
                      "rule": rule.map { [$0.owner, $0.layer, $0.minCount] as [Any] }],
                     forKey: Self.defaultsKey)
    }

    private func forget() {
        mode = .unknown
        rule = []
        holds = 0
        falseHits = 0
        defaults.removeObject(forKey: Self.defaultsKey)
    }

    private func learnedFires(_ counts: [OwnerLayer: Int]) -> Bool {
        rule.contains { counts[OwnerLayer(owner: $0.owner, layer: $0.layer), default: 0] >= $0.minCount }
    }

    /// For the per-frame pulse while Mission Control is up. Only the verified
    /// legacy rule may end a session early; otherwise report present and let
    /// the AX liveness check end it.
    func present(_ counts: [OwnerLayer: Int]) -> Bool {
        mode == .legacy ? legacyBackdropRule(counts) : true
    }

    /// Idle poll: true when the AX tree is worth asking.
    func idleShouldCheck(_ counts: [OwnerLayer: Int], now: Double) -> Bool {
        ruleFired = false
        sample = counts
        switch mode {
        case .legacy:
            return legacyBackdropRule(counts)
        case .learned:
            if learnedFires(counts) {
                ruleFired = true
                return true
            }
            if now - lastFallback >= Self.fallbackInterval {
                lastFallback = now
                return true
            }
            return false
        case .unknown:
            return true
        }
    }

    /// The AX tree just confirmed Mission Control is closed: this tick's window
    /// counts are a clean idle baseline to diff a session against.
    func idleCheckFoundNothing() {
        baseline = sample
        if mode == .learned && ruleFired {
            falseHits += 1
            if falseHits >= Self.maxFalseHits { forget() }
        }
    }

    /// Mission Control confirmed up by the AX tree, `counts` taken now.
    func confirmed(_ counts: [OwnerLayer: Int]) {
        if mode == .legacy { return }
        falseHits = 0
        if mode == .learned && learnedFires(counts) {
            holds += 1
            if holds == Self.holdsToPersist { save() }
            return
        }
        let learned = counts.compactMap { key, n -> Clause? in
            let base = baseline[key, default: 0]
            return n > base ? Clause(owner: key.owner, layer: key.layer, minCount: base + 1) : nil
        }
        if learned.isEmpty {
            forget()
        } else {
            mode = .learned
            rule = learned
            holds = 1
        }
    }
}
