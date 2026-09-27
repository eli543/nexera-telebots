// PhoneServerTests.swift - offline fixtures, never the live token or fleet state.

import Foundation

enum PhoneServerTests {
    static func run() -> Int32 {
        var passed = 0, failed: [String] = []
        func check(_ name: String, _ condition: @autoclosure () -> Bool) {
            if condition() { passed += 1 } else { failed.append(name) }
        }
        func json(_ object: [String: Any]) -> Data {
            (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data()
        }
        let first = "100.64.0.0", last = "100.127.255.255"
        for address in [first, last, "100.100.1.23"] { check("tailnet allows \(address)", PhoneSecurity.isTailnetIPv4(address)) }
        for address in ["100.63.255.255", "100.128.0.0", "127.0.0.1", "0.0.0.0", "10.0.0.1", "192.168.1.2",
                        "100.100.1.256", "100.100.1", "100.100.1.2/32", "100.100.1.2\n", "100.064.1.2",
                        "::ffff:100.100.1.2", "100.100.1.2;touch x"] {
            check("tailnet rejects \(address.replacingOccurrences(of: "\n", with: " newline"))", !PhoneSecurity.isTailnetIPv4(address))
        }

        let token = String(repeating: "a1", count: 32)
        check("valid token", PhoneSecurity.isToken(token))
        check("equal token", PhoneSecurity.tokenMatches(token, expected: token))
        check("wrong first byte", !PhoneSecurity.tokenMatches("b" + String(token.dropFirst()), expected: token))
        check("wrong last byte", !PhoneSecurity.tokenMatches(String(token.dropLast()) + "0", expected: token))
        for invalid in ["", String(token.dropLast()), token + "0", token.uppercased(), token + "\n", String(repeating: "z", count: 64)] {
            check("invalid token shape", !PhoneSecurity.isToken(invalid))
            check("invalid token cannot authenticate", !PhoneSecurity.tokenMatches(invalid, expected: token))
        }
        check("missing token", !PhoneSecurity.tokenMatches(nil, expected: token))
        check("valid bearer", PhoneSecurity.bearerToken("Bearer " + token) == token)
        check("bearer scheme ignores case", PhoneSecurity.bearerToken("bearer " + token) == token)
        for header in [token, "Basic " + token, "Bearer  " + token, "Bearer " + token + ",other", "Bearer " + token + "\r\nX: y"] {
            check("bad bearer rejected", PhoneSecurity.bearerToken(header) == nil)
        }

        let ownIP = "100.100.1.23", peerIP = "100.100.1.24", userID = 123456
        var status: [String: Any] = [
            "BackendState": "Running", "HaveNodeKey": true,
            "Self": ["UserID": userID, "DNSName": "test-mac.example.ts.net.", "TailscaleIPs": [ownIP]],
            "User": [String(userID): ["ID": userID, "LoginName": "owner@example.test"]],
            "CurrentTailnet": ["MagicDNSEnabled": true],
        ]
        guard let identity = PhoneSecurity.identity(status: json(status), address: ownIP) else {
            print("phone self-test: fixture identity failed")
            return 1
        }
        check("MagicDNS hostname used", identity.baseURL == "http://test-mac.example.ts.net:8477/")
        check("CLI address must match own node", PhoneSecurity.identity(status: json(status), address: peerIP) == nil)
        check("loopback cannot be bound", PhoneSecurity.identity(status: json(status), address: "127.0.0.1") == nil)
        let goodStatus = status
        status["BackendState"] = "NeedsLogin"
        check("logged-out status denied", PhoneSecurity.identity(status: json(status), address: ownIP) == nil)
        status = goodStatus; status["HaveNodeKey"] = false
        check("missing node key denied", PhoneSecurity.identity(status: json(status), address: ownIP) == nil)
        status = goodStatus; status["CurrentTailnet"] = ["MagicDNSEnabled": false]
        check("MagicDNS off uses IP", PhoneSecurity.identity(status: json(status), address: ownIP)?.baseURL == "http://\(ownIP):8477/")
        status = goodStatus; status["Self"] = ["UserID": userID, "TailscaleIPs": [ownIP], "Tags": ["tag:server"]]
        check("tagged own node denied", PhoneSecurity.identity(status: json(status), address: ownIP) == nil)
        status = goodStatus; status["User"] = [String(userID): ["ID": userID + 1, "LoginName": "owner@example.test"]]
        check("own user map mismatch denied", PhoneSecurity.identity(status: json(status), address: ownIP) == nil)
        status = goodStatus; status["Self"] = ["UserID": userID, "DNSName": "bad\r\nInjected: yes", "TailscaleIPs": [ownIP]]
        check("unsafe DNS falls back to IP", PhoneSecurity.identity(status: json(status), address: ownIP)?.dnsName == nil)

        let goodNode: [String: Any] = ["User": userID, "Addresses": [peerIP + "/32"], "MachineAuthorized": true]
        let goodUser: [String: Any] = ["ID": userID, "LoginName": "owner@example.test"]
        let whois: [String: Any] = ["Node": goodNode, "UserProfile": goodUser]
        check("same-owner peer allowed", PhoneSecurity.sameOwner(source: peerIP, whois: json(whois), identity: identity))
        var phoneNode = goodNode
        phoneNode.removeValue(forKey: "MachineAuthorized")
        check("phone peer with omitted local-only field allowed", PhoneSecurity.sameOwner(source: peerIP, whois: json(["Node": phoneNode, "UserProfile": goodUser]), identity: identity))
        check("whois failure denied", !PhoneSecurity.sameOwner(source: peerIP, whois: nil, identity: identity))
        check("malformed whois denied", !PhoneSecurity.sameOwner(source: peerIP, whois: Data("not json".utf8), identity: identity))
        check("missing whois fields denied", !PhoneSecurity.sameOwner(source: peerIP, whois: json([:]), identity: identity))
        check("non-tailnet peer denied", !PhoneSecurity.sameOwner(source: "127.0.0.1", whois: json(whois), identity: identity))
        check("different socket address denied", !PhoneSecurity.sameOwner(source: ownIP, whois: json(whois), identity: identity))
        for change: [String: Any] in [["User": userID + 1], ["User": true], ["Tags": ["tag:server"]], ["Tags": "tag:server"], ["MachineAuthorized": false], ["Expired": true], ["Addresses": ["100.100.1.0/24"]]] {
            let node = goodNode.merging(change) { _, new in new }
            check("unsafe whois node denied", !PhoneSecurity.sameOwner(source: peerIP, whois: json(["Node": node, "UserProfile": goodUser]), identity: identity))
        }
        for user: [String: Any] in [["ID": userID + 1, "LoginName": "owner@example.test"], ["ID": userID, "LoginName": "other@example.test"], ["ID": userID]] {
            check("different or missing login denied", !PhoneSecurity.sameOwner(source: peerIP, whois: json(["Node": goodNode, "UserProfile": user]), identity: identity))
        }

        let verifiedOwner = PhoneSecurity.sameOwner(source: peerIP, whois: json(whois), identity: identity)
        let nonOwner = PhoneSecurity.sameOwner(source: peerIP, whois: json(["Node": goodNode,
            "UserProfile": ["ID": userID, "LoginName": "other@example.test"]]), identity: identity)
        func access(_ method: String, _ path: String, _ owner: Bool, _ authorization: String? = nil) -> Int {
            PhoneSecurity.accessStatus(method: method, path: path, ownerVerified: owner,
                authorization: authorization, expectedToken: token)
        }
        for path in ["/", "/manifest.webmanifest", "/icon-180.png", "/icon-512.png", "/apple-touch-icon.png"] {
            check("owner static GET without token: \(path)", access("GET", path, verifiedOwner) == 200)
            check("owner static GET ignores stale token: \(path)", access("GET", path, verifiedOwner, "Bearer stale") == 200)
            check("non-owner static GET denied: \(path)", access("GET", path, nonOwner) == 403)
            check("non-owner static GET with token denied: \(path)", access("GET", path, nonOwner, "Bearer " + token) == 403)
            check("POST cannot use static exemption: \(path)", access("POST", path, verifiedOwner) == 401)
        }
        for (method, path) in [("GET", "/api/state"), ("POST", "/api/action"), ("GET", "/api/unknown")] {
            check("owner API without token denied: \(path)", access(method, path, verifiedOwner) == 401)
            check("owner API with valid token allowed: \(path)", access(method, path, verifiedOwner, "Bearer " + token) == 200)
            check("owner API with wrong token denied: \(path)", access(method, path, verifiedOwner, "Bearer " + String(repeating: "0", count: 64)) == 401)
            check("non-owner API denied: \(path)", access(method, path, nonOwner) == 403)
            check("non-owner API with token denied: \(path)", access(method, path, nonOwner, "Bearer " + token) == 403)
        }
        let failedWhois = PhoneSecurity.sameOwner(source: peerIP, whois: nil, identity: identity)
        let nonTailnet = PhoneSecurity.sameOwner(source: "127.0.0.1", whois: json(whois), identity: identity)
        for owner in [failedWhois, nonTailnet] {
            check("unverified source cannot load page", access("GET", "/", owner, "Bearer " + token) == 403)
            check("unverified source cannot read API", access("GET", "/api/state", owner, "Bearer " + token) == 403)
        }

        let host = "test-mac.example.ts.net:8477"
        check("local host allowed", PhoneSecurity.sameOrigin(headers: ["host": host], identity: identity))
        check("IP host allowed", PhoneSecurity.sameOrigin(headers: ["host": ownIP + ":8477"], identity: identity))
        check("same origin allowed", PhoneSecurity.sameOrigin(headers: ["host": host, "origin": "http://" + host], identity: identity))
        let navigationHeaders = ["host": host, "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"]
        check("external link allows root navigation", PhoneSecurity.sameOrigin(headers: navigationHeaders, identity: identity, navigation: true))
        check("external navigation cannot reach APIs", !PhoneSecurity.sameOrigin(headers: navigationHeaders, identity: identity))
        check("external fetch cannot use navigation exception", !PhoneSecurity.sameOrigin(headers: ["host": host, "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"], identity: identity, navigation: true))
        check("navigation with cross-site Origin denied", !PhoneSecurity.sameOrigin(headers: navigationHeaders.merging(["origin": "https://other.example"]) { _, new in new }, identity: identity, navigation: true))
        let cliEnvironment = PhoneTailscale.environment(["HOME": "/example", "XPC_SERVICE_NAME": "example.app"])
        check("launchd child gets CLI terminal mode", cliEnvironment["TERM"] == "dumb")
        check("CLI keeps existing environment", cliEnvironment["HOME"] == "/example" && cliEnvironment["XPC_SERVICE_NAME"] == "example.app")
        for headers in [["host": "attacker.example:8477"], ["host": host, "origin": "https://attacker.example"],
                        ["host": host, "origin": "null"], ["host": host, "sec-fetch-site": "cross-site"], [:]] {
            check("cross-site request denied", !PhoneSecurity.sameOrigin(headers: headers, identity: identity))
        }

        func parse(_ raw: String) -> PhoneHTTPRequest? { try? PhoneHTTPParser.head(Data(raw.utf8)) }
        check("minimal GET parsed", parse("GET /api/state HTTP/1.1\r\nHost: \(host)\r\n\r\n")?.path == "/api/state")
        check("last header preserves every byte", parse("GET /api/state HTTP/1.1\r\nHost: \(host)\r\n\r\n")?.headers["host"] == host)
        check("JSON POST parsed", parse("POST /api/action HTTP/1.1\r\nHost: \(host)\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n")?.contentLength == 2)
        check("last JSON content type preserved", parse("POST /api/action HTTP/1.1\r\nHost: \(host)\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n")?.headers["content-type"] == "application/json")
        let invalidRequests = [
            "GET / HTTP/1.0\r\nHost: \(host)\r\n\r\n",
            "GET / HTTP/1.1\r\n\r\n",
            "GET / HTTP/1.1\r\nHost: \(host)\r\nHost: other\r\n\r\n",
            "GET / HTTP/1.1\r\nHost: \(host)\r\nAuthorization: one\r\nAUTHORIZATION: two\r\n\r\n",
            "GET /?t=secret HTTP/1.1\r\nHost: \(host)\r\n\r\n",
            "GET /#t=secret HTTP/1.1\r\nHost: \(host)\r\n\r\n",
            "GET /%2e%2e/path HTTP/1.1\r\nHost: \(host)\r\n\r\n",
            "GET http://other/ HTTP/1.1\r\nHost: \(host)\r\n\r\n",
            "GET / HTTP/1.1\r\nHost: \(host)\r\n Folded: yes\r\n\r\n",
            "GET / HTTP/1.1\r\nHost: \(host)\r\nBad\nHeader: yes\r\n\r\n",
            "GET / HTTP/1.1\r\nHost: \(host)\r\nTransfer-Encoding: chunked\r\n\r\n",
            "GET / HTTP/1.1\r\nHost: \(host)\r\nContent-Length: 2\r\n\r\n",
            "POST /api/action HTTP/1.1\r\nHost: \(host)\r\nContent-Type: application/json\r\n\r\n",
            "POST /api/action HTTP/1.1\r\nHost: \(host)\r\nContent-Length: -1\r\nContent-Type: application/json\r\n\r\n",
            "POST /api/action HTTP/1.1\r\nHost: \(host)\r\nContent-Length: 4097\r\nContent-Type: application/json\r\n\r\n",
            "POST /api/action HTTP/1.1\r\nHost: \(host)\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\n",
            "POST /api/action HTTP/1.1\r\nHost: \(host)\r\nContent-Length: 2\r\nContent-Type: application/json\r\nExpect: 100-continue\r\n\r\n",
            "POST /api/action HTTP/1.1\r\nHost: \(host)\r\nContent-Length: 2\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\n\r\n",
            "GET / HTTP/1.1\r\nHost: \(host)\r\nX-Large: " + String(repeating: "x", count: PhoneHTTPParser.headerLimit) + "\r\n\r\n",
        ]
        for raw in invalidRequests { check("unsafe HTTP rejected", parse(raw) == nil) }

        var gate = PhoneActionGate()
        check("fresh gate allows bot", gate.remaining(["one"], now: 0) == 0)
        gate.record(["one"], now: 100)
        check("same-second cooldown", gate.remaining(["one"], now: 100) == 10)
        check("cooldown at 9.99 seconds", gate.remaining(["one"], now: 109.99) == 1)
        check("cooldown clears at 10 seconds", gate.remaining(["one"], now: 110) == 0)
        check("another bot is independent", gate.remaining(["two"], now: 101) == 0)
        check("batch respects blocked bot", gate.remaining(["two", "one"], now: 101) == 9)
        let actions = ["reconnect", "fresh", "reconnect_all_down", "watchdog_on", "watchdog_off", "snapshot", "restore"]
        for action in actions {
            var object: [String: Any] = ["action": action]
            if action == "fresh" || action == "reconnect" { object["bot"] = "Example" }
            if action == "fresh" { object["confirm"] = true }
            check("allowed action parses", PhoneActionRequest.parse(json(object))?.action == action)
        }
        for object: [String: Any] in [["action": "execute"], ["action": "reconnect"], ["action": "fresh", "bot": "Example", "confirm": 1],
                                     ["action": "fresh", "bot": "Example", "confirm": "true"], ["action": "snapshot", "bot": "Example"],
                                     ["action": "snapshot", "command": "anything"], ["action": "reconnect", "bot": ""]] {
            check("unsafe action rejected", PhoneActionRequest.parse(json(object)) == nil)
        }
        check("malformed action rejected", PhoneActionRequest.parse(Data("{".utf8)) == nil)
        for failure in failed { print("FAIL: \(failure)") }
        print("phone self-test: \(passed) passed, \(failed.count) failed; no live files or actions touched")
        return failed.isEmpty ? 0 : 1
    }
}
