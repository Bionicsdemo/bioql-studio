import Foundation

/// The local BioQL Studio service: start it, watch it, talk to it.
///
/// Forked from Agtr's `desktop/macos/Sources/Service.swift`. Four defects of the original are fixed here, each noted
/// at the place it was. NOT COMPILED: correct by inspection only.
final class Service {
    // Studio's own identity. Agtr: port 7790 and the ping name "agtr-desktop" (Service.swift:5-6, :36).
    static let port = 7810
    static let pingName = "bioql-studio"
    static let base = "http://127.0.0.1:\(port)"

    /// Agtr hard-codes `Library/Application Support/AGTR/desktop` (Service.swift:7-8) while its Python decides its own
    /// home, so the two can drift and the shell then reads a token the service never wrote. Here this one path is the
    /// only definition, and it is handed to the child as BIOQL_STUDIO_HOME (see `start()`), so drift is impossible.
    static let home = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/BioQL Studio", isDirectory: true)
    static let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/BioQL Studio.log")
    static let maxLog = 8 * 1024 * 1024

    private var process: Process?
    private var exited: Int32?
    private let lock = NSLock()

    private let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 30
        c.connectionProxyDictionary = [:]           // localhost only: never through a proxy
        c.httpShouldSetCookies = false
        return URLSession(configuration: c)
    }()

    /// The per-install token (0600, written by the service). Agtr reads a second secret here too, `native.key`
    /// (Service.swift:25), and sends it as proof that Touch ID just passed (Service.swift:109). Studio has no such
    /// file and makes no such claim.
    var token: String {
        let s = try? String(contentsOf: Service.home.appendingPathComponent("ui.token"), encoding: .utf8)
        return (s ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The window's URL, with the token in the FRAGMENT. A fragment is never sent to a server, never logged by one
    /// and never put in a Referer — unlike Agtr, which substitutes the token into the HTML it serves
    /// (agtrdesktop/service.py:1255) and also accepts it as `?t=` (service.py:1228).
    var windowURL: URL? {
        let t = token
        guard !t.isEmpty else { return URL(string: Service.base + "/") }
        return URL(string: Service.base + "/#t=" + t)
    }

    /// Whoever answers on the port, by its own /api/ping. Blocking; call off the main thread.
    /// Agtr asks the same question and then trusts the answer: `name == "agtr-desktop"` from anything at all makes
    /// Service.swift:36 call it "the service", `start()` return success (Service.swift:62-63), and
    /// Service.swift:108-109 send it the token and the native key. A ping name is not authentication.
    func pingName(timeout: TimeInterval = 1.2) -> String? {
        guard let url = URL(string: Service.base + "/api/ping") else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = timeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        let sem = DispatchSemaphore(value: 0)
        var name: String?
        session.dataTask(with: req) { data, _, _ in
            if let data = data,
               let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                name = j["name"] as? String
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 0.5)
        return name
    }

    /// FIX 1 — "it reuses any server answering on its port" (Agtr: Service.swift:28-42 with :62-63 and :108-109).
    /// This shell trusts exactly one service: the child it started itself. Anything already listening on 7810 —
    /// another copy of Studio, a leftover service, Agtr Desktop, anything — is reported and nothing is sent to it.
    var isMine: Bool {
        lock.lock(); defer { lock.unlock() }
        return process?.isRunning == true
    }

    var isAlive: Bool { isMine && pingName() == Service.pingName }

    private func python() -> String? {
        let own = (Bundle.main.resourcePath ?? "") + "/python/bin/python3"
        let candidates = [own, "/opt/homebrew/opt/python@3.14/bin/python3.14", "/opt/homebrew/bin/python3",
                          "/usr/local/bin/python3", "/opt/homebrew/opt/python@3.13/bin/python3.13", "/usr/bin/python3"]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: c)
            p.arguments = ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"]
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            do { try p.run(); p.waitUntilExit() } catch { continue }
            if p.terminationStatus == 0 { return c }
        }
        return nil
    }

    /// FIX 2 — "it truncates its own log on every launch" (Agtr: Service.swift:71-73, where `createFile` on an
    /// existing path empties it and the `seekToEndOfFile` that follows then has nothing to seek past, so the log of
    /// the launch that just crashed is gone exactly when it is needed). Here the file is created only when it is
    /// missing, and it is rolled aside once it grows past 8 MB rather than being emptied.
    private func logHandle() -> FileHandle? {
        let fm = FileManager.default
        let path = Service.logURL.path
        try? fm.createDirectory(at: Service.logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let size = (try? fm.attributesOfItem(atPath: path)[.size] as? Int) ?? nil, size > Service.maxLog {
            let old = Service.logURL.deletingPathExtension().appendingPathExtension("1.log")
            try? fm.removeItem(at: old)
            try? fm.moveItem(at: Service.logURL, to: old)
        }
        if !fm.fileExists(atPath: path) { fm.createFile(atPath: path, contents: nil) }
        let h = try? FileHandle(forWritingTo: Service.logURL)
        h?.seekToEndOfFile()
        return h
    }

    /// Blocking; call off the main thread. Returns nil when the service is up, else what is wrong.
    func start() -> String? {
        if isAlive { return nil }
        if let other = pingName() {
            if other == Service.pingName {
                // FIX 1, continued: this one is NOT ours. Agtr would adopt it and hand it the token.
                return "Something is already serving on 127.0.0.1:\(Service.port) and this app did not start it. "
                     + "BioQL Studio will not attach to a service it did not start, because that service would be "
                     + "handed this install's token. Quit the other copy (or the leftover `python3 -m app serve`), "
                     + "then try again."
            }
            return "Port \(Service.port) is held by “\(other)”, which is not BioQL Studio. Free the port and try again."
        }
        guard let root = Bundle.main.resourceURL?.appendingPathComponent("bioql-studio", isDirectory: true),
              FileManager.default.fileExists(atPath: root.appendingPathComponent("app").path) else {
            return "This copy of BioQL Studio is incomplete (its service is missing)."
        }
        guard let py = python() else {
            return "BioQL Studio needs Python 3.10 or later."
        }
        let log = logHandle()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = ["-m", "app", "serve", "--port", String(Service.port)]
        p.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = root.path
        env["PYTHONUNBUFFERED"] = "1"
        env["BIOQL_STUDIO_HOME"] = Service.home.path        // one definition of the home folder, shared with the child
        // Python's compiled files go outside the bundle: written inside, they break the app's signature seal.
        let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        env["PYTHONPYCACHEPREFIX"] = caches.appendingPathComponent("BioQL Studio/pycache").path
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        p.environment = env
        if let log = log { p.standardOutput = log; p.standardError = log }
        // FIX 3, part one — "it never restarts a dead service". Agtr never learns that its child died: it sets no
        // terminationHandler (Service.swift:87-88) and its only reaction to a blank window is to reload forever
        // (AppDelegate.swift:158-160). Here the exit is recorded, and AppDelegate restarts on it.
        p.terminationHandler = { [weak self] proc in
            guard let self = self else { return }
            self.lock.lock()
            self.exited = proc.terminationStatus
            self.lock.unlock()
            DispatchQueue.main.async { self.onExit?(proc.terminationStatus) }
        }
        do { try p.run() } catch { return "The service could not start: \(error.localizedDescription)" }
        lock.lock(); process = p; exited = nil; lock.unlock()
        for _ in 0..<80 {
            if pingName(timeout: 0.6) == Service.pingName { return nil }
            if !p.isRunning { return "The service stopped while starting. See \(Service.logURL.path)." }
            Thread.sleep(forTimeInterval: 0.25)
        }
        return "The service did not answer in time. See \(Service.logURL.path)."
    }

    /// Called on the main thread when the child exits on its own.
    var onExit: ((Int32) -> Void)?

    func stop() {
        lock.lock()
        let p = process
        process = nil
        lock.unlock()
        p?.terminationHandler = nil
        if p?.isRunning == true { p?.terminate() }
    }

    /// POST to this app's own service, on a path this app chose.
    ///
    /// FIX 4 — Agtr's equivalent takes the path from the web page (AppDelegate.swift:190, 257-259: the page sends
    /// `{type:"verify", path:"/api/…"}` and the shell posts to it, with only a `hasPrefix("/api/")` check), so the
    /// page picks which privileged route the native side calls. Here the caller passes an enum, not a string, and
    /// the page cannot name a path at all.
    enum Route: String {
        case approve = "/api/approve"
        case call = "/api/call"
    }

    func post(_ route: Route, _ body: [String: Any], done: @escaping (Bool, String?) -> Void) {
        guard let url = URL(string: Service.base + route.rawValue) else { return done(false, "bad address") }
        let t = token
        guard !t.isEmpty else { return done(false, "This install has no token yet; restart BioQL Studio.") }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(t, forHTTPHeaderField: "X-Studio-Token")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        session.dataTask(with: req) { data, resp, err in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            var message: String? = err?.localizedDescription
            if let data = data, let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                message = (j["error"] as? String) ?? (j["detail"] as? String) ?? message
            }
            DispatchQueue.main.async { done(code == 200, code == 200 ? nil : (message ?? "the service did not answer")) }
        }.resume()
    }
}
