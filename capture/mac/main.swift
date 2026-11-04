// Flashcap Capture — a tiny menu-bar app for surfaces the userscript can't touch.
//
// Firefox's PDF viewer and Reader View are privileged pages: no userscript, no
// bookmarklet, no overlay. This app owns the UI instead of the page, so the
// flow stays "write it while you're looking at it":
//
//   ⌘⇧J  →  read the current selection  →  floating panel  →  ⌘↵  →  POST
//
// It reads a selection only from allowed reading apps, never touches the
// clipboard, and refuses to send captures anywhere except loopback or a
// Tailscale HTTPS origin. The server stays the source of truth.
//
// Build: ./build.sh   (produces FlashcapCapture.app)

import AppKit
import ApplicationServices
import Carbon.HIToolbox

private let defaultBase = "http://127.0.0.1:8765"
private let hotKeySignature: OSType = 0x464C4153 // 'FLAS'

/// Apps whose selections the hotkey may read. Override with:
///   defaults write local.flashcap.capture allowedApps -array org.mozilla.firefox
/// or allow every app with:
///   defaults write local.flashcap.capture allowAllApps -bool true
private let defaultAllowedApps: Set<String> = [
    "org.mozilla.firefox",
    "org.mozilla.firefoxdeveloperedition",
    "com.apple.Safari",
    "com.google.Chrome",
    "com.microsoft.edgemac",
    "com.apple.Preview",
    "com.apple.iBooksX",
    "com.amazon.Kindle",
    "com.adobe.Reader",
    "com.readdle.PDFExpert-Mac",
    "net.sourceforge.skim-app.skim",
    "org.zotero.zotero",
]

/// Server origin: `defaults write local.flashcap.capture base https://…`
/// or `FLASHCAP_URL` in the environment.
func configuredBaseURL() -> URL {
    let configured = UserDefaults.standard.string(forKey: "base")
        ?? ProcessInfo.processInfo.environment["FLASHCAP_URL"]
        ?? defaultBase
    return URL(string: configured) ?? URL(string: defaultBase)!
}

// MARK: - Selection

enum Selection {
    case text(String)
    case none
    case needsPermission
    case blocked(String)
}

enum SelectionReader {
    static func allowedAppIDs() -> Set<String> {
        if let configured = UserDefaults.standard.stringArray(forKey: "allowedApps"),
           !configured.isEmpty {
            return Set(configured)
        }
        return defaultAllowedApps
    }

    static func isAllowedApp() -> Bool {
        if UserDefaults.standard.bool(forKey: "allowAllApps") { return true }
        guard let bundleID = NSWorkspace.shared.frontmostApplication?.bundleIdentifier else {
            return false
        }
        return allowedAppIDs().contains(bundleID)
    }

    /// The focused element, or nil when the current app exposes none.
    static func focusedElement() -> AXUIElement? {
        let system = AXUIElementCreateSystemWide()
        var focused: CFTypeRef?
        guard
            AXUIElementCopyAttributeValue(
                system, kAXFocusedUIElementAttribute as CFString, &focused
            ) == .success,
            let element = focused as! AXUIElement?
        else { return nil }
        return element
    }

    /// Password fields advertise AXSecureTextField. Never read those.
    static func isSecureField(_ element: AXUIElement) -> Bool {
        var subrole: CFTypeRef?
        guard
            AXUIElementCopyAttributeValue(
                element, kAXSubroleAttribute as CFString, &subrole
            ) == .success,
            let value = subrole as? String
        else { return false }
        return value == "AXSecureTextField"
    }

    /// Read the focused element's selected text through the Accessibility API.
    /// Returns nil when the app exposes no selection attribute.
    static func viaAccessibility(_ element: AXUIElement) -> String? {
        var selected: CFTypeRef?
        guard
            AXUIElementCopyAttributeValue(
                element, kAXSelectedTextAttribute as CFString, &selected
            ) == .success,
            let text = selected as? String,
            !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return nil }

        return text
    }

    static func read() -> Selection {
        guard AXIsProcessTrusted() else {
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
            AXIsProcessTrustedWithOptions(options as CFDictionary)
            openAccessibilitySettings()
            return .needsPermission
        }

        guard isAllowedApp() else {
            let name = NSWorkspace.shared.frontmostApplication?.localizedName ?? "this app"
            return .blocked(
                "Flashcap does not read selections from \(name). "
                + "Use it from Firefox, Preview, Books, or another reading app, "
                + "or turn on “Read from all apps” in the menu."
            )
        }

        guard let element = focusedElement() else { return .none }
        if isSecureField(element) {
            return .blocked("That field is marked secure, so Flashcap did not read it.")
        }
        if let text = viaAccessibility(element) { return .text(text) }
        return .none
    }

    static func openAccessibilitySettings() {
        let url = URL(
            string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        )!
        NSWorkspace.shared.open(url)
    }
}

// MARK: - Panel

final class CapturePanel: NSPanel {
    var onSave: ((String, String, String) -> Void)?
    var onCancel: (() -> Void)?

    private let passageView = NSTextView()
    private let frontField = NSTextField()
    private let backView = NSTextView()
    private let statusLabel = NSTextField(labelWithString: "")
    private let saveButton = NSButton(title: "Save card", target: nil, action: nil)

    init(passage: String, notice: String?) {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 460),
            styleMask: [.titled, .closable, .utilityWindow],
            backing: .buffered,
            defer: false
        )

        title = "New flashcard"
        isFloatingPanel = true
        level = .floating
        hidesOnDeactivate = false
        isReleasedWhenClosed = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let content = NSView(frame: NSRect(x: 0, y: 0, width: 520, height: 460))

        content.addSubview(label("Passage", at: NSPoint(x: 20, y: 428)))
        let passageScroll = scroll(passageView, frame: NSRect(x: 20, y: 330, width: 480, height: 94))
        passageView.string = passage
        // When the hotkey could not read a selection, let the reader paste or
        // type the passage directly instead of dead-ending the panel.
        passageView.isEditable = passage.isEmpty
        content.addSubview(passageScroll)

        content.addSubview(label("Front", at: NSPoint(x: 20, y: 300)))
        frontField.frame = NSRect(x: 20, y: 272, width: 480, height: 24)
        frontField.font = .systemFont(ofSize: 13)
        frontField.placeholderString = "The question you'd want to be asked"
        content.addSubview(frontField)

        content.addSubview(label("Back", at: NSPoint(x: 20, y: 240)))
        let backScroll = scroll(backView, frame: NSRect(x: 20, y: 132, width: 480, height: 100))
        content.addSubview(backScroll)

        statusLabel.frame = NSRect(x: 20, y: 100, width: 340, height: 18)
        statusLabel.font = .systemFont(ofSize: 11)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.stringValue = notice ?? ""
        content.addSubview(statusLabel)

        let cancel = NSButton(title: "Cancel", target: self, action: #selector(cancelTapped))
        cancel.frame = NSRect(x: 330, y: 20, width: 80, height: 30)
        cancel.keyEquivalent = "\u{1b}"
        content.addSubview(cancel)

        saveButton.frame = NSRect(x: 420, y: 20, width: 80, height: 30)
        saveButton.target = self
        saveButton.action = #selector(saveTapped)
        saveButton.keyEquivalent = "\r"
        saveButton.bezelStyle = .rounded
        content.addSubview(saveButton)

        contentView = content
        makeFirstResponder(frontField)
    }

    private func label(_ text: String, at point: NSPoint) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.frame = NSRect(x: point.x, y: point.y, width: 480, height: 18)
        field.font = .systemFont(ofSize: 11, weight: .semibold)
        field.textColor = .secondaryLabelColor
        return field
    }

    private func scroll(_ textView: NSTextView, frame: NSRect) -> NSScrollView {
        let view = NSScrollView(frame: frame)
        view.hasVerticalScroller = true
        view.borderType = .bezelBorder
        view.autoresizingMask = [.width]

        textView.frame = NSRect(origin: .zero, size: frame.size)
        textView.minSize = NSSize(width: 0, height: frame.height)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.font = .systemFont(ofSize: 13)

        view.documentView = textView
        return view
    }

    override var canBecomeKey: Bool { true }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.contains(.command), event.keyCode == 36 {
            saveTapped()
            return true
        }
        return super.performKeyEquivalent(with: event)
    }

    override func cancelOperation(_ sender: Any?) {
        cancelTapped()
    }

    @objc private func cancelTapped() {
        onCancel?()
    }

    @objc private func saveTapped() {
        let front = frontField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let back = backView.string.trimmingCharacters(in: .whitespacesAndNewlines)
        let passage = passageView.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !front.isEmpty, !back.isEmpty else {
            setStatus("Front and back are both required.", error: true)
            return
        }
        onSave?(front, back, passage)
    }

    func setStatus(_ text: String, error: Bool = false, success: Bool = false) {
        statusLabel.stringValue = text
        statusLabel.textColor = error
            ? .systemRed
            : (success ? .systemGreen : .secondaryLabelColor)
    }

    func setBusy(_ busy: Bool) {
        saveButton.isEnabled = !busy
        saveButton.title = busy ? "Saving…" : "Save card"
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem?
    private var panel: CapturePanel?
    private var hotKeyRef: EventHotKeyRef?
    private var eventHandler: EventHandlerRef?

    private var baseURL: URL { configuredBaseURL() }

    /// Ephemeral session: no cookies, no disk cache, and a short timeout.
    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 30
        configuration.httpShouldSetCookies = false
        return URLSession(configuration: configuration)
    }()

    /// Refuse to send captured text anywhere except loopback or a Tailscale
    /// HTTPS origin. This is a guard rail, not a security boundary.
    private func baseURLProblem() -> String? {
        guard let host = baseURL.host?.lowercased(), !host.isEmpty else {
            return "The configured server address is not a valid URL."
        }
        if isLoopback(host) { return nil }
        if baseURL.scheme?.lowercased() != "https" {
            return "Refusing to send captured text over \(baseURL.scheme ?? "an insecure scheme"). "
                + "Use an https:// Tailscale URL, such as https://hermes.taila17c7c.ts.net."
        }
        if UserDefaults.standard.bool(forKey: "allowAnyHost") { return nil }
        if isTailscaleHost(host) { return nil }
        return "Refusing to send captured text to \(host): it is not a Tailscale address. "
            + "Set defaults write local.flashcap.capture allowAnyHost -bool true to override."
    }

    private func isLoopback(_ host: String) -> Bool {
        host == "localhost" || host == "::1" || host.hasPrefix("127.")
    }

    private func isTailscaleHost(_ host: String) -> Bool {
        if host.hasSuffix(".ts.net") { return true }
        if host.hasPrefix("100.") {
            let parts = host.split(separator: ".")
            if parts.count == 4, let second = Int(parts[1]), (64...127).contains(second) {
                return true
            }
        }
        return host.hasPrefix("fd7a:115c:a1e0:")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        installStatusItem()
        installHotKey()
    }

    private func installStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(
            systemSymbolName: "rectangle.stack.badge.plus",
            accessibilityDescription: "Flashcap"
        )
        item.button?.toolTip = "Flashcap — capture a flashcard (⌘⇧J)"

        let menu = NSMenu()
        menu.addItem(
            NSMenuItem(title: "New flashcard  ⌘⇧J", action: #selector(capture), keyEquivalent: "")
        )
        menu.addItem(
            NSMenuItem(
                title: "Open dashboard", action: #selector(openDashboard), keyEquivalent: ""
            )
        )
        menu.addItem(.separator())

        let destination = NSMenuItem(
            title: "Sending to \(baseURL.host ?? baseURL.absoluteString)",
            action: nil,
            keyEquivalent: ""
        )
        destination.isEnabled = false
        menu.addItem(destination)

        let allowAll = NSMenuItem(
            title: "Read selections from all apps",
            action: #selector(toggleAllowAllApps(_:)),
            keyEquivalent: ""
        )
        allowAll.state = UserDefaults.standard.bool(forKey: "allowAllApps") ? .on : .off
        menu.addItem(allowAll)

        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q"))
        for menuItem in menu.items { menuItem.target = self }

        item.menu = menu
        statusItem = item
    }

    private func installHotKey() {
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)
        )

        let callback: EventHandlerUPP = { _, _, userData in
            guard let userData else { return noErr }
            let delegate = Unmanaged<AppDelegate>.fromOpaque(userData).takeUnretainedValue()
            DispatchQueue.main.async { delegate.capture() }
            return noErr
        }

        InstallEventHandler(
            GetApplicationEventTarget(),
            callback,
            1,
            &eventType,
            Unmanaged.passUnretained(self).toOpaque(),
            &eventHandler
        )

        let hotKeyID = EventHotKeyID(signature: hotKeySignature, id: 1)
        RegisterEventHotKey(
            UInt32(kVK_ANSI_J),
            UInt32(cmdKey | shiftKey),
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )
    }

    @objc func capture() {
        if let problem = baseURLProblem() {
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.messageText = "Flashcap will not send this capture"
            alert.informativeText = problem
            alert.runModal()
            return
        }

        // Read before activating: activation can change the focused element.
        let selection = SelectionReader.read()
        NSApp.activate(ignoringOtherApps: true)

        switch selection {
        case .text(let passage):
            showPanel(passage: passage, notice: nil)
        case .none:
            showPanel(
                passage: "",
                notice: "No selection found — paste the passage into the box above."
            )
        case .blocked(let reason):
            showPanel(passage: "", notice: reason)
        case .needsPermission:
            let alert = NSAlert()
            alert.messageText = "Flashcap needs Accessibility access"
            alert.informativeText =
                "Grant access in System Settings → Privacy & Security → Accessibility, "
                + "then press ⌘⇧J again."
            alert.runModal()
        }
    }

    private func showPanel(passage: String, notice: String?) {
        panel?.close()

        let panel = CapturePanel(passage: passage, notice: notice)
        panel.onCancel = { [weak self] in
            self?.panel?.close()
            self?.panel = nil
        }
        panel.onSave = { [weak self] front, back, passage in
            self?.save(front: front, back: back, passage: passage, panel: panel)
        }
        panel.center()
        panel.makeKeyAndOrderFront(nil)
        self.panel = panel
    }

    private func save(front: String, back: String, passage: String, panel: CapturePanel) {
        panel.setBusy(true)
        panel.setStatus("Saving…")

        var request = URLRequest(url: baseURL.appendingPathComponent("api/capture"))
        request.httpMethod = "POST"
        request.timeoutInterval = 20
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        var body: [String: Any] = [
            "front": front,
            "back": back,
            "category": "other",
        ]
        if !passage.isEmpty {
            body["selection"] = passage
            body["quote"] = passage
        }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        session.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                panel.setBusy(false)

                if let error {
                    panel.setStatus("Could not reach \(self?.baseURL.absoluteString ?? "the server"): \(error.localizedDescription)", error: true)
                    return
                }

                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                guard (200..<300).contains(status) else {
                    let message = data.flatMap {
                        try? JSONSerialization.jsonObject(with: $0) as? [String: Any]
                    }?["error"] as? String
                    panel.setStatus(message ?? "Server returned \(status).", error: true)
                    return
                }

                panel.setStatus("Saved ✓", success: true)
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                    panel.close()
                    self?.panel = nil
                }
            }
        }
        .resume()
    }

    @objc func openDashboard() {
        NSWorkspace.shared.open(baseURL)
    }

    @objc func toggleAllowAllApps(_ sender: NSMenuItem) {
        let enabled = !UserDefaults.standard.bool(forKey: "allowAllApps")
        UserDefaults.standard.set(enabled, forKey: "allowAllApps")
        sender.state = enabled ? .on : .off
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }
}

/// Headless check of the save path: `--selftest` posts a throwaway card and
/// exits non-zero if the server does not accept it.
func runSelfTest() -> Never {
    let base = configuredBaseURL()
    let host = base.host?.lowercased() ?? ""
    let loopback = host == "localhost" || host == "::1" || host.hasPrefix("127.")
    guard loopback else {
        print("selftest: refusing to write a test card to \(host); run against 127.0.0.1")
        exit(2)
    }

    var request = URLRequest(url: base.appendingPathComponent("api/capture"))
    request.httpMethod = "POST"
    request.timeoutInterval = 20
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try? JSONSerialization.data(withJSONObject: [
        "front": "selftest front",
        "back": "selftest back",
        "category": "other",
        "selection": "selftest passage",
    ])

    let semaphore = DispatchSemaphore(value: 0)
    var status = 0
    URLSession.shared.dataTask(with: request) { _, response, error in
        if let error {
            print("selftest: request failed: \(error.localizedDescription)")
            status = 1
        } else {
            status = (response as? HTTPURLResponse)?.statusCode ?? 0
            print("selftest: POST /api/capture -> \(status)")
        }
        semaphore.signal()
    }.resume()
    semaphore.wait()
    exit(status == 201 ? 0 : 1)
}

if CommandLine.arguments.contains("--selftest") {
    runSelfTest()
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
