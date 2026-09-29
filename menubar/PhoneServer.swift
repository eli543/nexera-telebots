// PhoneServer.swift - a small, owner-only HTTP companion on the Tailscale address.
// No network request reaches an app action until both identity and token pass.

import Foundation
import Darwin
import Security
import CoreFoundation

struct PhoneResponse {
    let status: Int
    let body: Data
    var contentType: String = "application/json; charset=utf-8"

    static func json(_ status: Int, _ object: [String: Any]) -> PhoneResponse {
        PhoneResponse(status: status, body: (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{}".utf8))
    }
}

struct PhoneTailnetIdentity: Equatable {
    let address: String
    let dnsName: String?
    let userID: String
    let loginName: String

    var baseURL: String { "http://\(dnsName ?? address):8477/" }
    var cacheIdentity: String { userID + ":" + loginName }
    var authorities: Set<String> {
        var result: Set<String> = [address + ":8477"]
        if let dnsName { result.insert(dnsName.lowercased() + ":8477") }
        return result
    }
}

enum PhoneSecurity {
    static func isTailnetIPv4(_ value: String) -> Bool {
        let parts = value.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return false }
        var numbers: [Int] = []
        for part in parts {
            guard !part.isEmpty, part.count <= 3, part.allSatisfy({ $0 >= "0" && $0 <= "9" }),
                  !(part.count > 1 && part.first == "0"), let n = Int(part), n <= 255 else { return false }
            numbers.append(n)
        }
        return numbers[0] == 100 && (64...127).contains(numbers[1])
    }

    static func isToken(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    /// Both valid tokens always take the same 64-byte comparison path.
    static func tokenMatches(_ candidate: String?, expected: String) -> Bool {
        guard let candidate, isToken(candidate), isToken(expected) else { return false }
        let left = Array(candidate.utf8), right = Array(expected.utf8)
        var difference: UInt8 = 0
        for i in 0..<64 { difference |= left[i] ^ right[i] }
        return difference == 0
    }

    static func bearerToken(_ header: String?) -> String? {
        guard let header, header.utf8.count == 71, header.prefix(7).lowercased() == "bearer " else { return nil }
        let token = String(header.dropFirst(7))
        return isToken(token) ? token : nil
    }

    static func ownerOnlyResource(method: String, path: String) -> Bool {
        method == "GET" && ["/", "/manifest.webmanifest", "/icon-180.png", "/icon-512.png", "/apple-touch-icon.png"].contains(path)
    }

    /// Shared by the live handler and the offline authorization matrix.
    static func accessStatus(method: String, path: String, ownerVerified: Bool,
                             authorization: String?, expectedToken: String) -> Int {
        guard ownerVerified else { return 403 }
        if ownerOnlyResource(method: method, path: path) { return 200 }
        return tokenMatches(bearerToken(authorization), expected: expectedToken) ? 200 : 401
    }

    private static func identifier(_ value: Any?) -> String? {
        if let number = value as? NSNumber {
            guard CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
            let text = number.stringValue
            guard !text.isEmpty, text.allSatisfy({ $0 >= "0" && $0 <= "9" }), text != "0" else { return nil }
            return text
        }
        if let text = value as? String, !text.isEmpty, text.count <= 20, text.first != "0",
           text.allSatisfy({ $0 >= "0" && $0 <= "9" }) { return text }
        return nil
    }

    private static func dnsName(_ value: Any?) -> String? {
        guard var text = value as? String else { return nil }
        if text.hasSuffix(".") { text.removeLast() }
        guard !text.isEmpty, text.count <= 253 else { return nil }
        let labels = text.split(separator: ".", omittingEmptySubsequences: false)
        guard labels.count >= 2, labels.allSatisfy({ label in
            !label.isEmpty && label.count <= 63 && label.first != "-" && label.last != "-"
                && label.utf8.allSatisfy { (97...122).contains($0) || (65...90).contains($0) || (48...57).contains($0) || $0 == 45 }
        }) else { return nil }
        return text.lowercased()
    }

    private static func hasNoTags(_ value: Any?) -> Bool {
        guard let value, !(value is NSNull) else { return true }
        guard let tags = value as? [String] else { return false }
        return tags.isEmpty
    }

    static func identity(status: Data, address: String) -> PhoneTailnetIdentity? {
        guard isTailnetIPv4(address),
              let root = (try? JSONSerialization.jsonObject(with: status)) as? [String: Any],
              root["BackendState"] as? String == "Running", root["HaveNodeKey"] as? Bool == true,
              let own = root["Self"] as? [String: Any],
              hasNoTags(own["Tags"]),
              (own["TailscaleIPs"] as? [String] ?? []).contains(address),
              let userID = identifier(own["UserID"]),
              let users = root["User"] as? [String: Any], let user = users[userID] as? [String: Any],
              identifier(user["ID"]) == userID,
              let loginName = user["LoginName"] as? String, !loginName.isEmpty, loginName.count <= 320,
              loginName == loginName.trimmingCharacters(in: .whitespacesAndNewlines),
              !loginName.lowercased().hasPrefix("tagged-devices") else { return nil }
        let currentTailnet = root["CurrentTailnet"] as? [String: Any]
        let dns = currentTailnet?["MagicDNSEnabled"] as? Bool == true ? dnsName(own["DNSName"]) : nil
        return PhoneTailnetIdentity(address: address, dnsName: dns, userID: userID, loginName: loginName)
    }

    /// Check both references to the owner, and the actual address from the socket.
    /// A nil response represents a failed or timed-out CLI call and always denies.
    static func sameOwner(source: String, whois: Data?, identity: PhoneTailnetIdentity) -> Bool {
        guard isTailnetIPv4(source), let whois,
              let root = (try? JSONSerialization.jsonObject(with: whois)) as? [String: Any],
              let node = root["Node"] as? [String: Any], let user = root["UserProfile"] as? [String: Any],
              hasNoTags(node["Tags"]),
              // Peer results omit this field; only the local node includes it.
              node["MachineAuthorized"] as? Bool != false,
              node["Expired"] as? Bool != true,
              identifier(node["User"]) == identity.userID,
              identifier(user["ID"]) == identity.userID,
              user["LoginName"] as? String == identity.loginName,
              (node["Addresses"] as? [String] ?? []).contains(source + "/32") else { return false }
        return true
    }

    static func sameOrigin(headers: [String: String], identity: PhoneTailnetIdentity, navigation: Bool = false) -> Bool {
        guard let host = headers["host"]?.lowercased(), identity.authorities.contains(host) else { return false }
        if let origin = headers["origin"] {
            guard origin == "http://" + host else { return false }
        }
        if let site = headers["sec-fetch-site"], site != "same-origin" && site != "none" {
            // Following the phone link from another website is a document
            // navigation, not an API request. The handler grants this exception
            // only to GET /. Resource authorization is checked separately.
            guard navigation, site == "cross-site" || site == "same-site", headers["origin"] == nil,
                  headers["sec-fetch-mode"] == "navigate", headers["sec-fetch-dest"] == "document" else { return false }
        }
        return true
    }
}

enum PhoneTailscale {
    static let executable = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

    static func environment(_ original: [String: String] = Env.vars) -> [String: String] {
        var result = original
        // The Mac App Store binary selects GUI mode when TERM is absent. launchd
        // has no terminal, so explicitly select plain CLI output in this child.
        result["TERM"] = "dumb"
        return result
    }

    static func run(_ arguments: [String], limit: Int) throws -> Subprocess.Result {
        try Subprocess.run(executable, arguments, environment: environment(), limit: limit, deadline: 4)
    }

    /// `tailscale whois --json <address>`: its output, or nil when the command did not
    /// complete (could not start, timed out, was interrupted or exited non-zero).
    static func whois(_ source: String) -> Data? {
        guard let result = try? run(["whois", "--json", source], limit: 65_536),
              result.code == 0, !result.signaled, !result.timedOut else { return nil }
        return Data(result.output.utf8)
    }
}

/// Who may use the phone link, by source address. A read (the page, its manifest and
/// icons, GET /api/state) may reuse a verdict for up to ten minutes. A mutation
/// (POST /api/action) never does: it asks whois again every time, so a device that
/// stopped being the owner's is refused on its very next action. A fresh verdict
/// replaces the cached one only when the whois command completed, so a failed lookup
/// refuses the action without also locking the owner out of the page.
final class PhoneOwnerCheck: @unchecked Sendable {
    typealias Whois = (String) -> Data?
    static let readCacheSeconds: TimeInterval = 600
    private struct Entry { let allowed: Bool; let owner: String; let expires: TimeInterval }
    private let lock = NSLock()
    private let slots = DispatchSemaphore(value: 2)
    private let whois: Whois
    private let clock: () -> TimeInterval
    private var cache: [String: Entry] = [:]

    init(whois: @escaping Whois = PhoneTailscale.whois,
         clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.whois = whois
        self.clock = clock
    }

    /// The verdict, and the moment it was established on `clock`: for a mutation, the
    /// moment whois answered, which starts the window its admission may run in.
    func check(_ source: String, identity: PhoneTailnetIdentity, mutation: Bool) -> (allowed: Bool, at: TimeInterval) {
        guard PhoneSecurity.isTailnetIPv4(source) else { return (false, clock()) }
        if !mutation {
            lock.lock()
            let cached = cache[source]
            lock.unlock()
            if let cached, cached.owner == identity.cacheIdentity, cached.expires > clock() { return (cached.allowed, clock()) }
        }
        guard slots.wait(timeout: .now() + 5) == .success else { return (false, clock()) }
        let output = whois(source)
        slots.signal()
        let allowed = PhoneSecurity.sameOwner(source: source, whois: output, identity: identity)
        let now = clock()
        if !mutation || output != nil {
            lock.lock()
            if cache.count >= 256 { cache = cache.filter { $0.value.expires > now } }
            if cache.count >= 256 { cache.removeAll() }
            cache[source] = Entry(allowed: allowed, owner: identity.cacheIdentity, expires: now + Self.readCacheSeconds)
            lock.unlock()
        }
        return (allowed, now)
    }

    func clear() {
        lock.lock()
        cache.removeAll()
        lock.unlock()
    }
}

/// One phone action admitted by a fresh owner check. It may start once: only within
/// `window` seconds of that check (inside the eight-second reply wait) and only while
/// the phone token and the Mac's identity are the ones that were checked. When the
/// reply wait ends first, it expires, so a request that sat in a queue never runs late.
final class PhoneAdmission: @unchecked Sendable {
    static let window: TimeInterval = 5
    private enum State { case pending, started, expired }
    let verifiedAt: TimeInterval
    private let stillAuthorized: () -> Bool
    private let lock = NSLock()
    private var state = State.pending

    init(verifiedAt: TimeInterval, stillAuthorized: @escaping () -> Bool) {
        self.verifiedAt = verifiedAt
        self.stillAuthorized = stillAuthorized
    }

    /// Called as the action starts. True only for a pending admission inside its window
    /// whose token and identity still hold; anything else expires it for good.
    func start(now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard state == .pending else { return false }
        if now >= verifiedAt, now - verifiedAt <= Self.window, stillAuthorized() {
            state = .started
            return true
        }
        state = .expired
        return false
    }

    /// Called when the reply wait ends: a pending admission can no longer start.
    /// True when the action never started.
    func expire() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if state == .pending { state = .expired }
        return state == .expired
    }
}

enum PhoneTokenError: Error, LocalizedError {
    case unsafeDirectory, unsafeFile, io, random
    var errorDescription: String? {
        switch self {
        case .unsafeDirectory: return "the phone token folder is unsafe"
        case .unsafeFile: return "the phone token must be an owned regular file with mode 0600"
        case .io: return "the phone token could not be saved or read"
        case .random: return "secure random bytes are unavailable"
        }
    }
}

/// File descriptors and openat keep symlinks out of reads and atomic rotation.
/// The caller must already have created its own private fleet directory.
final class PhoneTokenStore {
    let path: String
    init(path: String = FleetPaths.fleetHome + "/phone-token") { self.path = path }

    private func directory() throws -> Int32 {
        let folder = (path as NSString).deletingLastPathComponent
        let fd = open(folder, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { throw PhoneTokenError.unsafeDirectory }
        var st = stat()
        guard fstat(fd, &st) == 0, st.st_uid == getuid(), (st.st_mode & 0o022) == 0 else {
            close(fd)
            throw PhoneTokenError.unsafeDirectory
        }
        return fd
    }

    private var leaf: String { (path as NSString).lastPathComponent }

    private func read(_ directory: Int32) throws -> String? {
        let fd = openat(directory, leaf, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        if fd < 0 {
            if errno == ENOENT { return nil }
            throw PhoneTokenError.unsafeFile
        }
        defer { close(fd) }
        var st = stat()
        guard fstat(fd, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG, st.st_uid == getuid(),
              (st.st_mode & 0o7777) == 0o600, st.st_nlink == 1, (64...65).contains(st.st_size) else {
            throw PhoneTokenError.unsafeFile
        }
        var bytes = [UInt8](repeating: 0, count: 66)
        let count = Darwin.read(fd, &bytes, bytes.count)
        guard count == st.st_size else { throw PhoneTokenError.io }
        if count == 65 && bytes[64] != 10 { throw PhoneTokenError.unsafeFile }
        let token = String(decoding: bytes.prefix(64), as: UTF8.self)
        guard PhoneSecurity.isToken(token) else { throw PhoneTokenError.unsafeFile }
        return token
    }

    private func create(_ directory: Int32, replacing: Bool) throws -> String {
        var random = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, random.count, &random) == errSecSuccess else { throw PhoneTokenError.random }
        let token = random.map { String(format: "%02x", $0) }.joined()
        let temporary = ".phone-token." + UUID().uuidString + ".tmp"
        let fd = openat(directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw PhoneTokenError.io }
        defer { close(fd); _ = unlinkat(directory, temporary, 0) }
        let data = Data((token + "\n").utf8)
        let written = data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        guard written == data.count, fchmod(fd, 0o600) == 0, fsync(fd) == 0 else { throw PhoneTokenError.io }
        if replacing {
            guard renameat(directory, temporary, directory, leaf) == 0 else { throw PhoneTokenError.io }
        } else if linkat(directory, temporary, directory, leaf, 0) != 0 {
            if errno == EEXIST, let existing = try read(directory) { return existing }
            throw PhoneTokenError.io
        }
        _ = unlinkat(directory, temporary, 0)
        _ = fsync(directory)
        return token
    }

    func loadOrCreate() throws -> String {
        let fd = try directory()
        defer { close(fd) }
        if let token = try read(fd) { return token }
        return try create(fd, replacing: false)
    }

    func rotate() throws -> String {
        let fd = try directory()
        defer { close(fd) }
        _ = try read(fd)
        return try create(fd, replacing: true)
    }
}

struct PhoneHTTPRequest {
    let method: String
    let path: String
    let headers: [String: String]
    let contentLength: Int
    var body = Data()
}

enum PhoneHTTPError: Error { case malformed, tooLarge, timedOut }

enum PhoneHTTPParser {
    static let headerLimit = 16_384
    static let bodyLimit = 4096

    static func head(_ data: Data) throws -> PhoneHTTPRequest {
        guard data.count <= headerLimit else { throw PhoneHTTPError.tooLarge }
        guard data.allSatisfy({ $0 == 9 || $0 == 10 || $0 == 13 || (32...126).contains($0) }),
              data.suffix(4) == Data("\r\n\r\n".utf8),
              let text = String(data: data.dropLast(4), encoding: .ascii) else { throw PhoneHTTPError.malformed }
        // CRLF is one Swift Character, so strip the four wire bytes before decoding.
        let lines = text.components(separatedBy: "\r\n")
        guard let start = lines.first, start.utf8.count <= 2048 else { throw PhoneHTTPError.malformed }
        let pieces = start.components(separatedBy: " ")
        guard pieces.count == 3, pieces[2] == "HTTP/1.1", ["GET", "POST"].contains(pieces[0]),
              pieces[1].hasPrefix("/"), !pieces[1].contains("?"), !pieces[1].contains("#"),
              !pieces[1].contains("%"), !pieces[1].contains("\\"),
              pieces[1].utf8.allSatisfy({ (33...126).contains($0) }) else { throw PhoneHTTPError.malformed }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":"), colon != line.startIndex else { throw PhoneHTTPError.malformed }
            let name = String(line[..<colon]).lowercased()
            guard name.utf8.allSatisfy({ (97...122).contains($0) || (48...57).contains($0) || $0 == 45 }),
                  headers[name] == nil else { throw PhoneHTTPError.malformed }
            let value = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
            guard !value.contains("\r"), !value.contains("\n"), !value.contains("\t") else { throw PhoneHTTPError.malformed }
            headers[name] = value
        }
        guard headers["host"] != nil, headers["transfer-encoding"] == nil, headers["content-encoding"] == nil,
              headers["expect"] == nil, headers["upgrade"] == nil else { throw PhoneHTTPError.malformed }
        var length = 0
        if let value = headers["content-length"] {
            guard !value.isEmpty, value.utf8.allSatisfy({ (48...57).contains($0) }), let n = Int(value) else { throw PhoneHTTPError.malformed }
            guard n <= bodyLimit else { throw PhoneHTTPError.tooLarge }
            length = n
        }
        guard pieces[0] != "GET" || length == 0 else { throw PhoneHTTPError.malformed }
        if pieces[0] == "POST" {
            guard length > 0, headers["content-length"] != nil,
                  let contentType = headers["content-type"]?.lowercased(),
                  contentType == "application/json" || contentType == "application/json; charset=utf-8" else { throw PhoneHTTPError.malformed }
        }
        return PhoneHTTPRequest(method: pieces[0], path: pieces[1], headers: headers, contentLength: length)
    }
}

final class PhoneServer {
    private let page: Data
    private let icons: [Int: Data]
    private let state: (@escaping (Data) -> Void) -> Void
    private let action: (Data, PhoneAdmission, @escaping (PhoneResponse) -> Void) -> Void
    private let status: (String, String?) -> Void
    private let queue = DispatchQueue(label: "com.nexera.telebots.phone")
    private let workers = DispatchQueue(label: "com.nexera.telebots.phone.requests", attributes: .concurrent)
    private let securityLock = NSLock()
    private let owners = PhoneOwnerCheck()
    private let tokenStore = PhoneTokenStore()
    private var token: String?
    private var identity: PhoneTailnetIdentity?
    private var listener: DispatchSourceRead?
    private var listenAddress: String?
    private var connections: [Int32: UUID] = [:]
    private var timer: DispatchSourceTimer?
    private var running = false
    private var lastStatus = ""

    init(page: Data, icons: [Int: Data], state: @escaping (@escaping (Data) -> Void) -> Void,
         action: @escaping (Data, PhoneAdmission, @escaping (PhoneResponse) -> Void) -> Void,
         status: @escaping (String, String?) -> Void) {
        self.page = page; self.icons = icons; self.state = state; self.action = action; self.status = status
    }

    func start() {
        queue.async { [weak self] in
            guard let self, !self.running else { return }
            self.running = true
            let timer = DispatchSource.makeTimerSource(queue: self.queue)
            timer.schedule(deadline: .now(), repeating: 30, leeway: .seconds(1))
            timer.setEventHandler { [weak self] in self?.refreshTailnet() }
            self.timer = timer
            timer.resume()
        }
    }

    func stop() {
        // Keep the server alive until all descriptors have been shut down, even
        // when the owning store releases its reference immediately after stop.
        queue.async { [self] in
            self.running = false
            self.timer?.cancel(); self.timer = nil
            self.closeListener()
            self.setStatus("Phone link: off", baseURL: nil)
        }
    }

    /// Only the local panel receives the token-bearing link. The log never does.
    func rotate(completion: @escaping (String?) -> Void) {
        queue.async { [weak self] in
            guard let self else { completion(nil); return }
            do {
                let token = try self.tokenStore.rotate()
                self.securityLock.lock()
                self.token = token
                let identity = self.identity
                self.securityLock.unlock()
                ActionLog.shared.writePhone("phone link rotated: \(identity?.baseURL ?? "Tailscale unavailable")#t=<redacted>")
                completion(identity.map { $0.baseURL + "#t=" + token })
            } catch {
                ActionLog.shared.writePhone("phone link rotation failed: \(error.localizedDescription)")
                completion(nil)
            }
        }
    }

    private func setStatus(_ text: String, baseURL: String?, detail: String? = nil) {
        let key = text + (baseURL ?? "") + (detail ?? "")
        guard key != lastStatus else { return }
        lastStatus = key
        ActionLog.shared.writePhone(text + (baseURL.map { ": " + $0 } ?? "") + (detail.map { " (" + $0 + ")" } ?? ""))
        status(text, baseURL)
    }

    private func waitingForTailnet(_ reason: String) {
        closeListener()
        setStatus("Phone link: waiting for Tailscale", baseURL: nil, detail: reason)
    }

    private func tailnetResult(_ arguments: [String], stage: String, limit: Int) -> Subprocess.Result? {
        guard let result = try? PhoneTailscale.run(arguments, limit: limit) else {
            waitingForTailnet(stage + " command could not start")
            return nil
        }
        guard !result.timedOut, !result.signaled, result.code == 0 else {
            let reason = result.timedOut ? " timed out" : result.signaled ? " was interrupted" : " exited \(result.code)"
            waitingForTailnet(stage + " command" + reason)
            return nil
        }
        return result
    }

    private func refreshTailnet() {
        guard running else { return }
        securityLock.lock(); let needsToken = token == nil; securityLock.unlock()
        if needsToken {
            do {
                let newToken = try tokenStore.loadOrCreate()
                securityLock.lock(); token = newToken; securityLock.unlock()
            } catch {
                closeListener()
                setStatus("Phone link: token unavailable", baseURL: nil)
                return
            }
        }
        guard let ip = tailnetResult(["ip", "-4"], stage: "ip", limit: 1024) else { return }
        let address = ip.output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard PhoneSecurity.isTailnetIPv4(address) else {
            waitingForTailnet("ip command did not return one Tailscale IPv4 address")
            return
        }
        guard let status = tailnetResult(["status", "--json"], stage: "status", limit: 1_048_576) else { return }
        let statusData = Data(status.output.utf8)
        guard (try? JSONSerialization.jsonObject(with: statusData)) is [String: Any] else {
            waitingForTailnet("status command did not return a JSON object")
            return
        }
        guard let found = PhoneSecurity.identity(status: statusData, address: address) else {
            waitingForTailnet("status has no running node with a matching signed-in owner")
            return
        }
        securityLock.lock()
        let old = identity
        if old != found { owners.clear() }
        identity = found
        securityLock.unlock()
        if old?.cacheIdentity != found.cacheIdentity {
            for fd in connections.keys { _ = shutdown(fd, SHUT_RDWR) }
        }
        if listenAddress != found.address {
            closeListener(clearIdentity: false)
            guard bind(found.address) else {
                setStatus("Phone link: port 8477 unavailable", baseURL: nil)
                return
            }
        }
        setStatus("Phone link: ready", baseURL: found.baseURL)
    }

    private func closeListener(clearIdentity: Bool = true) {
        listener?.cancel(); listener = nil; listenAddress = nil
        for fd in connections.keys { _ = shutdown(fd, SHUT_RDWR) }
        if clearIdentity {
            securityLock.lock(); identity = nil; owners.clear(); securityLock.unlock()
        }
    }

    private func bind(_ address: String) -> Bool {
        guard PhoneSecurity.isTailnetIPv4(address) else { return false }
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        var reuse: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout.size(ofValue: reuse)))
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
        _ = fcntl(fd, F_SETFL, O_NONBLOCK)
        var endpoint = sockaddr_in()
        endpoint.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        endpoint.sin_family = sa_family_t(AF_INET)
        endpoint.sin_port = in_port_t(8477).bigEndian
        guard inet_pton(AF_INET, address, &endpoint.sin_addr) == 1 else { close(fd); return false }
        let bound = withUnsafePointer(to: &endpoint) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard bound == 0, listen(fd, 16) == 0 else { close(fd); return false }
        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
        source.setEventHandler { [weak self] in self?.acceptRequests(fd) }
        source.setCancelHandler { close(fd) }
        listener = source; listenAddress = address
        source.resume()
        return true
    }

    private func acceptRequests(_ listenerFD: Int32) {
        while running {
            var endpoint = sockaddr_in(), size = socklen_t(MemoryLayout<sockaddr_in>.size)
            let fd = withUnsafeMutablePointer(to: &endpoint) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { accept(listenerFD, $0, &size) }
            }
            guard fd >= 0 else { return }
            _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
            _ = fcntl(fd, F_SETFL, 0)
            var noSignal: Int32 = 1, timeout = timeval(tv_sec: 2, tv_usec: 0)
            _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout.size(ofValue: noSignal)))
            _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
            _ = setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
            var chars = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
            let source = inet_ntop(AF_INET, &endpoint.sin_addr, &chars, socklen_t(chars.count)) == nil ? "unknown" : String(cString: chars)
            guard connections.count < 12 else {
                ActionLog.shared.writePhone("phone denied source \(source): connection limit")
                send(.json(503, ["error": "Phone link is busy. Try again."]), to: fd)
                close(fd)
                continue
            }
            let id = UUID()
            connections[fd] = id
            workers.async { [weak self] in
                guard let self else { close(fd); return }
                self.handle(fd, source: source)
                self.queue.async {
                    if self.connections[fd] == id { self.connections.removeValue(forKey: fd) }
                    close(fd)
                }
            }
        }
    }

    private func readRequest(_ fd: Int32) throws -> PhoneHTTPRequest {
        let deadline = ProcessInfo.processInfo.systemUptime + 8
        var data = Data(), headEnd: Int?, request: PhoneHTTPRequest?
        var bytes = [UInt8](repeating: 0, count: 4096)
        while ProcessInfo.processInfo.systemUptime < deadline {
            let count = recv(fd, &bytes, bytes.count, 0)
            if count < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) { continue }
            guard count > 0 else { throw PhoneHTTPError.malformed }
            data.append(contentsOf: bytes.prefix(count))
            if headEnd == nil {
                if let range = data.range(of: Data("\r\n\r\n".utf8)) {
                    headEnd = range.upperBound
                    request = try PhoneHTTPParser.head(Data(data.prefix(range.upperBound)))
                } else if data.count > PhoneHTTPParser.headerLimit { throw PhoneHTTPError.tooLarge }
            }
            if let headEnd, var parsed = request {
                let expected = headEnd + parsed.contentLength
                guard data.count <= expected else { throw PhoneHTTPError.malformed }
                if data.count == expected {
                    parsed.body = Data(data.suffix(parsed.contentLength))
                    return parsed
                }
            }
        }
        throw PhoneHTTPError.timedOut
    }

    /// The Mac's identity and the phone token are still the ones a request was checked against.
    private func stillCurrent(identity own: PhoneTailnetIdentity, token expected: String) -> Bool {
        securityLock.lock()
        defer { securityLock.unlock() }
        return identity == own && token == expected
    }

    private func denied(_ status: Int, _ reason: String, fd: Int32, source: String) {
        ActionLog.shared.writePhone("phone denied source \(source): \(reason) (\(status))")
        send(.json(status, ["error": reason]), to: fd)
    }

    private func handle(_ fd: Int32, source: String) {
        let request: PhoneHTTPRequest
        do { request = try readRequest(fd) }
        catch PhoneHTTPError.tooLarge { denied(413, "Request is too large", fd: fd, source: source); return }
        catch PhoneHTTPError.timedOut { denied(408, "Request timed out", fd: fd, source: source); return }
        catch { denied(400, "Invalid request", fd: fd, source: source); return }
        securityLock.lock()
        let own = identity, expected = token
        securityLock.unlock()
        guard let own, let expected else {
            denied(403, "This device is not the Mac owner's Tailscale peer", fd: fd, source: source); return
        }
        // An action asks whois afresh; everything else may use a verdict up to ten minutes old.
        let mutation = request.method == "POST" && request.path == "/api/action"
        let owner = owners.check(source, identity: own, mutation: mutation)
        let access = PhoneSecurity.accessStatus(method: request.method, path: request.path,
            ownerVerified: owner.allowed, authorization: request.headers["authorization"], expectedToken: expected)
        guard access != 403 else {
            denied(403, "This device is not the Mac owner's Tailscale peer", fd: fd, source: source); return
        }
        let ownerOnly = PhoneSecurity.ownerOnlyResource(method: request.method, path: request.path)
        if !ownerOnly {
            guard PhoneSecurity.sameOrigin(headers: request.headers, identity: own) else {
                denied(403, "Request origin is not this phone link", fd: fd, source: source); return
            }
            guard access == 200 else {
                denied(401, "Open the current phone link from Telebots Bar", fd: fd, source: source); return
            }
        }
        // The bearer token and identity again, right after the owner check: a link
        // rotated or an identity change while whois ran refuses the request.
        securityLock.lock()
        let stillAuthorized = identity == own && (ownerOnly || token == expected)
        securityLock.unlock()
        guard stillAuthorized else { denied(401, "Phone link changed; open the current link", fd: fd, source: source); return }
        let response: PhoneResponse
        switch (request.method, request.path) {
        case ("GET", "/"): response = PhoneResponse(status: 200, body: page, contentType: "text/html; charset=utf-8")
        case ("GET", "/manifest.webmanifest"):
            response = PhoneResponse(status: 200, body: Self.manifest, contentType: "application/manifest+json")
        case ("GET", "/icon-180.png"), ("GET", "/icon-512.png"), ("GET", "/apple-touch-icon.png"):
            let size = request.path == "/icon-512.png" ? 512 : 180
            guard let icon = icons[size] else { denied(404, "Icon unavailable", fd: fd, source: source); return }
            response = PhoneResponse(status: 200, body: icon, contentType: "image/png")
        case ("GET", "/api/state"):
            response = waitForResponse { done in self.state { done(PhoneResponse(status: 200, body: $0)) } }
        case ("POST", "/api/action"):
            let admission = PhoneAdmission(verifiedAt: owner.at, stillAuthorized: { [weak self] in
                self?.stillCurrent(identity: own, token: expected) ?? false
            })
            response = waitForResponse { done in
                self.action(request.body, admission, done)
            } timedOut: {
                admission.expire()
                    ? .json(503, ["error": "The app was busy, so the action did not run. Try again."])
                    : .json(503, ["error": "The app is busy. Refresh to check progress."])
            }
        default:
            denied(400, "Unknown route or method", fd: fd, source: source); return
        }
        if response.status >= 400 {
            ActionLog.shared.writePhone("phone denied source \(source): request rejected (\(response.status))")
        }
        send(response, to: fd)
    }

    private func waitForResponse(_ work: (@escaping (PhoneResponse) -> Void) -> Void,
                                 timedOut: () -> PhoneResponse = { .json(503, ["error": "The app is busy. Refresh to check progress."]) }) -> PhoneResponse {
        let result = PhoneReplyBox()
        work { result.finish($0) }
        return result.wait() ?? timedOut()
    }

    private func send(_ response: PhoneResponse, to fd: Int32) {
        guard response.body.count <= 2_097_152 else { return }
        let names = [200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                     408: "Request Timeout", 409: "Conflict", 413: "Content Too Large", 429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable"]
        let contentType = response.contentType.contains("\r") || response.contentType.contains("\n") ? "application/json" : response.contentType
        var header = "HTTP/1.1 \(response.status) \(names[response.status] ?? "Response")\r\nContent-Type: \(contentType)\r\nContent-Length: \(response.body.count)\r\nConnection: close\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
        header += "Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; manifest-src 'self' data: blob:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'\r\n"
        if response.status == 401 { header += "WWW-Authenticate: Bearer realm=\"Telebots\"\r\n" }
        if response.status == 429 { header += "Retry-After: 10\r\n" }
        header += "\r\n"
        var data = Data(header.utf8); data.append(response.body)
        let deadline = ProcessInfo.processInfo.systemUptime + 8
        data.withUnsafeBytes { raw in
            guard let pointer = raw.baseAddress else { return }
            var offset = 0
            while offset < raw.count && ProcessInfo.processInfo.systemUptime < deadline {
                let count = Darwin.send(fd, pointer.advanced(by: offset), raw.count - offset, 0)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { return }
                offset += count
            }
        }
    }

    // iOS can add an HTTP page to its home screen. A service worker is neither
    // available on plain HTTP nor needed. Static assets require the owner check.
    // Omit start_url so the installing document's URL carries its fragment into
    // standalone storage without putting a token in this shared manifest.
    private static let manifest = Data("""
    {"id":"/","name":"Telebots","short_name":"Telebots","scope":"/","display":"standalone","background_color":"#131313","theme_color":"#131313","icons":[{"src":"/icon-180.png","sizes":"180x180","type":"image/png"},{"src":"/icon-512.png","sizes":"512x512","type":"image/png","purpose":"any maskable"}]}
    """.utf8)
}

private final class PhoneReplyBox {
    private let lock = NSLock()
    private let semaphore = DispatchSemaphore(value: 0)
    private var value: PhoneResponse?
    func finish(_ response: PhoneResponse) {
        lock.lock()
        guard value == nil else { lock.unlock(); return }
        value = response
        lock.unlock()
        semaphore.signal()
    }
    func wait() -> PhoneResponse? {
        guard semaphore.wait(timeout: .now() + 8) == .success else { return nil }
        lock.lock(); defer { lock.unlock() }
        return value
    }
}
