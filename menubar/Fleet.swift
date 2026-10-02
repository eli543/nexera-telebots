// Fleet.swift - the model behind Telebots Bar: fleet config, bot health,
// reconnect plans, window cleanup, the watchdog kill switches and the action log.
//
// Everything here mirrors the fleet's own shell engines on purpose, so the menu
// bar never disagrees with the watchdog:
//   - config is resolved exactly like engine/fleet-config (FLEET_CONFIG, then
//     $FLEET_HOME/fleet.json, then ~/.claude/telegram-fleet/fleet.json);
//   - a bot is healthy exactly when the watchdog says so: a Claude session process
//     and a Telegram poller (server.ts) process whose TELEGRAM_STATE_DIR is the
//     bot's state dir, and a poller heartbeat no older than the watchdog's limit,
//     where a stale heartbeat does not count while Telegram itself is not
//     confirmed reachable (the watchdog's own outage rule);
//   - Reconnect and Fresh start work like /fresh: the bot first says in its own
//     chat that the menu bar started it, then the restart engine runs with the same
//     three arguments the /fresh hook passes (session pid or 999999, workdir, resume
//     id), the engine sends "Back and ready" itself and closes the old window, and
//     the engine does the pid + cwd guarded kill, so a sibling bot is never touched;
//   - a window is closed only when it is provably the bot's own old window: the tty
//     its session ran on, still held by exactly the shell that hosted it, as the
//     window's only tab, with no claude process in it. Otherwise it is left open and
//     the reason is logged;
//   - the watchdog switch is the per-bot <key>.disabled file (the manual kill
//     switch, which the snapshot engine also sets unless it runs --keep-watchdog);
//     the watchdog's crash-loop breaker marker, <key>.disabled-auto, is shown
//     separately and is only ever read, never created or removed.
// Processes are read natively (sysctl KERN_PROCARGS2, the data `ps eww` prints);
// nothing here shells out to the watchdog. A bot's token is read from
// <state_dir>/.env only when an action sends that bot's chat line, and it is never
// logged or kept.

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
private let phoneTokenPattern = try! NSRegularExpression(pattern: "(?i)[0-9a-f]{64}")

/// Any bot-token-shaped string becomes <redacted> before it can be shown or logged.
func redactTokens(_ text: String) -> String {
    let clean = tokenPattern.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "<redacted>")
    return phoneTokenPattern.stringByReplacingMatches(in: clean, range: NSRange(clean.startIndex..., in: clean), withTemplate: "<redacted>")
}

func shellQuote(_ arg: String) -> String {
    let safe = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./=:@%+,")
    if !arg.isEmpty && arg.unicodeScalars.allSatisfy({ safe.contains($0) }) { return arg }
    return "'" + arg.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

private let uuidPattern = try! NSRegularExpression(
    pattern: "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

func fullMatch(_ regex: NSRegularExpression, _ text: String) -> Bool {
    regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil
}

/// A Claude session id. Only these ever reach an engine as a resume id, because the
/// engines put that argument into shell and AppleScript text.
func isSessionUUID(_ text: String) -> Bool { fullMatch(uuidPattern, text) }

private let engineWordPattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9-]{0,40}$")

/// A resume sentinel such as fresh-keep or fresh-reset: letters, digits and dashes.
func isEngineWord(_ text: String) -> Bool { fullMatch(engineWordPattern, text) }

private let ttyPattern = try! NSRegularExpression(pattern: "^ttys[0-9]{1,4}$")

func isTTYName(_ text: String) -> Bool { fullMatch(ttyPattern, text) }

/// "ttys007" for a controlling-terminal device number, nil for none.
func ttyName(_ device: dev_t) -> String? {
    guard device != dev_t(bitPattern: UInt32.max), device != 0, let raw = devname(device, S_IFCHR) else { return nil }
    let name = String(cString: raw)
    return isTTYName(name) ? name : nil
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
    var serve: Bool? = nil
    var freshEngineLog: String? = nil
    /// Snapshot passes --keep-watchdog, so taking a snapshot preserves auto-recovery settings.
    var snapshotKeepWatchdog: Bool = true
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
    static let defaultTimeZone = TimeZone.current
    private static let keyPattern = try! NSRegularExpression(pattern: "^[a-z0-9][a-z0-9-]{0,30}$")
    private static let menubarKeys: Set<String> = [
        "fresh_engine", "watchdog_state_dir", "watchdog_log", "heartbeat_max_age", "resume_source", "time_zone",
        "snapshot_engine", "restore_engine", "snapshot_file", "fresh_start_arg",
        "serve", "fresh_engine_log", "snapshot_keep_watchdog",
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
                path("fresh_engine_log") { settings.freshEngineLog = $0 }
                path("watchdog_state_dir") { settings.watchdogStateDir = $0 }
                path("watchdog_log") { settings.watchdogLog = $0 }
                path("snapshot_engine") { settings.snapshotEngine = $0 }
                path("restore_engine") { settings.restoreEngine = $0 }
                path("snapshot_file") { settings.snapshotFile = $0 }
                if let v = mb["serve"] {
                    if let n = v as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() {
                        settings.serve = n.boolValue
                    } else {
                        warnings.append("menubar.serve must be true or false; default kept")
                    }
                }
                if let v = mb["snapshot_keep_watchdog"] {
                    if let n = v as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() {
                        settings.snapshotKeepWatchdog = n.boolValue
                    } else {
                        warnings.append("menubar.snapshot_keep_watchdog must be true or false; default kept (true: Snapshot preserves auto-recovery settings)")
                    }
                }
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
                    if let s = v as? String, isEngineWord(s) { settings.freshStartArg = s } else { warnings.append("menubar.fresh_start_arg must be letters, digits and dashes, like fresh-reset; default kept") }
                }
                if let v = mb["time_zone"] {
                    if let s = v as? String, let tz = TimeZone(identifier: s) { settings.timeZone = tz } else { warnings.append("menubar.time_zone must be a time zone name like Europe/London; system default kept") }
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
/// TELEGRAM_STATE_DIR each one was started with, plus the kernel's basic record of
/// every process (any user), which is what a tty check needs.
struct ProcessTable {
    struct Entry {
        let pid: pid_t
        let argv: [String]
        let stateDir: String?
        let isSession: Bool
        let isPoller: Bool
        let started: TimeInterval   // process start, epoch seconds
    }

    /// What the kernel lists for any process: no arguments, no environment.
    struct Basic {
        let pid: pid_t
        let ppid: pid_t
        let uid: uid_t
        let tty: dev_t              // controlling terminal, or NODEV
        let started: TimeInterval
        let name: String            // p_comm, at most 16 characters
    }

    /// The watchdog's own signatures: `grep 'channels plugin:telegram'` for the
    /// session and `grep 'server.ts'` for the poller, each followed by `grep -v grep`.
    static let sessionSignature = "channels plugin:telegram"
    static let pollerSignature = "server.ts"

    let entries: [Entry]
    let basics: [pid_t: Basic]

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
        basics[pid]?.started ?? entries.first { $0.pid == pid }?.started
    }

    /// Every process whose controlling terminal is `device`, lowest pid first.
    func processes(onTTY device: dev_t) -> [Basic] {
        basics.values.filter { $0.tty == device }.sorted { $0.pid < $1.pid }
    }

    /// Where a session's Terminal tab is: the tty it runs on and the processes that
    /// host it there (its shell, then the login above it), nearest first.
    func windowRecord(session pid: pid_t) -> WindowRecord? {
        guard let session = basics[pid], let tty = ttyName(session.tty) else { return nil }
        var hosts: [WindowRecord.Host] = []
        var next = session.ppid
        while hosts.count < 8, next > 1, let parent = basics[next], parent.tty == session.tty {
            hosts.append(WindowRecord.Host(pid: parent.pid, started: parent.started, name: parent.name))
            next = parent.ppid
        }
        guard !hosts.isEmpty else { return nil }
        return WindowRecord(tty: tty, sessionPID: pid, sessionStarted: session.started, hosts: hosts,
                            recorded: Date().timeIntervalSince1970)
    }

    static func snapshot() -> ProcessTable {
        var buffer = [UInt8](repeating: 0, count: argMax())
        let all = allProcesses()
        var basics: [pid_t: Basic] = [:]
        for basic in all { basics[basic.pid] = basic }
        let uid = getuid()
        var found: [Entry] = []
        for basic in all where basic.pid > 0 && basic.uid == uid {
            guard let (argv, env) = processArguments(basic.pid, &buffer) else { continue }
            let command = argv.joined(separator: " ")
            if command.contains("grep") { continue }
            let isSession = command.contains(sessionSignature)
            let isPoller = command.contains(pollerSignature)
            guard isSession || isPoller else { continue }
            let prefix = "TELEGRAM_STATE_DIR="
            let stateDir = env.first { $0.hasPrefix(prefix) }.map { String($0.dropFirst(prefix.count)) }
            found.append(Entry(pid: basic.pid, argv: argv, stateDir: stateDir, isSession: isSession,
                               isPoller: isPoller, started: basic.started))
        }
        return ProcessTable(entries: found, basics: basics)
    }

    /// The kernel's process list alone, cheap: for tty checks.
    static func basicsOnly() -> ProcessTable {
        var basics: [pid_t: Basic] = [:]
        for basic in allProcesses() { basics[basic.pid] = basic }
        return ProcessTable(entries: [], basics: basics)
    }

    private static func argMax() -> Int {
        var mib: [Int32] = [CTL_KERN, KERN_ARGMAX]
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        if sysctl(&mib, 2, &value, &size, nil, 0) == 0, value > 0 { return Int(value) }
        return 1 << 20
    }

    private static func allProcesses() -> [Basic] {
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
            return procs.prefix(size / stride).map { p in
                let start = p.kp_proc.p_un.__p_starttime
                let name = withUnsafeBytes(of: p.kp_proc.p_comm) { raw -> String in
                    String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
                }
                return Basic(pid: p.kp_proc.p_pid, ppid: p.kp_eproc.e_ppid, uid: p.kp_eproc.e_ucred.cr_uid,
                             tty: p.kp_eproc.e_tdev,
                             started: TimeInterval(start.tv_sec) + TimeInterval(start.tv_usec) / 1e6, name: name)
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

/// The working directory of a process, as the kernel reports it (what `lsof -d cwd`
/// prints), or nil when it cannot be read.
func processCWD(_ pid: pid_t) -> String? {
    var info = proc_vnodepathinfo()
    let size = Int32(MemoryLayout<proc_vnodepathinfo>.size)
    guard proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, &info, size) == size else { return nil }
    let path = withUnsafeBytes(of: info.pvi_cdir.vip_path) { raw -> String in
        String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
    }
    return path.isEmpty ? nil : path
}

// MARK: - Health

enum BotState: String {
    case connected = "Connected"
    case noPoller = "No poller"
    case down = "Down"
}

/// Whether a stale heartbeat counts against a bot right now. The live watchdog does
/// not treat one as a fault while Telegram is not confirmed reachable, or while the
/// code that writes heartbeats is known to be missing, and neither does the app.
enum HeartbeatTrust: Equatable {
    case trusted
    case excused(String)

    var excuse: String? {
        if case .excused(let why) = self { return why }
        return nil
    }
}

struct BotHealth {
    let bot: Bot
    let state: BotState
    let sessionPIDs: [pid_t]
    let pollerPIDs: [pid_t]
    let heartbeat: Int?          // epoch seconds from poller.heartbeat
    let heartbeatStale: Bool
    let staleExcused: String?    // why a stale heartbeat does not count right now
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
            if let age = heartbeatAge {
                return "Connected · heartbeat \(shortAge(age)) ago" + (staleExcused != nil ? ", waiting on Telegram" : "")
            }
            return "Connected"
        case .noPoller: return "No poller"
        case .down: return "Down"
        }
    }
}

extension FleetModel {
    /// The live watchdog's own numbers: a stale heartbeat only counts once Telegram
    /// has been confirmed reachable this long, and a pass older than this means the
    /// watchdog was asleep or stopped.
    static let networkGoodDwell = 600
    static let passGapMax = 180

    /// A state file's first line as an epoch, only when it is all digits (the
    /// watchdog rejects anything else the same way).
    static func strictEpoch(_ path: String) -> Int? {
        guard let line = firstLine(path)?.trimmingCharacters(in: CharacterSet(charactersIn: "\r\n ")),
              !line.isEmpty, line.allSatisfy({ $0 >= "0" && $0 <= "9" }) else { return nil }
        return Int(line)
    }

    /// The watchdog's rule for a stale heartbeat, from its own state files: the
    /// plugin.alert-only latch, and net.good_since against its 600s dwell while its
    /// passes are current. With no current watchdog, this Mac's own probe decides.
    static func heartbeatTrust(_ config: FleetConfig, now: Int, probe: Bool?) -> HeartbeatTrust {
        let state = config.settings.watchdogStateDir
        if isRegularFile(state + "/plugin.alert-only") {
            return .excused("the watchdog is alert-only because the code that writes heartbeats is missing, so a stale heartbeat is not a fault")
        }
        if let pass = strictEpoch(state + "/watchdog.last_pass"), now - pass >= 0, now - pass <= passGapMax {
            guard let since = strictEpoch(state + "/net.good_since"), since <= now else {
                return .excused("Telegram was not reachable on the watchdog's last check")
            }
            let good = now - since
            if good < networkGoodDwell {
                return .excused("Telegram has been reachable for only \(shortAge(good)) of the \(networkGoodDwell / 60)m the watchdog waits before it trusts a stale heartbeat")
            }
            return .trusted
        }
        if probe == false { return .excused("Telegram is not reachable from this Mac") }
        return .trusted
    }

    /// The watchdog's per-bot test, same files, same order.
    static func evaluate(_ bot: Bot, table: ProcessTable, now: Int, maxAge: Int,
                         trust: HeartbeatTrust = .trusted) -> BotHealth {
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
        let excused = stale ? trust.excuse : nil
        let state: BotState
        if !sessions.isEmpty && !pollers.isEmpty && (!stale || excused != nil) {
            state = .connected
        } else if sessions.isEmpty {
            state = .down
        } else {
            state = .noPoller
        }
        let argv = sessions.first.flatMap { table.argv(of: $0) }
        let started = sessions.first.flatMap { table.started($0) }
        return BotHealth(bot: bot, state: state, sessionPIDs: sessions, pollerPIDs: pollers,
                         heartbeat: heartbeat, heartbeatStale: stale, staleExcused: excused, sessionArgv: argv,
                         sessionStarted: started, checkedAt: now)
    }

    static func evaluateAll(_ config: FleetConfig, probe: Bool? = nil) -> [BotHealth] {
        let table = ProcessTable.snapshot()
        let now = Int(Date().timeIntervalSince1970)
        let trust = heartbeatTrust(config, now: now, probe: probe)
        return config.bots.map { evaluate($0, table: table, now: now, maxAge: config.settings.heartbeatMaxAge, trust: trust) }
    }

    static func evaluateOne(_ bot: Bot, _ config: FleetConfig, table: ProcessTable = ProcessTable.snapshot()) -> BotHealth {
        let now = Int(Date().timeIntervalSince1970)
        return evaluate(bot, table: table, now: now, maxAge: config.settings.heartbeatMaxAge,
                        trust: heartbeatTrust(config, now: now, probe: nil))
    }
}

// MARK: - Indicators (read-only)

/// Everything the second line, the expanded line and the header show for a bot.
struct BotIndicators: Equatable {
    var model: String?          // "Opus 5.5" from the newest reply, else the alias ("Opus")
    var servedModel: String?    // the name from the transcript alone, nil before the first reply
    var servedAt: TimeInterval? // when that transcript was last written (recency among siblings)
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

    /// The model family of a display name: "Opus 5.5" and "Opus" are both "opus".
    static func modelFamily(_ name: String) -> String {
        String(name.split(separator: " ").first ?? "").lowercased()
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
        var servedAt: TimeInterval?
        if withContext, health.state != .down {
            let resume = resolveResume(health.bot, source: config.settings.resumeSource)
            if resume.arg != "fresh-keep" {
                let modelText = (FileManager.default.contents(atPath: health.bot.stateDir + "/model-state"))
                    .map { String(decoding: $0, as: UTF8.self) } ?? ""
                let transcript = health.bot.projectDir + "/" + resume.arg + ".jsonl"
                let usage = transcriptUsage(transcript: transcript, modelStateText: modelText)
                // Context stays hidden until the session has a usage record of its own.
                if usage.model != nil {
                    context = usage.percent
                    served = usage.model
                    servedAt = modificationTime(transcript)
                }
            }
        }
        let beatPath = health.bot.stateDir + "/poller.heartbeat"
        let servedName = served.flatMap { modelName(fromID: $0) }
        return BotIndicators(
            model: servedName ?? alias.map { modelName(fromAlias: $0) },
            servedModel: servedName,
            servedAt: servedName == nil ? nil : servedAt,
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
}

// MARK: - Child processes

/// The last `limit` bytes a child wrote, gathered as it writes them.
final class OutputTail {
    private let lock = NSLock()
    private var data = Data()
    private let limit: Int
    private let ended = DispatchSemaphore(value: 0)

    init(limit: Int) { self.limit = limit }

    func append(_ chunk: Data) {
        lock.lock()
        data.append(chunk)
        if data.count > limit { data.removeFirst(data.count - limit) }
        lock.unlock()
    }

    func finish() { ended.signal() }

    func waitForEnd(_ seconds: TimeInterval) { _ = ended.wait(timeout: .now() + seconds) }

    var text: String {
        lock.lock()
        defer { lock.unlock() }
        return String(decoding: data, as: UTF8.self)
    }
}

/// A child process with an overall deadline and a bounded capture of its output. At
/// the deadline the exact child this started gets SIGTERM, then SIGKILL; nothing else.
enum Subprocess {
    struct Result {
        let code: Int32        // exit status, the signal number when signaled, -1 if it never ended
        let signaled: Bool
        let timedOut: Bool
        let output: String     // stdout and stderr together, the last bytes only
    }

    static func run(_ executable: String, _ arguments: [String], environment: [String: String]? = nil,
                    input: Data? = nil, capture: Bool = true, limit: Int = 65_536, processGroup: Bool = false,
                    deadline: TimeInterval, started: (pid_t) -> Void = { _ in }) throws -> Result {
        if processGroup {
            return try runGroup(executable, arguments, environment: environment, capture: capture,
                                limit: limit, deadline: deadline, started: started)
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        if let environment { process.environment = environment }
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        let tail = OutputTail(limit: limit)
        var pipe: Pipe?
        if capture {
            let out = Pipe()
            out.fileHandleForReading.readabilityHandler = { handle in
                let chunk = handle.availableData
                if chunk.isEmpty {
                    handle.readabilityHandler = nil
                    tail.finish()
                } else {
                    tail.append(chunk)
                }
            }
            process.standardOutput = out
            process.standardError = out
            pipe = out
        } else {
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
        }
        var inputPipe: Pipe?
        if input != nil {
            let pipeIn = Pipe()
            process.standardInput = pipeIn
            inputPipe = pipeIn
        } else {
            process.standardInput = FileHandle.nullDevice
        }
        let done = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in done.signal() }
        try process.run()
        let pid = process.processIdentifier
        started(pid)
        if let inputPipe, let input {
            // A child that exits before reading must not take this app down with SIGPIPE.
            let handle = inputPipe.fileHandleForWriting
            _ = fcntl(handle.fileDescriptor, F_SETNOSIGPIPE, 1)
            try? handle.write(contentsOf: input)
            try? handle.close()
        }
        var timedOut = false
        var ended = done.wait(timeout: .now() + deadline) == .success
        if !ended {
            timedOut = true
            kill(pid, SIGTERM)
            ended = done.wait(timeout: .now() + 5) == .success
            if !ended {
                kill(pid, SIGKILL)
                ended = done.wait(timeout: .now() + 5) == .success
            }
        }
        if let pipe {
            // What the child wrote last may still be on its way; a grandchild that kept
            // the pipe open must not hold this up, so the wait is short.
            tail.waitForEnd(ended ? 1 : 0)
            pipe.fileHandleForReading.readabilityHandler = nil
        }
        let code: Int32 = ended ? process.terminationStatus : -1
        let signaled = ended && process.terminationReason == .uncaughtSignal
        return Result(code: code, signaled: signaled, timedOut: timedOut, output: tail.text)
    }

    /// Engines and all their control children share a new process group. A shell
    /// exiting early cannot spare a child from the deadline's TERM/KILL sequence.
    private static func runGroup(_ executable: String, _ arguments: [String],
                                 environment: [String: String]?, capture: Bool, limit: Int,
                                 deadline: TimeInterval, started: (pid_t) -> Void) throws -> Result {
        var attrs: posix_spawnattr_t?
        var actions: posix_spawn_file_actions_t?
        posix_spawnattr_init(&attrs)
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawnattr_destroy(&attrs); posix_spawn_file_actions_destroy(&actions) }
        posix_spawnattr_setflags(&attrs, Int16(POSIX_SPAWN_SETPGROUP))
        posix_spawnattr_setpgroup(&attrs, 0)
        let tail = OutputTail(limit: limit)
        let output = Pipe()
        let null = open("/dev/null", O_RDWR)
        guard null >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        defer { close(null) }
        let outFD = capture ? output.fileHandleForWriting.fileDescriptor : null
        posix_spawn_file_actions_adddup2(&actions, null, STDIN_FILENO)
        posix_spawn_file_actions_adddup2(&actions, outFD, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(&actions, outFD, STDERR_FILENO)
        posix_spawn_file_actions_addclose(&actions, output.fileHandleForReading.fileDescriptor)
        posix_spawn_file_actions_addclose(&actions, output.fileHandleForWriting.fileDescriptor)
        posix_spawn_file_actions_addchdir_np(&actions, "/")
        var env = environment ?? ProcessInfo.processInfo.environment
        env["CC_FLEET_MANAGED_GROUP"] = "1"
        let argv = ([executable] + arguments).map { strdup($0) } + [nil]
        let envp = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { argv.forEach { free($0) }; envp.forEach { free($0) } }
        var pid: pid_t = 0
        let rc = argv.withUnsafeBufferPointer { av in
            envp.withUnsafeBufferPointer { ep in
                posix_spawn(&pid, executable, &actions, &attrs, av.baseAddress!, ep.baseAddress!)
            }
        }
        guard rc == 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(rc)) }
        try? output.fileHandleForWriting.close()
        if capture {
            output.fileHandleForReading.readabilityHandler = { handle in
                let bytes = handle.availableData
                if bytes.isEmpty { handle.readabilityHandler = nil; tail.finish() }
                else { tail.append(bytes) }
            }
        }
        started(pid)
        var status: Int32 = 0
        var ended = false
        func reap() {
            if !ended { ended = waitpid(pid, &status, WNOHANG) == pid }
        }
        let end = Date().addingTimeInterval(deadline)
        func groupAlive() -> Bool { kill(-pid, 0) == 0 || errno == EPERM }
        repeat {
            reap()
            if ended && !groupAlive() { break }
            Thread.sleep(forTimeInterval: 0.05)
        } while Date() < end
        let timedOut = !ended || groupAlive()
        if timedOut {
            kill(-pid, SIGTERM)
            let grace = Date().addingTimeInterval(5)
            repeat {
                reap()
                if kill(-pid, 0) != 0 && errno == ESRCH { break }
                Thread.sleep(forTimeInterval: 0.05)
            } while Date() < grace
            // Always address the group, even when its original shell has exited.
            if kill(-pid, 0) == 0 || errno == EPERM { kill(-pid, SIGKILL) }
            let killed = Date().addingTimeInterval(5)
            repeat {
                reap()
                if kill(-pid, 0) != 0 && errno == ESRCH { break }
                Thread.sleep(forTimeInterval: 0.05)
            } while Date() < killed
        }
        if capture { tail.waitForEnd(ended ? 1 : 0) }
        output.fileHandleForReading.readabilityHandler = nil
        let signal = status & 0x7f
        return Result(code: ended ? (signal == 0 ? (status >> 8) & 0xff : signal) : -1,
                      signaled: ended && signal != 0, timedOut: timedOut, output: tail.text)
    }

}

// MARK: - Terminal windows

/// Where a bot's session ran: the tty of its Terminal tab and the processes that
/// hosted it there. Recorded while the session runs, so the window it leaves behind
/// once it is gone can still be proven to be that bot's own.
struct WindowRecord: Codable, Equatable {
    struct Host: Codable, Equatable {
        let pid: Int32
        let started: Double
        let name: String
    }

    let tty: String              // "ttys007"
    let sessionPID: Int32
    let sessionStarted: Double
    let hosts: [Host]            // the session's shell, then the login above it
    var recorded: Double

    /// The same session in the same tab, whenever it was recorded.
    func sameSession(_ other: WindowRecord) -> Bool {
        tty == other.tty && sessionPID == other.sessionPID && abs(sessionStarted - other.sessionStarted) < 0.01
            && hosts == other.hosts
    }

    var hostList: String { hosts.map { "\($0.name) \($0.pid)" }.joined(separator: ", ") }
}

/// $FLEET_HOME/menubar-windows.json: each bot's last recorded window, written by the
/// running menu bar app and read by every action, in the app or on the command line.
enum WindowRecords {
    static var path: String { FleetPaths.fleetHome + "/menubar-windows.json" }

    private struct Stored: Codable {
        var version: Int
        var bots: [String: WindowRecord]
    }

    static func load() -> [String: WindowRecord] {
        guard let data = FileManager.default.contents(atPath: path),
              let stored = try? JSONDecoder().decode(Stored.self, from: data), stored.version == 1 else { return [:] }
        return stored.bots.filter { isTTYName($0.value.tty) && !$0.value.hosts.isEmpty }
    }

    /// Written whole to a temporary file and renamed into place.
    @discardableResult
    static func save(_ records: [String: WindowRecord]) -> Bool {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(Stored(version: 1, bots: records)) else { return false }
        let dir = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let temp = path + ".tmp.\(getpid())"
        guard FileManager.default.createFile(atPath: temp, contents: data, attributes: [.posixPermissions: 0o600]) else { return false }
        if rename(temp, path) != 0 {
            unlink(temp)
            return false
        }
        return true
    }
}

// MARK: - The bot's own chat

/// Telegram's API with a hard deadline: the task is cancelled at the deadline and the
/// session torn down, so nothing outlives the call.
enum HTTP {
    private final class Box: @unchecked Sendable {
        let lock = NSLock()
        var data: Data?
        var response: URLResponse?
        var error: Error?
    }

    static func run(_ request: URLRequest, timeout: TimeInterval) -> (Data?, URLResponse?, Error?) {
        let box = Box()
        let done = DispatchSemaphore(value: 0)
        let session = URLSession(configuration: .ephemeral)
        let task = session.dataTask(with: request) { data, response, error in
            box.lock.lock()
            box.data = data
            box.response = response
            box.error = error
            box.lock.unlock()
            done.signal()
        }
        task.resume()
        let finished = done.wait(timeout: .now() + timeout + 1) == .success
        if !finished { task.cancel() }
        session.invalidateAndCancel()
        guard finished else { return (nil, nil, URLError(.timedOut)) }
        box.lock.lock()
        defer { box.lock.unlock() }
        return (box.data, box.response, box.error)
    }
}

/// A cancellable, unauthenticated reachability check of Telegram's API host, for
/// installs whose watchdog does not record its own. No token is sent.
final class TelegramProbe {
    private let lock = NSLock()
    private var task: URLSessionDataTask?
    private var session: URLSession?

    func start(timeout: TimeInterval = 8, completion: @escaping (Bool) -> Void) {
        cancel()
        var request = URLRequest(url: URL(string: "https://api.telegram.org/")!)
        request.httpMethod = "HEAD"
        request.timeoutInterval = timeout
        let session = URLSession(configuration: .ephemeral)
        let task = session.dataTask(with: request) { [weak self] _, response, error in
            let cancelled = (error as? URLError)?.code == .cancelled
            self?.finished(session)
            if !cancelled { completion(error == nil && response != nil) }
        }
        lock.lock()
        self.task = task
        self.session = session
        lock.unlock()
        task.resume()
    }

    func cancel() {
        lock.lock()
        let task = self.task, session = self.session
        self.task = nil
        self.session = nil
        lock.unlock()
        task?.cancel()
        session?.invalidateAndCancel()
    }

    private func finished(_ done: URLSession) {
        lock.lock()
        if session === done {
            task = nil
            session = nil
        }
        lock.unlock()
        done.finishTasksAndInvalidate()
    }
}

struct ChatSend {
    let ok: Bool
    let detail: String         // "message 1234", or why it was not sent; never the token
}

/// The bot's own voice in its own chat, the way /fresh speaks: the same token and chat
/// the engines use (token_of_state and chat_id_of_state in engine/fleet-fresh).
enum BotChat {
    static let backAndReady = "\u{26A1} Back and ready"

    /// "Reconnect from the menu bar, one moment".
    static func line(_ action: String) -> String { "\(action) from the menu bar, one moment" }

    private static let tokenShape = try! NSRegularExpression(pattern: "^[0-9]{3,20}:[A-Za-z0-9_-]{20,200}$")
    private static let chatShape = try! NSRegularExpression(pattern: "^-?[0-9]{1,20}$")

    /// token_of_state: the first TELEGRAM_BOT_TOKEN= line of <state_dir>/.env, quotes
    /// and whitespace removed. Read only when a line is about to go out.
    static func token(_ bot: Bot) -> String? {
        guard let data = FileManager.default.contents(atPath: bot.stateDir + "/.env") else { return nil }
        let prefix = "TELEGRAM_BOT_TOKEN="
        for line in String(decoding: data, as: UTF8.self).split(separator: "\n") where line.hasPrefix(prefix) {
            let value = String(line.dropFirst(prefix.count).filter { $0 != "\"" && $0 != "'" && !$0.isWhitespace })
            return fullMatch(tokenShape, value) ? value : nil
        }
        return nil
    }

    /// chat_id_of_state: the first allowFrom entry of <state_dir>/access.json, the
    /// bot's own chat.
    static func chatID(_ bot: Bot) -> String? {
        guard let data = FileManager.default.contents(atPath: bot.stateDir + "/access.json"),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let list = root["allowFrom"] as? [Any], let first = list.first else { return nil }
        let text: String
        if let s = first as? String {
            text = s
        } else if let n = first as? NSNumber, !(first is Bool) {
            text = n.stringValue
        } else {
            return nil
        }
        return fullMatch(chatShape, text) ? text : nil
    }

    /// For a dry run: whether a line could go out, without keeping the token.
    static func readiness(_ bot: Bot) -> String {
        let chat = chatID(bot) != nil ? "its chat from access.json" : "no chat id in access.json, so it would be skipped"
        let token = token(bot) != nil ? "its token from .env" : "no bot token in .env, so it would be skipped"
        return "\(token), \(chat)"
    }

    private static func form(_ fields: [(String, String)]) -> Data {
        // Plain ASCII letters and digits only: everything else is percent-encoded.
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        let body = fields.map { key, value in
            key + "=" + (value.addingPercentEncoding(withAllowedCharacters: allowed) ?? "")
        }.joined(separator: "&")
        return Data(body.utf8)
    }

    /// One sendMessage from the bot to its chat. The token is used for this call only
    /// and never appears in what this returns.
    static func send(_ bot: Bot, _ text: String, timeout: TimeInterval = 10) -> ChatSend {
        guard let chat = chatID(bot) else { return ChatSend(ok: false, detail: "no chat id in \(bot.stateDir)/access.json") }
        guard let token = token(bot) else { return ChatSend(ok: false, detail: "no bot token in \(bot.stateDir)/.env") }
        guard let url = URL(string: "https://api.telegram.org/bot\(token)/sendMessage") else {
            return ChatSend(ok: false, detail: "the bot token cannot form a URL")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = form([("chat_id", chat), ("text", text)])
        let (data, response, error) = HTTP.run(request, timeout: timeout)
        if let error {
            let code = (error as? URLError)?.code.rawValue ?? (error as NSError).code
            return ChatSend(ok: false, detail: "the request failed (network error \(code))")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let json = data.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }
        if (json?["ok"] as? Bool) == true {
            let id = ((json?["result"] as? [String: Any])?["message_id"] as? NSNumber)?.stringValue ?? "?"
            return ChatSend(ok: true, detail: "Telegram accepted it as message \(id)")
        }
        let why = (json?["description"] as? String).map { redactTokens($0) } ?? "no description"
        return ChatSend(ok: false, detail: "Telegram answered HTTP \(status): \(why)")
    }
}

// MARK: - Reconnect

struct ReconnectPlan {
    let bot: Bot
    let state: BotState
    let targetPID: String     // the live session pid, or 999999 when the session is down
    let resumeArg: String     // a session id, fresh-keep, or the fresh-start word
    let resumeNote: String
    let engine: String

    /// The /fresh hook's exact shape: pid, workdir, resume id. No "quiet", so the
    /// engine itself sends "Back and ready" once the new poller is confirmed.
    var arguments: [String] { [engine, targetPID, bot.workdir, resumeArg] }
    var commandLine: String { (["/bin/bash"] + arguments).map(shellQuote).joined(separator: " ") }
    var keepsConversation: Bool { isSessionUUID(resumeArg) }
}

enum AutomationStatus: Equatable {
    case granted, wouldPrompt, denied, terminalNotRunning
    case unknown(Int32)

    var text: String {
        switch self {
        case .granted: return "granted"
        case .wouldPrompt: return "not granted yet (the first real Reconnect asks once)"
        case .denied: return "denied (System Settings > Privacy & Security > Automation)"
        case .terminalNotRunning: return "unconfirmed: Terminal is not running, so macOS cannot answer (open Terminal, then try again)"
        case .unknown(let code): return "unconfirmed (macOS answered \(code))"
        }
    }
}

enum ReconnectOutcome {
    case healedBeforeRun
    case changedBeforeRun(String)
    case dryRun(ReconnectPlan)
    case noEngine(String)
    case permissionBlocked(AutomationStatus)
    case engineRefused(Int32, String)
    case skipped(String)
    case engineTimedOut(Int)
    case connected
    case stillDown(BotState)

    /// How an action that did not work ended, as the tail of a sentence that starts
    /// with the action ("Reconnect failed: ..."); nil for an outcome that is not a failure.
    var failureText: String? {
        switch self {
        case .healedBeforeRun, .dryRun, .connected: return nil
        case .skipped(let why): return "skipped: \(why); this request was not applied"
        case .changedBeforeRun(let why): return "did not run: \(why)"
        case .noEngine(let note): return "did not run: \(note)"
        case .permissionBlocked(let status): return "did not run: Terminal automation is \(status.text)"
        case .engineRefused(let code, let meaning):
            if code == -2 { return "did not run: \(meaning)" }
            if code == -1 { return "failed: \(meaning)" }
            return "failed: engine exit \(code), \(meaning)"
        case .engineTimedOut(let seconds):
            return "failed: the engine ran past \(seconds < 120 ? "\(seconds)s" : "\(seconds / 60)m") and was stopped"
        case .stillDown(let state): return "failed: still \(state.rawValue.lowercased()) 45s after the engine finished"
        }
    }
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

    /// Overall engine deadlines; TELEBOTS_BAR_ENGINE_DEADLINE (seconds) overrides
    /// them, for tests.
    static func engineDeadline(_ normal: TimeInterval) -> TimeInterval {
        if let raw = Env.vars["TELEBOTS_BAR_ENGINE_DEADLINE"], let n = Double(raw), n > 0 { return n }
        return normal
    }

    /// The resume id the restart passes, so a reconnect keeps the conversation. Only a
    /// real session id (a UUID) is ever passed; anything else starts a new session on
    /// the same model.
    static func resolveResume(_ bot: Bot, source: ResumeSource) -> (arg: String, note: String) {
        switch source {
        case .sessionID:
            let file = bot.stateDir + "/session.id"
            if isRegularFile(file), let line = firstLine(file) {
                let candidate = line.trimmingCharacters(in: .whitespacesAndNewlines)
                if !candidate.isEmpty {
                    guard isSessionUUID(candidate) else { return ("fresh-keep", "session.id does not hold a session id") }
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
                .filter { $0.hasSuffix(".jsonl") && isSessionUUID(String($0.dropLast(".jsonl".count)))
                    && isRegularFile(bot.projectDir + "/" + $0) }
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

    /// A plan runs only when every argument is what it claims to be: the engines put
    /// them into shell and AppleScript text.
    static func planProblem(_ plan: ReconnectPlan) -> String? {
        guard let pid = Int32(plan.targetPID), pid > 0 else { return "the target pid '\(plan.targetPID)' is not a pid" }
        if !(isSessionUUID(plan.resumeArg) || isEngineWord(plan.resumeArg)) {
            return "the resume argument is neither a session id nor a plain word like fresh-keep"
        }
        if !plan.bot.workdir.hasPrefix("/") || plan.bot.workdir.contains("\"") || plan.bot.workdir.contains("\\") {
            return "the workdir \(plan.bot.workdir) is not a plain absolute path"
        }
        return nil
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
        env.removeValue(forKey: "TZ")
        return env
    }

    /// Optional evidence from an engine that writes its own log. Only the block
    /// naming this exact pid and workdir is considered. This records the engine's
    /// report, not a Telegram delivery receipt.
    static func engineReadyEvidence(path: String, offset: UInt64, plan: ReconnectPlan) -> String? {
        guard let handle = FileHandle(forReadingAtPath: path) else { return nil }
        defer { try? handle.close() }
        guard (try? handle.seek(toOffset: offset)) != nil,
              let data = try? handle.read(upToCount: 131_072), let text = String(data: data, encoding: .utf8) else { return nil }
        let marker = " restart pid=\(plan.targetPID) cwd=\(plan.bot.workdir) ==="
        var ours = false
        var reported: String?
        for line in text.split(separator: "\n").map(String.init) {
            if line.hasPrefix("=== "), line.contains("restart pid=") { ours = line.hasSuffix(marker); reported = nil }
            if ours, line.contains("back-online ping sent") { reported = line.contains("Continue button attached") ? " with Continue button" : "" }
            if ours, line == "=== done (exit=0) ===", let reported {
                return "engine reports \(BotChat.backAndReady) sent\(reported) (engine log; no Bot API receipt retained)"
            }
        }
        return nil
    }

    static func describe(_ health: BotHealth) -> String {
        "\(health.bot.key) (\(health.state.rawValue), session \(health.sessionPIDs.first.map { String($0) } ?? "none"), poller \(health.pollerPIDs.first.map { String($0) } ?? "none"))"
    }

    /// One reconnect or fresh start, start to finish, the way /fresh does it: the
    /// bot's first line in its chat, the engine (which sends "Back and ready" and
    /// owns old-window cleanup). Unproven or already-orphaned windows stay open.
    /// Runs off the main thread. Never restarts a connected bot unless the caller
    /// confirmed it, and re-checks everything right before the engine runs.
    static func performReconnect(_ bot: Bot, _ config: FleetConfig, allowConnected: Bool, dryRun: Bool,
                                 askForPermission: Bool, freshStart: Bool = false, expected: BotHealth? = nil,
                                 log: (String) -> Void) -> ReconnectOutcome {
        let action = freshStart ? "fresh start" : "reconnect"
        let spoken = freshStart ? "Fresh start" : "Reconnect"
        let table0 = ProcessTable.snapshot()
        let health0 = evaluateOne(bot, config, table: table0)
        if let expected, (expected.sessionPIDs != health0.sessionPIDs || expected.sessionStarted != health0.sessionStarted) {
            return .changedBeforeRun("the PID/start identity changed after confirmation or while queued")
        }
        if health0.state == .connected && !allowConnected && !dryRun {
            log("\(action) \(bot.key): connected again before the restart ran, nothing to do")
            return .healedBeforeRun
        }
        let resolved = resolveFreshEngine(config)
        guard let engine = resolved.path else {
            log("\(action) \(bot.key): cannot run, \(resolved.note)")
            return .noEngine(resolved.note)
        }
        let override = freshStart ? config.settings.freshStartArg : nil
        let plan0 = makePlan(health0, config, engine: engine, resumeOverride: override)
        if let problem = planProblem(plan0) {
            log("\(action) \(bot.key): refused before anything ran, \(problem)")
            return .engineRefused(-2, problem)
        }

        if dryRun {
            if freshStart {
                log("[DRY-RUN] fresh start \(bot.key): the panel would first ask \"Start \(bot.name) fresh?\" (Cancel is the default)")
            } else if health0.state == .connected && !allowConnected {
                log("[DRY-RUN] reconnect \(bot.key): connected, so the panel would first ask \"Restart \(bot.name) even though it is connected? It keeps the conversation.\" (Cancel is the default)")
            }
            log("[DRY-RUN] \(action) \(describe(health0)): \(bot.name) would first say in its chat \"\(BotChat.line(spoken))\" (\(BotChat.readiness(bot)))")
            log("[DRY-RUN] \(action) \(bot.key): would run: \(plan0.commandLine)")
            log("[DRY-RUN]   resume \(plan0.resumeArg.isEmpty ? "\"\" (empty)" : plan0.resumeArg) (from \(plan0.resumeNote)); PATH=\(FleetPaths.enginePATH)")
            log("[DRY-RUN]   the engine then sends \"\(BotChat.backAndReady)\" from \(bot.name) once the new poller is up")
            log("[DRY-RUN]   old-window cleanup belongs only to the engine; unproven windows stay open")
            return .dryRun(plan0)
        }

        // Terminal permission first; an answer macOS cannot confirm is not a go.
        var automation = terminalAutomation(askUser: false)
        if automation == .wouldPrompt && askForPermission {
            log("\(action) \(bot.key): asking macOS for permission to open the bot's Terminal window")
            automation = terminalAutomation(askUser: true)
        }
        guard automation == .granted else {
            log("\(action) \(bot.key): stopped before anything was touched, Terminal automation is \(automation.text)")
            return .permissionBlocked(automation)
        }

        // Everything again, right before the engine runs: a permission prompt can take
        // a while, and the bot or its session may have changed meanwhile.
        let table1 = ProcessTable.snapshot()
        let health1 = evaluateOne(bot, config, table: table1)
        if health1.state == .connected && !allowConnected {
            log("\(action) \(bot.key): connected again before the restart ran (\(describe(health1))), nothing was run")
            return .healedBeforeRun
        }
        let pidBefore = health0.sessionPIDs.first, pidNow = health1.sessionPIDs.first
        var change: String?
        if pidBefore != pidNow {
            change = "its session changed from \(pidBefore.map { String($0) } ?? "none") to \(pidNow.map { String($0) } ?? "none") while waiting"
        } else if let pid = pidNow {
            if health0.sessionStarted == nil || health1.sessionStarted == nil {
                change = "the session start time cannot be verified"
            } else if let s0 = health0.sessionStarted, let s1 = health1.sessionStarted, abs(s0 - s1) > 0.01 {
                change = "pid \(pid) now belongs to a different process"
            } else if !(table1.argv(of: pid)?.joined(separator: " ").contains(ProcessTable.sessionSignature) ?? false) {
                change = "pid \(pid) no longer carries the session signature"
            } else if let cwd = processCWD(pid), cwd != bot.workdir {
                change = "its session \(pid) runs in \(cwd), not \(bot.workdir)"
            } else if processCWD(pid) == nil {
                change = "the working directory of session \(pid) cannot be read"
            }
        }
        if let change {
            log("\(action) \(bot.key): stopped, \(change); nothing was run")
            return .changedBeforeRun(change)
        }
        let plan = makePlan(health1, config, engine: engine, resumeOverride: override)
        if let problem = planProblem(plan) {
            log("\(action) \(bot.key): refused before anything ran, \(problem)")
            return .engineRefused(-2, problem)
        }

        // Like /fresh: the bot first says, in its own chat, that a restart is coming.
        let first = BotChat.send(bot, BotChat.line(spoken))
        log("\(action) \(bot.key): " + (first.ok
            ? "said in its chat \"\(BotChat.line(spoken))\" (\(first.detail))"
            : "chat line not sent: \(first.detail)"))

        // The network request can outlast recovery. Recheck health and the confirmed
        // identity after it, then bind this admission to the engine's locked check.
        let health2 = evaluateOne(bot, config)
        if health2.state == .connected && !allowConnected {
            log("\(action) \(bot.key): connected during the chat request; restart skipped")
            return .healedBeforeRun
        }
        guard health2.sessionPIDs == health1.sessionPIDs,
              health2.sessionStarted == health1.sessionStarted else {
            return .changedBeforeRun("the session identity changed during the chat request")
        }
        if !health2.sessionPIDs.isEmpty && health2.sessionStarted == nil {
            return .changedBeforeRun("the session start time cannot be verified")
        }
        var admission = engineEnvironment()
        admission["CC_FRESH_EXPECT_PID"] = plan.targetPID
        admission["CC_FRESH_EXPECT_START"] = health2.sessionStarted.map { String(Int($0)) } ?? "absent"
        admission["CC_FRESH_ALLOW_CONNECTED"] = allowConnected ? "1" : "0"
        admission["CC_FRESH_HEARTBEAT_MAX_AGE"] = String(config.settings.heartbeatMaxAge)
        log("\(action) \(describe(health1)): running \(plan.commandLine) (resume from \(plan.resumeNote))")
        let deadline = engineDeadline(300)
        let engineLogOffset: UInt64? = config.settings.freshEngineLog.flatMap { path in
            guard let handle = FileHandle(forReadingAtPath: path) else { return 0 }
            defer { try? handle.close() }
            return try? handle.seekToEnd()
        }
        let result: Subprocess.Result
        do {
            result = try Subprocess.run("/bin/bash", plan.arguments, environment: admission, capture: false, processGroup: true,
                                        deadline: deadline, started: { log("\(action) \(bot.key): engine started as pid \($0)") })
        } catch {
            log("\(action) \(bot.key): could not start /bin/bash (\(error.localizedDescription))")
            let failed = BotChat.send(bot, "\(spoken) from the menu bar failed to launch, nothing was restarted")
            log("\(action) \(bot.key): " + (failed.ok ? "said so in its chat (\(failed.detail))" : "failure line not sent: \(failed.detail)"))
            return .engineRefused(-1, "could not start the engine")
        }
        if result.timedOut {
            log("\(action) \(bot.key): the engine ran past \(Int(deadline))s and was stopped (exit \(result.code)); its entire engine process group was stopped; a later engine invocation reclaims a departed owner's lock after checking its PID, start time and process group")
            return .engineTimedOut(Int(deadline))
        }
        let code = result.code
        let meaning = result.signaled ? "engine killed by signal \(code)" : (engineMeanings[code] ?? "engine failed")
        log("\(action) \(bot.key): engine exit \(code), \(meaning)")
        if code == 0, !result.signaled, let path = config.settings.freshEngineLog, let offset = engineLogOffset {
            log("\(action) \(bot.key): " + (engineReadyEvidence(path: path, offset: offset, plan: plan)
                ?? "Back and ready could not be verified in the engine log"))
        }

        if code == 7 && !result.signaled { return .skipped(meaning) }
        guard !result.signaled, [0, 6].contains(code) else { return .engineRefused(code, meaning) }

        // The shipped engine returns as soon as the window opens; wait for the poller.
        let wait = Date().addingTimeInterval(45)
        var after = evaluateOne(bot, config)
        while after.state != .connected && Date() < wait {
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
    var timedOut = false
}

/// What Restore did, bot by bot.
struct RestoreReport {
    var run: EngineRun?
    var wasDown: [String] = []      // bot keys that were down when Restore started
    var back: [String] = []         // of those, the ones that came back
    var notBack: [String] = []      // and the ones that did not
    var blocked: AutomationStatus?
}

extension FleetModel {
    static func enginePath(_ action: FleetEngineAction, _ config: FleetConfig) -> String {
        action == .snapshot ? config.settings.snapshotEngine : config.settings.restoreEngine
    }

    /// The engine and its arguments, for /bin/bash. Snapshot adds --keep-watchdog
    /// unless menubar.snapshot_keep_watchdog is false, so a snapshot from the panel,
    /// the phone or the command line never switches auto-recovery off.
    static func engineArguments(_ action: FleetEngineAction, _ config: FleetConfig) -> [String] {
        let path = enginePath(action, config)
        if action == .snapshot && config.settings.snapshotKeepWatchdog { return [path, "--keep-watchdog"] }
        return [path]
    }

    static func engineCommandLine(_ action: FleetEngineAction, _ config: FleetConfig) -> String {
        (["/bin/bash"] + engineArguments(action, config)).map(shellQuote).joined(separator: " ")
    }

    /// What a real run does, in one line, for the dry-run log.
    static func engineEffect(_ action: FleetEngineAction, _ config: FleetConfig) -> String {
        switch action {
        case .snapshot:
            if config.settings.snapshotKeepWatchdog {
                return "records every bot's current session in \(config.settings.snapshotFile) and keeps your auto-recovery settings as they are (--keep-watchdog: bots already off or paused stay that way); it kills and launches nothing"
            }
            return "records every bot's current session in \(config.settings.snapshotFile) and switches auto-recovery off (a <key>.disabled file for each bot not already off) until Restore switches it on again, because menubar.snapshot_keep_watchdog is false; it kills and launches nothing"
        case .restore:
            return "reopens every bot that is not running, resumed into its saved session in a new Terminal window, also restarts a running session whose poller is missing; it switches on again only the watchdog switches a snapshot itself switched off (a --keep-watchdog snapshot switches none off), and archives the snapshot once every bot it reopened is back"
        }
    }

    /// Runs the snapshot or restore engine with a deadline and a bounded capture of its
    /// output, or in dry run logs exactly what it would run.
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
        let deadline = engineDeadline(action == .snapshot ? 120 : 300)
        let result: Subprocess.Result
        do {
            result = try Subprocess.run("/bin/bash", engineArguments(action, config), environment: engineEnvironment(),
                                        capture: true, processGroup: true, deadline: deadline)
        } catch {
            log("\(action.rawValue): could not start /bin/bash (\(error.localizedDescription))")
            return nil
        }
        let lines = result.output.split(separator: "\n").map { redactTokens(String($0)).trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let summary = lines.last(where: { !$0.hasPrefix("  ") }) ?? "no output"
        if result.timedOut {
            log("\(action.rawValue): the engine ran past \(Int(deadline))s and was stopped; last line: \(summary)")
            return EngineRun(code: result.code == 0 ? -1 : result.code, summary: "ran past \(Int(deadline))s and was stopped", timedOut: true)
        }
        log("\(action.rawValue): engine exit \(result.code): \(summary)")
        return EngineRun(code: result.code, summary: summary)
    }

    /// Restore, the way /fresh speaks: every bot that is down says first that a restore
    /// is coming, the restore engine runs, then each bot that comes back says "Back and
    /// ready" and its old window is closed, when it is provably that window.
    static func performRestore(_ config: FleetConfig, dryRun: Bool, askForPermission: Bool,
                               log: (String) -> Void) -> RestoreReport {
        let table0 = ProcessTable.snapshot()
        let now0 = Int(Date().timeIntervalSince1970)
        let trust0 = heartbeatTrust(config, now: now0, probe: nil)
        let down = config.bots.filter {
            let state = evaluate($0, table: table0, now: now0, maxAge: config.settings.heartbeatMaxAge, trust: trust0).state
            return state == .down || state == .noPoller
        }
        var report = RestoreReport(wasDown: down.map(\.key))
        if dryRun {
            report.run = runFleetEngine(.restore, config, dryRun: true, log: log)
            if down.isEmpty {
                log("[DRY-RUN]   no bot is down or missing its poller right now, so a real run would reopen nothing and send nothing")
            }
            for bot in down {
                log("[DRY-RUN]   \(bot.name) is down: it would first say \"\(BotChat.line("Restore"))\" (\(BotChat.readiness(bot))), then \"\(BotChat.backAndReady)\" once its poller is up")

            }
            return report
        }
        guard isRegularFile(config.settings.snapshotFile) else {
            log("restore: no snapshot at \(config.settings.snapshotFile), nothing to restore")
            return report
        }
        if !down.isEmpty {
            var automation = terminalAutomation(askUser: false)
            if automation == .wouldPrompt && askForPermission {
                log("restore: asking macOS for permission to open the bots' Terminal windows")
                automation = terminalAutomation(askUser: true)
            }
            guard automation == .granted else {
                log("restore: stopped before anything was touched, Terminal automation is \(automation.text)")
                report.blocked = automation
                return report
            }
        }
        for bot in down {
            let sent = BotChat.send(bot, BotChat.line("Restore"))
            log("restore \(bot.key): " + (sent.ok
                ? "said in its chat \"\(BotChat.line("Restore"))\" (\(sent.detail))"
                : "chat line not sent: \(sent.detail)"))
        }
        report.run = runFleetEngine(.restore, config, dryRun: false, log: log)
        guard let run = report.run, run.code == 0 else {
            report.notBack = report.wasDown
            return report
        }
        var pending = down
        let wait = Date().addingTimeInterval(60)
        while !pending.isEmpty && Date() < wait {
            Thread.sleep(forTimeInterval: 2)
            let table = ProcessTable.snapshot()
            let now = Int(Date().timeIntervalSince1970)
            let trust = heartbeatTrust(config, now: now, probe: nil)
            var still: [Bot] = []
            for bot in pending {
                let health = evaluate(bot, table: table, now: now, maxAge: config.settings.heartbeatMaxAge, trust: trust)
                guard health.state == .connected else {
                    still.append(bot)
                    continue
                }
                report.back.append(bot.key)
                let sent = BotChat.send(bot, BotChat.backAndReady)
                log("restore \(bot.key): connected (\(describe(health))); " + (sent.ok
                    ? "said \"\(BotChat.backAndReady)\" (\(sent.detail))"
                    : "back-online line not sent: \(sent.detail)"))

            }
            pending = still
        }
        report.notBack = pending.map(\.key)
        for bot in pending {
            log("restore \(bot.key): still not connected 60s after the restore engine finished")
        }
        return report
    }
}

// MARK: - Watchdog kill switches

/// The watchdog's own crash-loop breaker for one bot: <key>.disabled-auto. The live
/// watchdog writes it after too many restarts in an hour (its first line the epoch,
/// its second when and why), skips that bot until the file is removed, and reminds
/// every 6 hours meanwhile. It is not the manual switch, and this app never removes it.
struct AutoBreaker: Equatable {
    let bot: Bot
    let path: String
    let since: TimeInterval?     // the first line as an epoch, else (a marker made by hand) its mtime
    let reason: String?          // "5 restarts in the last hour (limit 4)", from line 2
}

struct WatchdogSwitches {
    let dir: String
    let dirExists: Bool
    let disabled: [Bot]      // bots whose <key>.disabled is a regular file, the watchdog's own -f test
    let odd: [String]        // marker paths that exist but are not regular files; the watchdog ignores them
    let total: Int
    var autoPaused: [AutoBreaker] = []   // bots whose <key>.disabled-auto is a regular file

    /// With no state folder nothing can say whether auto-recovery runs, so the switch
    /// is unavailable rather than on.
    var available: Bool { dirExists }
    /// The manual switch alone: the crash-loop breaker is not a switch the app flips.
    var isOn: Bool { available && disabled.isEmpty }
    var isFullyOff: Bool { available && total > 0 && disabled.count == total }
    /// Bots the breaker paused that the manual switch has not already turned off.
    var pausedOnly: [AutoBreaker] { autoPaused.filter { breaker in !disabled.contains { $0.key == breaker.bot.key } } }

    var label: String {
        if !available { return "Watchdog: unavailable (no state folder)" }
        let paused = pausedOnly.isEmpty ? "" : ", auto-recovery paused for " + pausedOnly.map(\.bot.name).joined(separator: ", ")
        if isOn { return "Watchdog: on" + paused }
        if isFullyOff { return "Watchdog: off" }
        return "Watchdog: off for " + disabled.map(\.name).joined(separator: ", ") + paused
    }
}

extension FleetModel {
    /// Whether anything at all sits at a path, without following a symlink.
    static func somethingAt(_ path: String) -> Bool {
        var st = stat()
        return lstat(path, &st) == 0
    }

    static func readWatchdog(_ config: FleetConfig) -> WatchdogSwitches {
        let dir = config.settings.watchdogStateDir
        let exists = isDirectory(dir)
        var disabled: [Bot] = []
        var odd: [String] = []
        var paused: [AutoBreaker] = []
        if exists {
            for bot in config.bots {
                let file = dir + "/" + bot.key + ".disabled"
                if isRegularFile(file) {
                    disabled.append(bot)
                } else if somethingAt(file) {
                    odd.append(file)
                }
                let auto = dir + "/" + bot.key + ".disabled-auto"
                if isRegularFile(auto) {
                    paused.append(readAutoBreaker(bot, path: auto))
                } else if somethingAt(auto) {
                    odd.append(auto)
                }
            }
        }
        return WatchdogSwitches(dir: dir, dirExists: exists, disabled: disabled, odd: odd, total: config.bots.count,
                                autoPaused: paused)
    }

    /// The breaker's own record, read the way the watchdog reads it: line 1 the epoch
    /// it tripped (a marker made by hand has none, so its mtime), line 2 when and why.
    static func readAutoBreaker(_ bot: Bot, path: String) -> AutoBreaker {
        var since: TimeInterval?
        var reason: String?
        if let handle = FileHandle(forReadingAtPath: path), let data = try? handle.read(upToCount: 1024) {
            try? handle.close()
            let lines = String(decoding: data, as: UTF8.self)
                .split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
            if let first = lines.first?.trimmingCharacters(in: .whitespaces), !first.isEmpty,
               first.allSatisfy({ $0 >= "0" && $0 <= "9" }), let epoch = Int(first), epoch > 1_000_000_000 {
                since = TimeInterval(epoch)
            }
            if lines.count > 1 {
                // "2026-09-29 13:05:00 crash-loop breaker: 5 restarts in the last hour (limit 4)"
                // reads "5 restarts in the last hour (limit 4)": its local date stamp and
                // label dropped, control characters removed, at most 160 characters.
                var text = lines[1].trimmingCharacters(in: .whitespaces)
                if let range = text.range(of: "crash-loop breaker:") {
                    text = String(text[range.upperBound...]).trimmingCharacters(in: .whitespaces)
                }
                text = String(String.UnicodeScalarView(text.unicodeScalars.filter { $0.value >= 32 && $0.value != 127 }).prefix(160))
                if !text.isEmpty { reason = text }
            }
        }
        return AutoBreaker(bot: bot, path: path, since: since ?? modificationTime(path), reason: reason)
    }

    /// Off: create <key>.disabled for every bot that lacks one (what the snapshot
    /// engine touches). On: remove every bot's <key>.disabled (what restore removes).
    /// Only regular files are ever created or removed, one unlink at a time; a
    /// directory or anything else at a marker path is left alone and reported.
    /// Nothing else is touched, not even the crash-loop breaker's own markers.
    /// Returns the problems, empty when every change went through.
    @discardableResult
    static func applyWatchdog(on: Bool, _ config: FleetConfig, dryRun: Bool, log: (String) -> Void) -> [String] {
        let dir = config.settings.watchdogStateDir
        guard isDirectory(dir) else {
            let problem = "no watchdog state folder at \(dir), so auto-recovery cannot be switched (is the watchdog installed?)"
            log("watchdog: \(problem); nothing changed")
            return [problem]
        }
        let prefix = dryRun ? "[DRY-RUN] " : ""
        var changed = 0
        var problems: [String] = []
        for bot in config.bots {
            let file = dir + "/" + bot.key + ".disabled"
            let regular = isRegularFile(file)
            let present = somethingAt(file)
            if on {
                if regular {
                    if dryRun {
                        log("\(prefix)watchdog on: would remove \(file)")
                    } else if unlink(file) == 0 {
                        log("watchdog on: removed \(file)")
                    } else {
                        let problem = "could not remove \(file) (\(String(cString: strerror(errno))))"
                        log("watchdog on: \(problem)")
                        problems.append(problem)
                    }
                    changed += 1
                } else if present {
                    log("\(prefix)watchdog on: left \(file) alone, it is not a regular file (the watchdog ignores it too)")
                }
            } else if !regular {
                if present {
                    let problem = "\(file) exists but is not a regular file, so \(bot.name) could not be switched off"
                    log("\(prefix)watchdog off: \(problem)")
                    problems.append(problem)
                } else if dryRun {
                    log("\(prefix)watchdog off: would create \(file)")
                    changed += 1
                } else {
                    let fd = open(file, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o644)
                    if fd >= 0 {
                        close(fd)
                        log("watchdog off: created \(file)")
                    } else {
                        let problem = "could not create \(file) (\(String(cString: strerror(errno))))"
                        log("watchdog off: \(problem)")
                        problems.append(problem)
                    }
                    changed += 1
                }
            }
        }
        let verb = on ? "on" : "off"
        log("\(prefix)watchdog \(verb): \(changed) file(s) \(dryRun ? "would change" : "changed") in \(dir), \(config.bots.count - changed) already \(on ? "absent" : "present")"
            + (problems.isEmpty ? "" : ", \(problems.count) problem(s)"))
        return problems
    }
}

// MARK: - Action log

/// Appends one line per action to $FLEET_HOME/menubar.log, stamped in the
/// configured time zone (the Mac's zone unless menubar.time_zone says otherwise).
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
    private let phoneFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "EEEE, MMMM d, yyyy h:mm:ssa 'ET'"
        f.amSymbol = "am"
        f.pmSymbol = "pm"
        f.timeZone = FleetModel.defaultTimeZone
        return f
    }()

    func setTimeZone(_ zone: TimeZone) {
        queue.sync { formatter.timeZone = zone }
    }

    func writePhone(_ message: String) { write(message, eastern: true) }

    func write(_ message: String, eastern: Bool = false) {
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
            let line = (eastern ? phoneFormatter : formatter).string(from: Date()) + " " + clean + "\n"
            let fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
            guard fd >= 0 else { return }
            line.utf8CString.withUnsafeBufferPointer { buf in
                _ = Darwin.write(fd, buf.baseAddress, buf.count - 1)
            }
            close(fd)
        }
    }
}
