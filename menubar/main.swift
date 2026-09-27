// main.swift - Telebots Bar, a macOS menu bar monitor for a Nexera Telebots fleet.
//
// The status item draws one dot per bot (filled = connected, hollow = not) next to
// the connected count, and the count turns red when any bot is down. Clicking it
// opens a Liquid Glass panel: one row per bot with a Reconnect capsule, details on
// click, Reconnect all down, the watchdog switch, Snapshot, Restore, Refresh and a
// legend with Quit. Health, reconnect, window cleanup and the watchdog switch live
// in Fleet.swift; refreshes run off the main thread and land whole.
//
// Command line (see README.md): --dump, --reconnect <bot> [--yes] [--dry-run],
// --fresh-start <bot> (--yes | --dry-run), --toggle-watchdog [--dry-run],
// --snapshot [--dry-run], --restore [--dry-run], --render-preview <png> [--demo],
// --render-icon <dir.iconset>, and for the menu bar app itself --dry-run,
// --open-panel (alias --open-menu), --demo [--appearance light|dark], --launchd.

import AppKit
import SwiftUI

let appVersion = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "dev"
let appBundleID = Bundle.main.bundleIdentifier ?? "com.nexera.telebots-menubar"

// MARK: - Design tokens

enum Palette {
    static func isDark(_ appearance: NSAppearance) -> Bool {
        appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
    }

    /// The single semantic color: a bot that is down or has no poller. A deeper red in
    /// light appearance and a lighter one in dark, so text in it holds 4.5:1.
    static let alertNS = NSColor(name: NSColor.Name("TelebotsAlert")) { appearance in
        isDark(appearance)
            ? NSColor(srgbRed: 1.00, green: 0.42, blue: 0.40, alpha: 1)
            : NSColor(srgbRed: 0.77, green: 0.09, blue: 0.11, alpha: 1)
    }
    static let alert = Color(nsColor: alertNS)
    /// The alert red as it sits on the black logo tile in either appearance.
    static let alertOnTile = Color(red: 1.00, green: 0.42, blue: 0.40)
    /// A deeper red for the filled Reconnect all down button, so white text on it
    /// stays above 4.5:1 in both appearances.
    static let alertFill = Color(red: 0.75, green: 0.08, blue: 0.10)
    /// The same red tuned for text on glass: deep red on light, light red on dark,
    /// both above 4.5:1 on the row and header backgrounds measured on real glass.
    static let alertTextNS = NSColor(name: NSColor.Name("TelebotsAlertText")) { appearance in
        isDark(appearance)
            ? NSColor(srgbRed: 1.00, green: 0.61, blue: 0.59, alpha: 1)
            : NSColor(srgbRed: 0.62, green: 0.06, blue: 0.09, alpha: 1)
    }
    static let alertText = Color(nsColor: alertTextNS)

    /// Monochrome tint for the switch and the spinner: charcoal on light, silver on dark.
    static let monoNS = NSColor(name: NSColor.Name("TelebotsMono")) { appearance in
        isDark(appearance) ? NSColor(white: 0.56, alpha: 1) : NSColor(white: 0.16, alpha: 1)
    }
    static let mono = Color(nsColor: monoNS)

    /// All text: the system primary label color, drawn as a plain color so glass
    /// vibrancy cannot thin it out. Measured on real glass, the secondary label
    /// color fell to 2.6-4.4:1, so hierarchy comes from size and weight instead.
    static let label = Color(nsColor: .labelColor)

    /// Solid ink for the connected dots and ring segments (near black / near white).
    static let inkNS = NSColor(name: NSColor.Name("TelebotsInk")) { appearance in
        isDark(appearance) ? NSColor(white: 0.96, alpha: 1) : NSColor(white: 0.11, alpha: 1)
    }
    static let ink = Color(nsColor: inkNS)

    /// A light neutral veil under the panel content. It keeps the glass translucent
    /// but stops a bright or busy backdrop from pulling text below 4.5:1.
    static let scrimNS = NSColor(name: NSColor.Name("TelebotsScrim")) { appearance in
        isDark(appearance) ? NSColor(white: 0, alpha: 0.30) : NSColor(white: 1, alpha: 0.30)
    }
    static let scrim = Color(nsColor: scrimNS)

    /// The logo tile, near black (#131313).
    static let tileNS = NSColor(srgbRed: 0.075, green: 0.075, blue: 0.075, alpha: 1)
    static let tile = Color(nsColor: tileNS)
}

enum Space {
    static let xs: CGFloat = 4
    static let s: CGFloat = 8
    static let m: CGFloat = 12
    static let l: CGFloat = 16
}

enum Radius {
    static let panel: CGFloat = 24
    static let group: CGFloat = 20
    static let row: CGFloat = 14
    static let tile: CGFloat = 9
}

// MARK: - The six-dot motif, drawn in code

/// Marks the menu bar icon can carry next to the dots.
enum MenuMark: Equatable {
    case watchdogOff
    case telegramDown

    var symbol: String {
        switch self {
        case .watchdogOff: return "shield.slash"
        case .telegramDown: return "antenna.radiowaves.left.and.right.slash"
        }
    }

    var meaning: String {
        switch self {
        case .watchdogOff: return "Auto-recovery is off: the watchdog will not restart a bot that drops"
        case .telegramDown: return "Telegram is unreachable from this Mac: bots cannot hear or reply until the network is back"
        }
    }
}

enum DotArt {
    /// Up to three bots sit on one row; more fill two rows (six is a 2 x 3 grid).
    static func grid(_ count: Int) -> (rows: Int, cols: Int) {
        let n = max(count, 1)
        return n <= 3 ? (1, n) : (2, (n + 1) / 2)
    }

    /// Dots in fleet order, left to right, top row first.
    static func drawDots(_ states: [Bool], in rect: NSRect, dot: CGFloat, gap: CGFloat,
                         on: NSColor, off: NSColor, ring: CGFloat) {
        let (rows, cols) = grid(states.count)
        let width = CGFloat(cols) * dot + CGFloat(cols - 1) * gap
        let height = CGFloat(rows) * dot + CGFloat(rows - 1) * gap
        let x0 = rect.midX - width / 2
        let top = rect.midY + height / 2 - dot
        for (i, connected) in states.enumerated() {
            let r = CGFloat(i / cols), c = CGFloat(i % cols)
            let circle = NSRect(x: x0 + c * (dot + gap), y: top - r * (dot + gap), width: dot, height: dot)
            if connected {
                on.setFill()
                NSBezierPath(ovalIn: circle).fill()
            } else {
                off.setStroke()
                let path = NSBezierPath(ovalIn: circle.insetBy(dx: ring / 2, dy: ring / 2))
                path.lineWidth = ring
                path.stroke()
            }
        }
    }

    static func bitmap(pixelsWide: Int, pixelsHigh: Int, points: NSSize) -> NSBitmapImageRep? {
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixelsWide, pixelsHigh: pixelsHigh,
                                   bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                   colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)
        rep?.size = points
        return rep
    }

    static func draw(into rep: NSBitmapImageRep, _ body: () -> Void) {
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSGraphicsContext.current?.imageInterpolation = .high
        body()
        NSGraphicsContext.restoreGraphicsState()
    }

    static let statusDot: CGFloat = 5
    static let statusGap: CGFloat = 2
    static let markGap: CGFloat = 4

    static func dotsWidth(_ count: Int) -> CGFloat {
        let cols = CGFloat(grid(count).cols)
        return cols * statusDot + (cols - 1) * statusGap
    }

    static func markImage(_ mark: MenuMark) -> NSImage? {
        NSImage(systemSymbolName: mark.symbol, accessibilityDescription: mark.meaning)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 10, weight: .semibold))
    }

    static func statusSize(_ count: Int, marks: [MenuMark] = []) -> NSSize {
        let markWidth = marks.reduce(CGFloat(0)) { $0 + markGap + ceil(markImage($1)?.size.width ?? 12) }
        return NSSize(width: ceil(dotsWidth(count) + markWidth), height: 16)
    }

    /// A menu bar mark (auto-recovery off, Telegram unreachable) in one color.
    static func drawMark(_ mark: MenuMark, x: CGFloat, height: CGFloat, color: NSColor) -> CGFloat {
        guard let symbol = markImage(mark) else { return 12 }
        let size = symbol.size
        let tinted = NSImage(size: size, flipped: false) { rect in
            symbol.draw(in: rect)
            color.set()
            rect.fill(using: .sourceAtop)
            return true
        }
        tinted.draw(in: NSRect(x: x, y: floor((height - size.height) / 2), width: size.width, height: size.height))
        return ceil(size.width)
    }

    /// Dots, then any marks, in one color, into the current context.
    static func drawStatus(_ states: [Bool], marks: [MenuMark], height: CGFloat, color: NSColor, originX: CGFloat = 0, originY: CGFloat = 0) {
        let dots = NSRect(x: originX, y: originY, width: dotsWidth(states.count), height: height)
        drawDots(states, in: dots, dot: statusDot, gap: statusGap, on: color, off: color, ring: 1.2)
        var x = dots.maxX
        for mark in marks {
            x += markGap
            let context = NSGraphicsContext.current
            context?.saveGraphicsState()
            let transform = NSAffineTransform()
            transform.translateX(by: 0, yBy: originY)
            transform.concat()
            x += drawMark(mark, x: x, height: height, color: color)
            context?.restoreGraphicsState()
        }
    }

    /// The menu bar icon: a template image with 1x and 2x reps, so the menu bar tints
    /// it for light and dark.
    static func statusImage(_ states: [Bool], marks: [MenuMark] = []) -> NSImage {
        let size = statusSize(states.count, marks: marks)
        let image = NSImage(size: size)
        for scale in [1, 2] {
            guard let rep = bitmap(pixelsWide: Int(size.width) * scale, pixelsHigh: Int(size.height) * scale, points: size) else { continue }
            draw(into: rep) {
                drawStatus(states, marks: marks, height: size.height, color: .black)
            }
            image.addRepresentation(rep)
        }
        image.isTemplate = true
        return image
    }

    /// The app icon: the six-dot motif on a black rounded square (macOS icon grid:
    /// an 824/1024 square with a 185/1024 corner radius).
    static func appIcon(pixels: Int) -> NSBitmapImageRep? {
        let s = CGFloat(pixels)
        guard let rep = bitmap(pixelsWide: pixels, pixelsHigh: pixels, points: NSSize(width: s, height: s)) else { return nil }
        draw(into: rep) {
            let inset = s * 100 / 1024
            let square = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
            let radius = s * 185.4 / 1024
            let tile = NSBezierPath(roundedRect: square, xRadius: radius, yRadius: radius)
            Palette.tileNS.setFill()
            tile.fill()
            if pixels >= 64 {
                NSColor(white: 1, alpha: 0.12).setStroke()
                let rim = NSBezierPath(roundedRect: square.insetBy(dx: s / 1024, dy: s / 1024), xRadius: radius, yRadius: radius)
                rim.lineWidth = max(1, s / 512)
                rim.stroke()
            }
            let dot = square.width * 0.15
            drawDots(Array(repeating: true, count: 6), in: square, dot: dot, gap: square.width * 0.085,
                     on: NSColor(white: 0.97, alpha: 1), off: .white, ring: dot * 0.2)
        }
        return rep
    }
}

// MARK: - Store

/// One bot's row, as the panel and the text dump show it.
struct FleetRow: Identifiable, Equatable {
    let id: String
    let name: String
    var state: BotState
    var statusText: String          // "12s", "No poller", "Down"
    var statusIcon: String?         // the pulse glyph beside a heartbeat age
    var statusTip: String
    var dumpStatus: String
    var spokenStatus: String
    var detailLine: String          // "Opus 5.5 · max · 41% ctx" (+ restarts when above zero)
    var detailTip: String
    var stall: Bool
    var uptimeLine: String?         // shown when the row is expanded
    var pulse: Int
    var connected: Bool { state == .connected }
}

/// Watchdog and Telegram state for the header line and the menu bar marks.
struct HeaderStatus: Equatable {
    var watchdogOffFor: [String]      // bot names whose kill switch is set
    var watchdogTotal: Int
    var watchdogAvailable: Bool = true
    var lastPassAge: Int?             // seconds since the watchdog's last pass
    var telegramReachable: Bool?      // nil when unknown
}

/// A line under a row (or under the bottom group) that stays until it no longer
/// applies: a failed action, or a short note about what happened instead.
struct RowNote: Equatable {
    let text: String
    let isError: Bool
    let at: TimeInterval
    let session: pid_t?               // the session running when it was written
}

/// Everything one refresh computes, built off the main thread and handed over whole.
struct RefreshResult {
    var configProblem: String?
    var config: FleetConfig?
    var healths: [BotHealth] = []
    var rows: [FleetRow] = []
    var watchdog: WatchdogSwitches?
    var header: HeaderStatus?
    var marks: [MenuMark] = []
    var snapshotAvailable = false
    var snapshotTip = "No snapshot saved"
    var reachable: Bool?
    var windowNote: String?
}

/// The refresh work: the process scan, transcript reads and the network probe. It
/// runs on one serial queue, and its caches live there, never on the main thread
/// (hence unchecked Sendable: `queue` is the only way in).
final class RefreshWorker: @unchecked Sendable {
    let queue = DispatchQueue(label: "com.nexera.telebots-menubar.refresh", qos: .utility)
    private var contextCache: [String: (session: pid_t, value: Int?, served: String?, servedAt: TimeInterval?, at: Date)] = [:]
    /// Model family to its newest full name seen in any bot's transcript ("opus" to
    /// "Opus 5.5"). Kept in memory, so it survives the sibling it came from going down.
    private var familyNames: [String: (name: String, at: TimeInterval)] = [:]
    private var records: [String: WindowRecord]?
    private let probe = TelegramProbe()
    private let probeLock = NSLock()
    private var probeResult: (reachable: Bool, at: Date)?
    private var probing = false
    private let timeOfDay: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "h:mma"
        f.amSymbol = "am"
        f.pmSymbol = "pm"
        f.timeZone = FleetModel.defaultTimeZone
        return f
    }()

    func stop() { probe.cancel() }

    /// One whole refresh. Called on `queue` only.
    func compute(recordWindows: Bool) -> RefreshResult {
        var result = RefreshResult()
        let cfg: FleetConfig
        switch FleetModel.loadConfig() {
        case .problem(let message, _):
            result.configProblem = message
            return result
        case .ok(let loaded):
            cfg = loaded
        }
        result.config = cfg
        timeOfDay.timeZone = cfg.settings.timeZone
        let now = Int(Date().timeIntervalSince1970)
        let table = ProcessTable.snapshot()
        let signals = FleetModel.signals(cfg, now: now)
        var reachable = signals.telegramReachable
        if reachable == nil {
            reachable = currentProbe()
            startProbeIfDue()
        }
        result.reachable = reachable
        let trust = FleetModel.heartbeatTrust(cfg, now: now, probe: reachable)
        let healths = cfg.bots.map {
            FleetModel.evaluate($0, table: table, now: now, maxAge: cfg.settings.heartbeatMaxAge, trust: trust)
        }
        result.healths = healths
        var allIndicators = healths.map { indicators($0, cfg) }
        // Learn each family's newest full name from the bots that have replied...
        for ind in allIndicators {
            guard let served = ind.servedModel else { continue }
            let family = FleetModel.modelFamily(served)
            let at = ind.servedAt ?? 0
            if let known = familyNames[family], known.at > at { continue }
            familyNames[family] = (served, at)
        }
        // ...and lend it to a bot on the same family that has not replied yet.
        var borrowed = Set<String>()
        for i in allIndicators.indices where allIndicators[i].servedModel == nil {
            if let alias = allIndicators[i].model, let known = familyNames[FleetModel.modelFamily(alias)] {
                allIndicators[i].model = known.name
                borrowed.insert(healths[i].bot.key)
            }
        }
        for (health, ind) in zip(healths, allIndicators) {
            var row = makeRow(health, ind, now: now)
            if borrowed.contains(health.bot.key), let model = ind.model {
                row.detailTip = "\(model): the model this session was launched on, named from a sibling on the same model until this bot's first reply"
                    + (health.state == .down ? " (it is down, so this is the model it starts with)" : "")
                    + ". Context used appears after its first reply."
            }
            result.rows.append(row)
        }
        let switches = FleetModel.readWatchdog(cfg)
        result.watchdog = switches
        let status = HeaderStatus(watchdogOffFor: switches.disabled.map(\.name), watchdogTotal: cfg.bots.count,
                                  watchdogAvailable: switches.available,
                                  lastPassAge: signals.lastPass.map { max(0, now - Int($0)) },
                                  telegramReachable: reachable)
        result.header = status
        if !status.watchdogOffFor.isEmpty || !status.watchdogAvailable || (status.lastPassAge ?? 0) > 180 {
            result.marks.append(.watchdogOff)
        }
        if status.telegramReachable == false { result.marks.append(.telegramDown) }
        result.snapshotAvailable = signals.snapshotExists
        result.snapshotTip = signals.snapshotExists
            ? "Snapshot saved · \(signals.snapshotSessions) sessions · \(signals.snapshotSaved.map { clockTime($0) + " ET" } ?? "time unknown"). Restore reopens each bot that is not running in its saved conversation and turns auto-recovery back on."
            : "No snapshot saved"
        if recordWindows { result.windowNote = recordWindowsIfChanged(healths, table) }
        return result
    }

    /// Keeps $FLEET_HOME/menubar-windows.json current: each running session's tab,
    /// so its window can be proven to be the bot's own after the session is gone. A
    /// bot that is down keeps its last record.
    private func recordWindowsIfChanged(_ healths: [BotHealth], _ table: ProcessTable) -> String? {
        if records == nil { records = WindowRecords.load() }
        var next = records ?? [:]
        var changed: [String] = []
        for health in healths {
            guard let pid = health.sessionPIDs.first, let record = table.windowRecord(session: pid) else { continue }
            if let old = next[health.bot.key], old.sameSession(record) { continue }
            next[health.bot.key] = record
            changed.append("\(health.bot.key) on \(record.tty) (session \(pid), hosted by \(record.hostList))")
        }
        guard !changed.isEmpty else { return nil }
        guard WindowRecords.save(next) else { return "windows: could not write \(WindowRecords.path)" }
        records = next
        return "windows recorded: " + changed.joined(separator: "; ")
    }

    private func currentProbe() -> Bool? {
        probeLock.lock()
        defer { probeLock.unlock() }
        return probeResult?.reachable
    }

    private func startProbeIfDue() {
        probeLock.lock()
        let due = !probing && (probeResult.map { Date().timeIntervalSince($0.at) >= 60 } ?? true)
        if due { probing = true }
        probeLock.unlock()
        guard due else { return }
        probe.start { [weak self] ok in
            guard let self else { return }
            self.probeLock.lock()
            self.probeResult = (ok, Date())
            self.probing = false
            self.probeLock.unlock()
        }
    }

    /// Indicators for one bot; the context read (up to 5 MB of transcript) is cached
    /// for 60 seconds per session.
    private func indicators(_ health: BotHealth, _ cfg: FleetConfig) -> BotIndicators {
        let session = health.sessionPIDs.first ?? 0
        let cached = contextCache[health.bot.key]
        let fresh = cached.map { $0.session == session && Date().timeIntervalSince($0.at) < 60 } ?? false
        var ind = FleetModel.indicators(health, cfg, withContext: !fresh)
        if fresh, let cached {
            ind.contextPercent = cached.value
            if let served = cached.served {
                ind.servedModel = served
                ind.servedAt = cached.servedAt
                ind.model = served
            }
        } else {
            contextCache[health.bot.key] = (session, ind.contextPercent, ind.servedModel, ind.servedAt, Date())
        }
        return ind
    }

    func clockTime(_ epoch: TimeInterval) -> String {
        timeOfDay.string(from: Date(timeIntervalSince1970: epoch))
    }

    private func uptime(_ seconds: Int) -> String {
        let s = max(0, seconds)
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m" }
        if s < 86_400 { return String(format: "%dh %02dm", s / 3600, (s % 3600) / 60) }
        return "\(s / 86_400)d \((s % 86_400) / 3600)h"
    }

    static func spokenAge(_ seconds: Int) -> String {
        let s = max(0, seconds)
        func unit(_ n: Int, _ word: String) -> String { "\(n) \(word)\(n == 1 ? "" : "s")" }
        if s < 60 { return unit(s, "second") }
        if s < 3600 { return unit(s / 60, "minute") }
        if s < 86_400 { return unit(s / 3600, "hour") }
        return unit(s / 86_400, "day")
    }

    private func makeRow(_ h: BotHealth, _ ind: BotIndicators, now: Int) -> FleetRow {
        let status: String
        var icon: String?
        let tip: String
        let spoken: String
        let dump: String
        switch h.state {
        case .connected:
            if let age = h.heartbeatAge {
                status = shortAge(age)
                icon = "waveform.path.ecg"
                if let excuse = h.staleExcused {
                    tip = "Heartbeat \(Self.spokenAge(age)) ago, older than the watchdog's limit, but not a fault right now: \(excuse). The poller reconnects on its own once Telegram answers again."
                    spoken = "connected, heartbeat \(Self.spokenAge(age)) ago, waiting on Telegram"
                    dump = "Connected · heartbeat \(shortAge(age)) ago, waiting on Telegram"
                } else {
                    tip = "Heartbeat \(Self.spokenAge(age)) ago: the Telegram poller's own liveness ping, written after every poll. It is not the time of the last message."
                    spoken = "connected, heartbeat \(Self.spokenAge(age)) ago"
                    dump = "Connected · heartbeat \(shortAge(age)) ago"
                }
            } else {
                status = "Connected"
                tip = "Connected: the session and its Telegram poller are both running (this poller writes no heartbeat file)."
                spoken = "connected"
                dump = "Connected"
            }
        case .noPoller:
            status = "No poller"
            tip = "No poller: the session is running but its Telegram poller is gone or stopped polling, so this bot hears nothing. Reconnect restarts it through the watchdog's engine and keeps the conversation."
            spoken = "no poller, not receiving messages"
            dump = "No poller"
        case .down:
            status = "Down"
            tip = "Down: this bot's session is not running. Reconnect starts it again through the watchdog's engine, resumed into its latest conversation."
            spoken = "down, session not running"
            dump = "Down"
        }
        var parts: [String] = []
        if let model = ind.model { parts.append(model) }
        if let effort = ind.effort { parts.append(effort) }
        if let ctx = ind.contextPercent { parts.append("\(ctx)% ctx") }
        if ind.restartsLastHour > 0 { parts.append("\(ind.restartsLastHour) restart\(ind.restartsLastHour == 1 ? "" : "s")/h") }
        let detailLine = parts.isEmpty ? (h.state == .down ? "not running" : "no session details yet") : parts.joined(separator: " · ")
        var detailTip = h.state == .down
            ? "The model and effort this bot starts with (its saved model-state)"
            : "The model that served this session's newest reply (or its launch model before the first reply), its effort"
        if let ctx = ind.contextPercent { detailTip += ", \(ctx)% of its context window used" }
        if ind.restartsLastHour > 0 { detailTip += ", and \(ind.restartsLastHour) watchdog restart\(ind.restartsLastHour == 1 ? "" : "s") in the last hour" }
        detailTip += "."
        var uptimeLine: String?
        if let started = ind.sessionStarted {
            uptimeLine = "Up \(uptime(now - Int(started))) · \(ind.launchMode) \(clockTime(started))"
        } else if let beat = ind.lastHeartbeat {
            uptimeLine = "Down since \(clockTime(beat))"
        } else {
            uptimeLine = "Down"
        }
        return FleetRow(id: h.bot.key, name: h.bot.name, state: h.state, statusText: status, statusIcon: icon,
                        statusTip: tip, dumpStatus: dump, spokenStatus: spoken, detailLine: detailLine, detailTip: detailTip,
                        stall: ind.stallSuspected, uptimeLine: uptimeLine, pulse: 0)
    }
}

@MainActor
final class FleetStore: ObservableObject {
    enum Mode { case live, dryRun, demo, snapshot }

    typealias Row = FleetRow

    @Published private(set) var rows: [Row] = []
    @Published private(set) var problem: String?
    @Published private(set) var watchdog: WatchdogSwitches?
    @Published private(set) var inFlight: Set<String> = []
    @Published private(set) var succeeded: Set<String> = []
    @Published var expanded: Set<String> = []
    @Published private(set) var reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    @Published private(set) var header: HeaderStatus?
    @Published private(set) var marks: [MenuMark] = []
    @Published var showLegend = false
    @Published private(set) var snapshotAvailable = false
    @Published private(set) var snapshotTip = "No snapshot saved"
    @Published private(set) var snapshotBusy = false
    @Published private(set) var restoreBusy = false
    @Published private(set) var confirmation: String?
    /// Per-row lines that stay until they no longer apply (failures, what happened instead).
    @Published private(set) var notes: [String: RowNote] = [:]
    /// The bottom group's own lasting line: a switch, snapshot or restore that failed.
    @Published private(set) var footerNote: RowNote?

    let mode: Mode
    private(set) var config: FleetConfig?
    var onChange: (() -> Void)?
    private var lastStates: [String: BotState] = [:]
    private var lastSessions: [String: pid_t] = [:]
    private var didFirstRefresh = false
    private var lastProblem: String?
    private var lastHeaderProblem: String?
    private var loggedWarnings: [String] = []
    private var confirmationToken = 0
    private let worker = RefreshWorker()
    private var refreshing = false
    private var refreshPending = false
    private var pendingReason: String?
    private let reconnectQueue = DispatchQueue(label: "com.nexera.telebots-menubar.reconnect")
    private let engineQueue = DispatchQueue(label: "com.nexera.telebots-menubar.engines")
    private let timeOfDay: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "h:mma"
        f.amSymbol = "am"
        f.pmSymbol = "pm"
        f.timeZone = FleetModel.defaultTimeZone
        return f
    }()

    init(mode: Mode) { self.mode = mode }

    var total: Int { rows.count }
    var connectedCount: Int { rows.filter(\.connected).count }
    var anyDown: Bool { problem == nil && rows.contains { !$0.connected } }
    var canReconnectAll: Bool { problem == nil && rows.contains { !$0.connected && !inFlight.contains($0.id) } }
    var alarm: Bool { problem != nil || connectedCount < total }
    var countText: String { problem != nil ? "?" : "\(connectedCount)/\(total)" }
    var summary: String { problem != nil ? "Fleet config problem" : "\(connectedCount) of \(total) online" }
    var actionsLive: Bool { mode == .live || mode == .dryRun }
    var badge: String? {
        switch mode {
        case .demo: return "DEMO"
        case .dryRun: return "DRY RUN"
        default: return nil
        }
    }
    var watchdogCaption: String {
        guard let w = watchdog else { return "Unavailable" }
        if !w.available { return "Unavailable: no watchdog state folder" }
        if w.isOn { return "Auto-recovery on" }
        if w.isFullyOff { return "Auto-recovery off" }
        return "Auto-recovery off for " + w.disabled.map(\.name).joined(separator: ", ")
    }

    /// The one short red header line, only when self-healing or Telegram is broken.
    var headerProblem: String? {
        guard let h = header else { return nil }
        var parts: [String] = []
        if !h.watchdogAvailable {
            parts.append("Auto-recovery unavailable")
        } else if let age = h.lastPassAge, age > 180 {
            parts.append("Watchdog not running")
        } else if !h.watchdogOffFor.isEmpty {
            parts.append(h.watchdogOffFor.count >= h.watchdogTotal
                         ? "Auto-recovery off"
                         : "Auto-recovery off for " + h.watchdogOffFor.joined(separator: ", "))
        }
        if h.telegramReachable == false { parts.append("Telegram unreachable") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func log(_ message: String) {
        guard actionsLive else { return }
        ActionLog.shared.write(message)
    }

    func refreshReduceMotion() {
        reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    func stop() { worker.stop() }

    func logStartup(owner: String) {
        guard actionsLive else { return }
        var line = "start: Telebots Bar \(appVersion) (pid \(getpid()), \(mode == .dryRun ? "dry run" : "live"), started \(owner))"
        switch FleetModel.loadConfig() {
        case .ok(let cfg):
            let engine = FleetModel.resolveFreshEngine(cfg)
            line += ", config \(cfg.path), \(cfg.bots.count) bots, engine \(engine.path ?? "none (\(engine.note))")"
            line += ", watchdog state \(cfg.settings.watchdogStateDir), heartbeat limit \(cfg.settings.heartbeatMaxAge)s"
            line += ", resume from \(cfg.settings.resumeSource.rawValue)"
            line += ", snapshot \(cfg.settings.snapshotEngine), restore \(cfg.settings.restoreEngine)"
        case .problem(let message, _):
            line += ", config problem: \(message)"
        }
        log(line)
        DispatchQueue.global(qos: .utility).async {
            let status = FleetModel.terminalAutomation(askUser: false)
            ActionLog.shared.write("start: Terminal automation for Reconnect is \(status.text)")
        }
    }

    // MARK: Refresh

    /// A refresh off the main thread; the result lands in one piece. Refreshes asked
    /// for while one runs fold into a single follow-up.
    func refresh(reason: String? = nil) {
        guard mode != .demo else { onChange?(); return }
        if refreshing {
            refreshPending = true
            if let reason { pendingReason = reason }
            return
        }
        refreshing = true
        let worker = self.worker
        let record = mode == .live
        worker.queue.async { [weak self] in
            let result = worker.compute(recordWindows: record)
            Task { @MainActor in
                guard let self else { return }
                self.refreshing = false
                self.apply(result, reason: reason)
                if self.refreshPending {
                    self.refreshPending = false
                    let next = self.pendingReason
                    self.pendingReason = nil
                    self.refresh(reason: next)
                }
            }
        }
    }

    /// The same refresh, waited for: the command line and previews only.
    func refreshNow(reason: String? = nil) {
        guard mode != .demo else { onChange?(); return }
        let worker = self.worker
        let record = mode == .live
        let result = worker.queue.sync { worker.compute(recordWindows: record) }
        apply(result, reason: reason)
    }

    private func apply(_ result: RefreshResult, reason: String?) {
        if let message = result.configProblem {
            config = nil
            problem = message
            rows = []
            watchdog = nil
            header = nil
            marks = []
            if message != lastProblem { log("config problem: \(message)") }
            lastProblem = message
            onChange?()
            return
        }
        guard let cfg = result.config else { return }
        if lastProblem != nil { log("config readable again: \(cfg.path)") }
        lastProblem = nil
        if cfg.warnings != loggedWarnings {
            for warning in cfg.warnings { log("config warning: \(warning)") }
            loggedWarnings = cfg.warnings
        }
        config = cfg
        problem = nil
        ActionLog.shared.setTimeZone(cfg.settings.timeZone)
        timeOfDay.timeZone = cfg.settings.timeZone
        var previous: [String: Row] = [:]
        for row in rows { previous[row.id] = row }
        var next: [Row] = []
        for (health, fresh) in zip(result.healths, result.rows) {
            var row = fresh
            var pulse = previous[health.bot.key]?.pulse ?? 0
            let before = lastStates[health.bot.key]
            if let before, before != health.state {
                pulse += 1
                log("\(health.bot.key): \(before.rawValue) -> \(health.state.rawValue) (\(detail(health)))")
            }
            lastStates[health.bot.key] = health.state
            row.pulse = pulse
            next.append(row)
            settleNote(health, before: before)
        }
        rows = next
        watchdog = result.watchdog
        header = result.header
        marks = result.marks
        snapshotAvailable = result.snapshotAvailable
        snapshotTip = result.snapshotTip
        if let note = result.windowNote { log(note) }
        if let odd = result.watchdog?.odd, !odd.isEmpty, didFirstRefresh == false {
            log("watchdog: not regular files, ignored like the watchdog ignores them: " + odd.joined(separator: ", "))
        }
        let problemNow = headerProblem
        if problemNow != lastHeaderProblem {
            if didFirstRefresh { log("header: \(problemNow ?? "all quiet (auto-recovery on, Telegram reachable)")") }
            lastHeaderProblem = problemNow
        }
        let switchLabel = result.watchdog?.label.lowercased() ?? "watchdog: unknown"
        if !didFirstRefresh {
            didFirstRefresh = true
            log("first refresh: \(result.healths.count) bots evaluated, \(connectedCount)/\(total) connected, \(switchLabel)"
                + ", watchdog last pass \(result.header?.lastPassAge.map { "\($0)s ago" } ?? "unknown")"
                + ", Telegram \(result.reachable.map { $0 ? "reachable" : "unreachable" } ?? "unknown")"
                + ", snapshot \(result.snapshotAvailable ? "saved" : "none")")
            for (health, row) in zip(result.healths, rows) {
                log("  \(health.bot.key): \(health.statusLine) (\(detail(health))) \(row.detailLine)\(row.stall ? " · stall suspected" : "")")
            }
        }
        if let reason { log("\(reason): \(connectedCount)/\(total) connected, \(switchLabel)") }
        onChange?()
    }

    /// A failure note stays until the bot comes back (it turns connected, or it is
    /// connected on a new session) or a new action starts; a plain note fades after a
    /// minute.
    private func settleNote(_ health: BotHealth, before: BotState?) {
        let key = health.bot.key
        let session = health.sessionPIDs.first
        defer { if let session { lastSessions[key] = session } }
        guard let note = notes[key] else { return }
        if note.isError {
            guard health.state == .connected else { return }
            let cameBack = before != nil && before != .connected
            let newSession = session != nil && session != note.session
            if cameBack || newSession { notes[key] = nil }
        } else if Date().timeIntervalSince1970 - note.at > 60 {
            notes[key] = nil
        }
    }

    private func detail(_ h: BotHealth) -> String {
        let session = h.sessionPIDs.isEmpty ? "none" : h.sessionPIDs.map(String.init).joined(separator: ",")
        let poller = h.pollerPIDs.isEmpty ? "none" : h.pollerPIDs.map(String.init).joined(separator: ",")
        var beat = h.heartbeatAge.map { "heartbeat \(shortAge($0)) ago" + (h.heartbeatStale ? " (stale)" : "") } ?? "no heartbeat"
        if h.staleExcused != nil { beat += ", not a fault while Telegram is unconfirmed" }
        return "session \(session), poller \(poller), \(beat), launch \(h.launchMode)"
    }

    func clockTime(_ epoch: TimeInterval) -> String {
        timeOfDay.string(from: Date(timeIntervalSince1970: epoch))
    }

    func noteText(_ note: RowNote) -> String { "\(note.text) · \(clockTime(note.at))" }

    func toggleExpanded(_ id: String) {
        if expanded.contains(id) { expanded.remove(id) } else { expanded.insert(id) }
    }

    // MARK: Actions

    /// Reconnect from a row. The bot's state is read fresh off the main thread; a
    /// connected bot asks first. The action itself re-checks everything again right
    /// before the engine runs.
    func requestReconnect(_ key: String) {
        guard actionsLive else { return }
        guard let cfg = config, let bot = cfg.bots.first(where: { $0.key == key }), !inFlight.contains(key) else { return }
        inFlight.insert(key)
        notes[key] = nil
        let engine = FleetModel.resolveFreshEngine(cfg).path ?? "the restart engine"
        // Not the reconnect queue: a restart already running there must not hold up
        // this read-only check.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let health = FleetModel.evaluateOne(bot, cfg)
            let plan = FleetModel.makePlan(health, cfg, engine: engine)
            Task { @MainActor in
                guard let self else { return }
                var confirmed = false
                if health.state == .connected {
                    NSApp.activate()
                    let alert = NSAlert()
                    alert.alertStyle = .warning
                    alert.messageText = "Restart \(bot.name) even though it is connected? It keeps the conversation."
                    alert.informativeText = plan.keepsConversation
                        ? "It resumes conversation \(plan.resumeArg.prefix(8)) in a new Terminal window."
                        : "No saved conversation was found, so it starts a new session on the same model."
                    alert.addButton(withTitle: "Cancel")
                    alert.addButton(withTitle: "Restart")
                    guard alert.runModal() == .alertSecondButtonReturn else {
                        self.inFlight.remove(key)
                        self.log("reconnect \(key): cancelled at the confirm prompt (it is connected)")
                        return
                    }
                    confirmed = true
                }
                self.startReconnect(bot, cfg, allowConnected: confirmed, freshStart: false, origin: "Reconnect \(bot.name)")
            }
        }
    }

    /// Fresh start: a new, empty conversation through the /fresh path. Always asks.
    func requestFreshStart(_ key: String) {
        guard actionsLive else { return }
        guard let cfg = config, let bot = cfg.bots.first(where: { $0.key == key }), !inFlight.contains(key) else { return }
        NSApp.activate()
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Start \(bot.name) fresh?"
        alert.informativeText = "This ends \(bot.name)'s current conversation and starts a new, empty one (the /fresh path). Reconnect keeps the conversation instead."
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "Fresh start")
        guard alert.runModal() == .alertSecondButtonReturn else {
            log("fresh start \(key): cancelled at the confirm prompt")
            return
        }
        inFlight.insert(key)
        notes[key] = nil
        startReconnect(bot, cfg, allowConnected: true, freshStart: true, origin: "Fresh start \(bot.name)")
    }

    func reconnectAllDown() {
        guard actionsLive, let cfg = config else { return }
        let targets = rows.filter { !$0.connected && !inFlight.contains($0.id) }
        guard !targets.isEmpty else { return }
        log("Reconnect all down: \(targets.map(\.id).joined(separator: ", "))")
        for row in targets {
            if let bot = cfg.bots.first(where: { $0.key == row.id }) {
                inFlight.insert(bot.key)
                notes[bot.key] = nil
                startReconnect(bot, cfg, allowConnected: false, freshStart: false, origin: "Reconnect all down")
            }
        }
    }

    private func startReconnect(_ bot: Bot, _ cfg: FleetConfig, allowConnected: Bool, freshStart: Bool, origin: String) {
        inFlight.insert(bot.key)
        let dryRun = mode == .dryRun
        log("\(origin): queued \(bot.key)\(allowConnected && !freshStart ? " (confirmed restart of a connected bot)" : "")")
        reconnectQueue.async { [weak self] in
            let outcome = FleetModel.performReconnect(bot, cfg, allowConnected: allowConnected, dryRun: dryRun,
                                                      askForPermission: true, freshStart: freshStart,
                                                      log: { ActionLog.shared.write($0) })
            Task { @MainActor in self?.finishReconnect(bot, outcome, freshStart: freshStart) }
        }
    }

    private func finishReconnect(_ bot: Bot, _ outcome: ReconnectOutcome, freshStart: Bool) {
        inFlight.remove(bot.key)
        let now = Date().timeIntervalSince1970
        let session = lastSessions[bot.key]
        switch outcome {
        case .connected:
            notes[bot.key] = nil
            succeeded.insert(bot.key)
            Task { @MainActor [weak self] in
                try? await Task.sleep(for: .seconds(2.5))
                self?.succeeded.remove(bot.key)
            }
        case .healedBeforeRun:
            notes[bot.key] = RowNote(text: "Connected again before the restart ran, so nothing was restarted",
                                     isError: false, at: now, session: session)
        case .dryRun:
            notes[bot.key] = RowNote(text: "Dry run: the \(freshStart ? "fresh start" : "reconnect") was logged, nothing ran",
                                     isError: false, at: now, session: session)
        case .permissionBlocked:
            notes[bot.key] = RowNote(text: "\(freshStart ? "Fresh start" : "Reconnect") \(outcome.failureText ?? "did not run")",
                                     isError: true, at: now, session: session)
            showPermissionAlert(bot.name + "'s Terminal window")
        default:
            let what = freshStart ? "Fresh start" : "Reconnect"
            notes[bot.key] = RowNote(text: "\(what) \(outcome.failureText ?? "failed, see menubar.log")",
                                     isError: true, at: now, session: session)
        }
        refresh()
    }

    private func showPermissionAlert(_ what: String) {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = "Telebots Bar can't open \(what)"
        alert.informativeText = "This needs permission to control Terminal, and macOS has not confirmed it (Terminal must be running for macOS to answer). Allow Telebots Bar in System Settings > Privacy & Security > Automation, then try again. Nothing was restarted."
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    func setWatchdog(on: Bool) {
        guard actionsLive, let cfg = config else { return }
        log("watchdog switch set \(on ? "on" : "off") from the panel")
        let problems = FleetModel.applyWatchdog(on: on, cfg, dryRun: mode == .dryRun, log: { ActionLog.shared.write($0) })
        if problems.isEmpty {
            footerNote = nil
        } else {
            footerNote = RowNote(text: "Auto-recovery switch: " + problems.joined(separator: "; "), isError: true,
                                 at: Date().timeIntervalSince1970, session: nil)
        }
        refresh()
    }

    /// Snapshot: the fleet snapshot engine, with a spinner, then a short confirmation.
    func snapshotNow() {
        guard actionsLive, let cfg = config, !snapshotBusy, !restoreBusy else { return }
        snapshotBusy = true
        let dryRun = mode == .dryRun
        log("Snapshot pressed")
        engineQueue.async { [weak self] in
            let run = FleetModel.runFleetEngine(.snapshot, cfg, dryRun: dryRun, log: { ActionLog.shared.write($0) })
            Task { @MainActor in self?.finishSnapshot(run, dryRun: dryRun) }
        }
    }

    private func finishSnapshot(_ run: EngineRun?, dryRun: Bool) {
        snapshotBusy = false
        refresh()
        let now = Date().timeIntervalSince1970
        guard let run else {
            footerNote = RowNote(text: "Snapshot could not run, see menubar.log", isError: true, at: now, session: nil)
            return
        }
        if dryRun { return confirm("Dry run · snapshot command logged") }
        guard run.code == 0, let cfg = config else {
            footerNote = RowNote(text: "Snapshot failed: \(run.summary)", isError: true, at: now, session: nil)
            return
        }
        footerNote = nil
        let signals = FleetModel.signals(cfg, now: Int(now))
        let when = signals.snapshotSaved.map { clockTime($0) + " ET" } ?? "just now"
        confirm("Snapshot saved · \(signals.snapshotSessions) sessions · \(when)")
    }

    /// Restore: every bot that is down says so first, the fleet restore engine runs,
    /// then each bot that comes back says "Back and ready" and its old window closes.
    func restoreNow() {
        guard actionsLive, let cfg = config, !snapshotBusy, !restoreBusy else { return }
        restoreBusy = true
        let dryRun = mode == .dryRun
        log("Restore pressed")
        engineQueue.async { [weak self] in
            let report = FleetModel.performRestore(cfg, dryRun: dryRun, askForPermission: true,
                                                   log: { ActionLog.shared.write($0) })
            Task { @MainActor in self?.finishRestore(report, dryRun: dryRun) }
        }
    }

    private func finishRestore(_ report: RestoreReport, dryRun: Bool) {
        restoreBusy = false
        let now = Date().timeIntervalSince1970
        if let blocked = report.blocked {
            footerNote = RowNote(text: "Restore did not run: Terminal automation is \(blocked.text)", isError: true, at: now, session: nil)
            showPermissionAlert("the bots' Terminal windows")
            refresh()
            return
        }
        guard let run = report.run else {
            footerNote = RowNote(text: "Restore could not run, see menubar.log", isError: true, at: now, session: nil)
            refresh()
            return
        }
        if dryRun {
            refresh()
            return confirm("Dry run · restore command logged")
        }
        guard run.code == 0 else {
            footerNote = RowNote(text: "Restore failed: \(run.summary)", isError: true, at: now, session: nil)
            refresh()
            return
        }
        footerNote = nil
        for key in report.notBack {
            notes[key] = RowNote(text: "Restore did not bring it back within a minute", isError: true, at: now,
                                 session: lastSessions[key])
        }
        refresh()
        let back = report.back.count
        confirm(report.wasDown.isEmpty
                ? "Restore ran · no bot was down"
                : "Restored · \(back) of \(report.wasDown.count) back")
    }

    /// A brief inline confirmation under the bottom group that fades after 6 seconds.
    private func confirm(_ text: String) {
        log("confirmation: \(text)")
        confirmationToken += 1
        let token = confirmationToken
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) { confirmation = text }
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(6))
            guard let self, self.confirmationToken == token else { return }
            withAnimation(self.reduceMotion ? nil : .easeOut(duration: 0.4)) { self.confirmation = nil }
        }
    }

    func quit() {
        log("quit from the panel")
        NSApp.terminate(nil)
    }

    // MARK: Text forms

    var iconText: String {
        let dots = rows.map { $0.connected ? "●" : "○" }
        let cols = DotArt.grid(max(dots.count, 1)).cols
        return stride(from: 0, to: dots.count, by: cols)
            .map { dots[$0..<min($0 + cols, dots.count)].joined() }
            .joined(separator: "/")
    }

    func dumpText() -> String {
        var out: [String] = []
        let markText = marks.map { $0 == .watchdogOff ? "[shield-slash: auto-recovery off]" : "[antenna-slash: Telegram unreachable]" }
        var itemParts: [String] = ["Status item: " + (iconText.isEmpty ? "?" : iconText)]
        itemParts.append(contentsOf: markText)
        itemParts.append(countText + "  (" + (alarm ? "red" : "monochrome") + ")")
        out.append(itemParts.joined(separator: "  "))
        out.append("TELEBOTS · \(summary)")
        if let headerProblem { out.append("  \(headerProblem)  (red)") }
        if let problem { out.append("  \(problem)") }
        let width = rows.map(\.name.count).max() ?? 0
        let statusWidth = rows.map(\.dumpStatus.count).max() ?? 0
        for row in rows {
            let name = row.name.padding(toLength: width, withPad: " ", startingAt: 0)
            let status = row.dumpStatus.padding(toLength: statusWidth, withPad: " ", startingAt: 0)
            out.append("  \(row.connected ? "●" : "○") \(name)  \(status)  \(row.detailLine)\(row.stall ? " · stall suspected" : "")  [Reconnect]")
        }
        if anyDown { out.append("[Reconnect all down]") }
        out.append("Watchdog: \(watchdogCaption) [switch \(watchdog?.available == false ? "unavailable" : (watchdog?.isOn == true ? "on" : "off"))]")
        out.append("[Snapshot]  [Restore\(snapshotAvailable ? "" : " (disabled: no snapshot saved)")]  [Refresh]")
        if let cfg = config { out.append("config: \(cfg.path)") }
        return out.joined(separator: "\n")
    }

    // MARK: Demo (clearly labelled, never used for real decisions)

    enum DemoFrame { case main, legend, snapshot }

    static func demo(_ frame: DemoFrame = .main) -> FleetStore {
        let store = FleetStore(mode: .demo)
        var names = ["Main", "Research", "Ops", "Support", "Sales", "Night"]
        if case .ok(let cfg) = FleetModel.loadConfig(), cfg.bots.count >= 6 {
            names = cfg.bots.prefix(6).map(\.name)
        }
        func row(_ i: Int, _ state: BotState, _ status: String, detail: String, stall: Bool = false,
                 uptime: String? = nil) -> Row {
            let beating = state == .connected
            return Row(id: "demo-\(i)", name: names[i], state: state, statusText: status,
                       statusIcon: beating ? "waveform.path.ecg" : nil,
                       statusTip: beating
                           ? "Heartbeat \(status) ago: the Telegram poller's own liveness ping, written after every poll. It is not the time of the last message."
                           : "\(status): this bot cannot receive or answer messages.",
                       dumpStatus: status, spokenStatus: status, detailLine: detail,
                       detailTip: "Model and effort of the running session and context window used.",
                       stall: stall, uptimeLine: uptime, pulse: 0)
        }
        switch frame {
        case .main, .legend:
            store.rows = [
                row(0, .connected, "12s", detail: "Opus 5.5 · max · 41% ctx", uptime: "Up 5h 02m · resumed 7:46am"),
                row(1, .connected, "3s", detail: "Opus 5.5 · max · 63% ctx", stall: true, uptime: "Up 1h 20m · fresh 11:28am"),
                row(2, .noPoller, "No poller", detail: "Opus 5.5 · max · 12% ctx · 2 restarts/h",
                    uptime: "Up 2h 14m · resumed 10:34am"),
                row(3, .noPoller, "No poller", detail: "Fable 5.1 · high · 18% ctx", uptime: "Up 38m · fresh 12:10pm"),
                row(4, .down, "Down", detail: "Opus 5.5 · max · 1 restart/h", uptime: "Down since 12:31pm"),
                row(5, .connected, "1s", detail: "Opus 5.5 · max · 7% ctx", uptime: "Up 1m · resumed 12:47pm"),
            ]
            store.inFlight = ["demo-3"]
            store.succeeded = ["demo-5"]
            store.expanded = ["demo-2"]
            store.notes = ["demo-4": RowNote(text: "Reconnect failed: engine exit 4, refused: that pid runs in another workdir (sibling protection)",
                                             isError: true, at: Date().timeIntervalSince1970, session: nil)]
            store.watchdog = WatchdogSwitches(dir: "demo", dirExists: true, disabled: [], odd: [], total: 6)
            store.header = HeaderStatus(watchdogOffFor: [], watchdogTotal: 6, lastPassAge: 12, telegramReachable: true)
            store.showLegend = frame == .legend
        case .snapshot:
            store.rows = [
                row(0, .connected, "12s", detail: "Opus 5.5 · max · 41% ctx"),
                row(1, .connected, "3s", detail: "Opus 5.5 · max · 63% ctx"),
                row(2, .connected, "9s", detail: "Opus 5.5 · max · 12% ctx"),
                row(3, .connected, "20s", detail: "Fable 5.1 · high · 18% ctx"),
                row(4, .connected, "5s", detail: "Opus 5.5 · max · 22% ctx"),
                row(5, .connected, "1s", detail: "Opus 5.5 · max · 7% ctx"),
            ]
            let bots = names.prefix(6).map { Bot(key: $0.lowercased(), name: $0, workdir: "", stateDir: "") }
            store.watchdog = WatchdogSwitches(dir: "demo", dirExists: true, disabled: Array(bots), odd: [], total: 6)
            store.header = HeaderStatus(watchdogOffFor: Array(names.prefix(6)), watchdogTotal: 6, lastPassAge: 12,
                                        telegramReachable: true)
            store.marks = [.watchdogOff]
            store.snapshotAvailable = true
            store.snapshotTip = "Snapshot saved · 6 sessions · 12:41pm ET. Restore reopens each bot that is not running in its saved conversation and turns auto-recovery back on."
            store.confirmation = "Snapshot saved · 6 sessions · 12:41pm ET"
        }
        return store
    }
}

// MARK: - SwiftUI helpers
//
// Liquid Glass on macOS 26 and later, .ultraThinMaterial before it. ImageRenderer
// cannot draw glass or the AppKit-backed controls (switch, spinner, glass buttons),
// so --render-preview sets flatPreview and these helpers draw flat stand-ins with
// the same geometry. The live panel never uses the stand-ins.

private struct FlatPreviewKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var flatPreview: Bool {
        get { self[FlatPreviewKey.self] }
        set { self[FlatPreviewKey.self] = newValue }
    }
}

struct GlassSurface: ViewModifier {
    let cornerRadius: CGFloat
    @Environment(\.flatPreview) private var flat

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        if flat {
            content
                .background(shape.fill(Color.primary.opacity(0.05)))
                .overlay(shape.strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5))
        } else if #available(macOS 26.0, *) {
            content.glassEffect(.regular, in: .rect(cornerRadius: cornerRadius))
        } else {
            content.background(.ultraThinMaterial, in: shape)
        }
    }
}

struct ActionStyle: ViewModifier {
    enum Shape { case capsule, circle }
    let shape: Shape
    let prominent: Bool
    let tint: Color
    @Environment(\.flatPreview) private var flat

    func body(content: Content) -> some View {
        if flat {
            content.buttonStyle(FlatButtonStyle(circle: shape == .circle, prominent: prominent, tint: tint))
        } else if #available(macOS 26.0, *) {
            switch (shape, prominent) {
            case (.circle, _): content.buttonStyle(.glass).buttonBorderShape(.circle)
            case (.capsule, true): content.buttonStyle(.glassProminent).tint(tint)
            case (.capsule, false): content.buttonStyle(.glass)
            }
        } else {
            switch (shape, prominent) {
            case (.circle, _): content.buttonStyle(.bordered).buttonBorderShape(.circle)
            case (.capsule, true): content.buttonStyle(.borderedProminent).buttonBorderShape(.capsule).tint(tint)
            case (.capsule, false): content.buttonStyle(.bordered).buttonBorderShape(.capsule)
            }
        }
    }
}

struct FlatButtonStyle: ButtonStyle {
    let circle: Bool
    let prominent: Bool
    let tint: Color
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.controlSize) private var controlSize

    func makeBody(configuration: Configuration) -> some View {
        let large = controlSize == .large
        let fill = prominent ? tint : Color.primary.opacity(0.09)
        return configuration.label
            .padding(.horizontal, circle ? 7 : (large ? 14 : 10))
            .padding(.vertical, circle ? 7 : (large ? 8 : 4))
            .foregroundStyle(prominent ? Color.white : Palette.label)
            .background {
                if circle { Circle().fill(fill) } else { Capsule().fill(fill) }
            }
            .overlay {
                if circle {
                    Circle().strokeBorder(Color.primary.opacity(prominent ? 0 : 0.14), lineWidth: 0.5)
                } else {
                    Capsule().strokeBorder(Color.primary.opacity(prominent ? 0 : 0.14), lineWidth: 0.5)
                }
            }
            .opacity(isEnabled ? 1 : 0.4)
    }
}

struct SwitchStyleChoice: ViewModifier {
    @Environment(\.flatPreview) private var flat

    func body(content: Content) -> some View {
        if flat { content.toggleStyle(FlatSwitchStyle()) } else { content.toggleStyle(.switch) }
    }
}

struct FlatSwitchStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 8) {
            configuration.label
            ZStack(alignment: configuration.isOn ? .trailing : .leading) {
                Capsule().fill(configuration.isOn ? Palette.mono : Color.primary.opacity(0.18))
                Circle().fill(Color.white).padding(2)
            }
            .frame(width: 32, height: 18)
        }
    }
}

/// The in-row spinner (a static arc in the flat preview).
struct RowSpinner: View {
    @Environment(\.flatPreview) private var flat

    var body: some View {
        if flat {
            Circle()
                .trim(from: 0, to: 0.72)
                .stroke(Palette.mono, style: StrokeStyle(lineWidth: 1.6, lineCap: .round))
                .frame(width: 12, height: 12)
                .rotationEffect(.degrees(-90))
                .frame(width: 16, height: 16)
        } else {
            ProgressView()
                .controlSize(.small)
                .scaleEffect(0.7)
                .tint(Palette.mono)
                .frame(width: 16, height: 16)
        }
    }
}

extension View {
    func glassSurface(cornerRadius: CGFloat) -> some View {
        modifier(GlassSurface(cornerRadius: cornerRadius))
    }

    func capsuleButton(prominent: Bool, tint: Color = Palette.mono) -> some View {
        modifier(ActionStyle(shape: .capsule, prominent: prominent, tint: tint))
    }

    func circleButton() -> some View {
        modifier(ActionStyle(shape: .circle, prominent: false, tint: Palette.mono))
    }
}

struct GlassGroup<Content: View>: View {
    @ViewBuilder var content: Content
    @Environment(\.flatPreview) private var flat

    var body: some View {
        if flat {
            content
        } else if #available(macOS 26.0, *) {
            GlassEffectContainer { content }
        } else {
            content
        }
    }
}

// MARK: - Views

struct PulseValues {
    var scale: CGFloat = 1
    var opacity: Double = 0
}

/// A bot's dot. Filled when connected, a red ring when not; one pulse when its state
/// changes (skipped when Reduce Motion is on).
struct PulseDot: View {
    let connected: Bool
    let pulse: Int
    let size: CGFloat
    let on: Color
    let off: Color
    let animate: Bool

    var body: some View {
        ZStack {
            if connected {
                Circle().fill(on)
            } else {
                Circle().strokeBorder(off, lineWidth: max(1.5, size * 0.22))
            }
            if animate {
                Circle()
                    .stroke(connected ? on : off, lineWidth: 1.5)
                    .keyframeAnimator(initialValue: PulseValues(), trigger: pulse) { view, value in
                        view.scaleEffect(value.scale).opacity(value.opacity)
                    } keyframes: { _ in
                        KeyframeTrack(\.scale) {
                            LinearKeyframe(1, duration: 0.01)
                            CubicKeyframe(2.6, duration: 0.7)
                        }
                        KeyframeTrack(\.opacity) {
                            LinearKeyframe(0.8, duration: 0.01)
                            CubicKeyframe(0, duration: 0.7)
                        }
                    }
            }
        }
        .frame(width: size, height: size)
    }
}

/// The panel header mark: the six-dot motif on the black tile, live.
struct MotifMark: View {
    let rows: [FleetStore.Row]
    let animate: Bool

    var body: some View {
        let count = max(rows.count, 1)
        let (r, c) = DotArt.grid(count)
        ZStack {
            RoundedRectangle(cornerRadius: Radius.tile, style: .continuous).fill(Palette.tile)
            // A hairline in the ink color: invisible on light glass, a soft edge on dark.
            RoundedRectangle(cornerRadius: Radius.tile, style: .continuous).strokeBorder(Palette.ink.opacity(0.3), lineWidth: 1)
            VStack(spacing: 4) {
                ForEach(0..<r, id: \.self) { ri in
                    HStack(spacing: 4) {
                        ForEach(0..<c, id: \.self) { ci in
                            let i = ri * c + ci
                            if i < rows.count {
                                PulseDot(connected: rows[i].connected, pulse: rows[i].pulse, size: 6,
                                         on: .white, off: Palette.alertOnTile, animate: animate)
                            } else {
                                Color.clear.frame(width: 6, height: 6)
                            }
                        }
                    }
                }
            }
        }
        .frame(width: 36, height: 36)
    }
}

/// A compact ring, one segment per bot: primary when connected, red when not.
struct StatusRing: View {
    let states: [Bool]

    var body: some View {
        let n = max(states.count, 1)
        let gap = n > 1 ? 0.04 : 0.0
        ZStack {
            ForEach(0..<n, id: \.self) { i in
                Circle()
                    .trim(from: Double(i) / Double(n) + gap / 2, to: Double(i + 1) / Double(n) - gap / 2)
                    .stroke(color(i), style: StrokeStyle(lineWidth: 3, lineCap: .butt))
            }
        }
        .rotationEffect(.degrees(-90))
        .padding(1.5)
    }

    private func color(_ i: Int) -> Color {
        guard i < states.count else { return Palette.ink.opacity(0.3) }
        return states[i] ? Palette.ink : Palette.alert
    }
}

struct IconButton: View {
    let symbol: String
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Palette.label)
                .frame(width: 14, height: 14)
        }
        .circleButton()
        .help(label)
        .accessibilityLabel(label)
    }
}

struct PanelView: View {
    @ObservedObject var store: FleetStore
    let close: () -> Void
    @FocusState private var headerFocused: Bool

    /// The legend and the rows scroll once they would push the panel past the screen:
    /// the visible height, less the header, the bottom group and the margins.
    private var middleLimit: CGFloat {
        max(240, (NSScreen.main?.visibleFrame.height ?? 800) - 330)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            // The header takes the first focus when the panel opens, so no row shows a
            // focus ring until Tab moves into the list.
            HeaderV2(store: store)
                .focusable()
                .focused($headerFocused)
                .focusEffectDisabled()
            BoundedScroll(limit: middleLimit) {
                VStack(alignment: .leading, spacing: Space.m) {
                    if store.showLegend {
                        LegendV2(store: store)
                    }
                    RowsGroupV2(store: store)
                }
            }
            FooterV2(store: store)
        }
        .padding(Space.l)
        .frame(width: 320)
        .background(Palette.scrim, in: RoundedRectangle(cornerRadius: Radius.panel, style: .continuous))
        .defaultFocus($headerFocused, true)
        .onExitCommand(perform: close)
    }
}

private struct ContentHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

/// Its content at full height until that passes `limit`, then a scroll view of that
/// height. The flat preview draws the content as it is (ImageRenderer cannot draw a
/// scroll view).
struct BoundedScroll<Content: View>: View {
    let limit: CGFloat
    @ViewBuilder var content: Content
    @State private var height: CGFloat = 0
    @Environment(\.flatPreview) private var flat

    var body: some View {
        if flat {
            content
        } else {
            ScrollView(.vertical) {
                content
                    .background(GeometryReader { proxy in
                        Color.clear.preference(key: ContentHeightKey.self, value: proxy.size.height)
                    })
            }
            .scrollBounceBehavior(.basedOnSize)
            .scrollIndicators(height > limit ? .automatic : .hidden)
            .frame(height: min(max(height, 1), limit))
            .onPreferenceChange(ContentHeightKey.self) { height = $0 }
        }
    }
}

struct ConfigProblemView: View {
    let problem: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("Fleet config problem", systemImage: "exclamationmark.triangle")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Palette.label)
            Text(problem)
                .font(.system(size: 11))
                .foregroundStyle(Palette.label)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Space.m)
    }
}

// MARK: - Panel v2
//
// One spacing scale (8 / 12 / 16), 12pt insets inside every glass group, one right
// column for status and actions, secondary text one size down. Quiet by default:
// red appears only for a bot that is down, a missing poller, or the one header line
// when auto-recovery is off or Telegram is unreachable.

enum Grid2 {
    static let inset: CGFloat = 12
    static let gap: CGFloat = 8
    static let dot: CGFloat = 8
    static let textIndent: CGFloat = 16      // dot + gap: where every text line starts
    static let statusWidth: CGFloat = 62
    static let actionWidth: CGFloat = 60     // label width inside every row capsule
    static let rowPadding: CGFloat = 10
}

extension Palette {
    /// Secondary text: the full label color, the same color as the names. Measured on
    /// the real light glass, 80% of it came out at 4.29 to 4.37:1, under 4.5:1, while
    /// the full color measured 6.99 and 7.34:1 on the same capture. So nothing is
    /// lighter than the label color, and hierarchy comes from size and weight alone.
    static let secondary = Color(nsColor: .labelColor)
    /// Labels on disabled buttons (the explicit label color would otherwise hide the
    /// system's dimming).
    static func label(enabled: Bool) -> Color {
        enabled ? label : Color(nsColor: .labelColor).opacity(0.35)
    }
}

struct HeaderV2: View {
    @ObservedObject var store: FleetStore

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: Space.m) {
                MotifMark(rows: store.rows, animate: !store.reduceMotion)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text("TELEBOTS")
                            .font(.system(size: 13, weight: .bold))
                            .tracking(2.2)
                            .foregroundStyle(Palette.label)
                        if let badge = store.badge {
                            Text(badge)
                                .font(.system(size: 9, weight: .bold))
                                .tracking(0.8)
                                .foregroundStyle(Palette.secondary)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1)
                                .overlay(Capsule().strokeBorder(Palette.secondary, lineWidth: 1))
                        }
                    }
                    Text(store.summary)
                        .font(.system(size: 12))
                        .monospacedDigit()
                        .foregroundStyle(Palette.secondary)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Telebots, \(store.summary)")
                Spacer(minLength: Grid2.gap)
                Button {
                    store.showLegend.toggle()
                } label: {
                    Image(systemName: store.showLegend ? "questionmark.circle.fill" : "questionmark.circle")
                        .font(.system(size: 13))
                        .foregroundStyle(Palette.secondary)
                        .frame(width: 20, height: 20)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(store.showLegend ? "Hide the legend." : "What every dot, mark and number means.")
                .accessibilityLabel(store.showLegend ? "Hide legend" : "Show legend")
                StatusRing(states: store.rows.map(\.connected))
                    .frame(width: 26, height: 26)
                    .help("One segment per bot, in fleet order: solid when connected, red when not.")
                    .accessibilityElement()
                    .accessibilityLabel("Status ring, \(store.summary)")
            }
            if let problem = store.headerProblem {
                Text(problem)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Palette.alertText)
                    .padding(.leading, 36 + Space.m)
                    .help(problem.contains("Telegram")
                          ? "Telegram's API does not answer from this Mac, so no bot can hear or reply. Nothing is restarted for a network outage."
                          : "The watchdog is not restarting dropped bots. Turn Auto-recovery back on with the switch below, or press Restore after a snapshot.")
            }
        }
    }
}

/// The legend behind the "?" button: every mark in the menu bar and the panel, and
/// the quiet way out of the app.
struct LegendV2: View {
    @ObservedObject var store: FleetStore

    private func dots(_ states: [Bool]) -> some View {
        HStack(spacing: 2) {
            ForEach(Array(states.enumerated()), id: \.offset) { _, on in
                if on {
                    Circle().fill(Palette.ink).frame(width: 5, height: 5)
                } else {
                    Circle().strokeBorder(Palette.ink, lineWidth: 1).frame(width: 5, height: 5)
                }
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Grid2.gap + 2) {
            legendGrid
            Button {
                store.quit()
            } label: {
                Text("Quit Telebots Bar")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Palette.secondary)
                    .underline()
            }
            .buttonStyle(.plain)
            .help("Quits this menu bar app only; the bots keep running")
            .accessibilityLabel("Quit Telebots Bar")
        }
        .padding(Grid2.inset)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Palette.ink.opacity(0.05)))
    }

    private var legendGrid: some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 6) {
            GridRow {
                dots([true, true, false])
                Text("Dots: one per bot, hollow if down")
            }
            GridRow {
                Text("5/6").foregroundStyle(Palette.alertText).fontWeight(.semibold).monospacedDigit()
                Text("Red count: a bot is not connected")
            }
            GridRow {
                Image(systemName: "shield.slash")
                Text("Auto-recovery is off")
            }
            GridRow {
                Image(systemName: "antenna.radiowaves.left.and.right.slash")
                Text("Telegram unreachable from this Mac")
            }
            GridRow {
                HStack(spacing: 3) { Image(systemName: "waveform.path.ecg"); Text("12s") }
                Text("Poller heartbeat (liveness ping)")
            }
            GridRow {
                Text("ctx")
                Text("Context window used by the session")
            }
            GridRow {
                Text("restarts/h")
                Text("Watchdog restarts in the last hour")
            }
            GridRow {
                Text("stall")
                Text("A message waited 10+ min, idle")
            }
        }
        .font(.system(size: 11))
        .foregroundStyle(Palette.label)
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Self.spokenLegend)
    }

    /// The whole legend in words, since half of it is glyphs VoiceOver cannot name.
    static let spokenLegend = "Legend. "
        + "Menu bar dots: one per bot, filled when connected, hollow when down. "
        + "A red count such as 5 of 6: a bot is not connected. "
        + "A shield with a slash: auto-recovery is off. "
        + "An antenna with a slash: Telegram is unreachable from this Mac. "
        + "A pulse with seconds: the poller heartbeat, its liveness ping. "
        + "ctx: the context window used by the session. "
        + "restarts per hour: watchdog restarts in the last hour. "
        + "stall: a message waited 10 or more minutes while the bot sat idle."
}

struct RowDivider: View {
    var leading: CGFloat = Grid2.inset + Grid2.textIndent

    var body: some View {
        Rectangle()
            .fill(Palette.ink.opacity(0.1))
            .frame(height: 0.5)
            .padding(.leading, leading)
            .padding(.trailing, Grid2.inset)
    }
}

struct RowsGroupV2: View {
    @ObservedObject var store: FleetStore

    var body: some View {
        GlassGroup {
            VStack(spacing: 0) {
                if let problem = store.problem {
                    ConfigProblemView(problem: problem)
                } else {
                    ForEach(Array(store.rows.enumerated()), id: \.element.id) { index, row in
                        if index > 0 { RowDivider() }
                        RowV2(row: row, store: store)
                    }
                }
            }
            .padding(.vertical, 2)
            .glassSurface(cornerRadius: Radius.group)
        }
    }
}

/// The quieter, outlined companion capsule (Fresh start).
struct OutlineCapsuleStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .foregroundStyle(Palette.label)
            .background(Capsule().fill(Palette.ink.opacity(configuration.isPressed ? 0.12 : 0)))
            .overlay(Capsule().strokeBorder(Palette.ink.opacity(0.35), lineWidth: 1))
            .opacity(isEnabled ? 1 : 0.4)
    }
}

struct RowV2: View {
    let row: FleetStore.Row
    @ObservedObject var store: FleetStore
    @State private var hovering = false
    @FocusState private var focused: Bool

    private var expanded: Bool { store.expanded.contains(row.id) }
    private var busy: Bool { store.inFlight.contains(row.id) }
    private var justBack: Bool { store.succeeded.contains(row.id) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 0) {
                PulseDot(connected: row.connected, pulse: row.pulse, size: Grid2.dot, on: Palette.ink,
                         off: Palette.alert, animate: !store.reduceMotion)
                Text(row.name)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Palette.label)
                    .lineLimit(1)
                    .padding(.leading, Grid2.gap)
                Spacer(minLength: Grid2.gap)
                status
                    .frame(width: Grid2.statusWidth, alignment: .trailing)
                Button {
                    let id = row.id
                    Task { @MainActor in store.requestReconnect(id) }
                } label: {
                    Text("Reconnect")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(Palette.label(enabled: !(busy || store.problem != nil)))
                        .lineLimit(1)
                        .frame(width: Grid2.actionWidth)
                }
                .capsuleButton(prominent: false)
                .controlSize(.small)
                .disabled(busy || store.problem != nil)
                .help("Restart \(row.name) through the watchdog's own engine, resumed into its conversation."
                      + (row.connected ? " It asks first because the bot is connected." : ""))
                .accessibilityLabel("Reconnect \(row.name)")
                .padding(.leading, Grid2.gap)
            }
            .frame(height: 22)

            (Text(row.detailLine) + (row.stall ? Text(" · ") + Text("stall suspected").fontWeight(.semibold) : Text("")))
                .font(.system(size: 11))
                .monospacedDigit()
                .foregroundStyle(Palette.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.leading, Grid2.textIndent)
                .padding(.top, 3)
                .help(row.detailTip + (row.stall
                      ? " Stall suspected: a message reached this bot over 10 minutes ago and nothing has happened since. The watchdog only flags this; it never restarts for it."
                      : ""))

            if let note = store.notes[row.id] {
                Text(store.noteText(note))
                    .font(.system(size: 11, weight: note.isError ? .semibold : .regular))
                    .foregroundStyle(note.isError ? Palette.alertText : Palette.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, Grid2.textIndent)
                    .padding(.top, 3)
                    .help(note.isError
                          ? "What went wrong with the last action on \(row.name). It stays until \(row.name) is back or you try again; menubar.log has every step."
                          : "What happened with the last action on \(row.name).")
            }

            if expanded, let uptime = row.uptimeLine {
                // One line and one button. When a long uptime cannot share the line with
                // the capsule, the capsule drops below it instead of truncating the text.
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 0) {
                        uptimeText(uptime)
                        Spacer(minLength: 4)
                        freshStartButton
                    }
                    VStack(alignment: .trailing, spacing: Grid2.gap) {
                        uptimeText(uptime).frame(maxWidth: .infinity, alignment: .leading)
                        freshStartButton
                    }
                }
                .padding(.leading, Grid2.textIndent)
                .padding(.top, Grid2.gap)
            }
        }
        .padding(.horizontal, Grid2.inset)
        .padding(.vertical, Grid2.rowPadding)
        .contentShape(Rectangle())
        .background(
            RoundedRectangle(cornerRadius: Radius.row, style: .continuous)
                .fill(Palette.ink.opacity(hovering || focused ? 0.06 : 0))
                .padding(.horizontal, 4)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Radius.row, style: .continuous)
                .strokeBorder(Palette.ink.opacity(focused ? 0.55 : 0), lineWidth: 1.5)
                .padding(.horizontal, 4)
        )
        .onHover { hovering = $0 }
        .onTapGesture { store.toggleExpanded(row.id) }
        .help(expanded ? "Click to hide uptime and Fresh start." : "Click for uptime and a Fresh start button.")
        .focusable()
        .focused($focused)
        .focusEffectDisabled()
        .onKeyPress(.return) { store.toggleExpanded(row.id); return .handled }
        .onKeyPress(.space) { store.toggleExpanded(row.id); return .handled }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(row.name), \(busy ? "reconnecting" : row.spokenStatus)"
                            + (store.notes[row.id].map { ", \($0.text)" } ?? ""))
        .accessibilityValue(expanded ? "details shown" : "details hidden")
        .accessibilityAction(named: Text(expanded ? "Hide details" : "Show details")) { store.toggleExpanded(row.id) }
    }

    private func uptimeText(_ uptime: String) -> some View {
        Text(uptime)
            .font(.system(size: 11))
            .monospacedDigit()
            .foregroundStyle(Palette.secondary)
            .lineLimit(1)
            .fixedSize()
            .help("How long this session has been up, how it started (resumed keeps the conversation, fresh starts a new one), and at what time, Eastern (ET).")
    }

    private var freshStartButton: some View {
        Button {
            let id = row.id
            Task { @MainActor in store.requestFreshStart(id) }
        } label: {
            Text("Fresh start")
                .font(.system(size: 10.5, weight: .medium))
                .lineLimit(1)
                .frame(width: Grid2.actionWidth)
        }
        .buttonStyle(OutlineCapsuleStyle())
        .controlSize(.small)
        .disabled(busy || store.problem != nil)
        .help("Restart \(row.name) with a new, empty conversation (the /fresh path). It asks first. Reconnect keeps the conversation.")
        .accessibilityLabel("Fresh start \(row.name)")
    }

    @ViewBuilder private var status: some View {
        if busy {
            RowSpinner()
                .help("Reconnecting now.")
                .accessibilityLabel("Reconnecting")
        } else if justBack {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Palette.label)
                .transition(store.reduceMotion ? .identity : .scale(scale: 0.6).combined(with: .opacity))
                .help("Back: reconnected just now.")
                .accessibilityLabel("Reconnected")
        } else if row.connected {
            HStack(spacing: 4) {
                if let icon = row.statusIcon {
                    Image(systemName: icon).font(.system(size: 10, weight: .semibold))
                }
                Text(row.statusText)
                    .font(.system(size: 12))
                    .monospacedDigit()
            }
            .foregroundStyle(Palette.secondary)
            .help(row.statusTip)
        } else {
            Text(row.statusText)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Palette.alertText)
                .lineLimit(1)
                .help(row.statusTip)
        }
    }
}

struct FooterV2: View {
    @ObservedObject var store: FleetStore

    var body: some View {
        VStack(spacing: Space.m) {
            if store.anyDown {
                Button {
                    Task { @MainActor in store.reconnectAllDown() }
                } label: {
                    Label("Reconnect all down", systemImage: "arrow.triangle.2.circlepath")
                        .font(.system(size: 13, weight: .semibold))
                        .frame(maxWidth: .infinity)
                }
                .capsuleButton(prominent: true, tint: Palette.alertFill)
                .controlSize(.large)
                .disabled(!store.canReconnectAll)
                .help("Reconnect every bot that is not connected, one at a time, each resumed into its conversation.")
                .accessibilityLabel("Reconnect all down")
            }

            VStack(spacing: 0) {
                HStack(spacing: Grid2.gap) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Watchdog")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(Palette.label)
                        Text(store.watchdogCaption)
                            .font(.system(size: 11))
                            .foregroundStyle(store.watchdog?.isOn == false ? Palette.alertText : Palette.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: Grid2.gap)
                    Toggle("", isOn: Binding(
                        get: { store.watchdog?.isOn ?? false },
                        set: { value in Task { @MainActor in store.setWatchdog(on: value) } }
                    ))
                    .labelsHidden()
                    .modifier(SwitchStyleChoice())
                    .tint(Palette.mono)
                    .controlSize(.small)
                    .disabled(store.watchdog?.available != true)
                    .help(store.watchdog?.available == false
                          ? "Unavailable: there is no watchdog state folder, so nothing can say whether auto-recovery runs."
                          : "Auto-recovery: when on, the watchdog restarts a bot that stays disconnected. Off pauses it for every bot.")
                    .accessibilityLabel("Auto-recovery")
                    .accessibilityValue(store.watchdogCaption)
                }
                .padding(Grid2.inset)

                if let note = store.footerNote {
                    Text(store.noteText(note))
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Palette.alertText)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, Grid2.inset)
                        .padding(.bottom, Grid2.inset)
                        .help("It stays until that action works; menubar.log has every step.")
                }

                RowDivider(leading: Grid2.inset)

                HStack(spacing: Grid2.gap) {
                    Button {
                        Task { @MainActor in store.snapshotNow() }
                    } label: {
                        HStack(spacing: 4) {
                            if store.snapshotBusy { RowSpinner() } else { Image(systemName: "camera") }
                            Text("Snapshot").lineLimit(1)
                        }
                        .font(.system(size: 11, weight: .medium))
                        .fixedSize()
                        .foregroundStyle(Palette.label(enabled: !store.snapshotBusy && !store.restoreBusy))
                    }
                    .capsuleButton(prominent: false)
                    .controlSize(.small)
                    .disabled(store.snapshotBusy || store.restoreBusy)
                    .help("Save every bot's current conversation so a reboot or macOS update can bring each one back (the fleet snapshot engine). It pauses auto-recovery until Restore.")
                    .accessibilityLabel("Snapshot")

                    Button {
                        Task { @MainActor in store.restoreNow() }
                    } label: {
                        HStack(spacing: 4) {
                            if store.restoreBusy { RowSpinner() } else { Image(systemName: "clock.arrow.circlepath") }
                            Text("Restore").lineLimit(1)
                        }
                        .font(.system(size: 11, weight: .medium))
                        .fixedSize()
                        .foregroundStyle(Palette.label(enabled: store.snapshotAvailable && !store.snapshotBusy && !store.restoreBusy))
                    }
                    .capsuleButton(prominent: false)
                    .controlSize(.small)
                    .disabled(!store.snapshotAvailable || store.snapshotBusy || store.restoreBusy)
                    .help(store.snapshotTip)
                    .accessibilityLabel("Restore")

                    Spacer(minLength: 0)

                    IconButton(symbol: "arrow.clockwise", label: "Refresh now: check every bot again") {
                        store.refresh(reason: "refresh now")
                    }
                }
                .padding(Grid2.inset)

                if let confirmation = store.confirmation {
                    HStack(spacing: 5) {
                        Image(systemName: "checkmark.circle.fill")
                        Text(confirmation).monospacedDigit()
                    }
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Palette.label)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, Grid2.inset)
                    .padding(.bottom, Grid2.inset)
                    .transition(.opacity)
                    .accessibilityElement(children: .combine)
                }
            }
            .glassSurface(cornerRadius: Radius.group)
        }
    }
}

// MARK: - App

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    let store: FleetStore
    let openPanelAtLaunch: Bool
    let quitAfterPanel: Bool
    let forcedAppearance: NSAppearance?
    let owner: String
    private var statusItem: NSStatusItem?
    private let popover = NSPopover()
    private var timer: Timer?

    init(store: FleetStore, openPanelAtLaunch: Bool, quitAfterPanel: Bool, appearance: NSAppearance?, owner: String) {
        self.store = store
        self.openPanelAtLaunch = openPanelAtLaunch
        self.quitAfterPanel = quitAfterPanel
        self.forcedAppearance = appearance
        self.owner = owner
    }

    func applicationWillTerminate(_ notification: Notification) {
        store.stop()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.target = self
            button.action = #selector(togglePanel(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
            button.imagePosition = .imageLeading
        }
        statusItem = item

        let host = NSHostingController(rootView: PanelView(store: store, close: { [weak self] in self?.closePanel() }))
        host.sizingOptions = [.preferredContentSize]
        popover.contentViewController = host
        popover.behavior = .transient
        popover.animates = !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        popover.delegate = self
        if let forcedAppearance { popover.appearance = forcedAppearance }

        store.onChange = { [weak self] in self?.updateStatusItem() }
        store.logStartup(owner: owner)
        store.refresh()

        let refreshTimer = Timer(timeInterval: 15, target: self, selector: #selector(tick), userInfo: nil, repeats: true)
        RunLoop.main.add(refreshTimer, forMode: .common)
        timer = refreshTimer
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(displayOptionsChanged),
            name: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification, object: nil)

        if openPanelAtLaunch {
            Task { @MainActor [weak self] in
                try? await Task.sleep(for: .seconds(1.5))
                self?.openForPreview()
            }
        }
    }

    @objc private func tick() { store.refresh() }

    @objc private func displayOptionsChanged() {
        store.refreshReduceMotion()
        popover.animates = !store.reduceMotion
    }

    @objc private func togglePanel(_ sender: Any?) {
        if popover.isShown { closePanel() } else { showPanel() }
    }

    private func showPanel() {
        guard let button = statusItem?.button else { return }
        store.refresh()
        // The panel's own open and close animation follows Reduce Motion too.
        store.refreshReduceMotion()
        popover.animates = !store.reduceMotion
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        NSApp.activate()
        popover.contentViewController?.view.window?.makeKey()
    }

    private func closePanel() {
        popover.performClose(nil)
    }

    func popoverWillShow(_ notification: Notification) {
        store.refreshReduceMotion()
    }

    private func updateStatusItem() {
        guard let button = statusItem?.button else { return }
        let states = store.rows.map(\.connected)
        button.image = DotArt.statusImage(states.isEmpty ? [false] : states, marks: store.marks)
        let text = " " + (store.mode == .demo ? "DEMO " : "") + store.countText
        if store.alarm {
            button.attributedTitle = NSAttributedString(string: text, attributes: [
                .foregroundColor: Palette.alertNS,
                .font: NSFont.menuBarFont(ofSize: 0),
            ])
        } else {
            button.title = text
        }
        let extras = store.marks.map { $0 == .watchdogOff ? "auto-recovery off" : "Telegram unreachable" }
        button.setAccessibilityLabel((["Telebots, \(store.summary)"] + extras).joined(separator: ", "))
        var tip = "Telebots: \(store.summary). One dot per bot, hollow when not connected; click for details."
        for mark in store.marks { tip += "\n" + mark.meaning + "." }
        button.toolTip = tip
    }

    /// --open-panel: show the panel once so it can be captured, report where it is,
    /// then close it and hand focus back.
    private func openForPreview() {
        showPanel()
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .milliseconds(800))
            self?.reportFrames()
            try? await Task.sleep(for: .seconds(12))
            self?.closePanel()
            self?.note("open-panel: panel closed")
            NSApp.hide(nil)
            if self?.quitAfterPanel == true {
                try? await Task.sleep(for: .milliseconds(500))
                NSApp.terminate(nil)
            }
        }
    }

    private func reportFrames() {
        let screenHeight = NSScreen.screens.first?.frame.height ?? 0
        func topLeft(_ f: NSRect) -> String {
            "x=\(Int(f.minX)) y=\(Int(screenHeight - f.maxY)) w=\(Int(f.width)) h=\(Int(f.height))"
        }
        if let window = popover.contentViewController?.view.window {
            note("open-panel: panel frame \(topLeft(window.frame)) (points, top-left origin)")
        } else {
            note("open-panel: the panel did not open")
        }
        if let window = statusItem?.button?.window {
            let visible = window.occlusionState.contains(.visible)
            note("open-panel: status item frame \(topLeft(window.frame)), \(visible ? "visible" : "hidden (menu bar full or behind the notch)")")
        }
    }

    private func note(_ message: String) {
        ActionLog.shared.write(message)
    }
}

// MARK: - Command line

enum CLI {
    static let usage = """
    Telebots Bar \(appVersion) - menu bar monitor for a Nexera Telebots fleet

    Menu bar app (no arguments):
      --dry-run                 every action is logged instead of run (or TELEBOTS_BAR_DRY_RUN=1)
      --open-panel              open the panel once after launch, then close it (alias --open-menu)
      --demo [--appearance light|dark]
                                a clearly labelled DEMO panel with made-up states, for previews only
      --launchd                 set by install.sh's LaunchAgent: this copy is the supervised one
                                and takes over from a copy started by hand

    Command line:
      --dump                    print the panel text for the fleet right now
      --reconnect <bot>         reconnect one bot that is not connected, the way /fresh does it
                                (its chat line first, the engine, Back and ready, the old window
                                closed); --yes also restarts a connected bot, as the panel's
                                confirmation does; --dry-run logs every step instead
      --fresh-start <bot> --yes a new, empty conversation through the /fresh path (--yes is the
                                panel's confirmation); --dry-run logs every step instead
      --toggle-watchdog         flip the watchdog kill switches; add --dry-run to log only
      --snapshot                run the fleet snapshot engine; add --dry-run to log only
      --restore                 run the fleet restore engine, with the chat lines and window
                                cleanup; add --dry-run to log only
      --render-preview <png>    render the panel (light and dark) and the status icon to PNGs;
                                add --demo for the labelled demo states
      --render-icon <dir>       write the app icon PNGs into an .iconset folder

    Config: fleet.json resolved like fleet-config (FLEET_CONFIG, $FLEET_HOME, ~/.claude/telegram-fleet).
    Log: $FLEET_HOME/menubar.log
    """

    static func loadOrExplain() -> FleetConfig? {
        switch FleetModel.loadConfig() {
        case .ok(let cfg):
            ActionLog.shared.setTimeZone(cfg.settings.timeZone)
            for warning in cfg.warnings {
                FileHandle.standardError.write(Data(("telebots-bar: config warning: " + warning + "\n").utf8))
            }
            return cfg
        case .problem(let message, _):
            FileHandle.standardError.write(Data(("telebots-bar: " + message + "\n").utf8))
            return nil
        }
    }

    @MainActor static func dump() -> Int32 {
        ActionLog.shared.enabled = false
        let store = FleetStore(mode: .snapshot)
        store.refreshNow()
        print(store.dumpText())
        return store.problem == nil ? 0 : 1
    }

    static func findBot(_ key: String, _ cfg: FleetConfig) -> Bot? {
        guard let bot = cfg.bots.first(where: { $0.key == key || $0.name.lowercased() == key.lowercased() }) else {
            FileHandle.standardError.write(Data("telebots-bar: no bot '\(key)' in \(cfg.path)\n".utf8))
            return nil
        }
        return bot
    }

    static func reconnect(_ key: String, dryRun: Bool, confirmed: Bool) -> Int32 {
        ActionLog.shared.echo = true
        guard let cfg = loadOrExplain() else { return 2 }
        guard let bot = findBot(key, cfg) else { return 2 }
        let health = FleetModel.evaluateOne(bot, cfg)
        ActionLog.shared.write("cli: --reconnect \(bot.key)\(confirmed ? " --yes" : "")\(dryRun ? " --dry-run" : "") (\(health.statusLine))")
        if !dryRun && !confirmed && health.state == .connected {
            ActionLog.shared.write("reconnect \(bot.key): not run, it is connected (restarting a connected bot needs --yes, or the confirmation in the panel)")
            return 3
        }
        let outcome = FleetModel.performReconnect(bot, cfg, allowConnected: confirmed, dryRun: dryRun,
                                                  askForPermission: false, log: { ActionLog.shared.write($0) })
        if let failure = outcome.failureText { ActionLog.shared.write("reconnect \(bot.key): \(failure)") }
        switch outcome {
        case .dryRun, .connected, .healedBeforeRun: return 0
        default: return 1
        }
    }

    static func freshStart(_ key: String, dryRun: Bool, confirmed: Bool) -> Int32 {
        ActionLog.shared.echo = true
        guard let cfg = loadOrExplain() else { return 2 }
        guard let bot = findBot(key, cfg) else { return 2 }
        ActionLog.shared.write("cli: --fresh-start \(bot.key)\(confirmed ? " --yes" : "")\(dryRun ? " --dry-run" : "")")
        guard dryRun || confirmed else {
            ActionLog.shared.write("fresh start \(bot.key): not run, a fresh start ends the conversation, so it needs --yes (the panel asks instead)")
            return 3
        }
        let outcome = FleetModel.performReconnect(bot, cfg, allowConnected: true, dryRun: dryRun, askForPermission: false,
                                                  freshStart: true, log: { ActionLog.shared.write($0) })
        if let failure = outcome.failureText { ActionLog.shared.write("fresh start \(bot.key): \(failure)") }
        switch outcome {
        case .dryRun, .connected: return 0
        default: return 1
        }
    }

    static func fleetEngine(_ action: FleetEngineAction, dryRun: Bool) -> Int32 {
        ActionLog.shared.echo = true
        guard let cfg = loadOrExplain() else { return 2 }
        ActionLog.shared.write("cli: --\(action.rawValue)\(dryRun ? " --dry-run" : "")")
        if action == .restore {
            let report = FleetModel.performRestore(cfg, dryRun: dryRun, askForPermission: false,
                                                   log: { ActionLog.shared.write($0) })
            guard let run = report.run, report.blocked == nil else { return 1 }
            if !dryRun {
                ActionLog.shared.write("restore: \(report.back.count) of \(report.wasDown.count) down bots back"
                                       + (report.notBack.isEmpty ? "" : ", not back: " + report.notBack.joined(separator: ", ")))
            }
            return run.code == 0 && report.notBack.isEmpty ? 0 : 1
        }
        guard let run = FleetModel.runFleetEngine(action, cfg, dryRun: dryRun, log: { ActionLog.shared.write($0) }) else { return 1 }
        return run.code == 0 ? 0 : 1
    }

    static func toggleWatchdog(dryRun: Bool) -> Int32 {
        ActionLog.shared.echo = true
        guard let cfg = loadOrExplain() else { return 2 }
        let current = FleetModel.readWatchdog(cfg)
        ActionLog.shared.write("cli: --toggle-watchdog\(dryRun ? " --dry-run" : "") (\(current.label.lowercased()) now)")
        guard current.available else {
            ActionLog.shared.write("watchdog: unavailable, there is no state folder at \(current.dir); nothing changed")
            return 1
        }
        return FleetModel.applyWatchdog(on: !current.isOn, cfg, dryRun: dryRun, log: { ActionLog.shared.write($0) }).isEmpty ? 0 : 1
    }

    static func writePNG(_ rep: NSBitmapImageRep, _ path: String) -> Bool {
        guard let data = rep.representation(using: .png, properties: [:]) else { return false }
        return FileManager.default.createFile(atPath: path, contents: data)
    }

    static func renderIcon(_ dir: String) -> Int32 {
        do {
            try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        } catch {
            FileHandle.standardError.write(Data("telebots-bar: cannot create \(dir)\n".utf8))
            return 1
        }
        let specs: [(String, Int)] = [
            ("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64),
            ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256), ("icon_256x256@2x", 512),
            ("icon_512x512", 512), ("icon_512x512@2x", 1024),
        ]
        for (name, pixels) in specs {
            guard let rep = DotArt.appIcon(pixels: pixels), writePNG(rep, dir + "/" + name + ".png") else {
                FileHandle.standardError.write(Data("telebots-bar: could not write \(name).png\n".utf8))
                return 1
            }
        }
        return 0
    }

    /// The menu bar icon in its four states, side by side and labelled, on a light
    /// menu bar (top) and a dark one (bottom), at 4x.
    static func renderStatusStrip(botCount: Int, path: String) -> Bool {
        let scale = 4
        let n = max(botCount, 1)
        let cell = NSSize(width: 132, height: 26)
        let labelHeight: CGFloat = 16
        let rowHeight = cell.height + labelHeight
        let size = NSSize(width: cell.width * 4, height: rowHeight * 2)
        guard let rep = DotArt.bitmap(pixelsWide: Int(size.width) * scale, pixelsHigh: Int(size.height) * scale, points: size) else { return false }
        var oneDown = Array(repeating: true, count: n)
        oneDown[min(2, n - 1)] = false
        let all = Array(repeating: true, count: n)
        let states: [(label: String, dots: [Bool], marks: [MenuMark], red: Bool)] = [
            ("All good", all, [], false),
            ("One bot not connected", oneDown, [], true),
            ("Auto-recovery off", all, [.watchdogOff], false),
            ("Telegram unreachable", all, [.telegramDown], false),
        ]
        DotArt.draw(into: rep) {
            for (rowIndex, dark) in [false, true].enumerated() {
                let top = size.height - CGFloat(rowIndex) * rowHeight
                let band = NSRect(x: 0, y: top - rowHeight, width: size.width, height: rowHeight)
                (dark ? NSColor(white: 0.12, alpha: 1) : NSColor(white: 0.93, alpha: 1)).setFill()
                band.fill()
                let ink = dark ? NSColor.white : NSColor.black
                var red = ink
                NSAppearance(named: dark ? .darkAqua : .aqua)!.performAsCurrentDrawingAppearance {
                    red = Palette.alertNS.usingColorSpace(.sRGB) ?? .red
                }
                for (i, state) in states.enumerated() {
                    let x0 = CGFloat(i) * cell.width
                    let barY = top - cell.height
                    let count = "\(state.dots.filter { $0 }.count)/\(n)"
                    let title = NSAttributedString(string: count, attributes: [
                        .font: NSFont.menuBarFont(ofSize: 0),
                        .foregroundColor: state.red ? red : ink,
                    ])
                    let iconSize = DotArt.statusSize(n, marks: state.marks)
                    let total = iconSize.width + 5 + title.size().width
                    let startX = x0 + (cell.width - total) / 2
                    DotArt.drawStatus(state.dots, marks: state.marks, height: 16, color: ink,
                                      originX: startX, originY: barY + (cell.height - 16) / 2)
                    title.draw(at: NSPoint(x: startX + iconSize.width + 5, y: barY + (cell.height - title.size().height) / 2))
                    let caption = NSAttributedString(string: state.label, attributes: [
                        .font: NSFont.systemFont(ofSize: 9, weight: .medium),
                        .foregroundColor: ink.withAlphaComponent(0.85),
                    ])
                    caption.draw(at: NSPoint(x: x0 + (cell.width - caption.size().width) / 2, y: top - rowHeight + 3))
                }
            }
        }
        return writePNG(rep, path)
    }

    @MainActor static func renderPanel(_ store: FleetStore, dark: Bool, path: String) -> Bool {
        let panel = PanelView(store: store, close: {})
            .background(
                RoundedRectangle(cornerRadius: Radius.panel, style: .continuous)
                    .fill(dark ? Color(white: 0.16) : Color(white: 0.96))
            )
            .padding(24)
            .background(dark ? Color(white: 0.06) : Color(white: 0.82))
            .environment(\.colorScheme, dark ? .dark : .light)
            .environment(\.flatPreview, true)
        let renderer = ImageRenderer(content: panel)
        renderer.scale = 2
        var image: CGImage?
        NSAppearance(named: dark ? .darkAqua : .aqua)!.performAsCurrentDrawingAppearance {
            image = renderer.cgImage
        }
        guard let cg = image else { return false }
        return writePNG(NSBitmapImageRep(cgImage: cg), path)
    }

    @MainActor static func renderPreview(_ target: String, demo: Bool) -> Int32 {
        ActionLog.shared.enabled = false
        let base = target.hasSuffix(".png") ? String(target.dropLast(4)) : target
        let tag = demo ? "-demo" : ""
        var frames: [(store: FleetStore, dark: Bool, path: String)] = []
        if demo {
            frames.append((FleetStore.demo(.main), false, base + tag + "-light.png"))
            frames.append((FleetStore.demo(.main), true, base + tag + "-dark.png"))
            frames.append((FleetStore.demo(.legend), false, base + tag + "-legend-light.png"))
            frames.append((FleetStore.demo(.snapshot), false, base + tag + "-snapshot-light.png"))
            frames.append((FleetStore.demo(.snapshot), true, base + tag + "-snapshot-dark.png"))
        } else {
            let store = FleetStore(mode: .snapshot)
            store.refreshNow()
            frames.append((store, false, base + "-light.png"))
            frames.append((store, true, base + "-dark.png"))
        }
        var written: [String] = []
        for frame in frames {
            guard renderPanel(frame.store, dark: frame.dark, path: frame.path) else {
                FileHandle.standardError.write(Data("telebots-bar: could not render \(frame.path)\n".utf8))
                return 1
            }
            written.append(frame.path)
        }
        let iconPath = base + tag + "-icon.png"
        let botCount = frames.first.map { $0.store.rows.isEmpty ? 6 : $0.store.rows.count } ?? 6
        guard renderStatusStrip(botCount: botCount, path: iconPath) else {
            FileHandle.standardError.write(Data("telebots-bar: could not render \(iconPath)\n".utf8))
            return 1
        }
        written.append(iconPath)
        for path in written { print(path) }
        return 0
    }
}

// MARK: - Entry

let arguments = Array(CommandLine.arguments.dropFirst()).filter { !$0.hasPrefix("-psn_") }

func hasFlag(_ flag: String) -> Bool { arguments.contains(flag) }

func value(after flag: String) -> String? {
    guard let i = arguments.firstIndex(of: flag), i + 1 < arguments.count, !arguments[i + 1].hasPrefix("--") else { return nil }
    return arguments[i + 1]
}

func usageExit(_ problem: String) -> Never {
    FileHandle.standardError.write(Data(("telebots-bar: " + problem + "\n\n" + CLI.usage + "\n").utf8))
    exit(64)
}

let envDryRun = ["1", "true", "yes"].contains((Env.vars["TELEBOTS_BAR_DRY_RUN"] ?? "").lowercased())
let dryRunRequested = hasFlag("--dry-run") || envDryRun

if hasFlag("--help") || hasFlag("-h") {
    print(CLI.usage)
    exit(0)
}
if hasFlag("--render-icon") {
    guard let dir = value(after: "--render-icon") else { usageExit("--render-icon needs a folder") }
    exit(CLI.renderIcon(dir))
}
if hasFlag("--dump") {
    exit(MainActor.assumeIsolated { CLI.dump() })
}
if hasFlag("--reconnect") {
    guard let key = value(after: "--reconnect") else { usageExit("--reconnect needs a bot key") }
    exit(CLI.reconnect(key, dryRun: dryRunRequested, confirmed: hasFlag("--yes")))
}
if hasFlag("--toggle-watchdog") {
    exit(CLI.toggleWatchdog(dryRun: dryRunRequested))
}
if hasFlag("--fresh-start") {
    guard let key = value(after: "--fresh-start") else { usageExit("--fresh-start needs a bot key") }
    exit(CLI.freshStart(key, dryRun: dryRunRequested, confirmed: hasFlag("--yes")))
}
if hasFlag("--snapshot") {
    exit(CLI.fleetEngine(.snapshot, dryRun: dryRunRequested))
}
if hasFlag("--restore") {
    exit(CLI.fleetEngine(.restore, dryRun: dryRunRequested))
}
if hasFlag("--render-preview") {
    guard let path = value(after: "--render-preview") else { usageExit("--render-preview needs a PNG path") }
    exit(MainActor.assumeIsolated { CLI.renderPreview(path, demo: hasFlag("--demo")) })
}

let guiFlags: Set<String> = ["--dry-run", "--open-panel", "--open-menu", "--demo", "--appearance", "light", "dark", "--launchd"]
if let unknown = arguments.first(where: { !guiFlags.contains($0) }) {
    usageExit("unknown argument \(unknown)")
}

/// One running copy per user: an exclusive lock on a file only this user can reach.
/// The kernel drops it when the process ends, crash or not, so it never goes stale.
enum InstanceLock {
    static var path: String { Env.home + "/Library/Application Support/Telebots Bar/instance.lock" }
    private static var held: Int32 = -1

    enum Outcome {
        case acquired
        case heldBy(pid: pid_t?, owner: String)
        case failed(String)
    }

    static func acquire(owner: String) -> Outcome {
        let dir = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        let fd = open(path, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return .failed("cannot open \(path) (\(String(cString: strerror(errno))))") }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            let holder = readHolder(fd)
            close(fd)
            return .heldBy(pid: holder.pid, owner: holder.owner)
        }
        let line = "\(getpid()) \(owner)\n"
        _ = ftruncate(fd, 0)
        _ = line.withCString { pwrite(fd, $0, strlen($0), 0) }
        held = fd
        return .acquired
    }

    /// Waits for another copy to let go of the lock, up to `seconds`.
    static func waitAndAcquire(owner: String, seconds: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        repeat {
            if case .acquired = acquire(owner: owner) { return true }
            usleep(200_000)
        } while Date() < deadline
        return false
    }

    private static func readHolder(_ fd: Int32) -> (pid: pid_t?, owner: String) {
        var buffer = [UInt8](repeating: 0, count: 64)
        let count = pread(fd, &buffer, 63, 0)
        let text = count > 0 ? String(decoding: buffer[0..<count], as: UTF8.self) : ""
        let parts = text.split(whereSeparator: { $0 == " " || $0 == "\n" })
        return (parts.first.flatMap { pid_t($0) }, parts.count > 1 ? String(parts[1]) : "unknown")
    }
}

/// True only for this user's own Telebots Bar executable.
func isTelebotsBar(_ pid: pid_t) -> Bool {
    guard pid > 1, let basic = ProcessTable.basicsOnly().basics[pid], basic.uid == getuid() else { return false }
    var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    guard proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 else { return false }
    return String(cString: buffer).hasSuffix("/Telebots Bar.app/Contents/MacOS/Telebots Bar")
}

/// SIGTERM (launchd stopping the job, or a supervised copy taking over) quits
/// cleanly and says so in the log.
var terminationSource: DispatchSourceSignal?

@MainActor func runMenuBarApp() -> Never {
    let demoMode = hasFlag("--demo")
    let mode: FleetStore.Mode = demoMode ? .demo : (dryRunRequested ? .dryRun : .live)
    let owner = hasFlag("--launchd") ? "launchd" : "manual"
    if demoMode {
        // A demo never writes the action log; its notes go to stdout.
        ActionLog.shared.enabled = false
        ActionLog.shared.echo = true
    } else {
        switch InstanceLock.acquire(owner: owner) {
        case .acquired:
            break
        case .heldBy(let pid, let holder):
            // The LaunchAgent's copy is the supervised one, so it takes over from a copy
            // started by hand; any other second copy just leaves.
            if owner == "launchd", holder != "launchd", let pid, isTelebotsBar(pid) {
                ActionLog.shared.write("start (launchd): a copy started by hand is running (pid \(pid)); asking it to quit so launchd supervises this one")
                kill(pid, SIGTERM)
                guard InstanceLock.waitAndAcquire(owner: owner, seconds: 10) else {
                    ActionLog.shared.write("start (launchd): pid \(pid) did not quit within 10s; this copy exits with an error so launchd tries again")
                    exit(1)
                }
                ActionLog.shared.write("start (launchd): took over from pid \(pid)")
            } else {
                ActionLog.shared.write("start: Telebots Bar is already running (pid \(pid.map { String($0) } ?? "unknown"), started \(holder)), this copy exits")
                exit(0)
            }
        case .failed(let why):
            ActionLog.shared.write("start: cannot take the single-copy lock (\(why)); this copy exits")
            exit(1)
        }
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler {
            ActionLog.shared.write("stop: SIGTERM received (pid \(getpid())), quitting")
            NSApp.terminate(nil)
        }
        source.resume()
        terminationSource = source
    }

    var appearance: NSAppearance?
    switch value(after: "--appearance") {
    case "dark": appearance = NSAppearance(named: .darkAqua)
    case "light": appearance = NSAppearance(named: .aqua)
    default: appearance = nil
    }

    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let delegate = AppDelegate(store: demoMode ? FleetStore.demo() : FleetStore(mode: mode),
                               openPanelAtLaunch: hasFlag("--open-panel") || hasFlag("--open-menu") || demoMode,
                               quitAfterPanel: demoMode,
                               appearance: appearance,
                               owner: owner)
    app.delegate = delegate
    app.run()
    exit(0)
}

MainActor.assumeIsolated { runMenuBarApp() }
