# Compatibility

Ember's desktop interface and the game's runtime have separate requirements. Chaos Zero Nightmare is a Windows x64 game.

| Platform | Launcher | Game | Validation |
| --- | --- | --- | --- |
| Linux x86_64 | GTK3 / WebKitGTK 4.1 | Wine | Game sign-in, fresh installation and game control tested on Linux |
| Windows x64 | WebView2 | Native Windows executable | Cross-compiled packages and engine tests under Wine; desktop and game on Windows hardware still pending |
| macOS | WKWebView | Compatible Wine environment required | Experimental; no current release package or confirmed desktop/game test |

## Current download packages

Linux CI builds target Ubuntu 22.04 (glibc 2.35 or newer), with AppImage startup checks configured for Ubuntu 22.04 and Debian 12. Consult the release notes for the result of those checks; they do not verify game execution in either container.

The retained `v0.10.0` Linux packages were built locally and require **glibc 2.44 or newer**. They are not validated for older distributions, including Ubuntu 22.04 or Debian 12. AppImage still requires a compatible glibc and the required host libraries.

Windows packages are unsigned. The NSIS installer can download the official WebView2 bootstrapper. The portable ZIP requires WebView2 to be installed already and must be extracted in full.

New source changes are not included in an existing release unless new artifacts are published. Check the release notes before downloading. GitHub Actions produces new packages; the configured CI platform matrix does not itself prove game compatibility.

## Known limits

- Only Chaos Zero Nightmare is supported.
- Restoring a remembered session has automated tests, but real account restoration in Ember and prolonged sessions need further validation.
- A Linux Secret Service, macOS Keychain or Windows Credential Manager is needed for remembered credentials. A storage failure leaves the current session in memory and displays a notice.
- STOVE protocol or game SDK changes may require an Ember update.
- An automatic country cannot be determined from a language without a territory or from a UTC offset alone. If the system has neither a recognized timezone nor a regional territory, check its regional settings.
- New game updates and platform combinations should be reported with their exact version and environment.

Steam Proton builds (for example Proton-GE) are detected automatically, including Flatpak Steam. They contain 32-bit components, so the host needs 32-bit (`i386`) libraries such as FreeType and GnuTLS; without them the WebView2 installer fails with a TLS error. On Debian/Ubuntu, enable the `i386` architecture and install `libfreetype6:i386` and `libgnutls30t64:i386` (or `libgnutls30:i386`).
