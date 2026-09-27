// Fleet.swift - the model behind Telebots Bar: fleet config, bot health,
// reconnect plans, the watchdog kill switches and the action log.
//
// Everything here mirrors the fleet's own shell engines on purpose, so the menu
// bar never disagrees with the watchdog:
//   - config is resolved exactly like engine/fleet-config (FLEET_CONFIG, then
//     $FLEET_HOME/fleet.json, then ~/.claude/telegram-fleet/fleet.json);
//   - a bot is healthy exactly when the watchdog says so: a Claude session process
//     and a Telegram poller (server.ts) process whose TELEGRAM_STATE_DIR is the
//     bot's state dir, and a poller heartbeat no older than the watchdog's limit;
//   - Reconnect runs the watchdog's restart engine with the same four arguments
//     (session pid or 999999, workdir, resume id, quiet), and the engine does the
//     pid + cwd guarded kill, so a sibling bot can never be touched;
//   - the watchdog switch is the per-bot <key>.disabled file that the snapshot and
//     restore engines create and remove.
// Processes are read natively (sysctl KERN_PROCARGS2, the data `ps eww` prints);
// nothing here shells out to the watchdog. No bot token is ever read.

import Foundation
import Darwin
import CoreServices

// MARK: - Small helpers

enum Env {
    static var vars: [String: String] { ProcessInfo.processInfo.environment }
    static var home: String {
        if let h = vars["HOME"], !h.isEmpty { return h }
        return NSHomeDirectory()
    }
}

/// `~` expansion the way Python's os.path.expanduser does it for the fleet config.
func expandTilde(_ path: String) -> String {
    if path == "~" { return Env.home }
    if path.hasPrefix("~/") { return Env.home + String(path.dropFirst(1)) }
    return path
}

func isRegularFile(_ path: String) -> Bool {
    var st = stat()
    return stat(path, &st) == 0 && (st.st_mode & S_IFMT) == S_IFREG
}

func isDirectory(_ path: String) -> Bool {
    var st = stat()
    return stat(path, &st) == 0 && (st.st_mode & S_IFMT) == S_IFDIR
}

func isExecutableFile(_ path: String) -> Bool {
    isRegularFile(path) && access(path, X_OK) == 0
}

func modificationTime(_ path: String) -> TimeInterval? {
    var st = stat()
    guard stat(path, &st) == 0 else { return nil }
    return TimeInterval(st.st_mtimespec.tv_sec) + TimeInterval(st.st_mtimespec.tv_nsec) / 1e9
}

/// The first line of a small text file (what `head -1` prints), without its newline.
func firstLine(_ path: String) -> String? {
    guard let data = FileManager.default.contents(atPath: path) else { return nil }
    let text = String(decoding: data.prefix(4096), as: UTF8.self)
    if let nl = text.firstIndex(of: "\n") { return String(text[..<nl]) }
    return text
}

/// `head -1 | tr -cd '0-9'` as a number, or nil when no digits are there.
func firstLineDigits(_ path: String) -> Int? {
    guard let line = firstLine(path) else { return nil }
    let digits = line.filter { $0 >= "0" && $0 <= "9" }
    return digits.isEmpty ? nil : Int(digits)
}

/// 12s, 5m, 3h, 2d.
func shortAge(_ seconds: Int) -> String {
    let s = max(0, seconds)
    if s < 60 { return "\(s)s" }
    if s < 3600 { return "\(s / 60)m" }
    if s < 86_400 { return "\(s / 3600)h" }
    return "\(s / 86_400)d"
}

private let tokenPattern = try! NSRegularExpression(pattern: "[0-9]{6,}:[A-Za-z0-9_-]{20,}")

/// Any bot-token-shaped string becomes <redacted> before it can be shown or logged.
func redactTokens(_ text: String) -> String {
    tokenPattern.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "<redacted>")
}

func shellQuote(_ arg: String) -> String {
    let safe = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./=:@%+,")
    if !arg.isEmpty && arg.unicodeScalars.allSatisfy({ safe.contains($0) }) { return arg }
    return "'" + arg.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

// MARK: - Paths

enum FleetPaths {
    /// $FLEET_HOME, default ~/.claude/telegram-fleet (same as every engine).
    static var fleetHome: String {
        if let h = Env.vars["FLEET_HOME"], !h.isEmpty { return expandTilde(h) }
        return Env.home + "/.claude/telegram-fleet"
    }

    /// fleet-config's resolution order.
    static var configPath: String {
        if let p = Env.vars["FLEET_CONFIG"], !p.isEmpty { return expandTilde(p) }
        if let h = Env.vars["FLEET_HOME"], !h.isEmpty {
            return (expandTilde(h) as NSString).appendingPathComponent("fleet.json")
        }
        return Env.home + "/.claude/telegram-fleet/fleet.json"
    }

    /// Every action the app takes is written here, with an ET (or configured) timestamp.
    static var actionLog: String { fleetHome + "/menubar.log" }

    /// The PATH restart engines run with.
    static var enginePATH: String {
        let h = Env.home
        return "\(h)/.local/bin:\(h)/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    }
}

// MARK: - Config

enum ResumeSource: String {
    /// Resume the newest transcript in the bot's project dir (the shipped engines' layout).
    case latestTranscript = "latest-transcript"
    /// Resume the id in <state_dir>/session.id when its transcript exists, else start
    /// an empty session that keeps the model (installs whose SessionStart hook writes it).
    case sessionID = "session-id"
}

struct Bot: Equatable {
    let key: String
    let name: String
    let workdir: String    // ~ expanded, exactly as fleet-config prints it
    let stateDir: String   // ~ expanded

    /// Claude Code's project dir for the workdir (fleet-config project-dir).
    var projectDir: String {
        Env.home + "/.claude/projects/" + workdir.replacingOccurrences(of: "/", with: "-")
    }
}

struct MenubarSettings {
    var freshEngine: String?
    var watchdogStateDir: String
    var watchdogLog: String
    var heartbeatMaxAge: Int
    var resumeSource: ResumeSource
    var timeZone: TimeZone
    var snapshotEngine: String
    var restoreEngine: String
    var snapshotFile: String
    var freshStartArg: String
}

struct FleetConfig {
    let path: String
    let fleetHome: String
    let bots: [Bot]
    let settings: MenubarSettings
    /// Menubar options this build does not understand or could not use. They are
    /// logged and ignored, so a config newer than the app still shows the fleet.
    var warnings: [String] = []
}

enum ConfigLoad {
    case ok(FleetConfig)
    case problem(String, path: String)
}

enum FleetModel {
    static let defaultTimeZone = TimeZone(identifier: "America/New_York") ?? .current
    private static let keyPattern = try! NSRegularExpression(pattern: "^[a-z0-9][a-z0-9-]{0,30}$")
    private static let menubarKeys: Set<String> = [
        "fresh_engine", "watchdog_state_dir", "watchdog_log", "heartbeat_max_age", "resume_source", "time_zone",
        "snapshot_engine", "restore_engine", "snapshot_file", "fresh_start_arg",
    ]

    static func loadConfig() -> ConfigLoad {
        let path = FleetPaths.configPath
        guard let data = FileManager.default.contents(atPath: path) else {
            if FileManager.default.fileExists(atPath: path) {
                return .problem("cannot read config \(path)", path: path)
            }
            return .problem("config not found: \(path)", path: path)
        }
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .problem("config is not a JSON object: \(path)", path: path)
        }

        var problems: [String] = []
        if (root["version"] as? NSNumber)?.intValue != 1 || root["version"] is String {
            problems.append("version must be 1")
        }
        guard let rawBots = root["bots"] as? [Any], !rawBots.isEmpty else {
            problems.append("bots must be a non-empty array")
            return .problem(problems.joined(separator: "; "), path: path)
        }

        var seen = Set<String>()
        var bots: [Bot] = []
        for (index, raw) in rawBots.enumerated() {
            guard let entry = raw as? [String: Any] else {
                problems.append("bot #\(index + 1) is not an object")
                continue
            }
            let key = (entry["key"] as? String) ?? ""
            let keyRange = NSRange(key.startIndex..., in: key)
            if keyPattern.firstMatch(in: key, range: keyRange) == nil {
                problems.append("bot #\(index + 1) has an invalid key '\(key)'")
            } else if seen.contains(key) {
                problems.append("duplicate bot key '\(key)'")
            } else {
                seen.insert(key)
            }
            let workdir = (entry["workdir"] as? String) ?? ""
            let stateDir = (entry["state_dir"] as? String) ?? ""
            if workdir.isEmpty { problems.append("bot '\(key)' missing workdir") }
            if stateDir.isEmpty { problems.append("bot '\(key)' missing state_dir") }
            var name = (entry["name"] as? String) ?? ""
            if name.isEmpty { name = key }
            bots.append(Bot(key: key, name: name, workdir: expandTilde(workdir), stateDir: expandTilde(stateDir)))
        }

        let home = FleetPaths.fleetHome
        var settings = MenubarSettings(
            freshEngine: nil,
            watchdogStateDir: home + "/watchdog-state",
            watchdogLog: home + "/logs/watchdog.log",
            heartbeatMaxAge: 180,
            resumeSource: .latestTranscript,
            timeZone: defaultTimeZone,
            snapshotEngine: home + "/bin/fleet-snapshot",
            restoreEngine: home + "/bin/fleet-restore",
            snapshotFile: home + "/restart-state/snapshot.tsv",
            freshStartArg: ""
        )
        var warnings: [String] = []
        if let mb = root["menubar"] {
            if let mb = mb as? [String: Any] {
                for key in mb.keys.sorted() where !menubarKeys.contains(key) {
                    warnings.append("menubar.\(key) is not an option this build knows; ignored")
                }
                func path(_ key: String, _ apply: (String) -> Void) {
                    guard let v = mb[key] else { return }
                    if let s = v as? String, !s.isEmpty { apply(expandTilde(s)) } else { warnings.append("menubar.\(key) must be a path; default kept") }
                }
                path("fresh_engine") { settings.freshEngine = $0 }
                path("watchdog_state_dir") { settings.watchdogStateDir = $0 }
                path("watchdog_log") { settings.watchdogLog = $0 }
                path("snapshot_engine") { settings.snapshotEngine = $0 }
                path("restore_engine") { settings.restoreEngine = $0 }
                path("snapshot_file") { settings.snapshotFile = $0 }
                if let v = mb["heartbeat_max_age"] {
                    if let n = v as? NSNumber, !(v is Bool), n.intValue > 0 {
                        settings.heartbeatMaxAge = n.intValue
                    } else if let s = v as? String, let n = Int(s), n > 0 {
                        settings.heartbeatMaxAge = n
                    } else {
                        warnings.append("menubar.heartbeat_max_age must be a positive number of seconds; default kept")
                    }
                }
                if let v = mb["resume_source"] {
                    if let s = v as? String, let source = ResumeSource(rawValue: s) { settings.resumeSource = source } else { warnings.append("menubar.resume_source must be latest-transcript or session-id; default kept") }
                }
                if let v = mb["fresh_start_arg"] {
                    if let s = v as? String { settings.freshStartArg = s } else { warnings.append("menubar.fresh_start_arg must be a string; default kept") }
                }
                if let v = mb["time_zone"] {
                    if let s = v as? String, let tz = TimeZone(identifier: s) { settings.timeZone = tz } else { warnings.append("menubar.time_zone must be a time zone name like America/New_York; default kept") }
                }
            } else {
                warnings.append("menubar must be an object; ignored")
            }
        }

        if !problems.isEmpty {
            return .problem(problems.joined(separator: "; "), path: path)
        }
        return .ok(FleetConfig(path: path, fleetHome: home, bots: bots, settings: settings, warnings: warnings))
    }

    /// The restart engine: menubar.fresh_engine if set, else fleet-fresh from
    /// $FLEET_HOME/bin, else fleet-fresh on the engine PATH (the engines' resolve_bin).
    static func resolveFreshEngine(_ config: FleetConfig) -> (path: String?, note: String) {
        if let engine = config.settings.freshEngine {
            return isRegularFile(engine)
                ? (engine, "menubar.fresh_engine")
                : (nil, "menubar.fresh_engine is not a file: \(engine)")
        }
        let installed = config.fleetHome + "/bin/fleet-fresh"
        if isExecutableFile(installed) { return (installed, "$FLEET_HOME/bin") }
        for dir in FleetPaths.enginePATH.split(separator: ":") {
            let candidate = String(dir) + "/fleet-fresh"
            if isExecutableFile(candidate) { return (candidate, "PATH") }
        }
        return (nil, "no fleet-fresh in \(config.fleetHome)/bin or on PATH (set menubar.fresh_engine)")
    }
}

// MARK: - Processes

/// Same-user processes that look like a bot session or a bot poller, with the
/// TELEGRAM_STATE_DIR each one was started with.
struct ProcessTable {
    struct Entry {
        let pid: pid_t
        let argv: [String]
        let stateDir: String?
        let isSession: Bool
        let isPoller: Bool
        let started: TimeInterval   // process start, epoch seconds
    }

    /// The watchdog's own signatures: `grep 'channels plugin:telegram'` for the
    /// session and `grep 'server.ts'` for the poller, each followed by `grep -v grep`.
    static let sessionSignature = "channels plugin:telegram"
    static let pollerSignature = "server.ts"

    let entries: [Entry]

    func sessionPIDs(stateDir: String) -> [pid_t] {
        entries.filter { $0.isSession && $0.stateDir == stateDir }.map(\.pid).sorted()
    }

    func pollerPIDs(stateDir: String) -> [pid_t] {
        entries.filter { $0.isPoller && $0.stateDir == stateDir }.map(\.pid).sorted()
    }

    func argv(of pid: pid_t) -> [String]? {
        entries.first { $0.pid == pid }?.argv
    }

    func started(_ pid: pid_t) -> TimeInterval? {
        entries.first { $0.pid == pid }?.started
    }

    static func snapshot() -> ProcessTable {
        var buffer = [UInt8](repeating: 0, count: argMax())
        var found: [Entry] = []
        for (pid, started) in userPIDs() where pid > 0 {
            guard let (argv, env) = processArguments(pid, &buffer) else { continue }
            let command = argv.joined(separator: " ")
            if command.contains("grep") { continue }
            let isSession = command.contains(sessionSignature)
            let isPoller = command.contains(pollerSignature)
            guard isSession || isPoller else { continue }
            let prefix = "TELEGRAM_STATE_DIR="
            let stateDir = env.first { $0.hasPrefix(prefix) }.map { String($0.dropFirst(prefix.count)) }
            found.append(Entry(pid: pid, argv: argv, stateDir: stateDir, isSession: isSession, isPoller: isPoller,
                               started: started))
        }
        return ProcessTable(entries: found)
    }

    private static func argMax() -> Int {
        var mib: [Int32] = [CTL_KERN, KERN_ARGMAX]
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        if sysctl(&mib, 2, &value, &size, nil, 0) == 0, value > 0 { return Int(value) }
        return 1 << 20
    }

    private static func userPIDs() -> [(pid_t, TimeInterval)] {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL]
        let stride = MemoryLayout<kinfo_proc>.stride
        for _ in 0..<4 {
            var size = 0
            guard sysctl(&mib, 3, nil, &size, nil, 0) == 0 else { return [] }
            var procs = [kinfo_proc](repeating: kinfo_proc(), count: size / stride + 64)
            size = procs.count * stride
            let rc = procs.withUnsafeMutableBufferPointer { buf -> Int32 in
                sysctl(&mib, 3, buf.baseAddress, &size, nil, 0)
            }
            if rc != 0 {
                if errno == ENOMEM { continue }
                return []
            }
            let uid = getuid()
            return procs.prefix(size / stride).compactMap { p in
                guard p.kp_eproc.e_ucred.cr_uid == uid else { return nil }
                let start = p.kp_proc.p_un.__p_starttime
                return (p.kp_proc.p_pid, TimeInterval(start.tv_sec) + TimeInterval(start.tv_usec) / 1e6)
            }
        }
        return []
    }

    /// argv and the start-up environment of a process (the KERN_PROCARGS2 layout:
    /// argc, exec path, padding, argv strings, environment strings).
    private static func processArguments(_ pid: pid_t, _ buffer: inout [UInt8]) -> ([String], [String])? {
        var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        var size = buffer.count
        let rc = buffer.withUnsafeMutableBytes { raw -> Int32 in
            sysctl(&mib, 3, raw.baseAddress, &size, nil, 0)
        }
        guard rc == 0, size > MemoryLayout<Int32>.size else { return nil }
        let argc = Int(buffer.withUnsafeBytes { $0.loadUnaligned(fromByteOffset: 0, as: Int32.self) })
        var i = MemoryLayout<Int32>.size
        while i < size && buffer[i] != 0 { i += 1 }   // exec path
        while i < size && buffer[i] == 0 { i += 1 }   // padding
        var argv: [String] = []
        while argv.count < argc && i < size {
            let start = i
            while i < size && buffer[i] != 0 { i += 1 }
            argv.append(String(decoding: buffer[start..<i], as: UTF8.self))
            i += 1
        }
        var env: [String] = []
        while i < size {
            let start = i
            while i < size && buffer[i] != 0 { i += 1 }
            if i == start { break }
            env.append(String(decoding: buffer[start..<i], as: UTF8.self))
            i += 1
        }
        return (argv, env)
    }
}

// MARK: - Health

enum BotState: String {
    case connected = "Connected"
    case noPoller = "No poller"
    case down = "Down"
}

struct BotHealth {
    let bot: Bot
    let state: BotState
    let sessionPIDs: [pid_t]
    let pollerPIDs: [pid_t]
    let heartbeat: Int?          // epoch seconds from poller.heartbeat
    let heartbeatStale: Bool
    let sessionArgv: [String]?
    let sessionStarted: TimeInterval?
    let checkedAt: Int

    var heartbeatAge: Int? {
        guard let hb = heartbeat, hb > 1_000_000_000 else { return nil }
        return max(0, checkedAt - hb)
    }

    /// fresh, resumed <id>, continued, or not running.
    var launchMode: String {
        guard let argv = sessionArgv else { return "not running" }
        if let i = argv.firstIndex(where: { $0 == "--resume" || $0 == "-r" }), i + 1 < argv.count {
            return "resumed " + String(argv[i + 1].prefix(8))
        }
        if let arg = argv.first(where: { $0.hasPrefix("--resume=") }) {
            return "resumed " + String(arg.dropFirst("--resume=".count).prefix(8))
        }
        if argv.contains("--continue") || argv.contains("-c") { return "continued" }
        return "fresh"
    }

    /// Status text: "Connected · heartbeat 12s ago", "No poller", "Down".
    var statusLine: String {
        switch state {
        case .connected:
            if let age = heartbeatAge { return "Connected · heartbeat \(shortAge(age)) ago" }
            return "Connected"
        case .noPoller: return "No poller"
        case .down: return "Down"
        }
    }
}

extension FleetModel {
    /// The watchdog's per-bot test, same files, same order.
    static func evaluate(_ bot: Bot, table: ProcessTable, now: Int, maxAge: Int) -> BotHealth {
        let sessions = table.sessionPIDs(stateDir: bot.stateDir)
        let pollers = table.pollerPIDs(stateDir: bot.stateDir)
        let heartbeatPath = bot.stateDir + "/poller.heartbeat"
        let heartbeat = isRegularFile(heartbeatPath) ? firstLineDigits(heartbeatPath) : nil
        // A heartbeat only counts against a bot whose session and poller both run; no
        // heartbeat file at all falls back to the pid check, like the watchdog.
        var stale = false
        if !sessions.isEmpty && !pollers.isEmpty, let hb = heartbeat, now - hb > maxAge {
            stale = true
        }
        let state: BotState
        if !sessions.isEmpty && !pollers.isEmpty && !stale {
            state = .connected
        } else if sessions.isEmpty {
            state = .down
        } else {
            state = .noPoller
        }
        let argv = sessions.first.flatMap { table.argv(of: $0) }
        let started = sessions.first.flatMap { table.started($0) }
        return BotHealth(bot: bot, state: state, sessionPIDs: sessions, pollerPIDs: pollers,
                         heartbeat: heartbeat, heartbeatStale: stale, sessionArgv: argv,
                         sessionStarted: started, checkedAt: now)
    }

    static func evaluateAll(_ config: FleetConfig) -> [BotHealth] {
        let table = ProcessTable.snapshot()
        let now = Int(Date().timeIntervalSince1970)
        return config.bots.map { evaluate($0, table: table, now: now, maxAge: config.settings.heartbeatMaxAge) }
    }

    static func evaluateOne(_ bot: Bot, _ config: FleetConfig) -> BotHealth {
        evaluate(bot, table: ProcessTable.snapshot(), now: Int(Date().timeIntervalSince1970), maxAge: config.settings.heartbeatMaxAge)
    }
}

// MARK: - Indicators (read-only)

/// Everything the second line, the expanded line and the header show for a bot.
struct BotIndicators: Equatable {
    var model: String?          // "Opus 5.5" from the newest reply, else the alias ("Opus")
    var servedModel: String?    // the name from the transcript alone, nil before the first reply
    var effort: String?         // "max", "high", ...
    var contextPercent: Int?    // nil when unknown (no transcript yet)
    var restartsLastHour: Int
    var stallSuspected: Bool
    var sessionStarted: TimeInterval?
    var launchMode: String      // "resumed", "fresh", "continued", "not running"
    var lastHeartbeat: TimeInterval?
}

/// Fleet-wide state for the header line and the menu bar marks.
struct FleetSignals: Equatable {
    var lastPass: TimeInterval?        // the watchdog's last pass
    var telegramReachable: Bool?       // nil when unknown
    var snapshotFile: String
    var snapshotExists: Bool
    var snapshotSessions: Int
    var snapshotSaved: TimeInterval?
}

extension FleetModel {
    /// A served model id as a name: claude-opus-5-5 reads "Opus 5.5", claude-fable-5-1
    /// "Fable 5.1", claude-sonnet-5 "Sonnet 5". The claude- prefix and any [1m] suffix
    /// are dropped, the family is capitalized, short version numbers are joined with
    /// dots, and a trailing date stamp is ignored.
    static func modelName(fromID id: String) -> String? {
        var text = id.lowercased()
        if let bracket = text.firstIndex(of: "[") { text = String(text[..<bracket]) }
        if text.hasPrefix("claude-") { text = String(text.dropFirst("claude-".count)) }
        let tokens = text.split(separator: "-").map(String.init)
        guard let family = tokens.first(where: { $0.first?.isLetter == true }) else { return nil }
        let version = tokens.filter { !$0.isEmpty && $0.count <= 2 && $0.allSatisfy(\.isNumber) }
        let name = family.prefix(1).uppercased() + family.dropFirst()
        return version.isEmpty ? name : name + " " + version.joined(separator: ".")
    }

    /// A launch alias as a name ("opus[1m]" reads "Opus"), used until the session's
    /// transcript has a reply that names the exact model.
    static func modelName(fromAlias alias: String) -> String {
        if alias.lowercased().hasPrefix("claude-"), let name = modelName(fromID: alias) { return name }
        var text = alias
        if let bracket = text.firstIndex(of: "[") { text = String(text[..<bracket]) }
        return text.prefix(1).uppercased() + text.dropFirst()
    }

    static func flagValue(_ argv: [String], _ names: [String]) -> String? {
        for (i, arg) in argv.enumerated() {
            if names.contains(arg), i + 1 < argv.count { return argv[i + 1] }
            for name in names where arg.hasPrefix(name + "=") { return String(arg.dropFirst(name.count + 1)) }
        }
        return nil
    }

    /// MODEL= and EFFORT= from <state_dir>/model-state (what the launcher reads).
    static func modelState(_ bot: Bot) -> (model: String?, effort: String?) {
        guard let data = FileManager.default.contents(atPath: bot.stateDir + "/model-state") else { return (nil, nil) }
        var model: String?, effort: String?
        for line in String(decoding: data, as: UTF8.self).split(separator: "\n") {
            let parts = line.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
            guard parts.count == 2, !parts[1].isEmpty else { continue }
            if parts[0].uppercased() == "MODEL" { model = parts[1] }
            if parts[0].uppercased() == "EFFORT" { effort = parts[1] }
        }
        return (model, effort)
    }

    /// Context window used, the fleet's own formula: the newest non-sidechain
    /// assistant record's input + cache read + cache creation tokens, over a 1M cap
    /// for [1m] or fable models and 200k otherwise. Reads at most the last 5 MB.
    static func contextPercent(transcript: String, modelStateText: String) -> Int? {
        transcriptUsage(transcript: transcript, modelStateText: modelStateText).percent
    }

    /// The newest non-sidechain usage record: context used and the model that served it.
    static func transcriptUsage(transcript: String, modelStateText: String) -> (percent: Int?, model: String?) {
        guard isRegularFile(transcript), let handle = FileHandle(forReadingAtPath: transcript) else { return (nil, nil) }
        defer { try? handle.close() }
        let budget: UInt64 = 5_000_000
        let size = (try? handle.seekToEnd()) ?? 0
        let start = size > budget ? size - budget : 0
        try? handle.seek(toOffset: start)
        var payload = (try? handle.readToEnd()) ?? Data()
        if start > 0, let nl = payload.firstIndex(of: 0x0A) { payload = payload.suffix(from: payload.index(after: nl)) }
        let configured = modelStateText.lowercased()
        for line in payload.split(separator: 0x0A).reversed() {
            guard let record = (try? JSONSerialization.jsonObject(with: Data(line))) as? [String: Any] else { continue }
            if (record["isSidechain"] as? Bool) == true || (record["type"] as? String) != "assistant" { continue }
            let message = record["message"] as? [String: Any] ?? [:]
            guard let usage = message["usage"] as? [String: Any], usage["input_tokens"] != nil else { continue }
            func tokens(_ key: String) -> Double {
                guard let n = usage[key] as? NSNumber, !(usage[key] is Bool) else { return 0 }
                return max(0, n.doubleValue.isFinite ? n.doubleValue.rounded(.towardZero) : 0)
            }
            let used = tokens("input_tokens") + tokens("cache_read_input_tokens") + tokens("cache_creation_input_tokens")
            let served = message["model"] as? String
            let serving = (served ?? "").lowercased()
            let cap: Double = (configured.contains("[1m]") || configured.contains("fable") || serving.contains("fable")) ? 1_000_000 : 200_000
            return (max(0, min(100, Int((used / cap * 100).rounded(.toNearestOrEven)))), served)
        }
        return (start == 0 ? 0 : nil, nil)
    }

    /// Watchdog restarts in the last hour: epochs in <watchdog state>/<key>.restart_log.
    static func restartsLastHour(_ bot: Bot, _ config: FleetConfig, now: Int) -> Int {
        guard let data = FileManager.default.contents(atPath: config.settings.watchdogStateDir + "/" + bot.key + ".restart_log") else { return 0 }
        return String(decoding: data, as: UTF8.self).split(separator: "\n")
            .compactMap { Int($0.filter { $0 >= "0" && $0 <= "9" }) }
            .filter { $0 >= now - 3600 }.count
    }

    /// The watchdog's stall flag, counted only when the waiting message reached the
    /// session that is running now (a flag left from before a restart is stale).
    static func stallSuspected(_ bot: Bot, _ config: FleetConfig, sessionStarted: TimeInterval?) -> Bool {
        let flag = config.settings.watchdogStateDir + "/" + bot.key + ".stall_alerted"
        guard isRegularFile(flag), let inbound = firstLineDigits(flag) else { return false }
        guard let started = sessionStarted else { return true }
        return TimeInterval(inbound) >= started
    }

    static func indicators(_ health: BotHealth, _ config: FleetConfig, withContext: Bool) -> BotIndicators {
        let saved = modelState(health.bot)
        let argv = health.sessionArgv ?? []
        let alias = flagValue(argv, ["--model"]) ?? saved.model
        let effort = flagValue(argv, ["--effort"]) ?? saved.effort
        var context: Int?
        var served: String?
        if withContext, health.state != .down {
            let resume = resolveResume(health.bot, source: config.settings.resumeSource)
            if resume.arg != "fresh-keep" {
                let modelText = (FileManager.default.contents(atPath: health.bot.stateDir + "/model-state"))
                    .map { String(decoding: $0, as: UTF8.self) } ?? ""
                let usage = transcriptUsage(transcript: health.bot.projectDir + "/" + resume.arg + ".jsonl", modelStateText: modelText)
                context = usage.percent
                served = usage.model
            }
        }
        let beatPath = health.bot.stateDir + "/poller.heartbeat"
        let servedName = served.flatMap { modelName(fromID: $0) }
        return BotIndicators(
            model: servedName ?? alias.map { modelName(fromAlias: $0) },
            servedModel: servedName,
            effort: effort,
            contextPercent: context,
            restartsLastHour: restartsLastHour(health.bot, config, now: health.checkedAt),
            stallSuspected: health.state == .down ? false : stallSuspected(health.bot, config, sessionStarted: health.sessionStarted),
            sessionStarted: health.sessionStarted,
            launchMode: health.launchMode.hasPrefix("resumed") ? "resumed" : health.launchMode,
            lastHeartbeat: modificationTime(beatPath))
    }

    /// The watchdog's last pass and its own network verdict, the snapshot on disk.
    static func signals(_ config: FleetConfig, now: Int) -> FleetSignals {
        let state = config.settings.watchdogStateDir
        var lastPass: TimeInterval?
        var reachable: Bool?
        if let pass = firstLineDigits(state + "/watchdog.last_pass") {
            lastPass = TimeInterval(pass)
            // This watchdog records a good network as net.good_since and removes it
            // on a pass that could not reach Telegram.
            if now - pass <= 180 { reachable = isRegularFile(state + "/net.good_since") }
        } else if let logTime = modificationTime(config.settings.watchdogLog) {
            lastPass = logTime   // the shipped watchdog logs one heartbeat line per pass
        }
        let file = config.settings.snapshotFile
        var sessions = 0
        var saved: TimeInterval?
        let exists = isRegularFile(file)
        if exists, let data = FileManager.default.contents(atPath: file) {
            saved = modificationTime(file)
            for line in String(decoding: data, as: UTF8.self).split(separator: "\n") where !line.hasPrefix("#") {
                let cols = line.split(separator: "\t", omittingEmptySubsequences: false)
                if cols.count >= 5, !cols[4].isEmpty { sessions += 1 }
            }
        }
        return FleetSignals(lastPass: lastPass, telegramReachable: reachable, snapshotFile: file,
                            snapshotExists: exists, snapshotSessions: sessions, snapshotSaved: saved)
    }

    /// Unauthenticated HTTPS request to Telegram's API host, for installs whose
    /// watchdog does not record its own network check. No token is sent.
    static func probeTelegram(timeout: TimeInterval = 8) -> Bool {
        var request = URLRequest(url: URL(string: "https://api.telegram.org/")!)
        request.httpMethod = "HEAD"
        request.timeoutInterval = timeout
        let done = DispatchSemaphore(value: 0)
        var ok = false
        URLSession(configuration: .ephemeral).dataTask(with: request) { _, response, error in
            ok = error == nil && response != nil
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + timeout + 1)
        return ok
    }
}

// MARK: - Reconnect

struct ReconnectPlan {
    let bot: Bot
    let state: BotState
    let targetPID: String     // the live session pid, or 999999 when the session is down
    let resumeArg: String     // a session id, or fresh-keep
    let resumeNote: String
    let engine: String

    var arguments: [String] { [engine, targetPID, bot.workdir, resumeArg, "quiet"] }
    var commandLine: String { (["/bin/bash"] + arguments).map(shellQuote).joined(separator: " ") }
    var keepsConversation: Bool { resumeArg != "fresh-keep" }
}

enum AutomationStatus: Equatable {
    case granted, wouldPrompt, denied, terminalNotRunning
    case unknown(Int32)

    var text: String {
        switch self {
        case .granted: return "granted"
        case .wouldPrompt: return "not granted yet (the first real Reconnect asks once)"
        case .denied: return "denied (System Settings > Privacy & Security > Automation)"
        case .terminalNotRunning: return "unknown (Terminal is not running)"
        case .unknown(let code): return "unknown (status \(code))"
        }
    }
}

enum ReconnectOutcome {
    case healedBeforeRun
    case dryRun(ReconnectPlan)
    case noEngine(String)
    case permissionBlocked(AutomationStatus)
    case engineRefused(Int32, String)
    case connected
    case stillDown(BotState)
}

extension FleetModel {
    static let engineMeanings: [Int32: String] = [
        0: "restart done",
        1: "refused: missing arguments",
        2: "refused: that workdir is not a known fleet bot",
        3: "refused: that pid is not a claude process",
        4: "refused: that pid runs in another workdir (sibling protection)",
        5: "refused: the launcher is missing",
        6: "spawned, but the new poller never came up",
        7: "skipped: another restart of this bot was already running",
        8: "refused: the target pid could not be verified",
    ]

    /// The resume id the restart passes, so a reconnect keeps the conversation.
    static func resolveResume(_ bot: Bot, source: ResumeSource) -> (arg: String, note: String) {
        switch source {
        case .sessionID:
            let file = bot.stateDir + "/session.id"
            if isRegularFile(file), let line = firstLine(file) {
                let candidate = line.filter { $0 != "\r" && $0 != "\n" }
                if !candidate.isEmpty && !candidate.contains("/") {
                    if isRegularFile(bot.projectDir + "/" + candidate + ".jsonl") {
                        return (candidate, "session.id")
                    }
                    return ("fresh-keep", "session.id names \(candidate.prefix(8)), which has no transcript yet")
                }
            }
            return ("fresh-keep", "no usable session.id")
        case .latestTranscript:
            guard let names = try? FileManager.default.contentsOfDirectory(atPath: bot.projectDir) else {
                return ("fresh-keep", "no project dir yet")
            }
            let newest = names
                .filter { $0.hasSuffix(".jsonl") && !$0.hasPrefix(".") && isRegularFile(bot.projectDir + "/" + $0) }
                .map { ($0, modificationTime(bot.projectDir + "/" + $0) ?? 0) }
                .sorted { $0.1 != $1.1 ? $0.1 > $1.1 : $0.0 < $1.0 }
                .first
            if let (name, _) = newest { return (String(name.dropLast(".jsonl".count)), "latest transcript") }
            return ("fresh-keep", "no transcript yet")
        }
    }

    static func makePlan(_ health: BotHealth, _ config: FleetConfig, engine: String,
                         resumeOverride: String? = nil) -> ReconnectPlan {
        let resume = resumeOverride.map { (arg: $0, note: "a fresh start, the /fresh path") }
            ?? resolveResume(health.bot, source: config.settings.resumeSource)
        let pid = health.sessionPIDs.first.map { String($0) } ?? "999999"
        return ReconnectPlan(bot: health.bot, state: health.state, targetPID: pid,
                             resumeArg: resume.arg, resumeNote: resume.note, engine: engine)
    }

    /// Whether this app may drive Terminal (the engines open the new bot window with
    /// osascript). askUser false never shows a dialog.
    static func terminalAutomation(askUser: Bool) -> AutomationStatus {
        var address = AEAddressDesc()
        let bundle = "com.apple.Terminal"
        let created = bundle.withCString { ptr -> OSErr in
            AECreateDesc(DescType(typeApplicationBundleID), ptr, strlen(ptr), &address)
        }
        guard created == noErr else { return .unknown(Int32(created)) }
        defer { AEDisposeDesc(&address) }
        let status = AEDeterminePermissionToAutomateTarget(&address, AEEventClass(typeWildCard), AEEventID(typeWildCard), askUser)
        switch Int(status) {
        case Int(noErr): return .granted
        case -1744: return .wouldPrompt          // errAEEventWouldRequireUserConsent
        case -1743: return .denied               // errAEEventNotPermitted
        case -600: return .terminalNotRunning    // procNotFound
        default: return .unknown(status)
        }
    }

    static func engineEnvironment() -> [String: String] {
        var env = Env.vars.filter { !$0.key.hasPrefix("TELEGRAM_") }
        env["PATH"] = FleetPaths.enginePATH
        return env
    }

    /// One reconnect, start to finish. Runs off the main thread (it waits on the
    /// engine and then on the new poller). Never restarts a connected bot unless the
    /// caller confirmed it.
    static func performReconnect(_ bot: Bot, _ config: FleetConfig, allowConnected: Bool, dryRun: Bool,
                                 askForPermission: Bool, freshStart: Bool = false,
                                 log: (String) -> Void) -> ReconnectOutcome {
        let action = freshStart ? "fresh start" : "reconnect"
        let health = evaluateOne(bot, config)
        let who = "\(bot.key) (\(health.state.rawValue), session \(health.sessionPIDs.first.map { String($0) } ?? "none"), poller \(health.pollerPIDs.first.map { String($0) } ?? "none"))"
        if health.state == .connected && !allowConnected && !dryRun {
            log("\(action) \(bot.key): connected again before the restart ran, nothing to do")
            return .healedBeforeRun
        }
        let resolved = resolveFreshEngine(config)
        guard let engine = resolved.path else {
            log("\(action) \(bot.key): cannot run, \(resolved.note)")
            return .noEngine(resolved.note)
        }
        let plan = makePlan(health, config, engine: engine,
                            resumeOverride: freshStart ? config.settings.freshStartArg : nil)

        if dryRun {
            if freshStart {
                log("[DRY-RUN] fresh start \(bot.key): the panel would first ask \"Start \(bot.name) fresh?\" (Cancel is the default)")
            } else if health.state == .connected && !allowConnected {
                log("[DRY-RUN] reconnect \(bot.key): connected, so the panel would first ask \"Restart \(bot.name) even though it is connected? It keeps the conversation.\" (Cancel is the default)")
            }
            log("[DRY-RUN] \(action) \(who): would run: \(plan.commandLine)")
            log("[DRY-RUN]   resume \(plan.resumeArg.isEmpty ? "\"\" (empty)" : plan.resumeArg) (from \(plan.resumeNote)); PATH=\(FleetPaths.enginePATH)")
            return .dryRun(plan)
        }

        // The engine kills the old session before it asks Terminal for the new
        // window, so a Terminal permission that is missing must stop us first.
        var automation = terminalAutomation(askUser: false)
        if automation == .wouldPrompt && askForPermission {
            log("\(action) \(bot.key): asking macOS for permission to open the bot's Terminal window")
            automation = terminalAutomation(askUser: true)
        }
        if automation == .denied || automation == .wouldPrompt {
            log("\(action) \(bot.key): stopped before anything was touched, Terminal automation is \(automation.text)")
            return .permissionBlocked(automation)
        }

        log("\(action) \(who): running \(plan.commandLine) (resume from \(plan.resumeNote))")
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = plan.arguments
        process.environment = engineEnvironment()
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            log("\(action) \(bot.key): could not start /bin/bash (\(error.localizedDescription))")
            return .engineRefused(-1, "could not start the engine")
        }
        process.waitUntilExit()
        let code = process.terminationStatus
        let signaled = process.terminationReason == .uncaughtSignal
        let meaning = signaled ? "engine killed by signal \(code)" : (engineMeanings[code] ?? "engine failed")
        log("\(action) \(bot.key): engine exit \(code), \(meaning)")
        guard !signaled, [0, 6, 7].contains(code) else { return .engineRefused(code, meaning) }

        // The shipped engine returns as soon as the window opens; wait for the poller.
        let deadline = Date().addingTimeInterval(45)
        var after = evaluateOne(bot, config)
        while after.state != .connected && Date() < deadline {
            Thread.sleep(forTimeInterval: 2)
            after = evaluateOne(bot, config)
        }
        if after.state == .connected {
            log("\(action) \(bot.key): connected (session \(after.sessionPIDs.first.map { String($0) } ?? "?"), poller \(after.pollerPIDs.first.map { String($0) } ?? "?"), \(after.launchMode))")
            return .connected
        }
        log("\(action) \(bot.key): still \(after.state.rawValue) 45s after the engine finished")
        return .stillDown(after.state)
    }
}

// MARK: - Snapshot and restore

enum FleetEngineAction: String {
    case snapshot, restore
}

struct EngineRun {
    let code: Int32
    let summary: String
}

extension FleetModel {
    static func enginePath(_ action: FleetEngineAction, _ config: FleetConfig) -> String {
        action == .snapshot ? config.settings.snapshotEngine : config.settings.restoreEngine
    }

    static func engineCommandLine(_ action: FleetEngineAction, _ config: FleetConfig) -> String {
        ["/bin/bash", enginePath(action, config)].map(shellQuote).joined(separator: " ")
    }

    /// What a real run does, in one line, for the dry-run log and the tooltips.
    static func engineEffect(_ action: FleetEngineAction, _ config: FleetConfig) -> String {
        switch action {
        case .snapshot:
            return "records every bot's current session in \(config.settings.snapshotFile) and pauses auto-recovery (a <key>.disabled file for each bot not already off) until Restore; it kills and launches nothing"
        case .restore:
            return "reopens every bot that is not running, resumed into its saved session in a new Terminal window, skips bots that are running, then turns auto-recovery back on and archives the snapshot"
        }
    }

    /// Runs the snapshot or restore engine, or in dry run logs exactly what it would run.
    static func runFleetEngine(_ action: FleetEngineAction, _ config: FleetConfig, dryRun: Bool,
                               log: (String) -> Void) -> EngineRun? {
        let path = enginePath(action, config)
        guard isRegularFile(path) else {
            log("\(action.rawValue): cannot run, no engine at \(path)")
            return nil
        }
        let command = engineCommandLine(action, config)
        let noSnapshot = action == .restore && !isRegularFile(config.settings.snapshotFile)
        if dryRun {
            log("[DRY-RUN] \(action.rawValue): would run: \(command)")
            log("[DRY-RUN]   a real run \(engineEffect(action, config))")
            if noSnapshot {
                log("[DRY-RUN]   no snapshot at \(config.settings.snapshotFile) right now, so a real run would stop at once and change nothing")
            }
            return EngineRun(code: 0, summary: "dry run")
        }
        if noSnapshot {
            log("restore: no snapshot at \(config.settings.snapshotFile), nothing to restore")
            return nil
        }
        log("\(action.rawValue): running \(command)")
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [path]
        process.environment = engineEnvironment()
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
        } catch {
            log("\(action.rawValue): could not start /bin/bash (\(error.localizedDescription))")
            return nil
        }
        let output = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let lines = String(decoding: output.prefix(65_536), as: UTF8.self)
            .split(separator: "\n").map { redactTokens(String($0)).trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let summary = lines.last(where: { !$0.hasPrefix("  ") }) ?? "no output"
        log("\(action.rawValue): engine exit \(process.terminationStatus): \(summary)")
        return EngineRun(code: process.terminationStatus, summary: summary)
    }
}

// MARK: - Watchdog kill switches

struct WatchdogSwitches {
    let dir: String
    let dirExists: Bool
    let disabled: [Bot]
    let total: Int

    var isOn: Bool { disabled.isEmpty }
    var isFullyOff: Bool { total > 0 && disabled.count == total }

    var label: String {
        if isOn { return "Watchdog: on" }
        if isFullyOff { return "Watchdog: off" }
        return "Watchdog: off for " + disabled.map(\.name).joined(separator: ", ")
    }
}

extension FleetModel {
    static func readWatchdog(_ config: FleetConfig) -> WatchdogSwitches {
        let dir = config.settings.watchdogStateDir
        let disabled = config.bots.filter { FileManager.default.fileExists(atPath: dir + "/" + $0.key + ".disabled") }
        return WatchdogSwitches(dir: dir, dirExists: isDirectory(dir), disabled: disabled, total: config.bots.count)
    }

    /// Off: create <key>.disabled for every bot that lacks one (what the snapshot
    /// engine touches). On: remove every bot's <key>.disabled (what restore removes).
    /// Nothing else is touched, not even the crash-loop breaker's own markers.
    @discardableResult
    static func applyWatchdog(on: Bool, _ config: FleetConfig, dryRun: Bool, log: (String) -> Void) -> Bool {
        let dir = config.settings.watchdogStateDir
        guard isDirectory(dir) else {
            log("watchdog: \(dir) does not exist, nothing changed (is the watchdog installed?)")
            return false
        }
        let prefix = dryRun ? "[DRY-RUN] " : ""
        var changed = 0
        var ok = true
        for bot in config.bots {
            let file = dir + "/" + bot.key + ".disabled"
            let exists = FileManager.default.fileExists(atPath: file)
            if on && exists {
                if dryRun {
                    log("\(prefix)watchdog on: would remove \(file)")
                } else if (try? FileManager.default.removeItem(atPath: file)) != nil {
                    log("watchdog on: removed \(file)")
                } else {
                    log("watchdog on: could not remove \(file)"); ok = false
                }
                changed += 1
            } else if !on && !exists {
                if dryRun {
                    log("\(prefix)watchdog off: would create \(file)")
                } else if FileManager.default.createFile(atPath: file, contents: Data()) {
                    log("watchdog off: created \(file)")
                } else {
                    log("watchdog off: could not create \(file)"); ok = false
                }
                changed += 1
            }
        }
        let verb = on ? "on" : "off"
        log("\(prefix)watchdog \(verb): \(changed) file(s) \(dryRun ? "would change" : "changed") in \(dir), \(config.bots.count - changed) already \(on ? "absent" : "present")")
        return ok
    }
}

// MARK: - Action log

/// Appends one line per action to $FLEET_HOME/menubar.log, stamped in the
/// configured time zone (America/New_York unless menubar.time_zone says otherwise).
final class ActionLog {
    static let shared = ActionLog()

    var enabled = true
    var echo = false
    private let queue = DispatchQueue(label: "com.nexera.telebots-menubar.log")
    private let formatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss zzz"
        f.timeZone = FleetModel.defaultTimeZone
        return f
    }()

    func setTimeZone(_ zone: TimeZone) {
        queue.sync { formatter.timeZone = zone }
    }

    func write(_ message: String) {
        let clean = redactTokens(message)
        if echo {
            print(clean)
            fflush(stdout)
        }
        guard enabled else { return }
        queue.sync {
            let path = FleetPaths.actionLog
            let dir = (path as NSString).deletingLastPathComponent
            try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
            var st = stat()
            if stat(path, &st) == 0 && st.st_size > 1_048_576 {
                _ = rename(path, path + ".1")
            }
            let line = formatter.string(from: Date()) + " " + clean + "\n"
            let fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
            guard fd >= 0 else { return }
            line.utf8CString.withUnsafeBufferPointer { buf in
                _ = Darwin.write(fd, buf.baseAddress, buf.count - 1)
            }
            close(fd)
        }
    }
}
