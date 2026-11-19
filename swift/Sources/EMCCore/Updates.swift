import Foundation

/// Check GitHub for a newer release than the running version.
enum Updates {
    static let latestURL = URL(string: "https://api.github.com/repos/seastwood/ExtraMissionControls/releases/latest")!
    static let releasesPage = "https://github.com/seastwood/ExtraMissionControls/releases"
    static let ignoreKey = "EMCIgnoredUpdateVersion"

    /// The release the user chose to skip. A newer release still notifies.
    static var ignoredVersion: String? {
        get { UserDefaults.standard.string(forKey: ignoreKey) }
        set { UserDefaults.standard.set(newValue, forKey: ignoreKey) }
    }

    /// "EMC_0.9.2" / "v0.9.2" / "0.9.2" -> [0, 9, 2]; nil if unparseable.
    static func parse(_ tag: String?) -> [Int]? {
        var tag = (tag ?? "").trimmingCharacters(in: .whitespaces)
        for prefix in ["EMC_", "v", "V"] where tag.hasPrefix(prefix) {
            tag = String(tag.dropFirst(prefix.count))
        }
        let parts = tag.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
        guard !parts.isEmpty, parts.allSatisfy({ $0 != nil }) else { return nil }
        return parts.map { $0! }
    }

    /// Python tuple ordering: element by element, then the longer wins.
    static func isNewer(_ a: [Int], than b: [Int]) -> Bool {
        for (x, y) in zip(a, b) where x != y { return x > y }
        return a.count > b.count
    }

    /// Fetch the latest release in the background; on the main thread call
    /// `onUpdate(version, url)` only if it is newer than the running version.
    /// Failures are silently ignored — a missed check just means no badge.
    static func checkAsync(_ onUpdate: @escaping (String, String) -> Void) {
        var request = URLRequest(url: latestURL, cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: 15)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: request) { data, _, error in
            guard error == nil, let data = data,
                  let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let latest = parse(info["tag_name"] as? String),
                  let current = parse(EMCVersion.current),
                  isNewer(latest, than: current) else { return }
            let version = latest.map(String.init).joined(separator: ".")
            let url = info["html_url"] as? String ?? releasesPage
            DispatchQueue.main.async { onUpdate(version, url) }
        }.resume()
    }
}
