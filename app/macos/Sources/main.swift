// BioQL Studio for macOS — the native shell.
//
// A fork of Agtr Desktop's shell (~/Library/Application Support/AGTR/agtr-os/desktop/macos/Sources/main.swift), cut
// down to what Studio needs: a window, the menus, and the local service it starts. Agtr's Touch ID path, its
// `native.key`, its voice, its quick panel and its global hot key are all gone — see app/README.md for why the
// Touch ID path could not be ported honestly.
//
// NOT COMPILED. There is no Xcode run in this work: this file and its neighbours are correct by inspection only.
import AppKit

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
