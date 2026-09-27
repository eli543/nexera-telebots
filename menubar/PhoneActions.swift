import Foundation

/// The panel and the phone use one clock and one per-bot cooldown.
struct PhoneActionGate {
    private var last: [String: TimeInterval] = [:]
    let interval: TimeInterval = 10

    func remaining(_ keys: [String], now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> Int {
        Int(ceil(keys.compactMap { last[$0] }.map { max(0, interval - (now - $0)) }.max() ?? 0))
    }

    mutating func record(_ keys: [String], now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        for key in keys { last[key] = now }
    }
}

struct PhoneActionRequest: Decodable {
    let action: String
    let bot: String?
    let confirm: Bool?

    static func parse(_ data: Data) -> PhoneActionRequest? {
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              Set(object.keys).isSubset(of: ["action", "bot", "confirm"]),
              let request = try? JSONDecoder().decode(Self.self, from: data),
              ["reconnect", "fresh", "reconnect_all_down", "watchdog_on", "watchdog_off", "snapshot", "restore"].contains(request.action)
        else { return nil }
        if request.action == "reconnect" || request.action == "fresh" {
            guard let bot = request.bot, !bot.isEmpty, bot.utf8.count <= 128 else { return nil }
        } else if request.bot != nil { return nil }
        return request
    }
}
