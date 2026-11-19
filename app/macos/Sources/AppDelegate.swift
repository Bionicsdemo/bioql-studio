import AppKit
import WebKit

/// BioQL Studio's window. Forked from Agtr's `desktop/macos/Sources/AppDelegate.swift` (400 lines) and cut to the
/// window, the menus, and watching the service. Agtr's Touch ID approvals (:230-278), its voice (:196-204), its quick
/// panel and its global hot key (:322-330) are not ported: the approval that replaces them is an explicit single-use
/// code handled entirely by the service, because this shell cannot honestly prove human presence (app/README.md).
///
/// NOT COMPILED. Correct by inspection only; there is no Xcode run in this work.
final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    private let service = Service()
    private var window: NSWindow!
    private var web: WKWebView!
    private var restarts = 0
    private var restarting = false
    private let maxRestarts = 5

    // ------------------------------------------------------------------ launch
    func applicationDidFinishLaunching(_ note: Notification) {
        // A pipeline a person approved can run for hours inside the service this app owns, so macOS must not quit the
        // app on its own when memory is tight (Agtr: AppDelegate.swift:19-22, after it lost running jobs that way).
        ProcessInfo.processInfo.disableAutomaticTermination("an approved BioQL pipeline may be running")
        ProcessInfo.processInfo.disableSuddenTermination()
        buildMenus()
        buildWindow()
        // FIX 3, part two — "it never restarts a dead service": the shell is told when the child exits and acts on it.
        service.onExit = { [weak self] status in self?.serviceDied(status) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        bringUp()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow(nil)
        return true
    }

    func applicationWillTerminate(_ note: Notification) {
        service.stop()
    }

    private func bringUp() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let problem = self?.service.start()
            DispatchQueue.main.async {
                if let problem = problem { self?.fail(problem) } else { self?.restarts = 0; self?.load() }
            }
        }
    }

    /// The window is loaded with the token in the URL fragment, which a browser never sends to a server.
    private func load() {
        guard let url = service.windowURL else { return }
        web.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    private func fail(_ text: String) {
        let a = NSAlert()
        a.messageText = "BioQL Studio could not start"
        a.informativeText = text
        a.addButton(withTitle: "Try Again")
        a.addButton(withTitle: "Open the Log")
        a.addButton(withTitle: "Quit")
        switch a.runModal() {
        case .alertFirstButtonReturn: bringUp()
        case .alertSecondButtonReturn: NSWorkspace.shared.open(Service.logURL); fail(text)
        default: NSApp.terminate(nil)
        }
    }

    /// FIX 3 — Agtr's only answer to a service that died is `didFailProvisionalNavigation` reloading the same dead
    /// address every 1.5 s, for ever (AppDelegate.swift:158-160): the window shows an error page and nothing ever
    /// brings the engine back. Here a dead service is restarted, a bounded number of times, and when the restarts run
    /// out the person is told rather than left with a blank window.
    private func serviceDied(_ status: Int32) {
        guard !restarting else { return }
        restarting = true
        restarts += 1
        guard restarts <= maxRestarts else {
            restarting = false
            return fail("BioQL Studio's service stopped \(restarts) times (last exit \(status)). "
                      + "See \(Service.logURL.path). Nothing was sent to BioQL.")
        }
        let wait = min(8.0, pow(2.0, Double(restarts - 1)) * 0.5)
        DispatchQueue.main.asyncAfter(deadline: .now() + wait) { [weak self] in
            self?.restarting = false
            self?.bringUp()
        }
    }

    // ------------------------------------------------------------------ window
    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 740),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "BioQL Studio"
        window.minSize = NSSize(width: 820, height: 540)
        window.isReleasedWhenClosed = false
        window.center()
        window.setFrameAutosaveName("BioQLStudioMain")      // never "AgtrDesktopMain": its own saved frame

        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "studio")
        cfg.websiteDataStore = .nonPersistent()             // the token in the fragment is never written to disk
        web = WKWebView(frame: window.contentLayoutRect, configuration: cfg)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        window.contentView = web
    }

    @objc func showWindow(_ sender: Any?) {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // ------------------------------------------------------------------ web view
    /// FIX 5 — Agtr's navigation policy checks only the HOST (AppDelegate.swift:151: `host != "127.0.0.1"`), so any
    /// other service on this Mac — BioQL's own frontend on 3000, its backend on 5035, Agtr Desktop on 7790 — loads
    /// inside the window and inherits a same-origin-looking frame. Here the scheme, host AND port must be Studio's;
    /// anything else goes to the browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        let mine = url.scheme == "http" && url.host == "127.0.0.1" && (url.port ?? 80) == Service.port
        if mine { return decisionHandler(.allow) }
        if url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)                     // anything else opens outside this window
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        // Agtr reloads for ever here (AppDelegate.swift:158-160). Studio reloads only while the service is answering;
        // otherwise it is the service that is wrong, and serviceDied / bringUp is what fixes that.
        if service.isAlive {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.load() }
        } else {
            bringUp()
        }
    }

    /// FIX 6 — Agtr's bridge trusts every frame and every origin: `userContentController(_:didReceive:)`
    /// (AppDelegate.swift:171-172) reads `message.body` and acts, with no check of `frameInfo`, so an iframe or any
    /// page that reaches the webview can drive the native side (its own CSP allows `frame-src http://127.0.0.1:*`).
    /// Here a message is read only from the main frame of this app's own webview, served from Studio's own origin.
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.webView === web, message.frameInfo.isMainFrame else { return }
        let origin = message.frameInfo.securityOrigin
        guard origin.protocol == "http", origin.host == "127.0.0.1", origin.port == Service.port else { return }
        guard let d = message.body as? [String: Any], let type = d["type"] as? String else { return }
        switch type {
        case "openLog":
            NSWorkspace.shared.open(Service.logURL)
        case "openHome":
            NSWorkspace.shared.open(Service.home)
        case "restartService":
            service.stop()                                   // the exit handler brings it back
        default:
            // No `verify`, no `approve`, no page-chosen path. Agtr's `verify` (AppDelegate.swift:189-191, 257-278)
            // let the page name the POST path and marked the call as biometrically verified; neither exists here.
            break
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = "BioQL Studio"
        a.informativeText = message
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    // ------------------------------------------------------------------ menus
    private func item(_ title: String, _ action: Selector?, _ key: String = "",
                      target: AnyObject? = nil, mods: NSEvent.ModifierFlags = [.command]) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.keyEquivalentModifierMask = mods
        i.target = target
        return i
    }

    @objc func reload(_ sender: Any?) { load() }
    @objc func openLog(_ sender: Any?) { NSWorkspace.shared.open(Service.logURL) }
    @objc func openHome(_ sender: Any?) { NSWorkspace.shared.open(Service.home) }
    @objc func restartService(_ sender: Any?) { service.stop() }

    private func buildMenus() {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(item("About BioQL Studio", #selector(NSApplication.orderFrontStandardAboutPanel(_:))))
        appMenu.addItem(.separator())
        appMenu.addItem(item("Hide BioQL Studio", #selector(NSApplication.hide(_:)), "h"))
        appMenu.addItem(.separator())
        appMenu.addItem(item("Quit BioQL Studio", #selector(NSApplication.terminate(_:)), "q"))
        let appItem = NSMenuItem(); appItem.submenu = appMenu; main.addItem(appItem)

        let edit = NSMenu(title: "Edit")
        edit.addItem(item("Cut", #selector(NSText.cut(_:)), "x"))
        edit.addItem(item("Copy", #selector(NSText.copy(_:)), "c"))
        edit.addItem(item("Paste", #selector(NSText.paste(_:)), "v"))
        edit.addItem(item("Select All", #selector(NSText.selectAll(_:)), "a"))
        let editItem = NSMenuItem(); editItem.submenu = edit; main.addItem(editItem)

        let studio = NSMenu(title: "Studio")
        studio.addItem(item("Reload the Window", #selector(reload(_:)), "r", target: self))
        studio.addItem(item("Restart the Service", #selector(restartService(_:)), "r",
                            target: self, mods: [.command, .shift]))
        studio.addItem(.separator())
        studio.addItem(item("Open the Log", #selector(openLog(_:)), "l", target: self))
        studio.addItem(item("Open Studio's Data Folder", #selector(openHome(_:)), "", target: self))
        let studioItem = NSMenuItem(); studioItem.submenu = studio; main.addItem(studioItem)

        let win = NSMenu(title: "Window")
        win.addItem(item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"))
        win.addItem(item("Zoom", #selector(NSWindow.performZoom(_:))))
        win.addItem(item("Close", #selector(NSWindow.performClose(_:)), "w"))
        win.addItem(.separator())
        win.addItem(item("BioQL Studio", #selector(showWindow(_:)), "0", target: self))
        let winItem = NSMenuItem(); winItem.submenu = win; main.addItem(winItem)
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }
}
